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


def _assert_padding_delta(window, tight, loose, pixels):
    """loose used the larger padding, so each margin is smaller by that many pixels."""
    image = window.viewer.getImage(0)
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
        _assert_padding_delta(window, tight, loose, pixels)
