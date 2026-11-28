"""Image placement rectangles in the viewer, and the highlight drawn for them."""
import numpy as np
import pymupdf
import pytest

from krop.imagehighlight import VIEW_DPI, map_image_rects


def test_map_scales_dpi_and_viewer_origin():
    rects = map_image_rects([(18, 36, 54, 108)], 0, 72, 144, 96, (5, 7))
    scale = 96 / 72.0
    assert rects == [pytest.approx((
        18 * scale + 5, 36 * scale + 7, 36 * scale, 72 * scale, True))]


def test_map_normalizes_inverted_rects_and_marks_the_first_primary():
    rects = map_image_rects(
        [(40, 20, 10, 5), (0, 0, 0, 10), (8, 8, 16, 12)],
        0, 100, 100, 72, (0, 0))
    assert len(rects) == 2
    assert rects[0] == pytest.approx((10, 5, 30, 15, True))
    assert rects[1][4] is False


def test_map_follows_rotation_and_crop_display():
    """The mapped rectangle covers the pixels PyMuPDF actually draws."""
    for rotation in (0, 90, 180, 270):
        doc = pymupdf.open()
        page = doc.new_page(width=400, height=300)
        page.set_cropbox(pymupdf.Rect(50, 40, 350, 240))
        arr = np.zeros((4, 8, 3), np.uint8)
        arr[:] = (0, 200, 0)
        pix = pymupdf.Pixmap(pymupdf.csRGB, 8, 4, arr.tobytes(), False)
        xref = page.insert_image(pymupdf.Rect(80, 60, 140, 100), pixmap=pix)
        if rotation:
            page.set_rotation(rotation)
        raw = [(r.x0, r.y0, r.x1, r.y1) for r in page.get_image_rects(xref)]
        mapped = map_image_rects(
            raw, page.rotation, page.cropbox.width, page.cropbox.height,
            72, (0, 0))
        pm = page.get_pixmap(dpi=72, alpha=False)
        assert mapped, rotation
        x, y, w, h, primary = mapped[0]
        assert primary is True
        # The corners of the highlight land on the drawn image, not the page edge.
        samples = []
        for px, py in ((int(x) + 1, int(y) + 1),
                       (int(x + w) - 2, int(y + h) - 2)):
            px = min(max(px, 0), pm.width - 1)
            py = min(max(py, 0), pm.height - 1)
            samples.append(pm.pixel(px, py))
        assert all(p[1] > 100 and p[0] < 40 for p in samples), (rotation, mapped, samples)
        doc.close()


def test_highlight_dims_without_hue_and_keeps_a_crisp_red_outline(qapp):
    """The dim stays neutral. The red frame stays about 3 px even when scaled down."""
    from krop.qt import QColor, QGraphicsRectItem, QGraphicsScene, QImage, QPainter, QRectF
    from krop.vieweritem import ImageHighlightItem, ImageOutlineItem
    try:
        fmt = QImage.Format.Format_ARGB32
    except AttributeError:
        fmt = QImage.Format_ARGB32
    host = QGraphicsRectItem(0, 0, 120, 90)
    host.irect = QRectF(0, 0, 120, 90)
    scene = QGraphicsScene()
    scene.addItem(host)
    item = ImageHighlightItem(host)
    rects = [(20, 16, 40, 28, True), (70, 16, 30, 24, False)]
    item.set_rects(rects)
    outlines = [ImageOutlineItem(host, rect) for rect in rects]
    assert all(outline.isVisible() for outline in outlines)
    assert outlines[0].zValue() > item.zValue()
    assert outlines[0].placementRect() == (20.0, 16.0, 40.0, 28.0)
    assert outlines[1].placementRect() == (70.0, 16.0, 30.0, 24.0)
    assert outlines[0].isPrimary() and not outlines[1].isPrimary()

    def render(scale):
        image = QImage(max(1, int(120 * scale)), max(1, int(90 * scale)), fmt)
        image.fill(QColor(255, 255, 255))
        painter = QPainter(image)
        scene.render(painter, QRectF(image.rect()), QRectF(0, 0, 120, 90))
        painter.end()
        return image

    shot = render(1)
    outside = shot.pixelColor(4, 4)
    inside = shot.pixelColor(30, 28)
    assert outside.red() == outside.green() == outside.blue()
    assert outside.red() < 255
    assert inside.red() > 250 and inside.green() > 250 and inside.blue() > 250

    def thickness(scale):
        image = render(scale)
        x = int(round(40 * scale))
        x = min(max(x, 0), image.width() - 1)
        y0 = int(round(16 * scale))
        count = 0
        for y in range(max(0, y0 - 8), min(image.height(), y0 + 9)):
            color = image.pixelColor(x, y)
            if color.red() > 180 and color.green() < 80 and color.blue() < 80:
                count += 1
        return count

    # Device pixels, not scene pixels: shrinking the view does not erase the stroke.
    assert 2 <= thickness(1) <= 6
    assert 2 <= thickness(0.25) <= 6


