from __future__ import annotations

import asyncio
import json
from difflib import SequenceMatcher
from typing import Any

import aiohttp

from .config import QAConfig
from .napcat import NapCatClient
from .realname import extract_text


class QuestionAnswerer:
    def __init__(self, config: QAConfig, client: NapCatClient):
        self.config = config
        self.client = client

    async def on_group_message(self, event: dict[str, Any]) -> None:
        if not self.config.enabled:
            return
        text = extract_text(event.get("message", event.get("raw_message", ""))).strip()
        if not text:
            return

        answer = await self.match_question(text)
        if not answer:
            return

        sent_id = await self.client.send_group_msg(int(event["group_id"]), answer)
        if sent_id is not None and self.config.recall_after_seconds > 0:
            asyncio.create_task(self._recall_later(sent_id))

    async def match_question(self, text: str) -> str | None:
        if self.config.llm.enabled and self.config.llm.api_key:
            answer = await self.match_question_llm(text)
            if answer is not None:
                return answer
        return self.match_question_builtin(text)

    async def match_question_llm(self, text: str) -> str | None:
        options = [
            {"index": index, "question": item.question, "aliases": item.aliases}
            for index, item in enumerate(self.config.preset_answers)
        ]
        if not options:
            return None

        prompt = (
            "判断用户消息是否属于预设问题之一。只返回 JSON，格式："
            "{\"index\": 数字或 -1, \"confidence\": 0到1}。\n"
            f"用户消息：{text}\n"
            f"预设问题：{json.dumps(options, ensure_ascii=False)}"
        )
        payload = {
            "model": self.config.llm.model,
            "messages": [
                {"role": "system", "content": "你是严格的问题分类器。不要回答问题，只做分类。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        }
        headers = {"Authorization": f"Bearer {self.config.llm.api_key}", "Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.config.llm.api_url,
                    json=payload,
                    headers=headers,
                    timeout=self.config.llm.timeout_seconds,
                ) as resp:
                    resp.raise_for_status()
                    body = await resp.json()
            content = body["choices"][0]["message"]["content"]
            result = json.loads(content)
            index = int(result.get("index", -1))
            confidence = float(result.get("confidence", 0))
            if 0 <= index < len(self.config.preset_answers) and confidence >= self.config.confidence_threshold:
                return self.config.preset_answers[index].answer
        except Exception as exc:
            print(f"[qa] llm classify failed, fallback to builtin matcher: {exc}")
        return None

    def match_question_builtin(self, text: str) -> str | None:
        normalized = normalize_text(text)
        best_score = 0.0
        best_answer: str | None = None
        for item in self.config.preset_answers:
            candidates = [item.question, *item.aliases]
            for candidate in candidates:
                score = SequenceMatcher(None, normalized, normalize_text(candidate)).ratio()
                if normalize_text(candidate) in normalized:
                    score = max(score, 0.95)
                if score > best_score:
                    best_score = score
                    best_answer = item.answer

        if best_score >= self.config.confidence_threshold:
            return best_answer
        return None

    async def _recall_later(self, message_id: int | str) -> None:
        await asyncio.sleep(self.config.recall_after_seconds)
        try:
            await self.client.delete_msg(message_id)
        except Exception as exc:
            print(f"[qa] failed to recall answer {message_id}: {exc}")


def normalize_text(text: str) -> str:
    return "".join(ch for ch in text.lower().strip() if not ch.isspace())
