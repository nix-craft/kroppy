"""Bundled jbig2 / jbig2dec / jpegtran are preferred over PATH."""
import os, shutil, stat, subprocess, sys

import pytest

from krop import hardcrop


def test_bundled_codec_is_preferred(tmp_path, monkeypatch):
    codecs = tmp_path / "codecs"
    codecs.mkdir()
    tool = codecs / ("jbig2.exe" if sys.platform == "win32" else "jbig2")
    tool.write_text("#!/bin/sh\nprintf 'bundled\\n'\n")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(hardcrop.sys, "frozen", True, raising=False)
    monkeypatch.setattr(hardcrop.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    hardcrop._bundled_ready = False
    try:
        found = hardcrop.tool_path("jbig2")
        assert os.path.abspath(found) == os.path.abspath(str(tool))
        assert found != shutil.which("jbig2") or os.path.abspath(found) == os.path.abspath(str(tool))
    finally:
        hardcrop._bundled_ready = False


@pytest.mark.skipif(
    not all(shutil.which(name) for name in ("jbig2", "jbig2dec", "jpegtran")),
    reason="jbig2, jbig2dec, and jpegtran are not installed",
)
@pytest.mark.skipif(
    sys.platform.startswith("linux") and not shutil.which("patchelf"),
    reason="patchelf is required to stage Linux libraries",
)
def test_stage_codecs_smoke(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dest = tmp_path / "codecs"
    subprocess.check_call(
        [sys.executable, os.path.join(root, "packaging", "stage_codecs.py"), "--dest", str(dest)],
        cwd=root,
    )
    subprocess.check_call(
        [sys.executable, os.path.join(root, "packaging", "smoke_codecs.py"), "--bin", str(dest / "bin")],
    )
    notice = (dest / "licenses" / "NOTICES.txt").read_text()
    assert "Apache" in notice
    assert "Affero" in notice
    assert (dest / "licenses" / "jbig2dec-COPYING.txt").is_file()
    assert (dest / "licenses" / "Apache-2.0.txt").is_file()
