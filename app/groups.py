"""Several characters in one conversation (§15, roadmap 8).

Membership, and the question that actually makes a group chat work: **who
speaks next**.

The turn-order policy is deliberately not round-robin by default. Round-robin
is the arrangement where you say something to one person and the other one
answers, forever, and it is the single thing that makes group chats read as a
mechanism rather than as a room. The default policy is free — no model call —
and works in the order a person would expect:

1. **Addressed by name.** If the message names someone, they answer. This is
   how anyone would read it, and it costs a substring search.
2. **Otherwise, weighted chance**, by each character's talkativeness, with the
   one who just spoke pushed down so the room does not become two people.

Muted characters never speak, but stay in the prompt: someone standing there
saying nothing is still in the scene, and dropping them from the context would
have the others talk as if the room were empty.
"""

from __future__ import annotations

import json

import random
import re
import sqlite3
from typing import Any

from .db import Database, now

POLICIES: list[dict[str, str]] = [
    {"id": "natural", "label": "Whoever would answer",
     "note": "Named in your message, or the likeliest to speak up. Costs nothing."},
    {"id": "round_robin", "label": "Take turns",
     "note": "Strict order. Predictable, and reads like a mechanism."},
    {"id": "manual", "label": "You choose",
     "note": "Pick who replies before each message."},
]

POLICY_IDS = tuple(policy["id"] for policy in POLICIES)
DEFAULT_POLICY = "natural"

# How much the last speaker's weight is cut. Not zero: a character can follow
# their own line, and a room where that is impossible has its own tell.
REPEAT_PENALTY = 0.25


def members(db: Database, chat_id: str) -> list[dict[str, Any]]:
    """Everyone in this chat, in join order, with their card details."""
    rows = db.query(
        "SELECT m.character_id, m.muted, m.talkativeness, m.joined_at, c.name, c.data "
        "FROM chat_members m JOIN characters c ON c.id = m.character_id "
        "WHERE m.chat_id=? ORDER BY m.joined_at, m.rowid",
        (chat_id,),
    )
    return [
        {
            "character_id": row["character_id"],
            "name": row["name"],
            "muted": bool(row["muted"]),
            "talkativeness": float(row["talkativeness"]),
            "joined_at": row["joined_at"],
            # So a row in a group chat can carry the right face. Neutral only:
            # the expression slice is per chat, not per member, and a list of
            # faces nobody is showing is a list of files to load.
            "pfp": _neutral_pfp(row["data"]),
            # And the shape it is drawn in: two members of one group can be
            # framed differently, and each row has to know which.
            "pfp_shape": _pfp_shape(row["data"]),
            # Same for a colour treatment — it belongs to the member, not to
            # the room they are standing in.
            "pfp_effect": _pfp_effect(row["data"]),
        }
        for row in rows
    ]


def _card(raw: Any) -> dict:
    try:
        card = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return card if isinstance(card, dict) else {}


def _neutral_pfp(raw: Any) -> str:
    pfp_set = _card(raw).get("pfp_set") or {}
    if not isinstance(pfp_set, dict):
        return ""
    return str(pfp_set.get("neutral") or next(iter(pfp_set.values()), "") or "")


def _pfp_shape(raw: Any) -> str:
    return "square" if _card(raw).get("pfp_shape") == "square" else "portrait"


def _pfp_effect(raw: Any) -> dict:
    effect = _card(raw).get("pfp_effect")
    return effect if isinstance(effect, dict) else {}


def is_group(db: Database, chat_id: str) -> bool:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM chat_members WHERE chat_id=?", (chat_id,)
    )
    return bool(row and row["n"] > 1)


def add_member(
    db: Database, chat_id: str, character_id: str, *, talkativeness: float = 1.0
) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO chat_members(chat_id, character_id, muted, talkativeness, "
            "joined_at) VALUES(?,?,0,?,?) ON CONFLICT(chat_id, character_id) DO NOTHING",
            (chat_id, character_id, _clamp(talkativeness), now()),
        )
    )


def remove_member(db: Database, chat_id: str, character_id: str) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "DELETE FROM chat_members WHERE chat_id=? AND character_id=?",
            (chat_id, character_id),
        )
    )


