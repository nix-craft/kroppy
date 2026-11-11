# -*- coding: iso-8859-1 -*-

"""
The main window of krop

Copyright (C) 2010-2025 Armin Straub, http://arminstraub.com
"""

"""
This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 3 of the License, or
(at your option) any later version.
"""

import sys
from os.path import exists, splitext
from shutil import which

from krop.qt import *
from krop.config import PYQT6

if PYQT6:
    from krop.mainwindowui_qt6 import Ui_MainWindow
else:
    from krop.mainwindowui_qt5 import Ui_MainWindow

from krop.viewerselections import ViewerSelections, aspectRatioFromStr
from krop.vieweritem import ViewerItem
from krop.pdfcropper import PdfFile, PdfCropper, PdfEncryptedError, optimizePdfGhostscript
from krop.autotrim import autoTrimMargins
from krop.copiertrim import copierTrimMargins
from krop.activitylog import log
from krop.skipregion import skip_rect, EDGES


class AspectRatioType:
    def __init__(self, name, width, height):
        self.name = name
        self.width = width
        self.height = height

class AspectRatioTypeManager:

    def __init__(self):
        self.types = []
        self.defaultsUsed = False

    def __iter__(self):
        return iter(self.types)

    def addType(self, name, width, height):
        self.types.append(AspectRatioType(name, width, height))

    def getType(self, index):
        if index >= len(self.types):
            return None
        return self.types[index]

    def addDefaults(self):
        pass

    def settingsCaption(self):
        pass
  
    def saveTypes(self, settings):
        c = self.settingsCaption()
        # if the defaults have been used, we still write them to the config
        # file, making it easier for the user to edit/add, but we append
        # Defaults to the caption so that these get automatically overriden by
        # future versions of krop (which may have updated defaults)
        if self.defaultsUsed:
            c += "Defaults"
        settings.beginWriteArray(c)
        for i in range(len(self.types)):
            t = self.types[i]
            settings.setArrayIndex(i)
            settings.setValue("Name", t.name)
            settings.setValue("Width", t.width)
            settings.setValue("Height", t.height)
        settings.endArray()
  
    def loadTypes(self, settings):
        count = settings.beginReadArray(self.settingsCaption())
        for i in range(count):
            settings.setArrayIndex(i)
            name = settings.value("Name")
            width = float(settings.value("Width"))
            height = float(settings.value("Height"))
            self.addType(name, width, height)
        settings.endArray()
        if count==0:
            self.defaultsUsed = True
            self.addDefaults()

class SelAspectRatioTypeManager(AspectRatioTypeManager):

    def settingsCaption(self):
        return "SelAspectRatios"

    def addDefaults(self):
        self.addType("Flexible aspect ratio", 0, 0)
        self.addType("A4/A5 etc.", 1, 1.414)
        self.addType("A4/A5 etc. (landscape)", 1.414, 1)
        self.addType("Letter", 8.5, 11)
        self.addType("Letter (landscape)", 11, 8.5)
        self.addType("Legal", 8.5, 14)
        self.addType("Legal (landscape)", 14, 8.5)

class DeviceTypeManager(AspectRatioTypeManager):

    def settingsCaption(self):
        return "DeviceTypes"

    def addDefaults(self):
        self.addType("No fitting", 0, 0)
        self.addType("4:3 eReader", 4, 3)
        self.addType("4:3 eReader (widescreen)", 3, 4)
        self.addType("Nook 1st Ed.", 600, 730)
        self.addType("Nook 1st Ed. (widescreen)", 730, 600)


