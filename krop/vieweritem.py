# -*- coding: iso-8859-1 -*-

"""
Viewer for krop used to display PDF files.

Copyright (C) 2010-2025 Armin Straub, http://arminstraub.com
"""

"""
This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 3 of the License, or
(at your option) any later version.
"""

import sys

from krop.config import PYQT6
from krop.qt import *

from krop.viewerselections import ViewerSelections
from krop.imagehighlight import (
    HIGHLIGHT_DIM, HIGHLIGHT_RED, HIGHLIGHT_WIDTH, VIEW_DPI, map_image_rects)


class _PreviewJob(QThread):
    """Quantize one image off the UI thread. Display only."""
    ready = pyqtSignal(object)

    def __init__(self, path, xref, target, rotation, gen):
        QThread.__init__(self)
        self.path = path
        self.xref = xref
        self.target = target
        self.rotation = rotation
        self.gen = gen

    def run(self):
        rgba = None
        try:
            from krop.preview import preview_rgba
            rgba = preview_rgba(self.path, self.xref, self.target, self.rotation)
        except Exception:
            rgba = None
        self.ready.emit({
            "xref": int(self.xref), "target": self.target,
            "rotation": int(self.rotation or 0), "gen": self.gen, "rgba": rgba,
        })


def _rgba_qimage(rgba):
    import numpy as np
    rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
    height, width, channels = rgba.shape
    try:
        fmt = QImage.Format.Format_RGBA8888 if channels == 4 else QImage.Format.Format_RGB888
    except AttributeError:
        fmt = QImage.Format_RGBA8888 if channels == 4 else QImage.Format_RGB888
    image = QImage(rgba.data, width, height, width * channels, fmt)
    return image.copy()


class ImageHighlightItem(QGraphicsItem):
    """Neutral dim around an image, with a crisp red outline. Not a crop selection."""

    def __init__(self, parent):
        QGraphicsItem.__init__(self, parent)
        self._rects = []
        self.setZValue(-1)
        try:
            self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        except AttributeError:
            self.setAcceptedMouseButtons(Qt.NoButton)
        try:
            selectable = QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            focusable = QGraphicsItem.GraphicsItemFlag.ItemIsFocusable
        except AttributeError:
            selectable = QGraphicsItem.ItemIsSelectable
            focusable = QGraphicsItem.ItemIsFocusable
        self.setFlag(selectable, False)
        self.setFlag(focusable, False)
        self._page_rect = QRectF()
        self.hide()

    def outline_pen(self, primary):
        """Cosmetic so the stroke stays about 2 px at any zoom."""
        pen = QPen(QColor(*HIGHLIGHT_RED))
        pen.setCosmetic(True)
        pen.setWidthF(HIGHLIGHT_WIDTH if primary else max(1.0, HIGHLIGHT_WIDTH - 0.5))
        try:
            solid = Qt.PenStyle.SolidLine
            dash = Qt.PenStyle.DashLine
        except AttributeError:
            solid = Qt.SolidLine
            dash = Qt.DashLine
        pen.setStyle(solid if primary else dash)
        return pen

    def set_rects(self, rects):
        self.prepareGeometryChange()
        self._rects = list(rects or [])
        parent = self.parentItem()
        if parent is not None and hasattr(parent, "irect"):
            self._page_rect = QRectF(parent.irect)
        else:
            self._page_rect = QRectF()
        self.setVisible(bool(self._rects))
        self.update()

    def rects(self):
        return list(self._rects)

    def boundingRect(self):
        if not self._rects:
            return QRectF()
        if not self._page_rect.isNull():
            return QRectF(self._page_rect).adjusted(-2, -2, 2, 2)
        x0 = min(r[0] for r in self._rects)
        y0 = min(r[1] for r in self._rects)
        x1 = max(r[0] + r[2] for r in self._rects)
        y1 = max(r[1] + r[3] for r in self._rects)
        return QRectF(x0, y0, x1 - x0, y1 - y0).adjusted(-3, -3, 3, 3)

    def paint(self, painter, option, widget):
        if not self._rects:
            return
        try:
            no_pen = Qt.PenStyle.NoPen
            no_brush = Qt.BrushStyle.NoBrush
            odd = Qt.FillRule.OddEvenFill
            no_aa = QPainter.RenderHint.Antialiasing
        except AttributeError:
            no_pen = Qt.NoPen
            no_brush = Qt.NoBrush
            odd = Qt.OddEvenFill
            no_aa = QPainter.Antialiasing
        page = self._page_rect
        if not page.isNull():
            path = QPainterPath()
            path.setFillRule(odd)
            path.addRect(page)
            for rect in self._rects:
                path.addRect(QRectF(rect[0], rect[1], rect[2], rect[3]))
            painter.setPen(QPen(no_pen))
            painter.setBrush(QBrush(QColor(*HIGHLIGHT_DIM)))
            painter.drawPath(path)
        painter.setRenderHint(no_aa, False)
        painter.setBrush(QBrush(no_brush))
        for rect in self._rects:
            primary = len(rect) > 4 and rect[4]
            painter.setPen(self.outline_pen(primary))
            painter.drawRect(QRectF(rect[0], rect[1], rect[2], rect[3]))


