"""Pass I/O contract (§5.6).

Pass 1 streams clean prose and then emits a delimited suffix carrying the rough
deltas and signals:

    *She looks up.* "You're late."
    <<<state>>>{"deltas": {"willingness": -1}, "signals": {"narrative_drive": "minor"}}

Streaming and structure therefore coexist without a second round trip. The
marker must never reach the screen, including when it arrives split across
stream chunks — hence the filter below holds back a short tail.

Structured (non-reply) passes just return JSON, which models wrap in prose and
code fences often enough that parsing has to be lenient.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..models import SIGNAL_LEVELS

MARKER = "<<<state>>>"

# The marker as it actually arrives. Small models mangle the delimiter while
# keeping the shape — `***state>>>` was observed in the wild, and the reply that
# produced it had its whole state payload printed in the message because an
# exact match found nothing. Anything bracket-ish or starred around the word,
# closed by two or more of anything pointy, counts.
MARKER_LIKE = re.compile(
    r"[<*_\-#]{2,6}\s{0,2}state\s{0,2}[>*_\-#]{2,6}", re.IGNORECASE
)
# What the stream filter has to hold back to be sure a marker is not straddling
# two chunks: a little more than the longest thing MARKER_LIKE can match.
MARKER_HOLD = 24

# The contract as told to the model. Lives here, next to the parser that has to
# survive whatever the model does with it.
REPLY_SUFFIX_MARKER_HELP = (
    f"After the reply — and only after it — emit one line beginning with {MARKER} "
    "followed by a single JSON object:\n"
    f'{MARKER}{{"deltas": {{"<variable>": <integer change from -3 to 3>}}, '
    '"signals": {"narrative_drive": "none|minor|major", '
    '"emotional_shift": "none|minor|major", "scene_change": "none|minor|major"}}\n'
    "Report only variables that actually moved. Signals are one of exactly none, "
    "minor or major — never a number. Write nothing after the JSON."
)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_loose(text: str) -> dict[str, Any] | None:
    """Best-effort JSON out of model prose. Returns None if nothing parses."""
    if not text:
        return None
    candidates: list[str] = [text.strip()]
    for match in _FENCE.finditer(text):
        candidates.append(match.group(1).strip())
    # Outermost brace pair — handles "Sure! {...}" and trailing commentary.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"items": parsed}
    return None


def _after_json(text: str) -> str:
    """Whatever follows the first complete JSON object in `text`.

    Brace-counting rather than a regex, because the payload contains nested
    objects and a lazy match stops at the first inner `}`. Strings are tracked
    so a brace inside one does not close the object.
    """
    start = text.find("{")
    if start == -1:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 :]
    return ""


def split_state_suffix(text: str) -> tuple[str, dict[str, Any] | None]:
    """Split a completed reply into (visible prose, state payload).

    The contract asks for the marker *after* the reply, and small models often
    put it first instead — it is the last instruction in the system prompt and
    so the most salient thing in it. Taking `text[:index]` alone then throws the
    entire reply away and leaves the turn blank, which is one of the two ways
    this used to hand back nothing at all.

    So prose on both sides of the payload counts. Anything before the marker,
    plus anything after the JSON object that follows it.
    """
    match = MARKER_LIKE.search(text)
    if match is None:
        return text, None
    before = text[: match.start()]
    rest = text[match.end() :]
    payload = parse_json_loose(rest)
    after = _after_json(rest) if payload is not None else ""
    body = (before + ("\n" if before.strip() and after.strip() else "") + after)
    return body, payload


def normalise_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce a suffix payload into {deltas, signals, ...}, dropping junk.

    Signals are rubric levels (§5.2). A model that answers `0.7` instead of
    `minor` gets mapped onto the ladder rather than rejected — the pass still
    produced usable information.
    """
    result: dict[str, Any] = {"deltas": {}, "signals": {}, "extra": {}}
    if not isinstance(payload, dict):
        return result

    deltas = payload.get("deltas")
    if isinstance(deltas, dict):
        for name, value in deltas.items():
            try:
                result["deltas"][str(name)] = float(value)
            except (TypeError, ValueError):
                continue

    signals = payload.get("signals")
    if isinstance(signals, dict):
        for name, value in signals.items():
            level = coerce_signal(value)
            if level is not None:
                result["signals"][str(name)] = level

    for key, value in payload.items():
        if key not in ("deltas", "signals"):
            result["extra"][key] = value
    return result


def coerce_signal(value: Any) -> str | None:
    """Map whatever the model said onto none|minor|major."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in SIGNAL_LEVELS:
            return lowered
        aliases = {
            "low": "minor", "medium": "minor", "moderate": "minor",
            "high": "major", "significant": "major", "large": "major",
            "": "none", "no": "none", "false": "none", "nil": "none",
        }
        if lowered in aliases:
            return aliases[lowered]
        try:
            value = float(lowered)
        except ValueError:
            return None
    if isinstance(value, bool):
        return "major" if value else "none"
    if isinstance(value, (int, float)):
        if value <= 0.15:
            return "none"
        return "minor" if value < 0.6 else "major"
    return None


def signal_rank(level: str) -> int:
    try:
        return SIGNAL_LEVELS.index(level)
    except ValueError:
        return 0


class SuffixStreamFilter:
    """Emits reply text while withholding the `<<<state>>>` suffix.

    A naive `if MARKER in delta` misses the marker when it straddles two chunks,
    which is exactly how it usually arrives. So the filter keeps the last
    len(MARKER)-1 characters back until they are proven safe to release.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._tail = ""  # text after the marker
        self._found = False

    def feed(self, delta: str) -> str:
        if self._found:
            self._tail += delta
            return ""
        self._buffer += delta
        match = MARKER_LIKE.search(self._buffer)
        if match is not None:
            self._found = True
            emit = self._buffer[: match.start()]
            self._tail = self._buffer[match.end() :]
            self._buffer = ""
            return emit
        # Hold back anything that could still turn out to be the marker's head.
        if len(self._buffer) <= MARKER_HOLD:
            return ""
        emit = self._buffer[:-MARKER_HOLD]
        self._buffer = self._buffer[-MARKER_HOLD:]
        return emit

    def finish(self) -> tuple[str, dict[str, Any] | None]:
        """Flush the held-back tail and parse the payload.

        When the marker arrived first — which small models do, because the
        contract is the last thing they read — the reply is sitting *after* the
        JSON in the tail. It cannot be streamed, since nothing could tell it
        from the payload until the object closed, so it arrives in one piece at
        the end. That is worse than streaming and enormously better than the
        blank reply this used to produce.
        """
        if self._found:
            payload = parse_json_loose(self._tail)
            after = _after_json(self._tail) if payload is not None else ""
            return after, payload
        remainder = self._buffer
        self._buffer = ""
        # A marker can still be sitting entirely inside the held-back tail.
        body, payload = split_state_suffix(remainder)
        return body, payload
