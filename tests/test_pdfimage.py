"""Palette dither, extremes, and a working-copy PDF preview."""
import os

import numpy as np
import pymupdf

from krop.imagetarget import _mae_max, encode_file_image, reconstruct
from krop.pdfimage import MIN_PYMUPDF, WorkingCopy, cached_encoded, pymupdf_version, stream_length


def _poster(height=240, width=320):
    """Blue field, a lighter strip, a dark box, white, and thin black text."""
    arr = np.zeros((height, width, 3), np.uint8)
    arr[:] = (30, 90, 200)
    arr[30:70] = (150, 190, 245)
    arr[90:170, 30:140] = (8, 24, 70)
    arr[180:210] = (255, 255, 255)
    for y in (100, 112, 124, 136):
        arr[y:y + 3, 40:280] = (0, 0, 0)
    arr[100:150, 160:163] = (0, 0, 0)
    return arr


def test_desky_palette_is_exact_on_the_17_grid():
    """pdf-to-gray.py palettize: colors already on the x17 grid round-trip."""
    from krop.desky import palettize
    rng = np.random.default_rng(1)
    pal = rng.integers(0, 16, (12, 3)) * 17
    idx = rng.integers(0, 12, (50, 71))
    rgb = pal[idx].astype(np.uint8)
    out_idx, out_pal, st = palettize(rgb, 1e-4)
    assert st["remapped_px"] == 0
    assert (out_pal[out_idx] == rgb).all()


def test_palette_keeps_black_text():
    arr = _poster()
    got = reconstruct(arr, "indexed-16")
    text = got[100:103, 40:280]
    luma = text.astype(np.float32) @ np.array([0.2126, 0.7152, 0.0722])
    assert float(luma.mean()) < 40, float(luma.mean())
    colors = np.unique(got.reshape(-1, 3), axis=0).astype(np.float32)
    darkest = (colors @ np.array([0.2126, 0.7152, 0.0722])).min()
    assert darkest < 25, darkest
    mae, _p, _m = _mae_max(arr, got)
    assert mae < 12, mae


def _place(path, arr, twice=False):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=500)
    pix = pymupdf.Pixmap(pymupdf.csRGB, arr.shape[1], arr.shape[0], np.ascontiguousarray(arr).tobytes(), False)
    rect = pymupdf.Rect(40, 50, 300, 280)
    xref = page.insert_image(rect, pixmap=pix)
    if twice:
        doc.new_page(width=400, height=500).insert_image(pymupdf.Rect(30, 40, 250, 240), xref=xref)
    doc.save(path)
    doc.close()
    return xref, rect


def _page_rgb(doc, index, dpi=72):
    pix = doc[index].get_pixmap(alpha=False, dpi=dpi)
    raw = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.stride)[:, :pix.w * 3]
    return raw.reshape(pix.h, pix.w, 3).copy()


def _rect_pixels(page_arr, page, rect, dpi=72):
    scale = dpi / 72.0
    x0 = max(0, int(rect.x0 * scale))
    y0 = max(0, int(rect.y0 * scale))
    x1 = min(page_arr.shape[1], int(rect.x1 * scale))
    y1 = min(page_arr.shape[0], int(rect.y0 * scale) + max(1, int(rect.height * scale)))
    # y1 from y0 of rect
    y1 = min(page_arr.shape[0], int(rect.y1 * scale))
    return page_arr[y0:y1, x0:x1], (x0, y0, x1, y1)


def _mask(page_arr, doc, page_index, xref, inset=6):
    """Return (inside, outside). The antialiased rim is in neither."""
    rect = doc[page_index].get_image_rects(xref)[0]
    _crop, box = _rect_pixels(page_arr, doc[page_index], rect)
    x0, y0, x1, y1 = box
    inside = np.zeros(page_arr.shape[:2], bool)
    inside[y0 + inset:y1 - inset, x0 + inset:x1 - inset] = True
    outside = np.ones(page_arr.shape[:2], bool)
    outside[max(0, y0 - 2):y1 + 2, max(0, x0 - 2):x1 + 2] = False
    return inside, outside, rect