class AbstractViewerItem(QGraphicsItem):
    """Abstract class for displaying a PDF document and for allowing the user
    to create selections."""
    def __init__(self, mainwindow):
        QGraphicsItem.__init__(self)
        self.selections = ViewerSelections(self)
        self._highlight = None
        self._highlight_xref = None
        self.reset()
        self.mainwindow = mainwindow
        self._highlight = ImageHighlightItem(self)

    def reset(self):
        self._currentPageIndex = 0
        self.brect = QRectF()
        self.irect = QRectF()
        self._images = []
        self._highlight_xref = None
        if getattr(self, "_highlight", None) is not None:
            self._highlight.set_rects([])
        self.selections.deleteSelections()
        self._preview_cache = {}
        self._preview_wanted = set()
        self._preview_pending = set()
        self._preview_jobs = []
        self._preview_gen = getattr(self, "_preview_gen", 0) + 1

    def boundingRect(self):
        return self.brect

    def isPortrait(self):
        return self.irect.width() <= self.irect.height()

    def paint(self, painter, option, widget):
        img = self.getImage(self.currentPageIndex)
        if img is None:
            return
        painter.drawRect(self.irect.adjusted(-1,-1,1,1))
        painter.drawImage(self.irect, img)
        self._paint_previews(painter)

    def mapRectToImage(self, r):
        return r.translated(-self.irect.left(), -self.irect.top())

    def mapRectFromImage(self, r):
        return r.translated(self.irect.left(), self.irect.top())

    def getCurrentPageIndex(self):
        return self._currentPageIndex

    def setCurrentPageIndex(self, idx):
        if idx >= self.numPages():
            idx = self.numPages()-1
        if idx < 0:
            idx = 0
        self._currentPageIndex = idx
        panel = getattr(self.mainwindow, "analysisPanel", None)
        if panel is not None:
            panel.follow_page(idx + 1)

        img = self.getImage(idx)
        if img is None:
            return
        self.selections.updateSelectionVisibility()

        self.prepareGeometryChange()
        rect = QRectF(img.rect())
        # inflate slightly so that bounding rect will be visible
        padding = 5
        self.brect = QRectF(0,0,rect.width()+2*padding,rect.height()+2*padding)
        self.irect = QRectF(padding,padding,rect.width(),rect.height())
        self.scene().setSceneRect(self.brect)
        self.refreshImageHighlight()

    def showImageHighlight(self, xref):
        """Shade every placement of xref on the current page."""
        try:
            self._highlight_xref = int(xref) if xref else None
        except (TypeError, ValueError):
            self._highlight_xref = None
        self.refreshImageHighlight()

    def clearImageHighlight(self):
        self._highlight_xref = None
        if getattr(self, "_highlight", None) is not None:
            self._highlight.set_rects([])

    def refreshImageHighlight(self):
        if getattr(self, "_highlight", None) is None:
            return
        xref = self._highlight_xref
        if not xref:
            self._highlight.set_rects([])
            return
        rects = self.imagePlacementRects(xref)
        if not rects:
            self._highlight_xref = None
            self._highlight.set_rects([])
            return
        self._highlight.set_rects(rects)

    def clearPreviews(self):
        """Hide previews. Cached images are kept so they can return at once."""
        self._preview_wanted = set()
        self.update()

    def setPreviewPlan(self, path, plan, rotation):
        """Show these (xref, target) previews. Quantize any that are not cached."""
        rot = int(rotation or 0)
        wanted = set()
        for item in plan or []:
            try:
                wanted.add((int(item["xref"]), str(item["target"]), rot))
            except (TypeError, ValueError, KeyError):
                continue
        self._preview_wanted = wanted
        self._preview_gen = getattr(self, "_preview_gen", 0)
        self.update()
        for xref, target, item_rot in wanted:
            key = (xref, target, item_rot)
            if key in self._preview_cache or key in self._preview_pending:
                continue
            self._preview_pending.add(key)
            job = _PreviewJob(path, xref, target, item_rot, self._preview_gen)
            job.ready.connect(self._on_preview_ready)
            job.finished.connect(job.deleteLater)
            self._preview_jobs.append(job)
            job.finished.connect(lambda job=job: self._preview_jobs.remove(job) if job in self._preview_jobs else None)
            job.start()

    def _on_preview_ready(self, payload):
        if payload.get("gen") != getattr(self, "_preview_gen", None):
            return
        key = (payload.get("xref"), payload.get("target"), payload.get("rotation"))
        self._preview_pending.discard(key)
        rgba = payload.get("rgba")
        if rgba is None:
            return
        self._preview_cache[key] = _rgba_qimage(rgba)
        self.update()

    def previewReady(self):
        return [key for key in self._preview_wanted if key in self._preview_cache]

    def previewImage(self, xref, target, rotation):
        return self._preview_cache.get((int(xref), str(target), int(rotation or 0)))

    def _paint_previews(self, painter):
        wanted = getattr(self, "_preview_wanted", None)
        cache = getattr(self, "_preview_cache", None)
        if not wanted or not cache:
            return
        painter.save()
        try:
            hint = QPainter.RenderHint.SmoothPixmapTransform
        except AttributeError:
            hint = QPainter.SmoothPixmapTransform
        painter.setRenderHint(hint, True)
        for key in list(wanted):
            image = cache.get(key)
            if image is None:
                continue
            xref = key[0]
            for rect in self.imagePlacementRects(xref):
                painter.drawImage(QRectF(rect[0], rect[1], rect[2], rect[3]), image)
        painter.restore()

    def imageHighlightRects(self):
        if getattr(self, "_highlight", None) is None:
            return []
        return self._highlight.rects()

    def imagePlacementRects(self, xref):
        """Viewer rectangles for xref, or [] when it is not on this page."""
        page = None
        doc = getattr(self, "_pdfdoc", None)
        if doc is not None and hasattr(doc, "__getitem__"):
            try:
                page = doc[self.currentPageIndex]
            except Exception:
                page = None
        if page is None or not hasattr(page, "get_image_rects"):
            return []
        try:
            raw = page.get_image_rects(int(xref))
        except Exception:
            return []
        tuples = []
        for item in raw or []:
            rect = item[0] if isinstance(item, (tuple, list)) and item and hasattr(item[0], "x0") else item
            if not hasattr(rect, "x0"):
                continue
            tuples.append((rect.x0, rect.y0, rect.x1, rect.y1))
        crop = getattr(page, "cropbox", None) or page.rect
        return map_image_rects(
            tuples, getattr(page, "rotation", 0) or 0,
            float(crop.width), float(crop.height), VIEW_DPI,
            (self.irect.left(), self.irect.top()))

    currentPageIndex = property(getCurrentPageIndex, setCurrentPageIndex)

    def previousPage(self):
        self.currentPageIndex = self.currentPageIndex-1

    def nextPage(self):
        self.currentPageIndex = self.currentPageIndex+1

    def firstPage(self):
        self.currentPageIndex = 0

    def lastPage(self):
        self.currentPageIndex = self.numPages()-1

    def getImage(self, idx):
        if idx < 0 or idx >= self.numPages():
            return None
        if self._images[idx] is None:
            self._images[idx] = self.cacheImage(idx)
        return self._images[idx]        

    def mousePressEvent(self, event):
        self.selections.mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self.selections.mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self.selections.mouseReleaseEvent(event)

    def load(self, filename):
        self.reset()
        self.doLoad(filename)
        self._images = [None for i in range(self.numPages())]
        self.firstPage()

    # To be implemented in deriving classes:

    def doLoad(self, filename):
        pass

    def numPages(self):
        return 0

    def isEmpty(self):
        return self.numPages() <= 0

    def cacheImage(self, idx):        
        return None

    def pageGetRotation(self, idx):        
        return 0

    def cropValues(self, idx):
        def adjustForOrientation(cv):
            if r == 90: # Landscape
                return [ cv[1], cv[2], cv[3], cv[0] ]
            elif r == 180: # UpsideDown
                return [ cv[2], cv[3], cv[0], cv[1] ]
            elif r == 270: # Seascape
                return [ cv[3], cv[0], cv[1], cv[2] ]
            else: # r == 0, Portrait
                return cv
        crop_values = self.selections.cropValues(idx)
        r = self.pageGetRotation(idx)
        return [ adjustForOrientation(cv) for cv in crop_values ]


