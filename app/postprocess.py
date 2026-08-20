"""Output post-processing (§13).

Part of anti-slop lives here rather than in the prompt, because prompting a
model not to write the user's next turn is unreliable and deleting it is not.
Runs on the reply *after* the state suffix has been split off.
"""

from __future__ import annotations

import re

# Template scaffolding a model sometimes emits verbatim.
_ARTIFACTS = [
    re.compile(r"<\|im_(start|end)\|>"),
    re.compile(r"<\|eot_id\|>"),
    re.compile(r"<\|(start|end)_header_id\|>"),
    re.compile(r"^\s*</s>\s*"),
    re.compile(r"\[/?INST\]"),
]

# "User:" / "{{user}}:" starting a line — the model continuing our turn.
_LEAKAGE = re.compile(
    r"\n\s*(?:\{\{user\}\}|user|you|human)\s*:.*\Z",
    re.IGNORECASE | re.DOTALL,
)

_THINK = re.compile(r"<think>(.*?)</think>\s*", re.IGNORECASE | re.DOTALL)
_OPEN_THINK = re.compile(r"<think>.*\Z", re.IGNORECASE | re.DOTALL)


def split_thinking(text: str) -> tuple[str, str]:
    """Pull `<think>` blocks out of the visible reply (§5.6).

    Reasoning models put their scratchpad inline. It is captured for the HUD and
    removed from display — never shown in the message stream.
    """
    thoughts = [m.group(1).strip() for m in _THINK.finditer(text)]
    body = _THINK.sub("", text)
    # An unterminated block means the stream was cut mid-thought; drop the tail
    # rather than showing it.
    if _OPEN_THINK.search(body):
        thoughts.append(_OPEN_THINK.search(body).group(0)[len("<think>") :].strip())
        body = _OPEN_THINK.sub("", body)
    return body, "\n\n".join(t for t in thoughts if t)


def clean_reply(text: str, *, strip_leakage: bool = True, user_names: tuple[str, ...] = ()) -> str:
    """Strip template artifacts and any continuation of the user's turn."""
    body = text
    for pattern in _ARTIFACTS:
        body = pattern.sub("", body)
    if strip_leakage:
        body = _LEAKAGE.sub("", body)
        for name in user_names:
            if not name.strip():
                continue
            named = re.compile(rf"\n\s*{re.escape(name)}\s*:.*\Z", re.IGNORECASE | re.DOTALL)
            body = named.sub("", body)
    # Collapse the runaway blank lines that stop-token truncation leaves behind.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


class ThinkStreamFilter:
    """Splits `<think>` reasoning out of a reply *while it streams* (§5.6).

    `split_thinking` can only run once the whole reply is in hand, which is too
    late for two things. The reasoning sat in the bubble for the length of the
    generation and then vanished when the finished text landed — the one thing
    §5.6 says must never happen — and while a model reasons no visible token
    arrives at all, so "thinking" and "the backend has not answered yet" looked
    identical from the outside.

    A naive `if "<think>" in delta` misses the tag when it straddles two chunks,
    which is how it usually arrives, so the same hold-back trick as
    `SuffixStreamFilter` is used: the last len(tag)-1 characters are kept until
    they are proven not to be the head of one.
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False
        # `<think>...</think>\s*` — the whitespace after the block is part of
        # what split_thinking removes, and a reply that streams in starting on
        # its third line is not what the model wrote.
        self._trim = False
        # Two blocks in one reply are joined the way split_thinking joins them,
        # and only once the second one turns out to have anything in it.
        self._seen = False
        self._gap = False

    @staticmethod
    def _find(haystack: str, needle: str) -> int:
        return haystack.lower().find(needle)

    def _emit(self, text: str) -> str:
        if not self._trim:
            return text
        stripped = text.lstrip()
        if stripped:
            self._trim = False
        return stripped

    def _thought(self, text: str) -> str:
        if not text:
            return text
        if self._gap:
            self._gap = False
            text = "\n\n" + text
        self._seen = True
        return text

    def feed(self, delta: str) -> tuple[str, str]:
        """One chunk in; the text to show and the reasoning that arrived out."""
        self._buffer += delta
        shown: list[str] = []
        thought: list[str] = []
        while True:
            tag = self._CLOSE if self._inside else self._OPEN
            index = self._find(self._buffer, tag)
            if index == -1:
                hold = len(tag) - 1
                if len(self._buffer) > hold:
                    part, self._buffer = self._buffer[:-hold], self._buffer[-hold:]
                    if self._inside:
                        thought.append(self._thought(part))
                    else:
                        shown.append(self._emit(part))
                break
            part = self._buffer[:index]
            if self._inside:
                thought.append(self._thought(part))
            else:
                shown.append(self._emit(part))
            self._buffer = self._buffer[index + len(tag) :]
            if self._inside:
                self._trim = True
            else:
                self._gap = self._seen
            self._inside = not self._inside
        return "".join(shown), "".join(thought)

    def finish(self) -> tuple[str, str]:
        """Flush the held-back tail: the same two halves, one last time.

        An unterminated block means the stream was cut mid-thought, so the tail
        is reasoning rather than reply — exactly what split_thinking decides
        about the same text.
        """
        rest, self._buffer = self._buffer, ""
        if self._inside:
            return "", self._thought(rest)
        return self._emit(rest), ""
