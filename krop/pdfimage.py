# -*- coding: utf-8 -*-
"""Encode a storage target as a PDF image and install it in a working copy.

The file the user opened is never written. A second Document, opened from
the same bytes, receives the replacement streams. PyMuPDF then renders that
document, so masks, decode, ICC, and placement stay MuPDF's problem.

Requires PyMuPDF 1.24 or newer (Document.update_stream, Document.xref_set_key).
That is the generation that warns that ``import fitz`` is deprecated. The
same calls exist on the 1.23 series; 1.24 is the floor exercised with the
viewer on Ubuntu 22.04's pip-installed PyMuPDF.
"""
import os
import zlib

MIN_PYMUPDF = (1, 24)

# (path, xref, target) -> {"data", "keys", "codec", "entry"}
_CACHE = {}


def pymupdf_version():
    """PyMuPDF release as a tuple, e.g. (1, 28, 6). Not the fitz binding number."""
    import pymupdf
    ver = getattr(pymupdf, "version", None)
    text = ""
    if isinstance(ver, (tuple, list)) and len(ver) >= 2:
        text = str(ver[1])
    if not text:
        text = str(getattr(pymupdf, "pymupdf_version", "") or "")
    parts = []
    for piece in text.split("."):
        num = ""
        for ch in piece:
            if ch.isdigit():
                num += ch
            else:
                break
        if not num:
            break
        parts.append(int(num))
    return tuple(parts)


def remember(path, xref, target, encoded, entry, stages=None):
    stored = dict(encoded)
    stored.pop("recon", None)
    _CACHE[(os.path.abspath(path), int(xref), str(target))] = {
        "encoded": stored, "entry": entry, "stages": list(stages or []),
    }


def cached_entry(path, xref, target):
    item = _CACHE.get((os.path.abspath(path), int(xref), str(target)))
    return None if item is None else item.get("entry")


def cached_encoded(path, xref, target):
    item = _CACHE.get((os.path.abspath(path), int(xref), str(target)))
    return None if item is None else item.get("encoded")


def cached_stages(path, xref, target):
    item = _CACHE.get((os.path.abspath(path), int(xref), str(target)))
    return [] if item is None else list(item.get("stages") or [])


_SAVE = {}


def arm_save(sha, encoded):
    """Remember one chosen encoding so save can write those exact bytes."""
    if sha and encoded and encoded.get("data"):
        _SAVE[str(sha)] = encoded


def apply_cached_choices(output_path, dedup=False):
    """Replace images in a saved PDF with the chosen desky bytes, then optionally dedup.

    Matching is by the source stream's sha256, so the bytes are reused unchanged.
    No-op when nothing was chosen and dedup is off.
    """
    if not _SAVE and not dedup:
        return 0
    import pikepdf
    from krop.desky import dedup as dedup_images
    pdf = pikepdf.open(output_path, allow_overwriting_input=True)
    n = 0
    if _SAVE:
        import hashlib
        name = pikepdf.Name
        for obj in pdf.objects:
            if not isinstance(obj, pikepdf.Stream):
                continue
            if str(obj.get("/Subtype")) != "/Image":
                continue
            sha = hashlib.sha256(obj.read_raw_bytes()).hexdigest()
            enc = _SAVE.get(sha)
            if not enc:
                continue
            _install_pikepdf(obj, _flate_for_save(enc), name)
            n += 1
    if dedup:
        dedup_images(pdf)
    if n or dedup:
        pdf.save(output_path)
    pdf.close()
    return n


def _flate_for_save(enc):
    """Preview streams use zlib level 6. Save writes the same samples at level 9."""
    raw = enc.get("flate_raw")
    level = enc.get("flate_level")
    if not raw or str(enc.get("keys", {}).get("Filter") or "") != "/FlateDecode":
        return enc
    if level is None or int(level) >= 9:
        return enc
    out = dict(enc)
    out["data"] = zlib.compress(raw, 9)
    out["flate_level"] = 9
    return out


def _install_pikepdf(obj, enc, name):
    filt = str(enc.get("keys", {}).get("Filter") or "/FlateDecode")
    if "/DecodeParms" in obj:
        del obj["/DecodeParms"]
    parms = enc.get("decode_parms")
    obj.write(bytes(enc["data"]), filter=name(filt), decode_parms=pikepdf.Dictionary(parms) if parms else None)
    obj.Width = int(enc["keys"]["Width"])
    obj.Height = int(enc["keys"]["Height"])
    obj.BitsPerComponent = int(enc["keys"]["BitsPerComponent"])
    if enc.get("palette") is not None:
        obj.ColorSpace = pikepdf.Array([
            name.Indexed, name.DeviceRGB, int(enc.get("palette_last") or 0),
            pikepdf.String(enc["palette"])])
    else:
        cs = str(enc["keys"].get("ColorSpace") or "/DeviceGray")
        if cs.startswith("/"):
            obj.ColorSpace = name(cs)
    if enc["keys"].get("DecodeParms") and not parms:
        pass