class PopplerViewerItem(AbstractViewerItem):
    """Viewer implementation which uses Poppler to display PDF documents."""
    def reset(self):
        AbstractViewerItem.reset(self)
        self._pdfdoc = None

    def doLoad(self, filename):
        self._pdfdoc = Poppler.Document.load(filename)
        if self._pdfdoc:
            self._pdfdoc.setRenderHint(Poppler.Document.Antialiasing and
                    Poppler.Document.TextAntialiasing)

    def numPages(self):
        if self._pdfdoc is None:    
            return 0
        else:
            return self._pdfdoc.numPages()

    def cacheImage(self, idx):        
        page = self._pdfdoc.page(idx)
        return page.renderToImage(float(VIEW_DPI), float(VIEW_DPI))
        # return page.renderToImage() # default dpi = 72

    def pageGetRotation(self, idx):        
        page = self._pdfdoc.page(idx)
        o = page.orientation()
        if o == page.Landscape:
            return 90
        elif o == page.UpsideDown:
            return 180
        elif o == page.Seascape:
            return 270
        else: # o == page.Portrait
            return 0


class MuPDFViewerItem(AbstractViewerItem):
    """Viewer implementation which uses PyMuPDF to display PDF documents."""
    def reset(self):
        AbstractViewerItem.reset(self)
        self._pdfdoc = None

    def doLoad(self, filename):
        self._pdfdoc = fitz.open(filename)
        # if self._pdfdoc:
        #     self._pdfdoc.setRenderHint(Poppler.Document.Antialiasing and
        #             Poppler.Document.TextAntialiasing)

    def numPages(self):
        if self._pdfdoc is None:    
            return 0
        else:
            return len(self._pdfdoc)

    def cacheImage(self, idx):        
        page = self._pdfdoc[idx]
        pix = page.get_pixmap(alpha=False, dpi=VIEW_DPI) # default dpi is 72
        return QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888)
        # It might be faster to use samples_ptr but the code results in crashes.
        # https://pymupdf.readthedocs.io/en/latest/tutorial.html
        # pix = page.get_pixmap()
        # set the correct QImage format depending on alpha
        # fmt = QImage.Format_RGBA8888 if pix.alpha else QImage.Format.Format_RGB888
        # return QImage(pix.samples_ptr, pix.width, pix.height, fmt)

    def pageGetRotation(self, idx):        
        page = self._pdfdoc[idx]
        return page.rotation


