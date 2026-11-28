"""Encoders ported from https://github.com/nix-craft/desky-scan-tools."""
import io
import zlib

import numpy as np
import pikepdf
import pymupdf
import pytest
from PIL import Image

from krop.desky import (
    color_stats,
    decode,
    dedup,
    despeckle,
    encode_gray_flate,
    encode_indexed,
    encode_one,
    gray_stats,
    jbig2_encode_bw,
    jpegtran_crop,
    otsu,
    palettize,
    pred_encode,
    ccitt_encode,
)
from krop.pdfimage import apply_cached_choices, arm_save

N = pikepdf.Name


def test_display_keep_is_exact_and_rgb24_is_close(tmp_path):
    """preview_display is compared to the original render of the same clip."""
    from krop.imagetarget import _mae_max, encode_file_image
    from krop.pdfimage import cached_encoded, paired_display
    arr = np.zeros((48, 64, 3), np.uint8)
    arr[:] = (30, 90, 200)
    arr[16:20, 8:56] = (0, 0, 0)
    arr[:, :, 0] = np.linspace(20, 220, 64, dtype=np.float32).astype(np.uint8)
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=240)
    pix = pymupdf.Pixmap(pymupdf.csRGB, 64, 48, np.ascontiguousarray(arr).tobytes(), False)
    xref = page.insert_image(pymupdf.Rect(20, 30, 160, 150), pixmap=pix)
    path = str(tmp_path / "clip.pdf")
    doc.save(path)
    doc.close()
    shown, original = paired_display(path, xref, None, dpi=72)
    mae, _p, _m = _mae_max(original, shown)
    assert mae == 0
    encode_file_image(path, xref, "rgb-24", 10000, "")
    enc = cached_encoded(path, xref, "rgb-24")
    assert enc
    shown, original = paired_display(path, xref, enc, dpi=72)
    assert shown.shape == original.shape
    mae, _p, _m = _mae_max(original, shown)
    assert mae < 1, mae


def test_gray4_display_error_is_against_the_color_original(tmp_path):
    """A blue page converted to gray-4 cannot report a perfect match."""
    from krop.control import display_metrics
    arr = np.zeros((48, 64, 3), np.uint8)
    arr[:] = (20, 40, 180)
    arr[16:22, 8:56] = (0, 0, 0)
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=240)
    pix = pymupdf.Pixmap(pymupdf.csRGB, 64, 48, np.ascontiguousarray(arr).tobytes(), False)
    xref = page.insert_image(pymupdf.Rect(20, 30, 160, 150), pixmap=pix)
    path = str(tmp_path / "blue.pdf")
    doc.save(path)
    doc.close()
    metrics = display_metrics(path, xref, "gray-4", dpi=72)
    assert not metrics.get("refused")
    assert metrics["error_mae"] > 10
    assert metrics["error_max"] > 0
    shown = metrics["shown"]
    # The replacement is gray: the channels match. The reference is the color page.
    assert int(np.abs(shown[:, :, 0].astype(int) - shown[:, :, 2].astype(int)).max()) < 2


def test_refused_target_is_not_a_zero_error_preview(tmp_path):
    from krop.control import display_metrics
    from krop.imagetarget import encode_file_image
    arr = np.zeros((32, 40, 3), np.uint8)
    arr[:] = (20, 40, 180)
    doc = pymupdf.open()
    page = doc.new_page(width=120, height=120)
    pix = pymupdf.Pixmap(pymupdf.csRGB, 40, 32, np.ascontiguousarray(arr).tobytes(), False)
    xref = page.insert_image(pymupdf.Rect(10, 10, 100, 90), pixmap=pix)
    path = str(tmp_path / "color.pdf")
    doc.save(path)
    doc.close()
    entry = encode_file_image(path, xref, "gray-jpeg-85", 1000, "Flate")
    assert entry["refused"] is True
    assert str(entry["note"]).startswith("refused")
    metrics = display_metrics(path, xref, "gray-jpeg-85", dpi=72)
    assert metrics["refused"] is True
    assert "error_mae" not in metrics


def test_indexed_keeps_black_text_on_blue():
    """Black type on a blue page must survive indexed-16 and indexed-8."""
    from krop.imagetarget import reconstruct
    arr = np.zeros((180, 240, 3), np.uint8)
    arr[:] = (30, 90, 200)
    arr[30:60] = (150, 190, 245)
    arr[80:140, 16:110] = (8, 24, 70)
    arr[88:92, 20:210] = (0, 0, 0)
    arr[108:112, 20:210] = (12, 16, 36)
    for tid in ("indexed-16", "indexed-8"):
        got = reconstruct(arr, tid)
        bar = got[88:92, 20:210].astype(np.float32)
        luma = bar @ np.array([0.2126, 0.7152, 0.0722])
        assert float(luma.mean()) < 25, (tid, float(luma.mean()))
        colors = np.unique(got.reshape(-1, 3), axis=0).astype(np.float32)
        darkest = float((colors @ np.array([0.2126, 0.7152, 0.0722])).min())
        assert darkest < 20, (tid, darkest)


