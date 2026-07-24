from __future__ import annotations

from typing import Any

from .config import dump_config_template_if_missing, load_config
from .messages import MessageArchive
from .napcat import NapCatClient
from .qa import QuestionAnswerer
from .realname import RealNameAuditor
from .storage import JsonStore


class BotApp:
    def __init__(self) -> None:
        dump_config_template_if_missing()
        self.config = load_config()
        self.store = JsonStore(self.config.storage.data_dir)
        self.client = NapCatClient(self.config.napcat.ws_url, self.config.napcat.access_token)
        self.archive = MessageArchive(
            self.store,
            self.client,
            download_media=self.config.message_archive.download_media,
        )
        self.realname = RealNameAuditor(self.config, self.store, self.client)
        self.qa = QuestionAnswerer(self.config.qa, self.client)

    async def run(self) -> None:
        print(f"[bot] connecting to {self.config.napcat.ws_url}")
        await self.client.connect_forever(self.handle_event, self.config.napcat.reconnect_seconds)

    async def handle_event(self, event: dict[str, Any]) -> None:
        try:
            await self._handle_event(event)
        except Exception as exc:
            print(f"[bot] event handler failed: {exc}; event={event}")

    async def _handle_event(self, event: dict[str, Any]) -> None:
        post_type = event.get("post_type")

        if post_type == "message" and event.get("message_type") == "group":
            if self.config.message_archive.enabled:
                await self.archive.record_group_message(event)
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
                await self.archive.mark_recalled(event)


async def main() -> None:
    await BotApp().run()
