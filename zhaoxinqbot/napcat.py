"""用于 NapCat 的轻量 OneBot 11 WebSocket 客户端。

NapCat 可以开启正向 WebSocket 服务端。本客户端连接该服务端，在同一条
连接上接收事件帧并发送 API 调用帧，再通过 OneBot 的 echo 字段匹配响应。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import websockets


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]
SEND_MESSAGE_ACTIONS = {"send_group_msg", "send_private_msg", "send_msg"}


class NapCatClient:
    """NapCat OneBot WebSocket API 的异步封装。"""

    def __init__(self, ws_url: str, access_token: str = "", send_message_delay_seconds: float = 0):
        self.ws_url = ws_url
        self.access_token = access_token
        self.send_message_delay_seconds = max(0.0, float(send_message_delay_seconds))
        self._ws: Any = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @property
    def is_connected(self) -> bool:
        return self._ws is not None

    async def connect_forever(
        self,
        handler: EventHandler,
        reconnect_seconds: int = 5,
        on_connect: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """连接、分发事件，并在临时故障后重连。"""

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
                    if on_connect is not None:
                        asyncio.create_task(on_connect())
                    async for raw in ws:
                        await self._dispatch(raw, handler)
            except Exception as exc:
                print(f"[napcat] websocket disconnected: {exc}")
                await asyncio.sleep(reconnect_seconds)
            finally:
                self._ws = None
                # 断线时让所有未完成 API 调用失败，避免功能任务一直等待。
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("NapCat websocket disconnected"))
                self._pending.clear()

    async def _dispatch(self, raw: str, handler: EventHandler) -> None:
        """区分 API 响应和 NapCat 主动推送的 OneBot 事件。"""

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
        """调用一个 OneBot 动作，并返回响应中的 data 对象。"""

        if self._ws is None:
            raise ConnectionError("NapCat websocket is not connected")

        echo = f"{action}:{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = future

        try:
            if action in SEND_MESSAGE_ACTIONS and self.send_message_delay_seconds > 0:
                await asyncio.sleep(self.send_message_delay_seconds)
            await self._ws.send(json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=30)
            if response.get("status") != "ok" or response.get("retcode") not in (0, None):
                raise RuntimeError(f"{action} failed: {response}")
            return response.get("data") or {}
        finally:
            self._pending.pop(echo, None)

    async def send_group_msg(self, group_id: int, message: str | list[dict[str, Any]]) -> int | str | None:
        """发送群消息，并在 NapCat 返回时取出 message_id。"""

        data = await self.call("send_group_msg", group_id=group_id, message=message)
        return data.get("message_id")

    async def delete_msg(self, message_id: int | str) -> None:
        """按 message_id 撤回消息。"""

        await self.call("delete_msg", message_id=message_id)

    async def set_group_ban(self, group_id: int, user_id: int, duration: int) -> None:
        """禁言或解除禁言群成员；duration 为 0 表示解除禁言。"""

        await self.call("set_group_ban", group_id=group_id, user_id=user_id, duration=duration)

    async def get_group_member_list(self, group_id: int) -> list[dict[str, Any]]:
        """获取群成员列表。"""

        data = await self.call("get_group_member_list", group_id=group_id)
        return data if isinstance(data, list) else []

    async def get_image(self, file: str = "", file_id: str = "") -> dict[str, Any]:
        """把图片消息段里的文件标识解析为路径或 URL 元数据。"""

        params = {}
        if file:
            params["file"] = file
        if file_id:
            params["file_id"] = file_id
        return await self.call("get_image", **params)

    async def get_file(self, file: str = "", file_id: str = "") -> dict[str, Any]:
        """把非图片媒体/文件消息段解析为路径或 URL 元数据。"""

        params = {}
        if file:
            params["file"] = file
        if file_id:
            params["file_id"] = file_id
        return await self.call("get_file", **params)
