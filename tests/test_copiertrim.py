"""Unit tests for krop.copiertrim on synthetic images."""
import pytest
from krop.qt import QImage, QPainter, QColor, QRect, Qt


def make(w=600, h=800, shadow=True, dust=True, pagenum=True):
    img = QImage(w, h, QImage.Format.Format_Grayscale8)
    img.fill(QColor(255, 255, 255))
    p = QPainter(img)
    black = QColor(0, 0, 0)
    if shadow:
        p.fillRect(QRect(0, 0, 20, h), black)      # left platen shadow
        p.fillRect(QRect(0, 0, w, 15), black)      # top platen shadow
    for y in range(120, 620, 25):                  # text block 100..500 x 120..~620
        p.fillRect(QRect(100, y, 400, 10), black)
    if pagenum:
        p.fillRect(QRect(295, 700, 10, 14), black) # page number 700..713
    if dust:
        p.fillRect(QRect(50, 400, 1, 1), black)
        p.fillRect(QRect(560, 760, 1, 1), black)
    p.end()
    return img


def trim(img):
    from krop.copiertrim import copierTrimMargins
    return copierTrimMargins(img, img.rect(), None)


def edges(r):
    return r.left(), r.top(), r.left() + r.width(), r.top() + r.height()


def test_skips_shadow_and_dust_keeps_pagenum(qapp):
    l, t, r, b = edges(trim(make()))
    assert abs(l - 100) <= 2 and abs(t - 120) <= 2
    assert abs(r - 500) <= 2
    assert 714 <= b <= 717, b          # page number kept, 1px bottom pad


def test_without_pagenum_stops_at_block(qapp):
    l, t, r, b = edges(trim(make(pagenum=False)))
    assert b < 630


def test_clean_scan_same_as_shadowed(qapp):
    assert edges(trim(make(shadow=False))) == edges(trim(make()))


def test_minr_is_united(qapp):
    from krop.copiertrim import copierTrimMargins
    img = make()
    minr = QRect(10, 10, 5, 5)
    nr = copierTrimMargins(img, img.rect(), minr)
    assert nr.contains(minr)


def test_tiny_rect_passthrough(qapp):
    from krop.copiertrim import copierTrimMargins
    img = make()
    r = QRect(0, 0, 5, 5)
    assert copierTrimMargins(img, r, None) == r
