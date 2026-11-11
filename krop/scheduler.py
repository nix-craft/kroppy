# -*- coding: utf-8 -*-
"""Live priority for analysis, measurement, and preview work.

Tiers are computed when a job is taken, from the page on screen and the
target the user just chose. Nothing is lined up for every page in advance.

P0  the chosen (image, target) measurement and preview
P1  work for the page on screen that produces its default
P2  the rest of that page's ladder, and only after a short idle
P3  other pages, nearest first, and only when P0-P2 are empty and idle
"""
import threading
import time

P0, P1, P2, P3 = 0, 1, 2, 3


def _now():
    return time.monotonic()


class Scheduler(object):
    def __init__(self, idle_delay=1.5, background=True, workers=1):
        self.idle_delay = float(idle_delay)
        self.background = bool(background)
        # At most workers-1 slots may sit on other pages, so one slot can
        # take a P0 job. With a single worker that slot does P3 only when
        # nothing more urgent exists.
        self.workers = max(1, int(workers))
        self.max_p3 = max(0, self.workers - 1) if self.workers > 1 else 1
        self.focus = 0
        self.urgent = None
        self.page_count = 0
        self.idle_at = _now()
        self.jobs = {}
        self.scanned = {}
        self.queued_pages = set()
        self.running_tier = None
        self.running_page = None
        self.p3_running = 0
        self._seq = 0
        self._lock = threading.Lock()

    def configure(self, page_count, focus):
        with self._lock:
            self.page_count = int(page_count or 0)
            self.focus = self._clamp(focus)
            self.jobs.clear()
            self.scanned.clear()
            self.queued_pages.clear()
            self.urgent = None
            self.idle_at = _now()
            self.running_tier = None
            self.running_page = None
            self.p3_running = 0

    def _clamp(self, index):
        index = int(index or 0)
        if self.page_count <= 0:
            return 0
        if index < 0:
            return 0
        if index >= self.page_count:
            return self.page_count - 1
        return index

    def touch(self):
        """A click, a choice, or a page change. Other pages wait again."""
        with self._lock:
            self.idle_at = _now()

    def set_focus(self, index):
        with self._lock:
            self.focus = self._clamp(index)
            self.idle_at = _now()

    def set_urgent(self, xref, target):
        with self._lock:
            if xref is None or not target or target == "keep":
                self.urgent = None
            else:
                self.urgent = (int(xref), str(target))
            self.idle_at = _now()

    def set_background(self, enabled):
        with self._lock:
            self.background = bool(enabled)

    def add(self, key, payload):
        with self._lock:
            self._seq += 1
            item = dict(payload or {})
            item["_seq"] = self._seq
            item["_key"] = key
            self.jobs[key] = item
            if item.get("kind") == "page":
                self.queued_pages.add(item.get("page"))

    def mark_scanned(self, index, mode):
        with self._lock:
            self.scanned[int(index)] = mode
            self.queued_pages.discard(int(index))

    def note_not_queued(self, index):
        with self._lock:
            self.queued_pages.discard(int(index))

    def _tier_unlocked(self, job, now):
        kind = job.get("kind")
        page = job.get("page")
        urgent = self.urgent
        if (kind in ("measure", "preview") and urgent
                and job.get("xref") == urgent[0] and str(job.get("target")) == urgent[1]):
            return P0
        if page == self.focus:
            if job.get("lane") == "extra":
                if now - self.idle_at < self.idle_delay:
                    return None
                return P2
            return P1
        if not self.background:
            return None
        if now - self.idle_at < self.idle_delay:
            return None
        return P3

    def tier(self, job, now=None):
        now = _now() if now is None else now
        with self._lock:
            return self._tier_unlocked(job, now)

    def _distance(self, page):
        if page is None:
            return 0
        return abs(int(page) - int(self.focus))

    def pop(self, now=None):
        """Take the best job under the tiers as they stand right now."""
        now = _now() if now is None else now
        with self._lock:
            best_rank = None
            best_key = None
            p3_busy = self.p3_running >= self.max_p3 and self.running_tier == P3
            for key, job in self.jobs.items():
                tier = self._tier_unlocked(job, now)
                if tier is None:
                    continue
                if tier == P3 and self.p3_running >= self.max_p3 and self.workers > 1:
                    continue
                rank = (tier, self._distance(job.get("page")), job.get("_seq", 0))
                if best_rank is None or rank < best_rank:
                    best_rank = rank
                    best_key = key
            if best_key is None:
                return None
            job = self.jobs.pop(best_key)
            job["_tier"] = best_rank[0]
            return job

    def should_pause(self, job, now=None):
        """True when something more urgent appeared while this job was running."""
        now = _now() if now is None else now
        with self._lock:
            mine = self._tier_unlocked(job, now)
            if mine is None:
                # A page change or a fresh click made this work ineligible.
                return True
            best = None
            for other in self.jobs.values():
                tier = self._tier_unlocked(other, now)
                if tier is None:
                    continue
                if best is None or tier < best:
                    best = tier
            if best is None:
                return False
            return best < mine

    def _next_unlocked(self, now):
        from krop.analyze import page_visit_order
        focus = self.focus
        if self.page_count <= 0:
            return None
        if self.scanned.get(focus) != "full" and focus not in self.queued_pages:
            return (focus, "full")
        if any(self._tier_unlocked(job, now) in (P0, P1, P2) for job in self.jobs.values()):
            return None
        if any(job.get("kind") == "page" and job.get("page") != focus for job in self.jobs.values()):
            return None
        if not self.background or now - self.idle_at < self.idle_delay:
            return None
        for index in page_visit_order(self.page_count, focus):
            if index == focus:
                continue
            if index in self.scanned or index in self.queued_pages:
                continue
            return (index, "cheap")
        return None

    def next_page_to_submit(self, now=None):
        """The single page to submit next, or None. Never the whole document."""
        now = _now() if now is None else now
        with self._lock:
            return self._next_unlocked(now)

    def claim_next_page(self, path, threshold, gen, now=None):
        """Submit at most one page job. Other pages stay unqueued."""
        now = _now() if now is None else now
        with self._lock:
            spec = self._next_unlocked(now)
            if spec is None:
                return None
            index, mode = spec
            self._seq += 1
            key = ("page", int(index))
            self.jobs[key] = {
                "kind": "page", "page": int(index), "cheap": mode == "cheap",
                "path": path, "threshold": threshold, "gen": gen,
                "_seq": self._seq, "_key": key,
            }
            self.queued_pages.add(int(index))
            return key

    def order(self, now=None):
        """Keys in the order pop would take them, without removing them."""
        now = _now() if now is None else now
        with self._lock:
            ranked = []
            for key, job in self.jobs.items():
                tier = self._tier_unlocked(job, now)
                if tier is None:
                    continue
                ranked.append((tier, self._distance(job.get("page")), job.get("_seq", 0), key))
            ranked.sort()
            return [item[3] for item in ranked]

    def has_priority(self, tier, now=None):
        now = _now() if now is None else now
        with self._lock:
            if self.running_tier is not None and self.running_tier <= tier:
                return True
            for job in self.jobs.values():
                got = self._tier_unlocked(job, now)
                if got is not None and got <= tier:
                    return True
            return False

    def set_running(self, tier, page):
        with self._lock:
            if self.running_tier == P3 and tier != P3:
                self.p3_running = max(0, self.p3_running - 1)
            self.running_tier = tier
            self.running_page = page
            if tier == P3:
                self.p3_running += 1

    def clear_running(self):
        with self._lock:
            if self.running_tier == P3:
                self.p3_running = max(0, self.p3_running - 1)
            self.running_tier = None
            self.running_page = None

    def unscanned_others(self):
        with self._lock:
            for index in range(self.page_count):
                if index == self.focus:
                    continue
                if index not in self.scanned:
                    return True
            return False

    def all_visited(self):
        with self._lock:
            return self.page_count > 0 and all(index in self.scanned for index in range(self.page_count))
