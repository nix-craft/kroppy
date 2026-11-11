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
    HIGHLIGHT_DIM, HIGHLIGHT_HALO, HIGHLIGHT_HALO_WIDTH, HIGHLIGHT_RED,
    HIGHLIGHT_WIDTH, VIEW_DPI, map_image_rects)


class _Spinner(QObject):
    """Repaint the busy glyph. QPainter only, so it works over X11 without GL."""

    def __init__(self, item):
        QObject.__init__(self)
        self._item = item
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        item = self._item
        if item is None or not item.isVisible():
            return
        item._angle = (getattr(item, "_angle", 0) + 30) % 360
        item.update()

    def stop(self):
        self._timer.stop()
        self._item = None


class BusyOverlayItem(QGraphicsItem):
    """Darkens one image and shows a spinner while its preview is computed."""

    def __init__(self, parent):
        QGraphicsItem.__init__(self, parent)
        self._rects = []
        self._angle = 0
        self.setZValue(1200)
        self.setAcceptHoverEvents(True)
        try:
            self.setCursor(Qt.CursorShape.WaitCursor)
        except AttributeError:
            self.setCursor(Qt.WaitCursor)
        try:
            self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        except AttributeError:
            self.setAcceptedMouseButtons(Qt.NoButton)
        self.hide()

    def set_rects(self, rects):
        self.prepareGeometryChange()
        self._rects = list(rects or [])
        self.setVisible(bool(self._rects))
        self.update()

    def rects(self):
        return list(self._rects)

    def boundingRect(self):
        if not self._rects:
            return QRectF()
        x0 = min(r[0] for r in self._rects)
        y0 = min(r[1] for r in self._rects)
        x1 = max(r[0] + r[2] for r in self._rects)
        y1 = max(r[1] + r[3] for r in self._rects)
        return QRectF(x0, y0, x1 - x0, y1 - y0).adjusted(-16, -16, 16, 16)

    def shape(self):
        path = QPainterPath()
        for rect in self._rects:
            path.addRect(QRectF(rect[0], rect[1], rect[2], rect[3]))
        return path

    def hoverEnterEvent(self, event):
        cursor = Qt.CursorShape.WaitCursor if hasattr(Qt, "CursorShape") else Qt.WaitCursor
        scene = self.scene()
        if scene is not None:
            for view in scene.views():
                view.setCursor(cursor)
        QGraphicsItem.hoverEnterEvent(self, event)

    def hoverLeaveEvent(self, event):
        scene = self.scene()
        if scene is not None:
            for view in scene.views():
                view.unsetCursor()
        QGraphicsItem.hoverLeaveEvent(self, event)

    def paint(self, painter, option, widget):
        if not self._rects:
            return
        try:
            no_pen = Qt.PenStyle.NoPen
        except AttributeError:
            no_pen = Qt.NoPen
        painter.setPen(QPen(no_pen))
        painter.setBrush(QBrush(QColor(0, 0, 0, 120)))
        primary = self._rects[0]
        for rect in self._rects:
            if len(rect) > 4 and rect[4]:
                primary = rect
            painter.drawRect(QRectF(rect[0], rect[1], rect[2], rect[3]))
        cx = primary[0] + primary[2] / 2.0
        cy = primary[1] + primary[3] / 2.0
        painter.save()
        center = painter.worldTransform().map(QPointF(cx, cy))
        painter.setWorldTransform(QTransform())
        painter.translate(center)
        painter.rotate(getattr(self, "_angle", 0))
        # Hourglass, drawn in device pixels so the stroke cannot vanish on X11.
        glass = QPainterPath()
        glass.moveTo(-7, -9)
        glass.lineTo(7, -9)
        glass.lineTo(0, 0)
        glass.lineTo(7, 9)
        glass.lineTo(-7, 9)
        glass.lineTo(0, 0)
        glass.closeSubpath()
        painter.fillPath(glass, QBrush(QColor(255, 255, 255)))
        for step in range(8):
            alpha = 50 + step * 25
            painter.fillRect(QRectF(9, -1.5, 5, 3), QColor(255, 255, 255, min(alpha, 255)))
            painter.rotate(45)
        painter.restore()


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


