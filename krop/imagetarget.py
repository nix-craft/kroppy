# -*- coding: utf-8 -*-
"""Smallest image encoding whose error stays under a threshold.

The error metric is the mean absolute error (MAE) of decoded samples on a
0-255 scale. The maximum absolute error is measured on the same pixels and
reported beside the mean. A candidate is acceptable when

    MAE <= threshold  and  max error <= max(16, threshold * 4).

The default threshold is 4, so the maximum allowed error is 16. Pixels of a
large image are strided down to about 200 000 samples for this search; the
stride is regular, so thin features still contribute to the mean. Candidates
are tried from smallest to largest and the search stops at the first one
that passes. Only that winner is entropy-coded:

  * 1 bpp: CCITT Group 4, or JBIG2 when the jbig2 tool is available and smaller
  * gray and indexed: Flate (zlib)
  * JPEG only when the image is already DCT and the JPEG is smaller than Flate

An image that is already 1-bit CCITT or JBIG2 is kept and not encoded again.
"""
import io
import os
import shutil
import subprocess
import tempfile
import zlib

# Mean absolute error on a 0-255 scale. See the module docstring.
DEFAULT_ERROR_MAE = 4.0
_SAMPLE_PIXELS = 200000
_ENCODE_PIXELS = 1500000


def _limit(threshold):
    threshold = float(threshold)
    return threshold, max(16.0, threshold * 4.0)


def _sample(arr, limit=_SAMPLE_PIXELS):
    n = arr.shape[0] * arr.shape[1]
    if n <= limit:
        return arr, 1
    step = int(round((n / float(limit)) ** 0.5)) or 1
    return arr[::step, ::step], step


def _mae_max(original, quantized):
    import numpy as np
    diff = np.abs(original.astype(np.int16) - quantized.astype(np.int16))
    if diff.size == 0:
        return 0.0, 0
    return float(diff.mean()), int(diff.max())


def _quantize_gray(y, bpp):
    import numpy as np
    y = y.astype(np.int16)
    if bpp == 1:
        return np.where(y >= 128, 255, 0).astype(np.uint8)
    if bpp >= 8:
        return y.astype(np.uint8)
    levels = (1 << bpp) - 1
    q = np.rint(y.astype(np.float32) * levels / 255.0)
    return np.rint(q * 255.0 / levels).astype(np.uint8)


def _as_gray(sample):
    import numpy as np
    if sample.ndim == 2:
        return sample
    rgb = sample[..., :3].astype(np.float32)
    y = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
    return np.clip(y, 0, 255).astype(np.uint8)


def _neutral(sample):
    import numpy as np
    if sample.ndim == 2 or sample.shape[-1] < 3:
        return True
    rgb = sample[..., :3].astype(np.int16)
    chroma = (rgb.max(axis=2) - rgb.min(axis=2)) / 255.0
    return float(np.percentile(chroma, 99)) <= 0.03


def _indexed(sample, n):
    """Return (reconstructed uint8 array, ok). Uses Pillow without dithering."""
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return None, False
    if sample.ndim == 2:
        rgb = np.repeat(sample[:, :, None], 3, axis=2)
    else:
        rgb = sample[..., :3]
    im = Image.fromarray(rgb.astype("uint8"), "RGB")
    try:
        q = im.quantize(colors=int(n), method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE)
    except Exception:
        try:
            q = im.quantize(colors=int(n), dither=Image.Dither.NONE)
        except Exception:
            return None, False
    back = np.asarray(q.convert("RGB"))
    return back, True


def _pack_gray(y, bpp):
    import numpy as np
    if bpp >= 8:
        return y.astype(np.uint8).tobytes()
    levels = (1 << bpp) - 1
    idx = np.rint(y.astype(np.float32) * levels / 255.0).astype(np.uint8)
    if bpp == 1:
        return np.packbits(idx, axis=1).tobytes()
    if bpp == 2:
        h, w = idx.shape
        pad = (-w) % 4
        if pad:
            idx = np.pad(idx, ((0, 0), (0, pad)))
        idx = idx.reshape(h, -1, 4)
        byte = (idx[:, :, 0] << 6) | (idx[:, :, 1] << 4) | (idx[:, :, 2] << 2) | idx[:, :, 3]
        return byte.astype(np.uint8).tobytes()
    if bpp == 4:
        h, w = idx.shape
        if w % 2:
            idx = np.pad(idx, ((0, 0), (0, 1)))
        idx = idx.reshape(h, -1, 2)
        byte = (idx[:, :, 0] << 4) | idx[:, :, 1]
        return byte.astype(np.uint8).tobytes()
    return idx.tobytes()


