# -*- coding: utf-8 -*-
"""Activity log for krop (local patch, Nov 2026).

On by default: ~/.local/state/krop-trimall/activity.log, recreated at start and
deleted on clean exit (kept after a crash).  --log FILE / KROP_LOG=FILE appends
to FILE and keeps it; --log off / KROP_LOG=off disables.  Writes one timestamped, line-flushed entry per event so the file
can be followed live with `tail -f`:
  ACTION   toolbar/menu actions        UI      widget value changes
  CALL/RET every slot + trim function, with arguments, results and timing
  SEL      selection boxes after each mouse release / key press in the view
  ERROR    uncaught exceptions with traceback
"""
import functools, os, sys, time, traceback

from krop.qt import *

_f = None
DEFAULT = "~/.local/state/krop-trimall/activity.log"
_temp = None


def log(kind, msg):
    if _f is None:
        return
    t = time.strftime("%H:%M:%S") + ".%03d" % (int(time.time() * 1000) % 1000)
    _f.write("%s %-6s %s\n" % (t, kind, msg))
    _f.flush()


def _fmt(v):
    if isinstance(v, (QRect, QRectF)):
        return "(%g,%g %gx%g)" % (v.x(), v.y(), v.width(), v.height())
    if isinstance(v, QImage):
        return "QImage %dx%d" % (v.width(), v.height())
    s = repr(v)
    return s if len(s) <= 120 else s[:117] + "..."


def traced(fn, name=None):
    name = name or fn.__qualname__
    @functools.wraps(fn)
    def w(*a, **k):
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
        if t in (QEvent.MouseButtonRelease, QEvent.KeyRelease) and \
                obj is self.win.ui.documentView.viewport() or \
                (t == QEvent.KeyRelease and obj is self.win.ui.documentView):
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


def install(win, path):
    """path None -> default log, truncated at start and deleted on clean exit."""
    global _f, _temp
    if path and path.lower() in ("off", "0", "no", "false"):
        return
    if not path:
        path = _temp = os.path.expanduser(DEFAULT)
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _f = open(path, "w" if _temp else "a", buffering=1)
    log("START", "krop pid %d, argv %r" % (os.getpid(), sys.argv))

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

    filt = _Filter(win)
    win.ui.documentView.viewport().installEventFilter(filt)
    win.ui.documentView.installEventFilter(filt)
    win._activityFilter = filt
    QApplication.instance().aboutToQuit.connect(_stop)


def _stop():
    """Clean exit: delete the default log (crashes never get here, so it stays)."""
    global _f
    log("STOP", "krop exiting")
    if _temp and _f:
        _f.close(); _f = None
        try:
            os.remove(_temp)
        except OSError:
            pass
