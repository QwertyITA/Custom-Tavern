"""Short in-character reaction lines, generated once and cached on the card
(§models.CharacterReactions).

Three sentences a character never says in the story but the app needs: how
they would take being starred, unstarred, or permanently deleted. Written by
the backend behind the Messages tier — the same one that writes the
character's replies, so the voice matches — and generated once per line:
whichever of the three already has something in it, typed by hand or
generated earlier, is never touched again.

Called from three places: `main.py` right after a card import and
`passes/scheduler.py`'s `run_turn` (both fire-and-forget — a broken
generation must never hold up an import or a reply — so a character that
failed at import, or had a line cleared by hand, keeps getting another try
each time someone actually talks to it), and `main.py`'s reactions/regenerate
route, awaited so the character sheet can show what came back.

Settings.feature_character_reactions gates all three at once, checked here
rather than at each call site: off, `fill_missing` is a no-op regardless of
who called it or why.
"""

from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .db import Database
from .models import REACTION_KEYS, Character, Sampling
from .passes.contract import parse_json_loose
from .providers import GenRequest, ProviderError, provider_for_tier
from . import repo

log = logging.getLogger(__name__)

_ASK = {
    "starred": "being marked as a favourite",
    "unstarred": "being un-favourited, having been one before",
    "killed": "being permanently deleted — their very last line, ever",
}


def missing_keys(character: Character) -> list[str]:
    """Which of the three lines this character still does not have."""
    data = character.reactions.model_dump()
    return [key for key in REACTION_KEYS if not (data.get(key) or "").strip()]


def _prompt(character: Character, wanted: list[str]) -> str:
    asks = "\n".join(f"- {key}: a reaction to {_ASK[key]}" for key in wanted)
    persona = character.persona.strip() or "No further description is given."
    return (
        f"You write one-line in-character reactions for {character.name or 'a character'}.\n"
        f"{persona}\n\n"
        "Write these, each 5 to 8 words, in their own voice — however they "
        "would actually say or show it, not a narrator describing them:\n"
        f"{asks}\n\n"
        'Answer with only a JSON object — one key per line above, e.g. '
        '{"starred": "...", "unstarred": "...", "killed": "..."} — and '
        "nothing else."
    )


async def fill_missing(
    db: Database, settings: Settings, character: Character, *, force: list[str] | None = None
) -> Character | None:
    """Generate whichever lines are missing and save them.

    `force`, when given, regenerates exactly those keys regardless of what is
    already there — the one place a line already filled in gets overwritten
    by this module itself, used by the "Regenerate reactions" button
    (§ main.py's reactions/regenerate route) for a deliberate do-over. Left
    at the default, this only ever fills in a blank.

    Returns the updated character, or None when there was nothing to do or
    the attempt failed outright. A failure here is silent by design — see the
    module docstring for where the next attempt comes from.
    """
    if not settings.feature_character_reactions:
        return None
    wanted = force if force is not None else missing_keys(character)
    if not wanted:
        return None

    try:
        provider = provider_for_tier("blocking", settings)
    except Exception as exc:  # noqa: BLE001 — a bad backend config is not fatal here
        log.info("reaction generation: no backend for %s: %s", character.id, exc)
        return None

    request = GenRequest(
        system=_prompt(character, wanted),
        messages=[{"role": "user", "content": "Write the lines now."}],
        sampling=Sampling(temp=0.9, max_tokens=200),
        expects_json=True,
        pass_id="character_reactions",
    )

    retries = settings.background_retries
    payload = None
    for attempt in range(1, retries + 2):
        try:
            result = await asyncio.wait_for(
                provider.generate(request), timeout=settings.pass_timeout
            )
        except (ProviderError, asyncio.TimeoutError, OSError) as exc:
            if attempt <= retries:
                await asyncio.sleep(min(2**attempt, 8))
                continue
            log.info("reaction generation failed for %s: %s", character.id, exc)
            return None
        payload = parse_json_loose(result.text)
        if payload:
            break
        if attempt > retries:
            return None

    updates = {
        key: str(payload[key]).strip()
        for key in wanted
        if isinstance(payload, dict)
        and isinstance(payload.get(key), str)
        and str(payload[key]).strip()
    }
    if not updates:
        return None

    character.reactions = character.reactions.model_copy(update=updates)
    repo.save_character(db, character)
    return character


async def spawn(db: Database, settings: Settings, character: Character) -> None:
    """`fill_missing`, but for a caller that fires it and moves on.

    Both call sites (a card import, and after a reply in run_turn) launch this
    as a bare `asyncio.create_task` with nothing watching it — so an exception
    `fill_missing` did not already swallow would otherwise surface only as an
    "exception was never retrieved" warning at garbage-collection time, the
    same failure mode `_run_background` guards against for real passes.
    """
    try:
        await fill_missing(db, settings, character)
    except Exception as exc:  # noqa: BLE001 — nothing is downstream of this to take with it
        log.info("reaction generation crashed for %s: %s", character.id, exc)
