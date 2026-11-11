"""Hard-crop on save: crop embedded images to the visible box, keep encoding."""
import io, os, shutil, subprocess, sys
import numpy as np
import pikepdf
import pymupdf
import pytest
from PIL import Image, ImageDraw

from krop.hardcrop import hardcrop_file, lzw_encode
from krop.qt import QRectF, QSettings


W, H = 600, 800
BOX = (80, 120, 520, 680)          # PDF user space, bottom-left origin
# image rows/cols that BOX covers when the image fills the page 1:1
EXP_L, EXP_T, EXP_R, EXP_B = 80, H - 680, 520, H - 120


def _pattern():
    im = Image.new("1", (W, H), 1)
    dr = ImageDraw.Draw(im)
    dr.rectangle([0, 0, W, 40], fill=0)          # outside the crop
    dr.rectangle([0, 0, 30, H], fill=0)
    dr.rectangle([120, 200, 400, 500], fill=0)   # inside the crop
    dr.line([(100, 150), (500, 700)], fill=0, width=3)
    return im


def _ccitt_payload(im, K=-1):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    try:
        h = im.height
        info = {278: h}
        opt = (1 if K > 0 else 0)
        if K >= 0 and opt:
            info[292] = opt
        im.save(path, "TIFF", compression="group4" if K < 0 else "group3", tiffinfo=info)
        with Image.open(path) as t:
            offs, cnts = t.tag_v2[273], t.tag_v2[279]
            offs = offs if isinstance(offs, tuple) else (offs,)
            cnts = cnts if isinstance(cnts, tuple) else (cnts,)
            start, count = offs[0], cnts[0]
        with open(path, "rb") as fh:
            return fh.read()[start:start + count]
    finally:
        os.remove(path)


def _save_image_pdf(path, encoded, *, bpc, cs, filt, parms=None, box=BOX,
                    w=W, h=H, contents=None, resources_extra=None):
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(w, h))
    st = pikepdf.Stream(pdf, encoded)
    st.Type = pikepdf.Name.XObject
    st.Subtype = pikepdf.Name.Image
    st.Width = w
    st.Height = h
    st.ColorSpace = cs
    st.BitsPerComponent = bpc
    st.Filter = filt
    if parms is not None:
        st.DecodeParms = parms
    xobjects = pikepdf.Dictionary(Im0=st)
    resources = pikepdf.Dictionary(XObject=xobjects)
    if resources_extra:
        resources_extra(pdf, page, resources, st)
    page.Resources = resources
    if contents is None:
        contents = b"q %d 0 0 %d 0 0 cm /Im0 Do Q\n" % (w, h)
    page.Contents = pikepdf.Stream(pdf, contents)
    if box is not None:
        arr = pikepdf.Array([float(v) for v in box])
        page.MediaBox = arr
        page.CropBox = pikepdf.Array([float(v) for v in box])
    # keep the image's own filter; default save would turn LZW into Flate
    pdf.save(path, compress_streams=False,
             stream_decode_level=pikepdf.StreamDecodeLevel.none,
             object_stream_mode=pikepdf.ObjectStreamMode.generate)
    return path


def _pdf_image(path, page=0):
    doc = pymupdf.open(path)
    imgs = doc[page].get_images(full=True)
    if not imgs:
        return None
    im = imgs[0]
    xref = im[0]
    pix = pymupdf.Pixmap(doc, xref)
    if pix.colorspace and pix.colorspace.n > 1:
        pix = pymupdf.Pixmap(pymupdf.csGRAY, pix)
    samples = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, 0]
    return {
        "w": im[2], "h": im[3], "bpc": im[4], "cs": im[5], "filt": im[8],
        "raw": doc.xref_stream_raw(xref),
        "samples": samples,
        "parms": doc.xref_get_key(xref, "DecodeParms")[1],
        "n": len(doc[page].get_images()),
    }


