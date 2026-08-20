"""The pass library and the toggle library (§5.3, §10).

Canonical passes ship with the engine and each carries its own animation.
Custom passes are user-defined and fall back to cogs (blocking) or ambient
(background). Both live in the same table; `kind` is the only difference.

Every prompt here asks for rubric levels rather than floats, and for JSON only,
because that is what the small/cheap models on the background tier can actually
hold to.
"""

from __future__ import annotations

import json
import sqlite3

from ..db import Database
from ..models import PassDef, PassOutput, Sampling, Toggle, Trigger
from ..state import SLICE_BACKGROUND, SLICE_EVENT, SLICE_EXPRESSION, SLICE_SCENE
from ..state import SLICE_VARS

CANONICAL_PASSES: list[PassDef] = [
    PassDef(
        id="basic",
        kind="canonical",
        label="Reply",
        blocking=True,
        model_tier="blocking",
        trigger=Trigger(type="every_turn"),
        # 5000. The shipped writing blocks (§7.5) ask for four to eight
        # paragraphs, the state suffix goes after them, and a reasoning model
        # spends several hundred tokens before it writes a word — at 600 the
        # reply was cut mid-sentence and the suffix never arrived, which reads
        # as the state engine having stopped working. Room to spare costs
        # nothing on a backend that stops when it is finished; running out
        # costs the whole turn.
        sampling=Sampling(temp=0.9, top_p=0.95, rep_penalty=1.08, max_tokens=5000),
        output=PassOutput(type="reply"),
        writes_slice=SLICE_VARS,
        animation="typing",
        expects_json=False,
        prompt="",  # assembled per turn (§7.1)
    ),
    PassDef(
        id="state_auditor",
        kind="canonical",
        label="State auditor",
        blocking=False,
        # The Refiner group (§3): it reads the reply back and corrects what the
        # reply guessed about state and narrative drive. Mid-latency rather
        # than background, because its answer changes the *next* prompt.
        model_tier="foreground",
        # Only worth paying for when pass 1 says something actually moved.
        trigger=Trigger(type="on_signal", signal="emotional_shift", op=">=", threshold="minor"),
        sampling=Sampling(temp=0.2, top_p=0.9, rep_penalty=1.05, max_tokens=300),
        output=PassOutput(type="state_modifier", target=SLICE_VARS),
        writes_slice=SLICE_VARS,
        prompt=(
            "You audit emotional state changes for a roleplay character.\n"
            "Given the character's personality, the current state bands and the latest "
            "exchange, decide whether the provisional deltas were right. Correct them if "
            "the character's personality makes them implausible.\n"
            'Reply with JSON only: {"deltas": {"<variable>": <integer -3..3>}, '
            '"reason": "<one short sentence>"}\n'
            "Deltas are absolute corrections applied to the values before this turn, not "
            "adjustments to the provisional ones."
        ),
    ),
    PassDef(
        id="scene",
        kind="canonical",
        label="Scene tracker",
        blocking=False,
        model_tier="background",
        trigger=Trigger(type="on_signal", signal="scene_change", op=">=", threshold="minor"),
        sampling=Sampling(temp=0.4, top_p=0.9, max_tokens=200),
        output=PassOutput(type="gui_panel", target="scene"),
        writes_slice=SLICE_SCENE,
        prompt=(
            "You track the physical setting of a roleplay scene.\n"
            "From the recent exchange, report where the scene is, the weather, and the "
            "time of day. Each answer is a label, not a description.\n"
            'Reply with JSON only: {"place": "<1-3 words>", "weather": "<one word>", '
            '"time": "<one word>"}\n'
            "place: no article. 'Tavern', 'Back room', 'Harbour road'.\n"
            "weather: one word for the sky. 'Rainy', 'Clear', 'Snowy', 'Windy', "
            "'Foggy', 'Cold', 'Hot'.\n"
            "time: exactly one of Dawn, Morning, Midday, Afternoon, Dusk, Evening, "
            "Night, Midnight. Never a clock reading.\n"
            "If something is unchanged or unknown, repeat the current value."
        ),
    ),
    PassDef(
        id="expression",
        kind="canonical",
        label="Expression",
        blocking=False,
        # Refiner as well: the portrait should change while the reply is still
        # on screen, not two turns later.
        model_tier="foreground",
        trigger=Trigger(type="on_signal", signal="emotional_shift", op=">=", threshold="minor"),
        sampling=Sampling(temp=0.1, top_p=0.9, max_tokens=60),
        output=PassOutput(type="gui_panel", target="expression"),
        writes_slice=SLICE_EXPRESSION,
        prompt=(
            "You choose which portrait expression fits the character's last reply.\n"
            'Reply with JSON only: {"emotion": "<one label from the allowed list>"}\n'
            "Choose only from the allowed list given in the context."
        ),
    ),
    PassDef(
        id="background_swap",
        kind="canonical",
        label="Background",
        blocking=False,
        model_tier="background",
        trigger=Trigger(type="on_signal", signal="scene_change", op=">=", threshold="major"),
        sampling=Sampling(temp=0.1, top_p=0.9, max_tokens=60),
        output=PassOutput(type="gui_panel", target="background"),
        writes_slice=SLICE_BACKGROUND,
        # DATA dependency: it consumes the scene slice, not a write-order rule (§5.5).
        depends_on=["scene"],
        prompt=(
            "You pick the background image that matches the current scene.\n"
            'Reply with JSON only: {"background": "<one id from the allowed list>"}\n'
            "Choose only from the allowed list given in the context. If nothing fits, "
            "repeat the current background."
        ),
    ),
    PassDef(
        id="random_event",
        kind="canonical",
        label="Something happens",
        blocking=False,
        model_tier="background",
        # A dice roll, not a model call: on the turns it does not fire this
        # costs nothing at all. Setting the probability to zero switches the
        # whole thing off, which is why there is no second on/off flag to
        # disagree with it.
        trigger=Trigger(type="chance", probability=0.12),
        sampling=Sampling(temp=0.95, top_p=0.95, max_tokens=120),
        output=PassOutput(type="state_modifier", target=SLICE_EVENT),
        writes_slice=SLICE_EVENT,
        # It reads the setting to avoid proposing weather indoors.
        depends_on=["scene"],
        prompt=(
            "You introduce one small unplanned thing into a roleplay scene.\n"
            "It must be something the world does, not something either person "
            "decides: a knock at the door, the rain starting, a stranger sitting "
            "down, a dropped glass, the power going. One sentence, physical and "
            "specific, and plausible for the setting given.\n"
            "Never resolve it, never say how anyone reacts, and never contradict "
            "what just happened.\n"
            'Reply with JSON only: {"event": "<one sentence>"}\n'
            'Reply {"event": ""} if nothing would plausibly intrude right now.'
        ),
    ),
    PassDef(
        id="summary",
        kind="canonical",
        label="Summary",
        blocking=False,
        model_tier="background",
        trigger=Trigger(type="every_n", n=8),
        sampling=Sampling(temp=0.3, top_p=0.9, max_tokens=400),
        output=PassOutput(type="state_modifier", target="summary"),
        writes_slice="summary",
        prompt=(
            "You maintain a rolling summary of a roleplay conversation.\n"
            "Fold the new messages into the existing summary. Keep what still matters, "
            "drop what has been superseded, and stay under the stated budget.\n"
            'Reply with JSON only: {"summary": "<the merged summary>"}\n'
            "Write it as terse third-person narration of what happened and what changed "
            "between the characters."
        ),
    ),
    PassDef(
        id="memory",
        kind="canonical",
        label="Memory",
        blocking=False,
        model_tier="background",
        trigger=Trigger(type="every_n", n=6),
        sampling=Sampling(temp=0.2, top_p=0.9, max_tokens=400),
        output=PassOutput(type="state_modifier", target="memory"),
        writes_slice="memory",
        prompt=(
            "You extract durable facts from a roleplay conversation so they survive "
            "after the raw messages are evicted from context.\n"
            "Extract only facts that will still matter in fifty turns: names, "
            "relationships, promises, injuries, possessions, standing arrangements. "
            "Ignore mood, weather and anything already implied by the character sheet.\n"
            'Reply with JSON only: {"memories": [{"text": "<one fact, one sentence>", '
            '"keys": ["<lookup keyword>", ...]}]}\n'
            "Return an empty list if nothing durable happened."
        ),
    ),
]


