"""Local control socket. Off unless --control or KROPPY_CONTROL is set."""
import json
import os
import stat
import subprocess
import sys
import threading
import time

import pytest

from krop.control import (
    default_socket_path,
    discover_sockets,
    normalize_target,
    resolve_control_path,
    start_control,
)
from krop.ctl import build_args, transact


def test_control_is_off_unless_requested(tmp_path, monkeypatch, window):
    monkeypatch.delenv("KROPPY_CONTROL", raising=False)
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    assert resolve_control_path(None, None) is None
    assert resolve_control_path(None, "") is None
    assert start_control(window, None) is None
    assert os.listdir(run) == []
    assert not os.path.exists(default_socket_path())


def test_target_names_and_cli_args():
    assert normalize_target("indexed16") == "indexed-16"
    assert normalize_target("indexed-16") == "indexed-16"
    assert normalize_target("jpeg85") == "jpeg-85"
    assert normalize_target("gray2") == "gray-2"
    assert normalize_target("keep") == "keep"
    assert build_args("set_target", ["59", "indexed16"]) == {"xref": 59, "target": "indexed16"}
    assert build_args("export_image", ["59", "preview_display", "/tmp/p.png"]) == {
        "xref": 59, "which": "preview_display", "path": "/tmp/p.png",
    }
    assert build_args("state", []) == {}
    assert build_args("analysis", []) == {}
    assert build_args("analysis", ["2"]) == {"page": 2}


def _pump(qapp, thread, timeout):
    deadline = time.time() + timeout
    while thread.is_alive() and time.time() < deadline:
        qapp.processEvents()
        thread.join(0.02)
    thread.join(1)
    if thread.is_alive():
        raise AssertionError("control client timed out")


def _call(qapp, path, cmd, args=None, timeout=30):
    box = {}

    def work():
        try:
            box["resp"] = transact(path, cmd, args or {}, timeout=timeout)
        except Exception as exc:
            box["exc"] = exc

    thread = threading.Thread(target=work)
    thread.start()
    _pump(qapp, thread, timeout + 5)
    if "exc" in box:
        raise box["exc"]
    resp = box["resp"]
    assert resp.get("ok") is True, resp
    return resp["result"]


