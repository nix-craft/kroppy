# -*- coding: utf-8 -*-
"""Analysis dock: per-page color and bit-depth recommendations."""
import threading

from krop.qt import *

try:
    _USER = Qt.ItemDataRole.UserRole
except AttributeError:
    _USER = Qt.UserRole


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
        self.tree.itemClicked.connect(self._clicked)
        layout.addWidget(self.tree)
        self.setWidget(body)

    def set_running(self, name):
        self.summary.setText("Analyzing %s…" % name)
        self.tree.clear()

    def set_result(self, data):
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

    def _clicked(self, item, _column):
        page = item.data(0, _USER)
        if page:
            self.page_requested.emit(int(page))
