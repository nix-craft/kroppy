# -*- coding: utf-8 -*-
"""Image encoders ported from https://github.com/nix-craft/desky-scan-tools

The functions below follow pdf-to-gray.py, pdf-to-jbig2.py, pdf-hardcrop.py
(encode_one, pred_encode, ccitt_encode, jpegtran crop), pdf-fill-solid.py
(the ``jbig2 -p`` generic-region path), pdf-analyze.py (decode, gray_stats,
color_stats) and pdf-dedup-images.py. They are not reimplemented. scipy is
optional: despeckle and solid-fill are skipped when it is not installed.
Pillow 8 and 9.0 are enough (no Image.Dither / Image.Quantize enums).
"""
import io
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zlib

import numpy as np

try:
    import pikepdf
    from pikepdf import Array, Dictionary, Name, Stream
except ImportError:  # pragma: no cover
    pikepdf = None
    Name = Array = Dictionary = Stream = None


def _scipy_ndimage():
    try:
        from scipy import ndimage
        return ndimage
    except ImportError:
        return None


# --- pdf-to-gray.py -------------------------------------------------------

def png_predict(g, ftype):
    h, w = g.shape
    a = g.astype(np.int16)
    if ftype == 1:
        d = a.copy()
        d[:, 1:] = a[:, 1:] - a[:, :-1]
    elif ftype == 2:
        d = a.copy()
        d[1:] = a[1:] - a[:-1]
    else:
        d = a
    d = (d & 0xFF).astype(np.uint8)
    return np.hstack([np.full((h, 1), ftype, np.uint8), d]).tobytes()


def best_flate(g):
    h, w = g.shape
    cands = [(zlib.compress(g.tobytes(), 9), None)]
    for f in (1, 2):
        cands.append((zlib.compress(png_predict(g, f), 9),
                      {"Predictor": 15, "Colors": 1, "BitsPerComponent": 8, "Columns": w}))
    return min(cands, key=lambda c: len(c[0]))


def pack4(q):
    h, w = q.shape
    if w % 2:
        q = np.hstack([q, np.zeros((h, 1), np.uint8)])
    return (q[:, 0::2] << 4) | q[:, 1::2]


_STAGES = threading.local()


def stages_begin():
    """Start a per-thread timing list. Nested encodes leave it alone."""
    if getattr(_STAGES, "on", False):
        return
    _STAGES.on = True
    _STAGES.parts = []
    _STAGES.t = time.perf_counter()


def stage(name):
    """Record the time since the previous mark. No-op outside stages_begin."""
    if not getattr(_STAGES, "on", False):
        return
    now = time.perf_counter()
    _STAGES.parts.append((str(name), now - _STAGES.t))
    _STAGES.t = now


def stages_take():
    """Return the recorded (name, seconds) pairs and stop timing."""
    parts = list(getattr(_STAGES, "parts", []) or [])
    _STAGES.on = False
    _STAGES.parts = []
    return parts


def stages_format(parts):
    return " ".join("%s %.2fs" % (name, seconds) for name, seconds in (parts or []))


def flate_best(rows, bpc, w, level=9):
    """Smallest of raw Flate and PNG predictor 15 (Sub or Up), as in pdf-to-gray.py.

    Preview passes a lower zlib level. The uncompressed winner is returned
    so save can compress it at level 9. On a large image the predictor is
    chosen at level 1, then only that candidate is compressed.
    """
    rows = np.ascontiguousarray(rows)
    level = int(level)
    raws = [rows.tobytes()]
    parms = [None]
    for f in (1, 2):
        raws.append(png_predict(rows, f))
        parms.append({"Predictor": 15, "Colors": 1, "BitsPerComponent": int(bpc), "Columns": int(w)})
    if len(raws[0]) < 2_000_000:
        packed = [(zlib.compress(raw, level), parm, raw) for raw, parm in zip(raws, parms)]
        return min(packed, key=lambda c: len(c[0]))
    pick = min(range(len(raws)), key=lambda i: len(zlib.compress(raws[i], 1)))
    return zlib.compress(raws[pick], level), parms[pick], raws[pick]


def _pin_extreme_codes(keep, limit):
    """Ensure codes for (0,0,0) and (255,255,255) are in the palette."""
    keep = [int(v) for v in keep]
    if limit < 2:
        return np.array(keep, np.int64)
    for code in (0, (15 << 8) | (15 << 4) | 15):
        if code in keep:
            continue
        if len(keep) >= limit:
            keep.pop()
        keep.append(code)
    return np.array(keep, np.int64)


