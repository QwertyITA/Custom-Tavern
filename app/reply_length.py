"""Cutting a reply down to the paragraph count craft:length asks for
(§ Settings.cut_excess_paragraphs, the "Cut excess paragraphs" toggle in
Brain → Prompt).

The prompt only ever *asks* the model to stop at a paragraph count — see
craft:length's own comment in prompt_layout.py — and that is a soft
constraint no model reliably obeys, especially once a conversation has a few
longer turns in it to pattern-match against. This is the hard backstop for
anyone who wants the ceiling actually enforced rather than just requested.

Runs once, right after a reply is written and before it is stored — nothing
downstream (the state passes, the summary, the next turn's prompt) ever sees
the excess. It is not thrown away, though: the caller keeps the full reply
alongside the cut one, so a single message can be restored to what the model
actually wrote (§ repo.add_message/add_variant's `full_text`, the message
action wheel's "Restore full length").
"""

from __future__ import annotations

import re

from .config import Settings
from .markup import split_paragraphs
from .prompt_layout import normalise

# Mirrors static/app.js's setLengthRange()/lengthRange() exactly, on purpose:
# whatever number the paragraph-range stepper shows as the upper box is the
# number this enforces. Two different languages reading the same shape of
# text is the whole reason craft:length's shipped default has to stay in
# that exact "N to M paragraphs, ..." shape — see that block's own comment.
_LENGTH_RANGE = re.compile(r"^(\d+)(?:\s*to\s*(\d+))?\s*paragraphs?\b", re.IGNORECASE)


def configured_max(settings: Settings) -> int | None:
    """The paragraph ceiling craft:length currently asks for, or None when
    there is nothing to enforce: the block is switched off, or its text has
    been hand-edited into something the stepper (and this) can no longer
    read a number out of. A cleared block reads as "no opinion", not zero.
    """
    layout = normalise(settings.prompt_sections)
    section = next((s for s in layout if s["id"] == "craft:length"), None)
    if not section or not section.get("enabled"):
        return None
    match = _LENGTH_RANGE.match((section.get("text") or "").strip())
    if not match:
        return None
    return int(match.group(2) or match.group(1))


def cut(text: str, settings: Settings) -> tuple[str, str]:
    """(kept, full). `full` is `""` when nothing was cut — the toggle is off,
    there is no configured ceiling to cut against, or the reply was already
    short enough — which the caller reads as "nothing to store as the full
    version." The toggle is checked first, before touching craft:length's
    text or splitting the reply into paragraphs at all, so a reply that can
    never be cut does not pay for either.
    """
    if not settings.cut_excess_paragraphs:
        return text, ""
    max_paragraphs = configured_max(settings)
    if not max_paragraphs:
        return text, ""
    spans = split_paragraphs(text)
    if len(spans) <= max_paragraphs:
        return text, ""
    # Sliced from the original text at the real paragraph's own end, not
    # rejoined from parts — so whatever blank-line style the model actually
    # used survives inside the kept paragraphs instead of being normalised.
    cut_at = spans[max_paragraphs - 1][1]
    return text[:cut_at].rstrip(), text
