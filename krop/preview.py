# -*- coding: utf-8 -*-
"""Display-only preview of an image at its chosen storage target.

The pixels are produced by the same gray and palette quantizers the trial
encode uses. Nothing here writes the PDF. Masks and soft masks are kept as
the alpha plane so transparent parts of the placement stay transparent.
"""
import numpy as np


def orient_for_page(arr, rotation):
    """Rotate image pixels to match the page pixmap.

    Page rotation 90 is clockwise in the pixmap PyMuPDF draws, which is
    ``rot90(..., -1)``. 180 and 270 follow the same direction.
    """
    rot = int(rotation or 0) % 360
    turns = {0: 0, 90: -1, 180: 2, 270: 1}.get(rot, 0)
    if not turns:
        return arr
    return np.rot90(arr, turns)


def quantize_pixels(arr, target_id):
    """Return uint8 HxWx3 pixels for a storage target id.

    JPEG targets are a real encode/decode, so the preview shows the artifacts.
    """
    from krop.imagetarget import reconstruct
    return reconstruct(arr, target_id)


def _mask_xref(doc, xref):
    for key in ("SMask", "Mask"):
        try:
            kind, val = doc.xref_get_key(xref, key)
        except Exception:
            continue
        if kind == "xref" and val and val != "null":
            try:
                return int(str(val).split()[0])
            except ValueError:
                continue
    return None


def _is_image_mask(doc, xref):
    try:
        _kind, val = doc.xref_get_key(xref, "ImageMask")
    except Exception:
        return False
    return str(val).lower() == "true"


def _pixmap_array(pix):
    n = pix.n
    raw = np.frombuffer(pix.samples, np.uint8)
    raw = raw.reshape(pix.h, pix.stride)[:, :pix.w * n].reshape(pix.h, pix.w, n)
    return np.ascontiguousarray(raw)


def _resize_nearest(arr, height, width):
    if arr.shape[0] == height and arr.shape[1] == width:
        return arr
    ys = np.linspace(0, arr.shape[0] - 1, height).astype(np.int32)
    xs = np.linspace(0, arr.shape[1] - 1, width).astype(np.int32)
    return arr[ys][:, xs]


def load_image(path, xref):
    """Return ``(color, alpha)``. alpha is None when the image is opaque.

    color is HxW or HxWx3. alpha is HxW uint8, from the image's own alpha,
    an SMask, or a Mask XObject. An ImageMask becomes black with that bitmap
    as alpha (0 is transparent).
    """
    import pymupdf
    doc = pymupdf.open(path)
    try:
        pix = pymupdf.Pixmap(doc, int(xref))
        if pix.colorspace and pix.colorspace.n == 4:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
        raw = _pixmap_array(pix)
        alpha = None
        if pix.alpha:
            alpha = raw[:, :, -1]
            raw = raw[:, :, :-1]
        if raw.shape[-1] == 1:
            color = raw[:, :, 0]
        else:
            color = raw
        mask_xref = _mask_xref(doc, xref)
        if mask_xref:
            mpix = pymupdf.Pixmap(doc, int(mask_xref))
            mraw = _pixmap_array(mpix)
            alpha = mraw[:, :, 0]
            if alpha.shape != (pix.h, pix.w):
                alpha = _resize_nearest(alpha, pix.h, pix.w)
        if _is_image_mask(doc, xref):
            gray = color if color.ndim == 2 else color[:, :, 0]
            alpha = gray
            color = np.zeros_like(gray)
        return color, alpha
    finally:
        doc.close()


def preview_rgba(path, xref, target_id, rotation=0):
    """Quantized RGBA for one image, oriented for the page rotation."""
    color, alpha = load_image(path, xref)
    rgb = quantize_pixels(color, target_id)
    rgb = orient_for_page(rgb, rotation)
    if alpha is None:
        alpha = np.full(rgb.shape[:2], 255, np.uint8)
    else:
        alpha = orient_for_page(alpha, rotation)
        if alpha.shape[:2] != rgb.shape[:2]:
            alpha = _resize_nearest(alpha, rgb.shape[0], rgb.shape[1])
    return np.dstack([rgb, np.ascontiguousarray(alpha, dtype=np.uint8)])


def paste_preview(canvas, rgba, rect):
    """Nearest-neighbor composite of rgba into canvas at rect (x, y, w, h).

    canvas is HxWx3 or HxWx4. Returns a copy. Alpha 0 leaves the canvas
    pixel unchanged. Used by tests and matches a scaled placement.
    """
    out = np.array(canvas, copy=True)
    if rgba is None or rect is None or len(rect) < 4:
        return out
    x, y, w, h = (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    if w <= 0 or h <= 0:
        return out
    sh, sw = rgba.shape[:2]
    x0 = max(0, int(np.floor(x)))
    y0 = max(0, int(np.floor(y)))
    x1 = min(out.shape[1], int(np.ceil(x + w)))
    y1 = min(out.shape[0], int(np.ceil(y + h)))
    if x1 <= x0 or y1 <= y0:
        return out
    xs = np.arange(x0, x1)
    ys = np.arange(y0, y1)
    sx = np.clip(((xs + 0.5 - x) / w * sw).astype(np.int32), 0, sw - 1)
    sy = np.clip(((ys + 0.5 - y) / h * sh).astype(np.int32), 0, sh - 1)
    patch = rgba[sy][:, sx]
    if patch.shape[-1] == 4:
        alpha = patch[:, :, 3:4].astype(np.float32) / 255.0
        src = patch[:, :, :out.shape[2]].astype(np.float32)
        dst = out[y0:y1, x0:x1, :src.shape[-1]].astype(np.float32)
        mixed = src * alpha + dst * (1.0 - alpha)
        out[y0:y1, x0:x1, :src.shape[-1]] = np.clip(np.rint(mixed), 0, 255).astype(out.dtype)
    else:
        out[y0:y1, x0:x1, :patch.shape[-1]] = patch[:, :, :out.shape[-1]]
    return out
