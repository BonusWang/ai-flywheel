"""In-process pub/sub broadcaster for real-time SSE workflow visualization.

The portal's ``/api/events`` stream subscribes here; a pipeline stage publishes a
run snapshot after every node boundary. Subscribers live on their own event loop,
so a publish from a worker thread is marshalled with ``call_soon_threadsafe`` and
a closed loop is dropped instead of breaking the publisher.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


class EventBroker:
    """Broadcast run events to every live SSE subscriber."""

    def __init__(self) -> None:
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]] = []

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._subscribers.append((loop, q))
        return loop, q

    def unsubscribe(self, entry: tuple[asyncio.AbstractEventLoop, asyncio.Queue[str]]) -> None:
        if entry in self._subscribers:
            self._subscribers.remove(entry)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        dead = []
        for loop, q in list(self._subscribers):
            if loop.is_closed():
                dead.append((loop, q))
                continue
            try:
                loop.call_soon_threadsafe(self._safe_put, q, payload)
            except RuntimeError:
                dead.append((loop, q))
        for item in dead:
            self.unsubscribe(item)

    @staticmethod
    def _safe_put(q: asyncio.Queue[str], payload: str) -> None:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass
