"""Chosen targets jump the work queue, and the page shows a busy preview."""
import time

import numpy as np

from krop.jobqueue import JobQueue


def test_selected_target_moves_ahead_of_measurements_and_pages():
    queue = JobQueue()
    queue.push(("page", 1), {"kind": "page"}, 20)
    queue.push(("measure", 9, "jpeg-95"), {"kind": "measure"}, 10)
    queue.push(("preview", 9, "jpeg-95"), {"kind": "preview"}, 10)
    queue.push(("measure", 9, "jpeg-60"), {"kind": "measure"}, 10)
    queue.promote(("measure", 9, "jpeg-45"), {"kind": "measure"}, 0)
    queue.promote(("preview", 9, "jpeg-45"), {"kind": "preview"}, 0)
    order = queue.order()
    assert order[0] == ("preview", 9, "jpeg-45")
    assert order[1] == ("measure", 9, "jpeg-45")
    assert order.index(("measure", 9, "jpeg-45")) < order.index(("measure", 9, "jpeg-95"))
    assert order[-1] == ("page", 1)
    # A lower-priority job already popped is put back when a selection arrives
    # before that work starts.
    held = JobQueue()
    held.push(("measure", 1, "jpeg-95"), {"kind": "measure"}, 10)
    low = held.pop()
    held.promote(("measure", 3, "jpeg-85"), {"kind": "measure"}, 0)
    assert held.has_better_than(low["priority"])
    held.requeue(low)
    assert held.pop()["key"] == ("measure", 3, "jpeg-85")


def test_panel_prioritize_queues_the_choice_first(window):
    panel = window.analysisPanel
    panel._work_stop.set()
    panel._wake.set()
    panel._worker.wait(2000)
    panel._jobs.clear()
    panel._path = "/tmp/does-not-need-to-exist.pdf"
    panel._jobs.push(("measure", 9, "jpeg-95"), {"kind": "measure", "path": None}, 10)
    panel._jobs.push(("page", 2), {"kind": "page"}, 20)
    panel.prioritize(9, "jpeg-45", token=4)
    order = panel.job_order()
    assert ("measure", 9, "jpeg-45") in order[:2]
    assert ("preview", 9, "jpeg-45", 0) in order[:2]
    assert order.index(("measure", 9, "jpeg-45")) < order.index(("measure", 9, "jpeg-95"))
    assert order[-1] == ("page", 2)


def test_page_walk_waits_while_the_selected_job_is_queued(window):
    panel = window.analysisPanel
    panel._work_stop.set()
    panel._wake.set()
    panel._worker.wait(2000)
    panel._jobs.clear()
    panel._jobs.push(("measure", 4, "jpeg-45"), {"kind": "measure"}, 0)

    def release():
        time.sleep(0.15)
        panel._jobs.pop()
        panel._jobs.set_running(None)

    import threading
    threading.Thread(target=release).start()
    started = time.time()
    panel.yield_to_urgent()
    assert time.time() - started >= 0.1
    assert panel.job_order() == []


def test_busy_overlay_tracks_the_newest_preview_token(window, qapp):
    from krop.qt import Qt
    viewer = window.viewer
    rects = [(12, 14, 40, 24, True), (70, 14, 30, 18, False)]
    viewer.showBusy(5, 2, rects)
    qapp.processEvents()
    assert viewer.busyVisible()
    assert viewer.busyRects()[0][:4] == (12, 14, 40, 24)
    item = viewer.busyItem()
    assert item is not None and item.isVisible()
    assert item.zValue() > 1000
    try:
        wait = Qt.CursorShape.WaitCursor
    except AttributeError:
        wait = Qt.WaitCursor
    assert item.cursor().shape() == wait
    rgba = np.zeros((4, 4, 4), np.uint8)
    rgba[:, :, 3] = 255
    # An older encode finishes after the user has already picked again.
    assert viewer.deliverPreview({
        "xref": 5, "target": "jpeg-45", "rotation": 0, "token": 1, "rgba": rgba,
    }) is False
    assert viewer.busyVisible()
    assert viewer.previewImage(5, "jpeg-85", 0) is None
    assert viewer.deliverPreview({
        "xref": 5, "target": "jpeg-85", "rotation": 0, "token": 2, "rgba": rgba,
    }) is True
    assert not viewer.busyVisible()
    assert viewer.previewImage(5, "jpeg-85", 0) is not None
    # keep is instant: the overlay does not stay up.
    viewer.showBusy(5, 3, rects)
    assert viewer.busyVisible()
    viewer.clearBusy()
    assert not viewer.busyVisible()
