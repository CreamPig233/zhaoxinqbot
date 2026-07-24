"""Load runtime configuration and user-facing text from YAML files.

The project intentionally separates two kinds of editable data:

* ``config.yaml`` contains behavior switches, group IDs, storage locations, and
  external service credentials.
* ``strings.yaml`` contains bot commands, reply templates, review templates,
  LLM prompts, and preset Q&A text.

Keeping these layers separate lets operators tune behavior without hunting
through Python files, and lets copy be changed without touching credentials or
runtime switches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class NapCatConfig:
    """Connection settings for NapCat's forward WebSocket server."""

    ws_url: str
    access_token: str = ""
    reconnect_seconds: int = 5


@dataclass(frozen=True)
class GroupConfig:
    """QQ group IDs that define where each feature is active."""

    recruit_group: int
    admin_group: int


@dataclass(frozen=True)
class RealNameConfig:
    """Runtime switches for the real-name verification workflow."""

    enabled: bool = True
    one_qq_one_identity: bool = True
    mute_duration_seconds: int = 30 * 24 * 60 * 60
    admin_approvers: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class MessageArchiveConfig:
    """Runtime switches for message and media archiving."""

    enabled: bool = True
    download_media: bool = True


@dataclass(frozen=True)
class LLMConfig:
    """OpenAI-compatible Chat Completions settings for Q&A classification."""

    enabled: bool = False
    api_url: str = "https://api.openai.com/v1/chat/completions"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    timeout_seconds: int = 20


@dataclass(frozen=True)
class QAConfig:
    """Runtime switches for preset Q&A matching and reply recall."""

    enabled: bool = True
    recall_after_seconds: int = 60
    confidence_threshold: float = 0.72
    llm: LLMConfig = field(default_factory=LLMConfig)


@dataclass(frozen=True)
class StorageConfig:
    """Filesystem locations for all durable runtime data."""

    data_dir: Path


@dataclass(frozen=True)
class BotConfig:
    """All non-copy runtime settings loaded from ``config.yaml``."""

    napcat: NapCatConfig
    groups: GroupConfig
    realname: RealNameConfig
    message_archive: MessageArchiveConfig
    qa: QAConfig
    storage: StorageConfig


@dataclass(frozen=True)
class RealNameStrings:
    """Commands and message templates used by real-name verification."""

    submit_command: str
    approve_command: str
    reject_command: str
    revoke_command: str
    identity_format: str
    join_prompt: str
    resubmit_prompt: str
    invalid_format: str
    duplicate_identity: str
    submitted: str
    auto_reject_reason: str
    manual_reject_reason: str
    no_pending: str
    approved_admin: str
    approved_user: str
    rejected_admin: str
    rejected_user: str
    no_verified: str
    revoked_admin: str
    review_notice: str


@dataclass(frozen=True)
class PresetAnswer:
    """A single configured Q&A item used by local and LLM classifiers."""

    question: str
    answer: str
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QAStrings:
    """Prompt text and preset Q&A content loaded from ``strings.yaml``."""

    llm_system_prompt: str
    llm_user_prompt: str
    preset_answers: list[PresetAnswer] = field(default_factory=list)


@dataclass(frozen=True)
class BotStrings:
    """All user-facing copy and command words loaded from ``strings.yaml``."""

    realname: RealNameStrings
    qa: QAStrings


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Read a YAML mapping and fail early if the root value is not a mapping."""

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"{config_path} not found.")
    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a YAML object at the top level.")
    return raw


def load_config(path: str | Path = "config.yaml") -> BotConfig:
    """Load runtime options from ``config.yaml``."""

    raw = load_yaml(path)
    napcat = raw.get("napcat", {}) or {}
    groups = raw.get("groups", {}) or {}
    realname = raw.get("realname", {}) or {}
    archive = raw.get("message_archive", {}) or {}
    qa = raw.get("qa", {}) or {}
    llm = qa.get("llm", {}) or {}
    storage = raw.get("storage", {}) or {}

    return BotConfig(
        napcat=NapCatConfig(
            ws_url=str(napcat.get("ws_url", "ws://127.0.0.1:3001/")),
            access_token=str(napcat.get("access_token", "") or ""),
            reconnect_seconds=int(napcat.get("reconnect_seconds", 5)),
        ),
        groups=GroupConfig(
            recruit_group=int(groups.get("recruit_group", 810192062)),
            admin_group=int(groups.get("admin_group", 1065588188)),
        ),
        realname=RealNameConfig(
            enabled=bool(realname.get("enabled", True)),
            one_qq_one_identity=bool(realname.get("one_qq_one_identity", True)),
            mute_duration_seconds=int(realname.get("mute_duration_seconds", 30 * 24 * 60 * 60)),
            admin_approvers={int(x) for x in realname.get("admin_approvers", [])},
        ),
        message_archive=MessageArchiveConfig(
            enabled=bool(archive.get("enabled", True)),
            download_media=bool(archive.get("download_media", True)),
        ),
        qa=QAConfig(
            enabled=bool(qa.get("enabled", True)),
            recall_after_seconds=int(qa.get("recall_after_seconds", 60)),
            confidence_threshold=float(qa.get("confidence_threshold", 0.72)),
            llm=LLMConfig(
                enabled=bool(llm.get("enabled", False)),
                api_url=str(llm.get("api_url", "https://api.openai.com/v1/chat/completions")),
                api_key=str(llm.get("api_key", "") or ""),
                model=str(llm.get("model", "gpt-4o-mini")),
                timeout_seconds=int(llm.get("timeout_seconds", 20)),
            ),
        ),
        storage=StorageConfig(data_dir=Path(storage.get("data_dir", "data"))),
    )


def load_strings(path: str | Path = "strings.yaml") -> BotStrings:
    """Load commands, reply templates, and preset Q&A text from ``strings.yaml``."""

    raw = load_yaml(path)
    realname = raw.get("realname", {}) or {}
    qa = raw.get("qa", {}) or {}

    return BotStrings(
        realname=RealNameStrings(
            submit_command=str(realname["submit_command"]),
            approve_command=str(realname["approve_command"]),
            reject_command=str(realname["reject_command"]),
            revoke_command=str(realname["revoke_command"]),
            identity_format=str(realname["identity_format"]),
            join_prompt=str(realname["join_prompt"]),
            resubmit_prompt=str(realname["resubmit_prompt"]),
            invalid_format=str(realname["invalid_format"]),
            duplicate_identity=str(realname["duplicate_identity"]),
            submitted=str(realname["submitted"]),
            auto_reject_reason=str(realname["auto_reject_reason"]),
            manual_reject_reason=str(realname["manual_reject_reason"]),
            no_pending=str(realname["no_pending"]),
            approved_admin=str(realname["approved_admin"]),
            approved_user=str(realname["approved_user"]),
            rejected_admin=str(realname["rejected_admin"]),
            rejected_user=str(realname["rejected_user"]),
            no_verified=str(realname["no_verified"]),
            revoked_admin=str(realname["revoked_admin"]),
            review_notice=str(realname["review_notice"]),
        ),
        qa=QAStrings(
            llm_system_prompt=str(qa["llm_system_prompt"]),
            llm_user_prompt=str(qa["llm_user_prompt"]),
            preset_answers=[
                PresetAnswer(
                    question=str(item.get("question", "")),
                    answer=str(item.get("answer", "")),
                    aliases=[str(x) for x in item.get("aliases", [])],
                )
                for item in qa.get("preset_answers", [])
                if isinstance(item, dict) and item.get("question") and item.get("answer")
            ],
        ),
    )