def test_selecting_image_row_highlights_every_placement(window, tmp_path, qapp):
    import time
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    arr = np.zeros((8, 12, 3), np.uint8)
    arr[:] = (180, 30, 20)
    pix = pymupdf.Pixmap(pymupdf.csRGB, 12, 8, arr.tobytes(), False)
    xref = page.insert_image(pymupdf.Rect(40, 50, 100, 90), pixmap=pix)
    page.insert_image(pymupdf.Rect(180, 60, 260, 110), xref=xref)
    # A second page so changing pages can clear the highlight.
    doc.new_page(width=400, height=300).insert_text((72, 80), "Other", fontsize=16, color=(0, 0, 0))
    src = str(tmp_path / "placed.pdf")
    doc.save(src)
    doc.close()

    w = window
    w.show()
    qapp.processEvents()
    w.actionAnalysis.setChecked(True)
    w.openFile(src, False)
    deadline = time.time() + 10
    while time.time() < deadline:
        qapp.processEvents()
        if "Document:" in w.analysisPanel.summary.text():
            break
        time.sleep(0.05)
    row = w.analysisPanel.tree.topLevelItem(0)
    image = None
    for i in range(row.childCount()):
        child = row.child(i)
        if "xref %d" % xref in child.text(1):
            image = child
            break
    assert image is not None
    w.analysisPanel._clicked(image, 0)
    qapp.processEvents()
    assert w.viewer.currentPageIndex == 0
    shown = w.viewer.imageHighlightRects()
    assert len(shown) == 2
    assert shown[0][4] is True and shown[1][4] is False

    check = pymupdf.open(src)
    raw = [(r.x0, r.y0, r.x1, r.y1) for r in check[0].get_image_rects(xref)]
    expected = map_image_rects(
        raw, check[0].rotation, check[0].cropbox.width, check[0].cropbox.height,
        VIEW_DPI, (w.viewer.irect.left(), w.viewer.irect.top()))
    check.close()
    assert len(shown) == len(expected)
    for got, want in zip(shown, expected):
        assert got == pytest.approx(want)
    outlines = w.viewer.outlineItems()
    assert len(outlines) == len(shown)
    assert all(item.isVisible() for item in outlines)
    assert all(item.zValue() > w.viewer._highlight.zValue() for item in outlines)
    for item, rect in zip(outlines, shown):
        assert item.placementRect() == pytest.approx(rect[:4])

    # Zoom does not move the highlight in page coordinates.
    before = list(shown)
    w.ui.documentView.scale(1.4, 1.4)
    qapp.processEvents()
    assert w.viewer.imageHighlightRects() == before

    # A blocker that names the image xref highlights it too.
    blocker = None
    for i in range(row.childCount()):
        child = row.child(i)
        if child.text(1).startswith("color image xref %d" % xref):
            blocker = child
            break
    if blocker is not None:
        w.analysisPanel._clicked(blocker, 0)
        qapp.processEvents()
        assert len(w.viewer.imageHighlightRects()) == 2

    # A page row is not an image, so the highlight goes away.
    w.analysisPanel._clicked(row, 0)
    qapp.processEvents()
    assert w.viewer.imageHighlightRects() == []
    assert w.viewer.outlineItems() == []

    # And so does changing to a page that does not contain the image.
    w.analysisPanel._clicked(image, 0)
    qapp.processEvents()
    assert w.viewer.imageHighlightRects()
    w.ui.actionNextPage.trigger()
    qapp.processEvents()
    assert w.viewer.currentPageIndex == 1
    assert w.viewer.imageHighlightRects() == []

    # Closing the dock clears it too.
    w.analysisPanel._clicked(image, 0)
    qapp.processEvents()
    # next page moved us away; go back by clicking the image, which navigates home
    assert w.viewer.currentPageIndex == 0
    assert w.viewer.imageHighlightRects()
    w.actionAnalysis.setChecked(False)
    qapp.processEvents()
    assert w.viewer.imageHighlightRects() == []
