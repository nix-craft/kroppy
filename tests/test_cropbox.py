"""CropBox must stay inside MediaBox, including files that arrive illegal."""
import os
import pymupdf
import pytest

from krop.pdfcropper import PdfFile, PdfCropper
from krop.hardcrop import hardcrop_file


def _pdf(path, mediabox, cropbox=None, rotation=0):
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    if mediabox is not None:
        doc.xref_set_key(page.xref, "MediaBox", mediabox)
    if cropbox is not None:
        doc.xref_set_key(page.xref, "CropBox", cropbox)
    if rotation:
        page.set_rotation(rotation)
    doc.save(path)
    doc.close()


def _crop(src, crop, out):
    pdf = PdfFile()
    pdf.loadFromFile(src)
    cropper = PdfCropper()
    cropper.addPageCropped(pdf, 0, [crop], True, 0)
    cropper.writeToFile(out)


def _boxes(path):
    doc = pymupdf.open(path)
    page = doc[0]
    mb = tuple(round(float(v), 2) for v in page.mediabox)
    kind, cb = doc.xref_get_key(page.xref, "CropBox")
    return mb, cb


def test_cropbox_outside_mediabox_saves(tmp_path):
    src = str(tmp_path / "out.pdf")
    _pdf(src, "[0 0 612 792]", "[-20 -30 700 900]")
    out = str(tmp_path / "cropped.pdf")
    _crop(src, (0.1, 0.1, 0.1, 0.1), out)
    mb, cb = _boxes(out)
    # the visible page is the MediaBox, so a 10% margin matches a normal page
    assert mb == (61.2, 79.2, 550.8, 712.8)
    assert "61.2" in cb and "712.8" in cb


def test_cropbox_slightly_outside_full_page(tmp_path):
    src = str(tmp_path / "slight.pdf")
    _pdf(src, "[0 0 612 792]", "[-0.01 0 612.02 792]")
    out = str(tmp_path / "out.pdf")
    _crop(src, (0, 0, 0, 0), out)
    mb, cb = _boxes(out)
    assert mb == (0.0, 0.0, 612.0, 792.0)
    assert "612" in cb and "792" in cb


def test_selection_past_the_edge_is_clamped(tmp_path):
    src = str(tmp_path / "plain.pdf")
    _pdf(src, "[0 0 612 792]")
    out = str(tmp_path / "out.pdf")
    _crop(src, (-0.001, 0, 0, 0), out)
    mb, _cb = _boxes(out)
    assert mb[0] >= 0 and mb[2] <= 612 and mb[1] >= 0 and mb[3] <= 792
    assert mb[2] > mb[0] and mb[3] > mb[1]


def test_offset_cropbox_unchanged(tmp_path):
    src = str(tmp_path / "off.pdf")
    _pdf(src, "[0 0 612 792]", "[40 80 500 700]")
    out = str(tmp_path / "out.pdf")
    _crop(src, (0.1, 0.1, 0.1, 0.1), out)
    mb, cb = _boxes(out)
    assert mb == (86.0, 142.0, 454.0, 638.0)
    assert "86" in cb and "638" in cb


def test_rotated_page_with_bad_cropbox(tmp_path):
    src = str(tmp_path / "rot.pdf")
    _pdf(src, "[0 0 612 792]", "[-20 -30 700 900]", rotation=90)
    out = str(tmp_path / "out.pdf")
    _crop(src, (0.1, 0.05, 0.1, 0.05), out)
    mb, _cb = _boxes(out)
    assert mb[0] >= -0.01 and mb[2] <= 612.01
    assert mb[1] >= -0.01 and mb[3] <= 792.01
    assert mb[2] - mb[0] < 612


def test_previously_cropped_output(tmp_path):
    src = str(tmp_path / "krop.pdf")
    doc = pymupdf.open()
    page = doc.new_page()
    doc.xref_set_key(page.xref, "MediaBox", "[80 120 520 680]")
    doc.xref_set_key(page.xref, "CropBox", "[80 120 520 680]")
    doc.xref_set_key(page.xref, "KropFullBox", "[0 0 612 792]")
    doc.save(src)
    doc.close()
    out = str(tmp_path / "out.pdf")
    _crop(src, (0.1, 0.1, 0.1, 0.1), out)
    mb, _cb = _boxes(out)
    assert mb == (124.0, 176.0, 476.0, 624.0)


def test_gui_save_of_bad_cropbox(window, tmp_path):
    src = str(tmp_path / "bad.pdf")
    _pdf(src, "[0 0 612 792]", "[-20 -30 700 900]")
    w = window
    w.openFile(src, False)
    w.slotNewSelection()
    out = str(tmp_path / "saved.pdf")
    w.ui.editFile.setText(out)
    w.ui.checkGhostscript.setChecked(False)
    w.ui.checkHardCrop.setChecked(False)
    w.slotKrop()
    assert os.path.exists(out)
    mb, _cb = _boxes(out)
    assert mb[2] - mb[0] <= 612
    assert mb[0] >= -0.01 and mb[2] <= 612.01
    log = open(w.logPath).read()
    assert "PAGE" in log
    assert "MediaBox" in log
    assert "ERROR" not in log


def test_hardcrop_accepts_cropbox_outside_mediabox(tmp_path):
    import pikepdf, zlib
    from PIL import Image
    import numpy as np
    src = str(tmp_path / "img.pdf")
    im = Image.new("L", (200, 200), 255)
    raw = zlib.compress(np.asarray(im).tobytes())
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(200, 200))
    st = pikepdf.Stream(pdf, raw)
    st.Type = pikepdf.Name.XObject
    st.Subtype = pikepdf.Name.Image
    st.Width = st.Height = 200
    st.ColorSpace = pikepdf.Name.DeviceGray
    st.BitsPerComponent = 8
    st.Filter = pikepdf.Name.FlateDecode
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=st))
    # image covers more than the MediaBox; CropBox sticks out past it
    page.Contents = pikepdf.Stream(pdf, b"q 240 0 0 240 -20 -20 cm /Im0 Do Q\n")
    page.MediaBox = pikepdf.Array([0, 0, 200, 200])
    page.CropBox = pikepdf.Array([-30, -40, 260, 280])
    pdf.save(src, compress_streams=False,
             stream_decode_level=pikepdf.StreamDecodeLevel.none)
    stats = hardcrop_file(src)
    assert stats["skipped"] == 0
    assert stats["cropped"] == 1
    doc = pymupdf.open(src)
    im0 = doc[0].get_images(full=True)[0]
    assert im0[2] < 200 and im0[3] < 200
    assert im0[8] == "FlateDecode"


def test_hardcrop_disjoint_boxes_left_unchanged(tmp_path):
    import pikepdf
    src = str(tmp_path / "apart.pdf")
    pdf = pikepdf.Pdf.new()
    page = pdf.add_blank_page(page_size=(100, 100))
    page.MediaBox = pikepdf.Array([0, 0, 100, 100])
    page.CropBox = pikepdf.Array([200, 200, 300, 300])
    pdf.save(src)
    before = open(src, "rb").read()
    stats = hardcrop_file(src)
    assert stats["cropped"] == 0
    assert open(src, "rb").read() == before
