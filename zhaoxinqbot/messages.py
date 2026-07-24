"""Local message archive and recall marker support.

The archive writes one JSON file per group message and optionally stores media
files beside it. When a group recall notice arrives, the existing JSON record is
updated with recall metadata instead of being removed.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .napcat import NapCatClient
from .storage import JsonStore, utc_now_iso


MEDIA_TYPES = {"image", "record", "video", "file"}


class MessageArchive:
    """Persist group message events and mark recalled messages."""

    def __init__(self, store: JsonStore, client: NapCatClient, download_media: bool = True):
        self.store = store
        self.client = client
        self.download_media = download_media
        self.index = self.store.load_message_index()

    async def record_group_message(self, event: dict[str, Any]) -> None:
        """Save the raw group message event and any retrievable media files."""

        message_id = event.get("message_id")
        group_id = event.get("group_id")
        if message_id is None or group_id is None:
            return

        record = {
            "archived_at": utc_now_iso(),
            "recalled": False,
            "recall": None,
            "event": event,
            "media_files": [],
        }
        if self.download_media:
            record["media_files"] = await self._archive_media(int(group_id), message_id, event.get("message", []))

        path = self.store.message_path(int(group_id), message_id)
        self.store.save_json(path, record)
        self.index[str(message_id)] = str(path)
        self.store.save_message_index(self.index)

    async def mark_recalled(self, event: dict[str, Any]) -> None:
        """Append recall metadata to a message archive record."""

        message_id = str(event.get("message_id", ""))
        if not message_id:
            return

        path_text = self.index.get(message_id)
        if path_text:
            path = Path(path_text)
        else:
            group_id = int(event.get("group_id", 0))
            path = self.store.message_path(group_id, message_id)

        record = self.store.load_json(path, {"event": {}, "media_files": []})
        record["recalled"] = True
        record["recall"] = {
            "marked_at": utc_now_iso(),
            "group_id": event.get("group_id"),
            "message_id": event.get("message_id"),
            "sender_user_id": event.get("user_id"),
            "operator_id": event.get("operator_id"),
            "event_time": event.get("time"),
            "raw_event": event,
        }
        self.store.save_json(path, record)

    async def _archive_media(self, group_id: int, message_id: int | str, segments: Any) -> list[dict[str, Any]]:
        """Save every supported media segment and return metadata for the JSON record."""

        if not isinstance(segments, list):
            return []

        saved = []
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict) or segment.get("type") not in MEDIA_TYPES:
                continue
            data = segment.get("data") or {}
            local = await self._save_segment_media(group_id, message_id, index, segment["type"], data)
            if local:
                saved.append({"segment_index": index, "type": segment["type"], "path": str(local), "data": data})
        return saved

    async def _save_segment_media(
        self,
        group_id: int,
        message_id: int | str,
        index: int,
        media_type: str,
        data: dict[str, Any],
    ) -> Path | None:
        """Resolve and save one media segment to the local data directory."""

        source = await self._resolve_media_source(media_type, data)
        if not source:
            return None

        name = data.get("file") or data.get("file_id") or data.get("url") or f"{media_type}.bin"
        target = self.store.media_path(group_id, message_id, index, str(name))
        parsed = urlparse(source)
        try:
            if parsed.scheme in ("http", "https"):
                await self._download_url(source, target)
                return target
            source_path = Path(source)
            if source_path.exists():
                shutil.copyfile(source_path, target)
                return target
        except Exception as exc:
            print(f"[archive] media save failed for {media_type}: {exc}")
        return None

    async def _resolve_media_source(self, media_type: str, data: dict[str, Any]) -> str:
        """Find a URL or local path for a OneBot media segment."""

        if data.get("url"):
            return str(data["url"])
        if data.get("path"):
            return str(data["path"])

        try:
            if media_type == "image":
                info = await self.client.get_image(file=str(data.get("file", "")), file_id=str(data.get("file_id", "")))
            else:
                info = await self.client.get_file(file=str(data.get("file", "")), file_id=str(data.get("file_id", "")))
        except Exception as exc:
            print(f"[archive] media lookup failed for {media_type}: {exc}")
            return ""

        return str(info.get("url") or info.get("path") or info.get("file") or "")

    async def _download_url(self, url: str, target: Path) -> None:
        """Stream a remote media URL into ``target`` without loading it all at once."""

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as resp:
                resp.raise_for_status()
                with target.open("wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        f.write(chunk)
