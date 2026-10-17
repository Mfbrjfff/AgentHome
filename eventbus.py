"""
Event bus — the nervous system of the harness.

Every tool emits state-change events here; main.py subscribes and fans them
out to all connected WebSocket clients, which drives the "Liquid Glass"
dynamic glow effects on the dashboard (amber for Hue, blue for Roidmi,
green for Withings syncs, …).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Awaitable, Callable

# Canonical glow categories understood by the frontend CSS engine
GLOW_KINDS = {"hue", "spotify", "roidmi", "withings", "weather", "agent", "ha", "system"}


class EventBus:
    def __init__(self, history_size: int = 500) -> None:
        self._subscribers: list[Callable[[dict], Awaitable[None]]] = []
        self.history: deque[dict] = deque(maxlen=history_size)
        self.device_state: dict[str, Any] = {}   # latest known device snapshot per domain

    def subscribe(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[dict], Awaitable[None]]) -> None:
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    async def emit(
        self,
        kind: str,
        event: str,
        payload: dict | None = None,
        log_line: str | None = None,
        glow: bool = True,
    ) -> None:
        """Publish a structured event + optional human-readable agent-log line."""
        msg: dict[str, Any] = {
            "type": "event",
            "kind": kind,                     # integration domain (hue/roidmi/...)
            "event": event,                   # e.g. "lights_on", "cleaning_started"
            "payload": payload or {},
            "glow": glow and kind in GLOW_KINDS,
            "ts": time.time(),
        }
        self.device_state[kind] = {**(self.device_state.get(kind, {})), **(payload or {})}
        self.history.append(msg)
        if log_line:
            await self.emit_log(log_line, kind=kind)
        for cb in list(self._subscribers):
            try:
                await cb(msg)
            except Exception:  # a broken subscriber must never break tools
                pass

    async def emit_log(self, text: str, level: str = "info", kind: str = "agent") -> None:
        """Stream a line into the dashboard's 'Agent Thought Process' terminal."""
        msg = {"type": "log", "level": level, "kind": kind, "text": text, "ts": time.time()}
        self.history.append(msg)
        for cb in list(self._subscribers):
            try:
                await cb(msg)
            except Exception:
                pass

    def snapshot(self) -> dict[str, Any]:
        return {"device_state": self.device_state, "recent": list(self.history)[-50:]}


bus = EventBus()
