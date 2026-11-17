"""In-process pub/sub feeding the SSE streams. `publish` is safe to call from worker threads."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import defaultdict
from typing import Any, AsyncIterator


class EventBus:
    def __init__(self, loop: asyncio.AbstractEventLoop | None = None):
        self._loop = loop
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, topic: str, event: str, data: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            queues = list(self._subs.get(topic, ()))
        if not queues:
            return
        msg = (event, data)
        for q in queues:
            try:
                loop.call_soon_threadsafe(self._put, q, msg)
            except RuntimeError:  # loop closed during shutdown
                return

    @staticmethod
    def _put(q: asyncio.Queue, msg: tuple[str, dict]) -> None:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass  # slow consumer: drop (progress events are periodic anyway)

    async def subscribe(self, topic: str) -> AsyncIterator[tuple[str, dict]]:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        with self._lock:
            self._subs[topic].add(q)
        try:
            while True:
                yield await q.get()
        finally:
            with self._lock:
                self._subs[topic].discard(q)

    def subscriber_count(self, topic: str) -> int:
        with self._lock:
            return len(self._subs.get(topic, ()))


def sse(event: str, data: Any) -> dict:
    """Format one Server-Sent Event for sse-starlette."""
    return {"event": event, "data": json.dumps(data, ensure_ascii=False)}
