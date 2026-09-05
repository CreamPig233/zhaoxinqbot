"""招新群实名认证流程。

本功能承担三件事：

* 无论实名认证开关是否开启，都记录招新群成员加入和退出。
* 实名认证开启时，对新成员执行禁言。
* 接收私聊实名申请，并根据外部审核文件或管理群人工审核结果流转状态。

所有命令词和回复模板都来自 strings.yaml，方便后续只改 YAML 不改代码。
"""

from __future__ import annotations

import asyncio
import argparse
import importlib.util
import inspect
import re
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from .config import BotConfig, RealNameStrings
from .errors import ErrorReporter
from .napcat import NapCatClient
from .storage import JsonStore, utc_now_iso


LEGACY_MEDICAL_COLLEGE = "医学与生物信息工程学院（原中荷生物医学与信息工程学院）"
LEGACY_MEDICAL_COLLEGE_ALT = "医学与生物信息工程学院（中荷生物医学与信息工程学院）"
NORMALIZED_MEDICAL_COLLEGE = "医学与生物信息工程学院"


def normalize_college(college: str) -> str:
    """统一历史学院名称。"""

    if college in {LEGACY_MEDICAL_COLLEGE, LEGACY_MEDICAL_COLLEGE_ALT}:
        return NORMALIZED_MEDICAL_COLLEGE
    return college


def is_member_muted(member: dict[str, Any], now: float | None = None) -> bool:
    """Return whether a member's OneBot mute expiry is still in the future."""

    try:
        mute_until = float(member.get("shut_up_timestamp", 0) or 0)
    except (TypeError, ValueError):
        return False
    return mute_until > (time.time() if now is None else now)


