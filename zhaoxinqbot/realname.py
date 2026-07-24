from __future__ import annotations

from typing import Any

from .config import BotConfig
from .napcat import NapCatClient
from .storage import JsonStore, utc_now_iso


class RealNameAuditor:
    def __init__(self, config: BotConfig, store: JsonStore, client: NapCatClient):
        self.config = config
        self.store = store
        self.client = client

    async def on_member_change(self, event: dict[str, Any]) -> None:
        group_id = int(event.get("group_id", 0))
        if group_id != self.config.groups.recruit_group:
            return

        self.store.append_jsonl(
            self.store.membership_path,
            {
                "recorded_at": utc_now_iso(),
                "event_time": event.get("time"),
                "notice_type": event.get("notice_type"),
                "sub_type": event.get("sub_type"),
                "group_id": group_id,
                "user_id": event.get("user_id"),
                "operator_id": event.get("operator_id"),
                "raw_event": event,
            },
        )

        if event.get("notice_type") != "group_increase" or not self.config.realname.enabled:
            return

        user_id = int(event.get("user_id"))
        if await self.is_verified(user_id):
            return

        await self.client.set_group_ban(
            self.config.groups.recruit_group,
            user_id,
            self.config.realname.mute_duration_seconds,
        )
        await self.client.send_group_msg(
            self.config.groups.recruit_group,
            [
                {"type": "at", "data": {"qq": str(user_id)}},
                {"type": "text", "data": {"text": f" {self.config.realname.prompt}"}},
            ],
        )

    async def on_private_message(self, event: dict[str, Any]) -> None:
        if not self.config.realname.enabled:
            return

        text = extract_text(event.get("message", event.get("raw_message", ""))).strip()
        if not text.startswith("实名"):
            return

        user_id = int(event.get("user_id"))
        identity = self.parse_identity(text)
        if not identity:
            await self.client.call(
                "send_private_msg",
                user_id=user_id,
                message="格式不正确。请发送：实名 姓名 学号/工号 学院/部门",
            )
            return

        auto_decision = await self.auto_review(user_id, identity)
        if auto_decision == "approve":
            await self.approve(user_id, 0, "auto")
            return
        if auto_decision == "reject":
            await self.reject(user_id, 0, "auto", "自动审核未通过")
            return

        data = self.store.load_realnames()
        if self.config.realname.one_qq_one_identity:
            duplicate = self.find_identity_owner(data, identity)
            if duplicate and duplicate != str(user_id):
                await self.client.call(
                    "send_private_msg",
                    user_id=user_id,
                    message="该实名信息已被其他 QQ 绑定，请联系管理员处理。",
                )
                return

        data["pending"][str(user_id)] = {
            "identity": identity,
            "submitted_at": utc_now_iso(),
            "raw_text": text,
            "source_event": event,
        }
        self.store.save_realnames(data)

        await self.client.call(
            "send_private_msg",
            user_id=user_id,
            message="实名信息已提交，请等待管理员审核。",
        )
        await self.notify_admins(user_id, identity)

    async def on_admin_group_message(self, event: dict[str, Any]) -> None:
        if int(event.get("group_id", 0)) != self.config.groups.admin_group:
            return

        user_id = int(event.get("user_id", 0))
        if self.config.realname.admin_approvers and user_id not in self.config.realname.admin_approvers:
            return

        text = extract_text(event.get("message", event.get("raw_message", ""))).strip()
        target = self.extract_reply_target(event)

        if text.startswith("批准"):
            target_user = self.extract_user_arg(text) or target
            if target_user:
                await self.approve(target_user, user_id, "manual")
            return

        if text.startswith("拒绝"):
            target_user = self.extract_user_arg(text) or target
            reason = text.replace("拒绝", "", 1).strip() or "管理员拒绝"
            if target_user:
                await self.reject(target_user, user_id, "manual", reason)
            return

        if text.startswith("取消实名"):
            target_user = self.extract_user_arg(text)
            if target_user:
                await self.revoke(target_user, user_id, "manual")

    async def is_verified(self, user_id: int) -> bool:
        data = self.store.load_realnames()
        return str(user_id) in data.get("verified", {})

    def parse_identity(self, text: str) -> dict[str, str] | None:
        parts = text.split(maxsplit=3)
        if len(parts) < 4:
            return None
        return {"name": parts[1], "id": parts[2], "unit": parts[3]}

    async def auto_review(self, user_id: int, identity: dict[str, str]) -> str | None:
        return None

    async def approve(self, target_user: int, operator_id: int, mode: str) -> None:
        data = self.store.load_realnames()
        pending = data.get("pending", {}).pop(str(target_user), None)
        if not pending:
            await self.client.send_group_msg(self.config.groups.admin_group, f"{target_user} 没有待审核实名信息。")
            return

        data.setdefault("verified", {})[str(target_user)] = {
            **pending,
            "approved_at": utc_now_iso(),
            "approved_by": operator_id,
            "approve_mode": mode,
        }
        self.store.save_realnames(data)
        await self.client.set_group_ban(self.config.groups.recruit_group, target_user, 0)
        await self.client.send_group_msg(self.config.groups.admin_group, f"已批准 {target_user} 的实名信息，并解除禁言。")
        await self.client.call("send_private_msg", user_id=target_user, message="实名审核已通过，欢迎加入。")

    async def reject(self, target_user: int, operator_id: int, mode: str, reason: str) -> None:
        data = self.store.load_realnames()
        pending = data.get("pending", {}).pop(str(target_user), None)
        data.setdefault("rejected", {})[str(target_user)] = {
            "rejected_at": utc_now_iso(),
            "rejected_by": operator_id,
            "reject_mode": mode,
            "reason": reason,
            "last_pending": pending,
        }
        self.store.save_realnames(data)
        await self.client.send_group_msg(self.config.groups.admin_group, f"已拒绝 {target_user} 的实名信息。")
        await self.client.call(
            "send_private_msg",
            user_id=target_user,
            message=f"{self.config.realname.resubmit_prompt}\n原因：{reason}",
        )

    async def revoke(self, target_user: int, operator_id: int, mode: str) -> None:
        data = self.store.load_realnames()
        verified = data.get("verified", {}).pop(str(target_user), None)
        if not verified:
            await self.client.send_group_msg(self.config.groups.admin_group, f"{target_user} 当前没有已通过的实名信息。")
            return
        data.setdefault("revoked", {})[str(target_user)] = {
            **verified,
            "revoked_at": utc_now_iso(),
            "revoked_by": operator_id,
            "revoke_mode": mode,
        }
        self.store.save_realnames(data)
        await self.client.send_group_msg(self.config.groups.admin_group, f"已取消 {target_user} 的实名信息。")

    async def notify_admins(self, user_id: int, identity: dict[str, str]) -> None:
        msg = (
            f"实名审核待处理\n"
            f"QQ：{user_id}\n"
            f"姓名：{identity['name']}\n"
            f"编号：{identity['id']}\n"
            f"单位：{identity['unit']}\n"
            f"请回复：批准 {user_id} 或 拒绝 {user_id} 原因"
        )
        message_id = await self.client.send_group_msg(self.config.groups.admin_group, msg)
        if message_id is None:
            return
        data = self.store.load_realnames()
        data.setdefault("review_messages", {})[str(message_id)] = {
            "user_id": user_id,
            "created_at": utc_now_iso(),
        }
        self.store.save_realnames(data)

    def find_identity_owner(self, data: dict[str, Any], identity: dict[str, str]) -> str | None:
        key = (identity["name"], identity["id"], identity["unit"])
        for bucket in ("verified", "pending"):
            for user_id, item in data.get(bucket, {}).items():
                current = item.get("identity", {})
                if (current.get("name"), current.get("id"), current.get("unit")) == key:
                    return user_id
        return None

    def extract_user_arg(self, text: str) -> int | None:
        for part in text.split():
            if part.isdigit() and len(part) >= 5:
                return int(part)
        return None

    def extract_reply_target(self, event: dict[str, Any]) -> int | None:
        reply_id = None
        message = event.get("message")
        if isinstance(message, list):
            for segment in message:
                if isinstance(segment, dict) and segment.get("type") == "reply":
                    reply_id = (segment.get("data") or {}).get("id")
                    break
        if reply_id is None:
            return None
        data = self.store.load_realnames()
        item = data.get("review_messages", {}).get(str(reply_id))
        if item:
            return int(item["user_id"])
        return None


def extract_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return ""
    chunks = []
    for segment in message:
        if isinstance(segment, dict) and segment.get("type") == "text":
            chunks.append(str((segment.get("data") or {}).get("text", "")))
    return "".join(chunks)
