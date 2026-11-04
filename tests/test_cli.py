"""`krop --go` must crop and exit on its own (no window shown)."""
import os, subprocess, sys
import pymupdf


def test_go_exits_and_crops(scan, tmp_path):
    out = str(tmp_path / "go.pdf")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=root)
    subprocess.run([sys.executable, "-m", "krop", "--go", "--trim", "--trim-use", "all",
                    "--optimize", "no", "--log", "off", "-o", out, scan],
                   env=env, timeout=120, check=True)
    doc = pymupdf.open(out)
    assert doc.page_count == 4
    assert all(p.mediabox.width < 612 * 0.8 for p in doc)
