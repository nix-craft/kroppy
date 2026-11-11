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


def structure_text(channels, bpc, indexed_colors=None):
    """Compact channels-and-bits label, e.g. ``1ch 1bpp`` or ``idx 8bpp (16 colors)``."""
    bpc = int(bpc or 0)
    if indexed_colors:
        return "idx %dbpp (%d colors)" % (bpc, int(indexed_colors))
    channels = int(channels or 1)
    return "%dch %dbpp" % (channels, channels * max(bpc, 0))


def notation(target_id):
    """Same channels-and-bits notation for a storage target id."""
    if target_id in ("1bpp", "gray-1"):
        return "1ch 1bpp"
    if target_id == "gray-2":
        return "1ch 2bpp"
    if target_id == "gray-4":
        return "1ch 4bpp"
    if target_id == "gray-8":
        return "1ch 8bpp"
    if target_id and str(target_id).startswith("indexed-"):
        try:
            n = int(str(target_id).split("-", 1)[1])
        except ValueError:
            n = 0
        bits = 1 if n <= 2 else 2 if n <= 4 else 4 if n <= 16 else 8
        return structure_text(1, bits, n or None)
    if target_id == "keep":
        return "keep"
    return str(target_id or "keep")


def _candidate_specs(neutral):
    specs = [("1bpp", 1, None), ("gray-2", 2, None), ("gray-4", 4, None), ("gray-8", 8, None)]
    if not neutral:
        for n in (2, 4, 16, 256):
            specs.append(("indexed-%d" % n, None, n))
    return specs


def _measure(viewed, gray, bpp, ncolors):
    if ncolors:
        back, ok = _indexed(viewed, ncolors)
        if not ok:
            return None
        return _error_against(viewed, back)
    if bpp >= 8 and viewed.ndim == 2:
        return 0.0, 0
    q = _quantize_gray(gray, bpp)
    return _error_against(viewed, q)


def _encode_measured(viewed, gray, bpp, ncolors, filt, full_pixels, stored):
    sample_pixels = max(1, viewed.shape[0] * viewed.shape[1])
    if ncolors:
        back, ok = _indexed(viewed, ncolors)
        if not ok:
            return None
        import numpy as np
        flat = back.reshape(-1, 3)
        _colors, inverse = np.unique(flat, axis=0, return_inverse=True)
        raw = inverse.astype(np.uint8).tobytes()
        est = _flate_size(raw) + int(_colors.shape[0]) * 3
        est = int(est * (float(full_pixels) / float(sample_pixels)))
        return est, "Flate"
    est, codec = _encode_gray(gray, bpp or 8, filt, full_pixels, sample_pixels)
    return est, codec


def _entry(target_id, mae, mx, over, est, codec, stored):
    est_i = None if est is None else int(est)
    pct = None
    if est_i is not None and stored:
        pct = round((stored - est_i) * 100.0 / stored, 1)
    elif est_i is not None:
        pct = 0.0
    return {
        "id": target_id,
        "label": notation(target_id),
        "over": bool(over),
        "error_mae": None if mae is None else round(float(mae), 2),
        "error_max": None if mx is None else int(mx),
        "est_bytes": est_i,
        "codec": codec,
        "savings_pct": pct,
    }


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



def _finish(chosen, candidates, stored, filt, bpp):
    out = _result(
        chosen["id"], chosen.get("error_mae") or 0, chosen.get("error_max") or 0,
        chosen.get("est_bytes") if chosen.get("est_bytes") is not None else stored,
        stored, chosen.get("codec") or filt or "keep", bpp)
    out["recommended_target"] = chosen["id"]
    out["chosen_target"] = chosen["id"]
    out["target_label"] = chosen.get("label") or notation(chosen["id"])
    out["candidates"] = candidates
    return out


