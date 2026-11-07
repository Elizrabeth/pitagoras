"""WebSocket hub between the assistant and the visual character.

Outgoing (assistant -> character) messages are JSON objects with a "type".
Incoming (character -> assistant) messages are handled by registered handlers.
See README.md for the full protocol.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from websockets.asyncio.server import ServerConnection, broadcast, serve
from websockets.exceptions import ConnectionClosed

log = logging.getLogger("events")

# Events whose latest value is replayed to a character that connects later.
STICKY = {"state", "now_playing", "emotion", "hello", "timers"}


class EventHub:
    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.clients: set[ServerConnection] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self.sticky: dict[str, dict] = {}
        self._server = None

    def on(self, type_: str, fn: Callable[[dict], Awaitable[None]]) -> None:
        self.handlers[type_] = fn

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._server = await serve(self._handle, self.host, self.port)
        log.info("character events on ws://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, ws: ServerConnection) -> None:
        self.clients.add(ws)
        log.info("character connected (%d)", len(self.clients))
        try:
            for msg in self.sticky.values():
                await ws.send(json.dumps(msg))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                fn = self.handlers.get(msg.get("type", ""))
                if fn:
                    asyncio.create_task(fn(msg))
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(ws)
            log.info("character disconnected (%d)", len(self.clients))

    def emit(self, type_: str, **data) -> None:
        """Send an event to all characters. Safe to call from any thread."""
        msg = {"type": type_, **data}
        if type_ in STICKY:
            self.sticky[type_] = msg
        if self.loop is None or self.loop.is_closed():
            return
        try:
            same_loop = asyncio.get_running_loop() is self.loop
        except RuntimeError:
            same_loop = False
        if same_loop:
            self._broadcast(msg)
        else:
            self.loop.call_soon_threadsafe(self._broadcast, msg)

    def _broadcast(self, msg: dict) -> None:
        if self.clients:
            broadcast(self.clients, json.dumps(msg))