def _render(path, page=0, zoom=1):
    doc = pymupdf.open(path)
    pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy()


def _assert_same(a, b):
    assert a.shape == b.shape
    diff = int(np.count_nonzero(a != b))
    assert diff == 0, "pixel mismatch: %d of %d" % (diff, a.size)


@pytest.fixture
def reset_hardcrop_setting(qapp):
    s = QSettings()
    prev = s.value("PDF/HardCrop", "")
    s.setValue("PDF/HardCrop", "false")
    yield s
    s.setValue("PDF/HardCrop", prev if prev else "false")


def test_ccitt_stays_ccitt_and_matches_pixels(tmp_path):
    src = str(tmp_path / "ccitt.pdf")
    im = _pattern()
    _save_image_pdf(
        src, _ccitt_payload(im), bpc=1, cs=pikepdf.Name.DeviceGray,
        filt=pikepdf.Name.CCITTFaxDecode,
        parms=pikepdf.Dictionary(K=-1, Columns=W, Rows=H),
    )
    before = _pdf_image(src)
    assert "CCITT" in before["filt"]
    assert (before["w"], before["h"]) == (W, H)
    out = str(tmp_path / "out.pdf")
    shutil.copy(src, out)
    stats = hardcrop_file(out)
    assert stats["cropped"] == 1 and stats["skipped"] == 0
    after = _pdf_image(out)
    assert "CCITT" in after["filt"]
    assert "JBIG2" not in (after["filt"] or "")
    assert (after["w"], after["h"]) == (EXP_R - EXP_L, EXP_B - EXP_T)
    assert "/K-1" in after["parms"].replace(" ", "")
    # same samples as that region of the original image, and the page looks the same
    expect = before["samples"][EXP_T:EXP_B, EXP_L:EXP_R]
    _assert_same(after["samples"], expect)
    _assert_same(_render(src), _render(out))


def test_flate_and_lzw_keep_their_filters(tmp_path):
    im = _pattern().convert("L")
    raw = np.asarray(im).tobytes()
    import zlib
    cases = {
        "flate": (zlib.compress(raw), pikepdf.Name.FlateDecode, None),
        "lzw": (lzw_encode(raw), pikepdf.Name.LZWDecode, None),
    }
    for name, (encoded, filt, parms) in cases.items():
        src = str(tmp_path / (name + ".pdf"))
        _save_image_pdf(src, encoded, bpc=8, cs=pikepdf.Name.DeviceGray, filt=filt, parms=parms)
        assert _pdf_image(src)["filt"] == str(filt)[1:], name
        out = str(tmp_path / (name + "-out.pdf"))
        shutil.copy(src, out)
        stats = hardcrop_file(out)
        assert stats["cropped"] == 1 and stats["skipped"] == 0, name
        after = _pdf_image(out)
        assert after["filt"] == str(filt)[1:], name
        assert (after["w"], after["h"]) == (EXP_R - EXP_L, EXP_B - EXP_T)
        expect = np.asarray(im)[EXP_T:EXP_B, EXP_L:EXP_R]
        _assert_same(after["samples"], expect)
        _assert_same(_render(src), _render(out))


def zlib_compress(raw):
    import zlib
    return zlib.compress(raw)


def test_text_page_is_not_rewritten(tmp_path):
    path = str(tmp_path / "text.pdf")
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.insert_text((100, 400), "Hello hardcrop")
    doc.save(path)
    doc.close()
    # shrink the box the way krop does, without touching the drawing
    pdf = pikepdf.open(path, allow_overwriting_input=True)
    box = pikepdf.Array([float(v) for v in BOX])
    pdf.pages[0].MediaBox = box
    pdf.pages[0].CropBox = pikepdf.Array([float(v) for v in BOX])
    pdf.save(path)
    pdf.close()
    before = open(path, "rb").read()
    stats = hardcrop_file(path)
    assert stats["cropped"] == 0 and stats["removed"] == 0
    assert open(path, "rb").read() == before
    assert "Hello hardcrop" in pymupdf.open(path)[0].get_text()


