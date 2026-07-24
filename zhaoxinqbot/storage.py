"""Small JSON/JSONL persistence layer.

The bot stores durable state as plain files under ``data/`` so it remains easy
to inspect, back up, and migrate. JSON files are written atomically through a
temporary file plus replace, while append-only membership logs use JSON Lines.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """Return a local-time ISO timestamp with timezone information."""

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class JsonStore:
    """Filesystem-backed storage helper for bot state."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "messages").mkdir(exist_ok=True)
        (self.data_dir / "media").mkdir(exist_ok=True)
        self.realname_path = self.data_dir / "realname.json"
        self.membership_path = self.data_dir / "membership_events.jsonl"
        self.message_index_path = self.data_dir / "message_index.json"

    def load_json(self, path: Path, default: Any) -> Any:
        """Load a JSON file, returning ``default`` when it does not exist."""

        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def save_json(self, path: Path, data: Any) -> None:
        """Atomically write a JSON file with UTF-8 and readable indentation."""

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)

    def append_jsonl(self, path: Path, data: dict[str, Any]) -> None:
        """Append one JSON object as a line to an append-only log file."""

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
            f.write("\n")

    def load_realnames(self) -> dict[str, Any]:
        """Load the real-name state buckets."""

        return self.load_json(
            self.realname_path,
            {"verified": {}, "pending": {}, "rejected": {}, "revoked": {}},
        )

    def save_realnames(self, data: dict[str, Any]) -> None:
        """Persist the real-name state buckets."""

        self.save_json(self.realname_path, data)

    def load_message_index(self) -> dict[str, str]:
        """Load the message_id-to-archive-path index used by recall handling."""

        return self.load_json(self.message_index_path, {})

    def save_message_index(self, data: dict[str, str]) -> None:
        """Persist the message_id-to-archive-path index."""

        self.save_json(self.message_index_path, data)

    def message_path(self, group_id: int, message_id: int | str) -> Path:
        """Return the JSON archive path for one group message."""

        group_dir = self.data_dir / "messages" / str(group_id)
        group_dir.mkdir(parents=True, exist_ok=True)
        return group_dir / f"{message_id}.json"

    def media_path(self, group_id: int, message_id: int | str, index: int, name: str) -> Path:
        """Return a safe local media path for one message segment."""

        suffix = Path(name).suffix or ".bin"
        safe_suffix = "".join(ch for ch in suffix if ch.isalnum() or ch == ".")[:16] or ".bin"
        media_dir = self.data_dir / "media" / str(group_id) / str(message_id)
        media_dir.mkdir(parents=True, exist_ok=True)
        return media_dir / f"{index}{safe_suffix}"
