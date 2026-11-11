"""Quantized preview pixels, placed with the same mapping as the highlight."""
import io
import time

import numpy as np
import pymupdf
import pytest
from PIL import Image

from krop.imagehighlight import map_image_rects
from krop.imagetarget import _quantize_gray
from krop.preview import orient_for_page, paste_preview, preview_rgba, quantize_pixels


def test_quantize_uses_the_same_levels_as_the_trial_encode():
    arr = np.array([[0, 40, 100, 200, 255]], np.uint8)
    for bpp, target in ((1, "1bpp"), (2, "gray-2"), (4, "gray-4")):
        got = quantize_pixels(arr, target)[0, :, 0]
        expect = _quantize_gray(arr, bpp)[0]
        assert list(got) == list(expect)
    # 1ch 8bpp is the luminance of a color pixel, not a palette.
    color = np.zeros((1, 1, 3), np.uint8)
    color[0, 0] = (255, 0, 0)
    gray = quantize_pixels(color, "gray-8")[0, 0]
    assert gray[0] == gray[1] == gray[2]
    assert 60 < int(gray[0]) < 100


def test_jpeg_preview_is_a_real_round_trip():
    """The preview of a JPEG target is the decoded JPEG, not the untouched pixels."""
    arr = np.zeros((32, 32, 3), np.uint8)
    arr[:, :16] = (255, 0, 0)
    arr[:, 16:] = (0, 0, 255)
    got = quantize_pixels(arr, "jpeg-45")
    assert got.shape == arr.shape
    assert not np.array_equal(got, arr)
    # The same round-trip the trial encode measures.
    from krop.imagetarget import _jpeg_roundtrip
    back, _size = _jpeg_roundtrip(arr, 45)
    assert np.array_equal(got, back)


def test_indexed_preview_has_at_most_n_colors():
    arr = np.zeros((6, 6, 3), np.uint8)
    arr[:3] = (200, 10, 10)
    arr[3:] = (10, 10, 200)
    got = quantize_pixels(arr, "indexed-2")
    colors = {tuple(int(v) for v in pixel) for pixel in got.reshape(-1, 3)}
    assert len(colors) <= 2
    assert quantize_pixels(arr, "keep").shape == arr.shape


def test_orient_follows_the_page_pixmap():
    """Left/right of the image becomes top/bottom after a clockwise page rotation."""
    arr = np.array([[64, 255], [0, 255]], np.uint8)
    turned = orient_for_page(arr, 90)
    assert turned.tolist() == [[0, 64], [255, 255]]
    assert orient_for_page(arr, 180).tolist() == [[255, 0], [255, 64]]
    assert orient_for_page(arr, 270).tolist() == [[255, 255], [64, 0]]
    assert orient_for_page(arr, 0).tolist() == arr.tolist()


def _split_image(rotation):
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=120)
    arr = np.zeros((20, 40), np.uint8)
    arr[:, :20] = 40
    arr[:, 20:] = 200
    pix = pymupdf.Pixmap(pymupdf.csGRAY, 40, 20, arr.tobytes(), False)
    xref = page.insert_image(pymupdf.Rect(30, 20, 110, 60), pixmap=pix)
    if rotation:
        page.set_rotation(rotation)
    return doc, xref, arr


def test_quantized_preview_lands_inside_the_mapped_rect():
    doc, xref, arr = _split_image(0)
    page = doc[0]
    raw = [(r.x0, r.y0, r.x1, r.y1) for r in page.get_image_rects(xref)]
    mapped = map_image_rects(
        raw, page.rotation, page.cropbox.width, page.cropbox.height, 72, (0, 0))
    rgba = np.dstack([quantize_pixels(arr, "1bpp"), np.full(arr.shape, 255, np.uint8)])
    canvas = np.full((int(page.rect.height), int(page.rect.width), 3), 180, np.uint8)
    out = paste_preview(canvas, rgba, mapped[0])
    x, y, w, h, _primary = mapped[0]
    left = out[int(y + h * 0.25), int(x + w * 0.25)]
    right = out[int(y + h * 0.25), int(x + w * 0.75)]
    outside = out[2, 2]
    assert list(left) == [0, 0, 0]
    assert list(right) == [255, 255, 255]
    assert list(outside) == [180, 180, 180]
    doc.close()


