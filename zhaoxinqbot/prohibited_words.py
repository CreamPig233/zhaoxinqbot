"""Detect configured prohibited words in group message text."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable


def normalize_for_matching(text: str) -> str:
    """Remove separators, punctuation, symbols, and whitespace for matching.

    This makes ``你+好``, ``你 好``, and ``你/好`` equivalent to ``你好`` while
    retaining letters and numbers that may be meaningful parts of a word.
    """

    return "".join(
        character
        for character in unicodedata.normalize("NFKC", text).casefold()
        if not unicodedata.category(character).startswith(("P", "S", "Z"))
    )


class ProhibitedWordMatcher:
    """Match configured words as normalized substrings of normalized text."""

    def __init__(self, words: Iterable[str]):
        self.words = tuple(
            normalized
            for word in words
            if (normalized := normalize_for_matching(str(word)))
        )

    def find(self, text: str) -> str | None:
        normalized_text = normalize_for_matching(text)
        if not normalized_text:
            return None
        return next((word for word in self.words if word in normalized_text), None)