def encode_stream(arr, target_id, max_err=None, raw_jpeg=None, neutral_tol=0, flate_level=6):
    """Full-resolution PDF image stream for target_id.

    Gray, indexed, gray-JPEG and 1-bit/JBIG2 candidates are the encoders from
    desky-scan-tools (krop.desky). ``data`` is the exact content stream.
    """
    import numpy as np
    from krop.desky import encode_target, uses_desky
    from krop.imagetarget import _jpeg_roundtrip, _rgb
    tid = str(target_id or "keep")
    if uses_desky(tid):
        return encode_target(
            arr, tid, max_err=max_err, raw_jpeg=raw_jpeg, neutral_tol=neutral_tol,
            flate_level=flate_level)
    rgb = _rgb(arr)
    height, width = int(rgb.shape[0]), int(rgb.shape[1])
    if tid.startswith("jpeg-") or tid.startswith("gray-jpeg-"):
        quality = int(tid.rsplit("-", 1)[-1])
        src = _as_gray(arr) if tid.startswith("gray-jpeg-") else rgb
        back, _nbytes = _jpeg_roundtrip(src, quality)
        data = _jpeg_bytes(src, quality)
        cs = "/DeviceGray" if tid.startswith("gray-jpeg-") else "/DeviceRGB"
        recon = back if getattr(back, "ndim", 0) == 3 else np.repeat(back[:, :, None], 3, axis=2)
        return _blob(data, _image_keys(width, height, 8, cs, "/DCTDecode"), "DCT", recon)
    if tid.startswith("indexed-"):
        n, _dither, _smooth = _indexed_flags(tid)
        recon = reconstruct(arr, tid)
        return _indexed_stream(recon, n, width, height)
    if tid in ("1bpp", "jbig2"):
        y = _quantize_gray(_as_gray(arr), 1)
        recon = np.repeat(y[:, :, None], 3, axis=2)
        return _bilevel_stream(y, width, height, prefer_jbig2=(tid == "jbig2"))
    if tid in ("gray-2", "gray-4", "gray-8"):
        bpp = int(tid.split("-")[1])
        y = _as_gray(arr)
        if bpp < 8:
            y = _quantize_gray(y, bpp)
        recon = np.repeat(np.ascontiguousarray(y)[:, :, None], 3, axis=2)
        raw = _pack_gray_bits(y, bpp, width)
        return _blob(zlib.compress(raw, 9), _image_keys(width, height, bpp, "/DeviceGray", "/FlateDecode"), "Flate", recon)
    recon = rgb
    raw = np.ascontiguousarray(recon, dtype=np.uint8).tobytes()
    return _blob(zlib.compress(raw, 9), _image_keys(width, height, 8, "/DeviceRGB", "/FlateDecode"), "Flate", recon)


def _blob(data, keys, codec, recon):
    return {"data": bytes(data), "keys": keys, "codec": codec, "recon": recon}


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


