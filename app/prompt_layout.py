"""Which parts of the prompt are built, and in what order (§7.1, §14).

The prompt is assembled in three bands, and the bands are the optimisation:

    prefix    rarely changes    → the model's KV cache is built on this
    middle    changes slowly    → lore hits, memories, summary, the conversation
    volatile  changes every turn → state bands, scene, toggles

Sections can be reordered and switched off, but only **within** their band.
That single restriction is what keeps the cache rule from being something the
user has to remember: moving a volatile section earlier would put changing text
in front of stable text, and everything after the change is recomputed on every
turn — on a phone-hosted local model that is the difference between a reply
starting immediately and starting after a rebuild of the entire prefix.

So the rule is structural rather than advisory. There is no arrangement of the
controls that produces a slow prompt.
"""

from __future__ import annotations

import uuid
from typing import Any

CUSTOM_PREFIX = "custom:"

# Band order is fixed. These are the labels the panel prints for each one, and
# the sentence under it explaining why its contents sit where they do.
BANDS: list[dict[str, str]] = [
    {
        "id": "prefix",
        "label": "Who they are",
        "note": "Rarely changes, so the model can keep it cached between turns.",
    },
    {
        "id": "middle",
        "label": "What has happened",
        "note": "Recalled as the story needs it, then the conversation itself.",
    },
    {
        "id": "volatile",
        "label": "Right now",
        "note": "Different every turn — which is why this group stays last. "
        "Anything above a changing section is recomputed along with it.",
    },
]

BAND_IDS = tuple(band["id"] for band in BANDS)

# The built-in sections, in the order they are used when nothing is configured.
# `fixed` marks the ones that cannot be switched off: without the instruction
# there is no character, and without the conversation there is no chat.
BUILTIN: list[dict[str, Any]] = [
    {"id": "instruction", "band": "prefix", "label": "Main instruction",
     "note": "How to reply, and as whom.", "fixed": True},
    {"id": "character", "band": "prefix", "label": "Character description",
     "note": "The card's own persona text."},
    {"id": "scenario", "band": "prefix", "label": "Scenario",
     "note": "Where the story starts."},
    {"id": "user_persona", "band": "prefix", "label": "Who you are",
     "note": "Your persona, as they see you."},
    {"id": "cast", "band": "prefix", "label": "Who else is here",
     "note": "The other characters in a group chat. Nothing in a solo one."},
    {"id": "world", "band": "prefix", "label": "World",
     "note": "Lorebook entries marked always-on."},
    {"id": "examples", "band": "prefix", "label": "Example dialogue",
     "note": "Shows the voice. Costs tokens on every turn."},

    {"id": "lore", "band": "middle", "label": "Relevant lore",
     "note": "Lorebook entries the recent messages mentioned."},
    {"id": "memories", "band": "middle", "label": "Remembered",
     "note": "Durable facts pulled back from earlier chats."},
    {"id": "summary", "band": "middle", "label": "Story so far",
     "note": "The rolling summary of what fell out of the window."},
    {"id": "conversation", "band": "middle", "label": "The conversation",
     "note": "Recent messages, and the author's note at its depth.",
     "fixed": True},

    {"id": "state", "band": "volatile", "label": "Their current state",
     "note": "Trust, mood and the rest, as guidance rather than numbers."},
    {"id": "setting", "band": "volatile", "label": "Setting",
     "note": "Place, weather and time, as the scene pass last saw them."},
    {"id": "event", "band": "volatile", "label": "Something happening",
     "note": "An unplanned intrusion the world made, waiting to be used once."},
    {"id": "search", "band": "volatile", "label": "Looked up just now",
     "note": "What the web search found for this message. Nothing when it is off."},
    {"id": "toggles", "band": "volatile", "label": "Toggle injections",
     "note": "Text from whichever story toggles are on."},
    {"id": "final", "band": "volatile", "label": "The card's last word",
     "note": "Post-history instructions — what the card wants obeyed over "
             "whatever the conversation has drifted into."},
]

BUILTIN_BY_ID = {section["id"]: section for section in BUILTIN}
FIXED_IDS = frozenset(s["id"] for s in BUILTIN if s.get("fixed"))


def new_custom_id() -> str:
    return f"{CUSTOM_PREFIX}{uuid.uuid4().hex[:8]}"


def is_custom(section_id: str) -> bool:
    return section_id.startswith(CUSTOM_PREFIX)


def _clean_custom(raw: dict) -> dict[str, Any] | None:
    section_id = str(raw.get("id") or "")
    if not is_custom(section_id):
        return None
    band = str(raw.get("band") or "prefix")
    return {
        "id": section_id,
        "band": band if band in BAND_IDS else "prefix",
        "label": str(raw.get("label") or "Custom block").strip() or "Custom block",
        "text": str(raw.get("text") or ""),
        "enabled": bool(raw.get("enabled", True)),
        "custom": True,
    }


def normalise(raw: Any) -> list[dict[str, Any]]:
    """A complete, ordered layout from whatever was stored.

    Anything stored wins on order and on/off; anything missing is appended at
    the end of its own band with its default state. That is what makes adding
    a section in a later version safe — an older settings file simply does not
    mention it, and it arrives switched on at the bottom of its group rather
    than silently absent.
    """
    stored = raw if isinstance(raw, list) else []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for entry in stored:
        if not isinstance(entry, dict):
            continue
        section_id = str(entry.get("id") or "")
        if section_id in seen:
            continue
        if is_custom(section_id):
            custom = _clean_custom(entry)
            if custom:
                out.append(custom)
                seen.add(section_id)
        elif section_id in BUILTIN_BY_ID:
            base = BUILTIN_BY_ID[section_id]
            out.append({
                **base,
                # A fixed section is fixed however the file was edited.
                "enabled": True if section_id in FIXED_IDS else bool(entry.get("enabled", True)),
                "custom": False,
            })
            seen.add(section_id)

    for base in BUILTIN:
        if base["id"] not in seen:
            out.append({**base, "enabled": True, "custom": False})

    # Bands are not reorderable, so the stored order only decides position
    # inside one. Sorting by band here is what makes that true no matter what
    # a hand-edited file says.
    return sorted(out, key=lambda s: BAND_IDS.index(s["band"]))


def to_storage(layout: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The part worth writing to settings.json — order, state, and the custom
    blocks' own content. Labels and notes are code, not configuration."""
    out: list[dict[str, Any]] = []
    for section in normalise(layout):
        if section["custom"]:
            out.append({
                "id": section["id"], "band": section["band"], "label": section["label"],
                "text": section["text"], "enabled": section["enabled"],
            })
        else:
            out.append({"id": section["id"], "enabled": section["enabled"]})
    return out


def order_for(layout: list[dict[str, Any]], band: str) -> list[dict[str, Any]]:
    """The enabled sections of one band, in order."""
    return [s for s in layout if s["band"] == band and s["enabled"]]


def catalogue() -> dict[str, Any]:
    """Everything the panel needs to draw itself, so the frontend never carries
    a second copy of the section list."""
    return {
        "bands": [dict(band) for band in BANDS],
        "sections": [dict(section) for section in BUILTIN],
        "fixed": sorted(FIXED_IDS),
    }