def test_rotated_preview_matches_the_mapped_corners():
    doc, xref, arr = _split_image(90)
    page = doc[0]
    raw = [(r.x0, r.y0, r.x1, r.y1) for r in page.get_image_rects(xref)]
    mapped = map_image_rects(
        raw, page.rotation, page.cropbox.width, page.cropbox.height, 72, (0, 0))
    rgb = orient_for_page(quantize_pixels(arr, "1bpp"), 90)
    rgba = np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])
    pm = page.get_pixmap(dpi=72, alpha=False)
    canvas = np.full((pm.height, pm.width, 3), 180, np.uint8)
    out = paste_preview(canvas, rgba, mapped[0])
    x, y, w, h, _primary = mapped[0]
    top = out[int(y + h * 0.2), int(x + w * 0.5)]
    bottom = out[int(y + h * 0.8), int(x + w * 0.5)]
    # Clockwise: the dark left half is the top of the rotated placement.
    assert list(top) == [0, 0, 0]
    assert list(bottom) == [255, 255, 255]
    pm_top = pm.pixel(int(x + w * 0.5), int(y + h * 0.2))[0]
    pm_bottom = pm.pixel(int(x + w * 0.5), int(y + h * 0.8))[0]
    assert pm_top < 80 and pm_bottom > 180
    doc.close()


def test_soft_mask_stays_transparent(tmp_path):
    color = np.full((16, 16, 3), (220, 10, 10), np.uint8)
    mask = np.zeros((16, 16), np.uint8)
    mask[:, 8:] = 255
    ib = io.BytesIO()
    Image.fromarray(color, "RGB").save(ib, format="PNG")
    mb = io.BytesIO()
    Image.fromarray(mask, "L").save(mb, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(pymupdf.Rect(72, 72, 200, 200), stream=ib.getvalue(), mask=mb.getvalue())
    path = str(tmp_path / "masked.pdf")
    doc.save(path)
    xref = page.get_images()[0][0]
    doc.close()
    rgba = preview_rgba(path, xref, "keep")
    assert rgba.shape[-1] == 4
    assert int(rgba[4, 2, 3]) == 0
    assert int(rgba[4, 12, 3]) == 255
    canvas = np.full((16, 16, 3), (0, 0, 255), np.uint8)
    out = paste_preview(canvas, rgba, (0, 0, 16, 16))
    assert list(out[4, 2]) == [0, 0, 255]
    assert list(out[4, 12]) == [220, 10, 10]


def test_preview_toggle_shows_quantized_pixels_and_does_not_write(window, tmp_path, qapp):
    from krop.config import PYQT6
    from krop.qt import Qt
    if PYQT6:
        from PyQt6.QtTest import QTest
    else:
        from PyQt5.QtTest import QTest
    doc = pymupdf.open()
    page = doc.new_page()
    arr = np.full((24, 24), 100, np.uint8)
    pix = pymupdf.Pixmap(pymupdf.csGRAY, 24, 24, arr.tobytes(), False)
    page.insert_image(pymupdf.Rect(72, 72, 200, 200), pixmap=pix)
    src = tmp_path / "flat.pdf"
    doc.save(str(src))
    doc.close()
    original = src.read_bytes()
    w = window
    w.show()
    qapp.processEvents()
    assert w.analysisPanel.previewBox.isChecked()
    assert w.analysisPanel.previewBox.text() == "Preview target"
    w.actionAnalysis.setChecked(True)
    w.openFile(str(src), False)
    deadline = time.time() + 10
    ready = []
    while time.time() < deadline:
        qapp.processEvents()
        ready = w.viewer.previewReady()
        if ready and "Document:" in w.analysisPanel.summary.text():
            break
        time.sleep(0.05)
    assert ready, "preview did not arrive"
    xref, target, rotation = ready[0]
    assert target != "keep"
    image = w.viewer.previewImage(xref, target, rotation)
    expect = quantize_pixels(np.full((24, 24), 100, np.uint8), target)
    assert image.pixelColor(image.width() // 2, image.height() // 2).red() == int(expect[0, 0, 0])
    assert src.read_bytes() == original

    # Hold Alt to compare with the original. Releasing it brings the preview back.
    try:
        key = Qt.Key.Key_Alt
    except AttributeError:
        key = Qt.Key_Alt
    QTest.keyPress(w.analysisPanel.tree, key)
    qapp.processEvents()
    assert w._preview_hold
    assert w.viewer.previewReady() == []
    QTest.keyRelease(w.analysisPanel.tree, key)
    qapp.processEvents()
    assert not w._preview_hold
    assert w.viewer.previewReady()

    w.analysisPanel.previewBox.setChecked(False)
    qapp.processEvents()
    assert w.viewer.previewReady() == []
    w.analysisPanel.previewBox.setChecked(True)
    qapp.processEvents()
    assert w.viewer.previewReady()

    # keep as is shows the original page, with no preview layer.
    child = w.analysisPanel.tree.topLevelItem(0).child(0)
    combo = w.analysisPanel.tree.itemWidget(child, 2)
    combo.setCurrentIndex(combo.findData("keep"))
    qapp.processEvents()
    assert w.analysisPanel.preview_plan(1) == []
    assert w.viewer.previewReady() == []
