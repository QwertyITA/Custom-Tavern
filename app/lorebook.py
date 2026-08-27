"""Lorebook — keyword-triggered context injection (§7.4).

The classic "World Info", kept deliberately distinct from the Scene Tracker
(§10): this is authored static knowledge, that is a generated live panel.

Matching is whole-word to stop "art" from firing on "start", which is the
failure mode that makes keyword lorebooks feel random.
"""

from __future__ import annotations

import re

from .markup import to_plain
from .models import Character, LorebookEntry
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


# ~4 chars/token, the same estimate `estimate_tokens` inverts (§ providers.base).
_CHARS_PER_TOKEN = 4


def _capped(entry: LorebookEntry) -> str:
    """This entry's own content, cut to its own `token_budget` if it runs over.

    `scan`'s budget accounting already charges an oversized entry only
    `entry.token_budget` against the total — the per-entry cap this field
    documents (§7.4, DESIGN.md) — so the rendered text has to actually be
    that short too, or the total a chat is charged for and the total it is
    sent stop agreeing with each other, which is how one entry ends up
    spending three or four times its declared share.
    """
    text = entry.content.strip()
    limit = entry.token_budget * _CHARS_PER_TOKEN
    if limit <= 0 or len(text) <= limit:
        return text
    # Back off to the last whole word inside the limit rather than splitting
    # one in half; a lorebook entry is usually a single dense paragraph with
    # nowhere else to look for a clean edge.
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > 0 else limit].rstrip()


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
    return "\n".join(_capped(entry) for entry in entries if entry.content.strip())


def split_by_constancy(
    entries: list[LorebookEntry],
) -> tuple[list[LorebookEntry], list[LorebookEntry]]:
    """Constant entries belong in the cache-friendly prefix; hits do not (§7.1)."""
    return (
        [e for e in entries if e.constant],
        [e for e in entries if not e.constant],
    )


# --------------------------------------------------------- misattribution


# A single Title-Case token — the shape of a proper name, not a trait word or
# a sentence fragment. Deliberately narrow: this only ever adds a warning, so
# missing a name shaped like something else costs nothing, while a keyword
# wrongly read as a name would.
_NAME_SHAPED = re.compile(r"^[A-Z][a-zA-Z'-]+$")


def possible_misattributions(character: Character) -> list[LorebookEntry]:
    """Keyed entries that may describe someone other than this card's own
    character while still writing `{{char}}` for themselves.

    KNOWN-ISSUES.md, "A card's lorebook can misattribute another character's
    traits": a card can carry entries keyed on a different named character
    (a shared world, several demi-humans one owner keeps) whose own text
    lazily writes `{{char}}` instead of that character's actual name — which
    macro expansion (correctly) resolves to whoever this chat is actually
    about, attributing the traits fluently and wrongly. That fix is the
    card's content, which only whoever wrote or assembled the book can
    actually judge (same note) — this is a warning toward that judgement,
    not a correction of it.

    Heuristic, not proof, and deliberately conservative: flags a non-constant
    entry only when (a) its content uses the literal `{{char}}` macro, (b) at
    least one of its own keys is name-shaped and is not this character's own
    name, and (c) that key never actually appears as text anywhere in the
    entry's body — the specific shape of "this entry is keyed on a name it
    never once writes, because {{char}} is standing in for it throughout."
    An entry that names the other person alongside {{char}} — a legitimate
    scene between the two of them — does not trip this; constant entries,
    which this app's own convention already treats as always being about the
    chat's own character, are never checked.
    """
    own_name = character.name.strip().lower()
    flagged: list[LorebookEntry] = []
    for entry in character.lorebook:
        if entry.constant or not entry.content.strip():
            continue
        content_l = entry.content.lower()
        if "{{char}}" not in content_l:
            continue
        for key in entry.keys:
            key = key.strip()
            if not key or not _NAME_SHAPED.match(key):
                continue
            key_l = key.lower()
            if not own_name or key_l in own_name or own_name in key_l:
                continue  # a name or nickname for the character itself
            if key_l in content_l:
                continue  # named directly in the body too — not lazy {{char}}
            flagged.append(entry)
            break
    return flagged
