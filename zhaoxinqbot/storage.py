from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class JsonStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "messages").mkdir(exist_ok=True)
        (self.data_dir / "media").mkdir(exist_ok=True)
        self.realname_path = self.data_dir / "realname.json"
        self.membership_path = self.data_dir / "membership_events.jsonl"
        self.message_index_path = self.data_dir / "message_index.json"

    def load_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def save_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)

    def append_jsonl(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
            f.write("\n")

    def load_realnames(self) -> dict[str, Any]:
        return self.load_json(
            self.realname_path,
            {"verified": {}, "pending": {}, "rejected": {}, "revoked": {}},
        )

    def save_realnames(self, data: dict[str, Any]) -> None:
        self.save_json(self.realname_path, data)

    def load_message_index(self) -> dict[str, str]:
        return self.load_json(self.message_index_path, {})

    def save_message_index(self, data: dict[str, str]) -> None:
        self.save_json(self.message_index_path, data)

    def message_path(self, group_id: int, message_id: int | str) -> Path:
        group_dir = self.data_dir / "messages" / str(group_id)
        group_dir.mkdir(parents=True, exist_ok=True)
        return group_dir / f"{message_id}.json"

    def media_path(self, group_id: int, message_id: int | str, index: int, name: str) -> Path:
        suffix = Path(name).suffix or ".bin"
        safe_suffix = "".join(ch for ch in suffix if ch.isalnum() or ch == ".")[:16] or ".bin"
        media_dir = self.data_dir / "media" / str(group_id) / str(message_id)
        media_dir.mkdir(parents=True, exist_ok=True)
        return media_dir / f"{index}{safe_suffix}"