def test_unsafe_image_is_left_and_logged(tmp_path, capsys):
    src = str(tmp_path / "odd.pdf")
    raw = b"\x00" * (W * H)
    _save_image_pdf(src, raw, bpc=8, cs=pikepdf.Name("/SomeUnknownSpace"),
                    filt=pikepdf.Name("/FooDecode"))
    before = open(src, "rb").read()
    stats = hardcrop_file(src)
    assert stats["cropped"] == 0
    assert stats["skipped"] >= 1
    assert open(src, "rb").read() == before
    err = capsys.readouterr().err
    assert "skipped" in err


def test_image_outside_the_box_is_removed(tmp_path):
    src = str(tmp_path / "out.pdf")
    im = Image.new("L", (40, 40), 0)
    raw = zlib_compress(np.asarray(im).tobytes())
    # drawn entirely above BOX (PDF y 760..800)
    contents = b"q 40 0 0 40 0 760 cm /Im0 Do Q\n"
    _save_image_pdf(src, raw, bpc=8, cs=pikepdf.Name.DeviceGray,
                    filt=pikepdf.Name.FlateDecode, w=40, h=40, contents=contents)
    before = _render(src)
    stats = hardcrop_file(src)
    assert stats["removed"] == 1
    assert _pdf_image(src) is None
    _assert_same(before, _render(src))


def test_shared_image_stays_shared(tmp_path):
    src = str(tmp_path / "shared.pdf")
    im = _pattern().convert("L")
    raw = zlib_compress(np.asarray(im).tobytes())
    _save_image_pdf(src, raw, bpc=8, cs=pikepdf.Name.DeviceGray, filt=pikepdf.Name.FlateDecode)
    pdf = pikepdf.open(src, allow_overwriting_input=True)
    first = pdf.pages[0]
    second = pdf.add_blank_page(page_size=(W, H))
    second.Resources = first.Resources
    second.Contents = first.Contents
    second.MediaBox = first.MediaBox
    second.CropBox = first.CropBox
    pdf.save(src)
    pdf.close()
    hardcrop_file(src)
    doc = pymupdf.open(src)
    xrefs = {im[0] for p in doc for im in p.get_images()}
    assert len(xrefs) == 1
    assert doc[0].get_images()[0][2] == EXP_R - EXP_L


def test_missing_jpegtran_leaves_jpeg(tmp_path, monkeypatch, capsys):
    real = shutil.which
    monkeypatch.setattr("krop.hardcrop.shutil.which", lambda name: None if name == "jpegtran" else real(name))
    im = _pattern().convert("L")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    src = str(tmp_path / "jpg.pdf")
    _save_image_pdf(src, buf.getvalue(), bpc=8, cs=pikepdf.Name.DeviceGray,
                    filt=pikepdf.Name.DCTDecode)
    before = open(src, "rb").read()
    stats = hardcrop_file(src)
    assert stats["cropped"] == 0 and stats["skipped"] == 1
    assert open(src, "rb").read() == before
    assert "jpegtran is not available" in capsys.readouterr().err


@pytest.mark.skipif(shutil.which("jpegtran") is None, reason="jpegtran is not installed")
def test_jpeg_stays_dct(tmp_path):
    im = _pattern().convert("L")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    src = str(tmp_path / "jpg.pdf")
    _save_image_pdf(src, buf.getvalue(), bpc=8, cs=pikepdf.Name.DeviceGray,
                    filt=pikepdf.Name.DCTDecode)
    out = str(tmp_path / "out.pdf")
    shutil.copy(src, out)
    stats = hardcrop_file(out)
    assert stats["cropped"] == 1 and stats["skipped"] == 0
    after = _pdf_image(out)
    assert after["filt"] == "DCTDecode"
    assert after["w"] < W and after["h"] < H
    _assert_same(_render(src), _render(out))


