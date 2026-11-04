# -*- coding: utf-8 -*-
"""Copier-aware auto trim for krop (local patch, Nov 2026).

Scans from a copier often have: solid black at the outer edge (the lid /
platen shadow), then a white gap, then the real page content.  For each side
this skips the black edge band and the white gap and trims to the content.

dust: a band of consecutive non-empty rows/columns whose dark pixels add up
to fewer than `dust` counts as blank, so small dust dots in the margins are
ignored while thin content (a small page number, whose rows each have only a
few dark pixels) is kept.
"""

from krop.qt import *

_INK = bytes(1 if i < 128 else 0 for i in range(256))


def _profile(img, r, horizontal):
    """Dark-pixel count per row (horizontal=True) or per column of rect r."""
    if not horizontal:
        img = img.copy(r).transformed(QTransform().rotate(90))
        r = QRect(0, 0, img.width(), img.height())
    g = img.convertToFormat(QImage.Format.Format_Grayscale8)
    bpl = g.bytesPerLine()
    ptr = g.constBits()
    try:
        ptr.setsize(g.sizeInBytes())
    except AttributeError:
        ptr.setsize(g.byteCount())
    data = bytes(ptr)
    out = []
    for y in range(r.top(), r.top() + r.height()):
        row = data[y*bpl + r.left(): y*bpl + r.left() + r.width()]
        out.append(row.translate(_INK).count(1))
    return out


def _bounds(p, dust, mingap, maxsearch, width, solid=0.3):
    n = len(p)
    blank = [c == 0 for c in p]
    i = 0
    while i < n:                     # drop small isolated bands (dust)
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

    def side(idx):
        first = next((k for k, i in enumerate(idx) if not blank[i]), None)
        if first is None:
            return None
        edge = max(8, n // 100)       # black edge must start within ~1% of the side
        if first > edge:             # margin already white at the edge
            return first
        run = 0                      # black at the edge: find the white gap
        for k in range(first, min(maxsearch, n)):
            if blank[idx[k]]:
                run += 1
            elif run >= mingap:
                return k
            else:
                run = 0
        return first                 # no gap found: behave like plain trim

    a = side(list(range(n)))
    b = side(list(range(n - 1, -1, -1)))
    if a is None or b is None or a >= n - b:
        return 0, n
    return a, n - b


def copierTrimMargins(img, r, minr, dust=4, gapfrac=0.005, searchfrac=0.25):
    r = QRect(r).intersected(img.rect())
    if r.width() < 10 or r.height() < 10:
        return r
    x0, x1 = r.left(), r.left() + r.width()
    y0, y1 = r.top(), r.top() + r.height()
    # start from the middle 60% so side bands don't hide the gaps
    cx0, cx1 = x0 + r.width()//5, x1 - r.width()//5
    cy0, cy1 = y0 + r.height()//5, y1 - r.height()//5
    for _ in range(3):
        rows = _profile(img, QRect(cx0, y0, cx1 - cx0, y1 - y0), True)
        a, b = _bounds(rows, dust, max(3, int(r.height()*gapfrac)), int(r.height()*searchfrac), cx1 - cx0)
        cy0, cy1 = y0 + a, y0 + b
        cols = _profile(img, QRect(x0, cy0, x1 - x0, cy1 - cy0), False)
        # rotate(90) makes original columns into rows, in order
        a, b = _bounds(cols, dust, max(3, int(r.width()*gapfrac)), int(r.width()*searchfrac), cy1 - cy0)
        cx0, cx1 = x0 + a, x0 + b
    nr = QRect(cx0, cy0, cx1 - cx0, cy1 - cy0)
    if minr is not None:
        nr = nr.united(minr)
    return nr
