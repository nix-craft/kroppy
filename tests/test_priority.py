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


def test_p0_preempts_queued_other_pages():
    from krop.scheduler import Scheduler
    sched = Scheduler(idle_delay=0)
    sched.configure(6, 0)
    sched.idle_at = 0
    sched.add(("page", 4), {"kind": "page", "page": 4, "cheap": True})
    sched.add(("measure", 9, "jpeg-45"), {
        "kind": "measure", "page": 4, "xref": 9, "target": "jpeg-45", "lane": "extra",
    })
    sched.set_urgent(9, "jpeg-45")
    job = sched.pop(now=sched.idle_at)
    assert job["_key"] == ("measure", 9, "jpeg-45")
    assert job["_tier"] == 0
    # The other page was already running when the choice arrived.
    running = {"kind": "page", "page": 4, "cheap": True, "_key": ("page", 4)}
    sched.add(("measure", 9, "jpeg-45"), {
        "kind": "measure", "page": 0, "xref": 9, "target": "jpeg-45",
    })
    assert sched.should_pause(running, now=sched.idle_at)


def test_navigation_reprioritizes_and_does_not_prefill():
    from krop.scheduler import Scheduler
    sched = Scheduler(idle_delay=1.5)
    sched.configure(20, 0)
    now = sched.idle_at
    sched.add(("page", 0), {"kind": "page", "page": 0})
    sched.add(("page", 7), {"kind": "page", "page": 7, "cheap": True})
    sched.set_focus(7)
    now = sched.idle_at
    job = sched.pop(now=now)
    assert job["page"] == 7
    # The other page is no longer eligible immediately after the navigation.
    assert sched.order(now) == []
    # Opening a document submits the page on screen and nothing else.
    fresh = Scheduler(idle_delay=1.5)
    fresh.configure(20, 5)
    opened = fresh.idle_at
    assert fresh.next_page_to_submit(opened) == (5, "full")
    assert fresh.claim_next_page("/tmp/doc.pdf", 4.0, 1, opened) == ("page", 5)
    assert fresh.claim_next_page("/tmp/doc.pdf", 4.0, 1, opened) is None
    fresh.pop(opened)
    fresh.mark_scanned(5, "full")
    assert fresh.next_page_to_submit(opened) is None
    later = fresh.idle_at + 2
    nxt = fresh.next_page_to_submit(later)
    assert nxt == (6, "cheap")
    assert fresh.claim_next_page("/tmp/doc.pdf", 4.0, 1, later)[0] == "page"
    assert fresh.claim_next_page("/tmp/doc.pdf", 4.0, 1, later) is None


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
