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
        self.yield_to = None
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

            import pymupdf
            doc = pymupdf.open(self.path)
            try:
                try:
                    count = doc.page_count
                except Exception:
                    count = len(doc)
            finally:
                doc.close()
            result = {"count": int(count)}
            if not self.cancel.is_set():
                self.counted.emit(int(count))
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
        if not self.cancel.is_set() and isinstance(result, dict) and result.get("error"):
            self.done.emit(result)


def _run_queued(payload):
    kind = payload.get("kind")
    if not payload.get("path"):
        return None
    if kind == "measure":
        from krop.imagetarget import encode_file_image
        return encode_file_image(
            payload["path"], payload["xref"], payload["target"],
            payload.get("stored") or 0, payload.get("filt") or "")
    if kind == "preview":
        from krop.imagetarget import encode_file_image
        from krop.pdfimage import cached_encoded
        entry = encode_file_image(
            payload["path"], payload["xref"], payload["target"],
            payload.get("stored") or 0, payload.get("filt") or "")
        return {
            "entry": entry,
            "encoded": cached_encoded(payload["path"], payload["xref"], payload["target"]),
        }
    return None


class _P0Job(QThread):
    """One chosen target, started immediately so it does not wait on the ladder."""
    done = pyqtSignal(object)

    def __init__(self, payload):
        QThread.__init__(self)
        self.payload = payload

    def run(self):
        import time
        started = time.perf_counter()
        error = None
        result = None
        try:
            result = _run_queued(self.payload)
        except Exception as exc:
            error = exc
        self.done.emit({
            "payload": self.payload, "result": result, "error": error,
            "elapsed": time.perf_counter() - started, "p0": True,
        })


class _WorkThread(QThread):
    """One worker. Each pick re-reads which page and which target matter now."""
    done = pyqtSignal(object)

    def __init__(self, scheduler, wake, stop):
        QThread.__init__(self)
        self.scheduler = scheduler
        self.wake = wake
        self.stop = stop

    def run(self):
        while not self.stop.is_set():
            job = self.scheduler.pop()
            if job is None:
                self.wake.wait(0.25)
                self.wake.clear()
                continue
            if self.scheduler.should_pause(job):
                self.scheduler.add(job.get("_key"), job)
                continue
            self.scheduler.set_running(job.get("_tier"), job.get("page"))
            result = None
            paused = False
            error = None
            try:
                if job.get("kind") == "page":
                    from krop.analyze import analyze_page
                    def pause(job=job):
                        return self.scheduler.should_pause(job)
                    result = analyze_page(
                        job.get("path"), job.get("page"),
                        threshold=job.get("threshold"), defer_heavy=True,
                        cheap=bool(job.get("cheap")), pause=pause)
                    if result is None and error is None:
                        paused = True
                        self.scheduler.add(job.get("_key"), job)
                else:
                    result = _run_queued(job)
            except Exception as exc:
                error = exc
                result = None
            self.scheduler.clear_running()
            if paused and error is None:
                self.wake.set()
                continue
            self.done.emit({"payload": job, "result": result, "error": error})
            self.wake.set()


class _LadderJob(QThread):
    """Measure JPEG and palette options away from the UI thread."""
    ready = pyqtSignal(object)

    def __init__(self, path, xref, share, candidates, stored, filt, threshold, width, height, gen):
        QThread.__init__(self)
        self.path = path
        self.xref = xref
        self.share = share
        self.candidates = candidates
        self.stored = stored
        self.filt = filt
        self.threshold = threshold
        self.width = width
        self.height = height
        self.gen = gen

    def run(self):
        filled = self.candidates
        try:
            from krop.imagetarget import fill_pending_file
            filled = fill_pending_file(
                self.path, self.xref, self.candidates, self.stored, self.filt,
                self.threshold, self.width, self.height)
        except Exception:
            filled = self.candidates
        self.ready.emit({
            "xref": self.xref, "share": self.share, "candidates": filled, "gen": self.gen,
        })


class _EncodeJob(QThread):
    """Trial-encode one image candidate away from the UI thread."""
    ready = pyqtSignal(object)

    def __init__(self, path, xref, target_id, stored, filt, token, share):
        QThread.__init__(self)
        self.path = path
        self.xref = xref
        self.target_id = target_id
        self.stored = stored
        self.filt = filt
        self.token = token
        self.share = share

    def run(self):
        result = None
        error = None
        try:
            from krop.imagetarget import encode_file_image
            result = encode_file_image(self.path, self.xref, self.target_id, self.stored, self.filt)
        except Exception as exc:
            error = str(exc)
        self.ready.emit({
            "xref": self.xref, "share": self.share, "target": self.target_id,
            "token": self.token, "result": result, "error": error,
        })


