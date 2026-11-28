# -*- coding: utf-8 -*-
"""Priority queue for preview and measurement work.

A smaller priority number runs first. Insertion order breaks ties. Promoting
a job moves it ahead of every other queued job, including ones already marked
urgent, so the target the user just picked starts next.
"""
import threading


class JobQueue(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}
        self._seq = 0
        self._front = 0
        self.running_priority = None

    def push(self, key, payload, priority=10):
        """Add a job. If the key is already queued, keep the better priority."""
        with self._lock:
            self._seq += 1
            current = self._items.get(key)
            if current is not None:
                if priority < current["priority"]:
                    current["priority"] = priority
                    current["payload"] = payload
                return False
            self._items[key] = {
                "key": key, "payload": payload,
                "priority": int(priority), "order": self._seq,
            }
            return True

    def promote(self, key, payload, priority=0):
        """Insert or move this job to the front of its priority band.

        A newly promoted job is ahead of jobs promoted earlier.
        """
        with self._lock:
            self._front -= 1
            current = self._items.get(key)
            if current is None:
                self._items[key] = {
                    "key": key, "payload": payload,
                    "priority": int(priority), "order": self._front,
                }
            else:
                current["priority"] = int(priority)
                current["order"] = self._front
                current["payload"] = payload
            return True

    def requeue(self, job):
        """Put a popped job back, ahead of equal-priority work not yet popped."""
        if not job:
            return
        with self._lock:
            self._front -= 1
            job = dict(job)
            job["order"] = self._front
            self._items[job["key"]] = job

    def pop(self):
        with self._lock:
            if not self._items:
                return None
            key = min(self._items, key=lambda k: (self._items[k]["priority"], self._items[k]["order"]))
            return self._items.pop(key)

    def has_better_than(self, priority):
        with self._lock:
            return any(item["priority"] < priority for item in self._items.values())

    def has_priority(self, priority):
        """True when queued or running work is at least this urgent."""
        with self._lock:
            running = self.running_priority
            if running is not None and running <= priority:
                return True
            return any(item["priority"] <= priority for item in self._items.values())

    def set_running(self, priority):
        with self._lock:
            self.running_priority = priority

    def order(self):
        with self._lock:
            keys = sorted(self._items, key=lambda k: (self._items[k]["priority"], self._items[k]["order"]))
            return [self._items[k]["key"] for k in keys]

    def clear(self):
        with self._lock:
            self._items.clear()
            self.running_priority = None