def test_control_socket_roundtrip(window, tmp_path, qapp, monkeypatch):
    import numpy as np
    import pymupdf

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    os.makedirs(tmp_path / "run")
    doc = pymupdf.open()
    page = doc.new_page()
    arr = np.zeros((48, 64, 3), np.uint8)
    arr[:] = (255, 255, 255)
    arr[:20] = (0, 0, 180)
    arr[20:36] = (0, 0, 0)
    pix = pymupdf.Pixmap(pymupdf.csRGB, 64, 48, np.ascontiguousarray(arr).tobytes(), False)
    xref = page.insert_image(pymupdf.Rect(72, 72, 300, 250), pixmap=pix)
    doc.new_page()
    path = str(tmp_path / "page.pdf")
    doc.save(path)
    doc.close()

    window.resize(900, 700)
    window.ui.documentView.setMinimumSize(400, 300)
    window.show()
    qapp.processEvents()

    sock = str(tmp_path / "run" / ("kroppy-%d.sock" % os.getpid()))
    server = start_control(window, sock)
    assert server is not None
    mode = os.stat(sock).st_mode
    assert stat.S_ISSOCK(mode)
    assert mode & 0o777 == 0o600
    assert discover_sockets() == [sock]

    assert _call(qapp, sock, "ping") == {"pong": True}
    state = _call(qapp, sock, "state")
    assert state["page_count"] == 0
    assert state["options"]["mean_error"] >= 0
    assert "queued" in state["scheduler"]

    opened = _call(qapp, sock, "open", {"path": path})
    assert opened["pages"] == 2
    _call(qapp, sock, "wait_idle", {"timeout": 25}, timeout=40)
    _call(qapp, sock, "set_option", {"name": "analyze_other_pages", "value": False})

    state = _call(qapp, sock, "state")
    assert state["path"] == path
    assert state["page_count"] == 2
    assert state["page"] == 1
    assert state["options"]["analyze_other_pages"] is False
    assert state["window"]["width"] > 0

    assert _call(qapp, sock, "goto", {"page": 2})["page"] == 2
    assert _call(qapp, sock, "goto", {"page": 1})["page"] == 1
    zoomed = _call(qapp, sock, "zoom", {"factor": "fit"})
    assert zoomed["fit"] is True
    zoomed = _call(qapp, sock, "zoom", {"factor": 1.5})
    assert zoomed["fit"] is False
    assert abs(zoomed["zoom"] - 1.5) < 0.05

    selected = _call(qapp, sock, "select_image", {"page": 1, "xref": xref})
    assert selected["xref"] == xref
    scrolled = _call(qapp, sock, "scroll_to_image", {"xref": xref})
    assert scrolled["rect"][2] > 0 and scrolled["rect"][3] > 0

    chosen = _call(qapp, sock, "set_target", {"xref": xref, "target": "indexed4"})
    assert chosen["target"] == "indexed-4"
    _call(qapp, sock, "wait_idle", {"timeout": 25}, timeout=40)

    analysis = _call(qapp, sock, "analysis", {"page": 1})
    images = analysis["images"]
    match = [im for im in images if im.get("xref") == xref]
    assert match
    assert match[0].get("candidates")
    assert match[0].get("chosen_target") == "indexed-4"
    whole = _call(qapp, sock, "analysis")
    assert len(whole["pages"]) >= 1

    original = _call(qapp, sock, "export_image", {
        "xref": xref, "which": "original", "path": str(tmp_path / "original.png"),
    })
    assert (original["width"], original["height"]) == (64, 48)
    assert original["error_mae"] == 0
    preview = _call(qapp, sock, "export_image", {
        "xref": xref, "which": "preview", "path": str(tmp_path / "preview.png"),
    })
    assert (preview["width"], preview["height"]) == (64, 48)
    assert preview["target"] == "indexed-4"
    assert preview["error_mae"] < 5
    shown = _call(qapp, sock, "export_image", {
        "xref": xref, "which": "preview_display", "path": str(tmp_path / "display.png"),
    })
    assert shown["width"] > 0 and shown["height"] > 0
    assert "error_mae" in shown
    for name in ("original.png", "preview.png", "display.png"):
        blob = (tmp_path / name).read_bytes()
        assert blob.startswith(b"\x89PNG")

    view = _call(qapp, sock, "grab_view", {"path": str(tmp_path / "view.png")})
    window_png = _call(qapp, sock, "grab_window", {"path": str(tmp_path / "window.png")})
    assert view["width"] > 0 and view["height"] > 0
    assert window_png["width"] > 0 and window_png["height"] > 0
    assert (tmp_path / "view.png").read_bytes().startswith(b"\x89PNG")

    lines = _call(qapp, sock, "log_tail", {"n": 400})["lines"]
    assert lines
    blob = "\n".join(lines)
    assert ("OPEN" in blob or "ANALYZE" in blob or "indexed4" in blob or os.path.basename(path) in blob)

    option = _call(qapp, sock, "set_option", {"name": "mean_error", "value": 6})
    assert option["value"] == 6
    assert _call(qapp, sock, "state")["options"]["mean_error"] == 6

    proc = subprocess.Popen(
        [sys.executable, "-m", "krop.ctl", "--sock", sock, "ping"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/workspace",
        env=dict(os.environ, PYTHONPATH="/workspace"),
    )
    deadline = time.time() + 10
    while proc.poll() is None and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)
    out, err = proc.communicate(timeout=5)
    assert proc.returncode == 0, err
    assert json.loads(out.decode())["result"] == {"pong": True}

    assert _call(qapp, sock, "quit")["quit"] is True
    qapp.processEvents()
    assert not window.isVisible()
    assert not os.path.exists(sock)


def _drain(pipe, sink):
    try:
        sink.append(pipe.read())
    except Exception:
        pass


def test_main_control_flag_answers_ping_and_state(tmp_path):
    """python -m krop --control PATH file.pdf must listen before the window is used."""
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()
    pdf = str(tmp_path / "file.pdf")
    doc.save(pdf)
    doc.close()
    sock = str(tmp_path / "kroppy-amy.sock")
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = "/workspace" + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("KROPPY_CONTROL", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "krop", "--control", sock, "--log", "off", pdf],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/workspace", env=env,
    )
    err_box = []
    out_box = []
    pipes = [
        threading.Thread(target=_drain, args=(proc.stderr, err_box), daemon=True),
        threading.Thread(target=_drain, args=(proc.stdout, out_box), daemon=True),
    ]
    for thread in pipes:
        thread.start()

    def stderr_text():
        return b"".join(err_box).decode("utf-8", "replace")

    try:
        deadline = time.time() + 20
        ping = None
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                ping = transact(sock, "ping", {}, timeout=2)
                break
            except OSError:
                time.sleep(0.05)
        assert proc.poll() is None, stderr_text()
        assert ping and ping.get("ok") is True and ping.get("result") == {"pong": True}, err
        state = None
        while time.time() < deadline:
            state = transact(sock, "state", {}, timeout=5)
            assert state.get("ok") is True, state
            if state["result"].get("path") == pdf:
                break
            time.sleep(0.05)
        assert state["result"]["path"] == pdf, state
        assert state["result"]["page_count"] == 1
        assert state["result"]["page"] == 1
        quit_resp = transact(sock, "quit", {}, timeout=5)
        assert quit_resp.get("ok") is True
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        for thread in pipes:
            thread.join(2)
    err = stderr_text()
    assert "referenced before assignment" not in err
    assert "control socket failed" not in err
    assert proc.returncode == 0, err
