# -*- coding: utf-8 -*-
"""Map an image's PDF placement rectangles into viewer coordinates.

The viewer draws each page as a pixmap at VIEW_DPI. page.rect is the
displayed page (crop box, rotation already applied). Image rectangles from
Page.get_image_rects are in that same space. They are scaled into pixmap
pixels and then shifted by the image's origin in the scene, which is the
padding around the pixmap.
"""

VIEW_DPI = 96

# Neutral darkening of everything outside the image (no hue), plus a
# cosmetic red stroke that stays the same width on screen when zooming.
HIGHLIGHT_DIM = (0, 0, 0, 56)
HIGHLIGHT_RED = (255, 0, 0)
HIGHLIGHT_WIDTH = 2.0


def _rotate_rect(rect, rotation, width, height):
    """Move an unrotated page rectangle into the displayed page.

    width and height are the unrotated page size. Rotation matches the
    pixmap PyMuPDF draws: 90 and 270 swap the axes, 180 flips both.
    """
    x0, y0, x1, y1 = rect
    corners = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
    rot = int(rotation or 0) % 360
    if rot == 90:
        mapped = [(height - y, x) for x, y in corners]
    elif rot == 180:
        mapped = [(width - x, height - y) for x, y in corners]
    elif rot == 270:
        mapped = [(y, width - x) for x, y in corners]
    else:
        mapped = list(corners)
    xs = [p[0] for p in mapped]
    ys = [p[1] for p in mapped]
    return min(xs), min(ys), max(xs), max(ys)


def map_image_rects(image_rects, rotation, page_width, page_height, dpi, origin):
    """Return viewer rectangles (x, y, w, h, primary).

    image_rects are (x0, y0, x1, y1) from Page.get_image_rects, in the
    unrotated page space shared with an unrotated pixmap. rotation is the
    page rotation. page_width and page_height are the unrotated page size
    (the crop box). origin is the pixmap's top-left in the viewer, which
    is where the trim padding sits. The first rectangle is primary.
    """
    if dpi <= 0 or page_width <= 0 or page_height <= 0:
        return []
    scale = float(dpi) / 72.0
    left, top = float(origin[0]), float(origin[1])
    out = []
    for rect in image_rects or []:
        if rect is None or len(rect) < 4:
            continue
        x0, y0, x1, y1 = (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        if x1 <= x0 or y1 <= y0:
            continue
        dx0, dy0, dx1, dy1 = _rotate_rect((x0, y0, x1, y1), rotation, page_width, page_height)
        out.append((
            dx0 * scale + left,
            dy0 * scale + top,
            (dx1 - dx0) * scale,
            (dy1 - dy0) * scale,
            len(out) == 0,
        ))
    return out