# determine whether to use PopplerQt or PyMuPDF for rendering
POPPLERQT = 1
PYMUPDF = 2
lib_render = 0

from krop.config import PYQT6

# for PyQt6 use PyMuPDF
if PYQT6:
    try:
        import fitz
        lib_render = PYMUPDF
    except ImportError:
        _msg = "Please install PyMuPDF first (PyQt6 is being used)."\
            "\n\tOn recent versions of Ubuntu, the following should do the trick:"\
            "\n\tsudo apt-get install python3-pymupdf"
        raise RuntimeError(_msg)
else:
    # PyQt5 was requested
    if not '--use-poppler' in sys.argv:
        try:
            import fitz
            lib_render = PYMUPDF
        except ImportError:
            pass
    if not lib_render:
        try:
            from popplerqt5 import Poppler
            lib_render = POPPLERQT
        except ImportError:
            pass
    # complain if no version is available
    if not lib_render:
        _msg = "Please install PyMuPDF or Poppler Qt first (PyQt5 is being used)."\
            "\n\tOn versions of Ubuntu such as 22.04, one of the following should do the trick:"\
            "\n\tsudo apt install python3-fitz"\
            "\n\tsudo apt install python3-poppler-qt5"
        raise RuntimeError(_msg)

if lib_render == PYMUPDF:
    ViewerItem = MuPDFViewerItem
    print("Using PyMuPDF for rendering.", file=sys.stderr)
else:
    ViewerItem = PopplerViewerItem
    print("Using PopplerQt for rendering.", file=sys.stderr)
