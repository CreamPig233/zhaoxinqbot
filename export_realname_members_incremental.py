"""Incrementally export recruit-group QQ numbers and real-name identities.

Run this script from the project root or directly by path.  The recruit group
comes from ``config.yaml`` and the whitelist comes from ``.secrets``.


这个脚本用来将实名信息导出为配合zhaoxinsurvey使用的csv文件。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from export_recruit_members import fetch_group_members
from zhaoxinqbot.config import load_yaml


QQ_HEADER = "QQ号"
STUDENT_ID_HEADER = "学号"
NAME_HEADER = "姓名"
COLLEGE_HEADER = "学院"
GROUP_CARD_HEADER = "QQ群名片"
REQUIRED_CSV_HEADERS = [QQ_HEADER, STUDENT_ID_HEADER, NAME_HEADER]
CSV_HEADERS = [*REQUIRED_CSV_HEADERS, COLLEGE_HEADER, GROUP_CARD_HEADER]
LEGACY_MEDICAL_COLLEGE = "医学与生物信息工程学院（原中荷生物医学与信息工程学院）"
LEGACY_MEDICAL_COLLEGE_ALT = "医学与生物信息工程学院（中荷生物医学与信息工程学院）"
NORMALIZED_MEDICAL_COLLEGE = "医学与生物信息工程学院"


def normalize_college(college: str) -> str:
    if college in {LEGACY_MEDICAL_COLLEGE, LEGACY_MEDICAL_COLLEGE_ALT}:
        return NORMALIZED_MEDICAL_COLLEGE
    return college


def _as_user_key(value: Any) -> str | None:
    """Return a valid QQ number as text."""

    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    return text if text.isdigit() and text != "0" else None


def _verified_identity(realname_data: dict[str, Any], user_key: str) -> tuple[str, str, str]:
    verified = realname_data.get("verified")
    if not isinstance(verified, dict) or user_key not in verified:
        return "", "", ""
    record = verified[user_key]
    if not isinstance(record, dict):
        return "", "", ""
    identity = record.get("identity")
    if not isinstance(identity, dict):
        identity = record
    return (
        str(identity.get("id", identity.get("student_id", "")) or ""),
        str(identity.get("name", "") or ""),
        normalize_college(str(identity.get("college", "") or "")),
    )


def identity_for_user(
    user_id: Any,
    realname_data: dict[str, Any],
    whitelist_users: Iterable[Any],
) -> tuple[str, str, str]:
    """Return the CSV ``(student_id, name, college)`` for a QQ number."""

    user_key = _as_user_key(user_id)
    if user_key is None:
        return "", "", ""
    whitelist = {_as_user_key(item) for item in whitelist_users}
    whitelist.discard(None)
    if user_key in whitelist:
        return user_key[:4], f"管理员{user_key}", ""
    return _verified_identity(realname_data, user_key)


def _row_is_verified(row: dict[str, str]) -> bool:
    """Identify an old row whose identity should survive leaving the group."""

    student_id = row.get(STUDENT_ID_HEADER, "").strip()
    name = row.get(NAME_HEADER, "").strip()
    return bool(student_id and name)


def read_existing_csv(path: Path) -> dict[str, dict[str, str]]:
    """Read an existing export indexed by QQ, or return an empty index."""

    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None or not set(REQUIRED_CSV_HEADERS).issubset(reader.fieldnames):
            raise ValueError(f"CSV 缺少必要表头（需要 QQ号、学号、姓名）: {path}")
        rows: dict[str, dict[str, str]] = {}
        for row in reader:
            user_key = _as_user_key(row.get(QQ_HEADER))
            if user_key is None:
                continue
            rows.setdefault(
                user_key,
                {
                    QQ_HEADER: user_key,
                    STUDENT_ID_HEADER: str(row.get(STUDENT_ID_HEADER) or ""),
                    NAME_HEADER: str(row.get(NAME_HEADER) or ""),
                    COLLEGE_HEADER: normalize_college(str(row.get(COLLEGE_HEADER) or "")),
                    GROUP_CARD_HEADER: str(row.get(GROUP_CARD_HEADER) or ""),
                },
            )
        return rows


def merge_rows(
    current_members: Iterable[Any],
    existing_rows: dict[str, dict[str, str]],
    realname_data: dict[str, Any],
    whitelist_users: Iterable[Any],
) -> list[dict[str, str]]:
    """Apply the incremental rules and return rows in stable CSV order."""

    current_keys: list[str] = []
    current_key_set: set[str] = set()
    current_cards: dict[str, str] = {}
    for member in current_members:
        if not isinstance(member, dict):
            continue
        user_key = _as_user_key(member.get("user_id"))
        if user_key is not None and user_key not in current_key_set:
            current_keys.append(user_key)
            current_key_set.add(user_key)
        if user_key is not None:
            card = str(member.get("card") or "").strip()
            if not card:
                card = str(member.get("nickname") or "").strip()
            current_cards[user_key] = card

    result: list[dict[str, str]] = []
    handled: set[str] = set()

    # Preserve existing order. Current users are refreshed from real-name data
    # and their live group cards; departed verified users are retained exactly
    # as they were exported.
    for user_key, old_row in existing_rows.items():
        if user_key in current_key_set:
            student_id, name, college = identity_for_user(user_key, realname_data, whitelist_users)
            result.append(
                {
                    QQ_HEADER: user_key,
                    STUDENT_ID_HEADER: student_id,
                    NAME_HEADER: name,
                    COLLEGE_HEADER: college,
                    GROUP_CARD_HEADER: current_cards[user_key],
                }
            )
        elif _row_is_verified(old_row):
            result.append({column: old_row.get(column, "") for column in CSV_HEADERS})
        handled.add(user_key)

    # Append current QQ numbers that were not present in the existing file.
    for user_key in current_keys:
        if user_key in handled:
            continue
        student_id, name, college = identity_for_user(user_key, realname_data, whitelist_users)
        result.append(
            {
                QQ_HEADER: user_key,
                STUDENT_ID_HEADER: student_id,
                NAME_HEADER: name,
                COLLEGE_HEADER: college,
                GROUP_CARD_HEADER: current_cards[user_key],
            }
        )
    return result


def write_csv_atomically(path: Path, rows: Iterable[dict[str, str]]) -> None:
    """Write a UTF-8 BOM CSV through a temporary file in the same directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=CSV_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _load_inputs(config_path: Path) -> tuple[dict[str, Any], list[Any], int, str, str, Path]:
    config = load_yaml(config_path)
    groups = config.get("groups", {})
    napcat = config.get("napcat", {})
    storage = config.get("storage", {})
    if not isinstance(groups, dict) or "recruit_group" not in groups:
        raise ValueError(f"config.yaml 缺少 groups.recruit_group: {config_path}")
    if not isinstance(napcat, dict) or not napcat.get("ws_url"):
        raise ValueError(f"config.yaml 缺少 napcat.ws_url: {config_path}")
    if not isinstance(storage, dict):
        raise ValueError(f"config.yaml 中的 storage 必须是对象: {config_path}")

    secrets_path = config_path.parent / ".secrets"
    secrets = load_yaml(secrets_path)
    realname_secrets = secrets.get("realname", {})
    napcat_secrets = secrets.get("napcat", {})
    if not isinstance(realname_secrets, dict):
        raise ValueError(f".secrets 中的 realname 必须是对象: {secrets_path}")
    if not isinstance(napcat_secrets, dict):
        raise ValueError(f".secrets 中的 napcat 必须是对象: {secrets_path}")

    whitelist_users = realname_secrets.get("whitelist_users", []) or []
    if not isinstance(whitelist_users, (list, tuple, set)):
        raise ValueError(f".secrets 中的 realname.whitelist_users 必须是列表: {secrets_path}")
    group_id = int(groups["recruit_group"])
    ws_url = str(napcat["ws_url"])
    access_token = str(napcat_secrets.get("access_token", "") or "")
    data_dir = Path(storage.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = config_path.parent / data_dir
    realname_path = data_dir / "realname.json"
    if not realname_path.exists():
        raise FileNotFoundError(f"实名数据文件不存在: {realname_path}")
    with realname_path.open("r", encoding="utf-8") as source:
        realname_data = json.load(source)
    if not isinstance(realname_data, dict):
        raise ValueError(f"实名数据文件必须是 JSON 对象: {realname_path}")
    return realname_data, list(whitelist_users), group_id, ws_url, access_token, data_dir


async def _run(args: argparse.Namespace) -> Path:
    config_path = args.config.expanduser().resolve()
    realname_data, whitelist_users, group_id, ws_url, access_token, data_dir = _load_inputs(config_path)
    output = args.output or data_dir / "realname_members.csv"
    if not output.is_absolute():
        output = Path.cwd() / output

    current_members = await fetch_group_members(ws_url, access_token, group_id)
    existing_rows = read_existing_csv(output)
    rows = merge_rows(current_members, existing_rows, realname_data, whitelist_users)
    write_csv_atomically(output, rows)
    print(f"已增量导出 {len(rows)} 条记录: {output}")
    return output


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    project_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="增量导出群成员 QQ 号及实名信息")
    parser.add_argument("--config", type=Path, default=project_root / "config.yaml", help="配置文件路径")
    parser.add_argument("--output", type=Path, help="CSV 输出路径，默认写入 data/realname_members.csv")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        asyncio.run(_run(_parse_args(argv)))
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        print(f"导出失败: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
