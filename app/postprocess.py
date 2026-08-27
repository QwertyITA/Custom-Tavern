"""Output post-processing (§13).

Part of anti-slop lives here rather than in the prompt, because prompting a
model not to write the user's next turn is unreliable and deleting it is not.
Runs on the reply *after* the state suffix has been split off.
"""

from __future__ import annotations

import re

from .markup import repair_markup, to_plain

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
# A closing tag with nothing to close. Ollama and llama.cpp serve reasoning
# models whose chat template writes the opening `<think>` itself, so the model
# only ever emits the closer — and then everything before it is reasoning that
# the paired pattern above cannot see. Two replies in the chat this was written
# against were stored with the model's entire plan in them, headings and all.
_STRAY_CLOSE = re.compile(r"\A(.*?)</think>\s*", re.IGNORECASE | re.DOTALL)

# Tags a model emits that this app does not render — it draws model output with
# textContent (§8), so an <img> or a </b> arrives on screen as its own source.
_HTML = re.compile(
    r"</?(?:b|i|u|s|em|strong|br|hr|p|div|span|font|img|small|sub|sup)\b[^>]*>",
    re.IGNORECASE,
)


def strip_unrenderable(text: str) -> str:
    """Take out the tags this app draws as their own source (§8)."""
    return _HTML.sub("", text)


# ------------------------------------------------------------ echoed phrase


_WORD = re.compile(r"[\w']+")

# Long enough that ordinary shared phrasing ("I don't know", "what do you
# mean") never trips it, short enough to catch an actual quoted sentence
# fragment. KNOWN-ISSUES.md's own measurement — one echo in 47 stored
# variants — was a full clause, well past this floor.
MIN_ECHO_WORDS = 6


def _words(text: str) -> list[str]:
    # to_plain first: a reply's *actions* shouldn't count against or toward
    # a match, and neither should the asterisks/quotes marking them (§8).
    return [w.lower() for w in _WORD.findall(to_plain(text))]


def find_echoed_phrase(reply: str, user_message: str, *, min_words: int = MIN_ECHO_WORDS) -> str:
    """The longest run of `min_words`+ words `reply` repeats verbatim from
    `user_message`, or "" if there is none (ISSUES-TRIAGE.md #15,
    KNOWN-ISSUES.md "A reply can quote the user's own turn back").

    Deliberately a flag, not a fix: an earlier look at this concluded that
    correcting it automatically risks being worse than the problem — a
    character legitimately repeating a phrase back on purpose is a real
    thing, and this cannot always tell the difference. So it only ever
    reports what it found; nothing here rewrites the reply.

    A plain contiguous-word-run comparison, not a fuzzy one: `min_words` in
    a row is specific enough that a false positive would need the reply to
    coincidentally reconstruct a whole clause of what was just said, which
    ordinary shared vocabulary does not do.
    """
    reply_words = _words(reply)
    user_words = _words(user_message)
    if len(reply_words) < min_words or len(user_words) < min_words:
        return ""

    # Every min_words-long run in the user's message, keyed to where it
    # starts — the shortest match this function will ever report, and the
    # anchor a real match is then grown from in both replies' word lists.
    starts_in_user: dict[tuple[str, ...], int] = {}
    for i in range(len(user_words) - min_words + 1):
        starts_in_user.setdefault(tuple(user_words[i : i + min_words]), i)

    best_len, best_start = 0, -1
    for i in range(len(reply_words) - min_words + 1):
        j = starts_in_user.get(tuple(reply_words[i : i + min_words]))
        if j is None:
            continue
        length = min_words
        while (
            i + length < len(reply_words)
            and j + length < len(user_words)
            and reply_words[i + length] == user_words[j + length]
        ):
            length += 1
        if length > best_len:
            best_len, best_start = length, i

    if best_start == -1:
        return ""
    return " ".join(reply_words[best_start : best_start + best_len])


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
    # A closer with no opener: the template wrote the opening tag, so the reply
    # starts mid-thought and everything up to the closer is reasoning.
    stray = _STRAY_CLOSE.match(body)
    if stray:
        thoughts.append(stray.group(1).strip())
        body = body[stray.end() :]
    return body, "\n\n".join(t for t in thoughts if t)


def clean_reply(text: str, *, strip_leakage: bool = True, user_names: tuple[str, ...] = ()) -> str:
    """Strip template artifacts and any continuation of the user's turn."""
    body = strip_unrenderable(text)
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
    # Last, so it sees the text as it will be read: an asterisk left dangling by
    # something removed above is as much a mistake as one the model dangled
    # itself (§8).
    return repair_markup(body.strip())


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
    # How much text may be taken back when a closing tag turns up with nothing
    # to close. A template that writes the opening tag itself means the whole
    # reply starts mid-thought, and the retraction happens within a few hundred
    # characters or not at all — past this a `</think>` is likelier to be
    # something a character typed than a tag.
    _RETRACT_LIMIT = 4000

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False
        # Everything released so far, kept only until it is too late to take
        # back. `retracted` says it *was* taken back, so a caller streaming to
        # somewhere else knows to throw its copy away too.
        self._shown = ""
        self.retracted = False
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
        if self._trim:
            text = text.lstrip()
            if text:
                self._trim = False
        if len(self._shown) < self._RETRACT_LIMIT:
            self._shown += text
        return text

    def _retract(self, tail: str) -> str:
        """Everything shown so far was reasoning after all. Hand it back."""
        self.retracted = True
        thought, self._shown = self._shown + tail, ""
        self._trim = True
        return self._thought(thought)

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
            if not self._inside and not self.retracted:
                # A closer before any opener: the chat template wrote the
                # opening tag, so everything up to here has been reasoning —
                # including whatever has already been released downstream.
                closing = self._find(self._buffer, self._CLOSE)
                if closing != -1 and (index == -1 or closing < index) and (
                    len(self._shown) < self._RETRACT_LIMIT
                ):
                    shown = []
                    thought = [self._retract("".join(thought) + self._buffer[:closing])]
                    self._buffer = self._buffer[closing + len(self._CLOSE) :]
                    continue
            if index == -1:
                # Long enough to catch either tag straddling two chunks.
                hold = len(self._CLOSE) - 1
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