def _jpeg_bytes(arr, quality):
    import io
    import numpy as np
    from PIL import Image
    mode = "L" if getattr(arr, "ndim", 0) == 2 else "RGB"
    data = np.ascontiguousarray(arr if mode == "L" else arr[..., :3], dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(data, mode).save(buf, format="JPEG", quality=int(quality))
    return buf.getvalue()


def _indexed_stream(recon, n, width, height):
    import numpy as np
    flat = np.ascontiguousarray(recon[..., :3]).reshape(-1, 3)
    colors, inverse = np.unique(flat, axis=0, return_inverse=True)
    if colors.shape[0] > 256:
        colors = colors[:256]
        inverse = np.clip(inverse, 0, 255)
    bpc = 1 if colors.shape[0] <= 2 else 2 if colors.shape[0] <= 4 else 4 if colors.shape[0] <= 16 else 8
    idx = inverse.reshape(height, width).astype(np.uint8)
    raw = _pack_index_bits(idx, bpc)
    hexpal = colors.astype(np.uint8).tobytes().hex().upper()
    last = max(0, int(colors.shape[0]) - 1)
    space = "[/Indexed /DeviceRGB %d <%s>]" % (last, hexpal)
    keys = _image_keys(width, height, bpc, space, "/FlateDecode")
    return _blob(zlib.compress(raw, 9), keys, "Flate", recon)


def _pack_index_bits(idx, bpc):
    import numpy as np
    if bpc >= 8:
        return np.ascontiguousarray(idx, dtype=np.uint8).tobytes()
    vals = idx.astype(np.uint16)
    height, width = vals.shape
    if bpc == 1:
        packed = np.packbits(vals, axis=1)
        row_bytes = (width + 7) // 8
        return np.ascontiguousarray(packed[:, :row_bytes]).tobytes()
    if bpc == 2:
        pad = (-width) % 4
        if pad:
            vals = np.pad(vals, ((0, 0), (0, pad)))
        group = vals.reshape(height, -1, 4)
        byte = (group[:, :, 0] << 6) | (group[:, :, 1] << 4) | (group[:, :, 2] << 2) | group[:, :, 3]
        return byte.astype(np.uint8).tobytes()
    pad = width % 2
    if pad:
        vals = np.pad(vals, ((0, 0), (0, 1)))
    group = vals.reshape(height, -1, 2)
    byte = (group[:, :, 0] << 4) | group[:, :, 1]
    return byte.astype(np.uint8).tobytes()


def _pack_gray_bits(y, bpp, width):
    """Pack gray samples that are already 0..255 quantized levels."""
    import numpy as np
    from krop.imagetarget import _pack_gray
    return _pack_gray(np.ascontiguousarray(y, dtype=np.uint8), bpp)


def _bilevel_stream(y, width, height, prefer_jbig2=False):
    import numpy as np
    bits = np.where(np.ascontiguousarray(y) >= 128, 255, 0).astype(np.uint8)
    recon = np.repeat(bits[:, :, None], 3, axis=2)
    if prefer_jbig2:
        payload = _jbig2_bytes(bits)
        if payload:
            keys = _image_keys(width, height, 1, "/DeviceGray", "/JBIG2Decode")
            return _blob(payload, keys, "JBIG2", recon)
    payload = _ccitt_bytes(bits)
    if payload:
        parms = "<< /K -1 /Columns %d /Rows %d >>" % (int(width), int(height))
        keys = _image_keys(width, height, 1, "/DeviceGray", "/CCITTFaxDecode", {"DecodeParms": parms})
        return _blob(payload, keys, "CCITT", recon)
    raw = _pack_index_bits((bits >= 128).astype(np.uint8), 1)
    keys = _image_keys(width, height, 1, "/DeviceGray", "/FlateDecode")
    return _blob(zlib.compress(raw, 9), keys, "Flate", recon)


def _ccitt_bytes(bits):
    import io
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        im = Image.fromarray(bits, "L").convert("1")
        buf = io.BytesIO()
        im.save(buf, format="TIFF", compression="group4")
        raw = buf.getvalue()
        t = Image.open(io.BytesIO(raw))
        off = t.tag_v2[273]
        cnt = t.tag_v2[279]
        if isinstance(off, int):
            return raw[int(off):int(off) + int(cnt)]
        return b"".join(raw[int(o):int(o) + int(c)] for o, c in zip(off, cnt))
    except Exception:
        return None


def _jbig2_bytes(bits):
    import os
    import shutil
    import subprocess
    import tempfile
    import numpy as np
    tool = shutil.which("jbig2")
    if not tool:
        return None
    try:
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
        return proc.stdout
    except Exception:
        return None


def install_image(doc, xref, encoded):
    """Replace xref's stream and image dictionary. SMask and Mask stay.

    ``compress=0`` stores the bytes as they are. The stream is already
    Flate, DCT, CCITT, or JBIG2; asking MuPDF to compress it again is a
    second full deflate of data that will not get smaller.
    """
    data = bytes(encoded["data"])
    doc.update_stream(int(xref), data, compress=0)
    for key, value in encoded["keys"].items():
        doc.xref_set_key(int(xref), key, value)
    for key in ("Decode", "ImageMask"):
        try:
            doc.xref_set_key(int(xref), key, "null")
        except Exception:
            pass
    if "DecodeParms" not in encoded["keys"]:
        try:
            doc.xref_set_key(int(xref), "DecodeParms", "null")
        except Exception:
            pass
    return len(data)


def _pixmap_rgb(pix):
    """Own the RGB samples. A contiguous view would alias the pixmap buffer."""
    import numpy as np
    raw = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.stride)[:, :pix.w * pix.n]
    return np.array(raw.reshape(pix.h, pix.w, pix.n)[..., :3], dtype=np.uint8, copy=True)