class MainWindow(QMainWindow):

    fileName = None

    def __init__(self):
        QMainWindow.__init__(self)

        self.selAspectRatioTypes = SelAspectRatioTypeManager()
        self.deviceTypes = DeviceTypeManager()

        self._viewer = ViewerItem(self)

        self.ui=Ui_MainWindow()
        self.ui.setupUi(self)
        self.setWindowTitle("kroppy")
        about = QAction(self.tr("About kroppy"), self)
        menu_role = getattr(QAction, "MenuRole", None)
        about_role = getattr(menu_role, "AboutRole", None) if menu_role else getattr(QAction, "AboutRole", None)
        if about_role is not None:
            about.setMenuRole(about_role)
        about.triggered.connect(self.slotAbout)
        self.menuBar().addMenu(self.tr("&Help")).addAction(about)
        settings_menu = self.menuBar().addMenu(self.tr("&Settings"))
        log_prefs = QAction(self.tr("Run log..."), self)
        log_prefs.triggered.connect(self.slotLogPreferences)
        settings_menu.addAction(log_prefs)
        self._analysis_job = None
        self._analysis_jobs = []
        from krop.analyzepanel import AnalysisPanel
        self.analysisPanel = AnalysisPanel(self)
        self.analysisPanel.page_requested.connect(self.slotShowAnalysisPage)
        self.analysisPanel.focus_changed.connect(self._refocusAnalysis)
        self.analysisPanel.threshold_changed.connect(self._reanalyze)
        self.analysisPanel.highlight_requested.connect(self._showImageHighlight)
        self.analysisPanel.preview_changed.connect(self._updatePreview)
        self.analysisPanel.visibilityChanged.connect(self._analysisDockVisibility)
        self._preview_hold = False
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        try:
            dock_right = Qt.DockWidgetArea.RightDockWidgetArea
        except AttributeError:
            dock_right = Qt.RightDockWidgetArea
        self.addDockWidget(dock_right, self.analysisPanel)
        view_menu = self.menuBar().addMenu(self.tr("&View"))
        self.actionAnalysis = QAction(self.tr("Analysis"), self)
        self.actionAnalysis.setCheckable(True)
        self.actionAnalysis.setChecked(False)
        self.actionAnalysis.toggled.connect(self.analysisPanel.setVisible)
        self.analysisPanel.visibilityChanged.connect(self.actionAnalysis.setChecked)
        view_menu.addAction(self.actionAnalysis)
        self.analysisPanel.hide()

        # these options are awkwardly named and possibly confusing, so they are
        # not shown by default
        self.ui.labelAllowedChanges.hide()
        self.ui.editAllowedChanges.hide()
        self.ui.labelSensitivity.hide()
        self.ui.editSensitivity.hide()

        # self.ui.tabWidget.

        # http://standards.freedesktop.org/icon-naming-spec/icon-naming-spec-latest.html
        self.setWindowIcon(QIcon.fromTheme('edit-cut'))
        self.ui.actionOpenFile.setIcon(QIcon.fromTheme('document-open'))
        self.ui.actionKrop.setIcon(QIcon.fromTheme('face-smile'))
        self.ui.actionZoomIn.setIcon(QIcon.fromTheme('zoom-in'))
        self.ui.actionZoomOut.setIcon(QIcon.fromTheme('zoom-out'))
        self.ui.actionFitInView.setIcon(QIcon.fromTheme('zoom-fit-best'))
        self.ui.actionPreviousPage.setIcon(QIcon.fromTheme('go-previous'))
        self.ui.actionNextPage.setIcon(QIcon.fromTheme('go-next'))
        self.ui.actionFirstPage.setIcon(QIcon.fromTheme('go-first'))
        self.ui.actionLastPage.setIcon(QIcon.fromTheme('go-last'))
        self.ui.actionTrimMargins.setIcon(QIcon.fromTheme('transform-crop'))
        self.ui.actionTrimMarginsAll.setIcon(QIcon.fromTheme('transform-crop'))
        # self.ui.actionTrimMarginsAll.setIcon(QIcon.fromTheme('select-rectangular'))
        # self.ui.actionTrimMarginsAll.setIcon(QIcon.fromTheme('edit-guides'))
        self.ui.actionNewSelection.setIcon(QIcon.fromTheme('select-rectangular'))
        # self.ui.actionNewSelection.setIcon(QIcon.fromTheme('draw-rectangle'))
        self.ui.actionDeleteSelection.setIcon(QIcon.fromTheme('edit-delete'))

        if QIcon.hasThemeIcon('document-open'):
            self.ui.buttonFileSelect.setIcon(QIcon.fromTheme('document-open'))
        else:
            self.ui.buttonFileSelect.setText('...')
            self.ui.buttonFileSelect.setAutoRaise(False)

        if QIcon.hasThemeIcon('go-first') and QIcon.hasThemeIcon('go-previous') \
                and QIcon.hasThemeIcon('go-next') and QIcon.hasThemeIcon('go-last'):
            self.ui.buttonFirst.setIcon(QIcon.fromTheme('go-first'))
            self.ui.buttonPrevious.setIcon(QIcon.fromTheme('go-previous'))
            self.ui.buttonNext.setIcon(QIcon.fromTheme('go-next'))
            self.ui.buttonLast.setIcon(QIcon.fromTheme('go-last'))
        else:
            self.ui.buttonFirst.setText('<<')
            self.ui.buttonPrevious.setText('<')
            self.ui.buttonNext.setText('>')
            self.ui.buttonLast.setText('>>')
            self.ui.buttonFirst.setFlat(False)
            self.ui.buttonPrevious.setFlat(False)
            self.ui.buttonNext.setFlat(False)
            self.ui.buttonLast.setFlat(False)

        # we need to add the action to a widget in order for keyboard shortcuts to work
        self.addAction(self.ui.actionNewSelection)
        self.addAction(self.ui.actionNewSelectionGrid)
        self.addAction(self.ui.actionDeleteSelection)
        self.addAction(self.ui.actionFirstPage)
        self.addAction(self.ui.actionLastPage)

        self.ui.actionOpenFile.triggered.connect(self.slotOpenFile)
        self.ui.actionSelectFile.triggered.connect(self.slotSelectFile)
        self.ui.actionKrop.triggered.connect(self.slotKrop)
        self.ui.actionZoomIn.triggered.connect(self.slotZoomIn)
        self.ui.actionZoomOut.triggered.connect(self.slotZoomOut)
        self.ui.actionFitInView.toggled.connect(self.slotFitInView)
        self.ui.actionPreviousPage.triggered.connect(self.slotPreviousPage)
        self.ui.actionNextPage.triggered.connect(self.slotNextPage)
        self.ui.actionFirstPage.triggered.connect(self.slotFirstPage)
        self.ui.actionLastPage.triggered.connect(self.slotLastPage)
        self.ui.actionDeleteSelection.triggered.connect(self.slotDeleteSelection)
        self.ui.actionNewSelection.triggered.connect(self.slotNewSelection)
        self.ui.actionNewSelectionGrid.triggered.connect(self.slotNewSelectionGrid)
        self.ui.actionTrimMargins.triggered.connect(self.slotTrimMargins)
        self.ui.actionTrimMarginsAll.triggered.connect(self.slotTrimMarginsAll)
        # [trim-all-pages patch] "Trim Margins on all pages" (individual mode)
        self.actionTrimMarginsEveryPage = QAction(self.tr("Trim Margins on all pages"), self)
        self.actionTrimMarginsEveryPage.setObjectName("actionTrimMarginsEveryPage")
        self.actionTrimMarginsEveryPage.setIcon(QIcon.fromTheme('transform-crop'))
        self.actionTrimMarginsEveryPage.setShortcut(self.tr("Ctrl+Shift+T"))
        self.actionTrimMarginsEveryPage.triggered.connect(self.slotTrimMarginsEveryPage)
        _acts = self.ui.toolBar.actions()
        _i = _acts.index(self.ui.actionTrimMarginsAll)
        if _i + 1 < len(_acts):
            self.ui.toolBar.insertAction(_acts[_i+1], self.actionTrimMarginsEveryPage)
        else:
            self.ui.toolBar.addAction(self.actionTrimMarginsEveryPage)
        self.updateTrimEveryPageAction()
        # [copier-trim patch] copier-aware trim + dust filter on the toolbar
        self.checkCopierTrim = QCheckBox(self.tr("Copier trim"), self)
        self.checkCopierTrim.setToolTip(self.tr("Skip the black scanner edge and the white gap, then trim to the page content"))
        self.spinDust = QSpinBox(self)
        self.spinDust.setRange(0, 500)
        self.spinDust.setPrefix(self.tr("Dust "))
        self.spinDust.setSuffix(" px")
        self.spinDust.setToolTip(self.tr("Copier trim: rows/columns with fewer dark pixels than this (in kroppy's preview) count as blank"))
        _s = QSettings()
        self.checkCopierTrim.setChecked(_s.value("Trim/CopierTrim", "true") == "true")
        self.spinDust.setValue(int(_s.value("Trim/Dust", "4")))
        self.checkCopierTrim.toggled.connect(lambda v: QSettings().setValue("Trim/CopierTrim", "true" if v else "false"))
        self.spinDust.valueChanged.connect(lambda v: QSettings().setValue("Trim/Dust", str(v)))
        self.ui.toolBar.addWidget(self.checkCopierTrim)
        self.ui.toolBar.addWidget(self.spinDust)
        # [skip-region patch] minimum white gap that counts as a break between regions
        self.spinMinGap = QSpinBox(self)
        self.spinMinGap.setRange(1, 500)
        self.spinMinGap.setPrefix(self.tr("Min gap "))
        self.spinMinGap.setSuffix(" px")
        self.spinMinGap.setToolTip(self.tr("Skip region: white space must be at least this many rows/columns "
            "(in kroppy's preview) to count as a break; smaller gaps are skipped as part of the same region"))
        self.spinMinGap.setValue(int(_s.value("Skip/MinGap", "1")))
        self.spinMinGap.valueChanged.connect(lambda v: QSettings().setValue("Skip/MinGap", str(v)))
        self.ui.toolBar.addWidget(self.spinMinGap)
        self.ui.documentView.customContextMenuRequested.connect(self.slotContextMenu)
        self.ui.editCurrentPage.textEdited.connect(self.slotCurrentPageEdited)
        self.ui.radioSelAll.toggled.connect(self.slotSelectionMode)
        self.ui.radioSelEvenOdd.toggled.connect(self.slotSelectionMode)
        self.ui.radioSelIndividual.toggled.connect(self.slotSelectionMode)
        #  self.ui.editSelExceptions.editingFinished.connect(self.slotSelExceptionsChanged)
        self.ui.editSelExceptions.textEdited.connect(self.slotSelExceptionsEdited)
        self.ui.comboSelAspectRatioType.currentIndexChanged.connect(self.slotSelAspectRatioTypeChanged)
        self.ui.editSelAspectRatio.editingFinished.connect(self.slotSelAspectRatioChanged)
        self.ui.comboDistributeDevice.currentIndexChanged.connect(self.slotDeviceTypeChanged)
        self.ui.editDistributeAspectRatio.editingFinished.connect(self.slotDistributeAspectRatioChanged)
        self.ui.splitter.splitterMoved.connect(self.slotSplitterMoved)

        self.pdfScene = QGraphicsScene(self.ui.documentView)
        self.pdfScene.setBackgroundBrush(self.pdfScene.palette().dark())
        self.pdfScene.addItem(self.viewer)

        self.readSettings()

        # populate combobox with aspect ratio types
        for t in self.selAspectRatioTypes:
            self.ui.comboSelAspectRatioType.addItem(t.name)
        self.ui.comboSelAspectRatioType.addItem("Custom")
        # populate combobox with device types
        for t in self.deviceTypes:
            self.ui.comboDistributeDevice.addItem(t.name)
        self.ui.comboDistributeDevice.addItem("Custom")

        # disable Ghostscript option if gs is not available
        if not which('gs'):
            self.ui.checkGhostscript.setChecked(False)
            self.ui.checkGhostscript.setEnabled(False)

        self.ui.documentView.setScene(self.pdfScene)
        try:
            smooth = QPainter.RenderHint.SmoothPixmapTransform
        except AttributeError:
            smooth = QPainter.SmoothPixmapTransform
        self.ui.documentView.setRenderHint(smooth, True)
        self.ui.documentView.setFocus()


    @property
    def viewer(self):
        return self._viewer

    @property
    def selections(self):
        return self.viewer.selections

    def currentSelectionUpdated(self):
        sel = self.selections.currentSelection
        if sel:
            r = sel.boundingRect()
            self.ui.groupCurrentSel.setEnabled(True)
            index, s = sel.aspectRatioData
            self.ui.comboSelAspectRatioType.setCurrentIndex(index)
            self.ui.editSelAspectRatio.setText(s)
            if index == 0:
                w, h = int(r.width()), int(r.height())
                self.ui.editSelAspectRatio.setText("{} : {}".format(w, h))
        else:
            self.ui.comboSelAspectRatioType.setCurrentIndex(0)
            self.ui.editSelAspectRatio.setText("")
            self.ui.groupCurrentSel.setEnabled(False)

    def readSettings(self):
        settings = QSettings()
        geometry = settings.value("Window/Geometry", "")
        if geometry:
            self.restoreGeometry(geometry)
        splitter = settings.value("Window/Splitter", "")
        if splitter:
            self.ui.splitter.restoreState(splitter)
        self.ui.actionFitInView.setChecked(settings.value("Window/FitInView", "") == "true")
        if settings.value("Window/Analysis", "") == "true":
            self.actionAnalysis.setChecked(True)

        self.ui.checkTrimUseAllPages.setChecked(settings.value("Trim/UseAllPages", "") == "true")
        self.ui.editPadding.setText(
                settings.value("Trim/Padding", "2"))
        self.ui.editAllowedChanges.setText(
                settings.value("Trim/AllowedChanges", "0"))
        self.ui.editSensitivity.setText(
                settings.value("Trim/Sensitivity", "5"))

        self.ui.checkGhostscript.setChecked(settings.value("PDF/Optimize", "gs") == "gs")
        self.ui.checkHardCrop.setChecked(settings.value("PDF/HardCrop", "") == "true")
        self.ui.checkIncludePagesWithoutSelections.setChecked(
                settings.value("PDF/IncludePagesWithoutSelections", "") == "true")

        self.selAspectRatioTypes.loadTypes(settings)
        self.deviceTypes.loadTypes(settings)

    def writeSettings(self):
        settings = QSettings()
        settings.setValue("Window/Geometry", self.saveGeometry())
        settings.setValue("Window/Splitter", self.ui.splitter.saveState())
        settings.setValue("Window/FitInView", "true" if
                self.ui.actionFitInView.isChecked() else "false")
        settings.setValue("Window/Analysis", "true" if
                self.analysisPanel.isVisible() else "false")

        settings.setValue("Trim/UseAllPages", "true" if
                self.ui.checkTrimUseAllPages.isChecked() else "false")
        settings.setValue("Trim/Padding",
                self.ui.editPadding.text())
        settings.setValue("Trim/AllowedChanges",
                self.ui.editAllowedChanges.text())
        settings.setValue("Trim/Sensitivity",
                self.ui.editSensitivity.text())

        settings.setValue("PDF/Optimize", "gs" if
                self.ui.checkGhostscript.isChecked() else "no")
        settings.setValue("PDF/HardCrop", "true" if
                self.ui.checkHardCrop.isChecked() else "false")
        settings.setValue("PDF/IncludePagesWithoutSelections", "true" if
                self.ui.checkIncludePagesWithoutSelections.isChecked() else "false")

        self.selAspectRatioTypes.saveTypes(settings)
        self.deviceTypes.saveTypes(settings)

    def makeLogPreferencesDialog(self):
        """Dialog for where the run log goes and when it is deleted."""
        from krop.activitylog import user_log_dir
        settings = QSettings()
        keep = settings.value("Log/Keep", "on-error") or "on-error"
        place = settings.value("Log/Location", "beside") or "beside"
        dlg = QDialog(self)
        dlg.setWindowTitle(self.tr("Run log"))
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(self.tr("When to keep the log")))
        keep_group = QButtonGroup(dlg)
        keep_buttons = {}
        for key, label in (
                ("on-error", self.tr("Delete after clean save (default)")),
                ("always", self.tr("Always keep")),
                ("off", self.tr("Off (no log)"))):
            button = QRadioButton(label)
            button.setObjectName("logKeep-" + key)
            keep_group.addButton(button)
            layout.addWidget(button)
            keep_buttons[key] = button
        keep_buttons.get(keep, keep_buttons["on-error"]).setChecked(True)
        layout.addWidget(QLabel(self.tr("Where to write it")))
        place_group = QButtonGroup(dlg)
        place_buttons = {}
        for key, label in (
                ("beside", self.tr("Beside the PDF")),
                ("central", self.tr("Central folder (%s)") % user_log_dir())):
            button = QRadioButton(label)
            button.setObjectName("logPlace-" + key)
            place_group.addButton(button)
            layout.addWidget(button)
            place_buttons[key] = button
        place_buttons.get(place, place_buttons["beside"]).setChecked(True)
        try:
            ok = QDialogButtonBox.StandardButton.Ok
            cancel = QDialogButtonBox.StandardButton.Cancel
        except AttributeError:
            ok = QDialogButtonBox.Ok
            cancel = QDialogButtonBox.Cancel
        buttons = QDialogButtonBox(ok | cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg._keep_buttons = keep_buttons
        dlg._place_buttons = place_buttons
        return dlg

    def applyLogPreferencesDialog(self, dlg):
        keep = "on-error"
        for key, button in dlg._keep_buttons.items():
            if button.isChecked():
                keep = key
        place = "beside"
        for key, button in dlg._place_buttons.items():
            if button.isChecked():
                place = key
        settings = QSettings()
        settings.setValue("Log/Keep", keep)
        settings.setValue("Log/Location", place)
        settings.sync()
        from krop.activitylog import apply_preferences
        apply_preferences()

    def slotLogPreferences(self):
        dlg = self.makeLogPreferencesDialog()
        runner = getattr(dlg, "exec", None) or getattr(dlg, "exec_")
        if runner():
            self.applyLogPreferencesDialog(dlg)

    def slotAbout(self):
        from krop.version import __version__
        QMessageBox.about(self, self.tr("About kroppy"),
                self.tr("<h3>kroppy %s</h3>"
                    "<p>A tool to crop PDF files.</p>") % __version__)

    def openFile(self, fileName, recoverCrops=None):
        if fileName:
            from krop.activitylog import attach_input
            fallback = attach_input(fileName)
            if fallback:
                from krop.activitylog import log_where
                if log_where() == "fallback":
                    self.statusBar().showMessage(self.tr(
                        "Could not write a log beside the PDF. Logging to %s") % fallback)
                else:
                    self.statusBar().showMessage(self.tr("Run log: %s") % fallback)
            log("OPEN", fileName)
            self.startAnalysis(fileName)
            if recoverCrops is None:
                recoverCrops = self.recoverCrops
            recovered = None
            if recoverCrops:
                try:
                    from krop.recovercrop import expandCropped
                    recovered = expandCropped(fileName)
                except Exception as e:
                    sys.stderr.write("kroppy: could not recover crop boxes: %r\n" % e)
            self.viewer.load(recovered[0] if recovered else fileName)
            if not self.viewer.isEmpty():
                # crop from the full-page copy, but name output after the original
                self.fileName = recovered[0] if recovered else fileName
                self.originalFileName = fileName
                outputFileName = "%s-cropped.pdf" % splitext(fileName)[0]
                if recovered:
                    self.applyRecoveredCrops(recovered[1])
                self.slotFitInView(self.ui.actionFitInView.isChecked())
            else:
                self.fileName = ''
                outputFileName = ''
                self.showWarning(self.tr("Something got in our way"),
                        self.tr("The PDF file couldn't be read. "
                            "Please check the file and its permissions."))
            self.setWindowFilePath(fileName if self.fileName else '')
            self.ui.actionKrop.setEnabled(not self.viewer.isEmpty())
            self.ui.actionTrimMarginsAll.setEnabled(not self.viewer.isEmpty())
            self.updateTrimEveryPageAction()
            self.ui.editFile.setText(outputFileName)
            self.updateControls()

    def applyRecoveredCrops(self, crops):
        """Show each page's previous crop as an individual, editable selection."""
        sels = self.selections
        sels.deleteSelections()
        self.ui.radioSelIndividual.setChecked(True)
        sels.selectionMode = ViewerSelections.individual
        cur = self.viewer.currentPageIndex
        for idx, (l, t, r, b) in enumerate(crops):
            self.viewer.currentPageIndex = idx
            img = self.viewer.getImage(idx)
            w, h = img.width(), img.height()
            s = sels.addSelection()
            s.pageIndex = idx
            s.rect = s.mapRectFromImage(QRectF(l*w, t*h, w*(1-l-r), h*(1-t-b)))
            s.adjustBoundingRect()
        self.viewer.currentPageIndex = cur if cur < len(crops) else 0
        sels.updateSelectionVisibility()

    @property
    def recoverCrops(self):
        return QSettings().value("Open/RecoverCrops", "true") != "false"

    def slotOpenFile(self):
        # non-native dialog so we can add the "recover crop boxes" checkbox
        dlg = QFileDialog(self, self.tr("Open PDF"), "",
                self.tr("PDF Files (*.pdf *.PDF);;All Files (*)"))
        dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        check = QCheckBox(self.tr("Show existing crop boxes as editable selections (open at full page size)"), dlg)
        check.setChecked(self.recoverCrops)
        lay = dlg.layout()
        try:
            lay.addWidget(check, lay.rowCount(), 0, 1, -1)
        except TypeError:
            lay.addWidget(check)
        if not dlg.exec():
            return
        QSettings().setValue("Open/RecoverCrops", "true" if check.isChecked() else "false")
        files = dlg.selectedFiles()
        if files:
            self.openFile(files[0], check.isChecked())

    def slotSelectFile(self):
        fileName = QFileDialog.getSaveFileName(self,
                self.tr("Save cropped PDF to ..."), "", self.tr("PDF Files (*.pdf *.PDF)"))
                # None, QFileDialog.DontConfirmOverwrite)
        try:
            self.ui.editFile.setText(fileName)
        except TypeError:
            # new versions of Qt return a tuple (fileName, selectedFilter)
            self.ui.editFile.setText(fileName[0])

    def showWarning(self, title, text):
        # if krop is called with parameter --go, then the main window is never
        # shown; in that case, we output the warning to the shell
        log("WARN", "%s: %s" % (title, text.replace("\n", " | ")))
        if self.isVisible():
            QMessageBox.warning(self, title, text)
        else:
            sys.stderr.write(self.tr('WARNING: ') + title + '\n' + text + '\n')

    def str2pages(self, s):
        pages = []
        intervals = [ [ n.strip() for n in i.split('-') ]
                for i in s.split(',') ]
        for i in intervals:
            a,b = i[0], i[-1]
            if a:
                if a == 'current':
                    a = self.viewer.currentPageIndex + 1
                if b == 'current':
                    b = self.viewer.currentPageIndex + 1
                if not b: b = self.viewer.numPages()
                pages.extend(range(int(a)-1,int(b))) # subtract 1 because pages are counted from 0 internally
        return pages

    def slotKrop(self):
        # file names
        inputFileName = self.fileName
        outputFileName = self.ui.editFile.text()
        log("SAVE", "output %s hard-crop=%s ghostscript=%s" % (
            outputFileName, self.ui.checkHardCrop.isChecked(),
            self.ui.checkGhostscript.isChecked()))

        # which pages
        s = self.ui.editWhichPages.text()
        if not s:
            pages = range(self.viewer.numPages())
        else:
            pages = self.str2pages(s)

        alwaysinclude = self.ui.checkIncludePagesWithoutSelections.isChecked()

        rotation = [0, 270, 90, 180][self.ui.comboRotation.currentIndex()]

        # Done when selecting filename.
        # if exists(outputFileName):
        #     self.showWarning(self.tr("Overwrite File?"),
        #             self.tr("A file named \"...\" already exists. Are you sure you want to overwrite it?"))
        #     return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            pdf = PdfFile()
            pdf.loadFromFile(inputFileName)
            cropper = PdfCropper()
            cropper.copyDocumentRoot(pdf)
            for nr in pages:
                c = self.viewer.cropValues(nr)
                cropper.addPageCropped(pdf, nr, c, alwaysinclude, rotation)
            if self.ui.checkGhostscript.isChecked():
                import tempfile, os
                with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as fp:
                    cropper.writeToStream(fp.file)
                    # we close the file because it depends on the platform
                    # whether it can be read a second time while still open
                    fp.close()
                    optimizePdfGhostscript(fp.name, outputFileName)
                    os.remove(fp.name)
            else:
                cropper.writeToFile(outputFileName)
            if self.ui.checkHardCrop.isChecked():
                self.hardCropFile(outputFileName)
            from krop.activitylog import note_clean_save
            note_clean_save()
            QApplication.restoreOverrideCursor()
        except PdfEncryptedError as err:
            QApplication.restoreOverrideCursor()
            import traceback
            log("ERROR", traceback.format_exc().rstrip())
            self.showWarning(self.tr("PDF is encrypted"),
                    self.tr("This PDF needs to be decrypted before cropping. "
                        "You could try to do that using qpdf:"
                        "\nqpdf --password='hello' --decrypt encrypted.pdf decrypted.pdf"))
            raise err
        except IOError as err:
            QApplication.restoreOverrideCursor()
            import traceback
            log("ERROR", traceback.format_exc().rstrip())
            self.showWarning(self.tr("Could not write cropped PDF"),
                    self.tr("An error occured while writing the cropped PDF. "
                        "Please make sure that you have permission to write to "
                        "the selected file."
                        "\n\nThe official error is:\n\n{0}").format(err))
        except Exception as err:
            QApplication.restoreOverrideCursor()
            import traceback
            log("ERROR", traceback.format_exc().rstrip())
            msg = str(err)
            # PyMuPDF reports a missing output path as FzErrorSystem, not IOError.
            if "cannot open file" in msg.lower() or "permission denied" in msg.lower():
                self.showWarning(self.tr("Could not write cropped PDF"),
                        self.tr("An error occured while writing the cropped PDF. "
                            "Please make sure that you have permission to write to "
                            "the selected file."
                            "\n\nThe official error is:\n\n{0}").format(err))
            else:
                self.showWarning(self.tr("Something got in our way"),
                        self.tr("The following unexpected error has occured:"
                        "\n\n{0}").format(err))
                raise err

    def hardCropFile(self, outputFileName):
        """Physically crop embedded images to the visible box. Never fails the save."""
        try:
            from krop.hardcrop import hardcrop_file
            hardcrop_file(outputFileName)
        except Exception as err:
            log("HARD", "hard crop failed: %r" % err)
            sys.stderr.write("kroppy: hard crop failed (%s); output left as cropped by kroppy\n" % err)

    def slotZoomIn(self):
        self.ui.actionFitInView.setChecked(False)
        self.ui.documentView.scale(1.2, 1.2)

    def slotZoomOut(self):
        self.ui.actionFitInView.setChecked(False)
        self.ui.documentView.scale(1/1.2, 1/1.2)

    def slotFitInView(self, checked):
        if checked:
            self.ui.documentView.fitInView(self.pdfScene.sceneRect(),
                    Qt.AspectRatioMode.KeepAspectRatio)

    def slotSplitterMoved(self, pos, idx):
        self.slotFitInView(self.ui.actionFitInView.isChecked())

    def slotPreviousPage(self):
        self.viewer.previousPage()
        self.updateControls()

    def slotNextPage(self):
        self.viewer.nextPage()
        self.updateControls()

    def slotFirstPage(self):
        self.viewer.firstPage()
        self.updateControls()

    def slotLastPage(self):
        self.viewer.lastPage()
        self.updateControls()

    def slotCurrentPageEdited(self, text):
        try:
            n = int(text)
            self.viewer.currentPageIndex = n-1
            self.updateControls()
        except ValueError:
            pass

    def updateControls(self):
        cur = ''
        num = ''
        if not self.viewer.isEmpty():
            cur = str(self.viewer.currentPageIndex+1)
            num = str(self.viewer.numPages())
        self.ui.editCurrentPage.setText(cur)
        self.ui.editMaxPage.setText(num)

    def slotSelectionMode(self, checked):
        if checked:
            enableExceptions = True
            if self.ui.radioSelAll.isChecked():
                self.selections.selectionMode = ViewerSelections.all
            elif self.ui.radioSelEvenOdd.isChecked():
                self.selections.selectionMode = ViewerSelections.evenodd
            elif self.ui.radioSelIndividual.isChecked():
                enableExceptions = False
                self.selections.selectionMode = ViewerSelections.individual
            self.ui.editSelExceptions.setEnabled(enableExceptions)
            self.updateTrimEveryPageAction()

    def slotSelExceptionsChanged(self):
        s = self.ui.editSelExceptions.text()
        pages = self.str2pages(s)
        self.selections.selectionExceptions = pages

    def slotSelExceptionsEdited(self, text):
        try:
            pages = self.str2pages(text)
            self.selections.selectionExceptions = pages
        except ValueError:
            pass


    def slotSelAspectRatioChanged(self):
        sel = self.selections.currentSelection
        if sel:
            index = self.ui.comboSelAspectRatioType.currentIndex()
            # index=0: flexible
            # index=last: custom
            s = self.ui.editSelAspectRatio.text()
            sel.aspectRatioData = [index, s]

    def slotSelAspectRatioTypeChanged(self, index):
        sel = self.selections.currentSelection
        t = self.selAspectRatioTypes.getType(index)
        # index=0: flexible
        # t=None: custom
        ar = ""
        if index == 0 or t is None:
            if sel:
                r = sel.boundingRect()
                w, h = int(r.width()), int(r.height())
                ar = "{} : {}".format(w, h)
        elif t is not None:
            ar = "{} : {}".format(t.width, t.height)
        self.ui.editSelAspectRatio.setEnabled(t is None)
        self.ui.editSelAspectRatio.setText(ar)
        if sel:
            s = self.ui.editSelAspectRatio.text()
            sel.aspectRatioData = [index, s]


    def distributeAspectRatioChanged(self, aspectRatio):
        self.selections.distributeAspectRatio = aspectRatio

    def slotDistributeAspectRatioChanged(self):
        self.selections.distributeAspectRatio = aspectRatioFromStr(self.ui.editDistributeAspectRatio.text())

    def slotDeviceTypeChanged(self, index):
        t = self.deviceTypes.getType(index)
        ar = t and "%s : %s" % (t.width, t.height) or "w : h"
        self.ui.editDistributeAspectRatio.setEnabled(t is None)
        self.ui.editDistributeAspectRatio.setText(ar)
        self.slotDistributeAspectRatioChanged()

    def slotContextMenu(self, pos):
        if self.viewer.isEmpty():
            return

        item = self.ui.documentView.itemAt(pos)
        menuForSelection= False
        try:
            self.selections.currentSelection = item.selection
            menuForSelection= True
        except AttributeError:
            pass
        popMenu = QMenu()
        popMenu.addAction(self.ui.actionNewSelection)
        popMenu.addAction(self.ui.actionNewSelectionGrid)
        if menuForSelection:
            popMenu.addAction(self.ui.actionDeleteSelection)
            popMenu.addAction(self.ui.actionTrimMargins)
            self.addSkipRegionMenu(popMenu, item, pos)
        else:
            popMenu.addAction(self.ui.actionTrimMarginsAll)
            popMenu.addAction(self.actionTrimMarginsEveryPage)
        if getattr(self, '_skipUndo', None):
            popMenu.addAction(self.tr("Undo last skip region"), self.slotUndoSkipRegion)
        popMenu.exec(self.ui.documentView.mapToGlobal(pos))

    def slotDeleteSelection(self):
        if self.selections.currentSelection is not None:
            self.selections.deleteSelection(self.selections.currentSelection)

    def slotNewSelection(self):
        self.createSelectionGrid("1")

    def slotNewSelectionGrid(self):
        if not self.viewer.isEmpty():
            default = "2x1"
            if self.viewer.isPortrait():
                default = "1x2"
            grid, ok = QInputDialog.getText(self, self.tr('New Selection Grid...'),
                    self.tr('Enter the dimensions of the grid:'), text=default)
            if ok:
                self.createSelectionGrid(grid)

    def createSelectionGrid(self, grid):
        if self.viewer.isEmpty():
            return

        try:
            colsrows = [int(x) for x in grid.split('x')]
            cols = colsrows[0]
            # if only one value is specified, we determine the number of
            # columns/rows according to whether the page is landscape or
            # portrait
            if len(colsrows) == 1:
                if self.viewer.isPortrait():
                    cols, rows = 1, cols
                else:
                    rows = 1
            else:
                rows = colsrows[1]
        except:
            self.showWarning(self.tr("Bad value for grid parameter"), self.tr("For creating a grid "
                "of selections, you need to specify the dimensions of the grid in the form '2x3'. "
                "You can also enter a single number, in which case the number of columns/rows is "
                "determined according to whether the page is landscape or portrait."))
            return

        for j in range(rows):
            for i in range(cols):
                sel = self.selections.addSelection()
                r = sel.boundingRect()
                w = r.width()/cols
                h = r.height()/rows
                p0 = QPointF(r.left()+i*w, r.top()+j*h)
                sel.setBoundingRect(p0, p0 + QPointF(w, h))
        self.pdfScene.update()

    def getPadding(self):
        """Return [top, right, bottom, left] tuple specifying padding for trimming margins."""
        try:
            # padding can be specified as in CSS (using one to four values):
            # top, right, bottom, left
            # top, right+left, bottom
            # top+bottom, right+left
            # top+bottom+right+left
            padding = [ float(a) for a in self.ui.editPadding.text().split(',') if a ]
            if len(padding) == 0:
                return [0,0,0,0]
            if len(padding) == 1:
                return 4*padding
            if len(padding) == 2:
                return 2*padding
            if len(padding) == 3:
                return padding + [padding[1]]
            if len(padding) == 4:
                return padding
            raise ValueError
        except ValueError:
            self.showWarning(self.tr("Bad value for padding"), self.tr("The value of padding "
                "(under settings for trimming margins) must be a list of one to four floats, "
                "separated by a comma."
                "\n\nAs in CSS, options are: "
                "\n(four values) top, right, bottom, left"
                "\n(three values) top, right+left, bottom"
                "\n(two values) top+bottom, right+left"))
            return [0,0,0,0]

    def slotTrimMarginsAll(self):
        # trim margins of all selections on the current page
        noSelections = True
        for sel in self.selections.items:
            if sel.isVisible():
                noSelections = False
                self.trimMarginsSelection(sel)
        # if there is no selections, then create one
        if noSelections and not self.viewer.isEmpty():
            sel = self.selections.addSelection()
            self.trimMarginsSelection(sel)
        self.pdfScene.update()

    def updateTrimEveryPageAction(self):
        # [trim-all-pages patch]
        act = getattr(self, 'actionTrimMarginsEveryPage', None)
        if act is None:
            return
        individual = self.ui.radioSelIndividual.isChecked()
        act.setEnabled(individual and not self.viewer.isEmpty())
        if individual:
            act.setToolTip(self.tr("Trim margins on every page, giving each page its own crop box"))
        else:
            act.setToolTip(self.tr("Only available when selections apply to individual pages "
                "(Basic tab: 'Individual pages')"))

    def slotTrimMarginsEveryPage(self):
        # [trim-all-pages patch] for every page, trim its own selections (or
        # create one) using only that page's image
        if self.viewer.isEmpty() or not self.ui.radioSelIndividual.isChecked():
            return
        n = self.viewer.numPages()
        orig = self.viewer.currentPageIndex
        useAll = self.ui.checkTrimUseAllPages.isChecked()
        self.ui.checkTrimUseAllPages.setChecked(False)
        progress = QProgressDialog(self.tr("Trimming margins..."), self.tr("Cancel"), 0, n, self)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(500)
        try:
            for idx in range(n):
                progress.setValue(idx)
                QApplication.processEvents()
                if progress.wasCanceled():
                    break
                self.viewer.currentPageIndex = idx
                sels = [s for s in self.selections.items if s.selectionVisibleOnPage(idx)]
                if not sels:
                    sels = [self.selections.addSelection()]
                for sel in sels:
                    self.trimMarginsSelection(sel)
            progress.setValue(n)
        finally:
            self.ui.checkTrimUseAllPages.setChecked(useAll)
            self.viewer.currentPageIndex = orig
            self.pdfScene.update()
            self.updateControls()

    # [skip-region patch] right-click > "Skip next region inward"
    SKIP_SCOPES = ("page", "toend", "all")

    def nearestEdge(self, item, pos):
        from krop.viewerselections import SelectionHandleItem
        roles = {SelectionHandleItem.TopHandle: "top", SelectionHandleItem.BottomHandle: "bottom",
                 SelectionHandleItem.LeftHandle: "left", SelectionHandleItem.RightHandle: "right"}
        if isinstance(item, SelectionHandleItem):
            return roles[item.role]
        sel = item.selection
        pt = sel.mapFromScene(self.ui.documentView.mapToScene(pos))
        r = sel.rect
        d = {"top": abs(pt.y()-r.top()), "bottom": abs(pt.y()-r.bottom()),
             "left": abs(pt.x()-r.left()), "right": abs(pt.x()-r.right())}
        return min(d, key=d.get)

    def addSkipRegionMenu(self, popMenu, item, pos):
        sel = item.selection
        near = self.nearestEdge(item, pos)
        m = popMenu.addMenu(self.tr("Skip next region inward"))
        names = {"top": self.tr("Top edge"), "bottom": self.tr("Bottom edge"),
                 "left": self.tr("Left edge"), "right": self.tr("Right edge")}
        order = [near] + [e for e in EDGES if e != near]
        for e in order:
            label = names[e] + (self.tr(" (nearest)") if e == near else "")
            a = m.addAction(label, lambda e=e: self.skipRegion(sel, [e]))
            if e == near:
                nearAct = a
        # highlight the nearest edge when the submenu opens, so Enter picks it
        m.aboutToShow.connect(lambda: QTimer.singleShot(0, lambda: m.setActiveAction(nearAct)))
        m.addAction(self.tr("All four edges"), lambda: self.skipRegion(sel, list(EDGES)))
        m.addSeparator()
        grp = QActionGroup(m)
        cur = self.skipScope
        for key, label in zip(self.SKIP_SCOPES, (self.tr("This page only"),
                self.tr("This page to end"), self.tr("All pages"))):
            a = m.addAction(label)
            a.setCheckable(True)
            a.setChecked(key == cur)
            grp.addAction(a)
            a.triggered.connect(lambda _=False, k=key: setattr(self, 'skipScope', k))
        if getattr(self, '_skipUndo', None):
            m.addSeparator()
            m.addAction(self.tr("Undo last skip region"), self.slotUndoSkipRegion)
        return m

    def getSkipScope(self):
        v = QSettings().value("Skip/Scope", "page")
        return v if v in self.SKIP_SCOPES else "page"

    def setSkipScope(self, v):
        QSettings().setValue("Skip/Scope", v)

    skipScope = property(getSkipScope, setSkipScope)

    def _selFrac(self, sel):
        r, p = sel.mapRectToParent(sel.rect), self.viewer.irect
        return ((r.left()-p.left())/p.width(), (r.top()-p.top())/p.height(),
                r.width()/p.width(), r.height()/p.height())

    def _setSelFrac(self, sel, f):
        p = self.viewer.irect
        r = QRectF(p.left()+f[0]*p.width(), p.top()+f[1]*p.height(), f[2]*p.width(), f[3]*p.height())
        r = sel.mapRectFromParent(r)
        sel.setBoundingRect(r.topLeft(), r.bottomRight())

    def _splitToIndividual(self, undo):
        """Give every page its own copy of each shared selection."""
        sels = self.selections
        n = self.viewer.numPages()
        orig = self.viewer.currentPageIndex
        undo["mode"] = (sels.selectionMode, sels.selectionExceptions)
        shared = [(s, [i for i in range(n) if s.selectionVisibleOnPage(i)]) for s in sels.items]
        sels.selectionExceptions = []
        self.ui.radioSelIndividual.setChecked(True)
        sels.selectionMode = ViewerSelections.individual
        for s, pages in shared:
            self.viewer.currentPageIndex = s.pageIndex
            f = self._selFrac(s)
            for i in pages:
                if i == s.pageIndex:
                    continue
                self.viewer.currentPageIndex = i
                c = sels.addSelection()
                c.pageIndex = i
                c.aspectRatioData = list(s.aspectRatioData)
                self._setSelFrac(c, f)
                undo["created"].append(c)
        self.viewer.currentPageIndex = orig
        sels.updateSelectionVisibility()
        log("SKIP", "split shared selections into individual per-page selections (%d created)" % len(undo["created"]))

    def _matchSel(self, f, idx):
        """Selection on page idx that best overlaps fraction-rect f."""
        best, bo = None, -1.0
        for s in self.selections.items:
            if not s.selectionVisibleOnPage(idx):
                continue
            g = self._selFrac(s)
            ow = min(f[0]+f[2], g[0]+g[2]) - max(f[0], g[0])
            oh = min(f[1]+f[3], g[1]+g[3]) - max(f[1], g[1])
            o = max(0, ow) * max(0, oh)
            if o > bo:
                best, bo = s, o
        return best

    def skipRegion(self, sel, edges, scope=None, n=1):
        if self.viewer.isEmpty() or sel is None:
            return
        scope = scope or self.skipScope
        npages = self.viewer.numPages()
        cur = self.viewer.currentPageIndex
        pages = {"page": [cur], "toend": list(range(cur, npages)), "all": list(range(npages))}[scope]
        undo = {"rects": [], "created": [], "mode": None, "page": cur}
        log("SKIP", "edges=%s scope=%s pages=%d-%d" % (",".join(edges), scope, pages[0]+1, pages[-1]+1))
        if npages > 1 and self.selections.selectionMode != ViewerSelections.individual:
            self._splitToIndividual(undo)
            if not sel.selectionVisibleOnPage(cur):
                # the shared selection now belongs to its own page; use this page's copy
                sel = self._matchSel(self._selFrac(sel), cur) or sel
        f0 = self._selFrac(sel)
        multi = len(pages) > 1
        progress = None
        if multi:
            progress = QProgressDialog(self.tr("Skipping regions..."), self.tr("Cancel"), 0, len(pages), self)
            progress.setWindowModality(Qt.WindowModality.WindowModal)
            progress.setMinimumDuration(500)
        dust = self.spinDust.value() if getattr(self, 'spinDust', None) is not None else 4
        moved = 0
        pad = self.getPadding() or [0, 0, 0, 0]   # [top, right, bottom, left]
        mingap = self.spinMinGap.value() if getattr(self, 'spinMinGap', None) is not None else 1
        log("SKIP", "padding t,r,b,l = %s, min gap %d px, dust %d" % (pad, mingap, dust))
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for k, idx in enumerate(pages):
                if progress is not None:
                    progress.setValue(k)
                    QApplication.processEvents()
                    if progress.wasCanceled():
                        log("SKIP", "canceled at page %d" % (idx+1))
                        break
                self.viewer.currentPageIndex = idx
                s = sel if idx == cur else self._matchSel(f0, idx)
                if s is None:
                    s = self.selections.addSelection()
                    s.pageIndex = idx
                    self._setSelFrac(s, f0)
                    undo["created"].append(s)
                else:
                    undo["rects"].append((s, idx, self._selFrac(s)))
                img = self.viewer.getImage(idx)
                for e in edges:
                    orect = s.mapRectToImage(s.rect).toRect()
                    nrect, d = skip_rect(img, orect, e, n, dust, mingap)
                    if nrect is None:
                        log("SKIP", "page %d %s: no further ink, unchanged" % (idx+1, e))
                        continue
                    o = {"top": orect.top(), "left": orect.left(),
                         "bottom": orect.top()+orect.height(), "right": orect.left()+orect.width()}[e]
                    nv = {"top": nrect.top(), "left": nrect.left(),
                          "bottom": nrect.top()+nrect.height(), "right": nrect.left()+nrect.width()}[e]
                    # back off by the app's Padding field (same units as Trim), never past the old edge
                    nf = QRectF(nrect)
                    if e == "top":
                        nf.setTop(max(orect.top(), nf.top() - pad[0]))
                    elif e == "right":
                        nf.setRight(min(orect.left() + orect.width(), nf.right() + pad[1]))
                    elif e == "bottom":
                        nf.setBottom(min(orect.top() + orect.height(), nf.bottom() + pad[2]))
                    else:
                        nf.setLeft(max(orect.left(), nf.left() - pad[3]))
                    r = s.mapRectFromImage(nf)
                    s.setBoundingRect(r.topLeft(), r.bottomRight())
                    moved += 1
                    log("SKIP", "page %d %s: %d -> %d (%d px)" % (idx+1, e, o, nv, d))
                self.pdfScene.update()
                if multi:
                    QApplication.processEvents()
            if progress is not None:
                progress.setValue(len(pages))
        finally:
            QApplication.restoreOverrideCursor()
            self.viewer.currentPageIndex = cur
            self.selections.updateSelectionVisibility()
            if sel.scene() is not None:
                self.selections.currentSelection = sel
            self.pdfScene.update()
            self.updateControls()
        self._skipUndo = undo
        return moved

    def slotUndoSkipRegion(self):
        undo = getattr(self, '_skipUndo', None)
        if not undo:
            return
        self._skipUndo = None
        orig = self.viewer.currentPageIndex
        for s, idx, f in undo["rects"]:
            self.viewer.currentPageIndex = idx
            self._setSelFrac(s, f)
        for s in undo["created"]:
            if s in self.selections.items:
                self.selections.deleteSelection(s)
        if undo["mode"] is not None:
            mode, exc = undo["mode"]
            {ViewerSelections.all: self.ui.radioSelAll, ViewerSelections.evenodd: self.ui.radioSelEvenOdd,
             ViewerSelections.individual: self.ui.radioSelIndividual}[mode].setChecked(True)
            self.selections.selectionMode = mode
            self.selections.selectionExceptions = exc
        self.viewer.currentPageIndex = orig
        self.selections.updateSelectionVisibility()
        self.pdfScene.update()
        log("SKIP", "undo: restored %d selection(s), removed %d" % (len(undo["rects"]), len(undo["created"])))

    def slotTrimMargins(self):
        if self.selections.currentSelection is not None:
            self.trimMarginsSelection(self.selections.currentSelection)
            self.pdfScene.update()

    def trimMarginsSelection(self, sel):
        sensitivity = float(self.ui.editSensitivity.text())
        allowedchanges = float(self.ui.editAllowedChanges.text())

        # if requested, use all pages for trimming; otherwise, just the current page
        pages = [self.viewer.currentPageIndex]
        if self.ui.checkTrimUseAllPages.isChecked():
            pages = [i for i in range(self.viewer.numPages()) if sel.selectionVisibleOnPage(i)]

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            # orect is the original selection, nrect is the trimmed version
            orect = sel.mapRectToImage(sel.rect).toRect()
            nrect = None
            for idx in pages:
                # calculate values for trimming
                img = self.viewer.getImage(idx)
                if getattr(self, 'checkCopierTrim', None) is not None and self.checkCopierTrim.isChecked():
                    nrect = copierTrimMargins(img, orect, nrect, dust=self.spinDust.value())
                else:
                    nrect = autoTrimMargins(img, orect, nrect, sensitivity, allowedchanges)

            orect = QRectF(orect)
            nrect = QRectF(nrect)

            # adjust for padding ...
            dtop, dright, dbottom, dleft = self.getPadding()
            nrect.adjust(-dleft, -dtop, dright, dbottom)
            # ... but don't overadjust
            nrect = nrect.intersected(orect)

            # take fixed aspect ratio into account
            if sel.aspectRatio:
                r, w, h = sel.aspectRatio, nrect.width(), nrect.height()
                nw, nh = max(w, h*r), max(h, w/r)
                if nw > w:
                    d1 = (nw - w) / 2
                    if nrect.left() - orect.left() < d1:
                        d1 = nrect.left() - orect.left()
                    elif orect.right() - nrect.right() < d1:
                        d1 = nw - w - orect.right() + nrect.right()
                    d2 = nw - w - d1
                    nrect.adjust(-d1, 0, d2, 0)
                elif nh > h:
                    d1 = (nh - h) / 2
                    if nrect.top() - orect.top() < d1:
                        d1 = nrect.top() - orect.top()
                    elif orect.bottom() - nrect.bottom() < d1:
                        d1 = nh - h - orect.bottom() + nrect.bottom()
                    d2 = nh - h - d1
                    nrect.adjust(0, -d1, 0, d2)

            # set selection to new values
            nrect = sel.mapRectFromImage(nrect)
            sel.setBoundingRect(nrect.topLeft(), nrect.bottomRight())
        finally:
            QApplication.restoreOverrideCursor()

    def resizeEvent(self, event):
        self.slotFitInView(self.ui.actionFitInView.isChecked())

    def startAnalysis(self, path):
        self.cancelAnalysis()
        if not path or not getattr(self, "analyzeOnOpen", True):
            return
        from krop.analyzepanel import AnalyzeJob
        start = 0 if self.viewer.isEmpty() else self.viewer.currentPageIndex
        job = AnalyzeJob(path, start, self.analysisPanel.error_threshold())
        job.counted.connect(self.analysisPanel.begin)
        job.page_ready.connect(self.analysisPanel.add_page)
        job.image_ready.connect(self.analysisPanel.show_shared_image)
        job.done.connect(self._onAnalysis)
        job.yield_to = self.analysisPanel.yield_to_urgent
        self._analysis_job = job
        self._analysis_jobs.append(job)
        self.analysisPanel.set_running(path)
        job.start()

    def cancelAnalysis(self, wait=False):
        job = self._analysis_job
        if job is None:
            return
        self._analysis_job = None
        try:
            job.cancel.set()
            for sig, slot in ((job.done, self._onAnalysis),
                              (job.page_ready, self.analysisPanel.add_page),
                              (job.image_ready, self.analysisPanel.show_shared_image),
                              (job.counted, self.analysisPanel.begin)):
                try:
                    sig.disconnect(slot)
                except Exception:
                    pass
            if wait and job.isRunning():
                job.wait(60000)
        except RuntimeError:
            pass

    def eventFilter(self, obj, event):
        # Hold Alt to compare the original pixels with the target preview.
        try:
            press = QEvent.Type.KeyPress
            release = QEvent.Type.KeyRelease
            alt = Qt.Key.Key_Alt
        except AttributeError:
            press = QEvent.KeyPress
            release = QEvent.KeyRelease
            alt = Qt.Key_Alt
        etype = event.type()
        if etype in (press, release) and event.key() == alt and not event.isAutoRepeat():
            hold = etype == press
            if hold != self._preview_hold:
                self._preview_hold = hold
                self._updatePreview()
        return False

    def _showImageHighlight(self, xref):
        if not xref or not self.analysisPanel.isVisible():
            self.viewer.clearImageHighlight()
            return
        self.viewer.showImageHighlight(int(xref))

    def _updatePreview(self, *_args):
        viewer = getattr(self, "viewer", None)
        panel = getattr(self, "analysisPanel", None)
        if viewer is None or panel is None:
            return
        if self._preview_hold or not panel.isVisible() or not panel.preview_enabled():
            viewer.clearPreviews()
            return
        path = getattr(self, "originalFileName", None) or getattr(self, "fileName", None)
        if not path or viewer.isEmpty():
            viewer.clearPreviews()
            return
        try:
            rotation = viewer.pageGetRotation(viewer.currentPageIndex)
        except Exception:
            rotation = 0
        rotation = int(rotation or 0)
        plan = panel.preview_plan(viewer.currentPageIndex + 1)
        viewer.setPreviewPlan(path, plan, rotation)
        urgent = panel.urgent()
        if urgent and urgent[1] != "keep":
            if viewer.hasPreview(urgent[0], urgent[1], rotation):
                viewer.clearBusy()
            else:
                viewer.showBusy(urgent[0], urgent[2])
        else:
            viewer.clearBusy()
        for xref, target, rot in viewer.missingPreviews():
            token = urgent[2] if urgent and urgent[0] == xref and urgent[1] == target else None
            priority = 0 if token is not None else 10
            panel.enqueue_preview(path, xref, target, rot, token, priority)

    def _analysisDockVisibility(self, visible):
        if not visible:
            self.viewer.clearImageHighlight()
            self.viewer.clearPreviews()
        else:
            self._updatePreview()

    def _refocusAnalysis(self, page):
        job = self._analysis_job
        if job is not None:
            try:
                job.set_focus(int(page) - 1)
            except RuntimeError:
                pass

    def _reanalyze(self, _value):
        path = getattr(self, "originalFileName", None) or self.fileName
        if path and not self.viewer.isEmpty():
            self.startAnalysis(path)

    def _onAnalysis(self, data):
        self.analysisPanel.set_result(data)
        if not data or data.get("error"):
            if not data:
                log("ANALYZE", "failed")
                return
            msg = data.get("message") or data["error"].strip().splitlines()[-1]
            log("ANALYZE", "failed: %s" % msg)
            for line in data["error"].splitlines():
                log("ANALYZE", line)
            return
        from krop.analyze import analyze_lines
        for line in analyze_lines(data):
            log("ANALYZE", line)

    def slotShowAnalysisPage(self, page):
        if self.viewer.isEmpty():
            return
        self.viewer.currentPageIndex = max(0, int(page) - 1)
        self.updateControls()
        self.slotFitInView(self.ui.actionFitInView.isChecked())

    def closeEvent(self, event):
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except Exception:
                pass
        viewer = getattr(self, "viewer", None)
        if viewer is not None:
            viewer._preview_gen = getattr(viewer, "_preview_gen", 0) + 1
            for job in list(getattr(viewer, "_preview_jobs", []) or []):
                try:
                    job.wait(2000)
                except Exception:
                    pass
        panel = getattr(self, "analysisPanel", None)
        if panel is not None:
            panel._ladder_gen = getattr(panel, "_ladder_gen", 0) + 1
            stop = getattr(panel, "_work_stop", None)
            wake = getattr(panel, "_wake", None)
            if stop is not None:
                stop.set()
            if wake is not None:
                wake.set()
            worker = getattr(panel, "_worker", None)
            if worker is not None:
                try:
                    worker.wait(3000)
                except Exception:
                    pass
        if viewer is not None:
            spinner = getattr(viewer, "_spinner", None)
            if spinner is not None:
                spinner.stop()
        self.cancelAnalysis(wait=True)
        self.writeSettings()
