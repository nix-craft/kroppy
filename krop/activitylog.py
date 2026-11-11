# -*- coding: utf-8 -*-
"""Activity log for krop.

By default each run appends to <input>.krop.log beside the PDF being opened.
--log FILE / KROP_LOG=FILE appends to that file instead; --log off / KROP_LOG=off
disables logging. If the folder beside the PDF is not writable, the log goes
to ~/.local/state/krop-trimall/ (or $XDG_STATE_HOME/krop-trimall).

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
_buf = []
_path = None


def user_log_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "krop-trimall")


def log(kind, msg):
    if _mode == "off":
        return
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
    log("SESSION", "krop %s" % __version__)
    log("SESSION", "time %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log("SESSION", "platform %s" % platform.platform())
    log("SESSION", "python %s" % platform.python_version())
    log("SESSION", "log %s" % path)
    log("SESSION", "argv %r" % (sys.argv,))


def _switch_log(path):
    """Append a new session to path. Returns the path opened."""
    global _f, _path
    path = os.path.abspath(path)
    if _f is not None and _path == path:
        _write_header(path)
        return path
    if _f is not None:
        try:
            _f.flush()
            _f.close()
        except OSError:
            pass
        _f = None
    _f = _try_open(path)
    _path = path
    _write_header(path)
    if _buf:
        _f.write("".join(_buf))
        _f.flush()
        _buf.clear()
    return path


def attach_input(input_path):
    """Open the run log for this PDF.

    Returns the fallback path when the log could not be written beside the
    PDF, otherwise None. No-op when logging is off or --log named a file.
    """
    global _buf
    if _mode != "auto" or not input_path:
        return None
    beside = os.path.splitext(os.path.abspath(input_path))[0] + ".krop.log"
    try:
        _switch_log(beside)
        return None
    except OSError as err:
        name = os.path.basename(beside)
        fallback = os.path.join(user_log_dir(), name)
        try:
            _switch_log(fallback)
        except OSError:
            _mode_note = "could not open a log (%s)" % err
            log("WARN", _mode_note)
            return None
        log("WARN", "could not write %s (%s); logging to %s" % (beside, err, fallback))
        sys.stderr.write("krop: could not write %s (%s); logging to %s\n" % (beside, err, fallback))
        return fallback


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


def install(win, path):
    """path None -> log beside the input PDF once it is opened.
    path 'off' -> disable. Any other path is appended to for the whole run.
    """
    global _mode, _f, _path, _buf
    if _disabled(path):
        _mode = "off"
        return
    if path:
        _mode = "explicit"
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
    global _f
    log("STOP", "krop exiting")
    if _f is not None:
        try:
            _f.flush()
            _f.close()
        except OSError:
            pass
        _f = None
