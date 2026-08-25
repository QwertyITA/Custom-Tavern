"""Inline markup tokenizer (§8).

Dialogue vs action is *not* a stored message type — messages hold raw prose and
this parses it at render time. It is a real scanner, not a regex sweep, because
markup nests and interleaves freely and models emit unbalanced markers
constantly. The design rule is fail-soft: a stray `*` degrades to a literal
asterisk and never bleeds its colour across the rest of the message.

Output is a flat list of runs, each carrying the *set* of styles active over it.
A flat run list (rather than a tree) is what makes crossing spans like
`*a "b* c"` render sensibly instead of throwing.

The mirror implementation in `static/markup.js` must stay behaviourally
identical; `tests/fixtures/markup_cases.json` is the shared contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DIALOGUE = "dialogue"
ACTION = "action"
STRONG = "strong"

STYLE_ORDER = (DIALOGUE, ACTION, STRONG)

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")


@dataclass(frozen=True)
class Run:
    text: str
    styles: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"text": self.text, "styles": list(self.styles)}


@dataclass
class _Marker:
    style: str
    start: int
    end: int  # exclusive
    can_open: bool
    can_close: bool
    keep: bool  # quotes stay visible; asterisks are consumed
    force: str = ""  # "open" / "close" for directional quotes


def split_paragraphs(text: str) -> list[tuple[int, int]]:
    """Spans of text between blank-line breaks, in the original text's own
    coordinates. Delimiters only pair inside one — the main guard against a
    single stray marker capturing the whole message, the damage bounded to
    the paragraph it appears in — and it's also what "paragraph" means to
    reply_length.py's cut, so a reply and the count judging it agree on
    where one paragraph ends and the next begins.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        spans.append((cursor, match.start()))
        cursor = match.end()
    spans.append((cursor, len(text)))
    return [(a, b) for a, b in spans if b > a]


def _flanking(text: str, start: int, end: int) -> tuple[bool, bool]:
    prev_char = text[start - 1] if start > 0 else ""
    next_char = text[end] if end < len(text) else ""
    can_open = bool(next_char) and not next_char.isspace()
    can_close = bool(prev_char) and not prev_char.isspace()
    return can_open, can_close


def _lex(text: str, start: int, end: int) -> list[_Marker]:
    markers: list[_Marker] = []
    i = start
    while i < end:
        char = text[i]
        if char == "*":
            run_end = i
            while run_end < end and text[run_end] == "*":
                run_end += 1
            count = run_end - i
            can_open, can_close = _flanking(text, i, run_end)
            # Greedy: consume pairs as STRONG, a lone leftover as ACTION.
            cursor = i
            while count >= 2:
                markers.append(
                    _Marker(STRONG, cursor, cursor + 2, can_open, can_close, keep=False)
                )
                cursor += 2
                count -= 2
            if count == 1:
                markers.append(
                    _Marker(ACTION, cursor, cursor + 1, can_open, can_close, keep=False)
                )
            i = run_end
            continue
        if char in '"“”':
            can_open, can_close = _flanking(text, i, i + 1)
            force = ""
            if char == "“":
                force, can_open, can_close = "open", True, False
            elif char == "”":
                force, can_open, can_close = "close", False, True
            markers.append(
                _Marker(DIALOGUE, i, i + 1, can_open, can_close, keep=True, force=force)
            )
            i += 1
            continue
        i += 1
    return markers


def _pair(markers: list[_Marker]) -> list[tuple[str, int, int, list[_Marker]]]:
    """Resolve markers into spans. Unmatched markers are simply left literal."""
    stacks: dict[str, list[_Marker]] = {DIALOGUE: [], ACTION: [], STRONG: []}
    spans: list[tuple[str, int, int, list[_Marker]]] = []
    for marker in markers:
        stack = stacks[marker.style]
        if marker.can_close and stack:
            opener = stack.pop()
            if marker.keep:
                spans.append((marker.style, opener.start, marker.end, []))
            else:
                spans.append((marker.style, opener.end, marker.start, [opener, marker]))
        elif marker.can_open:
            stack.append(marker)
        # else: neither — `2 * 3`, a bare quote in whitespace. Stays literal.
    return spans


def parse(text: str) -> list[Run]:
    """Tokenize raw message text into styled runs."""
    if not text:
        return []

    length = len(text)
    styles: list[set[str]] = [set() for _ in range(length)]
    hidden = [False] * length

    for para_start, para_end in split_paragraphs(text):
        for style, span_start, span_end, consumed in _pair(_lex(text, para_start, para_end)):
            for index in range(max(0, span_start), min(length, span_end)):
                styles[index].add(style)
            for marker in consumed:
                for index in range(marker.start, marker.end):
                    hidden[index] = True

    runs: list[Run] = []
    buffer: list[str] = []
    current: tuple[str, ...] | None = None
    for index, char in enumerate(text):
        if hidden[index]:
            continue
        active = tuple(s for s in STYLE_ORDER if s in styles[index])
        if current is None or active != current:
            if buffer:
                runs.append(Run("".join(buffer), current or ()))
            buffer = []
            current = active
        buffer.append(char)
    if buffer:
        runs.append(Run("".join(buffer), current or ()))
    return [run for run in runs if run.text]


def repair_markup(text: str) -> str:
    """Delete asterisks the tokenizer could not pair (§8).

    Models lose count of them constantly, and one shape dominates: the reply
    closes an attribution and then closes again at the end of a clause it never
    opened —

        *she says through clenched teeth,* tail whipping once behind her leg.*

    The tokenizer fails soft and leaves that last one as a literal asterisk on
    screen, which is right for rendering and wrong to keep. Half the replies in
    the chat this was written against carried at least one.

    The rule is the tokenizer's own, so the two cannot disagree: a marker that
    could have opened or closed something but found no partner is a mistake and
    goes. One that could do neither — `2 * 3`, a lone star with space on both
    sides — was never markup and stays exactly where it is.
    """
    if "*" not in text:
        return text
    doomed: set[int] = set()
    for para_start, para_end in split_paragraphs(text):
        markers = [m for m in _lex(text, para_start, para_end) if not m.keep]
        paired: set[int] = set()
        for _style, _start, _end, consumed in _pair(markers):
            for marker in consumed:
                paired.add(marker.start)
        for marker in markers:
            if marker.start in paired:
                continue
            if marker.can_open or marker.can_close:
                doomed.update(range(marker.start, marker.end))
    if not doomed:
        return text
    return "".join(c for i, c in enumerate(text) if i not in doomed)


def to_plain(text: str) -> str:
    """Markup-stripped text — for token counting and keyword scans."""
    return "".join(run.text for run in parse(text))


def parse_to_dicts(text: str) -> list[dict]:
    return [run.to_dict() for run in parse(text)]
