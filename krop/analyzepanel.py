# -*- coding: utf-8 -*-
"""Analysis dock: per-page color and bit-depth recommendations."""
import threading

from krop.qt import *

try:
    _USER = Qt.ItemDataRole.UserRole
except AttributeError:
    _USER = Qt.UserRole

try:
    _SINGLE = QAbstractItemView.SelectionMode.SingleSelection
    _ROWS = QAbstractItemView.SelectionBehavior.SelectRows
    _FOCUS = Qt.FocusPolicy.StrongFocus
except AttributeError:
    _SINGLE = QAbstractItemView.SingleSelection
    _ROWS = QAbstractItemView.SelectRows
    _FOCUS = Qt.StrongFocus


class AnalyzeJob(QThread):
    done = pyqtSignal(object)

    def __init__(self, path):
        QThread.__init__(self)
        self.path = path
        self.cancel = threading.Event()

    def run(self):
        try:
            from krop.analyze import analyze_file, missing_message
            result = analyze_file(self.path, cancel=self.cancel)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            message = None
            missing = getattr(exc, "packages", None)
            try:
                from krop.analyze import missing_message
                message = missing_message(exc)
            except Exception:
                if isinstance(exc, ModuleNotFoundError):
                    name = getattr(exc, "name", None) or "unknown"
                    pip = "Pillow" if name == "PIL" else name
                    message = "Missing package: %s. Install it with: pip install %s" % (pip, pip)
                    missing = [pip]
            result = {"error": tb, "message": message, "missing": missing}
        if not self.cancel.is_set():
            self.done.emit(result)


class AnalysisPanel(QDockWidget):
    page_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        QDockWidget.__init__(self, "Analysis", parent)
        self.setObjectName("analysisDock")
        self._follow_page = None
        self._selecting = False
        self._pending_nav = None
        body = QWidget()
        layout = QVBoxLayout(body)
        self.summary = QLabel("Open a PDF to analyze it.")
        self.summary.setWordWrap(True)
        self.summary.setObjectName("analysisSummary")
        layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setObjectName("analysisTree")
        self.tree.setHeaderLabels(["Page", "Label", "Target", "Images", "Blockers"])
        self.tree.setColumnWidth(0, 50)
        self.tree.setSelectionMode(_SINGLE)
        self.tree.setSelectionBehavior(_ROWS)
        self.tree.setFocusPolicy(_FOCUS)
        # currentItemChanged covers keyboard up/down and a click that changes
        # the selection. itemClicked and itemPressed also fire for a click on
        # the row that is already selected, which currentItemChanged does not.
        self.tree.currentItemChanged.connect(self._on_current_changed)
        self.tree.itemClicked.connect(self._on_clicked)
        self.tree.itemPressed.connect(self._on_clicked)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.tree)
        self.setWidget(body)

    def set_running(self, name):
        self.summary.setText("Analyzing %s…" % name)
        self._selecting = True
        try:
            self.tree.clear()
        finally:
            self._selecting = False

    def set_result(self, data):
        self._selecting = True
        try:
            self._fill(data)
        finally:
            self._selecting = False
        self._select_followed()

    def _fill(self, data):
        self.tree.clear()
        if not data:
            self.summary.setText("Analysis cancelled.")
            return
        if data.get("error"):
            message = data.get("message")
            if not message:
                last = data["error"].strip().splitlines()[-1]
                message = "Analysis failed: %s" % last
            self.summary.setText(message)
            for line in data["error"].splitlines()[-12:]:
                self.tree.addTopLevelItem(QTreeWidgetItem(["", "", "", line, ""]))
            return
        s = data["summary"]
        bpp = "" if s.get("suggested_bpp") is None else ", %s bpp" % s["suggested_bpp"]
        self.summary.setText(
            "Document: %s → %s%s. %d page(s), %d image(s), %d duplicate(s), %d blocker(s)."
            % (s["label"], s["target"], bpp, s["pages"], s["images"], s["duplicates"], s["blocker_count"]))
        for page in data["pages"]:
            row = QTreeWidgetItem([
                str(page["page"]), page["label"], page["target"],
                page["image_summary"], str(page["blocker_count"])])
            row.setData(0, _USER, page["page"])
            for blocker in page["blockers"]:
                child = QTreeWidgetItem(["", blocker["text"], "", "", ""])
                child.setData(0, _USER, page["page"])
                row.addChild(child)
            for note in page.get("notes") or []:
                child = QTreeWidgetItem(["", note, "", "", ""])
                child.setData(0, _USER, page["page"])
                row.addChild(child)
            self.tree.addTopLevelItem(row)
        self.tree.expandAll()

    def follow_page(self, page):
        """Select the row for this 1-based page. Does not change the viewer."""
        try:
            page = int(page)
        except (TypeError, ValueError):
            return
        if page < 1:
            return
        self._follow_page = page
        self._select_followed()

    def _row_for_page(self, page):
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.data(0, _USER) == page:
                return item
        return None

    def _page_of(self, item):
        if item is None:
            return None
        page = item.data(0, _USER)
        if not page:
            return None
        try:
            return int(page)
        except (TypeError, ValueError):
            return None

    def _select_followed(self):
        page = self._follow_page
        if not page:
            return
        current = self.tree.currentItem()
        # A child of this page stays selected so keyboard movement is not yanked back.
        if self._page_of(current) == page:
            self._selecting = True
            try:
                self.tree.scrollToItem(current)
            finally:
                self._selecting = False
            return
        item = self._row_for_page(page)
        if item is None:
            return
        self._selecting = True
        try:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
        finally:
            self._selecting = False

    def _request(self, item):
        if self._selecting:
            return
        page = self._page_of(item)
        if not page or self._pending_nav == page:
            return
        self._pending_nav = page
        self.page_requested.emit(page)
        QTimer.singleShot(0, self._clear_pending)

    def _clear_pending(self):
        self._pending_nav = None

    def _on_current_changed(self, current, _previous):
        self._request(current)

    def _on_clicked(self, item, _column):
        self._request(item)

    def _on_selection_changed(self):
        self._request(self.tree.currentItem())

    def _clicked(self, item, _column):
        # Kept for tests that call the handler directly.
        self._on_clicked(item, _column)