def _no_mouse(item):
    try:
        item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
    except AttributeError:
        item.setAcceptedMouseButtons(Qt.NoButton)
    try:
        selectable = QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
        focusable = QGraphicsItem.GraphicsItemFlag.ItemIsFocusable
    except AttributeError:
        selectable = QGraphicsItem.ItemIsSelectable
        focusable = QGraphicsItem.ItemIsFocusable
    item.setFlag(selectable, False)
    item.setFlag(focusable, False)


def _fill_frame(painter, rect, width, color, dashed=False):
    """Draw a width-px frame in the painter's current coordinates."""
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    t = float(width)
    edges = (
        QRectF(x - t / 2.0, y - t / 2.0, w + t, t),
        QRectF(x - t / 2.0, y + h - t / 2.0, w + t, t),
        QRectF(x - t / 2.0, y + t / 2.0, t, max(0.0, h - t)),
        QRectF(x + w - t / 2.0, y + t / 2.0, t, max(0.0, h - t)),
    )
    if not dashed:
        for edge in edges:
            painter.fillRect(edge, color)
        return
    # Dashed along the long axis of each edge, in device pixels.
    step = 8.0
    gap = 6.0
    for edge in edges:
        horizontal = edge.width() >= edge.height()
        span = edge.width() if horizontal else edge.height()
        pos = 0.0
        while pos < span:
            take = min(step, span - pos)
            if horizontal:
                painter.fillRect(QRectF(edge.x() + pos, edge.y(), take, edge.height()), color)
            else:
                painter.fillRect(QRectF(edge.x(), edge.y() + pos, edge.width(), take), color)
            pos += step + gap


