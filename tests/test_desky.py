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