def test_palette_matches_desky_cases():
    idx, pal, st = palettize(np.dstack([np.full((100, 100), 255, np.uint8)] * 3), 1e-4)
    assert st["palette"] == 1 and (pal[idx] == 255).all()
    big = np.zeros((100, 100, 3), np.uint8)
    big[0, 0] = (17, 0, 0)
    assert palettize(big, 0.5)[2]["remapped_px"] == 0
    many = (np.arange(300 * 3).reshape(300, 1, 3) % 16 * 17).astype(np.uint8)
    many = np.repeat(np.concatenate([many, np.zeros((5000, 1, 3), np.uint8)]), 2, 1)
    _i, _p, s2 = palettize(many, 0.01)
    assert s2["palette"] <= 256 and s2["remapped_px"] > 0


def test_gray4_max_err_gate_and_exact_levels():
    g = ((np.arange(61 * 81).reshape(61, 81) % 16) * 17).astype(np.uint8)
    noisy = np.clip(g.astype(int) + np.random.default_rng(0).integers(-8, 9, g.shape), 0, 255).astype(np.uint8)
    blob = encode_gray_flate(np.dstack([noisy] * 3), 4, max_err=8, neutral_tol=0)
    assert blob and not blob["over"]
    assert blob["max_err"] <= 8
    assert blob["keys"]["BitsPerComponent"] == "4"
    # Off-grid values have max error 8, so a limit of 3 leaves the image over the gate.
    over = encode_gray_flate(np.dstack([g] * 3) if False else np.dstack([
        (np.arange(40 * 100).reshape(40, 100) % 256).astype(np.uint8)
    ] * 3), 4, max_err=3)
    assert over["over"] and over["max_err"] > 3


def test_indexed_round_trip_bytes_are_what_mupdf_decodes(tmp_path):
    rng = np.random.default_rng(1)
    pal = rng.integers(0, 16, (12, 3)) * 17
    rgb = pal[rng.integers(0, 12, (40, 50))].astype(np.uint8)
    blob = encode_indexed(rgb, 16, max_err=8)
    assert not blob["over"]
    assert blob["max_err"] == 0
    from krop.pdfimage import decode_stream
    got = decode_stream(blob)
    assert got.shape == rgb.shape
    assert (got == rgb).all()


def test_gray_stats_and_color_stats_and_decode():
    g = ((np.arange(20 * 30).reshape(20, 30) % 16) * 17).astype(np.uint8)
    st = gray_stats(g)
    assert st["qerr_max"] == 0 and st["exact17"] == 100
    rgb = np.dstack([g, g, (g + 17) % 256])
    cs = color_stats(rgb)
    assert cs["x17_colors"] >= 2 and "pal16" in cs
    pdf = pikepdf.new()
    xo = pikepdf.Stream(pdf, zlib.compress(g.tobytes()))
    xo.Filter = N.FlateDecode
    xo.Type = N.XObject
    xo.Subtype = N.Image
    xo.Width = 30
    xo.Height = 20
    xo.ColorSpace = N.DeviceGray
    xo.BitsPerComponent = 8
    assert (decode(xo) == g).all()


def test_otsu_and_despeckle_optional():
    g = np.array([0] * 50 + [255] * 50, np.uint8)
    assert 0 <= otsu(g) <= 255
    specks = np.zeros((20, 20), bool)
    specks[1, 1] = True
    out = despeckle(specks, 3)
    try:
        import scipy  # noqa: F401
    except ImportError:
        assert (out == specks).all()
    else:
        assert not out[1, 1]


def test_jbig2_p_and_ccitt_and_predictor_and_jpegtran(tmp_path):
    im = Image.fromarray(np.zeros((32, 32), np.uint8)).convert("1")
    jb = jbig2_encode_bw(im)
    if jb is not None:
        assert jb[:1] != b""
    arr = np.zeros((16, 16), bool)
    arr[:8] = True
    data = ccitt_encode(arr, -1, False, str(tmp_path))
    assert data
    raw = bytes(range(32))
    enc = pred_encode(raw, {"/Predictor": 1})
    assert enc == raw
    assert encode_one("/FlateDecode", raw, {"/Predictor": 1})
    # jpegtran lossless crop of a real JPEG
    buf = io.BytesIO()
    Image.fromarray(np.full((32, 32, 3), 40, np.uint8), "RGB").save(buf, format="JPEG", quality=80)
    try:
        out, w, h = jpegtran_crop(buf.getvalue(), 16, 16, 0, 0)
    except Exception:
        pytest.skip("jpegtran crop unavailable")
    assert out[:2] == b"\xff\xd8" and w > 0 and h > 0