def _clip_rgb(page, dpi, rect):
    kwargs = {"alpha": False, "dpi": int(dpi)}
    if rect is not None:
        kwargs["clip"] = rect
    return _pixmap_rgb(page.get_pixmap(**kwargs))


def _placement(doc, xref):
    for i, page in enumerate(doc):
        try:
            rects = page.get_image_rects(int(xref))
        except Exception:
            rects = []
        if rects:
            return i, rects[0]
    return 0, None


def render_page_array(path, xref, encoded, dpi=96):
    """Install ``encoded`` and return the page RGB at ``dpi``, plus install/render seconds.

    The working copy stays open. A later target replaces the stream in place
    instead of parsing the PDF again. The array owns its samples.
    """
    import time
    dpi_i = int(round(float(dpi)))
    wc = live_copy(path)
    started = time.perf_counter()
    if encoded:
        wc.rebuild({int(xref): "placed"}, {(int(xref), "placed"): encoded})
    install_s = time.perf_counter() - started
    page_i = wc.page_for(xref)
    started = time.perf_counter()
    rgb = _clip_rgb(wc.doc[page_i], dpi_i, None)
    wc.note_page(page_i)
    render_s = time.perf_counter() - started
    return rgb, install_s, render_s, page_i


def paired_display(path, xref, encoded, dpi=96):
    """(shown, original) renders with the same page, clip, dpi, and matrix.

    ``encoded`` None is the original image (target keep): the two arrays match.
    """
    import pymupdf
    dpi_i = int(round(float(dpi)))
    original = pymupdf.open(path)
    try:
        page_i, rect = _placement(original, xref)
        base = _clip_rgb(original[page_i], dpi_i, rect)
    finally:
        original.close()
    if not encoded:
        return base.copy(), base
    wc = WorkingCopy(path)
    try:
        wc.rebuild({int(xref): "placed"}, {(int(xref), "placed"): encoded})
        shown = _clip_rgb(wc.doc[page_i], dpi_i, rect)
    finally:
        wc.close()
    h = min(shown.shape[0], base.shape[0])
    w = min(shown.shape[1], base.shape[1])
    return shown[:h, :w].copy(), base[:h, :w].copy()


def render_placed_image(path, xref, encoded, dpi=96):
    """MuPDF render of one replaced image, cropped to its placement on the page.

    This is the same raster the viewer draws, not a Pillow downsample of the
    encoded samples. dpi is the viewer resolution times the current zoom.
    """
    import numpy as np
    wc = WorkingCopy(path)
    try:
        wc.rebuild({int(xref): "placed"}, {(int(xref), "placed"): encoded})
        page_i = 0
        rect = None
        for i, page in enumerate(wc.doc):
            try:
                rects = page.get_image_rects(int(xref))
            except Exception:
                rects = []
            if rects:
                page_i = i
                rect = rects[0]
                break
        dpi_i = int(round(float(dpi)))
        pix = wc.doc[page_i].get_pixmap(alpha=False, dpi=dpi_i)
        raw = np.frombuffer(pix.samples, np.uint8).reshape(pix.h, pix.stride)[:, :pix.w * pix.n]
        raw = raw.reshape(pix.h, pix.w, pix.n)[..., :3].copy()
        if rect is None:
            return raw
        scale = float(dpi_i) / 72.0
        x0 = max(0, int(rect.x0 * scale))
        y0 = max(0, int(rect.y0 * scale))
        x1 = min(raw.shape[1], int(rect.x1 * scale))
        y1 = min(raw.shape[0], int(rect.y1 * scale))
        if x1 <= x0 or y1 <= y0:
            return raw
        return raw[y0:y1, x0:x1]
    finally:
        wc.close()


def decode_stream(encoded):
    """RGB (or gray expanded to RGB) pixels MuPDF produces from the stream."""
    import numpy as np
    import pymupdf
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=10, height=10)
        pix = pymupdf.Pixmap(pymupdf.csRGB, 2, 2, b"\x00" * 12, False)
        xref = page.insert_image(pymupdf.Rect(0, 0, 10, 10), pixmap=pix)
        install_image(doc, xref, encoded)
        out = pymupdf.Pixmap(doc, xref)
        if out.colorspace and out.colorspace.n == 4:
            out = pymupdf.Pixmap(pymupdf.csRGB, out)
        if out.n == 1:
            arr = np.array(
                np.frombuffer(out.samples, np.uint8).reshape(out.h, out.stride)[:, :out.w],
                dtype=np.uint8, copy=True)
            return np.repeat(arr[:, :, None], 3, axis=2)
        return _pixmap_rgb(out)
    finally:
        doc.close()


