"""Real-name verification workflow for the recruit group.

This feature does three related jobs:

* record recruit-group joins and leaves regardless of the verification switch;
* mute new members while verification is enabled;
* accept private real-name submissions and let the admin group approve, reject,
  or revoke verification records.

All command words and reply templates come from ``strings.yaml`` so operators
can change copy without editing Python.
"""

from __future__ import annotations

from typing import Any

from .config import BotConfig, RealNameStrings
from .napcat import NapCatClient
from .storage import JsonStore, utc_now_iso


class RealNameAuditor:
    """Handle member tracking and real-name review state transitions."""

    def __init__(
        self,
        config: BotConfig,
        strings: RealNameStrings,
        store: JsonStore,
        client: NapCatClient,
    ):
        self.config = config
        self.strings = strings
        self.store = store
        self.client = client

    async def on_member_change(self, event: dict[str, Any]) -> None:
        """Record join/leave notices and start verification for new members."""

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
                {"type": "text", "data": {"text": f" {self.strings.join_prompt}"}},
            ],
        )

    async def on_private_message(self, event: dict[str, Any]) -> None:
        """Accept private submissions that begin with the configured command."""

        if not self.config.realname.enabled:
            return

        text = extract_text(event.get("message", event.get("raw_message", ""))).strip()
        if not text.startswith(self.strings.submit_command):
            return

        user_id = int(event.get("user_id"))
        identity = self.parse_identity(text)
        if not identity:
            await self.client.call("send_private_msg", user_id=user_id, message=self.strings.invalid_format)
            return

        data = self.store.load_realnames()
        if self.config.realname.one_qq_one_identity:
            duplicate = self.find_identity_owner(data, identity)
            if duplicate and duplicate != str(user_id):
                await self.client.call("send_private_msg", user_id=user_id, message=self.strings.duplicate_identity)
                return

        data.setdefault("pending", {})[str(user_id)] = {
            "identity": identity,
            "submitted_at": utc_now_iso(),
            "raw_text": text,
            "source_event": event,
        }
        self.store.save_realnames(data)

        auto_decision = await self.auto_review(user_id, identity)
        if auto_decision == "approve":
            await self.approve(user_id, 0, "auto")
            return
        if auto_decision == "reject":
            await self.reject(user_id, 0, "auto", self.strings.auto_reject_reason)
            return

        await self.client.call("send_private_msg", user_id=user_id, message=self.strings.submitted)
        await self.notify_admins(user_id, identity)

    async def on_admin_group_message(self, event: dict[str, Any]) -> None:
        """Handle approve/reject/revoke commands sent in the admin group."""

        if int(event.get("group_id", 0)) != self.config.groups.admin_group:
            return

        user_id = int(event.get("user_id", 0))
        if self.config.realname.admin_approvers and user_id not in self.config.realname.admin_approvers:
            return

        text = extract_text(event.get("message", event.get("raw_message", ""))).strip()
        target = self.extract_reply_target(event)

        if text.startswith(self.strings.approve_command):
            target_user = self.extract_user_arg(text) or target
            if target_user:
                await self.approve(target_user, user_id, "manual")
            return

        if text.startswith(self.strings.reject_command):
            target_user = self.extract_user_arg(text) or target
            reason = strip_command_and_user(text, self.strings.reject_command)
            reason = reason or self.strings.manual_reject_reason
            if target_user:
                await self.reject(target_user, user_id, "manual", reason)
            return

        if text.startswith(self.strings.revoke_command):
            target_user = self.extract_user_arg(text)
            if target_user:
                await self.revoke(target_user, user_id, "manual")

    async def is_verified(self, user_id: int) -> bool:
        """Return whether a QQ already has an approved real-name record."""

        data = self.store.load_realnames()
        return str(user_id) in data.get("verified", {})

    def parse_identity(self, text: str) -> dict[str, str] | None:
        """Parse ``实名 姓名 编号 单位`` into a durable identity object."""

        parts = text.split(maxsplit=3)
        if len(parts) < 4:
            return None
        return {"name": parts[1], "id": parts[2], "unit": parts[3]}

    async def auto_review(self, user_id: int, identity: dict[str, str]) -> str | None:
        """Extension point for future automatic review.

        Return ``"approve"`` to approve immediately, ``"reject"`` to reject
        immediately, or ``None`` to keep the current manual-review flow.
        """

        return None

    async def approve(self, target_user: int, operator_id: int, mode: str) -> None:
        """Approve a pending submission and lift the recruit-group mute."""

        data = self.store.load_realnames()
        pending = data.get("pending", {}).pop(str(target_user), None)
        if not pending:
            await self.client.send_group_msg(
                self.config.groups.admin_group,
                self.strings.no_pending.format(user_id=target_user),
            )
            return

        data.setdefault("verified", {})[str(target_user)] = {
            **pending,
            "approved_at": utc_now_iso(),
            "approved_by": operator_id,
            "approve_mode": mode,
        }
        self.store.save_realnames(data)
        await self.client.set_group_ban(self.config.groups.recruit_group, target_user, 0)
        await self.client.send_group_msg(
            self.config.groups.admin_group,
            self.strings.approved_admin.format(user_id=target_user),
        )
        await self.client.call("send_private_msg", user_id=target_user, message=self.strings.approved_user)

    async def reject(self, target_user: int, operator_id: int, mode: str, reason: str) -> None:
        """Reject a pending submission and ask the user to submit again."""

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
        await self.client.send_group_msg(
            self.config.groups.admin_group,
            self.strings.rejected_admin.format(user_id=target_user),
        )
        await self.client.call(
            "send_private_msg",
            user_id=target_user,
            message=self.strings.rejected_user.format(
                resubmit_prompt=self.strings.resubmit_prompt,
                reason=reason,
            ),
        )

    async def revoke(self, target_user: int, operator_id: int, mode: str) -> None:
        """Move an approved record to the revoked bucket."""

        data = self.store.load_realnames()
        verified = data.get("verified", {}).pop(str(target_user), None)
        if not verified:
            await self.client.send_group_msg(
                self.config.groups.admin_group,
                self.strings.no_verified.format(user_id=target_user),
            )
            return
        data.setdefault("revoked", {})[str(target_user)] = {
            **verified,
            "revoked_at": utc_now_iso(),
            "revoked_by": operator_id,
            "revoke_mode": mode,
        }
        self.store.save_realnames(data)
        await self.client.send_group_msg(
            self.config.groups.admin_group,
            self.strings.revoked_admin.format(user_id=target_user),
        )

    async def notify_admins(self, user_id: int, identity: dict[str, str]) -> None:
        """Send the admin group a review notice and index that notice ID."""

        msg = self.strings.review_notice.format(
            user_id=user_id,
            name=identity["name"],
            identity_id=identity["id"],
            unit=identity["unit"],
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
        """Find an existing QQ that already uses the same identity tuple."""

        key = (identity["name"], identity["id"], identity["unit"])
        for bucket in ("verified", "pending"):
            for user_id, item in data.get(bucket, {}).items():
                current = item.get("identity", {})
                if (current.get("name"), current.get("id"), current.get("unit")) == key:
                    return user_id
        return None

    def extract_user_arg(self, text: str) -> int | None:
        """Extract the first QQ-looking number from an admin command."""

        for part in text.split():
            if part.isdigit() and len(part) >= 5:
                return int(part)
        return None

    def extract_reply_target(self, event: dict[str, Any]) -> int | None:
        """Resolve a replied review notice back to the pending user's QQ."""

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
    """Collect text segments from OneBot message arrays."""

    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return ""
    chunks = []
    for segment in message:
        if isinstance(segment, dict) and segment.get("type") == "text":
            chunks.append(str((segment.get("data") or {}).get("text", "")))
    return "".join(chunks)


def strip_command_and_user(text: str, command: str) -> str:
    """Remove an admin command and optional QQ argument, leaving the reason text."""

    rest = text[len(command) :].strip()
    parts = rest.split(maxsplit=1)
    if parts and parts[0].isdigit():
        return parts[1].strip() if len(parts) > 1 else ""
    return rest
