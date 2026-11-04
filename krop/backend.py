# -*- coding: utf-8 -*-
"""
PDF rendering backend for krop (PyMuPDF / fitz).

Everything that needs to rasterize or inspect PDF pages for display goes
through this module, so the viewer does not depend on a specific library.
"""

from krop.qt import QImage

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        raise RuntimeError("Please install PyMuPDF first:"
                           "\n\tpython3 -m pip install --user pymupdf")


class Document:
    """Thin wrapper around a PyMuPDF document."""

    def __init__(self, filename):
        self._doc = fitz.open(filename)

    def numPages(self):
        return self._doc.page_count

    def pageSize(self, idx):
        """Displayed page size in points (rotation applied)."""
        r = self._doc[idx].rect
        return r.width, r.height

    def rotation(self, idx):
        """Page rotation in degrees clockwise: 0, 90, 180 or 270."""
        return self._doc[idx].rotation % 360

    def renderPage(self, idx, dpi=96.0):
        """Render page idx (its CropBox, rotated) to a QImage."""
        pix = self._doc[idx].get_pixmap(dpi=int(round(dpi)), alpha=False)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride,
                     QImage.Format_RGB888)
        return img.copy()  # detach from pix.samples buffer

    def close(self):
        self._doc.close()


def load(filename):
    try:
        return Document(filename)
    except Exception:
        return None