CANONICAL_TOGGLES: list[Toggle] = [
    Toggle(
        id="avoid_yes_person",
        label="Avoid yes-person",
        target_pass="basic",
        injection=(
            "Do not be agreeable by default. The character has their own agenda and may "
            "refuse, deflect, argue or change the subject. Never simply validate the "
            "user's last message."
        ),
        default_on=True,
    ),
    Toggle(
        id="anti_slop",
        label="Anti-slop",
        target_pass="basic",
        injection=(
            "Do not reuse phrasing from earlier replies. Do not restate what the user just "
            "said. Never write the user's next turn or narrate their actions or thoughts. "
            "Vary sentence openings and length."
        ),
        default_on=True,
    ),
    Toggle(
        id="scene_tracker",
        label="Scene tracker",
        target_pass="scene",
        enables_pass="scene",
        output="gui_panel",
        default_on=True,
    ),
    Toggle(
        id="state_auditor",
        label="State auditor",
        target_pass="state_auditor",
        enables_pass="state_auditor",
        output="state_modifier",
        default_on=True,
    ),
    Toggle(
        id="expression_pass",
        label="Portrait expressions",
        target_pass="expression",
        enables_pass="expression",
        output="gui_panel",
        default_on=True,
    ),
    Toggle(
        id="memory_pass",
        label="Memory extraction",
        target_pass="memory",
        enables_pass="memory",
        output="state_modifier",
        default_on=True,
    ),
    Toggle(
        id="web_search",
        label="Web search",
        target_pass="basic",
        # No injection and no pass: the search is one HTTP request the turn
        # makes for itself (roadmap 24), so this is a plain switch the
        # scheduler reads. Off by default, and inert until a search URL is
        # configured — there is no bundled provider to fall back on.
        default_on=False,
    ),
]


