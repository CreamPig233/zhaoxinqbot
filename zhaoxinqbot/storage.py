"""简单的 JSON/JSONL 持久化层。

机器人把长期状态保存为 data 目录下的普通文件，便于查看、备份和迁移。
JSON 文件通过临时文件加替换实现原子写入，追加型日志使用 JSON Lines。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """返回带时区信息的本地 ISO 时间戳。"""

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class JsonStore:
    """基于文件系统的机器人状态存储助手。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "messages").mkdir(exist_ok=True)
        (self.data_dir / "media").mkdir(exist_ok=True)
        self.realname_path = self.data_dir / "realname.json"
        self.realname_applications_path = self.data_dir / "realname_applications.jsonl"
        self.membership_path = self.data_dir / "membership_events.jsonl"
        self.message_index_path = self.data_dir / "message_index.json"

    def load_json(self, path: Path, default: Any) -> Any:
        """读取 JSON 文件；文件不存在时返回 default。"""

        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def save_json(self, path: Path, data: Any) -> None:
        """以 UTF-8 和可读缩进原子写入 JSON 文件。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)

    def append_jsonl(self, path: Path, data: dict[str, Any]) -> None:
        """向追加型日志文件写入一行 JSON 对象。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
            f.write("\n")

    def load_realnames(self) -> dict[str, Any]:
        """读取实名状态的各个分区。"""

        return self.load_json(
            self.realname_path,
            {
                "verified": {},
                "pending": {},
                "rejected": {},
                "revoked": {},
                "applications": {},
                "active_by_user": {},
                "review_messages": {},
            },
        )

    def save_realnames(self, data: dict[str, Any]) -> None:
        """保存实名状态的各个分区。"""

        self.save_json(self.realname_path, data)

    def load_message_index(self) -> dict[str, str]:
        """读取撤回处理用的 message_id 到归档路径索引。"""

        return self.load_json(self.message_index_path, {})

    def save_message_index(self, data: dict[str, str]) -> None:
        """保存 message_id 到归档路径索引。"""

        self.save_json(self.message_index_path, data)

    def message_path(self, group_id: int, message_id: int | str) -> Path:
        """返回某条群消息对应的 JSON 归档路径。"""

        group_dir = self.data_dir / "messages" / str(group_id)
        group_dir.mkdir(parents=True, exist_ok=True)
        return group_dir / f"{message_id}.json"

    def media_path(self, group_id: int, message_id: int | str, index: int, name: str) -> Path:
        """返回某个消息段对应的安全本地媒体路径。"""

        suffix = Path(name).suffix or ".bin"
        safe_suffix = "".join(ch for ch in suffix if ch.isalnum() or ch == ".")[:16] or ".bin"
        media_dir = self.data_dir / "media" / str(group_id) / str(message_id)
        media_dir.mkdir(parents=True, exist_ok=True)
        return media_dir / f"{index}{safe_suffix}"