class RealNameAuditor:
    """处理成员记录和实名审核状态流转。"""

    def __init__(
        self,
        config: BotConfig,
        strings: RealNameStrings,
        store: JsonStore,
        client: NapCatClient,
        error_reporter: ErrorReporter | None = None,
    ):
        self.config = config
        self.strings = strings
        self.store = store
        self.client = client
        self.error_reporter = error_reporter
        self._application_locks: dict[int, asyncio.Lock] = {}
        self._mute_refresh_task: asyncio.Task[None] | None = None
        self._member_export_lock = asyncio.Lock()
        self._member_export_module: Any | None = None

    async def export_members_incremental(self) -> None:
        """Refresh the recruit-group real-name member CSV after state changes."""

        if not self.config.realname.incremental_member_export_enabled:
            return

        project_root = Path(__file__).resolve().parent.parent
        script_path = project_root / "export_realname_members_incremental.py"
        if not script_path.is_file():
            return

        async with self._member_export_lock:
            try:
                if self._member_export_module is None:
                    spec = importlib.util.spec_from_file_location(
                        "zhaoxinqbot_incremental_member_export",
                        script_path,
                    )
                    if spec is None or spec.loader is None:
                        raise ImportError(f"cannot load export script: {script_path}")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    self._member_export_module = module
                await self._member_export_module._run(
                    argparse.Namespace(
                        config=project_root / "config.yaml",
                        output=None,
                    )
                )
            except Exception as exc:
                print(f"[realname] failed to export incremental member CSV: {exc}")
                await self.report_error("实名成员 CSV 增量导出失败", exc)

    def start_mute_refresh(self) -> asyncio.Task[None]:
        """启动未实名成员的周期性续禁言任务。"""

        if self._mute_refresh_task is None or self._mute_refresh_task.done():
            self._mute_refresh_task = asyncio.create_task(self._mute_refresh_loop())
        return self._mute_refresh_task

    async def stop_mute_refresh(self) -> None:
        """停止周期性续禁言任务。"""

        if self._mute_refresh_task is None:
            return
        self._mute_refresh_task.cancel()
        try:
            await self._mute_refresh_task
        except asyncio.CancelledError:
            pass
        finally:
            self._mute_refresh_task = None

    async def _mute_refresh_loop(self) -> None:
        interval = max(1, self.config.realname.mute_refresh_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            try:
                await self.refresh_unverified_mutes()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[realname] failed to refresh unverified mutes: {exc}")
                await self.report_error("未实名成员续禁言扫描失败", exc)

    async def refresh_unverified_mutes(self) -> None:
        """扫描招新群成员，只给当前未禁言的未实名成员设置禁言。"""

        if not self.config.realname.enabled or not self.client.is_connected:
            return

        members = await self.client.get_group_member_list(self.config.groups.recruit_group)
        data = self.store.load_realnames()
        verified = {str(user_id) for user_id in data.get("verified", {})}
        whitelist = {str(user_id) for user_id in self.config.realname.whitelist_users}
        for member in members:
            try:
                user_id = int(member.get("user_id", 0))
            except (TypeError, ValueError):
                continue
            if not user_id or str(user_id) in verified or str(user_id) in whitelist:
                continue
            # 管理员/群主不能被普通群禁言，跳过以免扫描日志反复报错。
            if str(member.get("role", "")).lower() in {"owner", "admin"}:
                continue
            if is_member_muted(member):
                continue
            if self.config.realname.global_mute_enabled:
                try:
                    await self.client.set_group_ban(
                        self.config.groups.recruit_group,
                        user_id,
                        self.config.realname.mute_duration_seconds,
                    )

                except Exception as exc:
                    print(f"[realname] failed to refresh mute for {user_id}: {exc}")
                    await self.report_error(
                        "未实名成员续禁言失败",
                        exc,
                        user_id=user_id,
                        group_id=self.config.groups.recruit_group,
                    )

    async def on_member_change(self, event: dict[str, Any]) -> None:
        """记录入群/退群通知，并为新成员启动实名流程。"""

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
        if event.get("notice_type") == "group_decrease":
            await self.cancel_active_application_for_leave(int(event.get("user_id", 0)))
            await self.export_members_incremental()
            return

        await self.export_members_incremental()

        if event.get("notice_type") != "group_increase" or not self.config.realname.enabled:
            return

        user_id = int(event.get("user_id"))
        if await self.is_verified(user_id):
            await self.send_rejoin_prompt(user_id)
            return

        if self.config.realname.global_mute_enabled:
            try:
                await self.client.set_group_ban(
                    self.config.groups.recruit_group,
                    user_id,
                    self.config.realname.mute_duration_seconds,
                )
            except Exception as exc:
                print(f"[realname] failed to mute new member {user_id}: {exc}")
                await self.report_error("新成员禁言失败", exc, user_id=user_id, group_id=self.config.groups.recruit_group)
                await self.safe_send_admin(f"新成员 {user_id} 入群后禁言失败，请管理员手动检查：{exc}")
        try:
            await self.client.send_group_msg(
                self.config.groups.recruit_group,
                [
                    {"type": "at", "data": {"qq": str(user_id)}},
                    {"type": "text", "data": {"text": f" {self.strings.join_prompt}"}},
                ],
            )
        except Exception as exc:
            print(f"[realname] failed to send join prompt to {user_id}: {exc}")
            await self.report_error("新成员实名提示发送失败", exc, user_id=user_id, group_id=self.config.groups.recruit_group)
            await self.safe_send_admin(f"新成员 {user_id} 的实名提示发送失败，请管理员手动提醒：{exc}")

    async def on_private_message(self, event: dict[str, Any]) -> None:
        """接收以配置命令开头的私聊实名申请。"""

        if not self.config.realname.enabled:
            return
        if is_self_message(event):
            return

        text = extract_text(event.get("message", event.get("raw_message", ""))).strip()
        if not text.startswith(self.strings.submit_command):
            return

        user_id = int(event.get("user_id"))
        identity = self.parse_identity(text)
        if not identity:
            await self.client.call("send_private_msg", user_id=user_id, message=self.strings.invalid_format)
            return

        lock = self.get_application_lock(user_id)
        if lock.locked():
            await self.client.call(
                "send_private_msg",
                user_id=user_id,
                message=self.strings.duplicate_active_application,
            )
            return

        async with lock:
            await self.handle_realname_submission(user_id, identity, text, event)

    async def handle_realname_submission(
        self,
        user_id: int,
        identity: dict[str, str],
        text: str,
        event: dict[str, Any],
        *,
        response_channel: str = "private",
        operator_id: int = 0,
    ) -> None:
        """在单用户锁内创建并推进一次实名申请，避免并发重复提交。"""

        async def send_response(message: str) -> None:
            if response_channel == "admin":
                await self.safe_send_admin(message)
            else:
                await self.client.call("send_private_msg", user_id=user_id, message=message)

        notify_user = response_channel != "admin"
        result_to_admin = response_channel == "admin"
        approve_mode = "admin_add" if response_channel == "admin" else "auto"

        data = self.store.load_realnames()
        if await self.is_verified(user_id):
            await send_response(self.strings.already_verified)
            return

        if self.get_active_application(data, user_id):
            await send_response(self.strings.duplicate_active_application)
            return

        if self.config.realname.one_qq_one_identity:
            duplicate = self.find_identity_owner(data, identity)
            if duplicate and duplicate != str(user_id):
                await send_response(self.strings.duplicate_identity)
                return

        application = self.create_application(user_id, identity, text, event)
        application["response_channel"] = response_channel
        if operator_id:
            application["submitted_by"] = operator_id
        data.setdefault("applications", {})[application["application_id"]] = application
        data.setdefault("active_by_user", {})[str(user_id)] = application["application_id"]
        self.save_application_state(data, application, "auto_reviewing", "auto_reviewing")
        self.store.save_realnames(data)
        await self.export_members_incremental()
        await send_response(self.strings.auto_reviewing)

        decision, reason, review_data = await self.run_external_review(application)
        data = self.store.load_realnames()
        application = data.get("applications", {}).get(application["application_id"])
        if not application:
            return
        if application.get("status") != "auto_reviewing":
            return

        if review_data.get("college"):
            application.setdefault("identity", {})["college"] = normalize_college(review_data["college"])
            self.store.save_realnames(data)

        if decision == "approve":
            await self.approve(
                user_id,
                operator_id,
                approve_mode,
                application["application_id"],
                reason,
                notify_user=notify_user,
                result_to_admin=result_to_admin,
            )
            return
        if decision == "reject":
            await self.reject(
                user_id,
                operator_id,
                approve_mode,
                reason or self.strings.auto_reject_reason,
                application["application_id"],
                notify_user=notify_user,
                result_to_admin=result_to_admin,
            )
            return

        self.save_application_state(data, application, "manual_pending", reason or self.strings.auto_timeout_reason)
        data.setdefault("pending", {})[str(user_id)] = {
            **application,
            "identity": application.get("identity", identity),
        }
        self.store.save_realnames(data)
        await self.export_members_incremental()
        await self.notify_admins(user_id, identity, application["application_id"])
        await send_response(self.strings.manual_handoff)

    async def on_admin_group_message(self, event: dict[str, Any]) -> None:
        """处理管理群发出的批准、拒绝和取消实名命令。"""

        if int(event.get("group_id", 0)) != self.config.groups.admin_group:
            return

        user_id = int(event.get("user_id", 0))
        if self.config.realname.admin_approvers and user_id not in self.config.realname.admin_approvers:
            return

        message = event.get("message", event.get("raw_message", ""))
        text = extract_text(message).strip()
        target = self.extract_reply_target(event)
        target_application_id = self.extract_reply_application_id(event)

        if text.startswith(self.strings.add_verified_command):
            if not has_at_self(event):
                return
            target_user, identity = self.parse_admin_add_identity(text)
            if not target_user or not identity:
                await self.safe_send_admin(
                    f"格式不正确。请在管理群 @机器人 后发送：{self.strings.add_verified_command} QQ号 姓名 学号"
                )
                return
            lock = self.get_application_lock(target_user)
            if lock.locked():
                await self.safe_send_admin(self.strings.duplicate_active_application)
                return
            async with lock:
                await self.handle_realname_submission(
                    target_user,
                    identity,
                    text,
                    event,
                    response_channel="admin",
                    operator_id=user_id,
                )
            return

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
        """判断某个 QQ 是否已有通过的实名记录。"""

        data = self.store.load_realnames()
        return user_id in self.config.realname.whitelist_users or str(user_id) in data.get("verified", {})

    def parse_identity(self, text: str) -> dict[str, str] | None:
        """把“实名 姓名 学号”及常见符号分隔写法解析为身份对象。"""

        command = self.strings.submit_command.strip()
        normalized = unicodedata.normalize("NFKC", text).strip()
        normalized_command = unicodedata.normalize("NFKC", command)
        if not normalized.startswith(normalized_command):
            return None

        content = normalized[len(normalized_command) :].strip()
        parts = [part for part in IDENTITY_SEPARATOR_RE.split(content) if part]
        if len(parts) != 2:
            return None

        name = normalize_person_name(parts[0])
        identity_id = parts[1].strip().upper()
        if (
            not name
            or not STUDENT_ID_RE.fullmatch(identity_id)
            or not MIN_STUDENT_ID_LENGTH <= len(identity_id) <= MAX_STUDENT_ID_LENGTH
        ):
            return None
        return {"name": name, "id": identity_id}

    def parse_admin_add_identity(self, text: str) -> tuple[int | None, dict[str, str] | None]:
        """解析“添加实名 QQ号 姓名 学号”管理员代提交命令。"""

        command = self.strings.add_verified_command.strip()
        normalized = unicodedata.normalize("NFKC", text).strip()
        normalized_command = unicodedata.normalize("NFKC", command)
        if not normalized.startswith(normalized_command):
            return None, None

        content = normalized[len(normalized_command) :].strip()
        parts = [part for part in IDENTITY_SEPARATOR_RE.split(content) if part]
        if len(parts) != 3:
            return None, None
        qq, name_part, identity_part = parts
        if not qq.isdigit():
            return None, None
        name = normalize_person_name(name_part)
        identity_id = identity_part.strip().upper()
        if (
            not name
            or not STUDENT_ID_RE.fullmatch(identity_id)
            or not MIN_STUDENT_ID_LENGTH <= len(identity_id) <= MAX_STUDENT_ID_LENGTH
        ):
            return None, None
        return int(qq), {"name": name, "id": identity_id}

    async def approve(
        self,
        target_user: int,
        operator_id: int,
        mode: str,
        application_id: str | None = None,
        reason: str = "",
        *,
        notify_user: bool = True,
        result_to_admin: bool | None = None,
    ) -> None:
        """批准待审核申请，并解除招新群禁言。"""

        data = self.store.load_realnames()
        application = self.resolve_application(data, target_user, application_id)
        if not application:
            await self.client.send_group_msg(
                self.config.groups.admin_group,
                self.strings.no_pending.format(user_id=target_user),
            )
            return
        if application.get("response_channel") == "admin":
            notify_user = False
            if result_to_admin is None:
                result_to_admin = True

        data.get("pending", {}).pop(str(target_user), None)
        data.get("active_by_user", {}).pop(str(target_user), None)
        self.save_application_state(data, application, "approved", reason or mode)
        data.setdefault("verified", {})[str(target_user)] = {
            **application,
            "approved_at": utc_now_iso(),
            "approved_by": operator_id,
            "approve_mode": mode,
        }
        self.store.save_realnames(data)
        await self.export_members_incremental()
        unban_failed = ""
        try:
            await self.client.set_group_ban(self.config.groups.recruit_group, target_user, 0)
        except Exception as exc:
            unban_failed = f"；解除禁言失败：{exc}"
            await self.report_error("实名通过后解除禁言失败", exc, user_id=target_user)
        if notify_user:
            await self.safe_send_private(target_user, self.strings.approved_user)
        if result_to_admin is None:
            result_to_admin = mode == "manual"
        if result_to_admin:
            await self.safe_send_admin(self.strings.approved_admin.format(user_id=target_user) + unban_failed)

    async def reject(
        self,
        target_user: int,
        operator_id: int,
        mode: str,
        reason: str,
        application_id: str | None = None,
        *,
        notify_user: bool = True,
        result_to_admin: bool | None = None,
    ) -> None:
        """拒绝待审核申请，并允许用户重新提交。"""

        data = self.store.load_realnames()
        application = self.resolve_application(data, target_user, application_id)
        if not application:
            await self.client.send_group_msg(
                self.config.groups.admin_group,
                self.strings.no_pending.format(user_id=target_user),
            )
            return
        if application.get("response_channel") == "admin":
            notify_user = False
            if result_to_admin is None:
                result_to_admin = True

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
        await self.export_members_incremental()
        rejected_message = self.strings.rejected_user.format(
            resubmit_prompt=self.strings.resubmit_prompt,
            reason=reason,
        )
        if notify_user:
            await self.safe_send_private(target_user, rejected_message)
        if result_to_admin is None:
            result_to_admin = mode == "manual"
        if result_to_admin:
            await self.safe_send_admin(f"{self.strings.rejected_admin.format(user_id=target_user)}\n原因：{reason}")

    async def revoke(self, target_user: int, operator_id: int, mode: str) -> None:
        """把已通过实名记录移入撤销分区。"""

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
        await self.export_members_incremental()
        await self.client.send_group_msg(
            self.config.groups.admin_group,
            self.strings.revoked_admin.format(user_id=target_user),
        )

    async def send_rejoin_prompt(self, user_id: int) -> None:
        """已实名用户重新加入招新群时发送单独欢迎语。"""

        try:
            await self.client.send_group_msg(
                self.config.groups.recruit_group,
                [
                    {"type": "at", "data": {"qq": str(user_id)}},
                    {"type": "text", "data": {"text": f" {self.strings.verified_join_prompt}"}},
                ],
            )
        except Exception as exc:
            print(f"[realname] failed to send verified join prompt to {user_id}: {exc}")
            await self.report_error("已实名用户入群欢迎语发送失败", exc, user_id=user_id)
            await self.safe_send_admin(f"已实名成员 {user_id} 的入群欢迎语发送失败，请管理员手动确认：{exc}")

    async def safe_send_private(self, user_id: int, message: str) -> None:
        """发送私聊通知；失败只记录日志，不中断实名状态流转。"""

        try:
            await self.client.call("send_private_msg", user_id=user_id, message=message)
        except Exception as exc:
            print(f"[realname] failed to send private message to {user_id}: {exc}")
            await self.report_error("私聊通知发送失败", exc, user_id=user_id, message=message)

    async def safe_send_admin(self, message: str) -> None:
        """发送管理群通知；失败只记录日志，不中断实名状态流转。"""

        try:
            await self.client.send_group_msg(self.config.groups.admin_group, message)
        except Exception as exc:
            print(f"[realname] failed to send admin message: {exc}; message={message}")

    async def notify_admins(self, user_id: int, identity: dict[str, str], application_id: str) -> None:
        """向管理群发送审核通知，并记录通知消息 ID。"""

        msg = self.strings.review_notice.format(
            user_id=user_id,
            application_id=application_id,
            name=identity["name"],
            identity_id=identity["id"],
        )
        try:
            message_id = await self.client.send_group_msg(self.config.groups.admin_group, msg)
        except Exception as exc:
            print(f"[realname] failed to notify admins for application {application_id}: {exc}")
            await self.report_error("实名人工审核通知发送失败", exc, application_id=application_id, user_id=user_id)
            return
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
        """查找是否已有 QQ 使用相同实名信息。"""

        key = (normalize_person_name(identity["name"]), identity["id"].upper())
        for bucket in ("verified", "pending"):
            for user_id, item in data.get(bucket, {}).items():
                current = item.get("identity", {})
                current_key = (
                    normalize_person_name(str(current.get("name", ""))),
                    str(current.get("id", "")).upper(),
                )
                if current_key == key:
                    return user_id
        for item in data.get("applications", {}).values():
            if item.get("status") not in ACTIVE_APPLICATION_STATUSES:
                continue
            current = item.get("identity", {})
            current_key = (
                normalize_person_name(str(current.get("name", ""))),
                str(current.get("id", "")).upper(),
            )
            if current_key == key:
                return str(item.get("user_id"))
        return None

    def extract_user_arg(self, text: str) -> int | None:
        """从管理员命令中提取第一个像 QQ 号的数字。"""

        for part in text.split():
            if part.isdigit() and len(part) >= 5:
                return int(part)
        return None

    def extract_reply_target(self, event: dict[str, Any]) -> int | None:
        """从回复的审核通知中解析待审核用户 QQ。"""

        reply_id = extract_reply_id(event)
        if reply_id is None:
            return None
        data = self.store.load_realnames()
        item = data.get("review_messages", {}).get(str(reply_id))
        if item:
            return int(item["user_id"])
        return None

    def extract_reply_application_id(self, event: dict[str, Any]) -> str | None:
        """从回复的审核通知中解析精确申请 ID。"""

        reply_id = extract_reply_id(event)
        if reply_id is None:
            return None
        data = self.store.load_realnames()
        item = data.get("review_messages", {}).get(str(reply_id))
        if item:
            return str(item.get("application_id") or "")
        return None

    def get_active_application(self, data: dict[str, Any], user_id: int) -> dict[str, Any] | None:
        """返回用户当前仍在审核中的申请。"""

        application_id = data.get("active_by_user", {}).get(str(user_id))
        if not application_id:
            return None
        application = data.get("applications", {}).get(application_id)
        if application and application.get("status") in ACTIVE_APPLICATION_STATUSES:
            return application
        return None

    def get_application_lock(self, user_id: int) -> asyncio.Lock:
        """返回某个用户专属的实名申请锁。"""

        lock = self._application_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._application_locks[user_id] = lock
        return lock

    async def cancel_active_application_for_leave(self, user_id: int) -> None:
        """用户退群时取消仍在处理中的实名申请。"""

        if not user_id:
            return
        data = self.store.load_realnames()
        application = self.get_active_application(data, user_id)
        if not application:
            return

        data.get("pending", {}).pop(str(user_id), None)
        data.get("active_by_user", {}).pop(str(user_id), None)
        self.save_application_state(data, application, "cancelled_by_leave", self.strings.left_group_cancel_reason)
        self.store.save_realnames(data)

    def create_application(
        self,
        user_id: int,
        identity: dict[str, str],
        raw_text: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        """在任何审核工作开始前创建可持久化的申请记录。"""

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
        """更新申请状态，并追加详细 JSONL 审计记录。"""

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
        """查找正在被批准或拒绝的实名申请。"""

        if application_id:
            application = data.get("applications", {}).get(application_id)
            if (
                application
                and int(application.get("user_id", 0)) == target_user
                and application.get("status") in ACTIVE_APPLICATION_STATUSES
            ):
                return application
            return None
        active_id = data.get("active_by_user", {}).get(str(target_user))
        if active_id:
            application = data.get("applications", {}).get(active_id)
            if application and application.get("status") in ACTIVE_APPLICATION_STATUSES:
                return application
        pending = data.get("pending", {}).get(str(target_user))
        if pending and pending.get("application_id"):
            application = data.get("applications", {}).get(pending["application_id"], pending)
            if application and application.get("status") in ACTIVE_APPLICATION_STATUSES:
                return application
        return None

    async def run_external_review(self, application: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
        """调用配置的外部 Python 审核文件，并规范化返回值。"""

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
            return "timeout", self.strings.auto_timeout_reason, {}
        except Exception as exc:
            await self.report_error("外部实名审核异常", exc, application_id=application.get("application_id"))
            return "timeout", self.strings.auto_exception_reason.format(error=exc), {}
        return normalize_review_result(result, self.strings.auto_unknown_reason)

    def load_external_reviewer(self) -> Any:
        """从配置的 Python 文件路径加载审核函数。"""

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

    async def report_error(self, title: str, exc: BaseException, **context: Any) -> None:
        if self.error_reporter is not None:
            await self.error_reporter.report(title, exc, **context)


def extract_text(message: Any) -> str:
    """从 OneBot 消息数组中提取所有文本消息段。"""

    if isinstance(message, str):
        return message
    if not isinstance(message, list):
        return ""
    chunks = []
    for segment in message:
        if isinstance(segment, dict) and segment.get("type") == "text":
            chunks.append(str((segment.get("data") or {}).get("text", "")))
    return "".join(chunks)


def is_self_message(event: dict[str, Any]) -> bool:
    """判断消息事件是否由机器人自己发出。"""

    self_id = event.get("self_id")
    if self_id is None:
        return False
    user_id = event.get("user_id")
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("user_id")
    return str(user_id) == str(self_id) or str(sender_id) == str(self_id)


def has_at_self(event: dict[str, Any]) -> bool:
    """判断群消息是否 @ 了机器人。"""

    self_id = event.get("self_id")
    if self_id is None:
        return False
    message = event.get("message")
    if not isinstance(message, list):
        return False
    for segment in message:
        if not isinstance(segment, dict) or segment.get("type") != "at":
            continue
        qq = (segment.get("data") or {}).get("qq")
        if str(qq) == str(self_id):
            return True
    return False


def extract_reply_id(event: dict[str, Any]) -> Any:
    """从群消息事件中提取 OneBot reply 消息段 ID。"""

    message = event.get("message")
    if not isinstance(message, list):
        return None
    for segment in message:
        if isinstance(segment, dict) and segment.get("type") == "reply":
            return (segment.get("data") or {}).get("id")
    return None


def strip_command_and_user(text: str, command: str) -> str:
    """移除管理员命令和可选 QQ 参数，留下原因文本。"""

    rest = text[len(command) :].strip()
    parts = rest.split(maxsplit=1)
    if parts and parts[0].isdigit():
        return parts[1].strip() if len(parts) > 1 else ""
    return rest


def normalize_review_result(result: Any, unknown_reason_template: str) -> tuple[str, str, dict[str, str]]:
    """把外部审核返回值规范化为内部状态码、原因和可信扩展字段。"""

    reason = ""
    status = result
    review_data: dict[str, str] = {}
    if isinstance(result, dict):
        status = result.get("status") or result.get("result") or result.get("decision")
        reason = str(result.get("reason") or result.get("message") or "")
        college = normalize_college(str(result.get("college") or "").strip())
        if college:
            review_data["college"] = college
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
    return mapping.get(normalized, "timeout"), reason or unknown_reason_template.format(status=status), review_data


def normalize_person_name(name: str) -> str:
    """统一姓名中常见的少数民族间隔号写法。"""

    return name.translate(NAME_PUNCTUATION_TRANSLATION).strip()


ACTIVE_APPLICATION_STATUSES = {"created", "auto_reviewing", "manual_pending"}

# 姓名中的少数民族间隔号不作为分隔符，并统一为常用的 U+00B7 中间点。
NAME_PUNCTUATION_TRANSLATION = str.maketrans({"•": "·", "‧": "·", "・": "·"})
IDENTITY_SEPARATOR_RE = re.compile(r"[\s,:;、/\\|]+")
STUDENT_ID_RE = re.compile(r"[A-Z0-9]+")
MIN_STUDENT_ID_LENGTH = 7
MAX_STUDENT_ID_LENGTH = 9