# ------------------------------------------------------------------ storage


def seed(db: Database) -> None:
    """Install canonical passes and toggles. Never clobbers user edits."""

    def _seed(conn: sqlite3.Connection) -> None:
        for definition in CANONICAL_PASSES:
            conn.execute(
                "INSERT INTO pass_defs(id, kind, enabled, data) VALUES(?,?,?,?) "
                "ON CONFLICT(id) DO NOTHING",
                (definition.id, definition.kind, 1, definition.model_dump_json()),
            )
        for toggle in CANONICAL_TOGGLES:
            conn.execute(
                "INSERT INTO toggles(id, data) VALUES(?,?) ON CONFLICT(id) DO NOTHING",
                (toggle.id, toggle.model_dump_json()),
            )
            conn.execute(
                "INSERT INTO toggle_state(scope, scope_id, toggle_id, enabled) "
                "VALUES('global','',?,?) ON CONFLICT DO NOTHING",
                (toggle.id, int(toggle.default_on)),
            )

    db.write_sync(_seed)
    _regroup(db)


# Passes that moved between tiers after they had already been seeded. Seeding
# never clobbers a stored definition, so without this an install from before
# the three groups were named keeps its auditor in the background group — and
# the panel then offers a Refiner with nothing in it.
REGROUPED = {"state_auditor": "foreground", "expression": "foreground"}


def _regroup(db: Database) -> None:
    def _fix(conn: sqlite3.Connection) -> None:
        for pass_id, tier in REGROUPED.items():
            row = conn.execute(
                "SELECT data FROM pass_defs WHERE id=?", (pass_id,)
            ).fetchone()
            if row is None:
                continue
            try:
                definition = PassDef.model_validate_json(row["data"])
            except ValueError:
                continue
            if definition.model_tier == tier:
                continue
            definition.model_tier = tier
            conn.execute(
                "UPDATE pass_defs SET data=? WHERE id=?",
                (definition.model_dump_json(), pass_id),
            )

    db.write_sync(_fix)