class ImageOutlineItem(QGraphicsItem):
    """Red frame in the same coordinates as the dim cutout.

    The stroke is filled in device pixels (at least 3 px, plus a white halo)
    so a scaled graphics view cannot shrink it to nothing. That happened with
    a cosmetic QPen over X11.
    """

    def __init__(self, parent, rect):
        QGraphicsItem.__init__(self, parent)
        self._rect = (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
        self._primary = len(rect) > 4 and bool(rect[4])
        self.setZValue(1000)
        _no_mouse(self)

    def placementRect(self):
        return self._rect

    def isPrimary(self):
        return self._primary

    def boundingRect(self):
        parent = self.parentItem()
        page = getattr(parent, "irect", None) if parent is not None else None
        if page is not None and not page.isNull():
            return QRectF(page).adjusted(-8, -8, 8, 8)
        x, y, w, h = self._rect
        return QRectF(x, y, w, h).adjusted(-8, -8, 8, 8)

    def paint(self, painter, option, widget):
        x, y, w, h = self._rect
        local = QRectF(x, y, w, h)
        painter.save()
        device = painter.worldTransform().mapRect(local)
        painter.setWorldTransform(QTransform())
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        except AttributeError:
            painter.setRenderHint(QPainter.Antialiasing, False)
        _fill_frame(painter, device, HIGHLIGHT_HALO_WIDTH, QColor(*HIGHLIGHT_HALO), dashed=not self._primary)
        _fill_frame(painter, device, HIGHLIGHT_WIDTH, QColor(*HIGHLIGHT_RED), dashed=not self._primary)
        painter.restore()


class ImageHighlightItem(QGraphicsItem):
    """Neutral dim around an image. The red frame is a separate item."""

    def __init__(self, parent):
        QGraphicsItem.__init__(self, parent)
        self._rects = []
        self.setZValue(10)
        _no_mouse(self)
        self._page_rect = QRectF()
        self.hide()

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
        self._busy = BusyOverlayItem(self)
        self._busy_token = None
        self._busy_xref = None
        self._spinner = _Spinner(self._busy)

    def reset(self):
        self._currentPageIndex = 0
        self.brect = QRectF()
        self.irect = QRectF()
        self._images = []
        self._highlight_xref = None
        if getattr(self, "_highlight", None) is not None:
            self._highlight.set_rects([])
        self._set_outlines([])
        if getattr(self, "_busy", None) is not None:
            self._busy.set_rects([])
        self._busy_token = None
        self._busy_xref = None
        self._preview_tokens = {}
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
        self._set_outlines([])

    def refreshImageHighlight(self):
        if getattr(self, "_highlight", None) is None:
            return
        xref = self._highlight_xref
        if not xref:
            self._highlight.set_rects([])
            self._set_outlines([])
            return
        rects = self.imagePlacementRects(xref)
        if not rects:
            self._highlight_xref = None
            self._highlight.set_rects([])
            self._set_outlines([])
            return
        self._highlight.set_rects(rects)
        self._set_outlines(rects)
        if getattr(self, "_busy_xref", None):
            busy = self.imagePlacementRects(self._busy_xref)
            if busy and getattr(self, "_busy", None) is not None:
                self._busy.set_rects(busy)

    def _set_outlines(self, rects):
        for item in list(getattr(self, "_outlines", []) or []):
            scene = item.scene()
            if scene is not None:
                scene.removeItem(item)
        self._outlines = []
        for rect in rects or []:
            self._outlines.append(ImageOutlineItem(self, rect))

    def outlineItems(self):
        return [item for item in getattr(self, "_outlines", []) or [] if item.scene() is not None]

    def clearPreviews(self):
        """Hide previews. Cached images are kept so they can return at once."""
        self._preview_wanted = set()
        self.clearBusy()
        self.update()

    def setPreviewPlan(self, path, plan, rotation):
        """Remember which previews the current page should show.

        Encoding is queued by the analysis panel so a chosen target can jump
        the line. path is unused here and kept so callers stay the same.
        """
        rot = int(rotation or 0)
        wanted = set()
        for item in plan or []:
            try:
                wanted.add((int(item["xref"]), str(item["target"]), rot))
            except (TypeError, ValueError, KeyError):
                continue
        self._preview_wanted = wanted
        self.update()

    def missingPreviews(self):
        wanted = getattr(self, "_preview_wanted", None) or set()
        cache = getattr(self, "_preview_cache", None) or {}
        return [key for key in wanted if key not in cache]

    def showBusy(self, xref, token, rects=None):
        """Darken xref until a preview with this token arrives."""
        try:
            self._busy_xref = int(xref) if xref else None
        except (TypeError, ValueError):
            self._busy_xref = None
        self._busy_token = token
        if self._busy_xref is not None:
            if not hasattr(self, "_preview_tokens"):
                self._preview_tokens = {}
            self._preview_tokens[self._busy_xref] = token
        if rects is None and self._busy_xref:
            rects = self.imagePlacementRects(self._busy_xref)
        if getattr(self, "_busy", None) is not None:
            self._busy.set_rects(rects or [])
        self.update()

    def clearBusy(self):
        self._busy_token = None
        self._busy_xref = None
        if getattr(self, "_busy", None) is not None:
            self._busy.set_rects([])
        self.update()

    def busyVisible(self):
        item = getattr(self, "_busy", None)
        return bool(item is not None and item.isVisible())

    def busyItem(self):
        return getattr(self, "_busy", None)

    def busyRects(self):
        item = getattr(self, "_busy", None)
        if item is None:
            return []
        return item.rects()

    def deliverPreview(self, payload):
        """Store a preview. A token that is no longer current is ignored."""
        if not payload or payload.get("rgba") is None:
            return False
        try:
            xref = int(payload.get("xref"))
        except (TypeError, ValueError):
            return False
        token = payload.get("token")
        expected = (getattr(self, "_preview_tokens", None) or {}).get(xref)
        if expected is not None and token != expected:
            return False
        key = (xref, str(payload.get("target")), int(payload.get("rotation") or 0))
        self._preview_cache[key] = _rgba_qimage(payload.get("rgba"))
        self._preview_pending.discard(key)
        if self._busy_token is not None and token == self._busy_token and xref == self._busy_xref:
            self.clearBusy()
        self.update()
        return True

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