def recommend(sample, kind, bpc, filt, threshold, width, height, stored_bytes):
    """Pick a storage target for one decoded image.

    sample is a uint8 array HxW or HxWxC, or None when the image was not
    decoded (already 1-bit, or undecodable). Every valid candidate is
    measured. Only the recommended one is encoded.
    """
    threshold, max_ok = _limit(threshold if threshold is not None else DEFAULT_ERROR_MAE)
    stored = int(stored_bytes or 0)
    filt = filt or ""
    if sample is None or kind == "undecodable":
        keep = _entry("keep", 0, 0, False, stored, filt or "keep", stored)
        candidates = [keep]
        bpp = None
        if kind == "bilevel":
            one = _entry("1bpp", 0, 0, False, stored if filt in ("CCITT", "JBIG2") else None,
                         filt if filt in ("CCITT", "JBIG2") else None, stored)
            for name in ("gray-2", "gray-4", "gray-8"):
                pass
            extras = [
                _entry(name, 0, 0, False, None, None, stored)
                for name in ("gray-2", "gray-4", "gray-8")]
            candidates = [one] + extras + [keep]
            bpp = 1
            if filt in ("CCITT", "JBIG2"):
                return _finish(keep, candidates, stored, filt, 1)
        return _finish(keep, candidates, stored, filt or "keep", bpp)
    viewed, _step = _sample(sample)
    gray = _as_gray(viewed)
    neutral = kind == "gray" or _neutral(viewed)
    full_pixels = max(1, int(width or sample.shape[1]) * int(height or sample.shape[0]))
    candidates = []
    chosen = None
    for name, bpp, ncolors in _candidate_specs(neutral):
        measured = _measure(viewed, gray, bpp, ncolors)
        if measured is None:
            continue
        mae, mx = measured
        over = mae > threshold or mx > max_ok
        entry = _entry(name, mae, mx, over, None, None, stored)
        candidates.append(entry)
        if chosen is not None or over:
            continue
        if name == "gray-8" and neutral and filt in ("Flate", "LZW") and (bpc or 8) >= 8:
            continue
        encoded = _encode_measured(viewed, gray, bpp, ncolors, filt, full_pixels, stored)
        if not encoded:
            continue
        est, codec = encoded
        entry["est_bytes"] = int(est)
        entry["codec"] = codec
        entry["savings_pct"] = round((stored - est) * 100.0 / stored, 1) if stored else 0.0
        if stored and est >= stored:
            continue
        chosen = entry
    keep = _entry("keep", 0, 0, False, stored, filt or "keep", stored)
    candidates.append(keep)
    if chosen is None:
        chosen = keep
    bpp_out = None
    if chosen["id"] == "1bpp":
        bpp_out = 1
    elif str(chosen["id"]).startswith("gray-"):
        bpp_out = int(str(chosen["id"]).split("-")[1])
    return _finish(chosen, candidates, stored, filt, bpp_out)


def encode_file_image(path, xref, target_id, stored_bytes, filt):
    """Trial-encode one candidate for xref. Safe to call off the UI thread."""
    import numpy as np
    import pymupdf
    doc = pymupdf.open(path)
    try:
        pix = pymupdf.Pixmap(doc, int(xref))
        if pix.alpha:
            pix = pymupdf.Pixmap(pix, 0)
        if pix.colorspace and pix.colorspace.n == 4:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        n = pix.n
        arr = np.frombuffer(pix.samples, np.uint8)
        arr = arr.reshape(pix.h, pix.stride)[:, :pix.w * n].reshape(pix.h, pix.w, n).copy()
        if n == 1:
            arr = arr[:, :, 0]
        width, height = pix.w, pix.h
    finally:
        doc.close()
    if target_id == "keep":
        stored = int(stored_bytes or 0)
        return _entry("keep", 0, 0, False, stored, filt or "keep", stored)
    viewed, _step = _sample(arr)
    gray = _as_gray(viewed)
    full_pixels = max(1, int(width) * int(height))
    ncolors = None
    bpp = None
    if str(target_id).startswith("indexed-"):
        ncolors = int(str(target_id).split("-", 1)[1])
    elif target_id == "1bpp":
        bpp = 1
    elif str(target_id).startswith("gray-"):
        bpp = int(str(target_id).split("-")[1])
    else:
        return None
    measured = _measure(viewed, gray, bpp, ncolors)
    if measured is None:
        return None
    mae, mx = measured
    encoded = _encode_measured(viewed, gray, bpp, ncolors, filt, full_pixels, stored_bytes)
    if not encoded:
        return None
    est, codec = encoded
    threshold, max_ok = _limit(DEFAULT_ERROR_MAE)
    over = mae > threshold or mx > max_ok
    return _entry(target_id, mae, mx, over, est, codec, int(stored_bytes or 0))
