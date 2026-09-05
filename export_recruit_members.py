"""Export recruit-group members and their real-name status to CSV.

The script uses NapCat's OneBot 11 WebSocket action ``get_group_member_list``
and the real-name state written by this project.  Run it from the repository
root with ``python export_recruit_members.py``

这个脚本用来将实名信息导出为可读性较好的csv。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import websockets

from zhaoxinqbot.config import load_config, load_yaml


STATUS_VERIFIED = "已实名"
STATUS_UNVERIFIED = "未实名"
STATUS_WHITELIST = "白名单"
STATUS_OTHER = "其它"
GROUP_STATUS_CURRENT = "当前在群"
GROUP_STATUS_LEFT = "已退群"
JOIN_TIME_TOLERANCE_SECONDS = 5.0

BASE_HEADERS = ["序号", "QQ号", "个人昵称", "群名片"]
JOIN_TIME_HEADER = "入群时间"
LEAVE_TIME_HEADER = "退群时间"
GROUP_STATUS_HEADER = "群状态"
TAIL_HEADERS = ["实名状态", "姓名", "学号", "学院"]
LEGACY_MEDICAL_COLLEGE = "医学与生物信息工程学院（原中荷生物医学与信息工程学院）"
NORMALIZED_MEDICAL_COLLEGE = "医学与生物信息工程学院"


def normalize_college(college: str) -> str:
    if college == LEGACY_MEDICAL_COLLEGE:
        return NORMALIZED_MEDICAL_COLLEGE
    return college

# These are the states used by RealNameAuditor for applications that have not
# reached approval. Manual review is represented by ``manual_pending`` and
# therefore deliberately lands in the unverified category.
UNVERIFIED_APPLICATION_STATES = {
    "created",
    "auto_reviewing",
    "manual_pending",
    "rejected",
    "revoked",
    "cancelled_by_leave",
}


def _as_user_key(value: Any) -> str | None:
    """Return a normalized QQ key, or ``None`` for an invalid value."""

    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    return text if text.isdigit() and text != "0" else None


def _mapping_has_user(mapping: Any, user_key: str) -> bool:
    return isinstance(mapping, dict) and user_key in mapping


def _application_belongs_to_user(application: Any, user_key: str) -> bool:
    if not isinstance(application, dict):
        return False
    return _as_user_key(application.get("user_id")) == user_key


def _has_unverified_record(realname_data: dict[str, Any], user_key: str) -> bool:
    """Whether storage contains a non-approved real-name record for a user."""

    if _mapping_has_user(realname_data.get("pending"), user_key):
        return True
    if _mapping_has_user(realname_data.get("rejected"), user_key):
        return True
    if _mapping_has_user(realname_data.get("revoked"), user_key):
        return True

    applications = realname_data.get("applications")
    if isinstance(applications, dict):
        for application in applications.values():
            if not _application_belongs_to_user(application, user_key):
                continue
            if application.get("status") in UNVERIFIED_APPLICATION_STATES:
                return True
    return False


def _identity_from_verified_record(record: Any) -> tuple[str, str, str]:
    if not isinstance(record, dict):
        return "", "", ""
    identity = record.get("identity")
    if not isinstance(identity, dict):
        identity = record
    name = identity.get("name", "")
    student_id = identity.get("id", identity.get("student_id", ""))
    college = normalize_college(str(identity.get("college", "") or ""))
    return str(name or ""), str(student_id or ""), college


def classify_member(
    user_id: Any,
    realname_data: dict[str, Any],
    whitelist_users: Iterable[Any],
) -> tuple[str, str, str, str]:
    """Return ``(status, name, student_id, college)`` for one group member.

    Whitelist membership is deliberately checked before ``verified`` so the
    CSV retains the distinction requested by the operator.  Any known
    non-approved application, including manual review, is unverified.  A
    member without a successful record is also conservatively unverified;
    ``其它`` is reserved for malformed/unsupported records rather than
    silently treating an unknown member as verified.
    """

    user_key = _as_user_key(user_id)
    if user_key is None:
        return STATUS_OTHER, "", "", ""

    whitelist = {_as_user_key(item) for item in whitelist_users}
    whitelist.discard(None)
    if user_key in whitelist:
        return STATUS_WHITELIST, "", "", ""

    verified = realname_data.get("verified")
    if _mapping_has_user(verified, user_key):
        name, student_id, college = _identity_from_verified_record(verified[user_key])
        return STATUS_VERIFIED, name, student_id, college

    if _has_unverified_record(realname_data, user_key):
        return STATUS_UNVERIFIED, "", "", ""

    # No verified record means the member has not completed this bot's
    # real-name flow.  This includes newly joined members with no application.
    # A group member without a successful record has not completed the
    # real-name flow, even when they have not submitted an application yet.
    return STATUS_UNVERIFIED, "", "", ""


def _timestamp_to_text(value: Any) -> str:
    """Format a OneBot join timestamp, accepting seconds or milliseconds."""

    timestamp = _timestamp_value(value)
    if timestamp is None:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def _timestamp_value(value: Any) -> float | None:
    """Return a normalized join timestamp for sorting or formatting."""

    if isinstance(value, bool) or value in (None, "", 0, "0"):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value).timestamp()
            except ValueError:
                pass
        return None
    if timestamp <= 0:
        return None
    if timestamp > 100_000_000_000:
        timestamp /= 1000
    return timestamp


def _collapse_nearby_timestamps(timestamps: Iterable[float]) -> list[float]:
    """Keep the first timestamp in each run whose adjacent gap is <= 5s."""

    ordered = sorted(set(timestamps))
    if not ordered:
        return []
    collapsed = [ordered[0]]
    previous = ordered[0]
    for timestamp in ordered[1:]:
        if timestamp - previous > JOIN_TIME_TOLERANCE_SECONDS:
            collapsed.append(timestamp)
        previous = timestamp
    return collapsed


def load_membership_history(path: Path, group_id: int) -> dict[str, dict[str, list[float]]]:
    """Load join/leave timestamps for the requested group from JSONL."""

    history: dict[str, dict[str, list[float]]] = {}
    if not path.exists():
        return history

    with path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"成员事件日志第 {line_number} 行不是有效 JSON: {path}") from exc
            if not isinstance(event, dict):
                continue
            try:
                event_group_id = int(event.get("group_id", 0))
            except (TypeError, ValueError):
                continue
            if event_group_id != group_id:
                continue
            user_key = _as_user_key(event.get("user_id"))
            if user_key is None:
                continue
            event_type = event.get("notice_type")
            if event_type not in {"group_increase", "group_decrease"}:
                continue
            timestamp = _timestamp_value(event.get("event_time"))
            if timestamp is None and isinstance(event.get("raw_event"), dict):
                timestamp = _timestamp_value(event["raw_event"].get("time"))
            if timestamp is None:
                timestamp = _timestamp_value(event.get("recorded_at"))
            if timestamp is None:
                continue

            item = history.setdefault(user_key, {"joined": [], "left": []})
            bucket = "joined" if event_type == "group_increase" else "left"
            if timestamp not in item[bucket]:
                item[bucket].append(timestamp)

    for item in history.values():
        item["joined"] = _collapse_nearby_timestamps(item["joined"])
        item["left"].sort()
    return history


def _history_time_texts(item: dict[str, list[float]] | None, key: str) -> str:
    if not item:
        return ""
    values = [_timestamp_to_text(value) for value in item.get(key, [])]
    return "；".join(value for value in values if value)


def _members_by_qq(members: Iterable[Any]) -> list[dict[str, Any]]:
    """Index members by QQ and sort them by earliest join time ascending."""

    indexed: dict[str, dict[str, Any]] = {}
    for member in members:
        if not isinstance(member, dict):
            continue
        user_key = _as_user_key(member.get("user_id"))
        if user_key is not None:
            indexed[user_key] = member
    def sort_key(item: dict[str, Any]) -> tuple[bool, float, int]:
        timestamp = _timestamp_value(item.get("_sort_join_time", item.get("join_time")))
        return timestamp is None, timestamp or 0, int(_as_user_key(item.get("user_id")) or 0)

    return sorted(indexed.values(), key=sort_key)


def build_csv_rows(
    members: Iterable[Any],
    realname_data: dict[str, Any],
    whitelist_users: Iterable[Any],
    membership_history: dict[str, dict[str, list[float]]] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Build CSV rows from current NapCat members and history."""

    history = membership_history or {}
    current_members = list(members)
    current_user_keys = {
        _as_user_key(member.get("user_id"))
        for member in current_members
        if isinstance(member, dict)
    }
    current_user_keys.discard(None)

    indexed_members: dict[str, dict[str, Any]] = {}
    for member in current_members:
        if not isinstance(member, dict):
            continue
        user_key = _as_user_key(member.get("user_id"))
        if user_key is not None:
            indexed_members[user_key] = dict(member)
    for user_key in history:
        indexed_members.setdefault(user_key, {"user_id": user_key, "nickname": "", "card": ""})

    for user_key, member in indexed_members.items():
        item = history.setdefault(user_key, {"joined": [], "left": []})
        current_join_time = _timestamp_value(member.get("join_time"))
        if current_join_time is not None and current_join_time not in item["joined"]:
            item["joined"].append(current_join_time)
        item["joined"] = _collapse_nearby_timestamps(item["joined"])
        member["_sort_join_time"] = item["joined"][0] if item["joined"] else None

    normalized_members = _members_by_qq(indexed_members.values())
    has_join_time = any(_history_time_texts(history.get(_as_user_key(member.get("user_id"))), "joined") for member in normalized_members)
    has_leave_time = any(_history_time_texts(history.get(_as_user_key(member.get("user_id"))), "left") for member in normalized_members)
    headers = [*BASE_HEADERS]
    if has_join_time:
        headers.append(JOIN_TIME_HEADER)
    if has_leave_time:
        headers.append(LEAVE_TIME_HEADER)
    headers.append(GROUP_STATUS_HEADER)
    headers.extend(TAIL_HEADERS)

    rows: list[dict[str, str]] = []
    for sequence, member in enumerate(normalized_members, start=1):
        user_id = _as_user_key(member.get("user_id")) or ""
        status, name, student_id, college = classify_member(user_id, realname_data, whitelist_users)
        member_history = history.get(user_id)
        row = {
            BASE_HEADERS[0]: str(sequence),
            BASE_HEADERS[1]: user_id,
            BASE_HEADERS[2]: str(member.get("nickname") or ""),
            BASE_HEADERS[3]: str(member.get("card") or ""),
            TAIL_HEADERS[1]: name if status == STATUS_VERIFIED else "",
            TAIL_HEADERS[2]: student_id if status == STATUS_VERIFIED else "",
            TAIL_HEADERS[3]: college if status == STATUS_VERIFIED else "",
        }
        row[TAIL_HEADERS[0]] = status
        row[GROUP_STATUS_HEADER] = GROUP_STATUS_CURRENT if user_id in current_user_keys else GROUP_STATUS_LEFT
        if has_join_time:
            row[JOIN_TIME_HEADER] = _history_time_texts(member_history, "joined")
        if has_leave_time:
            row[LEAVE_TIME_HEADER] = _history_time_texts(member_history, "left")
        rows.append(row)
    return headers, rows