def update_member(
    db: Database,
    chat_id: str,
    character_id: str,
    *,
    muted: bool | None = None,
    talkativeness: float | None = None,
) -> None:
    sets, values = [], []
    if muted is not None:
        sets.append("muted=?")
        values.append(int(muted))
    if talkativeness is not None:
        sets.append("talkativeness=?")
        values.append(_clamp(talkativeness))
    if not sets:
        return
    values.extend([chat_id, character_id])
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE chat_members SET {', '.join(sets)} "
            "WHERE chat_id=? AND character_id=?",
            tuple(values),
        )
    )


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(2.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def ensure_member(db: Database, chat_id: str, character_id: str) -> None:
    """Make sure a chat's own character is in its member list.

    Every chat has one from creation; this exists for the ones that predate the
    table, so opening an old chat quietly makes it a group of one rather than a
    group of none — which would have nobody to reply.
    """
    row = db.query_one(
        "SELECT 1 FROM chat_members WHERE chat_id=? AND character_id=?",
        (chat_id, character_id),
    )
    if row is None:
        add_member(db, chat_id, character_id)


# ------------------------------------------------------------ who speaks


def addressed(text: str, candidates: list[dict]) -> dict | None:
    """The character named in this message, if exactly one is.

    Exactly one: a message naming two people has not chosen between them, and
    picking the first would be arbitrary in a way the person would notice.

    Two details that are easy to get wrong and both show up immediately in a
    real room:

    * Boundaries are lookarounds, not `\\b`. A name ending in punctuation —
      "R. Vale (the elder)" — has no word boundary after the bracket, so `\\b`
      simply never matches it.
    * Longest name first, blanking what it matched. Otherwise "Anna Vale"
      also matches "Anna" standing beside her, the message looks like it named
      two people, and nobody is chosen.
    """
    if not text:
        return None
    remaining = text
    hits: list[dict] = []
    for person in sorted(candidates, key=lambda c: -len(c["name"] or "")):
        name = person["name"]
        if not name:
            continue
        found = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", remaining, re.IGNORECASE)
        if not found:
            continue
        hits.append(person)
        remaining = (
            remaining[: found.start()]
            + " " * (found.end() - found.start())
            + remaining[found.end() :]
        )
    return hits[0] if len(hits) == 1 else None


def choose_speaker(
    db: Database,
    chat_id: str,
    *,
    policy: str = DEFAULT_POLICY,
    user_text: str = "",
    last_speaker: str = "",
    forced: str = "",
    seed: Any = None,
) -> dict | None:
    """Who replies to this turn. None when there is nobody who can.

    `forced` wins outright — it is either the manual policy's choice or a
    deliberate "let them answer" — as long as that character is here and not
    muted. Asking a muted character to speak is a contradiction worth ignoring
    rather than honouring.
    """
    available = [m for m in members(db, chat_id) if not m["muted"]]
    if not available:
        return None
    if forced:
        return next((m for m in available if m["character_id"] == forced), None)
    if len(available) == 1:
        return available[0]

    if policy == "round_robin":
        if not last_speaker:
            return available[0]
        names = [m["character_id"] for m in available]
        if last_speaker not in names:
            return available[0]
        return available[(names.index(last_speaker) + 1) % len(available)]

    if policy == "manual":
        # Nothing was chosen, so nobody speaks. The UI asks before sending;
        # reaching here means the request did not say, and inventing a speaker
        # would defeat the point of the policy.
        return None

    # natural
    named = addressed(user_text, available)
    if named is not None:
        return named

    rng = random.Random(seed) if seed is not None else random
    weights = [
        max(0.01, m["talkativeness"] * (REPEAT_PENALTY if m["character_id"] == last_speaker else 1.0))
        for m in available
    ]
    return rng.choices(available, weights=weights, k=1)[0]


def last_speaker(db: Database, chat_id: str) -> str:
    row = db.query_one(
        "SELECT speaker_id FROM messages WHERE chat_id=? AND role='assistant' "
        "AND speaker_id != '' ORDER BY turn DESC, created_at DESC LIMIT 1",
        (chat_id,),
    )
    return row["speaker_id"] if row else ""


# ------------------------------------------------------------ the prompt


def cast_note(members_here: list[dict], speaking: str) -> str:
    """Who else is in the room, for the speaker's prompt.

    Muted characters are listed too. Someone standing there saying nothing is
    still in the scene, and leaving them out would have the others talk as if
    the room were empty.
    """
    others = [m for m in members_here if m["character_id"] != speaking]
    if not others:
        return ""
    names = ", ".join(m["name"] for m in others)
    return (
        f"## Also here\n{names}\n"
        "They are present and may be spoken to or about, but you write only "
        "your own words — never theirs."
    )
