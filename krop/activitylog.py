# -*- coding: utf-8 -*-
"""Activity log for kroppy.

By default each run appends to <input>.kroppy.log beside the PDF. The file is
flushed on every line. It is deleted when the session ends with no ERROR
lines (a clean save, or closing without saving). Errors keep the log.
--log FILE / KROP_LOG=FILE appends to that file and never deletes it.
--log off / KROP_LOG=off / --log-keep never writes nothing.

Every line is flushed so the file can be followed with `tail -f`:
  SESSION  version, platform, timestamp
  ACTION   toolbar/menu actions        UI      widget value changes
  CALL/RET every slot + trim function, with arguments, results and timing
  SEL      selection boxes after each mouse release / key press in the view
  PAGE     page boxes before and after a crop
  WARN     text shown to the user
  ERROR    exceptions with traceback
"""
import functools, os, sys, time, traceback

from krop.qt import *

_f = None
_mode = "off"          # off | explicit | auto
_policy = "on-error"   # on-error | always | off | discard
_location = "beside"   # beside | central
_cli_keep = None
_cli_location = None
_buf = []
_path = None
_errors = False
_last_input = None
_log_where = None
_stopped = False
_CENTRAL_DAYS = 30


def user_log_dir():
    """Per-user folder for run logs when they are not beside the PDF."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(base, "kroppy", "logs")
    if sys.platform == "darwin" and not os.environ.get("XDG_STATE_HOME"):
        return os.path.expanduser("~/Library/Logs/kroppy")
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "kroppy")


def tail(n=50):
    """The last n activity-log lines, oldest first. Empty when logging is off."""
    try:
        count = int(n)
    except (TypeError, ValueError):
        count = 50
    if count < 0:
        count = 0
    lines = []
    path = _path
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            lines = []
    if not lines:
        lines = list(_buf)
    return [line.rstrip("\n") for line in lines[-count:]]


def log(kind, msg):
    global _errors
    if _mode == "off":
        return
    if kind == "ERROR":
        _errors = True
    t = time.strftime("%H:%M:%S") + ".%03d" % (int(time.time() * 1000) % 1000)
    line = "%s %-6s %s\n" % (t, kind, msg)
    if _f is None:
        _buf.append(line)
        return
    _f.write(line)
    _f.flush()


def _try_open(path):
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    return open(path, "a", buffering=1)


def _write_header(path):
    import platform
    from krop.version import __version__
    log("SESSION", "kroppy %s" % __version__)
    log("SESSION", "time %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log("SESSION", "platform %s" % platform.platform())
    log("SESSION", "python %s" % platform.python_version())
    log("SESSION", "log %s" % path)
    log("SESSION", "argv %r" % (sys.argv,))


def _settings_value(key, default):
    try:
        from krop.qt import QSettings
        value = QSettings().value(key, default)
    except Exception:
        return default
    return default if value in (None, "") else value


def _close_log_file():
    """Flush and close the log. The path stays set so it can be deleted next."""
    global _f
    if _f is None:
        return
    try:
        _f.flush()
        _f.close()
    except OSError:
        pass
    _f = None


def _delete_log_file(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _should_delete():
    """Whether the current auto log should be removed when its session ends."""
    if _mode != "auto":
        return False
    if _policy in ("always", "off"):
        return False
    if _policy == "discard":
        return True
    return not _errors


def _end_log(delete):
    """Write the closing line, close the handle, then optionally delete."""
    global _path
    path = _path
    if _f is not None:
        log("STOP", "kroppy exiting")
    _close_log_file()
    if delete:
        _delete_log_file(path)
        _path = None


def _switch_log(path):
    """Append a new session to path. Returns the path opened."""
    global _f, _path, _errors
    path = os.path.abspath(path)
    if _f is not None and _path == path:
        _write_header(path)
        return path
    if _f is not None:
        _end_log(_should_delete())
        _errors = False
    _f = _try_open(path)
    _path = path
    _write_header(path)
    if _buf:
        _f.write("".join(_buf))
        _f.flush()
        del _buf[:]
    return path


def prune_central_logs(days=None):
    """Delete central *.kroppy.log files older than `days` (default 30)."""
    if days is None:
        days = _CENTRAL_DAYS
    folder = user_log_dir()
    if not os.path.isdir(folder):
        return
    cutoff = time.time() - days * 86400
    current = os.path.abspath(_path) if _path else None
    for name in os.listdir(folder):
        if not name.endswith(".kroppy.log"):
            continue
        path = os.path.join(folder, name)
        if current and os.path.abspath(path) == current:
            continue
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def _auto_log_path(input_path):
    name = os.path.splitext(os.path.basename(input_path))[0] + ".kroppy.log"
    if _location == "central":
        return os.path.join(user_log_dir(), name)
    return os.path.splitext(os.path.abspath(input_path))[0] + ".kroppy.log"


def attach_input(input_path):
    """Open the run log for this PDF.

    Returns a path when the log is not beside the PDF (chosen central folder,
    or a fallback because the PDF's folder was not writable), otherwise None.
    No-op when logging is off or --log named a file.
    """
    global _last_input, _log_where
    _last_input = input_path
    if _mode != "auto" or not input_path:
        return None
    if _location == "central":
        prune_central_logs()
    target = _auto_log_path(input_path)
    beside = os.path.splitext(os.path.abspath(input_path))[0] + ".kroppy.log"
    try:
        _switch_log(target)
    except OSError as err:
        fallback = os.path.join(user_log_dir(), os.path.basename(beside))
        try:
            _switch_log(fallback)
        except OSError:
            log("WARN", "could not open a log (%s)" % err)
            _log_where = None
            return None
        log("WARN", "could not write %s (%s); logging to %s" % (beside, err, fallback))
        sys.stderr.write("kroppy: could not write %s (%s); logging to %s\n" % (beside, err, fallback))
        _log_where = "fallback"
        return fallback
    if os.path.abspath(target) == os.path.abspath(beside):
        _log_where = None
        return None
    _log_where = "central"
    return target


def log_where():
    """'fallback', 'central', or None when the log is beside the PDF."""
    return _log_where


def note_clean_save():
    """A save finished. Drop the log when this session has no errors."""
    global _errors
    if not _should_delete():
        return
    reopen = _last_input
    _end_log(delete=True)
    _errors = False
    if reopen and _mode == "auto":
        attach_input(reopen)


def apply_preferences():
    """Re-read Log/Keep and Log/Location unless this process locked them."""
    global _policy, _location, _mode
    if _cli_keep is None and _mode != "explicit":
        _policy = _saved_policy()
        if _policy == "off":
            _mode = "off"
            _end_log(delete=False)
        elif _mode == "off":
            _mode = "auto"
    if _cli_location is None:
        _location = _saved_location()


def _saved_policy():
    value = _settings_value("Log/Keep", "on-error")
    if value not in ("on-error", "always", "off"):
        return "on-error"
    return value


def _saved_location():
    value = _settings_value("Log/Location", "beside")
    if value not in ("beside", "central"):
        return "beside"
    return value


def _fmt(v):
    if isinstance(v, (QRect, QRectF)):
        return "(%g,%g %gx%g)" % (v.x(), v.y(), v.width(), v.height())
    if isinstance(v, QImage):
        return "QImage %dx%d" % (v.width(), v.height())
    s = repr(v)
    return s if len(s) <= 120 else s[:117] + "..."


def traced(fn, name=None):
    name = name or fn.__qualname__
    # Qt only passes extra signal args (e.g. triggered's `checked`) to slots
    # that accept them; *a would accept everything, so drop the surplus.
    import inspect
    try:
        ps = inspect.signature(fn).parameters.values()
        nmax = None if any(p.kind == p.VAR_POSITIONAL for p in ps) else \
            sum(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in ps)
    except (TypeError, ValueError):
        nmax = None
    @functools.wraps(fn)
    def w(*a, **k):
        if nmax is not None:
            a = a[:nmax]
        args = [ _fmt(x) for x in a if not hasattr(x, 'ui') ]
        args += ["%s=%s" % (kk, _fmt(vv)) for kk, vv in k.items()]
        log("CALL", "%s(%s)" % (name, ", ".join(args)))
        t = time.time()
        try:
            r = fn(*a, **k)
        except Exception:
            log("ERROR", "%s raised:\n%s" % (name, traceback.format_exc().rstrip()))
            raise
        log("RET", "%s -> %s  [%.0f ms]" % (name, _fmt(r), (time.time() - t) * 1000))
        return r
    return w


def _selections(win):
    try:
        idx = win.viewer.currentPageIndex
        out = []
        for i, s in enumerate(win.selections.items):
            if s.selectionVisibleOnPage(idx):
                out.append("#%d %s crop(l,t,r,b)=%s" % (i, _fmt(s.mapRectToImage(s.rect)), _fmt(s.cropValues())))
        img = win.viewer.getImage(idx)
        log("SEL", "page %d (image %dx%d): %s" % (idx + 1, img.width(), img.height(),
                                                   "; ".join(out) or "none"))
    except Exception as e:
        log("SEL", "unavailable: %r" % e)


class _Filter(QObject):
    def __init__(self, win):
        QObject.__init__(self, win)
        self.win = win

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.Type.MouseButtonRelease, QEvent.Type.KeyRelease) and \
                obj is self.win.ui.documentView.viewport() or \
                (t == QEvent.Type.KeyRelease and obj is self.win.ui.documentView):
            QTimer.singleShot(0, lambda: _selections(self.win))
        return False


def patch_classes(path):
    """Must run before MainWindow() so signal connections use the traced slots."""
    if path and path.lower() in ("off", "0", "no", "false"):
        return
    import krop.mainwindow as mw
    C = mw.MainWindow
    for n, f in list(vars(C).items()):
        if callable(f) and (n.startswith("slot") or n in ("openFile", "trimMarginsSelection")):
            setattr(C, n, traced(f, n))
    for n in ("copierTrimMargins", "autoTrimMargins"):
        if hasattr(mw, n):
            setattr(mw, n, traced(getattr(mw, n), n))


def _disabled(path):
    return bool(path) and str(path).lower() in ("off", "0", "no", "false")


def install(win, path, keep=None, location=None):
    """path None -> log beside the input PDF (or the central folder) once opened.
    path 'off' -> disable. Any other path is appended to and never deleted.
    keep/location override the saved preferences for this process.
    """
    global _mode, _f, _path, _buf, _policy, _location, _cli_keep, _cli_location
    global _errors, _stopped, _last_input, _log_where
    if _f is not None:
        _end_log(_should_delete())
    _errors = False
    _stopped = False
    _last_input = None
    _log_where = None
    _cli_keep = keep
    _cli_location = location
    _location = location or _saved_location()
    if keep == "never":
        _policy = "off"
    elif keep == "always":
        _policy = "always"
    elif keep == "on-error":
        _policy = "on-error"
    else:
        _policy = _saved_policy()
    if _location == "central":
        prune_central_logs()
    if _disabled(path) or (_policy == "off" and not (path and not _disabled(path))):
        _mode = "off"
        return
    if path:
        _mode = "explicit"
        _policy = "always"
        _switch_log(os.path.expanduser(path))
    else:
        _mode = "auto"
        _buf = []

    old = sys.excepthook
    def hook(et, ev, tb):
        log("ERROR", "uncaught:\n" + "".join(traceback.format_exception(et, ev, tb)).rstrip())
        old(et, ev, tb)
    sys.excepthook = hook

    # every action and widget change
    for a in win.findChildren(QAction):
        if a.text():
            a.triggered.connect(lambda c=False, a=a: log("ACTION", "%s%s" % (
                a.text().replace("&", ""), " (checked=%s)" % c if a.isCheckable() else "")))
    for w in win.findChildren(QAbstractButton):
        if w.isCheckable() and w.text():
            w.toggled.connect(lambda v, w=w: log("UI", "%s = %s" % (w.text().replace("&", ""), v)))
    for w in win.findChildren(QSpinBox):
        w.valueChanged.connect(lambda v, w=w: log("UI", "%s = %s" % (w.objectName() or w.prefix().strip(), v)))
    for w in win.findChildren(QLineEdit):
        w.editingFinished.connect(lambda w=w: log("UI", "%s = %r" % (w.objectName(), w.text())))
    for w in win.findChildren(QComboBox):
        w.currentIndexChanged.connect(lambda i, w=w: log("UI", "%s = %r" % (w.objectName(), w.currentText())))
    tabs = getattr(win.ui, "tabWidget", None)
    if tabs is not None:
        tabs.currentChanged.connect(lambda i, w=tabs: log(
            "UI", "tab = %r" % w.tabText(i)))

    filt = _Filter(win)
    win.ui.documentView.viewport().installEventFilter(filt)
    win.ui.documentView.installEventFilter(filt)
    win._activityFilter = filt
    app = QApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(_stop)


def _stop():
    global _stopped
    if _stopped:
        return
    _stopped = True
    _end_log(_should_delete())
