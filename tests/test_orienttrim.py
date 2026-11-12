"""Keep a trim in the page's orientation, and the re-detect label."""
import os
import stat

import numpy as np
import pymupdf

from krop.control import start_control
from krop.ctl import build_args
from krop.orienttrim import keep_page_orientation
from tests.test_control import _call


def test_portrait_grows_into_the_larger_blank_side():
    # Page 100 x 200. Content is a wide short band near the top.
    full = (0, 0, 100, 200)
    content = (10, 15, 90, 45)  # 80 wide, 30 tall
    crop = content
    box, adjusted = keep_page_orientation(crop, content, full)
    assert adjusted
    left, top, right, bottom = box
    assert abs((right - left) - (bottom - top)) < 1e-6 or (bottom - top) >= (right - left)
    assert bottom - top + 1e-6 >= right - left
    # More blank below the content, so the top stays put and the bottom grows.
    assert abs(top - 15) < 1e-6
    assert bottom > 45


def test_portrait_overflows_onto_the_other_side():
    # Equal blank above and below, and the preferred side cannot hold it all.
    full = (0, 0, 100, 130)
    content = (5, 50, 95, 80)  # width 90, height 30; need +60
    box, adjusted = keep_page_orientation(content, content, full)
    assert adjusted
    left, top, right, bottom = box
    assert bottom - top + 1e-6 >= right - left
    # Prefer bottom (tie). It only has 50px, so 10px comes off the top.
    assert abs(bottom - 130) < 1e-6
    assert abs(top - 40) < 1e-6
    assert left == 5 and right == 95


def test_landscape_grows_into_the_larger_blank_side():
    full = (0, 0, 200, 100)
    content = (20, 10, 50, 90)  # width 30, height 80; more blank on the right
    box, adjusted = keep_page_orientation(content, content, full)
    assert adjusted
    left, top, right, bottom = box
    assert right - left + 1e-6 >= bottom - top
    assert abs(left - 20) < 1e-6
    assert right > 50
    assert top == 10 and bottom == 90


def test_landscape_overflows_onto_the_other_side():
    full = (0, 0, 130, 100)
    content = (50, 5, 80, 95)  # width 30, height 90; need +60, 50px each side
    box, adjusted = keep_page_orientation(content, content, full)
    assert adjusted
    left, top, right, bottom = box
    assert right - left + 1e-6 >= bottom - top
    assert abs(right - 130) < 1e-6
    assert abs(left - 40) < 1e-6


def test_already_matching_orientation_is_left_alone():
    full = (0, 0, 100, 200)
    content = (10, 20, 40, 160)
    assert keep_page_orientation(content, content, full) == (content, False)


def _portrait_sheet(path):
    """Tall page with a wide, short checker band near the top."""
    width, height = 200, 400
    arr = np.full((height, width, 3), 255, np.uint8)
    arr[20:70, 20:180] = 0
    arr[20:70:2, 20:180:2] = 255
    doc = pymupdf.open()
    page = doc.new_page(width=width * 72 / 96.0, height=height * 72 / 96.0)
    pix = pymupdf.Pixmap(pymupdf.csRGB, width, height, np.ascontiguousarray(arr).tobytes(), False)
    page.insert_image(page.rect, pixmap=pix)
    doc.save(path)
    doc.close()


def test_label_and_maintain_orientation_on_a_real_trim(window, tmp_path, qapp, monkeypatch):
    assert window.checkRecrop.text() == "Re-detect margins (ignore remembered crops)"
    assert "turns itself off after the next trim" in window.checkRecrop.toolTip()
    assert window.checkMaintainOrientation.text() == "Maintain orientation"
    assert window.checkMaintainOrientation.isChecked() is False

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    os.makedirs(tmp_path / "run")
    path = str(tmp_path / "tall.pdf")
    _portrait_sheet(path)
    window.resize(900, 700)
    window.show()
    qapp.processEvents()
    window.openFile(path, False)
    qapp.processEvents()
    sock = str(tmp_path / "run" / ("kroppy-%d.sock" % os.getpid()))
    assert start_control(window, sock) is not None
    assert stat.S_ISSOCK(os.stat(sock).st_mode)
    window.checkCopierTrim.setChecked(False)
    window.ui.checkTrimUseAllPages.setChecked(False)
    window.selections.deleteSelections()
    window.slotNewSelection()

    assert build_args("set_maintain_orientation", ["1"]) == {"value": "1"}
    turned = _call(qapp, sock, "set_maintain_orientation", {"value": "1"})
    assert turned["maintain_orientation"] is True
    assert _call(qapp, sock, "state")["maintain_orientation"] is True

    try:
        window.ui.editPadding.setText("0")
        window._trim_detection_count = 0
        first = _call(qapp, sock, "trim", {"scope": "page"})
        assert first["pages"][0]["detection"] == "fresh"
        assert first["pages"][0]["orientation"] == "adjusted"
        assert window._trim_detection_count == 1
        grown = window.selections.currentSelection.mapRectToImage(
            window.selections.currentSelection.rect)
        assert grown.height() + 1 >= grown.width()
        # The open area is below the band, so the top stays near the content.
        assert grown.top() < 40
        assert grown.bottom() > 150

        window._trim_detection_count = 0
        remembered = _call(qapp, sock, "trim", {"scope": "page"})
        assert remembered["pages"][0]["detection"] == "remembered"
        assert remembered["pages"][0]["orientation"] == "adjusted"
        assert window._trim_detection_count == 0
        again = window.selections.currentSelection.mapRectToImage(
            window.selections.currentSelection.rect)
        assert abs(again.top() - grown.top()) < 2
        assert abs(again.height() - grown.height()) < 2

        _call(qapp, sock, "set_maintain_orientation", {"value": "0"})
        assert _call(qapp, sock, "state")["maintain_orientation"] is False
        window._trim_detection_count = 0
        plain = _call(qapp, sock, "trim", {"scope": "page"})
        assert plain["pages"][0]["detection"] == "remembered"
        assert plain["pages"][0]["orientation"] == "unchanged"
        assert window._trim_detection_count == 0
        short = window.selections.currentSelection.mapRectToImage(
            window.selections.currentSelection.rect)
        assert short.height() < short.width()
    finally:
        window.ui.editPadding.setText("2")
        window.checkMaintainOrientation.setChecked(False)
        window.checkCopierTrim.setChecked(True)
