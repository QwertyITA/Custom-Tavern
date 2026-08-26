"""The post_process pass: an LLM copy-edit of a finished reply (§5.7).

Runs once, after a reply has been fully generated and cleaned but before it
is shown or stored — the model is handed its own draft and asked to fix what
is mechanical: grammar and spelling, a misspelled character name, whether it
kept to the configured paragraph length (craft:length), whether it slipped
out of the point-of-view convention (craft:pov). Nothing about the content,
the events or the voice is meant to change.

Distinct from `app/postprocess.py` (§13), which strips template artifacts and
stray markup out of *every* reply regardless of this being on — that one is
mechanical and always runs; this one is an optional model call, and the
reply stays hidden from the user for as long as it takes.

Best-effort only. Any failure, an empty response, or a response so different
in size from the draft that it reads as a refusal or a rewrite rather than an
edit, is treated the same as post-process being switched off: the draft is
what gets shown. A pass whose whole job is polish must never be the reason a
turn fails or hangs.
"""

from __future__ import annotations

import asyncio

from .models import PassDef
from .postprocess import clean_reply, split_thinking
from .providers.base import GenRequest, Provider, ProviderError

# A rewrite this much shorter or longer than the draft reads as the model
# doing something other than editing it — refusing, restarting the scene,
# answering as itself — and is discarded in favour of the untouched draft
# rather than shown as "the corrected version". Generous on purpose: a real
# edit can legitimately move a lot in either direction (cutting a reply down
# to a configured length, or padding a too-short one up to it).
_MIN_KEEP_FRACTION = 0.3
_MAX_GROW_FACTOR = 2.5


def _looks_like_an_edit(draft: str, candidate: str) -> bool:
    if not candidate.strip():
        return False
    lo = len(draft) * _MIN_KEEP_FRACTION
    hi = len(draft) * _MAX_GROW_FACTOR
    return lo <= len(candidate) <= hi


def _context(character_name: str, draft: str, assembled_parts: list[dict]) -> str:
    """The per-turn brief: who is speaking, the length/POV targets already
    given to the reply itself (pulled from the already-assembled prompt
    rather than re-read from settings, so this can never quote a different
    target than the one the draft was actually written against), and the
    draft itself.
    """
    pov_text = next((p["text"] for p in assembled_parts if p["id"] == "craft:pov"), "")
    length_text = next((p["text"] for p in assembled_parts if p["id"] == "craft:length"), "")
    lines = [f"The character speaking is {character_name}."]
    if pov_text:
        lines.append(f"Point of view to keep:\n{pov_text}")
    if length_text:
        lines.append(f"Length target:\n{length_text}")
    lines.append(f"The reply to edit:\n\n{draft}")
    return "\n\n".join(lines)


async def run(
    provider: Provider,
    definition: PassDef,
    draft: str,
    character_name: str,
    assembled_parts: list[dict],
    timeout: float,
) -> str:
    """The (possibly) corrected reply — `draft` itself on anything that goes
    wrong, so this can never be the reason a turn fails."""
    if not draft.strip():
        return draft

    request = GenRequest(
        system=definition.prompt,
        messages=[{"role": "user", "content": _context(character_name, draft, assembled_parts)}],
        sampling=definition.sampling,
        pass_id=definition.id,
    )
    try:
        result = await asyncio.wait_for(provider.generate(request), timeout=timeout)
    except (ProviderError, asyncio.TimeoutError, OSError):
        return draft

    text, _thinking = split_thinking(result.text)
    candidate = clean_reply(text, strip_leakage=False).strip()
    return candidate if _looks_like_an_edit(draft, candidate) else draft