def all_passes(db: Database) -> list[PassDef]:
    rows = db.query("SELECT data, enabled FROM pass_defs ORDER BY rowid")
    out: list[PassDef] = []
    for row in rows:
        try:
            definition = PassDef.model_validate_json(row["data"])
        except ValueError:
            continue
        definition.enabled = bool(row["enabled"])
        out.append(definition)
    return out


def get_pass(db: Database, pass_id: str) -> PassDef | None:
    row = db.query_one("SELECT data, enabled FROM pass_defs WHERE id=?", (pass_id,))
    if row is None:
        return None
    definition = PassDef.model_validate_json(row["data"])
    definition.enabled = bool(row["enabled"])
    return definition


async def save_pass(db: Database, definition: PassDef) -> None:
    await db.write(
        lambda conn: conn.execute(
            "INSERT INTO pass_defs(id, kind, enabled, data) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, enabled=excluded.enabled, "
            "data=excluded.data",
            (definition.id, definition.kind, int(definition.enabled), definition.model_dump_json()),
        )
    )


async def delete_pass(db: Database, pass_id: str) -> bool:
    if (definition := get_pass(db, pass_id)) and definition.kind == "canonical":
        # Canonical passes are part of the engine; disable rather than delete.
        definition.enabled = False
        await save_pass(db, definition)
        return False
    await db.write(lambda conn: conn.execute("DELETE FROM pass_defs WHERE id=?", (pass_id,)))
    return True


def all_toggles(db: Database) -> list[Toggle]:
    rows = db.query("SELECT data FROM toggles ORDER BY rowid")
    return [Toggle.model_validate_json(row["data"]) for row in rows]


def toggle_states(db: Database, character_id: str = "", chat_id: str = "") -> dict[str, bool]:
    """Resolve toggle on/off with per-chat beating per-character beating global."""
    resolved: dict[str, bool] = {t.id: t.default_on for t in all_toggles(db)}
    for scope, scope_id in (("global", ""), ("per_character", character_id), ("per_chat", chat_id)):
        if scope != "global" and not scope_id:
            continue
        for row in db.query(
            "SELECT toggle_id, enabled FROM toggle_state WHERE scope=? AND scope_id=?",
            (scope, scope_id),
        ):
            resolved[row["toggle_id"]] = bool(row["enabled"])
    return resolved


async def set_toggle(
    db: Database, toggle_id: str, enabled: bool, scope: str = "global", scope_id: str = ""
) -> None:
    await db.write(
        lambda conn: conn.execute(
            "INSERT INTO toggle_state(scope, scope_id, toggle_id, enabled) VALUES(?,?,?,?) "
            "ON CONFLICT(scope, scope_id, toggle_id) DO UPDATE SET enabled=excluded.enabled",
            (scope, scope_id, toggle_id, int(enabled)),
        )
    )


def active_injections(db: Database, states: dict[str, bool], target_pass: str) -> list[str]:
    """Injection text from every enabled toggle aimed at this pass (§10)."""
    out: list[str] = []
    for toggle in all_toggles(db):
        if not states.get(toggle.id, toggle.default_on):
            continue
        if toggle.target_pass != target_pass or not toggle.injection:
            continue
        out.append(toggle.injection)
    return out


def passes_disabled_by_toggle(db: Database, states: dict[str, bool]) -> set[str]:
    disabled: set[str] = set()
    for toggle in all_toggles(db):
        if toggle.enables_pass and not states.get(toggle.id, toggle.default_on):
            disabled.add(toggle.enables_pass)
    return disabled


def export_library(db: Database) -> dict:
    return {
        "passes": [json.loads(p.model_dump_json()) for p in all_passes(db)],
        "toggles": [json.loads(t.model_dump_json()) for t in all_toggles(db)],
    }
