"""Lorebook — keyword-triggered context injection (§7.4).

The classic "World Info", kept deliberately distinct from the Scene Tracker
(§10): this is authored static knowledge, that is a generated live panel.

Matching is whole-word to stop "art" from firing on "start", which is the
failure mode that makes keyword lorebooks feel random.
"""

from __future__ import annotations

import re

from .markup import to_plain
from .models import LorebookEntry
from .providers.base import estimate_tokens


def _pattern(key: str, case_sensitive: bool) -> re.Pattern[str]:
    flags = 0 if case_sensitive else re.IGNORECASE
    # Word boundaries only where the key edge is a word character, so keys like
    # "the King's men" or ":wave:" still match.
    left = r"\b" if key[:1].isalnum() else ""
    right = r"\b" if key[-1:].isalnum() else ""
    return re.compile(left + re.escape(key) + right, flags)


def matches(entry: LorebookEntry, haystack: str) -> bool:
    return any(_pattern(key, entry.case_sensitive).search(haystack) for key in entry.keys if key)


def scan(
    entries: list[LorebookEntry],
    recent_texts: list[str],
    *,
    scan_depth: int = 6,
    total_budget: int = 600,
) -> list[LorebookEntry]:
    """Return the entries to inject, constants first, within the token budget."""
    haystack = to_plain("\n".join(recent_texts[-scan_depth:] if scan_depth else recent_texts))

    selected: list[LorebookEntry] = []
    triggered: list[LorebookEntry] = []
    for entry in entries:
        if not entry.enabled or not entry.content.strip():
            continue
        if entry.constant:
            selected.append(entry)
        elif matches(entry, haystack):
            triggered.append(entry)

    # Constants are the authored baseline and win the budget over keyword hits.
    ordered = selected + sorted(triggered, key=lambda e: e.insertion_depth)
    kept: list[LorebookEntry] = []
    spent = 0
    for entry in ordered:
        cost = min(estimate_tokens(entry.content), entry.token_budget)
        if spent + cost > total_budget:
            continue
        kept.append(entry)
        spent += cost
    return kept


def render(entries: list[LorebookEntry]) -> str:
    return "\n".join(entry.content.strip() for entry in entries if entry.content.strip())


def split_by_constancy(
    entries: list[LorebookEntry],
) -> tuple[list[LorebookEntry], list[LorebookEntry]]:
    """Constant entries belong in the cache-friendly prefix; hits do not (§7.1)."""
    return (
        [e for e in entries if e.constant],
        [e for e in entries if not e.constant],
    )
