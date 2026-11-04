# -*- coding: utf-8 -*-
"""Recover existing crop boxes when opening a PDF (local patch, Nov 2026).

The full page is the private /KropFullBox that our krop writes when cropping
(it also shrinks the MediaBox, like krop 0.6), else the MediaBox.  If any
page's CropBox is smaller than its full page, write a copy with every box reset
to the full page (so krop shows it uncropped) and return the old crops as krop
crop fractions (left, top, right, bottom).

Uses PyMuPDF if available, else pypdf or PyPDF2.
"""
import os, re, tempfile

BOXES = ("MediaBox", "CropBox", "TrimBox", "ArtBox", "BleedBox")


def _tempPath(path):
    d = os.path.join(os.path.expanduser('~/.cache'), 'krop-trimall')
    os.makedirs(d, exist_ok=True)
    fd, out = tempfile.mkstemp(prefix=os.path.splitext(os.path.basename(path))[0] + '-full-',
            suffix='.pdf', dir=d)
    os.close(fd)
    return out


def _fractions(full, crop):
    mx0, my0, mx1, my1 = full
    cx0, cy0, cx1, cy1 = crop
    mw, mh = mx1 - mx0, my1 - my0
    c = ((cx0 - mx0) / mw, (my1 - cy1) / mh, (mx1 - cx1) / mw, (cy0 - my0) / mh)
    c = tuple(max(0.0, min(0.95, v)) for v in c)
    size = max(abs(v) * (mw if k in (0, 2) else mh) for k, v in enumerate(c))
    return c, size


def _expandPyMuPdf(path, eps):
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    doc = pymupdf.open(path)
    if doc.is_encrypted:
        return None
    def arr(xref, key):
        kind, val = doc.xref_get_key(xref, key)
        if kind != "array":
            return None
        v = [float(x) for x in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)", val)]
        return tuple(v) if len(v) == 4 else None
    crops, fulls, any_crop = [], [], False
    for page in doc:
        if page.rotation % 360:
            return None              # rotated pages: leave as is
        x = page.xref
        m = page.mediabox            # PDF coordinates (handles inheritance)
        media = arr(x, "MediaBox") or (m.x0, m.y0, m.x1, m.y1)
        full = arr(x, "KropFullBox") or media
        crop = arr(x, "CropBox") or media
        c, size = _fractions(full, crop)
        any_crop = any_crop or size > eps
        crops.append(c)
        fulls.append((x, full))
    if not any_crop:
        return None
    for x, full in fulls:
        a = "[%g %g %g %g]" % full
        for key in BOXES:
            if key == "MediaBox" or doc.xref_get_key(x, key)[0] != "null":
                doc.xref_set_key(x, key, a)
    out = _tempPath(path)
    doc.save(out)
    return out, crops


def _expandPyPdf(path, eps):
    try:
        from pypdf import PdfReader, PdfWriter
        new = True
    except ImportError:
        from PyPDF2 import PdfFileReader as PdfReader, PdfFileWriter as PdfWriter
        new = False
    def box(b):
        (x0, y0), (x1, y1) = (b.lower_left, b.upper_right) if new else (b.lowerLeft, b.upperRight)
        return float(x0), float(y0), float(x1), float(y1)
    def boxes(p):
        if new:
            return (p.mediabox, p.cropbox, p.trimbox, p.artbox, p.bleedbox)
        return (p.mediaBox, p.cropBox, p.trimBox, p.artBox, p.bleedBox)
    def full(p):
        fb = p.get('/KropFullBox')
        if fb is not None and len(fb) == 4:
            return tuple(float(v) for v in fb)
        return box(boxes(p)[0])
    with open(path, 'rb') as f:
        reader = PdfReader(f, strict=False)
        if (reader.is_encrypted if new else reader.isEncrypted):
            return None
        pages = list(reader.pages) if new else [reader.getPage(i) for i in range(reader.getNumPages())]
        crops, any_crop = [], False
        for p in pages:
            if int(p.get('/Rotate', 0) or 0) % 360:
                return None
            c, size = _fractions(full(p), box(boxes(p)[1]))
            any_crop = any_crop or size > eps
            crops.append(c)
        if not any_crop:
            return None
        writer = PdfWriter()
        for p in pages:
            x0, y0, x1, y1 = full(p)
            for b in boxes(p):
                if new:
                    b.lower_left, b.upper_right = (x0, y0), (x1, y1)
                else:
                    b.lowerLeft, b.upperRight = (x0, y0), (x1, y1)
            (writer.add_page if new else writer.addPage)(p)
        out = _tempPath(path)
        with open(out, 'wb') as o:
            writer.write(o)
    return out, crops


def expandCropped(path, eps=0.5):
    """Returns (fullPdfPath, [crop fractions per page]) or None if nothing is cropped."""
    try:
        return _expandPyMuPdf(path, eps)
    except ImportError:
        return _expandPyPdf(path, eps)
