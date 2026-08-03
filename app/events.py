"""In-process pub/sub for live scrape logs.

The previous design had a single module-level queue shared by every request,
so a second viewer stole the first viewer's lines and a second run replayed the
first run's backlog. This is per-run, fan-out to N subscribers, with a bounded
replay buffer so a client that connects late still sees what it missed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from app.utils import utcnow

logger = logging.getLogger(__name__)

MAX_BUFFER = 800
QUEUE_MAXSIZE = 1000


class RunBroker:
    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue]] = {}
        self._buffers: dict[int, deque[dict[str, Any]]] = {}
        self._finished: set[int] = set()

    # ------------------------------------------------------------- publishing
    async def publish(self, run_id: int, event: dict[str, Any]) -> None:
        event = {"ts": utcnow().isoformat(), **event}
        self._buffers.setdefault(run_id, deque(maxlen=MAX_BUFFER)).append(event)

        for queue in list(self._subscribers.get(run_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled reader must never block the scraper.
                logger.debug("Dropping event for slow subscriber on run %s", run_id)

    async def log(self, run_id: int, message: str, level: str = "info") -> None:
        await self.publish(run_id, {"type": "log", "level": level, "message": message})

    async def progress(self, run_id: int, done: int, total: int) -> None:
        percent = round(min(done / total, 1.0) * 100, 1) if total else 0.0
        await self.publish(
            run_id, {"type": "progress", "done": done, "total": total, "percent": percent}
        )

    async def finish(self, run_id: int, status: str, stats: dict[str, Any]) -> None:
        await self.publish(run_id, {"type": "done", "status": status, "stats": stats})
        self._finished.add(run_id)

    # ------------------------------------------------------------ subscribing
    def subscribe(self, run_id: int) -> tuple[asyncio.Queue, list[dict[str, Any]]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers.setdefault(run_id, set()).add(queue)
        return queue, list(self._buffers.get(run_id, ()))

    def unsubscribe(self, run_id: int, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(run_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(run_id, None)

    def is_finished(self, run_id: int) -> bool:
        return run_id in self._finished

    def forget(self, run_id: int) -> None:
        self._buffers.pop(run_id, None)
        self._subscribers.pop(run_id, None)
        self._finished.discard(run_id)

    def prune(self, keep_last: int = 10) -> None:
        """Drop buffers for old finished runs so memory stays flat."""
        finished = sorted(self._finished)
        for run_id in finished[:-keep_last]:
            self.forget(run_id)


broker = RunBroker()