async def fetch_group_members(ws_url: str, access_token: str, group_id: int) -> list[dict[str, Any]]:
    """Call NapCat's documented OneBot action over one WebSocket connection."""

    headers = {"Authorization": f"Bearer {access_token}"} if access_token else None
    echo = f"get_group_member_list:{uuid.uuid4().hex}"
    request = {
        "action": "get_group_member_list",
        "params": {"group_id": str(group_id), "no_cache": True},
        "echo": echo,
    }

    connect_kwargs: dict[str, Any] = {"ping_interval": 25, "ping_timeout": 20}
    if headers:
        connect_kwargs["additional_headers"] = headers
    async with websockets.connect(ws_url, **connect_kwargs) as websocket:
        await websocket.send(json.dumps(request, ensure_ascii=False))
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=30)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            response = json.loads(raw)
            if response.get("echo") != echo:
                continue
            if response.get("status") != "ok" or response.get("retcode") not in (0, None):
                raise RuntimeError(f"get_group_member_list failed: {response}")
            data = response.get("data")
            if not isinstance(data, list):
                raise RuntimeError(f"get_group_member_list returned invalid data: {response}")
            return [item for item in data if isinstance(item, dict)]


def write_csv(path: Path, headers: list[str], rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="导出招新群成员实名状态 CSV")
    parser.add_argument("--config", type=Path, default=project_root / "config.yaml", help="配置文件路径")
    parser.add_argument("--data-dir", type=Path, help="覆盖实名数据目录，目录内应有 realname.json")
    parser.add_argument(
        "--output",
        type=Path,
        help="CSV 输出路径，默认写入 data/recruit_group_members.csv",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> Path:
    config_path = args.config.expanduser().resolve()
    config = load_config(config_path)
    group_id = config.groups.recruit_group

    secrets_path = config_path.parent / ".secrets"
    if not secrets_path.exists():
        raise FileNotFoundError(f"白名单配置文件不存在: {secrets_path}")
    secrets = load_yaml(secrets_path)
    realname_secrets = secrets.get("realname", {})
    if not isinstance(realname_secrets, dict):
        raise ValueError(f".secrets 中的 realname 必须是 YAML 对象: {secrets_path}")
    whitelist_users = realname_secrets.get("whitelist_users", []) or []
    if not isinstance(whitelist_users, (list, tuple, set)):
        raise ValueError(f".secrets 中的 realname.whitelist_users 必须是列表: {secrets_path}")
    napcat_secrets = secrets.get("napcat", {})
    if not isinstance(napcat_secrets, dict):
        raise ValueError(f".secrets 中的 napcat 必须是 YAML 对象: {secrets_path}")
    access_token = (
        str(napcat_secrets["access_token"] or "")
        if "access_token" in napcat_secrets
        else config.napcat.access_token
    )

    data_dir = args.data_dir or config.storage.data_dir
    if not data_dir.is_absolute():
        data_dir = config_path.parent / data_dir
    realname_path = data_dir / "realname.json"
    if not realname_path.exists():
        raise FileNotFoundError(f"实名数据文件不存在: {realname_path}")
    with realname_path.open("r", encoding="utf-8") as source:
        realname_data = json.load(source)
    if not isinstance(realname_data, dict):
        raise ValueError(f"实名数据文件必须是 JSON 对象: {realname_path}")

    membership_path = data_dir / "membership_events.jsonl"
    membership_history = load_membership_history(membership_path, group_id)
    members = await fetch_group_members(config.napcat.ws_url, access_token, group_id)
    headers, rows = build_csv_rows(members, realname_data, whitelist_users, membership_history)
    output = args.output or data_dir / "recruit_group_members.csv"
    if not output.is_absolute():
        output = Path.cwd() / output
    write_csv(output, headers, rows)
    print(f"已导出 {len(rows)} 名群成员: {output}")
    return output


def main(argv: list[str] | None = None) -> int:
    try:
        asyncio.run(_run(_parse_args(argv)))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"导出失败: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
