"""User-defined find/replace rules (§16).

Three scopes, and the difference between them is what they destroy:

    input    rewrites what you typed, before it is stored and sent
    output   rewrites the reply, before it is stored
    display  rewrites neither — only what is drawn on screen

`display` is the one to reach for. The other two are edits to the record: undo
them by changing the rule and the damage is already in the database. `display`
is a lens, and turning the rule off restores the text because the text was
never touched.

All three run in Python, server-side, including `display`. Running the display
scope in the browser would be a second regex engine with different escapes,
different group syntax and different Unicode rules, and this project already
knows what it costs to keep two tokenizers behaving identically.

**On dangerous patterns.** `re` cannot be interrupted, so a pattern that
backtracks catastrophically would hang the one process serving the phone. There
is no timeout to reach for without a new dependency. Instead: every pattern is
timed against a purpose-built adversarial string when it is saved, and one that
is slow there is refused. That catches the classic shapes — nested quantifiers
over an overlapping alternation — and is honest about being a filter rather
than a proof. A hard cap on input length at apply time is the second half.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

SCOPES: list[dict[str, str]] = [
    {"id": "display", "label": "How it looks",
     "note": "Changes only what is drawn. The message itself is untouched, so "
             "turning the rule off brings the original back."},
    {"id": "input", "label": "What you send",
     "note": "Rewrites your message before it is stored. This is an edit to "
             "the record, not a lens."},
    {"id": "output", "label": "What it replies",
     "note": "Rewrites the reply before it is stored. Also permanent."},
]

SCOPE_IDS = tuple(scope["id"] for scope in SCOPES)

ROLES = ("both", "user", "assistant")

# Guards. All three exist because this runs on a phone, in one process, with no
# way to interrupt a regex once it has started.
MAX_RULES = 40
MAX_TEXT = 40_000          # characters a rule will look at
PATTERN_BUDGET_MS = 25.0   # how long a pattern may take across the probes below

# Short on purpose. Catastrophic backtracking is exponential in the length of
# the run it chews on, so the probe has to be long enough for the blowup to
# register and short enough that it still *finishes* — the guard hanging on the
# very pattern it exists to catch is the obvious way to write this wrong.
#
# At twenty characters the separation is three orders of magnitude: the nasty
# shapes take 100-200ms here, ordinary patterns take under 0.2ms, and the
# budget sits between them with room to spare. Each probe is a different shape
# of trap: a plain run, an alternating run, and words with spaces.
PROBES = (
    "a" * 20,
    "ab" * 10,
    "aaa bbb ccc ddd eee f",
    # Ends in a character that no letter class and no anchor can match, so a
    # pattern like `([a-z]+)+$` is forced to try every partition of the twenty
    # letters before failing. Without this one it looks harmless, because every
    # other probe lets it succeed on the first attempt.
    "abcdefghijklmnopqrst!",
    # The same trap for a pattern written around one character rather than a
    # class. `(a+)+$` is *the* textbook catastrophic pattern and every probe
    # above let it through: the plain run matches it on the first attempt, and
    # the mixed ones hold too few a's to blow up on. Twenty a's and a wall took
    # 103ms here, against a 25ms budget; at forty thousand — the cap a rule is
    # actually applied under — it never returns, and `re` cannot be
    # interrupted, so the one process serving the phone is gone.
    "a" * 20 + "!",
    "x" * 20 + "!",
)


class RuleError(ValueError):
    """The rule cannot be used, with a reason worth showing."""


def new_rule_id() -> str:
    return uuid.uuid4().hex[:8]


def _flags(rule: dict) -> int:
    flags = 0
    if rule.get("ignore_case", True):
        flags |= re.IGNORECASE
    if rule.get("multiline"):
        flags |= re.MULTILINE
    if rule.get("dot_all"):
        flags |= re.DOTALL
    return flags


def normalise(raw: Any) -> list[dict[str, Any]]:
    """A clean, ordered rule list from whatever was stored."""
    rules: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or not str(entry.get("find") or ""):
            continue
        scope = str(entry.get("scope") or "display")
        role = str(entry.get("role") or "both")
        rules.append({
            "id": str(entry.get("id") or new_rule_id()),
            "label": str(entry.get("label") or "").strip() or "Rule",
            "find": str(entry.get("find")),
            "replace": str(entry.get("replace") or ""),
            "scope": scope if scope in SCOPE_IDS else "display",
            "role": role if role in ROLES else "both",
            "enabled": bool(entry.get("enabled", True)),
            "ignore_case": bool(entry.get("ignore_case", True)),
            "multiline": bool(entry.get("multiline", False)),
            "dot_all": bool(entry.get("dot_all", False)),
        })
        if len(rules) >= MAX_RULES:
            break
    return rules


def compile_rule(rule: dict) -> re.Pattern:
    try:
        return re.compile(rule["find"], _flags(rule))
    except re.error as exc:
        raise RuleError(f"not a valid pattern: {exc}") from exc


def check(rule: dict) -> None:
    """Refuse a rule that is invalid, or that is slow enough to be a hazard.

    Raises RuleError with something worth reading. Called on save, never in the
    hot path — by the time a rule is being applied it has already passed here.
    """
    pattern = compile_rule(rule)
    replace = str(rule.get("replace") or "")

    spent = 0.0
    for probe in PROBES:
        started = time.perf_counter()
        try:
            pattern.sub(replace, probe)
        except re.error as exc:
            raise RuleError(f"the replacement is not valid: {exc}") from exc
        spent += (time.perf_counter() - started) * 1000
        # Checked after each probe rather than at the end, so a pattern that
        # blows up on the first one does not go on to pay for the rest.
        if spent > PATTERN_BUDGET_MS:
            raise RuleError(
                f"this pattern took {spent:.0f}ms on a {len(probe)}-character test "
                "string, which means it can hang the app on a real message. "
                "Nested repeats like (a+)+ are the usual cause."
            )


def apply(rules: list[dict], text: str, scope: str, role: str = "assistant") -> str:
    """Run every enabled rule for this scope, in order.

    A rule that fails at this point is skipped rather than raised: it was
    checked when it was saved, so a failure here means something changed
    underneath, and losing the message would be a much worse answer than
    showing it unrewritten.
    """
    if not text or len(text) > MAX_TEXT:
        return text
    for rule in rules:
        if not rule.get("enabled", True) or rule.get("scope") != scope:
            continue
        if rule.get("role", "both") not in ("both", role):
            continue
        try:
            text = re.sub(rule["find"], rule["replace"], text, flags=_flags(rule))
        except re.error:
            continue
    return text


def preview(rule: dict, sample: str) -> dict[str, Any]:
    """Run one rule against a sample, for the editor's live result."""
    try:
        check(rule)
    except RuleError as exc:
        return {"ok": False, "error": str(exc), "result": sample, "matches": 0}
    pattern = compile_rule(rule)
    sample = sample[:MAX_TEXT]
    return {
        "ok": True,
        "error": "",
        "result": pattern.sub(str(rule.get("replace") or ""), sample),
        "matches": len(pattern.findall(sample)),
    }


def catalogue() -> dict[str, Any]:
    return {
        "scopes": [dict(scope) for scope in SCOPES],
        "roles": list(ROLES),
        "max_rules": MAX_RULES,
    }
