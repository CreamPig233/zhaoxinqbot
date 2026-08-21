"""应用装配与 OneBot 事件分发。

本模块只负责加载 YAML、创建共享服务、连接 NapCat，并把收到的 OneBot
事件转交给对应功能模块处理。
"""

from __future__ import annotations

import time
from typing import Any

from .config import load_config, load_strings
from .errors import ErrorReporter
from .messages import MessageArchive
from .napcat import NapCatClient
from .qa import QuestionAnswerer
from .prohibited_words import ProhibitedWordMatcher
from .realname import RealNameAuditor, extract_text, is_self_message
from .storage import JsonStore


class BotApp:
    """机器人运行期间长期持有的服务容器。"""

    def __init__(self) -> None:
        self.config = load_config()
        self.strings = load_strings()
        self.started_at = time.time()
        self.store = JsonStore(self.config.storage.data_dir)
        self.client = NapCatClient(
            self.config.napcat.ws_url,
            self.config.napcat.access_token,
            self.config.napcat.send_message_delay_seconds,
        )
        self.error_reporter = ErrorReporter(self.client, self.config.groups.admin_group)
        self.prohibited_words = ProhibitedWordMatcher(self.config.prohibited_words.words)
        self.archive = MessageArchive(
            self.store,
            self.client,
            download_media=self.config.message_archive.download_media,
            error_reporter=self.error_reporter,
        )
        self.realname = RealNameAuditor(
            self.config,
            self.strings.realname,
            self.store,
            self.client,
            error_reporter=self.error_reporter,
        )
        self.qa = QuestionAnswerer(self.config.qa, self.strings.qa, self.client, error_reporter=self.error_reporter)

    async def run(self) -> None:
        """连接 NapCat，并在进程退出前持续自动重连。"""

        print(f"[bot] connecting to {self.config.napcat.ws_url}")
        self.realname.start_mute_refresh()
        try:
            await self.client.connect_forever(
                self.handle_event,
                self.config.napcat.reconnect_seconds,
                on_connect=self.on_connect,
            )
        finally:
            await self.realname.stop_mute_refresh()

    async def on_connect(self) -> None:
        """WebSocket 建立后执行一次需要在线 API 的初始化任务。"""

        try:
            await self.realname.refresh_unverified_mutes()
        except Exception as exc:
            print(f"[bot] connect hook failed: {exc}")
            await self.error_reporter.report("连接后初始化任务失败", exc)

    async def handle_event(self, event: dict[str, Any]) -> None:
        """包住事件处理，避免单个坏事件导致机器人退出。"""

        try:
            await self._handle_event(event)
        except Exception as exc:
            print(f"[bot] event handler failed: {exc}; event={event}")
            await self.error_reporter.report("事件处理失败", exc, event=event)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        """按 post_type 和子类型把 OneBot 事件路由到功能模块。"""

        post_type = event.get("post_type")

        if post_type == "message" and event.get("message_type") == "group":
            if is_self_message(event):
                return
            archive_config = self.config.message_archive
            group_id = event.get("group_id")
            in_archive_scope = not archive_config.group_ids or group_id in archive_config.group_ids
            if archive_config.enabled and in_archive_scope:
                await self.archive.record_group_message(event)
            if (
                self.config.prohibited_words.enabled
                and int(group_id) == self.config.groups.recruit_group
                and is_new_message(event, self.started_at)
            ):
                text = extract_text(event.get("message", event.get("raw_message", "")))
                matched_word = self.prohibited_words.find(text)
                if matched_word is not None:
                    await self.client.delete_msg(event["message_id"])
                    print(f"[prohibited_words] recalled message {event['message_id']} (matched {matched_word!r})")
                    return
            await self.realname.on_admin_group_message(event)
            await self.qa.on_group_message(event)
            return

        if post_type == "message" and event.get("message_type") == "private":
            await self.realname.on_private_message(event)
            return

        if post_type == "notice":
            notice_type = event.get("notice_type")
            if notice_type in {"group_increase", "group_decrease"}:
                await self.realname.on_member_change(event)
                return
            if notice_type == "group_recall" and self.config.message_archive.enabled:
                group_id = event.get("group_id")
                if self.config.message_archive.group_ids and group_id not in self.config.message_archive.group_ids:
                    return
                await self.archive.mark_recalled(event)


async def main() -> None:
    """供 run_bot.py 调用的异步入口。"""

    await BotApp().run()


def is_new_message(event: dict[str, Any], started_at: float) -> bool:
    """Only allow message events created during this process run to be screened.

    OneBot message events carry their creation timestamp in ``time``. Requiring
    that timestamp prevents replayed/history messages from triggering recall.
    """

    try:
        event_time = float(event["time"])
    except (KeyError, TypeError, ValueError):
        return False
    return event_time >= started_at
