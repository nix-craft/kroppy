"""The run log sits beside the PDF and records the session."""
import os, subprocess, sys
import pymupdf

from krop.version import __version__


def _env(tmp_path, **extra):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env.pop("KROP_LOG", None)
    env.update(extra)
    os.makedirs(env["XDG_CONFIG_HOME"], exist_ok=True)
    return env


def _pdf(path):
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    page.insert_text((40, 100), "log me")
    doc.save(path)
    doc.close()


def _run(args, env):
    return subprocess.run([sys.executable, "-m", "krop"] + args, env=env,
                          capture_output=True, text=True, timeout=120)


def test_go_writes_log_beside_input(tmp_path):
    src = tmp_path / "scan.pdf"
    _pdf(str(src))
    out = tmp_path / "out.pdf"
    env = _env(tmp_path)
    r = _run(["--go", "--optimize", "no", "--grid", "1", "-o", str(out), str(src)], env)
    assert r.returncode == 0, r.stderr
    log = src.with_suffix(".kroppy.log")
    assert log.is_file()
    text = log.read_text()
    assert "SESSION" in text
    assert __version__ in text
    assert "platform " in text
    assert "time " in text
    assert "OPEN" in text and "scan.pdf" in text
    assert "SAVE" in text
    assert "PAGE" in text and "MediaBox" in text
    # one session header (several SESSION lines), then a second run appends another
    assert text.count("SESSION kroppy") == 1
    r2 = _run(["--go", "--optimize", "no", "--grid", "1", "-o", str(out), str(src)], env)
    assert r2.returncode == 0, r2.stderr
    assert log.read_text().count("SESSION kroppy") == 2


def test_log_off_writes_nothing(tmp_path):
    src = tmp_path / "scan.pdf"
    _pdf(str(src))
    env = _env(tmp_path)
    r = _run(["--go", "--optimize", "no", "--log", "off", "--grid", "1",
              "-o", str(tmp_path / "out.pdf"), str(src)], env)
    assert r.returncode == 0, r.stderr
    assert not src.with_suffix(".kroppy.log").exists()


def test_explicit_log_and_help(tmp_path):
    src = tmp_path / "scan.pdf"
    _pdf(str(src))
    chosen = tmp_path / "mine.log"
    env = _env(tmp_path)
    help_r = _run(["--help"], env)
    assert help_r.returncode == 0
    assert ".kroppy.log" in help_r.stdout
    assert "off" in help_r.stdout
    r = _run(["--go", "--optimize", "no", "--log", str(chosen), "--grid", "1",
              "-o", str(tmp_path / "out.pdf"), str(src)], env)
    assert r.returncode == 0, r.stderr
    assert chosen.is_file()
    assert not src.with_suffix(".kroppy.log").exists()
    assert __version__ in chosen.read_text()


def test_fallback_is_named_in_the_status_bar(qapp, tmp_path, monkeypatch):
    from krop.mainwindow import MainWindow
    from krop import activitylog
    monkeypatch.setattr(activitylog, "user_log_dir", lambda: str(tmp_path / "userlog"))
    real = activitylog._try_open

    def wrapped(path):
        if str(path).endswith(".kroppy.log") and "userlog" not in str(path):
            raise PermissionError(path)
        return real(path)

    monkeypatch.setattr(activitylog, "_try_open", wrapped)
    saved = (activitylog._mode, activitylog._f, activitylog._path, list(activitylog._buf))
    activitylog._f = None
    activitylog._path = None
    activitylog._buf = []
    try:
        src = tmp_path / "doc.pdf"
        _pdf(str(src))
        w = MainWindow()
        activitylog.install(w, None)
        w.openFile(str(src), False)
        msg = w.statusBar().currentMessage()
        assert "userlog" in msg
        fallback = tmp_path / "userlog" / "doc.kroppy.log"
        assert fallback.is_file()
        text = fallback.read_text()
        assert __version__ in text
        assert "platform " in text
        assert "OPEN" in text
        w.ui.checkHardCrop.setChecked(False)
        w.ui.checkHardCrop.setChecked(True)
        w.ui.tabWidget.setCurrentIndex(0)
        w.ui.tabWidget.setCurrentIndex(1)
        text = fallback.read_text()
        assert "Hard Crop on Save" in text
        assert "tab = " in text
        w.close()
    finally:
        activitylog._mode, activitylog._f, activitylog._path, activitylog._buf = saved


def test_warning_text_is_logged(window, tmp_path):
    src = str(tmp_path / "plain.pdf")
    _pdf(src)
    w = window
    w.openFile(src, False)
    w.ui.editFile.setText(str(tmp_path / "missing-dir" / "out.pdf"))
    w.ui.checkGhostscript.setChecked(False)
    w.slotNewSelection()
    w.slotKrop()
    text = open(w.logPath).read()
    assert "Could not write cropped PDF" in text
    assert "cannot open file" in text
    assert "ERROR" in text
    assert "Traceback" in text
