# -*- coding: utf-8 -*-
"""Skip region: move one edge of a selection inward past the next
contiguous non-white band and the white space after it (local patch, Nov 2026).

Works on the same grayscale preview image and ink test as copier trim:
a row/column is ink if it has dark pixels (< 128); a band of consecutive
ink rows whose dark pixels add up to fewer than `dust` counts as blank.
"""

from krop.qt import *
from krop.copiertrim import _profile, PAD_RB

EDGES = ("top", "bottom", "left", "right")


def _blank(p, dust):
    blank = [c == 0 for c in p]
    i, n = 0, len(p)
    while i < n:
        if blank[i]:
            i += 1
            continue
        j = i
        while j < n and not blank[j]:
            j += 1
        if sum(p[i:j]) < dust:
            for k in range(i, j):
                blank[k] = True
        i = j
    return blank


def _close_gaps(blank, mingap):
    """Treat white runs shorter than `mingap` that lie between two ink bands as
    ink, so only white space at least `mingap` rows/cols long breaks a region."""
    if mingap <= 1:
        return blank
    b, n, i = list(blank), len(blank), 0
    while i < n:
        if not b[i]:
            i += 1
            continue
        j = i
        while j < n and b[j]:
            j += 1
        if i > 0 and j < n and (j - i) < mingap:
            for k in range(i, j):
                b[k] = False
        i = j
    return b


def _skip_once(blank, touch=PAD_RB + 1):
    """Inward index of the first row of the next ink band after removing one
    ink band plus adjoining white; None if there is no further ink.

    A white run of at most `touch` rows at the edge (e.g. the 1px right/bottom
    pad) still counts as the edge sitting on that ink band."""
    n, k = len(blank), 0
    while k < n and blank[k]:        # white at the edge
        k += 1
    if k >= n:
        return None
    while k < n and not blank[k]:    # the ink band to remove
        k += 1
    while k < n and blank[k]:        # adjoining white
        k += 1
    return k if k < n else None


def skip_region(img, rect, edge, n=1, dust=4, mingap=1):
    """Return the new coordinate of `edge` of QRect `rect` (image pixels) after
    `n` skips, or None if there is no further ink before the opposite edge.

    top/left return the first pixel row/col to keep; bottom/right return the
    exclusive end (left+width style), padded by PAD_RB like copier trim."""
    if edge not in EDGES:
        raise ValueError(edge)
    r = QRect(rect).intersected(img.rect())
    if r.width() < 2 or r.height() < 2:
        return None
    p = _profile(img, r, edge in ("top", "bottom"))
    if edge in ("bottom", "right"):
        p = p[::-1]
    blank = _close_gaps(_blank(p, dust), mingap)
    off = 0
    for _ in range(max(1, n)):
        k = _skip_once(blank[off:])
        if k is None:
            if off == 0:
                return None
            break
        off += k
    if edge == "top":
        return r.top() + off
    if edge == "left":
        return r.left() + off
    end = (r.top() + r.height()) if edge == "bottom" else (r.left() + r.width())
    return min(end, end - off + PAD_RB)


def skip_rect(img, rect, edge, n=1, dust=4, mingap=1):
    """Apply skip_region to a QRect; returns (newrect, moved_px) or (None, 0)."""
    r = QRect(rect).intersected(img.rect())
    v = skip_region(img, r, edge, n, dust, mingap)
    if v is None:
        return None, 0
    x0, y0 = r.left(), r.top()
    x1, y1 = x0 + r.width(), y0 + r.height()
    if edge == "top":
        d, y0 = v - y0, v
    elif edge == "left":
        d, x0 = v - x0, v
    elif edge == "bottom":
        d, y1 = y1 - v, v
    else:
        d, x1 = x1 - v, v
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None, 0
    return QRect(x0, y0, x1 - x0, y1 - y0), d
