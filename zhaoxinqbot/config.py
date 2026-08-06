"""从 YAML 文件加载运行配置和对外文案。

项目刻意把两类可编辑数据分开：

* config.yaml 保存功能开关、群号、存储路径和外部服务凭据。
* strings.yaml 保存命令词、回复模板、审核通知模板、LLM 提示词和预设问答。

这样改运行参数不需要找 Python 文件，改文案也不会碰到凭据和功能开关。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class NapCatConfig:
    """NapCat 正向 WebSocket 服务端连接配置。"""

    ws_url: str
    access_token: str = ""
    reconnect_seconds: int = 5
    send_message_delay_seconds: float = 1.0


@dataclass(frozen=True)
class GroupConfig:
    """定义各功能生效范围的 QQ 群号。"""

    recruit_group: int
    admin_group: int


@dataclass(frozen=True)
class AutoReviewConfig:
    """外部 Python 实名审核钩子的配置。"""

    module_path: Path
    function_name: str = "review_application"
    timeout_seconds: int = 30


@dataclass(frozen=True)
class RealNameConfig:
    """实名认证流程的运行开关。"""

    enabled: bool = True
    one_qq_one_identity: bool = True
    mute_duration_seconds: int = 90 * 24 * 60 * 60
    admin_approvers: set[int] = field(default_factory=set)
    auto_review: AutoReviewConfig = field(
        default_factory=lambda: AutoReviewConfig(module_path=Path("realname_reviewer.py"))
    )


@dataclass(frozen=True)
class MessageArchiveConfig:
    """消息和媒体归档功能的运行开关。"""

    enabled: bool = True
    download_media: bool = True


@dataclass(frozen=True)
class LLMConfig:
    """用于问答分类的 OpenAI 兼容 Chat Completions 配置。"""

    enabled: bool = False
    api_url: str = "https://api.openai.com/v1/chat/completions"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    timeout_seconds: int = 20


@dataclass(frozen=True)
class QAConfig:
    """预设问答匹配和回复撤回的运行开关。"""

    enabled: bool = True
    recall_after_seconds: int = 60
    confidence_threshold: float = 0.72
    llm: LLMConfig = field(default_factory=LLMConfig)


@dataclass(frozen=True)
class StorageConfig:
    """长期运行数据的文件系统位置。"""

    data_dir: Path


@dataclass(frozen=True)
class BotConfig:
    """从 config.yaml 读取的所有非文案运行配置。"""

    napcat: NapCatConfig
    groups: GroupConfig
    realname: RealNameConfig
    message_archive: MessageArchiveConfig
    qa: QAConfig
    storage: StorageConfig


@dataclass(frozen=True)
class RealNameStrings:
    """实名认证功能使用的命令词和消息模板。"""

    submit_command: str
    approve_command: str
    reject_command: str
    revoke_command: str
    identity_format: str
    join_prompt: str
    verified_join_prompt: str
    resubmit_prompt: str
    invalid_format: str
    already_verified: str
    duplicate_identity: str
    duplicate_active_application: str
    auto_reviewing: str
    submitted: str
    manual_handoff: str
    auto_reject_reason: str
    auto_timeout_reason: str
    auto_exception_reason: str
    auto_unknown_reason: str
    left_group_cancel_reason: str
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
    """供本地分类器和 LLM 分类器使用的一条预设问答。"""

    question: str
    answer: str
    aliases: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QAStrings:
    """从 strings.yaml 读取的提示词和预设问答内容。"""

    llm_system_prompt: str
    llm_user_prompt: str
    preset_answers: list[PresetAnswer] = field(default_factory=list)


@dataclass(frozen=True)
class BotStrings:
    """从 strings.yaml 读取的所有对外文案和命令词。"""

    realname: RealNameStrings
    qa: QAStrings


def load_yaml(path: str | Path) -> dict[str, Any]:
    """读取 YAML 映射；如果根节点不是映射则提前报错。"""

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"{config_path} not found.")
    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a YAML object at the top level.")
    return raw


def load_secrets(path: str | Path = ".secrets") -> dict[str, Any]:
    """Load optional local secrets without requiring the file in deployments."""

    secrets_path = Path(path)
    if not secrets_path.exists():
        return {}
    return load_yaml(secrets_path)


def load_config(path: str | Path = "config.yaml") -> BotConfig:
    """从 config.yaml 加载运行配置。"""

    raw = load_yaml(path)
    secrets = load_secrets()
    napcat = raw.get("napcat", {}) or {}
    groups = raw.get("groups", {}) or {}
    realname = raw.get("realname", {}) or {}
    archive = raw.get("message_archive", {}) or {}
    qa = raw.get("qa", {}) or {}
    llm = qa.get("llm", {}) or {}
    storage = raw.get("storage", {}) or {}
    napcat_secret = secrets.get("napcat", {}) or {}
    qa_secret = secrets.get("qa", {}) or {}
    llm_secret = qa_secret.get("llm", {}) or {}

    return BotConfig(
        napcat=NapCatConfig(
            ws_url=str(napcat.get("ws_url", "ws://127.0.0.1:3001/")),
            access_token=str(napcat_secret.get("access_token", napcat.get("access_token", "")) or ""),
            reconnect_seconds=int(napcat.get("reconnect_seconds", 5)),
            send_message_delay_seconds=float(napcat.get("send_message_delay_seconds", 1)),
        ),
        groups=GroupConfig(
            recruit_group=int(groups.get("recruit_group", 810192062)),
            admin_group=int(groups.get("admin_group", 1065588188)),
        ),
        realname=RealNameConfig(
            enabled=bool(realname.get("enabled", True)),
            one_qq_one_identity=bool(realname.get("one_qq_one_identity", True)),
            mute_duration_seconds=int(realname.get("mute_duration_seconds", 90 * 24 * 60 * 60)),
            admin_approvers={int(x) for x in realname.get("admin_approvers", [])},
            auto_review=AutoReviewConfig(
                module_path=Path((realname.get("auto_review") or {}).get("module_path", "realname_reviewer.py")),
                function_name=str((realname.get("auto_review") or {}).get("function_name", "review_application")),
                timeout_seconds=int((realname.get("auto_review") or {}).get("timeout_seconds", 30)),
            ),
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
                api_key=str(llm_secret.get("api_key", llm.get("api_key", "")) or ""),
                model=str(llm.get("model", "gpt-4o-mini")),
                timeout_seconds=int(llm.get("timeout_seconds", 20)),
            ),
        ),
        storage=StorageConfig(data_dir=Path(storage.get("data_dir", "data"))),
    )


def load_strings(path: str | Path = "strings.yaml") -> BotStrings:
    """从 strings.yaml 加载命令词、回复模板和预设问答。"""

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
            verified_join_prompt=str(realname["verified_join_prompt"]),
            resubmit_prompt=str(realname["resubmit_prompt"]),
            invalid_format=str(realname["invalid_format"]),
            already_verified=str(realname["already_verified"]),
            duplicate_identity=str(realname["duplicate_identity"]),
            duplicate_active_application=str(realname["duplicate_active_application"]),
            auto_reviewing=str(realname["auto_reviewing"]),
            submitted=str(realname["submitted"]),
            manual_handoff=str(realname["manual_handoff"]),
            auto_reject_reason=str(realname["auto_reject_reason"]),
            auto_timeout_reason=str(realname["auto_timeout_reason"]),
            auto_exception_reason=str(realname["auto_exception_reason"]),
            auto_unknown_reason=str(realname["auto_unknown_reason"]),
            left_group_cancel_reason=str(realname["left_group_cancel_reason"]),
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
