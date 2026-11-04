"""Unit tests for krop.skipregion on synthetic images."""
import pytest
from krop.qt import QImage, QPainter, QColor, QRect


def make(w=400, h=600, header=True, footer=True, speck=False):
    img = QImage(w, h, QImage.Format.Format_Grayscale8)
    img.fill(QColor(255, 255, 255))
    p = QPainter(img)
    k = QColor(0, 0, 0)
    if header:
        p.fillRect(QRect(50, 20, 300, 20), k)       # header 20..39
    p.fillRect(QRect(50, 100, 300, 400), k)         # body 100..499
    if footer:
        p.fillRect(QRect(180, 560, 40, 15), k)      # footer 560..574
    if speck:
        p.fillRect(QRect(200, 70, 1, 1), k)         # dust between header and body
    p.end()
    return img


def sr(img, edge, rect=None, n=1, dust=4):
    from krop.skipregion import skip_region
    return skip_region(img, rect or img.rect(), edge, n, dust)


def test_top_from_white_skips_header(qapp):
    assert sr(make(), "top") == 100


def test_top_on_ink_skips_header(qapp):
    assert sr(make(), "top", QRect(0, 20, 400, 580)) == 100


def test_bottom_skips_footer_with_pad(qapp):
    assert sr(make(), "bottom") == 501      # body ends at 500 (excl) + PAD_RB


def test_bottom_repeat_after_pad_does_not_eat_body(qapp):
    img = make(footer=False, header=False)
    img2 = make(header=False, footer=True)
    b = sr(img2, "bottom")
    assert b == 501
    # body is the last band: no further ink, so a second skip does nothing
    assert sr(img2, "bottom", QRect(0, 0, 400, b)) is None


def test_two_skips(qapp):
    img = make()
    # top: header, then body -> lands at footer
    assert sr(img, "top", n=2) == 560


def test_speck_ignored(qapp):
    assert sr(make(speck=True), "top") == 100
    assert sr(make(speck=True), "top", dust=0) == 70   # dust 0: the speck counts


def test_no_further_ink(qapp):
    assert sr(make(header=False, footer=False), "top") is None


def test_opposite_edge_guard(qapp):
    # only the body inside the rect: skipping it would pass the bottom edge
    assert sr(make(), "top", QRect(0, 90, 400, 420)) is None


def test_restricted_to_perpendicular_span(qapp):
    # left/right of the footer column only: footer is at x 180..219
    img = make(header=False)
    assert sr(img, "left", QRect(0, 540, 400, 50)) is None   # just the footer
    assert sr(img, "left") == 50 or sr(img, "left") is None


def test_left_right(qapp):
    img = make()
    assert sr(img, "left") is None          # only one column band (50..349)
    from krop.skipregion import skip_rect
    r, d = skip_rect(img, img.rect(), "top")
    assert r.top() == 100 and d == 100


def test_bad_edge(qapp):
    with pytest.raises(ValueError):
        sr(make(), "middle")


def test_min_gap_merges_small_gaps(qapp):
    """Header 20..39, then a 10px gap, a subheader 50..59, then the body at 100.
    With min gap 1 the first skip stops at the subheader; with min gap 20 the
    10px gap no longer counts as a break, so header+subheader skip together."""
    from krop.skipregion import skip_region
    img = make()
    p = QPainter(img)
    p.fillRect(QRect(50, 50, 300, 10), QColor(0, 0, 0))   # subheader 50..59
    p.end()
    assert skip_region(img, img.rect(), "top", 1, 4, mingap=1) == 50
    assert skip_region(img, img.rect(), "top", 1, 4, mingap=20) == 100
    # the 40px gap before the body still breaks at min gap 40, but not at 41
    assert skip_region(img, img.rect(), "top", 1, 4, mingap=40) == 100
    assert skip_region(img, img.rect(), "top", 1, 4, mingap=41) == 560
    # starting in white: the leading margin is never merged into the region
    assert skip_region(img, QRect(0, 5, 400, 595), "top", 1, 4, mingap=20) == 100
