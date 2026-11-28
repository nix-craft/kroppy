"""Trim reads the Padding field at the moment it runs."""
import os
import stat

import numpy as np
import pymupdf
import pytest

from krop.control import start_control
from krop.ctl import build_args
from krop.padding import parse_padding
from tests.test_control import _call


def test_padding_only_retrim_skips_detection_and_matches_fresh(window, tmp_path, qapp, monkeypatch):
    """Padding 30, 0, 30 from the remembered box matches a fresh search."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    os.makedirs(tmp_path / "run")
    path = str(tmp_path / "sheet.pdf")
    _sheet(path)
    window.resize(900, 700)
    window.show()
    qapp.processEvents()
    window.openFile(path, False)
    qapp.processEvents()
    sock = str(tmp_path / "run" / ("kroppy-%d.sock" % os.getpid()))
    assert start_control(window, sock) is not None
    window.checkCopierTrim.setChecked(False)
    window.ui.checkTrimUseAllPages.setChecked(False)
    _prepare(window, individual=False)

    def run(padding, fresh=""):
        _call(qapp, sock, "set_padding", {"value": str(padding)})
        args = {"scope": "page"}
        if fresh:
            args["fresh"] = fresh
        return _call(qapp, sock, "trim", args)

    try:
        window._trim_detection_count = 0
        first = run(30)
        assert first["pages"][0]["detection"] == "fresh"
        assert window._trim_detection_count == 1
        assert first["elapsed"] >= 0
        crops_30 = _margins(window)

        window._trim_detection_count = 0
        zero = run(0)
        assert zero["pages"][0]["detection"] == "remembered"
        assert window._trim_detection_count == 0
        _assert_padding_delta(window.viewer.getImage(0), _margins(window), crops_30, 30)

        window._trim_detection_count = 0
        again = run(30)
        assert again["pages"][0]["detection"] == "remembered"
        assert window._trim_detection_count == 0
        back = _margins(window)
        for edge in range(4):
            assert abs(back[edge] - crops_30[edge]) < 0.004

        window._trim_detection_count = 0
        fresh = run(30, "fresh")
        assert fresh["fresh"] is True
        assert fresh["pages"][0]["detection"] == "fresh"
        assert window._trim_detection_count == 1
        for edge in range(4):
            assert abs(_margins(window)[edge] - crops_30[edge]) < 0.004

        # A hand edit forgets the content box, so the next trim searches again.
        window.selections.currentSelection.adjustBoundingRect(6, 0, 6, 0)
        assert window.selections.currentSelection._trim_memory is None
        window._trim_detection_count = 0
        edited = run(30)
        assert edited["pages"][0]["detection"] == "fresh"
        assert window._trim_detection_count == 1

        assert build_args("trim", ["every", "--fresh"]) == {"scope": "every", "fresh": "--fresh"}
        assert build_args("recrop", ["every"]) == {"scope": "every"}
        recrop = _call(qapp, sock, "recrop", {"scope": "page"})
        assert recrop["fresh"] is True
        assert recrop["pages"][0]["detection"] == "fresh"
    finally:
        window.ui.editPadding.setText("2")
        window.checkCopierTrim.setChecked(True)
        window.checkRecrop.setChecked(False)


def test_parse_padding_is_css_and_has_no_memory():
    assert parse_padding("") == [0, 0, 0, 0]
    assert parse_padding("4") == [4, 4, 4, 4]
    assert parse_padding("3, 5") == [3, 5, 3, 5]
    assert parse_padding("1,2,3") == [1, 2, 3, 2]
    assert parse_padding("1,2,3,4") == [1, 2, 3, 4]
    with pytest.raises(ValueError):
        parse_padding("1,2,3,4,5")
    with pytest.raises(ValueError):
        parse_padding("wide")


def _sheet(path):
    """White page with one black block, sized so 96 dpi is one pixel per sample."""
    width, height = 240, 320
    arr = np.full((height, width, 3), 255, np.uint8)
    # Checker, not a solid block: plain trim treats a uniform row as blank.
    arr[50:250, 40:200] = 0
    arr[50:250:2, 40:200:2] = 255
    doc = pymupdf.open()
    for _ in range(2):
        page = doc.new_page(width=width * 72 / 96.0, height=height * 72 / 96.0)
        pix = pymupdf.Pixmap(pymupdf.csRGB, width, height, np.ascontiguousarray(arr).tobytes(), False)
        page.insert_image(page.rect, pixmap=pix)
    doc.save(path)
    doc.close()


def _prepare(window, individual=False):
    window.selections.deleteSelections()
    window.checkCopierTrim.setChecked(False)
    window.ui.checkTrimUseAllPages.setChecked(False)
    if individual:
        window.ui.radioSelIndividual.setChecked(True)
    else:
        window.ui.radioSelAll.setChecked(True)
    window.slotNewSelection()


def _margins(window):
    crops = window.viewer.cropValues(0)
    assert len(crops) == 1, crops
    return crops[0]


def _assert_padding_delta(image, tight, loose, pixels):
    """loose used the larger padding, so each margin is smaller by that many pixels."""
    sizes = (image.width(), image.height(), image.width(), image.height())
    for index, size in enumerate(sizes):
        got = (tight[index] - loose[index]) * size
        assert abs(got - pixels) <= 3, (index, got, pixels, tight, loose)


def test_each_trim_reads_padding_changed_between_runs(window, tmp_path, qapp, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    os.makedirs(tmp_path / "run")
    path = str(tmp_path / "sheet.pdf")
    _sheet(path)
    window.resize(900, 700)
    window.show()
    qapp.processEvents()
    window.openFile(path, False)
    qapp.processEvents()
    sock = str(tmp_path / "run" / ("kroppy-%d.sock" % os.getpid()))
    server = start_control(window, sock)
    assert server is not None
    assert stat.S_ISSOCK(os.stat(sock).st_mode)

    assert build_args("set_padding", ["12"]) == {"value": "12"}
    assert build_args("trim", ["page"]) == {"scope": "page"}
    assert build_args("trim_copier", ["current"]) == {"scope": "current"}

    # (command, scope, needs individual selections)
    actions = (
        ("trim", "current", False),
        ("trim", "page", False),
        ("trim", "every", True),
        ("trim_copier", "current", False),
        ("trim_copier", "page", False),
        ("trim_copier", "every", True),
    )
    pixels = 16
    try:
        _run_actions(window, qapp, sock, actions, pixels)
    finally:
        # close() writes QSettings. Leave the default the other tests assume.
        window.ui.editPadding.setText("2")


def _run_actions(window, qapp, sock, actions, pixels):
    for cmd, scope, individual in actions:
        _prepare(window, individual=individual)
        window.ui.editPadding.setText("0")
        first = _call(qapp, sock, cmd, {"scope": scope})
        assert first["padding"]["top"] == 0, first
        tight = _margins(window)

        _prepare(window, individual=individual)
        second = _call(qapp, sock, "set_padding", {"value": str(pixels)})
        assert second["text"] == str(pixels)
        assert _call(qapp, sock, "state")["padding"]["left"] == pixels
        again = _call(qapp, sock, cmd, {"scope": scope})
        assert again["padding"]["left"] == pixels
        assert again["copier"] is cmd.startswith("trim_copier")
        loose = _margins(window)
        _assert_padding_delta(window.viewer.getImage(0), tight, loose, pixels)


def _with_existing_crop(path):
    """Same sheet, but CropBox is inset and /KropFullBox keeps the full page."""
    _sheet(path)
    doc = pymupdf.open(path)
    for page in doc:
        box = page.mediabox
        doc.xref_set_key(page.xref, "KropFullBox", "[%g %g %g %g]" % (box.x0, box.y0, box.x1, box.y1))
        page.set_cropbox(pymupdf.Rect(box.x0 + 18, box.y0 + 18, box.x1 - 18, box.y1 - 18))
    tmp = path + ".tmp"
    doc.save(tmp)
    doc.close()
    os.replace(tmp, path)


def test_retrim_without_reset_on_a_file_with_crops(window, tmp_path, qapp, monkeypatch):
    """Nick's sequence: trim, change padding, trim again. Do not rebuild the selection."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    os.makedirs(tmp_path / "run")
    path = str(tmp_path / "cropped.pdf")
    _with_existing_crop(path)
    window.resize(900, 700)
    window.show()
    qapp.processEvents()
    window.openFile(path, True)
    qapp.processEvents()
    assert window.selections.items, "crop recovery should restore a selection per page"
    recovered = [window.viewer.cropValues(i) for i in range(window.viewer.numPages())]
    assert recovered[0][0][0] > 0.02

    sock = str(tmp_path / "run" / ("kroppy-%d.sock" % os.getpid()))
    server = start_control(window, sock)
    assert server is not None
    window.checkCopierTrim.setChecked(False)

    def run(cmd, scope, padding):
        _call(qapp, sock, "set_padding", {"value": str(padding)})
        result = _call(qapp, sock, cmd, {"scope": scope})
        assert result["padding"]["top"] == padding
        crops = []
        for index in range(window.viewer.numPages()):
            boxes = window.viewer.cropValues(index)
            crops.append(boxes[0] if boxes else None)
        return crops

    actions = (
        ("trim", "every"),
        ("trim", "page"),
        ("trim", "current"),
        ("trim_copier", "every"),
        ("trim_copier", "page"),
        ("trim_copier", "current"),
    )
    pixels = 30
    try:
        for cmd, scope in actions:
            window.openFile(path, True)
            qapp.processEvents()
            window.checkCopierTrim.setChecked(False)
            wide = run(cmd, scope, pixels)
            tight = run(cmd, scope, 0)
            again = run(cmd, scope, pixels)
            # page/current trim the page on screen. every trims each page.
            indexes = range(window.viewer.numPages()) if scope == "every" else (0,)
            for index in indexes:
                assert None not in (tight[index], wide[index], again[index]), (cmd, scope, index)
                _assert_padding_delta(window.viewer.getImage(index), tight[index], wide[index], pixels)
                for edge in range(4):
                    assert abs(wide[index][edge] - again[index][edge]) < 0.004, (
                        cmd, scope, index, wide[index], again[index])
        # No box yet: trim current and trim page still trim this page.
        window.openFile(path, False)
        qapp.processEvents()
        window.selections.deleteSelections()
        window.checkCopierTrim.setChecked(False)
        assert window.selections.currentSelection is None
        made = run("trim", "current", 0)
        assert made[0][0] > 0.05
        window.selections.deleteSelections()
        made = run("trim", "page", pixels)
        assert made[0][0] < 0.1
    finally:
        window.ui.editPadding.setText("2")
        window.checkCopierTrim.setChecked(True)
