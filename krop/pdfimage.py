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


def remember(path, xref, target, encoded, entry):
    stored = dict(encoded)
    stored.pop("recon", None)
    _CACHE[(os.path.abspath(path), int(xref), str(target))] = {
        "encoded": stored, "entry": entry,
    }


def cached_entry(path, xref, target):
    item = _CACHE.get((os.path.abspath(path), int(xref), str(target)))
    return None if item is None else item.get("entry")


def cached_encoded(path, xref, target):
    item = _CACHE.get((os.path.abspath(path), int(xref), str(target)))
    return None if item is None else item.get("encoded")


def encode_stream(arr, target_id):
    """Full-resolution PDF image stream for target_id.

    Returns ``{"data", "keys", "codec", "recon"}``. ``data`` is the exact
    bytes stored in the content stream (already compressed when the filter
    says so). ``recon`` is what MuPDF should decode it back to.
    """
    import numpy as np
    from krop.imagetarget import _indexed_flags, _jpeg_roundtrip, _quantize_gray, _as_gray, _rgb, reconstruct
    tid = str(target_id or "keep")
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
    """Replace xref's stream and image dictionary. SMask and Mask stay."""
    data = bytes(encoded["data"])
    doc.update_stream(int(xref), data, compress=False)
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


def decode_stream(encoded):
    """RGB (or gray expanded to RGB) pixels MuPDF produces from the stream."""
    import numpy as np
    import pymupdf
    recon = encoded.get("recon")
    height = int(recon.shape[0])
    width = int(recon.shape[1])
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
            arr = np.frombuffer(out.samples, np.uint8).reshape(out.h, out.stride)[:, :out.w]
            return np.repeat(np.ascontiguousarray(arr)[:, :, None], 3, axis=2)
        raw = np.frombuffer(out.samples, np.uint8).reshape(out.h, out.stride)[:, :out.w * out.n]
        raw = raw.reshape(out.h, out.w, out.n)
        if raw.shape[0] != height or raw.shape[1] != width:
            # The dictionary width/height is authoritative; pixmap follows it.
            pass
        return np.ascontiguousarray(raw[..., :3])
    finally:
        doc.close()


def stream_length(doc, xref):
    """Encoded length as stored, which is the /Length of the stream."""
    kind, value = doc.xref_get_key(int(xref), "Length")
    try:
        return int(value)
    except (TypeError, ValueError):
        return len(doc.xref_stream(int(xref)))


class WorkingCopy(object):
    """In-memory copy of a PDF. The file on disk is not opened for writing."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        with open(self.path, "rb") as fh:
            self._bytes = fh.read()
        self.doc = None
        self.choices = {}
        self.rebuild({}, {})

    def close(self):
        if self.doc is not None:
            self.doc.close()
            self.doc = None

    def rebuild(self, choices, blobs):
        """Reset to the original bytes, then install each non-keep choice.

        blobs maps (xref, target) to an encoded stream dict.
        """
        import pymupdf
        if self.doc is not None:
            self.doc.close()
        self.doc = pymupdf.open(stream=self._bytes, filetype="pdf")
        applied = {}
        for xref, target in (choices or {}).items():
            if not target or target == "keep":
                continue
            blob = blobs[(int(xref), str(target))]
            install_image(self.doc, int(xref), blob)
            applied[int(xref)] = str(target)
        self.choices = applied
        return self.doc

    def original_bytes(self):
        return self._bytes