def _claim_extremes(idx, pal, rgb):
    """Send near-black and near-white pixels to those palette entries.

    A luma test only. Comparing every pixel with its snapped color made a
    full-resolution indexed encode several times slower.
    """
    pal_i = np.asarray(pal, np.int16)
    if not len(pal_i):
        return idx, pal
    black_i = int(np.argmin(pal_i.sum(axis=1)))
    white_i = int(np.argmin(np.abs(pal_i - 255).sum(axis=1)))
    # 54/183/19 is the 8-bit form of 0.2126/0.7152/0.0722.
    r = rgb[..., 0].astype(np.uint16)
    g = rgb[..., 1].astype(np.uint16)
    b = rgb[..., 2].astype(np.uint16)
    luma = (r * 54 + g * 183 + b * 19) >> 8
    # Chroma guard: pure blue is dark in luma but must not become black.
    chroma = np.maximum(np.maximum(r, g), b) - np.minimum(np.minimum(r, g), b)
    out = np.array(idx, dtype=np.uint8, copy=True)
    # Luma 18 keeps blue-fringed black text and leaves a dark Oxford blue (~24).
    out[(luma <= 18) & (chroma <= 48)] = np.uint8(black_i)
    out[(luma >= 242) & (chroma <= 24)] = np.uint8(white_i)
    return out, pal


