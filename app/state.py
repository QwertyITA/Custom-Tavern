"""State slices, band interpretation, decay and write arbitration (§6, §5.5).

Two rules carry most of the weight here:

1. Raw numbers never reach a prompt. A value resolves to a band in code and the
   band's *guidance text* is what gets injected. Models act on "resistant,
   deflects, needs convincing" far more reliably than on "willingness: 2".
2. Arbitration is per-slice and by source turn only. Passes writing different
   slices never contend — whoever lands first just updates its own panel. Two
   passes writing the *same* slice (pass 1's provisional emotional read vs the
   auditor's corrected one) are ordered by the turn they came from, and an
   older-turn write loses.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from .db import Database, now
from .models import Band, VariableSchema

# Slice names the engine itself knows about. Passes may write any name.
SLICE_VARS = "state.vars"
SLICE_SCENE = "state.scene"
SLICE_EXPRESSION = "state.expression"
SLICE_BACKGROUND = "state.background"
SLICE_SIGNALS = "state.signals"
# Something the world does, unprompted (roadmap: random events). Shared, not
# per character: a knock at the door happens to the room.
SLICE_EVENT = "state.event"
# What was looked up for this turn (roadmap 24). Shared: a fact is a fact
# whoever in the room happens to be answering.
SLICE_SEARCH = "state.search"

# Which slices belong to one character rather than to the conversation (§15).
# Trust and mood are held *by someone*; the weather is not. Getting this split
# right is the prerequisite for group chats — without it, two characters in one
# room would share a single opinion of you and overwrite each other's turn by
# turn.
PER_CHARACTER_SLICES = frozenset({SLICE_VARS, SLICE_EXPRESSION, SLICE_SIGNALS})
SHARED_SLICES = frozenset({SLICE_SCENE, SLICE_BACKGROUND, SLICE_EVENT, SLICE_SEARCH})

NAMESPACE_SEPARATOR = ":"


def slice_for(name: str, character_id: str = "") -> str:
    """The stored name of a slice, namespaced when it belongs to a character.

    Namespacing is unconditional rather than "only in group chats": a solo chat
    is a group of one, and a rule that changes shape when a second character
    arrives is one that has to be right in two places forever.
    """
    if name in PER_CHARACTER_SLICES and character_id:
        return f"{name}{NAMESPACE_SEPARATOR}{character_id}"
    return name


def split_slice(stored: str) -> tuple[str, str]:
    """A stored name back into (slice name, character id).

    Character ids never contain the separator — they are hex — so the split is
    unambiguous. An unnamespaced name comes back with an empty id, which is
    what every row written before this existed looks like.
    """
    base, separator, owner = stored.partition(NAMESPACE_SEPARATOR)
    if separator and base in PER_CHARACTER_SLICES:
        return base, owner
    return stored, ""

# §18.1 — the initial canonical variable set. A character card overrides this
# wholesale by defining its own `state_schema`.
DEFAULT_STATE_SCHEMA: dict[str, dict[str, Any]] = {
    "willingness": {
        "label": "Willingness",
        "min": 0, "max": 10, "baseline": 5, "decay": 0.15,
        "bands": [
            {"range": [0, 3], "label": "guarded",
             "guidance": "resistant, deflects, needs convincing before giving ground"},
            {"range": [4, 6], "label": "neutral",
             "guidance": "engages if asked, but won't volunteer or push"},
            {"range": [7, 10], "label": "eager",
             "guidance": "leans in, initiates, generous with time and detail"},
        ],
    },
    "trust": {
        "label": "Trust",
        "min": 0, "max": 10, "baseline": 4, "decay": 0.08,
        "bands": [
            {"range": [0, 3], "label": "wary",
             "guidance": "withholds specifics, tests motives, keeps an exit in mind"},
            {"range": [4, 6], "label": "provisional",
             "guidance": "shares ordinary things, holds back what matters"},
            {"range": [7, 10], "label": "open",
             "guidance": "speaks plainly, admits doubt, offers private detail unprompted"},
        ],
    },
    "mood": {
        "label": "Mood",
        "min": 0, "max": 10, "baseline": 5, "decay": 0.25,
        "bands": [
            {"range": [0, 3], "label": "low",
             "guidance": "short sentences, little humour, easily worn down"},
            {"range": [4, 6], "label": "level",
             "guidance": "even-tempered, neither warm nor cold"},
            {"range": [7, 10], "label": "bright",
             "guidance": "quick, playful, teases and jokes readily"},
        ],
    },
    "energy": {
        "label": "Energy",
        "min": 0, "max": 10, "baseline": 6, "decay": 0.2,
        "bands": [
            {"range": [0, 3], "label": "spent",
             "guidance": "wants the scene to end, minimal physical action"},
            {"range": [4, 6], "label": "steady",
             "guidance": "ordinary pace, willing to continue"},
            {"range": [7, 10], "label": "restless",
             "guidance": "moves around, changes subject, drives the scene forward"},
        ],
    },
}


def load_schema(raw: dict[str, Any] | None) -> dict[str, VariableSchema]:
    source = raw if raw else DEFAULT_STATE_SCHEMA
    schema: dict[str, VariableSchema] = {}
    for name, spec in source.items():
        if isinstance(spec, VariableSchema):
            schema[name] = spec
            continue
        bands = [Band(**b) if not isinstance(b, Band) else b for b in spec.get("bands", [])]
        schema[name] = VariableSchema(
            min=spec.get("min", 0),
            max=spec.get("max", 10),
            baseline=spec.get("baseline", 5),
            decay=spec.get("decay", 0.0),
            label=spec.get("label", name),
            bands=bands,
        )
    return schema


def initial_values(schema: dict[str, VariableSchema]) -> dict[str, float]:
    return {name: spec.baseline for name, spec in schema.items()}


# ------------------------------------------------------------------- decay


def decay_step(schema: dict[str, VariableSchema], values: dict[str, float]) -> dict[str, float]:
    """Pull every variable one step toward its baseline. Deterministic, free.

    Never overshoots: a value within one decay step of baseline snaps to it.
    """
    out: dict[str, float] = {}
    for name, spec in schema.items():
        value = float(values.get(name, spec.baseline))
        if spec.decay <= 0:
            out[name] = spec.clamp(value)
            continue
        gap = spec.baseline - value
        if abs(gap) <= spec.decay:
            out[name] = spec.baseline
        else:
            out[name] = spec.clamp(value + spec.decay * (1 if gap > 0 else -1))
    # Preserve variables that exist in the session but not in the schema.
    for name, value in values.items():
        out.setdefault(name, value)
    return out


# ------------------------------------------------------------------- nudges


@dataclass
class NudgeRule:
    pattern: str
    variable: str
    delta: float
    applies_to: str = "user"  # user | assistant | any
    ignore_case: bool = True


def load_nudges(raw: list[dict[str, Any]] | None) -> list[NudgeRule]:
    rules: list[NudgeRule] = []
    for item in raw or []:
        try:
            rules.append(
                NudgeRule(
                    pattern=item["pattern"],
                    variable=item.get("variable") or item["var"],
                    delta=float(item.get("delta", 0)),
                    applies_to=item.get("applies_to", "user"),
                    ignore_case=bool(item.get("ignore_case", True)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # a malformed rule must not break the turn
    return rules


def apply_nudges(
    rules: list[NudgeRule],
    schema: dict[str, VariableSchema],
    values: dict[str, float],
    text: str,
    role: str = "user",
) -> tuple[dict[str, float], list[str]]:
    """Rule-based keyword/regex adjustment — the cheapest tier, no model at all."""
    out = dict(values)
    fired: list[str] = []
    for rule in rules:
        if rule.applies_to not in (role, "any"):
            continue
        flags = re.IGNORECASE if rule.ignore_case else 0
        try:
            if not re.search(rule.pattern, text, flags):
                continue
        except re.error:
            continue  # bad regex in a card: skip, don't crash the turn
        spec = schema.get(rule.variable)
        current = float(out.get(rule.variable, spec.baseline if spec else 0))
        updated = current + rule.delta
        out[rule.variable] = spec.clamp(updated) if spec else updated
        fired.append(f"{rule.variable}{rule.delta:+g}")
    return out, fired


def apply_deltas(
    schema: dict[str, VariableSchema],
    values: dict[str, float],
    deltas: dict[str, Any],
) -> dict[str, float]:
    """Fold a pass's reported deltas into the current values, clamped."""
    out = dict(values)
    for name, delta in (deltas or {}).items():
        try:
            amount = float(delta)
        except (TypeError, ValueError):
            continue
        spec = schema.get(name)
        current = float(out.get(name, spec.baseline if spec else 0))
        out[name] = spec.clamp(current + amount) if spec else current + amount
    return out


