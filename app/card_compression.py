"""AI-assisted card compression (§ /api/characters/{id}/compress).

For a card whose own prefix content (§ assembly.mandatory_cost) already
leaves too little room for real conversation (§ assembly.card_too_big):
this rewrites the four prose fields the editor can actually save back onto
a card — `persona`, `scenario`, `example_dialogue`, `system_prompt` — to a
shorter version that keeps the facts and drops the padding.

Lorebook entries are not offered here. They already have a mechanical
backstop of their own (§ lorebook._capped, entry.token_budget) that runs on
every turn with no AI call and no review step, and there is nowhere in the
editor yet to save a rewritten entry back to (§ main.py's `update_character`
docstring: "lorebook... come from the card and are not editable here").
Compressing them well is a real, separate feature — this one stops at what
the editor can already round-trip.

This never writes to the character on its own. It returns a preview —
before/after text and token counts per field — and the existing editor
`Save` button (§ main.py's `update_character`) is what actually commits it,
the same "generate, review, then the ordinary Save path" shape the rest of
this app already uses for anything an AI writes into a card (§ the writing
library, §ers character_reactions.py). A person can edit the compressed text
before saving it, same as anything else typed into that form.
"""

from __future__ import annotations

import logging

from .models import Character, Sampling
from .providers import GenRequest, ProviderError, provider_for_tier
from .providers.base import estimate_tokens

log = logging.getLogger(__name__)

# What the editor can actually save back onto a card (§ main.py's
# update_character `editable` tuple) — the only fields this can rewrite.
FIELDS: dict[str, str] = {
    "persona": "character description",
    "scenario": "scenario",
    "example_dialogue": "example dialogue",
    "system_prompt": "system prompt",
}

# Ask for a bit more than the bare shortfall, so one pass is usually enough
# rather than leaving a card that still needs a second round immediately
# after the first.
_TARGET_MARGIN = 1.25
# Never ask a field to shrink below this fraction of itself — a short field
# asked to lose 90% of its own length is a field asked to stop saying
# anything, not one asked to say it more efficiently.
_MIN_KEEP_FRACTION = 0.35
# A field this small is not worth a whole API call over — the token savings
# would not cover the request's own overhead, let alone the wait.
_MIN_WORTH_COMPRESSING = 60


def eligible_fields(character: Character) -> dict[str, str]:
    """This card's own text in the fields compression can touch, non-empty
    ones only."""
    return {
        key: (getattr(character, key, "") or "").strip()
        for key in FIELDS
        if (getattr(character, key, "") or "").strip()
    }


def field_targets(fields: dict[str, str], reduce_by: int) -> dict[str, int]:
    """A token target per field, splitting `reduce_by` across them in
    proportion to each field's own current size — the biggest field gives up
    the most, and nothing is asked to shrink past `_MIN_KEEP_FRACTION` of
    itself."""
    sizes = {k: estimate_tokens(v) for k, v in fields.items()}
    total = sum(sizes.values())
    if not total or reduce_by <= 0:
        return {}
    targets: dict[str, int] = {}
    for key, size in sizes.items():
        if size < _MIN_WORTH_COMPRESSING:
            continue
        share = int(round(reduce_by * (size / total)))
        floor = max(1, int(size * _MIN_KEEP_FRACTION))
        targets[key] = max(floor, size - share)
    return targets


def _prompt(character_name: str, label: str, text: str, target_tokens: int) -> str:
    return (
        f"Compress the following {label} for the roleplay character "
        f"{character_name or 'this character'}. It is currently about "
        f"{estimate_tokens(text)} tokens; bring it down to roughly "
        f"{target_tokens} tokens or fewer.\n\n"
        "Keep every fact, trait, relationship and stated preference a reader "
        "would actually need — cut redundant restatements of the same "
        "trait, decorative language that adds no information, and filler. "
        "Stay in the same register as the original: plain prose stays "
        "prose, a list stays a list, dialogue stays dialogue.\n\n"
        f"--- Original {label} ---\n{text}\n--- end ---\n\n"
        "Reply with only the compressed text. No preamble, no explanation, "
        "no quotation marks around it."
    )


def _clean(text: str) -> str:
    text = text.strip()
    # A model asked not to quote its answer sometimes does anyway.
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        text = text[1:-1].strip()
    return text


async def compress_field(
    provider, character_name: str, label: str, text: str, target_tokens: int
) -> tuple[str, bool]:
    """One field's compressed text, and whether compression actually helped.

    Falls back to the original on any failure, on an empty answer, or on an
    answer that came back *longer* than what it started from — a "compression"
    that grew the field is not one, whatever the backend returned.
    """
    request = GenRequest(
        system=_prompt(character_name, label, text, target_tokens),
        messages=[{"role": "user", "content": "Compress it now."}],
        sampling=Sampling(temp=0.3, max_tokens=max(200, target_tokens + 200)),
        pass_id="card_compression",
    )
    try:
        result = await provider.generate(request)
    except (ProviderError, OSError) as exc:
        log.info("card compression failed for a %s field: %s", label, exc)
        return text, False
    compressed = _clean(result.text)
    if not compressed or estimate_tokens(compressed) >= estimate_tokens(text):
        return text, False
    return compressed, True


async def preview(settings, character: Character, *, reduce_by: int) -> dict:
    """Compress every field worth compressing, and report before/after for
    each — nothing is saved. `reduce_by` is the total token count worth
    trying to recover, before `_TARGET_MARGIN` pads it; a caller passing 0
    or less gets every field back unchanged and `changed: False`.
    """
    fields = eligible_fields(character)
    targets = field_targets(fields, int(reduce_by * _TARGET_MARGIN)) if reduce_by > 0 else {}
    out: dict[str, dict] = {}
    if not targets:
        for key, text in fields.items():
            out[key] = {"before": text, "after": text, "before_tokens": estimate_tokens(text),
                        "after_tokens": estimate_tokens(text), "changed": False}
        return {"fields": out, "changed": False}

    try:
        provider = provider_for_tier("blocking", settings)
    except Exception as exc:  # noqa: BLE001 — surfaced as "nothing changed", not a 500
        log.info("card compression: no backend for compression: %s", exc)
        for key, text in fields.items():
            out[key] = {"before": text, "after": text, "before_tokens": estimate_tokens(text),
                        "after_tokens": estimate_tokens(text), "changed": False}
        return {"fields": out, "changed": False}

    changed_any = False
    try:
        for key, text in fields.items():
            if key not in targets:
                out[key] = {"before": text, "after": text, "before_tokens": estimate_tokens(text),
                            "after_tokens": estimate_tokens(text), "changed": False}
                continue
            after, changed = await compress_field(
                provider, character.name, FIELDS[key], text, targets[key]
            )
            changed_any = changed_any or changed
            out[key] = {
                "before": text, "after": after,
                "before_tokens": estimate_tokens(text), "after_tokens": estimate_tokens(after),
                "changed": changed,
            }
    finally:
        await provider.aclose()

    return {"fields": out, "changed": changed_any}