def palettize(rgb, min_frac, max_colors=None):
    """rgb uint8 (h,w,3) -> (indices, palette, stats).

    Same snap-to-multiples-of-17 and nearest-color remap as pdf-to-gray.py.
    max_colors, when set, keeps at most that many clusters (indexed-N).
    """
    q = ((rgb.astype(np.uint16) + 8) // 17)
    code = (q[..., 0] << 8) | (q[..., 1] << 4) | q[..., 2]
    cnt = np.bincount(code.ravel(), minlength=4096)
    used = np.flatnonzero(cnt)
    order = used[np.argsort(-cnt[used], kind="stable")]
    total = code.size
    ncolors = len(order)
    if ncolors <= 256:
        keep = order
    else:
        keep = order[cnt[order] >= min_frac * total][:256]
    if 16 < len(keep):
        k16 = order[cnt[order] >= min_frac * total]
        if len(k16) <= 16:
            keep = k16
    if max_colors is not None and len(keep) > int(max_colors):
        keep = order[:int(max_colors)]
    if max_colors is not None:
        # Black and white are on the x17 grid. Keep them even when rare, so
        # thin text is not remapped onto a blue JPEG-noise cluster.
        keep = _pin_extreme_codes(keep, int(max_colors))
    kc = np.stack([(keep >> 8) & 15, (keep >> 4) & 15, keep & 15], 1).astype(np.int32)
    lut = np.zeros(4096, np.uint8)
    lut[keep] = np.arange(len(keep))
    rest = np.setdiff1d(used, keep)
    if len(rest):
        rc = np.stack([(rest >> 8) & 15, (rest >> 4) & 15, rest & 15], 1).astype(np.int32)
        d = ((rc[:, None, :] - kc[None, :, :]) ** 2).sum(-1)
        lut[rest] = np.argmin(d, 1)
    idx = lut[code]
    pal = kc * 17
    err = np.abs(pal[idx].astype(np.int16) - rgb).max() if rgb.size else 0
    changed = int(cnt[rest].sum()) if len(rest) else 0
    return idx, pal.astype(np.uint8), dict(
        colors_snapped=ncolors, palette=len(keep), remapped_px=changed, max_err=int(err))


def quantize_bits(g, bits):
    """Nearest 2**bits gray levels. bits=4 is pdf-to-gray.py: (g+8)//17."""
    g = np.ascontiguousarray(g)
    if int(bits) >= 8:
        return g.astype(np.uint8), np.zeros(g.shape, np.int16)
    levels = (1 << int(bits)) - 1
    step = 255 // levels
    half = step // 2
    q = ((g.astype(np.uint16) + half) // step).astype(np.uint8)
    err = np.abs(q.astype(np.int16) * step - g.astype(np.int16))
    return q, err


def pack_indexed(q, bpc):
    """Pack 1/2/4/8-bit indices. 4-bit is pdf-to-gray.py pack4."""
    bpc = int(bpc)
    if bpc >= 8:
        return np.ascontiguousarray(q, dtype=np.uint8)
    if bpc == 4:
        return pack4(q)
    if bpc == 1:
        return np.packbits(q.astype(np.uint8), axis=1)
    h, w = q.shape
    pad = (-w) % 4
    qq = q
    if pad:
        qq = np.hstack([q, np.zeros((h, pad), np.uint8)])
    return ((qq[:, 0::4] << 6) | (qq[:, 1::4] << 4) | (qq[:, 2::4] << 2) | qq[:, 3::4]).astype(np.uint8)


def _parms_pdf(parms):
    if not parms:
        return None
    return "<< " + " ".join("/%s %s" % (k, int(v)) for k, v in parms.items()) + " >>"


def _gray_recon(q, bits, shape):
    if int(bits) >= 8:
        plane = q.astype(np.uint8)
    else:
        step = 255 // ((1 << int(bits)) - 1)
        plane = (q.astype(np.uint16) * step).astype(np.uint8)
    return np.repeat(plane.reshape(shape)[:, :, None], 3, axis=2)


def _chunk_error(diff_rows):
    """Exact max, mean, and a strided p99.5. ``diff_rows`` yields int16 abs diffs."""
    total = 0
    count = 0
    mx = 0
    samples = []
    for diff in diff_rows:
        if not diff.size:
            continue
        mx = max(mx, int(diff.max()))
        total += int(diff.sum())
        count += int(diff.size)
        # About 200k samples is enough for a stable 99.5th percentile.
        need = 200000
        if count <= need:
            samples.append(np.ascontiguousarray(diff).ravel())
        else:
            step = max(1, int(round((count / float(need)) ** 0.5)))
            samples.append(np.ascontiguousarray(diff[::step, ::step]).ravel())
    if not count:
        return 0, 0.0, 0.0
    stacked = np.concatenate(samples) if samples else np.zeros(1)
    p995 = float(np.percentile(stacked, 99.5)) if stacked.size else 0.0
    return mx, float(total) / float(count), p995


def _luma_and_chroma(rgb):
    """Integer luma and the peak channel gap. Chunked so a 17MP image stays in uint16."""
    rgb = np.ascontiguousarray(rgb[..., :3], dtype=np.uint8)
    g = np.empty(rgb.shape[:2], np.uint8)
    chroma = 0
    for y0 in range(0, rgb.shape[0], 128):
        sl = rgb[y0:y0 + 128]
        r = sl[..., 0].astype(np.uint32)
        gg = sl[..., 1].astype(np.uint32)
        b = sl[..., 2].astype(np.uint32)
        chroma = max(
            chroma,
            int(np.abs(r.astype(np.int16) - gg.astype(np.int16)).max()),
            int(np.abs(gg.astype(np.int16) - b.astype(np.int16)).max()))
        g[y0:y0 + 128] = ((r * 299 + gg * 587 + b * 114 + 500) // 1000).astype(np.uint8)
    return g, chroma


def _gray_against_color(plane, rgb):
    """Error of a gray plane repeated to RGB, against the color original."""
    plane = np.ascontiguousarray(plane, dtype=np.uint8)
    rgb = np.ascontiguousarray(rgb[..., :3], dtype=np.uint8)
    def rows():
        for y0 in range(0, plane.shape[0], 64):
            g = plane[y0:y0 + 64].astype(np.int16)
            src = rgb[y0:y0 + 64].astype(np.int16)
            yield np.maximum(
                np.maximum(np.abs(g - src[..., 0]), np.abs(g - src[..., 1])),
                np.abs(g - src[..., 2]))
    return _chunk_error(rows())


def _palette_against_color(idx, pal, rgb):
    pal_i = np.ascontiguousarray(pal, dtype=np.int16)
    rgb = np.ascontiguousarray(rgb[..., :3], dtype=np.uint8)
    def rows():
        for y0 in range(0, idx.shape[0], 64):
            block = pal_i[idx[y0:y0 + 64]]
            yield np.abs(block - rgb[y0:y0 + 64].astype(np.int16))
    return _chunk_error(rows())


def encode_gray_flate(arr, bits, max_err=None, neutral_tol=0, level=9):
    """4- or 8- or 2-bit DeviceGray Flate, using pdf-to-gray.py's predictor search.

    Returns None when the image is real color (chroma above neutral_tol) or,
    for bits<8, when max error would exceed max_err (their --max-err gate:
    the caller still receives the blob with skipped=True via encode_target).
    """
    rgb = _as_rgb(arr)
    height, width = rgb.shape[:2]
    # Luma, even when the image is real color. pdf-to-gray.py would skip a
    # color image; a chosen gray target still has to produce a preview.
    g, chroma = _luma_and_chroma(rgb)
    q, level_err = quantize_bits(g, bits)
    stage("quantize")
    bpc = 8 if int(bits) >= 8 else int(bits)
    rows = pack_indexed(q, bpc)
    data, parms, raw = flate_best(np.ascontiguousarray(rows), bpc, width, level=level)
    stage("flate")
    keys = _image_keys(width, height, bpc, "/DeviceGray", "/FlateDecode")
    if parms:
        keys["DecodeParms"] = _parms_pdf(parms)
    plane = q if int(bits) >= 8 else (q.astype(np.uint16) * (255 // ((1 << int(bits)) - 1))).astype(np.uint8)
    # Color error is the gray plane against the original RGB, not gray-vs-gray.
    mx, mean, p995 = _gray_against_color(plane, rgb)
    if chroma <= int(neutral_tol):
        mx = int(level_err.max()) if level_err.size else 0
        mean = float(level_err.mean()) if level_err.size else 0.0
        p995 = float(np.percentile(level_err, 99.5)) if level_err.size else 0.0
    stage("compare")
    over = max_err is not None and mx > int(max_err)
    return _blob(
        data, keys, "Flate", None, mx, mean, over, chroma=chroma, decode_parms=parms,
        p995=p995, flate_raw=raw, flate_level=int(level))


def encode_indexed(arr, n, min_frac=1e-4, max_err=None, level=9):
    """Indexed DeviceRGB via pdf-to-gray.py palettize (x17 grid)."""
    rgb = _as_rgb(arr)
    height, width = rgb.shape[:2]
    src = np.ascontiguousarray(rgb[..., :3])
    idx, pal, _st = palettize(src, min_frac, max_colors=int(n))
    idx, pal = _claim_extremes(idx, pal, src)
    stage("quantize")
    bpc = 4 if len(pal) <= 16 else 8
    rows = np.ascontiguousarray(pack_indexed(idx, bpc))
    data, parms, raw = flate_best(rows, bpc, width, level=level)
    stage("flate")
    hexpal = pal.astype(np.uint8).tobytes().hex().upper()
    last = max(0, int(len(pal)) - 1)
    space = "[/Indexed /DeviceRGB %d <%s>]" % (last, hexpal)
    keys = _image_keys(width, height, bpc, space, "/FlateDecode")
    if parms:
        keys["DecodeParms"] = _parms_pdf(parms)
    mx, mean, p995 = _palette_against_color(idx, pal, src)
    stage("compare")
    over = max_err is not None and mx > int(max_err)
    return _blob(
        data, keys, "Flate", None, mx, mean, over,
        decode_parms=parms, palette=pal.astype(np.uint8).tobytes(), palette_last=last,
        p995=p995, flate_raw=raw, flate_level=int(level))


def jpegtran_grayscale(raw):
    """pdf-to-gray.py DCT path: jpegtran -grayscale -copy none."""
    if not raw or not shutil.which("jpegtran"):
        return None
    proc = subprocess.run(
        ["jpegtran", "-grayscale", "-copy", "none"],
        input=bytes(raw), check=False, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def encode_gray_jpeg(arr, raw_jpeg=None, dct_tol=2, max_err=None):
    """Gray JPEG by jpegtran when the source is already DCT. No new JPEG encoder."""
    from PIL import Image
    rgb = _as_rgb(arr)
    b = rgb.astype(np.int16)
    chroma = int(max(np.abs(b[..., 0] - b[..., 1]).max(), np.abs(b[..., 1] - b[..., 2]).max())) if rgb.shape[2] == 3 else 0
    if chroma > int(dct_tol):
        return None
    gj = jpegtran_grayscale(raw_jpeg) if raw_jpeg else None
    if not gj:
        return None
    g = np.asarray(Image.open(io.BytesIO(gj)))
    if g.shape[:2] != rgb.shape[:2]:
        return None
    ref = rgb[..., 1] if rgb.shape[2] == 3 else rgb[..., 0]
    diff = np.abs(g.astype(np.int16) - ref.astype(np.int16))
    mx = int(diff.max()) if diff.size else 0
    height, width = rgb.shape[:2]
    keys = _image_keys(width, height, 8, "/DeviceGray", "/DCTDecode")
    recon = np.repeat(g.astype(np.uint8)[:, :, None], 3, axis=2)
    over = max_err is not None and mx > int(max_err)
    return _blob(gj, keys, "DCT", recon, mx, float(diff.mean()) if diff.size else 0.0, over, chroma=chroma)


# --- pdf-to-jbig2.py and pdf-fill-solid.py `jbig2 -p` ------------------------

def otsu(g):
    h = np.bincount(g.ravel(), minlength=256).astype(float)
    p = h / max(h.sum(), 1.0)
    w = np.cumsum(p)
    m = np.cumsum(p * np.arange(256))
    mt = m[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        s = (mt * w - m) ** 2 / (w * (1 - w))
    # A flat image makes every bin NaN. numpy 2 raises on nanargmax of that.
    if not np.isfinite(s).any():
        return int(np.clip(g.mean() if g.size else 128, 0, 255))
    return int(np.nanargmax(s))


def despeckle(black, n):
    """Remove 8-connected black specks of <= n pixels. No-op without scipy."""
    if n <= 0:
        return black
    ndimage = _scipy_ndimage()
    if ndimage is None:
        return black
    lab, k = ndimage.label(black, structure=np.ones((3, 3), bool))
    if k == 0:
        return black
    sizes = np.bincount(lab.ravel())
    keep = sizes > n
    keep[0] = False
    return keep[lab]


def jbig2_encode_png(png_path):
    """pdf-fill-solid.py / pdf-to-jbig2.py: `jbig2 -p` generic region."""
    tool = shutil.which("jbig2")
    if not tool:
        return None
    proc = subprocess.run([tool, "-p", png_path], check=False, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return None
    return proc.stdout


def jbig2_encode_bw(bw_image):
    """Encode a mode-1 PIL image with `jbig2 -p`. Returns the stream or None."""
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        bw_image.save(path)
        return jbig2_encode_png(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def encode_bilevel(arr, prefer_jbig2=False, despeckle_n=0, fixed_thresh=None, max_err=None):
    """Otsu (pdf-to-jbig2.py) then JBIG2 or CCITT group 4 (pdf-hardcrop.ccitt_encode)."""
    from PIL import Image
    rgb = _as_rgb(arr)
    g, _chroma = _luma_and_chroma(rgb)
    t = fixed_thresh if fixed_thresh is not None else otsu(g)
    ink = despeckle(g <= t, despeckle_n)
    bw = Image.fromarray(((~ink) * 255).astype(np.uint8)).convert("1")
    height, width = g.shape
    white = np.array(bw.convert("L"), dtype=np.uint8)
    if white.shape != (height, width):
        white = white.reshape(height, width)
    stage("quantize")
    mx, mean, p995 = _gray_against_color(white, rgb)
    stage("compare")
    over = max_err is not None and mx > int(max_err)
    recon = np.repeat(white[:, :, None], 3, axis=2)
    if prefer_jbig2:
        data = jbig2_encode_bw(bw)
        stage("jbig2")
        if data:
            keys = _image_keys(width, height, 1, "/DeviceGray", "/JBIG2Decode")
            return _blob(data, keys, "JBIG2", recon, mx, mean, over, thresh=t, p995=p995)
    tmp = tempfile.mkdtemp()
    try:
        # Pillow's Group 4 is min-is-white. PDF's default fax decode is the
        # other way, so the samples passed in are inverted.
        data = ccitt_encode((255 - white).astype(np.uint8), -1, False, tmp)
    except Exception:
        data = None
    finally:
        try:
            os.remove(os.path.join(tmp, "c.tif"))
        except OSError:
            pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass
    if not data:
        raw = np.packbits(np.array(bw) == 0, axis=1).tobytes()
        data = zlib.compress(raw, 9)
        keys = _image_keys(width, height, 1, "/DeviceGray", "/FlateDecode")
        return _blob(data, keys, "Flate", recon, mx, mean, over, thresh=t, p995=p995)
    parms = "<< /K -1 /Columns %d /Rows %d >>" % (width, height)
    keys = _image_keys(width, height, 1, "/DeviceGray", "/CCITTFaxDecode", {"DecodeParms": parms})
    return _blob(data, keys, "CCITT", recon, mx, mean, over, thresh=t, p995=p995)


# --- pdf-hardcrop.py encode path ------------------------------------------

class Unsupported(Exception):
    pass


def pget(p, k, d):
    return d if p is None or k not in p else p[k]


def pred_encode(data, p):
    pr = int(pget(p, "/Predictor", 1))
    if pr == 1:
        return data
    colors = int(pget(p, "/Colors", 1))
    bpc = int(pget(p, "/BitsPerComponent", 8))
    cols = int(pget(p, "/Columns", 1))
    rowlen = (colors * bpc * cols + 7) // 8
    if len(data) % rowlen:
        raise Unsupported("predictor row length")
    a = np.frombuffer(data, np.uint8).reshape(-1, rowlen)
    if pr >= 10:
        up = a.copy()
        up[1:] = a[1:] - a[:-1]
        return np.hstack([np.full((len(a), 1), 2, np.uint8), up]).tobytes()
    if pr == 2 and bpc in (8, 16):
        dt = np.uint8 if bpc == 8 else np.dtype(">u2")
        b = a.view(dt).reshape(len(a), cols, colors)
        d = b.copy()
        d[:, 1:] = b[:, 1:] - b[:, :-1]
        return d.astype(dt).tobytes()
    raise Unsupported("predictor %d with %d bpc" % (pr, bpc))


def lzw_encode(data, early=1):
    out = bytearray()
    acc = 0
    nb = 0

    def put(c, w):
        nonlocal acc, nb
        acc = (acc << w) | c
        nb += w
        while nb >= 8:
            nb -= 8
            out.append((acc >> nb) & 255)
        acc &= (1 << nb) - 1

    tab = {}
    nxt = 258
    w = 9
    put(256, w)
    code = None
    for b in data:
        if code is None:
            code = b
            continue
        k = (code << 8) | b
        c = tab.get(k)
        if c is not None:
            code = c
            continue
        put(code, w)
        tab[k] = nxt
        nxt += 1
        if nxt + early > (1 << w) and w < 12:
            w += 1
        if nxt >= 4094:
            put(256, w)
            tab = {}
            nxt = 258
            w = 9
        code = b
    if code is not None:
        put(code, w)
    put(257, w)
    if nb:
        out.append((acc << (8 - nb)) & 255)
    return bytes(out)


def rl_encode(d):
    out = bytearray()
    i = 0
    n = len(d)
    while i < n:
        j = i + 1
        while j < n and j - i < 128 and d[j] == d[i]:
            j += 1
        if j - i >= 2:
            out += bytes([257 - (j - i), d[i]])
            i = j
            continue
        j = i + 1
        while j < n and j - i < 128 and not (j + 1 < n and d[j] == d[j + 1]):
            j += 1
        out.append(j - i - 1)
        out += d[i:j]
        i = j
    out.append(128)
    return bytes(out)


def encode_one(f, data, p):
    """Re-encode with the same filter. From pdf-hardcrop.py."""
    full = {"/Fl": "/FlateDecode", "/LZW": "/LZWDecode", "/RL": "/RunLengthDecode",
            "/AHx": "/ASCIIHexDecode", "/A85": "/ASCII85Decode"}
    f = full.get(f, f)
    if f == "/FlateDecode":
        return zlib.compress(pred_encode(data, p), 9)
    if f == "/LZWDecode":
        return lzw_encode(pred_encode(data, p), int(pget(p, "/EarlyChange", 1)))
    if f == "/RunLengthDecode":
        return rl_encode(data)
    if f == "/ASCIIHexDecode":
        return data.hex().upper().encode() + b">"
    if f == "/ASCII85Decode":
        import base64
        return base64.a85encode(data, wrapcol=76) + b"~>"
    raise Unsupported("filter " + str(f))


def ccitt_encode(arr, K, align, tmp):
    """Group 4 (K<0) or Group 3 payload. From pdf-hardcrop.py."""
    from PIL import Image
    h, w = arr.shape
    path = os.path.join(tmp, "c.tif")
    info = {278: h}
    opt = (1 if K > 0 else 0) | (4 if align else 0)
    if K >= 0 and opt:
        info[292] = opt
    im = Image.fromarray(arr)
    if im.mode != "1":
        im = im.convert("1")
    im.save(path, "TIFF", compression="group4" if K < 0 else "group3", tiffinfo=info)
    t = Image.open(path)
    offs, cnts = t.tag_v2[273], t.tag_v2[279]
    offs = offs if isinstance(offs, tuple) else (offs,)
    cnts = cnts if isinstance(cnts, tuple) else (cnts,)
    if len(offs) != 1:
        raise Unsupported("libtiff wrote %d strips" % len(offs))
    return open(path, "rb").read()[offs[0]:offs[0] + cnts[0]]


def jpegtran_crop(payload, width, height, top, left):
    """Lossless DCT crop. pdf-hardcrop.py's jpegtran -crop invocation.

    width/height are the crop size, left/top the origin in the source JPEG.
    Returns (bytes, new_width, new_height). jpegtran snaps the origin to the iMCU grid.
    """
    if not shutil.which("jpegtran"):
        raise Unsupported("jpegtran is not installed")
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, "s.jpg")
        dst = os.path.join(tmp, "d.jpg")
        with open(src, "wb") as fh:
            fh.write(payload)
        subprocess.run(
            ["jpegtran", "-copy", "all", "-crop", "%dx%d+%d+%d" % (width, height, left, top),
             "-outfile", dst, src],
            check=True, capture_output=True)
        out = open(dst, "rb").read()
    finally:
        try:
            os.remove(src)
        except OSError:
            pass
        try:
            os.remove(dst)
        except OSError:
            pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass
    from PIL import Image
    im = Image.open(io.BytesIO(out))
    return out, im.size[0], im.size[1]


# --- pdf-analyze.py -------------------------------------------------------

def filters_of(o):
    f = o.get("/Filter")
    if f is None:
        return []
    return [str(x) for x in f] if isinstance(f, pikepdf.Array) else [str(f)]


def decode(o):
    """pdf-analyze.py decode: DCT via Pillow, everything else via pikepdf."""
    from PIL import Image
    f = filters_of(o)
    if f == ["/DCTDecode"]:
        im = Image.open(io.BytesIO(o.read_raw_bytes()))
    else:
        im = pikepdf.PdfImage(o).as_pil_image()
    if im.mode == "P":
        im = im.convert("RGB")
    if im.mode == "1":
        im = im.convert("L")
    if im.mode == "CMYK":
        im = im.convert("RGB")
    if im.mode not in ("L", "RGB"):
        im = im.convert("RGB")
    return np.asarray(im)


def gray_stats(g):
    g = np.ascontiguousarray(g).astype(np.int16)
    cnt = np.bincount(g.ravel(), minlength=256)
    e = np.abs(g - (g + 8) // 17 * 17)
    return dict(
        levels=int((cnt > 0).sum()),
        exact17=round(100 * cnt[::17].sum() / g.size, 2),
        within3=round(100 * float((e <= 3).mean()), 2),
        qerr_max=int(e.max()),
        qerr_mean=round(float(e.mean()), 3))


def color_stats(c):
    c = np.ascontiguousarray(c).reshape(-1, 3).astype(np.int32)
    raw = len(np.unique((c[:, 0] << 16) | (c[:, 1] << 8) | c[:, 2]))
    q = (c + 8) // 17
    k = (q[:, 0] << 8) | (q[:, 1] << 4) | q[:, 2]
    cnt = np.bincount(k, minlength=4096)
    order = np.argsort(-cnt, kind="stable")
    order = order[cnt[order] > 0]
    top = [("#%02x%02x%02x" % tuple(int(v) * 17 for v in ((j >> 8) & 15, (j >> 4) & 15, j & 15)),
            round(float(100 * cnt[j] / len(k)), 2)) for j in order[:6]]
    s = c[:: max(1, len(c) // 200000)]
    res = {}
    for n in (16, 256):
        keep = order[:n]
        pal = np.stack([(keep >> 8) & 15, (keep >> 4) & 15, keep & 15], 1) * 17
        best = np.full(len(s), 999, np.int32)
        for i in range(0, len(pal), 32):
            d = np.abs(s[:, None, :] - pal[None, i:i + 32, :]).max(2).min(1)
            best = np.minimum(best, d)
        res["pal%d" % n] = dict(
            maxerr=int(best.max()) if len(best) else 0,
            meanerr=round(float(best.mean()), 2) if len(best) else 0.0,
            cover=round(100 * cnt[keep].sum() / max(len(k), 1), 2))
    return dict(raw_colors=raw, x17_colors=int(len(order)), top=top, **res)


# --- pdf-dedup-images.py --------------------------------------------------

def image_key(o):
    import hashlib
    d = {k: repr(v) for k, v in o.items() if k != "/Length"}
    return hashlib.sha256(repr(sorted(d.items())).encode() + o.read_raw_bytes()).hexdigest()


def dedup(pdf):
    """Merge byte-identical image XObjects. Returns (unique, merged)."""
    canon = {}
    merged = 0
    for page in pdf.pages:
        res = page.obj.get("/Resources")
        xd = res.get("/XObject") if res is not None else None
        if xd is None:
            continue
        for name in list(xd.keys()):
            o = xd[name]
            if str(o.get("/Subtype")) != "/Image":
                continue
            k = image_key(o)
            if k in canon:
                if canon[k].objgen != o.objgen:
                    xd[name] = canon[k]
                    merged += 1
            else:
                canon[k] = o
    pdf.remove_unreferenced_resources()
    return len(canon), merged


def dedup_file(src, dst):
    """Save-time option: write dst with identical images merged. Does not overwrite src."""
    if os.path.abspath(src) == os.path.abspath(dst):
        raise Unsupported("refusing to overwrite the source")
    pdf = pikepdf.open(src)
    n, merged = dedup(pdf)
    pdf.save(dst, deterministic_id=True)
    return n, merged


# --- helpers used by kroppy ------------------------------------------------

def _as_rgb(arr):
    a = np.ascontiguousarray(arr)
    if a.ndim == 2:
        return np.repeat(a[:, :, None], 3, axis=2)
    return a[..., :3]


def _image_keys(width, height, bpc, colorspace, filt, extra=None):
    keys = {
        "Type": "/XObject",
        "Subtype": "/Image",
        "Width": str(int(width)),
        "Height": str(int(height)),
        "BitsPerComponent": str(int(bpc)),
        "ColorSpace": colorspace,
        "Filter": filt,
    }
    if extra:
        keys.update(extra)
    return keys


def _blob(data, keys, codec, recon, max_err, mean_err, over, **extra):
    out = {
        "data": bytes(data), "keys": keys, "codec": codec, "recon": recon,
        "max_err": int(max_err), "mean_err": float(mean_err), "over": bool(over),
        "gate": "max",
    }
    out.update(extra)
    return out


def uses_desky(target_id):
    tid = str(target_id or "")
    return (tid in ("gray-2", "gray-4", "gray-8", "1bpp", "jbig2")
            or tid.startswith("indexed-") or tid.startswith("gray-jpeg-"))


def encode_target(arr, target_id, max_err=None, raw_jpeg=None, neutral_tol=0, flate_level=9):
    """One candidate, using the desky-scan-tools encoders. None if not applicable."""
    tid = str(target_id or "")
    if tid in ("gray-2", "gray-4", "gray-8"):
        return encode_gray_flate(
            arr, int(tid.split("-")[1]), max_err=max_err, neutral_tol=neutral_tol,
            level=flate_level)
    if tid.startswith("gray-jpeg-"):
        return encode_gray_jpeg(arr, raw_jpeg=raw_jpeg, dct_tol=2, max_err=max_err)
    if tid.startswith("indexed-"):
        body = tid[len("indexed-"):]
        if body.endswith("-d"):
            body = body[:-2]
        if body.endswith("-s"):
            body = body[:-2]
        try:
            n = int(body)
        except ValueError:
            return None
        return encode_indexed(arr, n, max_err=max_err, level=flate_level)
    if tid == "jbig2":
        return encode_bilevel(arr, prefer_jbig2=True, max_err=max_err)
    if tid == "1bpp":
        return encode_bilevel(arr, prefer_jbig2=False, max_err=max_err)
    return None


def source_stream(path, xref):
    """Raw encoded bytes of an image XObject, for jpegtran and for save-time matching."""
    import hashlib
    pdf = pikepdf.open(path)
    try:
        try:
            obj = pdf.get_object(int(xref))
        except Exception:
            obj = pdf.get_object((int(xref), 0))
        raw = obj.read_raw_bytes()
        filt = filters_of(obj)
        return {"raw": raw, "sha": hashlib.sha256(raw).hexdigest(), "filters": filt}
    finally:
        pdf.close()