def _flate_size(raw):
    if not raw:
        return 0
    return len(zlib.compress(raw, 9))


def _ccitt_size(bits):
    """Byte size of a Group 4 payload. bits is uint8 0/255."""
    try:
        from PIL import Image
    except Exception:
        return None
    im = Image.fromarray(bits, "L").convert("1")
    buf = io.BytesIO()
    im.save(buf, format="TIFF", compression="group4")
    try:
        t = Image.open(io.BytesIO(buf.getvalue()))
        cnts = t.tag_v2[279]
        if isinstance(cnts, tuple):
            return int(sum(cnts))
        return int(cnts)
    except Exception:
        return len(buf.getvalue())


def _jbig2_size(bits):
    tool = shutil.which("jbig2")
    if not tool:
        return None
    if bits.shape[0] * bits.shape[1] > _ENCODE_PIXELS:
        return None
    try:
        import numpy as np
        packed = np.packbits(bits >= 128, axis=1).tobytes()
        fd, path = tempfile.mkstemp(suffix=".pbm")
        os.close(fd)
        try:
            with open(path, "wb") as fh:
                fh.write(b"P4\n%d %d\n" % (bits.shape[1], bits.shape[0]))
                fh.write(packed)
            proc = subprocess.run([tool, "-p", path], check=False, capture_output=True, timeout=3)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if proc.returncode != 0 or not proc.stdout:
            return None
        return len(proc.stdout)
    except Exception:
        return None


def _jpeg_size(arr):
    try:
        from PIL import Image
    except Exception:
        return None
    mode = "L" if arr.ndim == 2 else "RGB"
    data = arr if arr.ndim == 2 else arr[..., :3]
    im = Image.fromarray(data, mode)
    buf = io.BytesIO()
    try:
        im.save(buf, format="JPEG", quality=80, optimize=True)
    except Exception:
        return None
    return len(buf.getvalue())


def _error_against(sample, recon):
    """MAE of a gray or RGB reconstruction against the original samples."""
    import numpy as np
    if sample.ndim == 2:
        got = recon if recon.ndim == 2 else recon[..., 0]
        return _mae_max(sample, got)
    src = sample[..., :3]
    if recon.ndim == 2:
        got = np.repeat(recon[:, :, None], 3, axis=2)
    else:
        got = recon[..., :3]
    return _mae_max(src, got)


def _encode_gray(y, bpp, source_filter, full_pixels, sample_pixels):
    """Return (nbytes, codec) for a gray image of bpp bits."""
    scale = 1.0
    if full_pixels > _ENCODE_PIXELS and sample_pixels and sample_pixels < full_pixels:
        scale = float(full_pixels) / float(sample_pixels)
    enc = y
    if bpp == 1:
        bits = _quantize_gray(enc, 1)
        sizes = []
        ccitt = _ccitt_size(bits)
        if ccitt:
            sizes.append((int(ccitt * scale), "CCITT"))
        jbig = _jbig2_size(bits) if scale == 1.0 else None
        if jbig:
            sizes.append((jbig, "JBIG2"))
        if sizes:
            return min(sizes)
        return int(_flate_size(_pack_gray(bits, 1)) * scale), "Flate"
    raw = _pack_gray(_quantize_gray(enc, bpp), bpp)
    flate = int(_flate_size(raw) * scale)
    if source_filter == "DCT" and bpp >= 8:
        jpg = _jpeg_size(_quantize_gray(enc, 8))
        if jpg and int(jpg * scale) < flate:
            return int(jpg * scale), "DCT"
    return flate, "Flate"