class AnalysisPanel(QDockWidget):
    page_requested = pyqtSignal(int)
    focus_changed = pyqtSignal(int)
    threshold_changed = pyqtSignal(float)
    highlight_requested = pyqtSignal(int)
    preview_changed = pyqtSignal()

    def __init__(self, parent=None):
        QDockWidget.__init__(self, "Analysis", parent)
        self.setObjectName("analysisDock")
        self._follow_page = None
        self._selecting = False
        self._pending_nav = None
        self._path = None
        self._data = None
        self._choices = {}
        self._encoded = {}
        self._tokens = {}
        self._encode_jobs = []
        self._ladder_queue = []
        self._ladder_job = None
        self._ladder_gen = 0
        self._ladder_seen = set()
        self._urgent = None
        from krop.scheduler import Scheduler
        self._scheduler = Scheduler()
        self._wake = threading.Event()
        self._work_stop = threading.Event()
        self._worker = _WorkThread(self._scheduler, self._wake, self._work_stop)
        self._worker.done.connect(self._on_work)
        self._worker.start()
        self._p0_jobs = []
        self._preview_timeout_ms = 15000
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._on_preview_timeout)
        self._watch = None
        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(400)
        self._idle_timer.timeout.connect(self._submit_available)
        self._idle_timer.start()
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
        self.previewBox = QCheckBox("Preview target")
        self.previewBox.setObjectName("previewTarget")
        self.previewBox.setChecked(True)
        self.previewBox.setToolTip("Draw each image as its chosen target on the page. Hold Alt to compare with the original.")
        self.previewBox.toggled.connect(lambda _v: self.preview_changed.emit())
        threshold_row.addWidget(self.previewBox)
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
        self.legend = QLabel("orange = error over limit; measuring… = size still being computed")
        self.legend.setObjectName("analysisLegend")
        self.legend.setWordWrap(True)
        layout.addWidget(self.legend)
        self.backgroundBox = QCheckBox("Analyze other pages in background")
        self.backgroundBox.setObjectName("analyzeBackground")
        self.backgroundBox.setChecked(True)
        self.backgroundBox.setToolTip("Other pages are scanned only while you are idle, nearest page first. The page on screen always comes first.")
        self.backgroundBox.toggled.connect(self._toggle_background)
        layout.addWidget(self.backgroundBox)
        self.setWidget(body)

    def _toggle_background(self, enabled):
        self._scheduler.set_background(bool(enabled))
        self._wake.set()
        self._submit_available()
        self._refresh_progress()

    def set_running(self, name):
        self._path = name
        self._data = None
        self._pages = {}
        self._ladder_gen = getattr(self, "_ladder_gen", 0) + 1
        self._ladder_queue = []
        self._ladder_seen = set()
        self._urgent = None
        self._finalized = False
        self._failed = False
        if getattr(self, "_scheduler", None) is not None:
            self._scheduler.configure(0, 0)
        self.summary.setText("Analyzing %s…" % name)
        self.preview_changed.emit()
        self._selecting = True
        try:
            self.tree.clear()
        finally:
            self._selecting = False

    def set_result(self, data):
        self._data = data
        if data and not data.get("error"):
            self._reapply_choices()
        self._selecting = True
        try:
            self._fill(data)
        finally:
            self._selecting = False
        self._select_followed()
        self.preview_changed.emit()
        self._queue_pending_ladder()

    def _queue_pending_ladder(self):
        pages = []
        data = self._data or {}
        if data.get("pages") and not data.get("error"):
            pages = data.get("pages") or []
        else:
            pages = list(getattr(self, "_pages", {}).values())
        for page in pages:
            if not page:
                continue
            for im in page.get("images") or []:
                self._queue_ladder(im)

    def _queue_ladder(self, im):
        """Queue the displayed page's extra candidates. Other pages stay cheap."""
        if not im:
            return
        page_index = int(im.get("page") or 1) - 1
        if page_index != self._scheduler.focus:
            return
        for cand in im.get("candidates") or []:
            if not cand.get("pending"):
                continue
            self._scheduler.add(("measure", int(im.get("xref")), str(cand.get("id"))), {
                "kind": "measure", "path": self._path, "xref": int(im.get("xref")),
                "target": str(cand.get("id")), "page": page_index, "lane": "extra",
                "share": im.get("share_key") or "", "stored": int(im.get("bytes") or 0),
                "filt": im.get("filter_name") or "", "gen": self._ladder_gen,
            })
        self._wake.set()

    def prioritize(self, xref, target_id, token=None):
        """The chosen target is P0 until it is measured and previewed."""
        if target_id == "keep":
            self._scheduler.set_urgent(None, None)
            self._wake.set()
            return
        self._urgent = (int(xref), str(target_id), token)
        self._scheduler.set_urgent(xref, target_id)
        im = self._find_image(xref) or {
            "xref": xref, "page": self._follow_page or 1, "bytes": 0,
            "filter_name": "", "share_key": "",
        }
        page_index = int(im.get("page") or (self._follow_page or 1)) - 1
        cand = None
        for item in im.get("candidates") or []:
            if item.get("id") == target_id:
                cand = item
                break
        if cand is None or cand.get("pending") or cand.get("est_bytes") is None:
            self._start_p0({
                "kind": "measure", "path": self._path, "xref": int(xref),
                "target": str(target_id), "page": page_index, "lane": "choice",
                "share": im.get("share_key") or "", "stored": int(im.get("bytes") or 0),
                "filt": im.get("filter_name") or "", "gen": self._ladder_gen, "token": token,
            })
        self._wake.set()
        self._refresh_progress()

    def enqueue_preview(self, path, xref, target, rotation, token, priority):
        im = self._find_image(xref)
        if im is not None:
            page_index = int(im.get("page") or 1) - 1
        else:
            page_index = (self._follow_page or 1) - 1
        payload = {
            "kind": "preview", "path": path, "xref": int(xref), "target": str(target),
            "rotation": int(rotation or 0), "token": token, "page": page_index,
            "lane": "choice" if priority <= 0 else "page", "gen": self._ladder_gen,
            "max_edge": self._preview_edge(xref),
        }
        if priority <= 0:
            # The chosen preview runs on its own thread, not behind the ladder.
            self._start_p0(payload)
            self._arm_preview_watch(int(xref), str(target), token)
            return
        self._scheduler.add(("preview", int(xref), str(target), int(rotation or 0)), payload)
        self._wake.set()

    def _preview_edge(self, xref):
        """On-screen pixels of this image at the current zoom. None means full resolution."""
        import math
        viewer = getattr(self.parent(), "viewer", None)
        if viewer is None:
            return None
        try:
            rects = viewer.imagePlacementRects(int(xref))
        except Exception:
            return None
        edge = 0.0
        for rect in rects or []:
            edge = max(edge, float(rect[2]), float(rect[3]))
        if edge <= 0:
            return None
        scale = 1.0
        scene = viewer.scene() if hasattr(viewer, "scene") else None
        if scene is not None:
            for view in scene.views():
                tr = view.transform()
                scale = max(scale, abs(tr.m11()), abs(tr.m22()))
        return max(1, int(math.ceil(edge * scale)))

    def _start_p0(self, payload):
        job = _P0Job(payload)
        job.done.connect(self._on_p0)
        self._p0_jobs.append(job)
        job.finished.connect(lambda done=job: self._p0_jobs.remove(done) if done in self._p0_jobs else None)
        job.start()

    def _arm_preview_watch(self, xref, target, token):
        self._watch = (int(xref), str(target), token)
        self._preview_timer.start(int(self._preview_timeout_ms))

    def _on_preview_timeout(self):
        watch = getattr(self, "_watch", None)
        if not watch:
            return
        xref, target, token = watch
        viewer = getattr(self.parent(), "viewer", None)
        if viewer is None or not viewer.busyVisible() or viewer._busy_token != token:
            return
        self._close_preview(xref, token)
        self._mark_target_note(
            xref, target, "preview timed out",
            "Preview did not finish within %d seconds." % (int(self._preview_timeout_ms) // 1000))
        self._log_p0("preview xref %s %s: timed out" % (xref, str(target).replace("-", "")), None)

    def job_order(self):
        return list(self._scheduler.order())

    def urgent(self):
        return self._urgent

    def yield_to_urgent(self):
        """Kept for callers. Page jobs now yield on their own between images."""
        import time
        deadline = time.time() + 20
        while time.time() < deadline and self._scheduler.has_priority(0):
            self._wake.set()
            time.sleep(0.02)

    def _submit_available(self):
        if not self._path or getattr(self, "_scheduler", None) is None:
            return
        if self._scheduler.page_count <= 0:
            return
        claimed = self._scheduler.claim_next_page(self._path, self.error_threshold(), self._ladder_gen)
        if claimed:
            self._wake.set()
        self._refresh_progress()

    def _refresh_progress(self):
        if getattr(self, "_scheduler", None) is None or self._scheduler.page_count <= 0:
            return
        page = self._follow_page or (self._scheduler.focus + 1)
        rec = (self._pages or {}).get(page)
        images = (rec or {}).get("images") or []
        ready = sum(1 for im in images if im.get("target"))
        if not self._scheduler.background:
            other = "off"
        elif self._scheduler.running_tier == 3 and self._scheduler.running_page is not None:
            other = "scanning p%d" % (int(self._scheduler.running_page) + 1)
        elif self._scheduler.unscanned_others():
            other = "idle"
        else:
            other = "done"
        line = "page %d: %d/%d images ready; other pages: %s" % (page, ready, len(images), other)
        if getattr(self, "_finalized", False) or getattr(self, "_failed", False):
            return
        self.summary.setText(line)
        self._maybe_final()

    def _maybe_final(self):
        if getattr(self, "_finalized", False) or getattr(self, "_failed", False):
            return
        if not self._scheduler.all_visited():
            return
        pages = []
        images = []
        for index in range(self._scheduler.page_count):
            rec = (self._pages or {}).get(index + 1)
            if rec is None:
                return
            pages.append(rec)
            images.extend(rec.get("images") or [])
        try:
            from krop.analyze import _document, analyze_lines
            from krop.activitylog import log
            summary = _document(pages, images)
        except Exception:
            return
        self._data = {"summary": summary, "pages": pages, "images": images}
        self._finalized = True
        self._show_summary(summary)
        for line in analyze_lines(self._data):
            log("ANALYZE", line)

    def _show_worker_error(self, exc):
        import traceback
        self._failed = True
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        message = None
        try:
            from krop.analyze import missing_message
            message = missing_message(exc)
        except Exception:
            message = None
        if not message:
            message = "Analysis failed: %s" % exc
        self.summary.setText(message)
        try:
            from krop.activitylog import log
            log("ANALYZE", "failed: %s" % message)
            for line in tb.splitlines():
                log("ANALYZE", line)
        except Exception:
            pass

    def _on_work(self, message):
        payload = message.get("payload") or {}
        if payload.get("gen") not in (None, self._ladder_gen):
            return
        kind = payload.get("kind")
        if message.get("error") is not None:
            self._show_worker_error(message.get("error"))
            if kind == "page":
                self._scheduler.mark_scanned(payload.get("page"), "error")
            return
        if kind == "page":
            rec = message.get("result")
            index = payload.get("page")
            if rec:
                mode = "cheap" if payload.get("cheap") else "full"
                self._scheduler.mark_scanned(index, mode)
                self.add_page(rec)
                if mode == "full":
                    for im in rec.get("images") or []:
                        self._queue_ladder(im)
            else:
                self._scheduler.note_not_queued(index)
            self._refresh_progress()
            self._submit_available()
            self.preview_changed.emit()
            return
        if kind == "measure":
            self._apply_measure(payload, message.get("result"))
            self._refresh_progress()
            self._submit_available()
        elif kind == "preview":
            self._finish_preview(payload, message.get("result"), message.get("error"), message.get("elapsed"))

    def _on_p0(self, message):
        payload = message.get("payload") or {}
        if payload.get("gen") not in (None, self._ladder_gen):
            return
        kind = payload.get("kind")
        elapsed = message.get("elapsed")
        label = "%s xref %s %s: %.1f s" % (
            kind, payload.get("xref"), str(payload.get("target")).replace("-", ""), float(elapsed or 0))
        self._log_p0(label, message.get("error"))
        if kind == "measure":
            if message.get("error") is not None or message.get("result") is None:
                self._fail_p0(payload, message.get("error") or RuntimeError("measurement returned nothing"))
                return
            self._apply_measure(payload, message.get("result"))
            self._refresh_progress()
            return
        self._finish_preview(payload, message.get("result"), message.get("error"), elapsed)

    def _finish_preview(self, payload, result, error, elapsed):
        xref = payload.get("xref")
        target = str(payload.get("target"))
        token = payload.get("token")
        watch = getattr(self, "_watch", None)
        if watch and token is not None and watch[2] != token:
            return
        if error is not None or result is None:
            self._fail_p0(payload, error or RuntimeError("preview returned nothing"))
            return
        viewer = getattr(self.parent(), "viewer", None)
        if isinstance(result, dict) and result.get("encoded"):
            if result.get("entry"):
                self._apply_measure(payload, result["entry"])
            if viewer is not None:
                viewer.install_preview(
                    payload.get("path"), int(xref), target, result["encoded"], token,
                    int(payload.get("rotation") or 0))
        elif viewer is not None:
            viewer.deliverPreview({
                "xref": xref, "target": target,
                "rotation": payload.get("rotation") or 0, "token": token, "rgba": result,
            })
        if watch and watch[2] == token:
            self._preview_timer.stop()

    def _fail_p0(self, payload, exc):
        """A chosen preview or measurement failed. Drop the spinner and say why."""
        import traceback
        xref = payload.get("xref")
        target = str(payload.get("target"))
        token = payload.get("token")
        self._close_preview(xref, token)
        short = str(exc).strip().splitlines()[0] if exc else "unknown"
        if len(short) > 80:
            short = short[:77] + "..."
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else short
        self._mark_target_note(xref, target, "failed: " + short, detail)
        self._log_p0("failed %s xref %s %s: %s" % (payload.get("kind"), xref, target, short), exc)

    def _close_preview(self, xref, token):
        viewer = getattr(self.parent(), "viewer", None)
        if viewer is None:
            return
        if viewer._busy_token is not None and token is not None and viewer._busy_token != token:
            return
        viewer.clearBusy()
        tokens = getattr(viewer, "_preview_tokens", None)
        if tokens is not None and xref is not None:
            tokens[int(xref)] = (token, "closed")

    def _mark_target_note(self, xref, target, note, detail):
        _holder, matches = self._matching_images(xref, "")
        if not matches:
            im = self._find_image(xref)
            matches = [im] if im else []
        for im in matches:
            if im is None:
                continue
            for cand in im.get("candidates") or []:
                if str(cand.get("id")) == str(target):
                    cand["note"] = note
                    cand["note_detail"] = detail
                    cand["pending"] = False
            page_no = im.get("page")
            row = self._row_for_page(page_no) if page_no else None
            if row is not None:
                self._refresh_image_texts(row, im)

    def _log_p0(self, line, exc):
        import sys
        import traceback
        sys.stderr.write(line + "\n")
        try:
            from krop.activitylog import log
            log("ANALYZE", line)
            if exc is not None:
                tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                sys.stderr.write(tb)
                if not tb.endswith("\n"):
                    sys.stderr.write("\n")
                for row in tb.splitlines():
                    log("ANALYZE", row)
        except Exception:
            pass

    def _matching_images(self, xref, share):
        holder = self._holder()
        matches = []
        for page in holder.get("pages") or []:
            if not page:
                continue
            for im in page.get("images") or []:
                if im.get("xref") == xref or (share and im.get("share_key") == share):
                    matches.append(im)
        for im in holder.get("images") or []:
            if im.get("xref") == xref or (share and im.get("share_key") == share):
                matches.append(im)
        return holder, matches

    def _apply_measure(self, payload, result):
        if not result:
            return
        xref = payload.get("xref")
        target = str(payload.get("target"))
        share = payload.get("share") or ""
        holder, matches = self._matching_images(xref, share)
        if not matches:
            return
        limit = float(self.error_threshold())
        mae = result.get("error_mae")
        p995 = result.get("error_p995")
        try:
            tail = max(16.0, limit * 4.0)
            over = False
            if mae is not None and float(mae) > limit:
                over = True
            if p995 is not None and float(p995) > tail:
                over = True
        except (TypeError, ValueError):
            over = False
        fields = dict(result)
        fields["id"] = target
        fields["over"] = over
        fields["pending"] = False
        for im in matches:
            for cand in im.get("candidates") or []:
                if cand.get("id") == target:
                    cand.update(fields)
                    cand["pending"] = False
        chosen = self._choices.get(share) or self._choices.get(str(xref))
        if chosen == target:
            from krop.analyze import apply_chosen_target
            apply_chosen_target(holder, xref, fields)
        else:
            user_picked = bool(chosen)
            pending_left = any(c.get("pending") for im in matches for c in (im.get("candidates") or []))
            if not user_picked and not pending_left:
                from krop.imagetarget import choose_smallest
                from krop.analyze import apply_chosen_target
                best = choose_smallest(matches[0].get("candidates") or [], matches[0].get("bytes") or 0)
                if best and best.get("id") != matches[0].get("target"):
                    apply_chosen_target(holder, xref, best)
        self._refresh_after_choice()

    def _holder(self):
        data = self._data or {}
        if data.get("pages") and not data.get("error"):
            return data
        return {"pages": list(getattr(self, "_pages", {}).values()), "images": [], "summary": None}

    def preview_enabled(self):
        return bool(self.previewBox.isChecked())

    def preview_plan(self, page_no):
        """Images on this 1-based page whose chosen target is not keep."""
        if not self.preview_enabled():
            return []
        try:
            page_no = int(page_no)
        except (TypeError, ValueError):
            return []
        plan = []
        seen = set()
        pages = []
        data = self._data or {}
        if data.get("pages") and not data.get("error"):
            pages = data.get("pages") or []
        else:
            pages = list(getattr(self, "_pages", {}).values())
        for page in pages:
            if not page or page.get("page") != page_no:
                continue
            for im in page.get("images") or []:
                xref = im.get("xref")
                if xref in seen:
                    continue
                share = im.get("share_key") or ""
                chosen = self._choices.get(share) or self._choices.get(str(xref))
                chosen = chosen or im.get("chosen_target") or im.get("target")
                if not chosen or chosen == "keep":
                    continue
                seen.add(xref)
                plan.append({"xref": xref, "target": str(chosen)})
        return plan

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
        self._show_summary(data["summary"])
        for page in data["pages"]:
            if not page:
                continue
            row = QTreeWidgetItem(["", "", "", "", "", ""])
            row.setData(0, _USER, page["page"])
            self._fill_row(row, page)
            self.tree.addTopLevelItem(row)
        self.tree.expandAll()

    def _show_summary(self, summary):
        s = summary or {}
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
            % (s.get("label"), s.get("target"), bpp, s.get("pages") or 0, s.get("images") or 0,
               s.get("duplicates") or 0, s.get("blocker_count") or 0,
               _fmt_bytes(s.get("image_bytes")), _fmt_bytes(s.get("est_bytes")), pct_s))

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
        self._pages = {}
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
        if self._path and getattr(self, "_scheduler", None) is not None:
            focus = (self._follow_page or 1) - 1
            self._scheduler.configure(int(count), focus)
            self._submit_available()

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
        self._install_combo(child, im)

    def _paint_image(self, child, im):
        child.setText(1, im.get("detail") or "")
        child.setText(3, im.get("target_text") or "")
        tip = im.get("link_tip") or ""
        if tip:
            child.setToolTip(1, tip)

    def add_page(self, page):
        if not page:
            return
        if not hasattr(self, "_pages"):
            self._pages = {}
        self._pages[page["page"]] = page
        self._selecting = True
        try:
            row = self._row_for_page(page["page"])
            if row is None:
                row = QTreeWidgetItem(["", "", "", "", "", ""])
                row.setData(0, _USER, page["page"])
                self.tree.addTopLevelItem(row)
            self._fill_row(row, page)
            self._arrived = getattr(self, "_arrived", 0) + 1
        finally:
            self._selecting = False
        self._select_followed()
        self.preview_changed.emit()
        for im in page.get("images") or []:
            self._queue_ladder(im)
        self._refresh_progress()

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
            self._install_combo(child, im)
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

    def _install_combo(self, child, im):
        combo = QComboBox()
        combo.setObjectName("imageTarget")
        _scrollable_combo(combo)
        candidates = list(im.get("candidates") or [])
        if not any(c.get("id") == "keep" for c in candidates):
            candidates.append({"id": "keep", "label": "keep", "over": False})
        for cand in candidates:
            combo.addItem(_candidate_label(cand, im, self.error_threshold()), cand.get("id"))
            self._style_combo_row(combo, combo.count() - 1, cand, im)
        share = im.get("share_key") or ""
        chosen = self._choices.get(share) or self._choices.get(str(im.get("xref")))
        chosen = chosen or im.get("chosen_target") or im.get("recommended_target") or im.get("target")
        combo.blockSignals(True)
        index = combo.findData(chosen)
        if index >= 0:
            combo.setCurrentIndex(index)
            self._style_combo_row(combo, index, candidates[index], im)
        combo.blockSignals(False)
        combo.setProperty("xref", im.get("xref"))
        combo.setProperty("share", share)
        try:
            signal = combo.currentIndexChanged[int]
        except Exception:
            signal = combo.currentIndexChanged
        signal.connect(lambda i, box=combo, item=child: self._on_target_changed(box, item, i))
        self.tree.setItemWidget(child, 2, combo)

    def _on_target_changed(self, combo, item, index):
        if combo.signalsBlocked() or self._selecting:
            return
        target = combo.itemData(index)
        if not target:
            return
        xref = combo.property("xref")
        share = str(combo.property("share") or "")
        self.tree.setCurrentItem(item)
        self._choose(xref, share, str(target))

    def _choose(self, xref, share, target_id):
        key = share or str(xref)
        token = self._tokens.get(key, 0) + 1
        self._tokens[key] = token
        self._choices[key] = target_id
        self._choices[str(xref)] = target_id
        if target_id == "keep":
            self._urgent = None
        else:
            self._urgent = (int(xref), str(target_id), token)
            self.prioritize(xref, target_id, token)
        self.preview_changed.emit()
        cached = self._encoded.get((key, target_id))
        cand = self._candidate(xref, target_id)
        if target_id == "keep" or cached or (cand and cand.get("est_bytes") is not None and not cand.get("pending")):
            fields = dict(cand or {"id": "keep", "label": "keep", "error_mae": 0, "error_max": 0})
            if cached:
                fields.update(cached)
            fields["id"] = target_id
            if target_id == "keep":
                fields.setdefault("error_mae", 0)
                fields.setdefault("error_max", 0)
                if fields.get("est_bytes") is None and cand:
                    fields["est_bytes"] = cand.get("est_bytes")
            self._apply_fields(xref, fields)
            return
        self._mark_pending(xref)

    def _candidate(self, xref, target_id):
        im = self._find_image(xref)
        for cand in (im or {}).get("candidates") or []:
            if cand.get("id") == target_id:
                return cand
        return None

    def _find_image(self, xref):
        for page in self._holder().get("pages") or []:
            if not page:
                continue
            for im in page.get("images") or []:
                if im.get("xref") == xref:
                    return im
        return None

    def _image_facts(self, xref):
        im = self._find_image(xref) or {}
        return int(im.get("bytes") or 0), im.get("filter_name") or ""

    def _mark_pending(self, xref):
        for page in (self._data or {}).get("pages") or []:
            if not page:
                continue
            row = self._row_for_page(page["page"])
            if row is None:
                continue
            for i in range(row.childCount()):
                child = row.child(i)
                if child.data(0, _XREF) == xref:
                    child.setText(3, "...")

    def _on_encoded(self, payload):
        key = payload.get("share") or str(payload.get("xref"))
        if payload.get("token") != self._tokens.get(key):
            return
        result = payload.get("result")
        if not result:
            return
        target = payload.get("target")
        self._encoded[(key, target)] = result
        result = dict(result)
        result["id"] = target
        self._apply_fields(payload.get("xref"), result)

    def _apply_fields(self, xref, fields):
        holder = self._holder()
        if not holder.get("pages"):
            return
        from krop.analyze import apply_chosen_target
        apply_chosen_target(holder, xref, fields)
        self._refresh_after_choice()

    def _reapply_choices(self):
        if not self._data or not self._choices:
            return
        from krop.analyze import apply_chosen_target
        seen = set()
        for page in self._data.get("pages") or []:
            if not page:
                continue
            for im in page.get("images") or []:
                key = im.get("share_key") or str(im.get("xref"))
                if key in seen:
                    continue
                chosen = self._choices.get(key) or self._choices.get(str(im.get("xref")))
                if not chosen or chosen == im.get("target"):
                    continue
                cand = None
                for item in im.get("candidates") or []:
                    if item.get("id") == chosen and item.get("est_bytes") is not None:
                        cand = item
                        break
                cached = self._encoded.get((key, chosen))
                if cached:
                    cand = dict(cand or {})
                    cand.update(cached)
                    cand["id"] = chosen
                if not cand:
                    continue
                seen.add(key)
                apply_chosen_target(self._data, im.get("xref"), cand)

    def _refresh_after_choice(self):
        data = self._holder()
        self._selecting = True
        try:
            for page in data.get("pages") or []:
                if not page:
                    continue
                row = self._row_for_page(page["page"])
                if row is None:
                    continue
                row.setText(5, _size_text(page.get("image_bytes"), page.get("image_est"), page.get("savings_pct")))
                for im in page.get("images") or []:
                    self._refresh_image_texts(row, im)
            summary = data.get("summary")
            if summary:
                self._show_summary(summary)
        finally:
            self._selecting = False

    def _refresh_image_texts(self, row, im):
        for i in range(row.childCount()):
            child = row.child(i)
            if child.data(0, _XREF) != im.get("xref"):
                continue
            child.setText(1, im.get("detail") or "")
            child.setText(3, im.get("target_text") or "")
            combo = self.tree.itemWidget(child, 2)
            if combo is None:
                continue
            by_id = {c.get("id"): c for c in im.get("candidates") or []}
            combo.blockSignals(True)
            for index in range(combo.count()):
                cand = by_id.get(combo.itemData(index))
                if cand is None:
                    continue
                combo.setItemText(index, _candidate_label(cand, im, self.error_threshold()))
                self._style_combo_row(combo, index, cand, im)
            chosen = im.get("chosen_target") or im.get("target")
            index = combo.findData(chosen)
            if index >= 0 and combo.currentIndex() != index:
                combo.setCurrentIndex(index)
            combo.blockSignals(False)
            _refresh_open_popup(combo)
            return

    def _style_combo_row(self, combo, index, cand, im):
        try:
            fg = Qt.ItemDataRole.ForegroundRole
            tip_role = Qt.ItemDataRole.ToolTipRole
        except AttributeError:
            fg = Qt.ForegroundRole
            tip_role = Qt.ToolTipRole
        if cand.get("over"):
            combo.setItemData(index, QBrush(QColor(160, 70, 0)), fg)
        else:
            combo.setItemData(index, None, fg)
        tip = _candidate_tip(cand, im, self.error_threshold())
        combo.setItemData(index, tip, tip_role)
        if combo.currentIndex() == index:
            combo.setToolTip(tip)

    def follow_page(self, page):
        """Select the row for this 1-based page. Does not change the viewer."""
        try:
            page = int(page)
        except (TypeError, ValueError):
            return
        if page < 1:
            return
        self._follow_page = page
        if getattr(self, "_scheduler", None) is not None:
            self._scheduler.set_focus(page - 1)
            self._submit_available()
        self._select_followed()
        self.focus_changed.emit(page)
        self.preview_changed.emit()
        self._refresh_progress()

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


def _over_phrase(cand, limit):
    """Name the test that actually failed. Never claim a > b when a is not greater."""
    parts = []
    try:
        mae_lim = float(limit) if limit is not None else None
    except (TypeError, ValueError):
        mae_lim = None
    tail = None if mae_lim is None else max(16.0, mae_lim * 4.0)
    mae = cand.get("error_mae")
    if mae is not None and mae_lim is not None and float(mae) > mae_lim:
        parts.append("mae %.1f > %s" % (float(mae), _limit_text(mae_lim)))
    p995 = cand.get("error_p995")
    if p995 is not None and tail is not None and float(p995) > tail:
        parts.append("p99.5 %.1f > %s" % (float(p995), _limit_text(tail)))
    if not parts:
        return "over limit"
    return " and ".join(parts) + " (over limit)"


def _scrollable_combo(combo):
    """A short list with a scrollbar. Native macOS/Fusion popups ignore maxVisibleItems."""
    view = QListView()
    try:
        scroll = Qt.ScrollBarPolicy.ScrollBarAsNeeded
    except AttributeError:
        scroll = Qt.ScrollBarAsNeeded
    view.setVerticalScrollBarPolicy(scroll)
    combo.setView(view)
    combo.setMaxVisibleItems(15)
    combo.setStyleSheet("QComboBox { combobox-popup: 0; }")


def _limit_text(limit):
    if limit is None:
        return None
    value = float(limit)
    if abs(value - round(value)) < 1e-6:
        return str(int(round(value)))
    return "%g" % value


def _candidate_label(cand, image=None, limit=None):
    """Dropdown text: structure, error, size, and why a row is orange or unfinished."""
    if cand.get("id") == "keep":
        text = ""
        if image is not None and image.get("keep_text"):
            text = image.get("keep_text")
        elif cand.get("label") and str(cand.get("label")).startswith("keep:"):
            text = cand.get("label")
        else:
            text = "keep"
        if "mae" not in text:
            text += "  mae 0"
        return text
    label = cand.get("label") or cand.get("id") or ""
    if cand.get("note"):
        return "%s  %s" % (label, cand.get("note"))
    if cand.get("pending"):
        return "%s  measuring…" % label
    bits = [label]
    if cand.get("error_mae") is not None and not cand.get("over"):
        bits.append("mae %.1f" % float(cand["error_mae"]))
    if cand.get("est_bytes") is not None:
        bits.append(_fmt_bytes(cand["est_bytes"]))
    pct = cand.get("savings_pct")
    if pct is not None and not cand.get("over"):
        pct = float(pct)
        if pct > 0.5:
            bits.append("%.0f%% smaller" % pct)
        elif pct < -0.5:
            bits.append("%.0f%% larger" % (-pct))
    if cand.get("over"):
        bits.append(_over_phrase(cand, limit))
    return "  ".join(bits)


def _candidate_tip(cand, image=None, limit=None):
    shown = _limit_text(limit)
    if cand.get("id") == "keep":
        return "Original encoding, mean error 0. " + _candidate_label(cand, image, limit)
    if cand.get("note_detail"):
        return cand.get("note_detail")
    if cand.get("note"):
        return cand.get("note")
    if cand.get("pending"):
        return "measuring… means the size and error are still being computed."
    if cand.get("over"):
        return ("Orange because %s. The limit is the mean and the 99.5th percentile "
                "of absolute error, not the single worst pixel. You can still choose it."
                % _over_phrase(cand, limit))
    return _candidate_label(cand, image, limit)


def _refresh_open_popup(combo):
    """Push new labels into a combo whose popup is already open."""
    view = combo.view()
    model = combo.model()
    if model is not None and model.rowCount():
        try:
            top = model.index(0, 0)
            bottom = model.index(model.rowCount() - 1, 0)
            model.dataChanged.emit(top, bottom)
        except Exception:
            pass
    if view is not None:
        if view.isVisible():
            view.reset()
        view.viewport().update()
    current = combo.currentIndex()
    if current >= 0:
        combo.setCurrentIndex(current)


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