@pytest.mark.skipif(shutil.which("jbig2") is None, reason="jbig2 is not installed")
def test_jbig2_stays_jbig2(tmp_path):
    im = _pattern()
    pbm = str(tmp_path / "c.pbm")
    im.save(pbm)
    proc = subprocess.run(["jbig2", "-p", pbm], check=True, capture_output=True)
    src = str(tmp_path / "jb2.pdf")
    _save_image_pdf(src, proc.stdout, bpc=1, cs=pikepdf.Name.DeviceGray,
                    filt=pikepdf.Name.JBIG2Decode)
    out = str(tmp_path / "out.pdf")
    shutil.copy(src, out)
    stats = hardcrop_file(out)
    assert stats["cropped"] == 1 and stats["skipped"] == 0, stats
    after = _pdf_image(out)
    assert after["filt"] == "JBIG2Decode"
    assert "CCITT" not in (after["filt"] or "")
    assert (after["w"], after["h"]) == (EXP_R - EXP_L, EXP_B - EXP_T)
    _assert_same(_render(src), _render(out))


def test_form_xobject_image_is_cropped(tmp_path):
    src = str(tmp_path / "form.pdf")
    im = _pattern().convert("L")
    raw = zlib_compress(np.asarray(im).tobytes())
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(W, H))
    st = pikepdf.Stream(pdf, raw)
    st.Type = pikepdf.Name.XObject
    st.Subtype = pikepdf.Name.Image
    st.Width, st.Height = W, H
    st.ColorSpace = pikepdf.Name.DeviceGray
    st.BitsPerComponent = 8
    st.Filter = pikepdf.Name.FlateDecode
    form = pikepdf.Stream(pdf, b"q %d 0 0 %d 0 0 cm /Im0 Do Q\n" % (W, H))
    form.Type = pikepdf.Name.XObject
    form.Subtype = pikepdf.Name.Form
    form.BBox = pikepdf.Array([0, 0, W, H])
    form.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=st))
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm0=form))
    page.Contents = pikepdf.Stream(pdf, b"/Fm0 Do\n")
    box = pikepdf.Array([float(v) for v in BOX])
    page.MediaBox = box
    page.CropBox = pikepdf.Array([float(v) for v in BOX])
    pdf.save(src)
    before = _render(src)
    stats = hardcrop_file(src)
    assert stats["cropped"] == 1 and stats["skipped"] == 0
    after = _pdf_image(src)
    assert after["filt"] == "FlateDecode"
    assert (after["w"], after["h"]) == (EXP_R - EXP_L, EXP_B - EXP_T)
    _assert_same(before, _render(src))


def _set_margins(w, left, top, right, bottom):
    w.ui.radioSelAll.setChecked(True)
    img = w.viewer.getImage(0)
    s = w.selections.addSelection()
    s.rect = s.mapRectFromImage(QRectF(
        left * img.width(), top * img.height(),
        img.width() * (1 - left - right), img.height() * (1 - top - bottom)))
    s.adjustBoundingRect()
    w.selections.updateSelectionVisibility()


