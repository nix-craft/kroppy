"""Headless GUI test: right-click 'Skip next region inward' across scopes."""
from krop.qt import QApplication
from krop.viewerselections import ViewerSelections

# scanlike.pdf: shadow at top (0..14pt), block 120..640pt, page number ~688..702
H = 792.0


def frac(w, s):
    return w._selFrac(s)


def setup(w, scan):
    w.openFile(scan, False)
    w.ui.radioSelAll.setChecked(True)
    w.slotNewSelection()
    sel = w.selections.currentSelection
    # start right of the left platen shadow (which runs the full height)
    w._setSelFrac(sel, (25 / 612, 0, 1 - 25 / 612, 1))
    return sel


def test_menu_has_skip_entries(window, scan):
    w = window
    sel = setup(w, scan)
    from krop.qt import QMenu
    m = QMenu()
    sub = w.addSkipRegionMenu(m, sel, w.ui.documentView.mapFromScene(sel.mapToScene(sel.rect.center().x(), sel.rect.top())))
    texts = [a.text() for a in sub.actions() if a.text()]
    assert texts[0] == "Top edge (nearest)"
    for t in ("Bottom edge", "Left edge", "Right edge", "All four edges",
              "This page only", "This page to end", "All pages"):
        assert t in texts, texts


def test_page_only_splits_and_skips(window, scan):
    w = window
    sel = setup(w, scan)
    w.skipRegion(sel, ["top"], "page")
    assert w.selections.selectionMode == ViewerSelections.individual
    assert abs(frac(w, sel)[1] - 120 / H) < 0.012
    # other pages keep the full-page copy
    w.viewer.currentPageIndex = 2
    s2 = [s for s in w.selections.items if s.selectionVisibleOnPage(2)]
    assert len(s2) == 1 and frac(w, s2[0])[1] < 0.01


def test_all_pages_bottom_and_undo(window, scan):
    w = window
    sel = setup(w, scan)
    w.skipRegion(sel, ["bottom"], "all")
    QApplication.instance().processEvents()
    for i in range(4):
        cv = w.viewer.cropValues(i)
        assert len(cv) == 1
        assert abs((1 - cv[0][3]) - 640 / H) < 0.012, (i, cv)
    log = open(w.logPath).read()
    assert "SKIP" in log and "page 4 bottom" in log and "ERROR" not in log
    w.slotUndoSkipRegion()
    assert w.selections.selectionMode == ViewerSelections.all
    assert len(w.selections.items) == 1
    assert w.viewer.cropValues(3)[0][3] < 0.01


def test_to_end_and_all_four(window, scan):
    w = window
    sel = setup(w, scan)
    w.viewer.currentPageIndex = 2
    w.selections.autoSetCurrentSelection()
    sel = w.selections.currentSelection
    w.skipRegion(sel, ["top", "bottom", "left", "right"], "toend")
    assert w.viewer.cropValues(0)[0][1] < 0.01  # page 1 untouched
    for i in (2, 3):
        l, t, r, b = w.viewer.cropValues(i)[0]
        assert abs(t - 120 / H) < 0.012 and abs((1 - b) - 640 / H) < 0.012
    assert "no further ink" in open(w.logPath).read()   # left/right: one column band
    assert w.viewer.currentPageIndex == 2
