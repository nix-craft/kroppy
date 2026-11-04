# -*- coding: utf-8 -*-
"""Recover existing crop boxes when opening a PDF (local patch, Nov 2026).

The full page is the private /KropFullBox that our krop writes when cropping
(it also shrinks the MediaBox, like stock krop), else the MediaBox.  If any
page's CropBox is smaller than its full page, write a copy with every box reset
to the full page (so krop shows it uncropped) and return the old crops as krop
crop fractions (left, top, right, bottom).
"""
import os, tempfile

try:
    from PyPDF2 import PdfFileReader, PdfFileWriter
except ImportError:
    from pyPdf import PdfFileReader, PdfFileWriter


def _box(b):
    (x0, y0), (x1, y1) = b.lowerLeft, b.upperRight
    return float(x0), float(y0), float(x1), float(y1)


def _full(p):
    fb = p.get('/KropFullBox')
    if fb is not None and len(fb) == 4:
        return tuple(float(v) for v in fb)
    return _box(p.mediaBox)


def expandCropped(path, eps=0.5):
    """Returns (fullPdfPath, [crop fractions per page]) or None if nothing is cropped."""
    with open(path, 'rb') as f:
        reader = PdfFileReader(f, strict=False)
        if reader.isEncrypted:
            return None
        crops, any_crop = [], False
        for i in range(reader.getNumPages()):
            p = reader.getPage(i)
            if int(p.get('/Rotate', 0) or 0) % 360:
                return None              # rotated pages: leave as is
            mx0, my0, mx1, my1 = _full(p)
            cx0, cy0, cx1, cy1 = _box(p.cropBox)
            mw, mh = mx1 - mx0, my1 - my0
            c = ((cx0 - mx0) / mw, (my1 - cy1) / mh, (mx1 - cx1) / mw, (cy0 - my0) / mh)
            c = tuple(max(0.0, min(0.95, v)) for v in c)
            if max(abs(v) * (mw if k in (0, 2) else mh) for k, v in enumerate(c)) > eps:
                any_crop = True
            crops.append(c)
        if not any_crop:
            return None
        writer = PdfFileWriter()
        for i in range(reader.getNumPages()):
            p = reader.getPage(i)
            x0, y0, x1, y1 = _full(p)
            for b in (p.mediaBox, p.cropBox, p.trimBox, p.artBox, p.bleedBox):
                b.lowerLeft, b.upperRight = (x0, y0), (x1, y1)
            writer.addPage(p)
        d = os.path.join(os.path.expanduser('~/.cache'), 'krop-trimall')
        os.makedirs(d, exist_ok=True)
        fd, out = tempfile.mkstemp(prefix=os.path.splitext(os.path.basename(path))[0] + '-full-', suffix='.pdf', dir=d)
        with os.fdopen(fd, 'wb') as o:
            writer.write(o)
    return out, crops