# -------------------------------------------------------------------- bands


def band_guidance(
    schema: dict[str, VariableSchema], values: dict[str, float]
) -> list[tuple[str, str, str]]:
    """(variable, band label, guidance) for every variable in the schema."""
    out: list[tuple[str, str, str]] = []
    for name, spec in schema.items():
        band = spec.band_for(float(values.get(name, spec.baseline)))
        if band:
            out.append((spec.label or name, band.label, band.guidance))
    return out


def render_bands(schema: dict[str, VariableSchema], values: dict[str, float]) -> str:
    """The block injected into the volatile suffix. No raw numbers, by rule."""
    lines = [f"- {label} ({band}): {guidance}" for label, band, guidance in band_guidance(schema, values)]
    return "\n".join(lines)


# ----------------------------------------------------------------- storage


@dataclass
class SliceWrite:
    accepted: bool
    reason: str = ""
    value: Any = None


def read_slice(db: Database, chat_id: str, name: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT value, source_turn, source_pass, provisional, updated_at "
        "FROM state_slices WHERE chat_id=? AND slice_name=?",
        (chat_id, name),
    )
    if row is None:
        return None
    return {
        "value": json.loads(row["value"]),
        "source_turn": row["source_turn"],
        "source_pass": row["source_pass"],
        "provisional": bool(row["provisional"]),
        "updated_at": row["updated_at"],
    }