def test_checkbox_off_does_not_change_image(window, reset_hardcrop_setting, tmp_path):
    src = str(tmp_path / "in.pdf")
    im = _pattern()
    _save_image_pdf(
        src, _ccitt_payload(im), bpc=1, cs=pikepdf.Name.DeviceGray,
        filt=pikepdf.Name.CCITTFaxDecode,
        parms=pikepdf.Dictionary(K=-1, Columns=W, Rows=H),
        box=None,
    )
    w = window
    w.openFile(src)
    _set_margins(w, 0.15, 0.15, 0.15, 0.15)
    off = str(tmp_path / "off.pdf")
    w.ui.editFile.setText(off)
    w.ui.checkGhostscript.setChecked(False)
    w.ui.checkHardCrop.setChecked(False)
    assert not w.ui.checkHardCrop.isChecked()
    w.slotKrop()
    src_im, off_im = _pdf_image(src), _pdf_image(off)
    assert off_im["raw"] == src_im["raw"]
    assert (off_im["w"], off_im["h"], off_im["filt"]) == (src_im["w"], src_im["h"], src_im["filt"])
    assert pymupdf.open(off)[0].mediabox.width < W - 1

    on = str(tmp_path / "on.pdf")
    w.ui.editFile.setText(on)
    w.ui.checkHardCrop.setChecked(True)
    w.slotKrop()
    via = str(tmp_path / "via.pdf")
    shutil.copy(off, via)
    stats = hardcrop_file(via)
    assert stats["cropped"] >= 1
    on_im, via_im = _pdf_image(on), _pdf_image(via)
    assert "CCITT" in on_im["filt"] and "JBIG2" not in on_im["filt"]
    assert on_im["w"] < off_im["w"] and on_im["h"] < off_im["h"]
    assert on_im["filt"] == via_im["filt"]
    assert (on_im["w"], on_im["h"]) == (via_im["w"], via_im["h"])
    _assert_same(_render(off), _render(on))
    _assert_same(_render(on), _render(via))


def test_setting_defaults_off_and_persists(reset_hardcrop_setting, tmp_path, qapp):
    from conftest import new_window
    s = reset_hardcrop_setting
    w = new_window(str(tmp_path / "a.log"))
    assert w.ui.checkHardCrop.text() == "Hard Crop on Save"
    assert not w.ui.checkHardCrop.isChecked()
    # the checkbox sits directly under the save-to group in the Basic tab
    lay = w.ui.verticalLayout_4
    names = [lay.itemAt(i).widget().objectName() for i in range(lay.count()) if lay.itemAt(i).widget()]
    assert names.index("checkHardCrop") == names.index("groupSaveTo") + 1
    w.ui.checkHardCrop.setChecked(True)
    w.writeSettings()
    w.close()
    w2 = new_window(str(tmp_path / "b.log"))
    assert w2.ui.checkHardCrop.isChecked()
    w2.close()
    s.setValue("PDF/HardCrop", "false")


def test_cli_help_and_go(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONPATH=root)
    # keep krop's saved settings out of the real home directory
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    os.makedirs(env["XDG_CONFIG_HOME"], exist_ok=True)
    help_r = subprocess.run([sys.executable, "-m", "krop", "--help"], env=env, capture_output=True, text=True, check=True)
    assert "--hard-crop" in help_r.stdout
    src = str(tmp_path / "scan.pdf")
    # white page, dark block inset, so --trim actually crops
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 612, 792), False)
    pix.clear_with(255)
    pix.set_rect(pymupdf.IRect(120, 140, 480, 640), (0,))
    page.insert_image(page.rect, stream=pix.tobytes("png"))
    doc.save(src)
    doc.close()
    off, on = str(tmp_path / "off.pdf"), str(tmp_path / "on.pdf")
    base = [sys.executable, "-m", "krop", "--go", "--trim", "--trim-use", "all",
            "--optimize", "no", "--log", "off"]
    subprocess.run(base + ["-o", off, src], env=env, check=True, timeout=120)
    subprocess.run(base + ["--hard-crop", "-o", on, src], env=env, check=True, timeout=120)
    off_im, on_im = _pdf_image(off), _pdf_image(on)
    # encoding is unchanged (whatever PyMuPDF wrote); the image is smaller
    assert on_im["filt"] == off_im["filt"]
    assert "JBIG2" not in (on_im["filt"] or "")
    assert on_im["w"] < off_im["w"] and on_im["h"] < off_im["h"]
    # trim leaves a fractional box; 2x rasterization lands on the same pixels
    _assert_same(_render(off, zoom=2), _render(on, zoom=2))
