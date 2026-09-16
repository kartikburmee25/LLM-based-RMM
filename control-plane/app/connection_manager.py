"""Tracks which devices currently have a live WebSocket connection.

This is the heart of the NAT-traversal design: agents dial out to us (NAT-
friendly) and hold the socket open. As long as it's open, we can push a job
to that device with no polling delay. This map is in-process/in-memory,
which is the deliberate "keep it simple" tradeoff for a single control-plane
instance -- see the README for what changes if you need more than one.
"""

import asyncio
import uuid

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(self, device_id: uuid.UUID, ws: WebSocket) -> None:
        async with self._lock:
            existing = self._connections.get(device_id)
            self._connections[device_id] = ws
        # If the same device somehow opens a second connection (e.g. a stale
        # one hadn't been cleaned up yet), close the old one rather than
        # leaving two sockets registered for one device.
        if existing is not None and existing is not ws:
            await existing.close(code=4000, reason="replaced by new connection")

    async def disconnect(self, device_id: uuid.UUID, ws: WebSocket) -> None:
        async with self._lock:
            if self._connections.get(device_id) is ws:
                del self._connections[device_id]

    def is_connected(self, device_id: uuid.UUID) -> bool:
        return device_id in self._connections

    async def send_json(self, device_id: uuid.UUID, payload: dict) -> bool:
        ws = self._connections.get(device_id)
        if ws is None:
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception:
            return False


manager = ConnectionManager()
