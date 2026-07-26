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

import asyncio
import importlib.util
import inspect
import uuid
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
        if self.get_active_application(data, user_id):
            await self.client.call(
                "send_private_msg",
                user_id=user_id,
                message=self.strings.duplicate_active_application,
            )
            return

        if self.config.realname.one_qq_one_identity:
            duplicate = self.find_identity_owner(data, identity)
            if duplicate and duplicate != str(user_id):
                await self.client.call("send_private_msg", user_id=user_id, message=self.strings.duplicate_identity)
                return

        application = self.create_application(user_id, identity, text, event)
        data.setdefault("applications", {})[application["application_id"]] = application
        data.setdefault("active_by_user", {})[str(user_id)] = application["application_id"]
        self.save_application_state(data, application, "auto_reviewing", "external reviewer started")
        self.store.save_realnames(data)
        await self.client.call("send_private_msg", user_id=user_id, message=self.strings.auto_reviewing)

        decision, reason = await self.run_external_review(application)
        data = self.store.load_realnames()
        application = data.get("applications", {}).get(application["application_id"])
        if not application:
            return

        if decision == "approve":
            await self.approve(user_id, 0, "auto", application["application_id"], reason)
            return
        if decision == "reject":
            await self.reject(
                user_id,
                0,
                "auto",
                reason or self.strings.auto_reject_reason,
                application["application_id"],
            )
            return

        self.save_application_state(data, application, "manual_pending", reason or "external reviewer timeout")
        data.setdefault("pending", {})[str(user_id)] = {
            **application,
            "identity": identity,
        }
        self.store.save_realnames(data)
        await self.client.call("send_private_msg", user_id=user_id, message=self.strings.manual_handoff)
        await self.notify_admins(user_id, identity, application["application_id"])

    async def on_admin_group_message(self, event: dict[str, Any]) -> None:
        """Handle approve/reject/revoke commands sent in the admin group."""

        if int(event.get("group_id", 0)) != self.config.groups.admin_group:
            return

        user_id = int(event.get("user_id", 0))
        if self.config.realname.admin_approvers and user_id not in self.config.realname.admin_approvers:
            return

        text = extract_text(event.get("message", event.get("raw_message", ""))).strip()
        target = self.extract_reply_target(event)
        target_application_id = self.extract_reply_application_id(event)

        if text.startswith(self.strings.approve_command):
            target_user = self.extract_user_arg(text) or target
            if target_user:
                await self.approve(target_user, user_id, "manual", target_application_id)
            return

        if text.startswith(self.strings.reject_command):
            target_user = self.extract_user_arg(text) or target
            reason = strip_command_and_user(text, self.strings.reject_command)
            reason = reason or self.strings.manual_reject_reason
            if target_user:
                await self.reject(target_user, user_id, "manual", reason, target_application_id)
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

    async def approve(
        self,
        target_user: int,
        operator_id: int,
        mode: str,
        application_id: str | None = None,
        reason: str = "",
    ) -> None:
        """Approve a pending submission and lift the recruit-group mute."""

        data = self.store.load_realnames()
        application = self.resolve_application(data, target_user, application_id)
        if not application:
            await self.client.send_group_msg(
                self.config.groups.admin_group,
                self.strings.no_pending.format(user_id=target_user),
            )
            return

        data.get("pending", {}).pop(str(target_user), None)
        data.get("active_by_user", {}).pop(str(target_user), None)
        self.save_application_state(data, application, "approved", reason or f"{mode} approved")
        data.setdefault("verified", {})[str(target_user)] = {
            **application,
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

    async def reject(
        self,
        target_user: int,
        operator_id: int,
        mode: str,
        reason: str,
        application_id: str | None = None,
    ) -> None:
        """Reject a pending submission and ask the user to submit again."""

        data = self.store.load_realnames()
        application = self.resolve_application(data, target_user, application_id)
        if not application:
            await self.client.send_group_msg(
                self.config.groups.admin_group,
                self.strings.no_pending.format(user_id=target_user),
            )
            return

        data.get("pending", {}).pop(str(target_user), None)
        data.get("active_by_user", {}).pop(str(target_user), None)
        self.save_application_state(data, application, "rejected", reason)
        data.setdefault("rejected", {})[str(target_user)] = {
            **application,
            "rejected_at": utc_now_iso(),
            "rejected_by": operator_id,
            "reject_mode": mode,
            "reason": reason,
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

    async def notify_admins(self, user_id: int, identity: dict[str, str], application_id: str) -> None:
        """Send the admin group a review notice and index that notice ID."""

        msg = self.strings.review_notice.format(
            user_id=user_id,
            application_id=application_id,
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
            "application_id": application_id,
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
        for item in data.get("applications", {}).values():
            if item.get("status") not in ACTIVE_APPLICATION_STATUSES:
                continue
            current = item.get("identity", {})
            if (current.get("name"), current.get("id"), current.get("unit")) == key:
                return str(item.get("user_id"))
        return None

    def extract_user_arg(self, text: str) -> int | None:
        """Extract the first QQ-looking number from an admin command."""

        for part in text.split():
            if part.isdigit() and len(part) >= 5:
                return int(part)
        return None

    def extract_reply_target(self, event: dict[str, Any]) -> int | None:
        """Resolve a replied review notice back to the pending user's QQ."""

        reply_id = extract_reply_id(event)
        if reply_id is None:
            return None
        data = self.store.load_realnames()
        item = data.get("review_messages", {}).get(str(reply_id))
        if item:
            return int(item["user_id"])
        return None

    def extract_reply_application_id(self, event: dict[str, Any]) -> str | None:
        """Resolve a replied review notice back to the exact application ID."""

        reply_id = extract_reply_id(event)
        if reply_id is None:
            return None
        data = self.store.load_realnames()
        item = data.get("review_messages", {}).get(str(reply_id))
        if item:
            return str(item.get("application_id") or "")
        return None

    def get_active_application(self, data: dict[str, Any], user_id: int) -> dict[str, Any] | None:
        """Return the user's active application, if one is still under review."""

        application_id = data.get("active_by_user", {}).get(str(user_id))
        if not application_id:
            return None
        application = data.get("applications", {}).get(application_id)
        if application and application.get("status") in ACTIVE_APPLICATION_STATUSES:
            return application
        return None

    def create_application(
        self,
        user_id: int,
        identity: dict[str, str],
        raw_text: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a durable application record before any review work starts."""

        now = utc_now_iso()
        return {
            "application_id": uuid.uuid4().hex,
            "user_id": user_id,
            "identity": identity,
            "raw_text": raw_text,
            "source_event": event,
            "created_at": now,
            "updated_at": now,
            "status": "created",
            "approved": False,
            "status_history": [],
        }

    def save_application_state(
        self,
        data: dict[str, Any],
        application: dict[str, Any],
        status: str,
        detail: str = "",
    ) -> None:
        """Update an application status and append a detailed JSONL audit record."""

        now = utc_now_iso()
        application["status"] = status
        application["updated_at"] = now
        application["approved"] = status == "approved"
        entry = {
            "application_id": application["application_id"],
            "user_id": application["user_id"],
            "created_at": application["created_at"],
            "updated_at": application["updated_at"],
            "time": now,
            "status": status,
            "approved": application["approved"],
            "detail": detail,
            "identity": application.get("identity"),
            "raw_text": application.get("raw_text"),
        }
        application.setdefault("status_history", []).append(entry)
        data.setdefault("applications", {})[application["application_id"]] = application
        self.store.append_jsonl(self.store.realname_applications_path, entry)

    def resolve_application(
        self,
        data: dict[str, Any],
        target_user: int,
        application_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find the application being approved or rejected."""

        if application_id:
            return data.get("applications", {}).get(application_id)
        active_id = data.get("active_by_user", {}).get(str(target_user))
        if active_id:
            return data.get("applications", {}).get(active_id)
        pending = data.get("pending", {}).get(str(target_user))
        if pending and pending.get("application_id"):
            return data.get("applications", {}).get(pending["application_id"], pending)
        return None

    async def run_external_review(self, application: dict[str, Any]) -> tuple[str, str]:
        """Call the configured external Python review file and normalize its result."""

        async def call_reviewer() -> Any:
            reviewer = self.load_external_reviewer()
            if inspect.iscoroutinefunction(reviewer):
                result = await reviewer(application)
            else:
                result = await asyncio.to_thread(reviewer, application)
            return result

        try:
            result = await asyncio.wait_for(
                call_reviewer(),
                timeout=self.config.realname.auto_review.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return "timeout", "external reviewer timed out"
        except Exception as exc:
            return "timeout", f"external reviewer failed: {exc}"
        return normalize_review_result(result)

    def load_external_reviewer(self) -> Any:
        """Load the configured review function from a Python file path."""

        path = self.config.realname.auto_review.module_path
        if not path.is_absolute():
            path = path.resolve()
        if not path.exists():
            raise FileNotFoundError(path)

        spec = importlib.util.spec_from_file_location("zhaoxinqbot_external_realname_reviewer", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load reviewer module from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reviewer = getattr(module, self.config.realname.auto_review.function_name)
        if not callable(reviewer):
            raise TypeError(f"{self.config.realname.auto_review.function_name} is not callable")
        return reviewer


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


def extract_reply_id(event: dict[str, Any]) -> Any:
    """Extract a OneBot reply segment ID from a group message event."""

    message = event.get("message")
    if not isinstance(message, list):
        return None
    for segment in message:
        if isinstance(segment, dict) and segment.get("type") == "reply":
            return (segment.get("data") or {}).get("id")
    return None


def strip_command_and_user(text: str, command: str) -> str:
    """Remove an admin command and optional QQ argument, leaving the reason text."""

    rest = text[len(command) :].strip()
    parts = rest.split(maxsplit=1)
    if parts and parts[0].isdigit():
        return parts[1].strip() if len(parts) > 1 else ""
    return rest


def normalize_review_result(result: Any) -> tuple[str, str]:
    """Normalize external reviewer output to approve/reject/timeout plus reason."""

    reason = ""
    status = result
    if isinstance(result, dict):
        status = result.get("status") or result.get("result") or result.get("decision")
        reason = str(result.get("reason") or result.get("message") or "")
    elif isinstance(result, (tuple, list)) and result:
        status = result[0]
        if len(result) > 1:
            reason = str(result[1])

    normalized = str(status or "").strip().lower()
    mapping = {
        "approve": "approve",
        "approved": "approve",
        "pass": "approve",
        "passed": "approve",
        "通过": "approve",
        "reject": "reject",
        "rejected": "reject",
        "deny": "reject",
        "denied": "reject",
        "拒绝": "reject",
        "timeout": "timeout",
        "timed_out": "timeout",
        "超时": "timeout",
    }
    return mapping.get(normalized, "timeout"), reason or f"external reviewer returned {status!r}"


ACTIVE_APPLICATION_STATUSES = {"created", "auto_reviewing", "manual_pending"}