def test_working_copy_replaces_only_the_image_and_keeps_the_original(tmp_path):
    assert pymupdf_version() >= MIN_PYMUPDF
    grad = np.zeros((120, 180, 3), np.uint8)
    ramp = np.linspace(0, 255, 180, dtype=np.float32).astype(np.uint8)
    grad[:, :, 0] = ramp
    grad[:, :, 1] = 40
    grad[:, :, 2] = 180
    path = str(tmp_path / "grad.pdf")
    xref, _rect = _place(path, grad, twice=True)
    before = open(path, "rb").read()
    original = pymupdf.open(path)
    base = [_page_rgb(original, 0), _page_rgb(original, 1)]
    original_pix = pymupdf.Pixmap(original, xref)
    original_sample = bytes(original_pix.samples[:12])

    entry = encode_file_image(path, xref, "indexed-4", 100000, "DCT")
    encoded = cached_encoded(path, xref, "indexed-4")
    assert encoded is not None
    assert entry["est_bytes"] == len(encoded["data"])
    assert entry["est_bytes"] > 0

    copy = WorkingCopy(path)
    copy.rebuild({xref: "indexed-4"}, {(xref, "indexed-4"): encoded})
    assert stream_length(copy.doc, xref) == len(encoded["data"])
    shown = [_page_rgb(copy.doc, 0), _page_rgb(copy.doc, 1)]
    for page_i in (0, 1):
        inside, outside, _rect = _mask(shown[page_i], copy.doc, page_i, xref)
        assert np.array_equal(shown[page_i][outside], base[page_i][outside])
        assert not np.array_equal(shown[page_i][inside], base[page_i][inside])
    assert open(path, "rb").read() == before
    again = pymupdf.Pixmap(original, xref)
    assert bytes(again.samples[:12]) == original_sample
    original.close()
    copy.close()


def test_indexed_text_stays_black_when_mupdf_renders_it(tmp_path):
    arr = _poster(180, 240)
    path = str(tmp_path / "poster.pdf")
    xref, _rect = _place(path, arr, twice=False)
    encoded = cached_encoded(path, xref, "indexed-16")
    if encoded is None:
        encode_file_image(path, xref, "indexed-16", 100000, "DCT")
        encoded = cached_encoded(path, xref, "indexed-16")
    copy = WorkingCopy(path)
    copy.rebuild({xref: "indexed-16"}, {(xref, "indexed-16"): encoded})
    shown = _page_rgb(copy.doc, 0)
    inside, _outside, _rect = _mask(shown, copy.doc, 0, xref)
    crop = shown[inside].reshape(-1, 3)
    # The source has several flat black rows. The darkest rendered pixels stay black.
    luma = crop.astype(np.float32) @ np.array([0.2126, 0.7152, 0.0722])
    assert float(np.percentile(luma, 5)) < 35, float(np.percentile(luma, 5))
    copy.close()


def test_preview_off_uses_the_original_page(window, tmp_path, qapp):
    grad = np.zeros((48, 80, 3), np.uint8)
    grad[:, :, 0] = np.linspace(0, 255, 80, dtype=np.float32).astype(np.uint8)
    path = str(tmp_path / "grad.pdf")
    xref, rect = _place(path, grad, twice=False)
    before = open(path, "rb").read()
    window.analyzeOnOpen = False
    window.openFile(path, False)
    qapp.processEvents()
    viewer = window.viewer
    original = viewer.getImage(0).copy()
    entry = encode_file_image(path, xref, "indexed-4", 50000, "")
    encoded = cached_encoded(path, xref, "indexed-4")
    assert entry["est_bytes"] == len(encoded["data"])
    window.actionAnalysis.setChecked(True)
    qapp.processEvents()
    viewer.setPreviewPlan(path, [{"xref": xref, "target": "indexed-4"}], 0)
    assert viewer.install_preview(path, xref, "indexed-4", encoded, None, 0)
    qapp.processEvents()
    replaced = viewer.getImage(0)
    assert viewer.preview_cache_key()[2] == ((xref, "indexed-4"),)
    # A 4-color gradient is not the original smooth ramp.
    scale = 96 / 72.0
    px = int((rect.x0 + rect.width / 2) * scale)
    py = int((rect.y0 + rect.height / 2) * scale)
    changed = False
    for dx in range(0, 40, 4):
        a = replaced.pixelColor(px - 20 + dx, py).red()
        b = original.pixelColor(px - 20 + dx, py).red()
        if a != b:
            changed = True
            break
    assert changed
    viewer.clearPreviews()
    qapp.processEvents()
    restored = viewer.getImage(0)
    assert viewer.preview_cache_key() is None
    assert restored.pixelColor(px, py).red() == original.pixelColor(px, py).red()
    assert open(path, "rb").read() == before
