# -*- coding: utf-8 -*-
"""Analysis dock: per-page color and bit-depth recommendations."""
import threading

from krop.qt import *

try:
    _USER = Qt.ItemDataRole.UserRole
except AttributeError:
    _USER = Qt.UserRole
try:
    _XREF = int(_USER) + 1
except Exception:
    _XREF = 257
_PAGES = _XREF + 1


def _page_list(pages, fallback):
    values = pages or ([fallback] if fallback else [])
    return ",".join(str(int(p)) for p in values)


def _blocker_xref(blocker):
    import re
    if not blocker or blocker.get("kind") != "image":
        return None
    match = re.search(r"xref\s+(\d+)", blocker.get("text") or "")
    return int(match.group(1)) if match else None

try:
    _SINGLE = QAbstractItemView.SelectionMode.SingleSelection
    _ROWS = QAbstractItemView.SelectionBehavior.SelectRows
    _FOCUS = Qt.FocusPolicy.StrongFocus
except AttributeError:
    _SINGLE = QAbstractItemView.SingleSelection
    _ROWS = QAbstractItemView.SelectRows
    _FOCUS = Qt.StrongFocus


class AnalyzeJob(QThread):
    counted = pyqtSignal(int)
    page_ready = pyqtSignal(object)
    image_ready = pyqtSignal(object)
    done = pyqtSignal(object)

    def __init__(self, path, focus=0, threshold=None):
        QThread.__init__(self)
        self.path = path
        self.cancel = threading.Event()
        self.threshold = threshold
        self._focus = int(focus or 0)
        self._lock = threading.Lock()

    def set_focus(self, index):
        with self._lock:
            self._focus = max(0, int(index))

    def _focus_value(self):
        with self._lock:
            return self._focus

    def run(self):
        try:
            from krop.analyze import analyze_file, missing_message

            def on_start(count):
                if not self.cancel.is_set():
                    self.counted.emit(int(count))

            def on_page(rec):
                if not self.cancel.is_set():
                    self.page_ready.emit(rec)

            def on_image(im):
                if not self.cancel.is_set():
                    self.image_ready.emit(im)

            result = analyze_file(
                self.path, cancel=self.cancel, threshold=self.threshold,
                focus=self._focus_value, on_start=on_start, on_page=on_page,
                on_image=on_image)
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
    focus_changed = pyqtSignal(int)
    threshold_changed = pyqtSignal(float)
    highlight_requested = pyqtSignal(int)

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
        threshold_row = QHBoxLayout()
        threshold_row.addWidget(QLabel("Mean error ≤"))
        self.errorBox = QDoubleSpinBox()
        self.errorBox.setObjectName("analysisError")
        self.errorBox.setRange(0.0, 64.0)
        self.errorBox.setDecimals(1)
        self.errorBox.setSingleStep(1.0)
        self.errorBox.setToolTip("Mean absolute error allowed when choosing a smaller image encoding, on a 0-255 scale. The maximum error must also stay within max(16, 4× this value).")
        stored = QSettings().value("Analysis/ErrorThreshold", "")
        try:
            initial = float(stored) if stored not in ("", None) else 4.0
        except (TypeError, ValueError):
            initial = 4.0
        self.errorBox.blockSignals(True)
        self.errorBox.setValue(initial)
        self.errorBox.blockSignals(False)
        self._threshold_timer = QTimer(self)
        self._threshold_timer.setSingleShot(True)
        self._threshold_timer.timeout.connect(self._emit_threshold)
        self.errorBox.valueChanged.connect(lambda _v: self._threshold_timer.start(400))
        threshold_row.addWidget(self.errorBox)
        threshold_row.addWidget(QLabel("MAE"))
        threshold_row.addStretch(1)
        layout.addLayout(threshold_row)
        self.tree = QTreeWidget()
        self.tree.setObjectName("analysisTree")
        self.tree.setHeaderLabels(["Page", "Label", "Target", "Images", "Blockers", "Size"])
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
        pct = s.get("savings_pct")
        if pct is None:
            pct_s = ""
        elif pct > 0.5:
            pct_s = ", %.0f%% smaller" % pct
        else:
            pct_s = ""
        self.summary.setText(
            "Document: %s → %s%s. %d page(s), %d image(s), %d duplicate(s), %d blocker(s). %s → %s%s."
            % (s["label"], s["target"], bpp, s["pages"], s["images"], s["duplicates"], s["blocker_count"],
               _fmt_bytes(s.get("image_bytes")), _fmt_bytes(s.get("est_bytes")), pct_s))
        for page in data["pages"]:
            if not page:
                continue
            row = QTreeWidgetItem(["", "", "", "", "", ""])
            row.setData(0, _USER, page["page"])
            self._fill_row(row, page)
            self.tree.addTopLevelItem(row)
        self.tree.expandAll()

    def error_threshold(self):
        return float(self.errorBox.value())

    def _emit_threshold(self):
        value = float(self.errorBox.value())
        QSettings().setValue("Analysis/ErrorThreshold", str(value))
        self.threshold_changed.emit(value)

    def begin(self, count):
        """Show one pending row per page. Filled in as each page finishes."""
        self._selecting = True
        self._arrived = 0
        self._total = int(count)
        try:
            self.tree.clear()
            self.summary.setText("Analyzing… 0/%d" % count)
            for number in range(1, int(count) + 1):
                row = QTreeWidgetItem([str(number), "pending", "", "", "", ""])
                row.setData(0, _USER, number)
                self.tree.addTopLevelItem(row)
        finally:
            self._selecting = False
        self._select_followed()

    def show_shared_image(self, im):
        """Attach an already-analyzed shared image to every page that uses it."""
        if not im:
            return
        pages = im.get("pages") or []
        if not pages and im.get("page"):
            pages = [im.get("page")]
        self._selecting = True
        try:
            for number in pages:
                row = self._row_for_page(number)
                if row is None:
                    continue
                self._upsert_image(row, im, number)
        finally:
            self._selecting = False

    def _upsert_image(self, row, im, page_no):
        xref = im.get("xref")
        for i in range(row.childCount()):
            child = row.child(i)
            if child.data(0, _XREF) == xref:
                self._paint_image(child, im)
                return
        child = QTreeWidgetItem(["", "", "", "", "", ""])
        child.setData(0, _USER, page_no)
        child.setData(0, _XREF, xref)
        child.setData(0, _PAGES, _page_list(im.get("pages"), page_no))
        self._paint_image(child, im)
        row.insertChild(0, child)

    def _paint_image(self, child, im):
        child.setText(1, im.get("detail") or "")
        child.setText(2, im.get("target") or "")
        child.setText(3, im.get("target_text") or "")
        tip = im.get("link_tip") or ""
        if tip:
            child.setToolTip(1, tip)
            child.setToolTip(2, tip)

    def add_page(self, page):
        if not page:
            return
        self._selecting = True
        try:
            row = self._row_for_page(page["page"])
            if row is None:
                row = QTreeWidgetItem(["", "", "", "", "", ""])
                row.setData(0, _USER, page["page"])
                self.tree.addTopLevelItem(row)
            self._fill_row(row, page)
            self._arrived = getattr(self, "_arrived", 0) + 1
            total = getattr(self, "_total", self._arrived)
            self.summary.setText("Analyzing… %d/%d" % (self._arrived, total))
        finally:
            self._selecting = False
        self._select_followed()

    def _fill_row(self, row, page):
        images = page.get("images") or []
        count = page.get("image_count")
        if count is None:
            count = len(images)
        row.setText(0, str(page["page"]))
        row.setText(1, page.get("label") or "")
        row.setText(2, page.get("target") or "")
        row.setText(3, str(count))
        row.setText(4, str(page.get("blocker_count") or 0))
        row.setText(5, _size_text(page.get("image_bytes"), page.get("image_est"), page.get("savings_pct")))
        while row.childCount():
            row.removeChild(row.child(0))
        for im in images:
            child = QTreeWidgetItem(["", "", "", "", "", ""])
            child.setData(0, _USER, page["page"])
            child.setData(0, _XREF, im.get("xref"))
            child.setData(0, _PAGES, _page_list(im.get("pages"), page["page"]))
            self._paint_image(child, im)
            row.addChild(child)
        for blocker in page.get("blockers") or []:
            child = QTreeWidgetItem(["", blocker["text"], "", "", "", ""])
            child.setData(0, _USER, page["page"])
            xref = _blocker_xref(blocker)
            if xref:
                child.setData(0, _XREF, xref)
                child.setData(0, _PAGES, _page_list(None, page["page"]))
            row.addChild(child)
        for note in page.get("notes") or []:
            child = QTreeWidgetItem(["", note, "", "", "", ""])
            child.setData(0, _USER, page["page"])
            row.addChild(child)

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
        self.focus_changed.emit(page)

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
        self.highlight_requested.emit(0)

    def _xref_of(self, item):
        if item is None:
            return None
        xref = item.data(0, _XREF)
        try:
            return int(xref) if xref else None
        except (TypeError, ValueError):
            return None

    def _pages_of(self, item):
        if item is None:
            return []
        raw = item.data(0, _PAGES)
        if raw:
            try:
                return [int(part) for part in str(raw).split(",") if part]
            except ValueError:
                pass
        page = self._page_of(item)
        return [page] if page else []

    def _request(self, item):
        if self._selecting:
            return
        page = self._page_of(item)
        if not page:
            return
        xref = self._xref_of(item)
        pages = self._pages_of(item)
        # A shared image is highlighted on the page already on screen.
        if xref and self._follow_page in pages:
            self.highlight_requested.emit(int(xref))
            return
        if self._pending_nav == page:
            return
        self._pending_nav = page
        self.page_requested.emit(page)
        self.highlight_requested.emit(int(xref) if xref else 0)
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


def _fmt_bytes(n):
    n = int(n or 0)
    if n < 1024:
        return "%d B" % n
    return "%.1f KB" % (n / 1024.0)


def _size_text(current, estimate, pct):
    if not current and not estimate:
        return ""
    if pct is None:
        pct_s = ""
    elif pct > 0.5:
        pct_s = "%.0f%% smaller" % pct
    elif pct < -0.5:
        pct_s = "%.0f%% larger" % (-pct)
    else:
        pct_s = "0%"
    return "%s → %s %s" % (_fmt_bytes(current), _fmt_bytes(estimate), pct_s)
