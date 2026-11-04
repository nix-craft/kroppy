"""Objects shared by several source pages must stay shared after cropping."""
import io, os, subprocess, sys
import pymupdf


def _shared_image_pdf(path):
    pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 600, 800), False)
    pix.clear_with(255)
    pix.set_rect(pymupdf.IRect(100, 100, 500, 700), (0,))
    png = pix.tobytes("png")
    doc = pymupdf.open()
    xref = 0
    for _ in range(3):
        p = doc.new_page(width=612, height=792)
        if xref: p.insert_image(p.rect, xref=xref)
        else: xref = p.insert_image(p.rect, stream=png)
    doc.save(path)


def test_shared_image_stays_shared(tmp_path):
    src, out = str(tmp_path / "in.pdf"), str(tmp_path / "out.pdf")
    _shared_image_pdf(src)
    assert len({x[0] for p in pymupdf.open(src) for x in p.get_images()}) == 1
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=root)
    subprocess.run([sys.executable, "-m", "krop", "--go", "--trim", "--trim-use", "all",
                    "--optimize", "no", "--log", "off", "-o", out, src],
                   env=env, timeout=120, check=True)
    doc = pymupdf.open(out)
    assert doc.page_count == 3
    assert all(p.mediabox.width < 612 * 0.9 for p in doc)      # really cropped
    assert len({x[0] for p in doc for x in p.get_images()}) == 1
    assert os.path.getsize(out) < os.path.getsize(src) * 1.3