def stream_length(doc, xref):
    """Encoded length as stored, which is the /Length of the stream."""
    kind, value = doc.xref_get_key(int(xref), "Length")
    try:
        return int(value)
    except (TypeError, ValueError):
        return len(doc.xref_stream(int(xref)))


_SNAP_KEYS = (
    "Filter", "DecodeParms", "ColorSpace", "BitsPerComponent",
    "Width", "Height", "Decode", "ImageMask",
)

_LIVE = {}
_LIVE_LOCK = None


def _live_lock():
    global _LIVE_LOCK
    if _LIVE_LOCK is None:
        import threading
        _LIVE_LOCK = threading.Lock()
    return _LIVE_LOCK


def live_copy(path):
    """One working copy per file. Switching targets does not parse the PDF again."""
    path = os.path.abspath(path)
    with _live_lock():
        wc = _LIVE.get(path)
        if wc is None or wc.doc is None:
            wc = WorkingCopy(path)
            _LIVE[path] = wc
        return wc


class WorkingCopy(object):
    """In-memory copy of a PDF. The file on disk is not opened for writing.

    ``rebuild`` replaces streams in the document that is already open. It does
    not close, reopen, garbage-collect, or save the document.
    """

    def __init__(self, path):
        import pymupdf
        self.path = os.path.abspath(path)
        with open(self.path, "rb") as fh:
            self._bytes = fh.read()
        self.doc = pymupdf.open(stream=self._bytes, filetype="pdf")
        self.choices = {}
        self._snap = {}
        self._sig = {}
        self._pages = set()
        self._page_for = {}

    def close(self):
        if self.doc is not None:
            self.doc.close()
            self.doc = None
        if _LIVE.get(self.path) is self:
            _LIVE.pop(self.path, None)

    def page_for(self, xref):
        xref = int(xref)
        cached = self._page_for.get(xref)
        if cached is not None:
            return cached
        page_i, _rect = _placement(self.doc, xref)
        self._page_for[xref] = page_i
        return page_i

    def note_page(self, page_i):
        self._pages.add(int(page_i))

    def drop_cached_pages(self):
        """Drop pages already drawn so the next pixmap sees the new stream."""
        if self.doc is None:
            return
        for page_i in list(self._pages):
            try:
                self.doc.reload_page(self.doc[page_i])
            except Exception:
                pass

    def _snapshot(self, xref):
        xref = int(xref)
        if xref in self._snap:
            return
        try:
            raw = self.doc.xref_stream_raw(xref)
        except Exception:
            raw = None
        keys = {}
        for key in _SNAP_KEYS:
            try:
                keys[key] = self.doc.xref_get_key(xref, key)
            except Exception:
                keys[key] = ("null", "null")
        self._snap[xref] = (None if raw is None else bytes(raw), keys)

    def _restore(self, xref):
        xref = int(xref)
        snap = self._snap.get(xref)
        if not snap or snap[0] is None:
            return False
        raw, keys = snap
        self.doc.update_stream(xref, raw, compress=0)
        for key, (kind, value) in keys.items():
            try:
                if kind == "null" or value in ("null", None, ""):
                    self.doc.xref_set_key(xref, key, "null")
                else:
                    self.doc.xref_set_key(xref, key, value)
            except Exception:
                pass
        return True

    def rebuild(self, choices, blobs):
        """Install each non-keep choice into the open document.

        A choice that was dropped is put back from the original stream bytes.
        The PDF is not opened again and nothing is written to disk.
        """
        wanted = {}
        for xref, target in (choices or {}).items():
            if target and str(target) != "keep":
                wanted[int(xref)] = str(target)
        dropped = False
        changed = False
        for xref in list(self.choices):
            if xref not in wanted:
                self._restore(xref)
                self.choices.pop(xref, None)
                self._sig.pop(xref, None)
                dropped = True
        for xref, target in wanted.items():
            blob = blobs[(xref, target)]
            sig = id(blob.get("data"))
            if self.choices.get(xref) == target and self._sig.get(xref) == sig:
                continue
            self._snapshot(xref)
            install_image(self.doc, xref, blob)
            self.choices[xref] = target
            self._sig[xref] = sig
            changed = True
        if changed or dropped:
            self.drop_cached_pages()
        return self.doc

    def original_bytes(self):
        return self._bytes
