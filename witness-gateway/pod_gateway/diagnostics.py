"""Bounded JSON events on stdout without blocking gateway requests on log output."""

from __future__ import annotations

import json
import sys
import time
from contextlib import suppress
from queue import Full, Queue
from threading import Lock, Thread
from typing import TextIO


class EventOutput:
    def __init__(self, *, capacity: int = 1024) -> None:
        self.queue: Queue[tuple[TextIO, str]] = Queue(maxsize=capacity)
        self.lock = Lock()
        self.thread: Thread | None = None
        self.dropped = 0

    def write(self, event: str, **fields: object) -> None:
        with self.lock:
            if self.thread is None:
                self.thread = Thread(target=self._run, name="gateway-events", daemon=True)
                self.thread.start()
            record = {"event": event, "timestamp": time.time(), **fields}
            if self.dropped:
                record["dropped_logs"] = self.dropped
            try:
                self.queue.put_nowait((sys.stdout, json.dumps(record, separators=(",", ":"))))
            except Full:
                self.dropped += 1
            else:
                self.dropped = 0

    def _run(self) -> None:
        while True:
            stream, line = self.queue.get()
            try:
                with suppress(Exception):
                    stream.write(line + "\n")
                    stream.flush()
            finally:
                self.queue.task_done()


output = EventOutput()
