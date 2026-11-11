# -*- coding: utf-8 -*-
"""Smallest image encoding whose error stays under a threshold.

The error metric is the mean absolute error (MAE) of decoded samples on a
0-255 scale. A single outlier pixel is not treated as a hard failure: JPEG
and palette encodings always have a few pixels far from the original, so the
tail is the 99.5th percentile of absolute error, not the maximum. A candidate
is acceptable when

    MAE <= threshold  and  p99.5 error <= max(16, threshold * 4).

The default threshold is 4, so the percentile limit is 16. The true maximum
is still recorded as error_max, but it does not mark the row over the limit.
Pixels of a
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
    """Return (mae, p99.5, true max) of absolute error on a 0-255 scale."""
    import numpy as np
    diff = np.abs(original.astype(np.int16) - quantized.astype(np.int16))
    if diff.size == 0:
        return 0.0, 0.0, 0
    mae = float(diff.mean())
    # One wild pixel must not fail a palette or JPEG. p99.5 is the tail we gate on.
    p995 = float(np.percentile(diff, 99.5))
    return mae, p995, int(diff.max())


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


def pil_namespace(image, enum_name):
    """Pillow 9.1 put constants on enums. Pillow 8 and 9.0 keep them on Image."""
    return getattr(image, enum_name, image)


def pil_const(image, enum_name, name, default):
    return getattr(pil_namespace(image, enum_name), name, default)


def _quantize_image(im, n, method, dither):
    """Call Image.quantize. Pillow before 9.1 has no dither argument."""
    try:
        return im.quantize(colors=int(n), method=method, dither=dither)
    except TypeError:
        return im.quantize(colors=int(n), method=method)


def _indexed(sample, n, dither=False):
    """Return (reconstructed uint8 array, ok). Median-cut, dither optional."""
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
    # Image.Dither / Image.Quantize exist only from Pillow 9.1. Older releases
    # expose the same integers as Image.FLOYDSTEINBERG, Image.NONE, Image.MEDIANCUT.
    dith = pil_const(Image, "Dither", "FLOYDSTEINBERG" if dither else "NONE", 3 if dither else 0)
    median = pil_const(Image, "Quantize", "MEDIANCUT", 0)
    octree = pil_const(Image, "Quantize", "FASTOCTREE", 2)
    try:
        q = _quantize_image(im, n, median, dith)
    except Exception:
        try:
            q = _quantize_image(im, n, octree, dith)
        except Exception:
            try:
                q = _quantize_image(im, n, None, dith)
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


def _encode_gray(y, bpp, source_filter, full_pixels, sample_pixels, allow_jbig2=True):
    """Return (nbytes, codec) for a gray image of bpp bits."""
    scale = 1.0
    if full_pixels > sample_pixels and sample_pixels:
        scale = float(full_pixels) / float(sample_pixels)
    elif full_pixels > _ENCODE_PIXELS and sample_pixels and sample_pixels < full_pixels:
        scale = float(full_pixels) / float(sample_pixels)
    enc = y
    if bpp == 1:
        bits = _quantize_gray(enc, 1)
        sizes = []
        ccitt = _ccitt_size(bits)
        if ccitt:
            sizes.append((int(ccitt * scale), "CCITT"))
        jbig = _jbig2_size(bits) if allow_jbig2 and scale == 1.0 else None
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



def _jbig2_available():
    return shutil.which("jbig2") is not None


def notation(target_id):
    """Channels-and-bits label for a storage target id."""
    tid = str(target_id or "keep")
    if tid in ("1bpp", "gray-1"):
        return "1ch 1bpp"
    if tid == "jbig2":
        return "1ch 1bpp JBIG2"
    if tid == "gray-2":
        return "1ch 2bpp"
    if tid == "gray-4":
        return "1ch 4bpp"
    if tid == "gray-8":
        return "1ch 8bpp"
    if tid == "rgb-24":
        return "3ch 24bpp Flate"
    if tid.startswith("gray-jpeg-"):
        return "1ch 8bpp JPEG q%s" % tid.rsplit("-", 1)[-1]
    if tid.startswith("jpeg-"):
        return "3ch 24bpp JPEG q%s" % tid.split("-", 1)[1]
    if tid.startswith("indexed-"):
        dither = tid.endswith("-d")
        num = tid[len("indexed-"):]
        if dither:
            num = num[:-2]
        try:
            n = int(num)
        except ValueError:
            n = 0
        bits = 1 if n <= 2 else 2 if n <= 4 else 4 if n <= 16 else 8
        label = structure_text(1, bits, n or None)
        if dither:
            label += " dither"
        return label
    if tid == "keep":
        return "keep"
    return tid


def ladder_ids(kind, bpc, filt):
    """Target ids offered for an image, smallest-looking first, keep last."""
    filt = filt or ""
    if kind in ("bilevel", "undecodable", None):
        ids = ["1bpp", "gray-2", "gray-4", "gray-8"]
        if _jbig2_available():
            ids.append("jbig2")
        ids.append("keep")
        return ids
    ids = []
    already_rgb = kind == "color" and int(bpc or 8) >= 8 and filt in ("Flate", "LZW")
    if not already_rgb:
        ids.append("rgb-24")
    for quality in (95, 85, 75, 60, 45):
        ids.append("jpeg-%d" % quality)
    for colors in (256, 128, 64, 32, 16, 8, 4):
        ids.append("indexed-%d" % colors)
        ids.append("indexed-%d-d" % colors)
    ids.extend(["gray-8", "gray-4", "gray-2", "gray-jpeg-85", "gray-jpeg-60", "1bpp"])
    if _jbig2_available():
        ids.append("jbig2")
    ids.append("keep")
    return ids


def _rgb(arr):
    import numpy as np
    if arr.ndim == 2:
        return np.repeat(arr[:, :, None], 3, axis=2)
    return np.ascontiguousarray(arr[..., :3], dtype=np.uint8)


def _jpeg_roundtrip(arr, quality):
    """Return (decoded uint8 array, jpeg byte size)."""
    import numpy as np
    from PIL import Image
    mode = "L" if getattr(arr, "ndim", 0) == 2 else "RGB"
    data = np.ascontiguousarray(arr if mode == "L" else arr[..., :3], dtype=np.uint8)
    im = Image.fromarray(data, mode)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=int(quality))
    raw = buf.getvalue()
    back = np.asarray(Image.open(io.BytesIO(raw)).convert(mode))
    return back, len(raw)


def reconstruct(arr, target_id):
    """Pixels a target would show, including a real JPEG round-trip."""
    import numpy as np
    tid = str(target_id or "keep")
    if tid in ("keep", "rgb-24"):
        return _rgb(arr)
    if tid in ("1bpp", "jbig2"):
        y = _quantize_gray(_as_gray(arr), 1)
        return np.repeat(y[:, :, None], 3, axis=2)
    if tid in ("gray-2", "gray-4", "gray-8"):
        bpp = int(tid.split("-")[1])
        y = _as_gray(arr)
        if bpp < 8:
            y = _quantize_gray(y, bpp)
        return np.repeat(y[:, :, None], 3, axis=2)
    if tid.startswith("gray-jpeg-"):
        back, _size = _jpeg_roundtrip(_as_gray(arr), int(tid.rsplit("-", 1)[-1]))
        if back.ndim == 3:
            back = back[:, :, 0]
        return np.repeat(back[:, :, None], 3, axis=2)
    if tid.startswith("jpeg-"):
        back, _size = _jpeg_roundtrip(_rgb(arr), int(tid.split("-", 1)[1]))
        return np.ascontiguousarray(back, dtype=np.uint8)
    if tid.startswith("indexed-"):
        dither = tid.endswith("-d")
        num = tid[len("indexed-"):]
        if dither:
            num = num[:-2]
        back, ok = _indexed(_rgb(arr), int(num), dither=dither)
        if ok:
            return np.ascontiguousarray(back, dtype=np.uint8)
    return _rgb(arr)


def _indexed_est(recon, scale):
    import numpy as np
    flat = np.ascontiguousarray(recon[..., :3]).reshape(-1, 3)
    _colors, inverse = np.unique(flat, axis=0, return_inverse=True)
    raw = inverse.astype(np.uint8).tobytes()
    est = _flate_size(raw) + int(_colors.shape[0]) * 3
    return int(est * scale), "Flate"


def _eval_target(viewed, target_id, filt, full_pixels, stored, threshold, max_ok):
    """Error, estimated size, and codec for one ladder step. None if unavailable."""
    import numpy as np
    tid = str(target_id)
    sample_pixels = max(1, int(viewed.shape[0]) * int(viewed.shape[1]))
    scale = float(full_pixels) / float(sample_pixels) if full_pixels > sample_pixels else 1.0
    if tid.startswith("jpeg-") or tid.startswith("gray-jpeg-"):
        quality = int(tid.rsplit("-", 1)[-1])
        src = _as_gray(viewed) if tid.startswith("gray-jpeg-") else _rgb(viewed)
        try:
            back, nbytes = _jpeg_roundtrip(src, quality)
        except Exception:
            return None
        if getattr(back, "ndim", 0) == 2:
            recon = np.repeat(back[:, :, None], 3, axis=2)
        else:
            recon = back
        est, codec = int(nbytes * scale), "DCT"
    elif tid == "jbig2":
        y = _quantize_gray(_as_gray(viewed), 1)
        recon = np.repeat(y[:, :, None], 3, axis=2)
        jbig = _jbig2_size(y) if scale == 1.0 else None
        if not jbig:
            return None
        est, codec = int(jbig), "JBIG2"
    elif tid == "1bpp":
        y = _quantize_gray(_as_gray(viewed), 1)
        recon = np.repeat(y[:, :, None], 3, axis=2)
        est, codec = _encode_gray(y, 1, filt, full_pixels, sample_pixels, allow_jbig2=False)
    elif tid in ("gray-2", "gray-4", "gray-8"):
        bpp = int(tid.split("-")[1])
        y = _as_gray(viewed)
        if bpp < 8:
            y = _quantize_gray(y, bpp)
        recon = np.repeat(np.ascontiguousarray(y)[:, :, None], 3, axis=2)
        est, codec = _encode_gray(y, bpp, filt, full_pixels, sample_pixels, allow_jbig2=False)
    elif tid == "rgb-24":
        recon = _rgb(viewed)
        est = int(_flate_size(np.ascontiguousarray(recon).tobytes()) * scale)
        codec = "Flate"
    elif tid.startswith("indexed-"):
        recon = reconstruct(viewed, tid)
        est, codec = _indexed_est(recon, scale)
    else:
        return None
    mae, p995, mx = _error_against(viewed, recon)
    over = mae > threshold or p995 > max_ok
    return _entry(tid, mae, p995, mx, over, est, codec, stored)


def _entry(target_id, mae, p995, mx, over, est, codec, stored):
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
        "error_p995": None if p995 is None else round(float(p995), 2),
        "error_max": None if mx is None else int(mx),
        "est_bytes": est_i,
        "codec": codec,
        "savings_pct": pct,
    }


def _result(target, mae, p995, mx, est, stored, codec, bpp=None):
    stored = int(stored or 0)
    est = int(est or 0)
    pct = 0.0 if stored <= 0 else (stored - est) * 100.0 / stored
    return {
        "target": target,
        "error_mae": round(float(mae), 2),
        "error_p995": round(float(p995), 2),
        "error_max": int(mx),
        "error_metric": "mae",
        "est_bytes": est,
        "rep_bytes": est,
        "codec": codec,
        "savings_pct": round(pct, 1),
        "suggested_bpp": bpp,
    }


def _keep(stored, filt):
    return _result("keep", 0.0, 0.0, 0, stored, stored, filt or "keep", None)


def _finish(chosen, candidates, stored, filt, bpp):
    out = _result(
        chosen["id"], chosen.get("error_mae") or 0, chosen.get("error_p995") or 0,
        chosen.get("error_max") or 0,
        chosen.get("est_bytes") if chosen.get("est_bytes") is not None else stored,
        stored, chosen.get("codec") or filt or "keep", bpp)
    out["recommended_target"] = chosen["id"]
    out["chosen_target"] = chosen["id"]
    out["target_label"] = chosen.get("label") or notation(chosen["id"])
    out["candidates"] = candidates
    return out


def _bpp_of(target_id):
    tid = str(target_id or "")
    if tid in ("1bpp", "jbig2"):
        return 1
    if tid.startswith("gray-") and tid[5:6].isdigit():
        return int(tid.split("-")[1])
    return None


def _is_heavy(target_id):
    tid = str(target_id or "")
    return tid == "rgb-24" or tid.startswith("jpeg-") or tid.startswith("indexed-") or tid.startswith("gray-jpeg-")


def choose_smallest(candidates, stored):
    """Smallest option under the error limit. Pending options are not eligible."""
    eligible = [
        c for c in candidates or []
        if c.get("id") != "keep" and not c.get("over") and not c.get("pending")
        and c.get("est_bytes") is not None and (not stored or c["est_bytes"] < stored)
    ]
    if not eligible:
        for cand in candidates or []:
            if cand.get("id") == "keep":
                return cand
        return None
    return min(eligible, key=lambda c: (c["est_bytes"], c["id"]))


def recommend(sample, kind, bpc, filt, threshold, width, height, stored_bytes, defer_heavy=False):
    """Pick the smallest stored target whose error stays under the threshold.

    Heavy steps (JPEG, palette) can be left pending so the UI thread's
    analysis pass stays quick. ``fill_pending_file`` measures them later.
    Nothing here is written back into the PDF.
    """
    threshold, max_ok = _limit(threshold if threshold is not None else DEFAULT_ERROR_MAE)
    stored = int(stored_bytes or 0)
    filt = filt or ""
    if sample is None or kind in ("undecodable", "bilevel"):
        keep = _entry("keep", 0, 0, 0, False, stored, filt or "keep", stored)
        candidates = []
        for tid in ladder_ids("bilevel" if kind == "bilevel" else kind, bpc, filt):
            if tid == "keep":
                continue
            known = tid in ("1bpp", "jbig2")
            candidates.append(_entry(
                tid, 0 if known else None, 0 if known else None, 0 if known else None,
                False, stored if tid == "1bpp" and filt in ("CCITT", "JBIG2") else None,
                filt if tid == "1bpp" and filt in ("CCITT", "JBIG2") else None, stored))
        candidates.append(keep)
        if kind == "bilevel" and filt in ("CCITT", "JBIG2"):
            return _finish(keep, candidates, stored, filt, 1)
        return _finish(keep, candidates, stored, filt or "keep", 1 if kind == "bilevel" else None)
    viewed, _step = _sample(sample)
    full_pixels = max(1, int(width or sample.shape[1]) * int(height or sample.shape[0]))
    candidates = []
    for tid in ladder_ids(kind, bpc, filt):
        if tid == "keep":
            continue
        if defer_heavy and _is_heavy(tid):
            pending = _entry(tid, None, None, None, False, None, None, stored)
            pending["pending"] = True
            candidates.append(pending)
            continue
        entry = _eval_target(viewed, tid, filt, full_pixels, stored, threshold, max_ok)
        if entry is not None:
            candidates.append(entry)
    keep = _entry("keep", 0, 0, 0, False, stored, filt or "keep", stored)
    candidates.append(keep)
    chosen = choose_smallest(candidates, stored) or keep
    return _finish(chosen, candidates, stored, filt, _bpp_of(chosen["id"]))


def fill_pending_file(path, xref, candidates, stored, filt, threshold, width, height):
    """Measure ladder steps that were left pending. Safe off the UI thread."""
    import numpy as np
    import pymupdf
    pending = [c for c in candidates or [] if c.get("pending")]
    if not pending:
        return list(candidates or [])
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
    finally:
        doc.close()
    viewed, _step = _sample(arr)
    full_pixels = max(1, int(width or arr.shape[1]) * int(height or arr.shape[0]))
    limit, max_ok = _limit(threshold if threshold is not None else DEFAULT_ERROR_MAE)
    filled = []
    for cand in candidates:
        if not cand.get("pending"):
            filled.append(dict(cand))
            continue
        measured = _eval_target(viewed, cand.get("id"), filt or "", full_pixels, int(stored or 0), limit, max_ok)
        if measured is None:
            skipped = dict(cand)
            skipped["pending"] = False
            filled.append(skipped)
        else:
            measured["pending"] = False
            filled.append(measured)
    return filled


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
    stored = int(stored_bytes or 0)
    if target_id == "keep":
        return _entry("keep", 0, 0, 0, False, stored, filt or "keep", stored)
    viewed, _step = _sample(arr)
    full_pixels = max(1, int(width) * int(height))
    threshold, max_ok = _limit(DEFAULT_ERROR_MAE)
    return _eval_target(viewed, target_id, filt or "", full_pixels, stored, threshold, max_ok)
