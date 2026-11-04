"""Headless end-to-end: open, trim every page, save, check boxes, reopen."""
import pymupdf, pytest
from krop.qt import QApplication

# content of tests/data/scanlike.pdf as fractions (left, top, right, bottom)
# see tests/make_fixture.py: block 110..500 x 120..640 pt, page number ~688..702 pt
EXPECT = (110 / 612, 120 / 792, 1 - 500 / 612, 1 - 702 / 792)
TOL = 0.012


def trimmed(w):
    w.ui.radioSelIndividual.setChecked(True)
    w.slotNewSelection()
    w.actionTrimMarginsEveryPage.trigger()
    QApplication.instance().processEvents()
    return [w.viewer.cropValues(i) for i in range(w.viewer.numPages())]


def no_errors(w):
    QApplication.instance().processEvents()
    txt = open(w.logPath).read()
    assert "ERROR" not in txt, txt
    return txt


def test_trim_save_reopen(window, scan, tmp_path, qapp):
    w = window
    w.openFile(scan)
    assert w.viewer.numPages() == 4
    crops = trimmed(w)
    for i, cv in enumerate(crops):
        assert len(cv) == 1, (i, cv)
        for got, exp in zip(cv[0], EXPECT):
            assert abs(got - exp) < TOL, (i + 1, cv[0], EXPECT)

    out = str(tmp_path / "out.pdf")
    w.ui.editFile.setText(out)
    w.ui.checkGhostscript.setChecked(False)
    w.slotKrop()

    doc = pymupdf.open(out)
    assert doc.page_count == 4
    for p in doc:
        obj = doc.xref_object(p.xref)
        mb = doc.xref_get_key(p.xref, "MediaBox")[1]
        cb = doc.xref_get_key(p.xref, "CropBox")[1]
        assert cb in ("null", mb), (p.number, mb, cb)   # MediaBox shrunk to the crop
        assert "/KropFullBox" in obj, obj
        assert p.mediabox.width < 612 * 0.8
    doc.close()
    log = no_errors(w)
    assert "trimMargins" in log or "TrimMargins" in log

    # reopen in a fresh window (as when krop is relaunched): crops recovered
    from conftest import new_window
    w.close()
    w = new_window(str(tmp_path / "activity2.log"))
    w.openFile(out)
    qapp.processEvents()
    assert w.viewer.numPages() == 4
    again = [w.viewer.cropValues(i) for i in range(4)]
    for a, b in zip(crops, again):
        assert len(b) == 1
        for x, y in zip(a[0], b[0]):
            assert abs(x - y) < 0.003, (a, b)
    no_errors(w)
    w.close()


def test_no_recover(window, scan, tmp_path, qapp):
    w = window
    w.openFile(scan)
    trimmed(w)
    out = str(tmp_path / "out.pdf")
    w.ui.editFile.setText(out)
    w.ui.checkGhostscript.setChecked(False)
    w.slotKrop()
    w.openFile(out, False)
    qapp.processEvents()
    # opened as-is: no recovered selections spanning less than the page
    cv = w.viewer.cropValues(0)
    assert all(max(c) < 0.01 for c in cv) or cv == [], cv


def test_reopen_same_window(window, scan, tmp_path, qapp):
    """Reopening the saved file in the window that cropped it must not
    keep the old selections alongside the recovered ones."""
    w = window
    w.openFile(scan)
    crops = trimmed(w)
    out = str(tmp_path / "out.pdf")
    w.ui.editFile.setText(out)
    w.ui.checkGhostscript.setChecked(False)
    w.slotKrop()
    w.openFile(out)
    qapp.processEvents()
    for i in range(4):
        cv = w.viewer.cropValues(i)
        assert len(cv) == 1, (i + 1, cv)
        for x, y in zip(crops[i][0], cv[0]):
            assert abs(x - y) < 0.003
    assert len(w.selections.items) == 4
    no_errors(w)
