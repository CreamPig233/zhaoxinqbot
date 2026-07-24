"""Minimal OneBot 11 WebSocket client for NapCat.

NapCat can expose a forward WebSocket server. This client connects to that
server, receives event frames, and sends API action frames over the same socket.
Responses are matched back to requests through the OneBot ``echo`` field.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import websockets


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class NapCatClient:
    """Small async wrapper around NapCat's OneBot WebSocket API."""

    def __init__(self, ws_url: str, access_token: str = ""):
        self.ws_url = ws_url
        self.access_token = access_token
        self._ws: Any = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def connect_forever(self, handler: EventHandler, reconnect_seconds: int = 5) -> None:
        """Connect, dispatch events, and reconnect after transient failures."""

        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        while True:
            try:
                async with websockets.connect(
                    self.ws_url,
                    additional_headers=headers or None,
                    ping_interval=25,
                    ping_timeout=20,
                ) as ws:
                    self._ws = ws
                    async for raw in ws:
                        await self._dispatch(raw, handler)
            except Exception as exc:
                print(f"[napcat] websocket disconnected: {exc}")
                await asyncio.sleep(reconnect_seconds)
            finally:
                self._ws = None
                # Fail all in-flight API calls so feature tasks do not wait forever.
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("NapCat websocket disconnected"))
                self._pending.clear()

    async def _dispatch(self, raw: str, handler: EventHandler) -> None:
        """Separate API responses from pushed OneBot events."""

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[napcat] ignored non-json frame: {raw[:120]}")
            return

        echo = payload.get("echo")
        if echo and echo in self._pending:
            future = self._pending.pop(echo)
            if not future.done():
                future.set_result(payload)
            return

        asyncio.create_task(handler(payload))

    async def call(self, action: str, **params: Any) -> dict[str, Any]:
        """Call a OneBot action and return its ``data`` object."""

        if self._ws is None:
            raise ConnectionError("NapCat websocket is not connected")

        echo = f"{action}:{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = future

        await self._ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
        response = await asyncio.wait_for(future, timeout=30)
        if response.get("status") != "ok" or response.get("retcode") not in (0, None):
            raise RuntimeError(f"{action} failed: {response}")
        return response.get("data") or {}

    async def send_group_msg(self, group_id: int, message: str | list[dict[str, Any]]) -> int | str | None:
        """Send a group message and return NapCat's message_id when provided."""

        data = await self.call("send_group_msg", group_id=group_id, message=message)
        return data.get("message_id")

    async def delete_msg(self, message_id: int | str) -> None:
        """Recall a message by message_id."""

        await self.call("delete_msg", message_id=message_id)

    async def set_group_ban(self, group_id: int, user_id: int, duration: int) -> None:
        """Mute or unmute a group member. A duration of 0 lifts the mute."""

        await self.call("set_group_ban", group_id=group_id, user_id=user_id, duration=duration)

    async def get_image(self, file: str = "", file_id: str = "") -> dict[str, Any]:
        """Resolve an image segment's file identifier to path or URL metadata."""

        params = {}
        if file:
            params["file"] = file
        if file_id:
            params["file_id"] = file_id
        return await self.call("get_image", **params)

    async def get_file(self, file: str = "", file_id: str = "") -> dict[str, Any]:
        """Resolve a non-image media/file segment to path or URL metadata."""

        params = {}
        if file:
            params["file"] = file
        if file_id:
            params["file_id"] = file_id
        return await self.call("get_file", **params)
