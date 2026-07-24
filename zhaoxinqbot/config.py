from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class NapCatConfig:
    ws_url: str
    access_token: str = ""
    reconnect_seconds: int = 5


@dataclass(frozen=True)
class GroupConfig:
    recruit_group: int
    admin_group: int


@dataclass(frozen=True)
class RealNameConfig:
    enabled: bool = True
    one_qq_one_identity: bool = True
    mute_duration_seconds: int = 30 * 24 * 60 * 60
    admin_approvers: set[int] = field(default_factory=set)
    prompt: str = "请私聊机器人提交实名信息。"
    resubmit_prompt: str = "实名信息未通过审核，请重新提交。"


@dataclass(frozen=True)
class MessageArchiveConfig:
    enabled: bool = True
    download_media: bool = True


@dataclass(frozen=True)
class PresetAnswer:
    question: str
    answer: str
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool = False
    api_url: str = "https://api.openai.com/v1/chat/completions"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    timeout_seconds: int = 20


@dataclass(frozen=True)
class QAConfig:
    enabled: bool = True
    recall_after_seconds: int = 60
    confidence_threshold: float = 0.72
    llm: LLMConfig = field(default_factory=LLMConfig)
    preset_answers: list[PresetAnswer] = field(default_factory=list)


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path


@dataclass(frozen=True)
class BotConfig:
    napcat: NapCatConfig
    groups: GroupConfig
    realname: RealNameConfig
    message_archive: MessageArchiveConfig
    qa: QAConfig
    storage: StorageConfig


def load_config(path: str | Path = "config.yaml") -> BotConfig:
    config_path = Path(path)
    if not config_path.exists():
        example = Path("config.example.yaml")
        raise FileNotFoundError(
            f"{config_path} not found. Copy {example} to {config_path} and edit it."
        )

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    napcat = raw.get("napcat", {})
    groups = raw.get("groups", {})
    realname = raw.get("realname", {})
    archive = raw.get("message_archive", {})
    qa = raw.get("qa", {})
    storage = raw.get("storage", {})

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
            prompt=str(realname.get("prompt", "请私聊机器人提交实名信息。")),
            resubmit_prompt=str(realname.get("resubmit_prompt", "实名信息未通过审核，请重新提交。")),
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
                enabled=bool((qa.get("llm") or {}).get("enabled", False)),
                api_url=str((qa.get("llm") or {}).get("api_url", "https://api.openai.com/v1/chat/completions")),
                api_key=str((qa.get("llm") or {}).get("api_key", "") or ""),
                model=str((qa.get("llm") or {}).get("model", "gpt-4o-mini")),
                timeout_seconds=int((qa.get("llm") or {}).get("timeout_seconds", 20)),
            ),
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
        storage=StorageConfig(data_dir=Path(storage.get("data_dir", "data"))),
    )


def dump_config_template_if_missing(path: str | Path = "config.yaml") -> None:
    target = Path(path)
    if target.exists():
        return
    example = Path("config.example.yaml")
    target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