def slices_for(db: Database, chat_id: str, character_id: str) -> dict[str, dict[str, Any]]:
    """Every slice that applies to one character, keyed by its plain name.

    Namespacing is a storage concern (§15). A reader asking "what is her
    expression" wants `state.expression`, not `state.expression:mira` — and
    making every caller build the suffixed name would spread the scheme across
    the whole app, including the frontend, which has no business knowing it.

    Shared slices come through untouched; another character's do not come
    through at all.
    """
    out: dict[str, dict[str, Any]] = {}
    for stored, body in read_all_slices(db, chat_id).items():
        base, owner = split_slice(stored)
        if owner and owner != character_id:
            continue
        out[base] = body
    return out


def read_all_slices(db: Database, chat_id: str) -> dict[str, dict[str, Any]]:
    rows = db.query(
        "SELECT slice_name, value, source_turn, source_pass, provisional, updated_at "
        "FROM state_slices WHERE chat_id=?",
        (chat_id,),
    )
    return {
        row["slice_name"]: {
            "value": json.loads(row["value"]),
            "source_turn": row["source_turn"],
            "source_pass": row["source_pass"],
            "provisional": bool(row["provisional"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    }


def _write_slice_sync(
    conn: sqlite3.Connection,
    chat_id: str,
    name: str,
    value: Any,
    source_turn: int,
    source_pass: str,
    variant_id: str | None,
    provisional: bool,
) -> SliceWrite:
    existing = conn.execute(
        "SELECT value, source_turn, source_pass, provisional FROM state_slices "
        "WHERE chat_id=? AND slice_name=?",
        (chat_id, name),
    ).fetchone()

    if existing is not None and existing["source_turn"] > source_turn:
        # Stale-write rejection, and only here: same slice, older turn (§5.5).
        return SliceWrite(
            accepted=False,
            reason=f"stale: slice at turn {existing['source_turn']}, write from {source_turn}",
            value=json.loads(existing["value"]),
        )

    prev = (
        json.dumps(
            {
                "value": json.loads(existing["value"]),
                "source_turn": existing["source_turn"],
                "source_pass": existing["source_pass"],
                "provisional": existing["provisional"],
            }
        )
        if existing is not None
        else None
    )
    encoded = json.dumps(value)
    timestamp = now()
    conn.execute(
        "INSERT INTO state_slices(chat_id, slice_name, value, source_turn, source_pass, "
        "provisional, updated_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(chat_id, slice_name) DO UPDATE SET value=excluded.value, "
        "source_turn=excluded.source_turn, source_pass=excluded.source_pass, "
        "provisional=excluded.provisional, updated_at=excluded.updated_at",
        (chat_id, name, encoded, source_turn, source_pass, int(provisional), timestamp),
    )
    conn.execute(
        "INSERT INTO state_writes(chat_id, slice_name, value, prev_value, source_turn, "
        "source_pass, variant_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
        (chat_id, name, encoded, prev, source_turn, source_pass, variant_id, timestamp),
    )
    return SliceWrite(accepted=True, value=value)


async def write_slice(
    db: Database,
    chat_id: str,
    name: str,
    value: Any,
    *,
    source_turn: int,
    source_pass: str = "",
    variant_id: str | None = None,
    provisional: bool = False,
) -> SliceWrite:
    return await db.write(
        lambda conn: _write_slice_sync(
            conn, chat_id, name, value, source_turn, source_pass, variant_id, provisional
        )
    )


def _rollback_sync(
    conn: sqlite3.Connection, chat_id: str, turn: int, variant_id: str | None
) -> int:
    """Undo state writes bound to a swipe variant, newest first (§9)."""
    if variant_id is None:
        rows = conn.execute(
            "SELECT * FROM state_writes WHERE chat_id=? AND source_turn=? AND rolled_back=0 "
            "ORDER BY id DESC",
            (chat_id, turn),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM state_writes WHERE chat_id=? AND source_turn=? AND variant_id=? "
            "AND rolled_back=0 ORDER BY id DESC",
            (chat_id, turn, variant_id),
        ).fetchall()

    for row in rows:
        if row["prev_value"] is None:
            conn.execute(
                "DELETE FROM state_slices WHERE chat_id=? AND slice_name=?",
                (chat_id, row["slice_name"]),
            )
        else:
            prev = json.loads(row["prev_value"])
            conn.execute(
                "UPDATE state_slices SET value=?, source_turn=?, source_pass=?, "
                "provisional=?, updated_at=? WHERE chat_id=? AND slice_name=?",
                (
                    json.dumps(prev["value"]),
                    prev["source_turn"],
                    prev["source_pass"],
                    prev["provisional"],
                    now(),
                    chat_id,
                    row["slice_name"],
                ),
            )
        conn.execute("UPDATE state_writes SET rolled_back=1 WHERE id=?", (row["id"],))
    return len(rows)


async def rollback_turn(
    db: Database, chat_id: str, turn: int, variant_id: str | None = None
) -> int:
    """Roll a turn's state writes back before generating a new swipe variant."""
    return await db.write(lambda conn: _rollback_sync(conn, chat_id, turn, variant_id))