def test_every_ladder_target_finishes_without_leaving_the_overlay(window, tmp_path, qapp):
    """set_target used to hand a dict to deliverPreview and spin until timeout."""
    from krop.analyzepanel import _run_queued
    from krop.imagetarget import ladder_ids
    doc = pymupdf.open()
    page = doc.new_page()
    arr = np.zeros((36, 48, 3), np.uint8)
    arr[:] = (30, 90, 200)
    arr[12:16, 6:40] = (0, 0, 0)
    pix = pymupdf.Pixmap(pymupdf.csRGB, 48, 36, np.ascontiguousarray(arr).tobytes(), False)
    xref = page.insert_image(pymupdf.Rect(40, 50, 200, 170), pixmap=pix)
    path = str(tmp_path / "ladder.pdf")
    doc.save(path)
    doc.close()
    window.analyzeOnOpen = False
    window.openFile(path, False)
    qapp.processEvents()
    panel = window.analysisPanel
    viewer = window.viewer
    for tid in ladder_ids("color", 8, "DCT"):
        if tid == "keep":
            continue
        token = 3
        viewer.setPreviewPlan(path, [{"xref": xref, "target": tid}], 0)
        viewer.showBusy(xref, token, [(10, 12, 40, 24, True)])
        payload = {
            "kind": "preview", "path": path, "xref": xref, "target": tid,
            "stored": 80000, "filt": "DCT", "rotation": 0, "token": token,
            "gen": panel._ladder_gen,
        }
        try:
            result = _run_queued(payload)
        except Exception as exc:
            result = None
            error = exc
        else:
            error = None
        panel._on_p0({"payload": payload, "result": result, "error": error, "elapsed": 0.01})
        qapp.processEvents()
        assert not viewer.busyVisible(), tid
    # The crash: rgba is a dict. The overlay must clear, not spin.
    viewer.showBusy(xref, 9, [(10, 12, 40, 24, True)])
    assert viewer.deliverPreview({
        "xref": xref, "target": "gray-4", "rotation": 0, "token": 9, "rgba": {"entry": {}},
    }) is False
    panel._finish_preview(
        {"xref": xref, "target": "gray-4", "token": 9, "path": path},
        {"entry": {"id": "gray-4", "note": "left unchanged"}, "encoded": None},
        None, 0.1)
    qapp.processEvents()
    assert not viewer.busyVisible()


def test_dedup_merges_identical_images(tmp_path):
    pdf = pikepdf.new()
    for k in (0, 1, 0, 1, 0):
        data = bytes([k * 200]) * (40 * 30)
        xo = pikepdf.Stream(pdf, zlib.compress(data))
        xo.Filter = N.FlateDecode
        xo.Type = N.XObject
        xo.Subtype = N.Image
        xo.Width = 40
        xo.Height = 30
        xo.ColorSpace = N.DeviceGray
        xo.BitsPerComponent = 8
        pg = pikepdf.Dictionary(
            Type=N.Page, MediaBox=[0, 0, 40, 30],
            Resources=pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=xo)),
            Contents=pikepdf.Stream(pdf, b"q 40 0 0 30 0 0 cm /Im0 Do Q"))
        pdf.pages.append(pikepdf.Page(pg))
    src, out = str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")
    pdf.save(src)
    a = pymupdf.open(src)
    assert len({x[0] for p in a for x in p.get_images()}) == 5
    n, merged = dedup(pikepdf.open(src))
    assert n == 2 and merged == 3
    # Save-time reuse: the chosen bytes replace the matching source stream.
    src_pdf = pikepdf.open(src)
    raw = src_pdf.pages[0].Resources.XObject.Im0.read_raw_bytes()
    import hashlib
    sha = hashlib.sha256(raw).hexdigest()
    blob = encode_gray_flate(np.full((30, 40), 200, np.uint8), 8)
    blob["source_sha"] = sha
    arm_save(sha, blob)
    apply_cached_choices(src)
    after = pikepdf.open(src)
    assert after.pages[0].Resources.XObject.Im0.ColorSpace == N.DeviceGray
    assert len(after.pages[0].Resources.XObject.Im0.read_raw_bytes()) == len(blob["data"])
    a.close()
