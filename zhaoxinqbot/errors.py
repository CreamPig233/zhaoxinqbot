"""Centralized error reporting helpers."""

from __future__ import annotations

import traceback
from typing import Any

from .napcat import NapCatClient


class ErrorReporter:
    """Send concise runtime errors to the admin group without recursive failures."""

    def __init__(self, client: NapCatClient, admin_group: int):
        self.client = client
        self.admin_group = admin_group
        self._reporting = False

    async def report(self, title: str, exc: BaseException | None = None, **context: Any) -> None:
        if self._reporting:
            return

        lines = [f"机器人运行错误：{title}"]
        if exc is not None:
            lines.append(f"异常：{type(exc).__name__}: {exc}")
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            if tb:
                lines.append("Traceback：")
                lines.append(tb[-1200:])
        for key, value in context.items():
            text = repr(value)
            if len(text) > 600:
                text = text[:600] + "...<truncated>"
            lines.append(f"{key}：{text}")

        message = "\n".join(lines)
        if len(message) > 3500:
            message = message[:3500] + "\n...<truncated>"

        self._reporting = True
        try:
            await self.client.send_group_msg(self.admin_group, message)
        except Exception as report_exc:
            print(f"[error-report] failed to send admin error report: {report_exc}; original={message}")
        finally:
            self._reporting = False