def _result(target, mae, mx, est, stored, codec, bpp=None):
    stored = int(stored or 0)
    est = int(est or 0)
    pct = 0.0 if stored <= 0 else (stored - est) * 100.0 / stored
    return {
        "target": target,
        "error_mae": round(float(mae), 2),
        "error_max": int(mx),
        "error_metric": "mae",
        "est_bytes": est,
        "rep_bytes": est,
        "codec": codec,
        "savings_pct": round(pct, 1),
        "suggested_bpp": bpp,
    }


def _keep(stored, filt):
    return _result("keep", 0.0, 0, stored, stored, filt or "keep", None)


def recommend(sample, kind, bpc, filt, threshold, width, height, stored_bytes):
    """Pick a storage target for one decoded image.

    sample is a uint8 array HxW or HxWxC, or None when the image was not
    decoded (already 1-bit, or undecodable). kind is bilevel, gray, color,
    or undecodable. filt is a short name such as CCITT, JBIG2, DCT, Flate.
    """
    threshold, max_ok = _limit(threshold if threshold is not None else DEFAULT_ERROR_MAE)
    stored = int(stored_bytes or 0)
    filt = filt or ""
    if sample is None or kind in ("bilevel", "undecodable"):
        out = _keep(stored, filt or ("CCITT" if kind == "bilevel" else "keep"))
        if kind == "bilevel":
            out["suggested_bpp"] = 1
            out["target"] = "1bpp" if filt not in ("CCITT", "JBIG2") else "keep"
            if out["target"] == "1bpp":
                # Still 1-bit, but not in a bilevel codec. Encode once.
                if sample is not None:
                    y, _step = _sample(_as_gray(sample))
                    est, codec = _encode_gray(y, 1, filt, width * height, y.size)
                    if est < stored:
                        out.update(est_bytes=est, rep_bytes=est, codec=codec,
                                   savings_pct=round((stored - est) * 100.0 / stored, 1) if stored else 0.0)
                    else:
                        out["target"] = "keep"
        return out
    viewed, step = _sample(sample)
    gray = _as_gray(viewed)
    neutral = kind == "gray" or _neutral(viewed)
    full_pixels = max(1, int(width or sample.shape[1]) * int(height or sample.shape[0]))
    # Smallest first. Stop at the first candidate under the threshold.
    candidates = [("1bpp", 1), ("gray-2", 2), ("gray-4", 4), ("gray-8", 8)]
    for name, bpp in candidates:
        if bpp >= 8 and neutral and kind == "gray":
            q = gray
            mae, mx = 0.0, 0
        else:
            q = _quantize_gray(gray, bpp)
            mae, mx = _error_against(viewed, q)
        if mae <= threshold and mx <= max_ok:
            if name == "gray-8" and neutral and filt in ("Flate", "LZW", "DCT") and (bpc or 8) >= 8:
                # Already stored at 8 bpp gray. Keep unless a DCT/Flate trial is smaller,
                # which we only bother with for DCT sources.
                if filt != "DCT":
                    return _keep(stored, filt)
            est, codec = _encode_gray(q, bpp, filt, full_pixels, gray.shape[0] * gray.shape[1])
            if est >= stored and stored:
                kept = _keep(stored, filt)
                kept["error_mae"] = round(float(mae), 2)
                kept["error_max"] = int(mx)
                return kept
            return _result(name, mae, mx, est, stored, codec, bpp)
    if not neutral:
        import numpy as np
        for n in (2, 4, 16, 256):
            back, ok = _indexed(viewed, n)
            if not ok:
                break
            mae, mx = _error_against(viewed, back)
            if mae <= threshold and mx <= max_ok:
                flat = back.reshape(-1, 3)
                _colors, inverse = np.unique(flat, axis=0, return_inverse=True)
                raw = inverse.astype(np.uint8).tobytes()
                est = _flate_size(raw) + int(_colors.shape[0]) * 3
                scale = float(full_pixels) / float(max(1, viewed.shape[0] * viewed.shape[1]))
                est = int(est * scale)
                if est >= stored and stored:
                    kept = _keep(stored, filt)
                    kept["error_mae"] = round(float(mae), 2)
                    kept["error_max"] = int(mx)
                    return kept
                return _result("indexed-%d" % n, mae, mx, est, stored, "Flate", None)
    return _keep(stored, filt)
