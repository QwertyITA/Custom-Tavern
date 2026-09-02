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
        id="post_process",
        kind="canonical",
        label="Post-process",
        # Blocking like "basic" — the user is not shown a reply until this has
        # had its turn — but it is not run through the generic blocking-passes
        # loop the way §4 step 3 describes; there has only ever been one thing
        # in that loop ("basic"), and this makes two, so both are still
        # invoked by hand from _run_reply/_run_swipe rather than through a
        # loop built for a list of one.
        blocking=True,
        # Takes over the tier the Refiner group used to own (§7.7's ancestor,
        # now retired — see KNOWN-ISSUES.md). Same reasoning as before: an
        # on-device model answers this fast enough to sit in the critical path
        # without the user waiting on the slow/paid tier twice.
        model_tier="foreground",
        # Never fires through the generic eligible() loop (§ trigger_fires) —
        # excluded there by id, the same way "basic" is. This is the shape
        # PassDef expects a pass nothing schedules generically to declare
        # itself as; it also means it never appears in the "run every N
        # turns" spacing UI (§ static/app.js spacedPasses), which would be a
        # control this pass cannot actually honour.
        trigger=Trigger(type="manual"),
        sampling=Sampling(temp=0.2, top_p=0.9, rep_penalty=1.05, max_tokens=1200),
        output=PassOutput(type="reply"),
        animation="cogs",
        expects_json=False,
        prompt=(
            "You are a copy editor for one character's turn in a piece of "
            "roleplay fiction, written by another model. Fix only mechanical "
            "problems and leave the content, the events and the character's "
            "own voice alone:\n"
            "- Grammar, spelling and punctuation.\n"
            "- Names: a misspelled or wrong character name, corrected to the "
            "name actually in use.\n"
            "- Markup: speech in \"double quotes\", action in *single "
            "asterisks*, every marker that opens closes in the same "
            "paragraph.\n"
            "- Point of view and paragraph length, against the targets given "
            "below, if any are given.\n"
            "Nothing else changes: not word choice, not pacing, not what "
            "happens. If the reply already has none of these problems, "
            "return it completely unchanged.\n"
            "Reply with the corrected text and nothing else — no preamble, "
            "no explanation, no quotation marks around the whole thing."
        ),
    ),
    PassDef(
        id="state_auditor",
        kind="canonical",
        label="State auditor",
        blocking=False,
        # Moved to the background tier along with "expression" below when
        # post_process took over "foreground" — its answer used to change the
        # *next* prompt quickly enough to be worth a mid-latency tier of its
        # own; background is slower to land but the correction still arrives
        # before the turn after next, which is the only place a stale one
        # would actually show.
        model_tier="background",
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
            "You label the setting of a roleplay scene. One word per field.\n"
            "These are labels for a status bar, not descriptions: no articles, "
            "no adjectives, no phrases, nothing after a comma.\n"
            'Reply with JSON only: {"place": "", "weather": "", "time": ""}\n'
            "place: the room or the ground underfoot, in one word. Tavern. "
            "Cellar. Road. Kitchen. Forest. Never where it is in relation to "
            "something else — 'Tavern', never 'Room by a window'.\n"
            "weather: the sky, in one word. Rainy. Clear. Snowy. Windy. Foggy. "
            "Cold. Hot. Indoors, it is still whatever it is outside.\n"
            "time: exactly one of Dawn, Morning, Midday, Afternoon, Dusk, "
            "Evening, Night, Midnight. Never a clock reading.\n"
            "Unchanged or unknown: repeat the value you were given.\n"
            'Example: {"place": "Tavern", "weather": "Rainy", "time": "Evening"}'
        ),
    ),
    PassDef(
        id="expression",
        kind="canonical",
        label="Expression",
        blocking=False,
        # Moved to background with state_auditor above — the portrait now
        # changes a beat later than the reply that earned it rather than
        # alongside it, which is the trade for post_process owning the
        # foreground tier instead.
        model_tier="background",
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
            "You pick the background image that best matches where the scene is "
            "now, using each option's description and the recent exchange — not "
            "just the current scene line, which is only three words.\n"
            'Reply with JSON only: {"background": "<one id from the allowed list>"}\n'
            "Choose only an id from the allowed list given in the context, exactly "
            "as written there. If nothing fits, repeat the current background."
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
        # Off until asked for. A summary is not free context — it is the *only*
        # account of everything it covers, because a covered message leaves the
        # prompt for good (§7.2). Written by the cheapest model in the stack it
        # routinely gets the premise and the standing facts wrong, and then that
        # is what the character knows. Nothing is evicted while it is off, so a
        # chat under its context budget keeps every word it actually said.
        enabled=False,
        blocking=False,
        model_tier="background",
        # Not a turn count. It used to fire every eight turns whatever the
        # context was doing, so a chat at an eighth of its budget was being
        # summarised — and each firing covered turns still sitting in the
        # window, where the summary contradicted the transcript underneath it.
        # Now it runs when the prompt has actually run out of room, over the
        # messages that have actually left it.
        trigger=Trigger(type="over_budget"),
        sampling=Sampling(temp=0.3, top_p=0.9, max_tokens=400),
        output=PassOutput(type="state_modifier", target="summary"),
        writes_slice="summary",
        # Written against the way the old one failed. It lost the premise —
        # who these people are to each other and what the standing arrangement
        # is — because nothing later in the chat restates it, so a model told to
        # "drop what has been superseded" drops it. It also reversed who said
        # what, and recorded an agreement where there had been a refusal.
        prompt=(
            "You maintain a rolling summary of a roleplay conversation. It replaces "
            "messages that have left the context window, so anything you leave out is "
            "gone for good.\n"
            "Fold the new messages into the existing summary, and keep, in this order "
            "of priority:\n"
            "1. The premise — who these people are to each other, where they are, and "
            "any standing arrangement, debt, threat or promise between them. Carry it "
            "forward every time. It is stated once, at the start, and never repeated, "
            "so it is the thing most easily lost.\n"
            "2. What is unresolved: an open question, a demand not yet answered, a "
            "refusal that still stands.\n"
            "3. What changed between them, and what either of them decided.\n"
            "Drop only detail that later events have genuinely settled.\n"
            'Reply with JSON only: {"summary": "<the merged summary>"}\n'
            "Terse third-person narration, under the stated budget. Name who did and "
            "said each thing; never attribute one speaker's words to the other. Do not "
            "record an agreement that was not given — a refusal, a threat and a bargain "
            "struck are three different endings and the difference is the whole point."
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
                # From the definition, not a hardcoded 1: the column is what the
                # scheduler reads, so a canonical pass that ships switched off
                # was seeded on regardless of what it said about itself.
                (
                    definition.id,
                    definition.kind,
                    int(definition.enabled),
                    definition.model_dump_json(),
                ),
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
    _resupersede(db)


# Passes that moved between tiers after they had already been seeded. Seeding
# never clobbers a stored definition, so without this an install from before
# a move keeps its passes on the tier they were seeded on — the first time
# this ran it was background -> foreground, naming the group that became the
# Refiner; this is the same mechanism moving both back to background now that
# foreground belongs to post_process instead (§ CANONICAL_PASSES above,
# KNOWN-ISSUES.md). Unconditional, same as before: an install where someone
# had hand-assigned either pass to a different tier gets that choice
# overwritten too, exactly as the original move did.
REGROUPED = {"state_auditor": "background", "expression": "background"}

# Shipped prompts that have since been replaced, by pass. An install that still
# holds one of these has never edited that pass, so the new shipped version can
# take its place; anything else is someone's own writing and is left alone.
#
# The summary's was the reason this exists. It fired on a turn count rather than
# on context pressure, and it told the model to "drop what has been superseded"
# — which loses the premise, since nothing in a chat ever restates it.
SUPERSEDED_PROMPTS: dict[str, tuple[str, ...]] = {
    "summary": (
        "You maintain a rolling summary of a roleplay conversation.\n"
        "Fold the new messages into the existing summary. Keep what still matters, "
        "drop what has been superseded, and stay under the stated budget.\n"
        'Reply with JSON only: {"summary": "<the merged summary>"}\n'
        "Write it as terse third-person narration of what happened and what changed "
        "between the characters.",
    ),
}


def _resupersede(db: Database) -> None:
    """Bring an unedited canonical pass up to its current shipped version.

    Only the prompt and the trigger, and only when the stored prompt is one this
    engine used to ship: those two are the engine's own workings rather than
    settings, and an install that had already been seeded would otherwise keep
    a prompt that has since been found to be wrong. `enabled` is deliberately
    not touched — a switch is the user's, however it came to be where it is.
    """
    shipped = {p.id: p for p in CANONICAL_PASSES}

    def _fix(conn: sqlite3.Connection) -> None:
        for pass_id, old_prompts in SUPERSEDED_PROMPTS.items():
            current = shipped.get(pass_id)
            row = conn.execute("SELECT data FROM pass_defs WHERE id=?", (pass_id,)).fetchone()
            if current is None or row is None:
                continue
            try:
                definition = PassDef.model_validate_json(row["data"])
            except ValueError:
                continue
            if definition.prompt.strip() not in {p.strip() for p in old_prompts}:
                continue
            definition.prompt = current.prompt
            definition.trigger = current.trigger
            conn.execute(
                "UPDATE pass_defs SET data=? WHERE id=?",
                (definition.model_dump_json(), pass_id),
            )

    db.write_sync(_fix)


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
