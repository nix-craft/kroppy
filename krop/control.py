# -*- coding: utf-8 -*-
"""Local control socket for a running kroppy window.

Off unless the process was started with --control or KROPPY_CONTROL.
The socket is a Unix domain socket (QLocalServer), mode 0600, never TCP.
Requests are one JSON object per line and are handled on the GUI thread.
"""
import json
import os
import re
import sys
import tempfile
import time


def default_socket_path(pid=None):
    """$XDG_RUNTIME_DIR/kroppy-<pid>.sock, or /tmp/kroppy-$USER.sock."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "kroppy-%d.sock" % int(pid or os.getpid()))
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
    return os.path.join(tempfile.gettempdir(), "kroppy-%s.sock" % user)


def resolve_control_path(cli_value, env_value=None):
    """Return the socket path to listen on, or None when control is off.

    cli_value is None when --control was not passed, "default" when it was
    passed without a path, or a path. env_value is KROPPY_CONTROL. The flag
    wins when both are set.
    """
    chosen = cli_value
    if chosen is None:
        chosen = env_value
    if chosen is None:
        return None
    text = str(chosen).strip()
    if not text:
        return None
    if text == "default":
        return default_socket_path()
    return os.path.abspath(os.path.expanduser(text))


def socket_dirs():
    folders = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        folders.append(runtime)
    tmp = tempfile.gettempdir()
    if tmp not in folders:
        folders.append(tmp)
    return folders


def discover_sockets():
    """kroppy-*.sock files in the runtime dir and the temp dir."""
    import stat as statmod
    found = []
    for folder in socket_dirs():
        if not folder or not os.path.isdir(folder):
            continue
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not (name.startswith("kroppy-") and name.endswith(".sock")):
                continue
            path = os.path.join(folder, name)
            try:
                mode = os.stat(path).st_mode
            except OSError:
                continue
            if statmod.S_ISSOCK(mode):
                found.append(path)
    return sorted(found)


_COMPACT = re.compile(r"^(indexed|jpeg|grayjpeg|gray-jpeg|gray)(\d+)(-d)?$")


def normalize_target(value):
    """Accept indexed16 as well as indexed-16. Unknown text is left as given."""
    text = str(value or "").strip()
    low = text.lower()
    if low in ("keep", "1bpp", "jbig2", "rgb-24"):
        return low
    if low == "rgb24":
        return "rgb-24"
    match = _COMPACT.match(low)
    if not match:
        return text
    kind, num, dither = match.group(1), match.group(2), match.group(3) or ""
    if kind == "grayjpeg":
        kind = "gray-jpeg"
    return "%s-%s%s" % (kind, num, dither)


def _plain(value):
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        import numpy as np
        if isinstance(value, np.generic):
            return value.item()
    except Exception:
        pass
    return str(value)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError("expected a boolean, got %r" % (value,))


def _local_server():
    try:
        from PyQt6.QtNetwork import QLocalServer, QLocalSocket
    except ImportError:
        from PyQt5.QtNetwork import QLocalServer, QLocalSocket
    return QLocalServer, QLocalSocket


class ControlError(Exception):
    pass


class ControlServer(object):
    """QLocalServer bound to one MainWindow. Construct this on the GUI thread."""

    def __init__(self, window, path):
        from krop.qt import QObject, pyqtSignal, QEvent
        try:
            from krop.qt import Qt
            queued = Qt.ConnectionType.QueuedConnection
            close_type = QEvent.Type.Close
        except AttributeError:
            from krop.qt import Qt
            queued = Qt.QueuedConnection
            close_type = QEvent.Close

        class _Hub(QObject):
            incoming = pyqtSignal(object, object)

            def __init__(self, parent, close_type):
                QObject.__init__(self, parent)
                self._window = parent
                self._close_type = close_type
                self.server = None

            def eventFilter(self, obj, event):
                if obj is self._window and event.type() == self._close_type and self.server is not None:
                    self.server.stop()
                return False

            def deliver(self, sock, line):
                if self.server is not None:
                    self.server._dispatch(sock, line)

        self.window = window
        self.path = os.path.abspath(path)
        self._close_type = close_type
        self._buffers = {}
        self._stopped = False
        QLocalServer, _sock = _local_server()
        self._server_cls = QLocalServer
        self.server = QLocalServer(window)
        self._hub = _Hub(window, close_type)
        self._hub.server = self
        self._hub.incoming.connect(self._hub.deliver, queued)
        self.server.newConnection.connect(self._accept)
        window.installEventFilter(self._hub)

    def start(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass
        self._server_cls.removeServer(self.path)
        try:
            option = self._server_cls.SocketOption.UserAccessOption
        except AttributeError:
            option = self._server_cls.UserAccessOption
        self.server.setSocketOptions(option)
        if not self.server.listen(self.path):
            raise ControlError(self.server.errorString() or "could not listen")
        os.chmod(self.path, 0o600)
        sys.stderr.write(self.path + "\n")
        sys.stderr.flush()
        return self.path

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        try:
            self.server.close()
        except Exception:
            pass
        if self.path and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass

    def _accept(self):
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            self._buffers[sock] = bytearray()
            sock.readyRead.connect(lambda s=sock: self._read(s))
            sock.disconnected.connect(lambda s=sock: self._buffers.pop(s, None))

    def _read(self, sock):
        buf = self._buffers.setdefault(sock, bytearray())
        data = sock.readAll()
        try:
            raw = bytes(data)
        except Exception:
            raw = data.data() if hasattr(data, "data") else b""
        buf.extend(raw)
        while b"\n" in buf:
            line, _, rest = bytes(buf).partition(b"\n")
            self._buffers[sock] = bytearray(rest)
            buf = self._buffers[sock]
            self._hub.incoming.emit(sock, line)

    def _dispatch(self, sock, line):
        req_id = None
        try:
            text = line.decode("utf-8").strip()
            if not text:
                return
            msg = json.loads(text)
            if not isinstance(msg, dict):
                raise ControlError("request must be a JSON object")
            req_id = msg.get("id")
            cmd = msg.get("cmd")
            args = msg.get("args") or {}
            if not isinstance(args, dict):
                raise ControlError("args must be an object")
            if not cmd:
                raise ControlError("missing cmd")
            result = self._run(str(cmd), args, sock, req_id)
            if result is _DEFERRED:
                return
            self._reply(sock, req_id, True, result)
        except Exception as exc:
            self._reply(sock, req_id, False, error=str(exc))

    def _reply(self, sock, req_id, ok, result=None, error=None):
        body = {"ok": bool(ok)}
        if req_id is not None:
            body["id"] = req_id
        if ok:
            body["result"] = _plain(result)
        else:
            body["error"] = error or "error"
        try:
            payload = (json.dumps(body) + "\n").encode("utf-8")
            sock.write(payload)
            sock.flush()
        except Exception:
            pass

    def _run(self, cmd, args, sock, req_id):
        handler = getattr(self, "_cmd_" + cmd, None)
        if handler is None:
            raise ControlError("unknown command %s" % cmd)
        return handler(args, sock, req_id)

    def _cmd_ping(self, args, sock, req_id):
        return {"pong": True}

    def _cmd_state(self, args, sock, req_id):
        return _state(self.window)

    def _cmd_open(self, args, sock, req_id):
        path = args.get("path")
        if not path:
            raise ControlError("open requires path")
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(path):
            raise ControlError("no such file: %s" % path)
        self.window.openFile(path)
        viewer = self.window.viewer
        pages = 0 if viewer.isEmpty() else int(viewer.numPages())
        return {"path": path, "pages": pages}

    def _cmd_goto(self, args, sock, req_id):
        page = _page_number(args.get("page"))
        viewer = self.window.viewer
        if viewer.isEmpty():
            raise ControlError("no document")
        viewer.currentPageIndex = page - 1
        self.window.updateControls()
        self.window.slotFitInView(self.window.ui.actionFitInView.isChecked())
        return {"page": int(viewer.currentPageIndex) + 1}

    def _cmd_zoom(self, args, sock, req_id):
        factor = args.get("factor", args.get("zoom"))
        if factor is None:
            raise ControlError("zoom requires factor")
        view = self.window.ui.documentView
        if str(factor).strip().lower() == "fit":
            self.window.ui.actionFitInView.setChecked(True)
            self.window.slotFitInView(True)
            return {"fit": True, "zoom": _zoom_of(self.window)}
        value = float(factor)
        if value <= 0:
            raise ControlError("zoom factor must be positive")
        self.window.ui.actionFitInView.setChecked(False)
        view.resetTransform()
        view.scale(value, value)
        return {"fit": False, "zoom": _zoom_of(self.window)}

    def _cmd_scroll_to_image(self, args, sock, req_id):
        xref = _xref(args.get("xref"))
        return _scroll_to(self.window, xref)

    def _cmd_analysis(self, args, sock, req_id):
        doc = _analysis_document(self.window.analysisPanel)
        if doc is None:
            raise ControlError("no analysis")
        page = args.get("page")
        if page is None or page == "":
            return doc
        number = _page_number(page)
        for rec in doc.get("pages") or []:
            if rec and int(rec.get("page") or 0) == number:
                return rec
        raise ControlError("page %d is not ready" % number)

    def _cmd_select_image(self, args, sock, req_id):
        page = _page_number(args.get("page"))
        xref = _xref(args.get("xref"))
        _show_analysis(self.window)
        self.window.viewer.currentPageIndex = page - 1
        self.window.updateControls()
        item = _tree_item(self.window.analysisPanel, page, xref)
        if item is None:
            raise ControlError("image xref %d is not on page %d" % (xref, page))
        self.window.analysisPanel.tree.setCurrentItem(item)
        return {"page": page, "xref": xref, "target": _chosen_target(self.window.analysisPanel, xref)}

    def _cmd_set_target(self, args, sock, req_id):
        xref = _xref(args.get("xref"))
        target = normalize_target(args.get("target"))
        if not target:
            raise ControlError("set_target requires target")
        panel = self.window.analysisPanel
        im = panel._find_image(xref)
        if im is None:
            raise ControlError("unknown image xref %d" % xref)
        _show_analysis(self.window)
        page = int(im.get("page") or 1)
        if self.window.viewer.currentPageIndex != page - 1:
            self.window.viewer.currentPageIndex = page - 1
            self.window.updateControls()
        share = str(im.get("share_key") or "")
        panel._choose(xref, share, target)
        return {"xref": xref, "target": target, "page": page}

    def _cmd_set_option(self, args, sock, req_id):
        name = str(args.get("name") or "").strip().lower().replace("-", "_")
        if "value" not in args:
            raise ControlError("set_option requires value")
        value = args.get("value")
        panel = self.window.analysisPanel
        if name in ("mean_error", "error", "error_threshold", "mae", "threshold"):
            panel.errorBox.setValue(float(value))
            return {"name": "mean_error", "value": float(panel.errorBox.value())}
        if name in ("preview", "preview_target", "preview_toggle"):
            panel.previewBox.setChecked(_as_bool(value))
            return {"name": "preview", "value": bool(panel.previewBox.isChecked())}
        if name in ("analyze_other_pages", "background", "analyze_background"):
            panel.backgroundBox.setChecked(_as_bool(value))
            return {"name": "analyze_other_pages", "value": bool(panel.backgroundBox.isChecked())}
        raise ControlError("unknown option %s" % name)

    def _cmd_wait_idle(self, args, sock, req_id):
        timeout = args.get("timeout", 30)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            raise ControlError("timeout must be a number")
        if timeout < 0:
            raise ControlError("timeout must be >= 0")
        deadline = time.monotonic() + timeout
        self._poll_idle(sock, req_id, deadline)
        return _DEFERRED

    def _poll_idle(self, sock, req_id, deadline):
        from krop.qt import QTimer
        if _is_idle(self.window):
            self._reply(sock, req_id, True, {"idle": True})
            return
        if time.monotonic() >= deadline:
            self._reply(sock, req_id, False, error="timeout")
            return
        QTimer.singleShot(50, lambda: self._poll_idle(sock, req_id, deadline))

    def _cmd_grab_view(self, args, sock, req_id):
        path = _out_path(args.get("path"))
        widget = self.window.ui.documentView
        return _save_grab(widget, path)

    def _cmd_grab_window(self, args, sock, req_id):
        path = _out_path(args.get("path"))
        return _save_grab(self.window, path)

    def _cmd_export_image(self, args, sock, req_id):
        xref = _xref(args.get("xref"))
        which = str(args.get("which") or "").strip().lower()
        path = _out_path(args.get("path"))
        if which not in ("original", "preview", "preview_display"):
            raise ControlError("which must be original, preview, or preview_display")
        return _export_image(self.window, xref, which, path)

    def _cmd_log_tail(self, args, sock, req_id):
        from krop.activitylog import tail
        count = args.get("n", 50)
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise ControlError("n must be an integer")
        return {"lines": tail(count)}

    def _cmd_quit(self, args, sock, req_id):
        from krop.qt import QTimer
        QTimer.singleShot(0, self.window.close)
        return {"quit": True}


_DEFERRED = object()


def start_control(window, path):
    """Listen on path. None leaves the process unchanged and creates no socket."""
    if not path:
        return None
    server = ControlServer(window, path)
    server.start()
    window._control = server
    return server


def _page_number(value):
    try:
        page = int(value)
    except (TypeError, ValueError):
        raise ControlError("page must be an integer")
    if page < 1:
        raise ControlError("page must be >= 1")
    return page


def _xref(value):
    try:
        xref = int(value)
    except (TypeError, ValueError):
        raise ControlError("xref must be an integer")
    if xref <= 0:
        raise ControlError("xref must be positive")
    return xref


def _out_path(value):
    if not value:
        raise ControlError("path is required")
    path = os.path.abspath(os.path.expanduser(str(value)))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return path


def _zoom_of(window):
    try:
        return abs(float(window.ui.documentView.transform().m11()))
    except Exception:
        return None


def _show_analysis(window):
    action = getattr(window, "actionAnalysis", None)
    if action is not None and not action.isChecked():
        action.setChecked(True)
    panel = window.analysisPanel
    if not panel.isVisible():
        panel.setVisible(True)


def _chosen_target(panel, xref):
    im = panel._find_image(xref) or {}
    share = str(im.get("share_key") or "")
    chosen = panel._choices.get(share) or panel._choices.get(str(xref))
    return chosen or im.get("chosen_target") or im.get("target")


def _tree_item(panel, page, xref):
    tree = panel.tree
    for row_i in range(tree.topLevelItemCount()):
        row = tree.topLevelItem(row_i)
        if panel._page_of(row) != page:
            continue
        for child_i in range(row.childCount()):
            child = row.child(child_i)
            try:
                got = int(child.data(0, _xref_role()))
            except (TypeError, ValueError):
                continue
            if got == int(xref):
                return child
    return None


def _xref_role():
    from krop.analyzepanel import _XREF
    return _XREF


def _analysis_document(panel):
    data = panel._data or {}
    if data.get("pages") and not data.get("error"):
        return data
    page_map = getattr(panel, "_pages", None) or {}
    pages = []
    images = []
    for key in sorted(page_map, key=lambda item: int(item)):
        rec = page_map[key]
        if not rec:
            continue
        pages.append(rec)
        images.extend(rec.get("images") or [])
    if not pages:
        if data.get("error"):
            return data
        return None
    summary = data.get("summary")
    if summary is None:
        try:
            from krop.analyze import _document
            summary = _document(pages, images)
        except Exception:
            summary = None
    return {"summary": summary, "pages": pages, "images": images}


def _state(window):
    viewer = window.viewer
    panel = window.analysisPanel
    empty = viewer.isEmpty()
    path = getattr(window, "originalFileName", None) or getattr(window, "fileName", None) or ""
    item = panel.tree.currentItem()
    selected_xref = None
    selected_page = None
    if item is not None:
        try:
            raw = item.data(0, _xref_role())
            selected_xref = int(raw) if raw else None
        except (TypeError, ValueError):
            selected_xref = None
        selected_page = panel._page_of(item)
    highlight = getattr(viewer, "_highlight_xref", None)
    sched = panel._scheduler.queue_summary()
    job = getattr(window, "_analysis_job", None)
    p0 = 0
    for thread in getattr(panel, "_p0_jobs", []) or []:
        try:
            if thread.isRunning():
                p0 += 1
        except Exception:
            pass
    worker = getattr(panel, "_worker", None)
    threshold = float(panel.error_threshold())
    size = window.size()
    return {
        "path": path or None,
        "page_count": 0 if empty else int(viewer.numPages()),
        "page": None if empty else int(viewer.currentPageIndex) + 1,
        "zoom": _zoom_of(window),
        "fit": bool(window.ui.actionFitInView.isChecked()),
        "window": {"width": int(size.width()), "height": int(size.height())},
        "selection": {
            "page": selected_page,
            "xref": selected_xref,
            "target": None if selected_xref is None else _chosen_target(panel, selected_xref),
            "highlight_xref": int(highlight) if highlight else None,
        },
        "scheduler": sched,
        "worker": {
            "analysis_running": bool(job is not None and job.isRunning()),
            "worker_running": bool(worker is not None and worker.isRunning()),
            "p0_running": p0,
            "running_tier": sched.get("running_tier"),
        },
        "options": {
            "mean_error": threshold,
            "percentile_limit": max(16.0, threshold * 4.0),
            "preview": bool(panel.preview_enabled()),
            "analyze_other_pages": bool(panel.backgroundBox.isChecked()),
        },
    }


def _is_idle(window):
    panel = window.analysisPanel
    if not _analysis_settled(window):
        return False
    sched = panel._scheduler
    if sched.running_tier in (0, 1):
        return False
    if sched.page_count > 0 and sched.has_priority(1):
        return False
    for thread in getattr(panel, "_p0_jobs", []) or []:
        try:
            if thread.isRunning():
                return False
        except Exception:
            continue
    viewer = window.viewer
    try:
        if viewer.busyVisible():
            return False
    except Exception:
        pass
    try:
        if viewer.missingPreviews():
            return False
    except Exception:
        pass
    return True


def _analysis_settled(window):
    """The page on screen has been scanned, or there is nothing to scan."""
    panel = window.analysisPanel
    if not getattr(panel, "_path", None):
        return True
    if getattr(panel, "_failed", False):
        return True
    sched = panel._scheduler
    if sched.page_count <= 0:
        data = panel._data or {}
        if data.get("error"):
            return True
        return False
    focus = sched.focus
    return sched.scanned.get(focus) in ("full", "cheap", "error")


def _scroll_to(window, xref):
    panel = window.analysisPanel
    im = panel._find_image(xref)
    viewer = window.viewer
    if im and im.get("page") and not viewer.isEmpty():
        viewer.currentPageIndex = int(im["page"]) - 1
        window.updateControls()
    rects = viewer.imagePlacementRects(xref) if not viewer.isEmpty() else []
    if not rects:
        raise ControlError("image xref %d is not on the current page" % xref)
    rect = rects[0]
    x, y, w, h = float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])
    window.ui.documentView.centerOn(x + w / 2.0, y + h / 2.0)
    return {"xref": xref, "rect": [x, y, w, h]}


def _save_grab(widget, path):
    pixmap = widget.grab()
    if pixmap is None or pixmap.isNull():
        raise ControlError("grab failed")
    if not pixmap.save(path, "PNG"):
        raise ControlError("could not write %s" % path)
    return {"path": path, "width": int(pixmap.width()), "height": int(pixmap.height())}


def _export_image(window, xref, which, path):
    source = getattr(window, "originalFileName", None) or getattr(window, "fileName", None)
    if not source:
        raise ControlError("no document")
    panel = window.analysisPanel
    im = panel._find_image(xref)
    target = _chosen_target(panel, xref) or (im or {}).get("target") or "keep"
    from krop.preview import load_image, orient_for_page, preview_rgba, _resize_box
    color, alpha = load_image(source, xref)
    viewer = window.viewer
    try:
        rotation = int(viewer.pageGetRotation(viewer.currentPageIndex) or 0)
    except Exception:
        rotation = 0
    if which == "original":
        saved = _write_array(path, color, alpha)
        stats = {"error_mae": 0.0, "error_p995": 0.0, "error_max": 0}
    elif which == "preview":
        rgba = preview_rgba(source, xref, target, rotation=0, max_edge=None)
        saved = _write_array(path, rgba[:, :, :3], rgba[:, :, 3])
        stats = _error_stats(color, rgba[:, :, :3])
    else:
        image = _display_image(window, source, xref, target, rotation)
        if image is None or image.isNull():
            raise ControlError("no display preview for xref %d" % xref)
        if not image.save(path, "PNG"):
            raise ControlError("could not write %s" % path)
        shown = _qimage_rgb(image)
        oriented = orient_for_page(color, rotation)
        if shown.shape[:2] != oriented.shape[:2]:
            oriented = _resize_box(oriented, shown.shape[0], shown.shape[1])
        saved = {"width": int(image.width()), "height": int(image.height())}
        stats = _error_stats(oriented, shown)
    saved.update(stats)
    saved.update({"path": path, "which": which, "xref": xref, "target": target})
    return saved


def _display_image(window, source, xref, target, rotation):
    """The pixmap MuPDF draws for this image at the current zoom, not a Pillow thumbnail."""
    if target == "keep":
        return None
    from krop.imagetarget import encode_file_image
    from krop.imagehighlight import VIEW_DPI
    from krop.pdfimage import cached_encoded, render_placed_image
    from krop.vieweritem import _rgba_qimage
    encode_file_image(source, xref, target, 0, "")
    encoded = cached_encoded(source, xref, target)
    if not encoded:
        return None
    zoom = 1.0
    viewer = window.viewer
    try:
        scene = viewer.scene()
        for view in scene.views() if scene is not None else []:
            zoom = max(zoom, abs(float(view.transform().m11())))
    except Exception:
        zoom = 1.0
    dpi = int(round(max(72.0, min(216.0, float(VIEW_DPI) * zoom))))
    shown = render_placed_image(source, xref, encoded, dpi=dpi)
    return _rgba_qimage(shown)


def _write_array(path, color, alpha):
    import numpy as np
    from PIL import Image
    arr = np.ascontiguousarray(color)
    if arr.ndim == 2:
        image = Image.fromarray(arr, "L")
        if alpha is not None:
            image = image.convert("RGBA")
            image.putalpha(Image.fromarray(np.ascontiguousarray(alpha), "L"))
    else:
        rgb = np.ascontiguousarray(arr[..., :3])
        if alpha is not None:
            image = Image.fromarray(np.dstack([rgb, np.ascontiguousarray(alpha)]), "RGBA")
        else:
            image = Image.fromarray(rgb, "RGB")
    image.save(path, format="PNG")
    return {"width": int(image.size[0]), "height": int(image.size[1])}


def _error_stats(original, got):
    import numpy as np
    from krop.imagetarget import _mae_max

    def rgb(arr):
        arr = np.ascontiguousarray(arr)
        if arr.ndim == 2:
            return np.repeat(arr[:, :, None], 3, axis=2)
        return arr[..., :3]

    src = rgb(original)
    out = rgb(got)
    if src.shape != out.shape:
        raise ControlError("cannot compare %s with %s" % (src.shape, out.shape))
    mae, p995, mx = _mae_max(src, out)
    return {"error_mae": round(float(mae), 2), "error_p995": round(float(p995), 2), "error_max": int(mx)}


def _qimage_rgb(image):
    import numpy as np
    from krop.qt import QImage
    try:
        fmt = QImage.Format.Format_RGBA8888
    except AttributeError:
        fmt = QImage.Format_RGBA8888
    image = image.convertToFormat(fmt)
    width, height = image.width(), image.height()
    bits = image.bits()
    bits.setsize(height * width * 4)
    arr = np.frombuffer(bits, np.uint8).reshape(height, width, 4).copy()
    return arr[:, :, :3]
