"""The random events pass, and the `chance` trigger behind it.

The design constraint is that it must cost nothing on the turns it does not
fire — a dice roll, not a model call — and that when it does fire it must not
make the reply wait. It is invented in the background after one turn and woven
into the next, which is also why it has to be consumed exactly once.
"""

from __future__ import annotations

import pytest

from app import assembly, state as state_mod
from app.config import Settings
from app.models import PassDef, Trigger
from app.passes import registry
from app.passes.scheduler import PassScheduler, TurnContext
from app.state import SLICE_EVENT, read_slice
from tests.conftest import sync, turn


def a_context(chat, character, settings, turn_number: int = 1) -> TurnContext:
    return TurnContext(chat=chat, character=character, settings=settings, turn=turn_number)


# ------------------------------------------------------------ the trigger


def test_a_zero_chance_never_fires(sched, chat, character):
    """Zero is how the whole thing is switched off — no second flag that has to
    agree with the frequency."""
    definition = PassDef(id="x", trigger=Trigger(type="chance", probability=0.0))
    ctx = a_context(chat, character, sched.settings)
    assert not any(sched.trigger_fires(definition, ctx) for _ in range(200))


def test_a_certain_chance_always_fires(sched, chat, character):
    definition = PassDef(id="x", trigger=Trigger(type="chance", probability=1.0))
    ctx = a_context(chat, character, sched.settings)
    assert all(sched.trigger_fires(definition, ctx) for _ in range(50))


def test_the_chance_is_roughly_the_frequency(sched, chat, character):
    definition = PassDef(id="x", trigger=Trigger(type="chance", probability=0.25))
    ctx = a_context(chat, character, sched.settings)
    fired = sum(sched.trigger_fires(definition, ctx) for _ in range(2000))
    assert 350 < fired < 650, fired


@pytest.mark.parametrize("probability", [-1.0, 5.0])
def test_a_probability_outside_the_range_is_clamped(sched, chat, character, probability):
    definition = PassDef(id="x", trigger=Trigger(type="chance", probability=probability))
    ctx = a_context(chat, character, sched.settings)
    results = {sched.trigger_fires(definition, ctx) for _ in range(50)}
    assert results == ({False} if probability < 0 else {True})


def test_the_default_probability_is_zero_for_a_pass_that_does_not_say():
    """So adding `chance` to a pass without setting a frequency does nothing,
    rather than firing every turn."""
    assert Trigger(type="chance").probability == 0.0


# ------------------------------------------------------------- the pass


def test_the_pass_ships_gated_on_chance():
    definition = next(p for p in registry.CANONICAL_PASSES if p.id == "random_event")
    assert definition.trigger.type == "chance"
    assert 0 < definition.trigger.probability < 0.5, "occasional, not constant"
    assert definition.blocking is False, "it must never make a reply wait"
    assert definition.writes_slice == SLICE_EVENT


def test_the_event_slice_is_shared_not_per_character():
    """A knock at the door happens to the room, not to one person in it."""
    assert SLICE_EVENT in state_mod.SHARED_SLICES
    assert SLICE_EVENT not in state_mod.PER_CHARACTER_SLICES


def test_its_prompt_forbids_deciding_for_anyone():
    definition = next(p for p in registry.CANONICAL_PASSES if p.id == "random_event")
    assert "not something either person" in definition.prompt
    assert "Never resolve it" in definition.prompt


# --------------------------------------------------------- in the prompt


@pytest.fixture
def no_new_events(db):
    """Stop the pass inventing a *fresh* event mid-test.

    The consumption tests are about one specific event being spent. With the
    pass live at its shipped 12% it occasionally fires in the same turn and
    writes a new, unused one over the top — correct behaviour, and pure noise
    here. Without this the suite is flaky about one run in eight.
    """
    from app.passes import registry

    definition = registry.get_pass(db, "random_event")
    definition.trigger.probability = 0.0
    sync(registry.save_pass(db, definition))
    return definition


def write_event(db, chat_id: str, text: str, used: bool = False) -> None:
    sync(state_mod.write_slice(
        db, chat_id, SLICE_EVENT, {"event": text, "used": used},
        source_turn=1, source_pass="random_event",
    ))


def test_a_pending_event_reaches_the_prompt(db, chat, character):
    write_event(db, chat["id"], "A gull lands on the rail and stares in.")
    out = assembly.build_reply_context(db, chat, character, Settings())
    assert "A gull lands on the rail" in out.volatile
    assert "Work it into your reply" in out.volatile


def test_a_used_event_does_not(db, chat, character):
    write_event(db, chat["id"], "A gull lands on the rail.", used=True)
    assert "gull" not in assembly.build_reply_context(db, chat, character, Settings()).volatile


def test_an_empty_event_adds_nothing(db, chat, character):
    """The pass is told to return an empty string when nothing would plausibly
    intrude, and that must not leave a bare heading in the prompt."""
    write_event(db, chat["id"], "   ")
    assert "Something is happening" not in (
        assembly.build_reply_context(db, chat, character, Settings()).volatile
    )


def test_no_event_at_all_adds_nothing(db, chat, character):
    assert "Something is happening" not in (
        assembly.build_reply_context(db, chat, character, Settings()).volatile
    )


def test_the_event_sits_in_the_volatile_band(db, chat, character):
    """It changes turn to turn, so putting it anywhere earlier would cost a
    cache rebuild every reply (§7.1)."""
    write_event(db, chat["id"], "The lamp gutters.")
    out = assembly.build_reply_context(db, chat, character, Settings())
    part = next(p for p in out.parts if p["id"] == "event")
    assert part["band"] == "volatile"


def test_it_can_be_switched_off_like_any_section(db, chat, character):
    write_event(db, chat["id"], "The lamp gutters.")
    settings = Settings(prompt_sections=[{"id": "event", "enabled": False}])
    assert "lamp" not in assembly.build_reply_context(db, chat, character, settings).volatile


# ---------------------------------------------------------- consumption


def test_a_reply_spends_the_event(sched, chat, character, no_new_events):
    """Otherwise the same knock at the door happens on every turn forever."""
    write_event(sched.db, chat["id"], "Someone knocks twice and waits.")
    sync(turn(sched, chat["id"], "Who could that be?"))

    stored = read_slice(sched.db, chat["id"], SLICE_EVENT)
    assert stored["value"].get("used") is True
    assert assembly.pending_event(sched.db, chat["id"]) == ""


def test_the_event_was_actually_in_that_reply_s_prompt(sched, chat, character, no_new_events):
    """Consumed *after* being used, not before — the ordering is the whole
    point, and getting it backwards would spend it unread.

    Checked against the recorded itemisation (§15) rather than the reply text:
    that is the record of what was really sent, and the echo backend quotes
    only the user's message back."""
    import json

    write_event(sched.db, chat["id"], "A shutter bangs somewhere upstairs.")
    sync(turn(sched, chat["id"], "What was that?"))

    rows = sched.db.query(
        "SELECT prompt FROM pass_runs WHERE chat_id=? AND prompt IS NOT NULL",
        (chat["id"],),
    )
    assert rows, "a reply recorded its prompt"
    parts = json.loads(rows[-1]["prompt"])
    event = next((p for p in parts if p["id"] == "event"), None)
    assert event is not None, [p["id"] for p in parts]
    assert "shutter bangs" in event["text"]


def test_a_second_turn_gets_no_event(sched, chat, character, no_new_events):
    write_event(sched.db, chat["id"], "A cart goes past outside.")
    sync(turn(sched, chat["id"], "first"))
    sync(turn(sched, chat["id"], "second"))

    out = assembly.build_reply_context(sched.db, chat, character, Settings())
    assert "cart" not in out.volatile


def test_consuming_nothing_is_harmless(sched, chat, character, no_new_events):
    """No event pending is the common case, and it must not write a row."""
    sync(turn(sched, chat["id"], "hello"))
    assert read_slice(sched.db, chat["id"], SLICE_EVENT) is None


def test_an_empty_event_is_not_marked_used(db, chat, character, sched, no_new_events):
    """Nothing was spent, so there is nothing to mark — and marking it would
    stop a real event written later on the same turn from being seen."""
    write_event(db, chat["id"], "")
    sync(turn(sched, chat["id"], "hello"))
    stored = read_slice(sched.db, chat["id"], SLICE_EVENT)
    assert stored["value"].get("used") is not True


# ------------------------------------------------------- through the API


def test_the_frequency_is_editable_through_the_pass(client):
    """It lives on the trigger rather than in settings, so there is one number
    instead of two that have to agree."""
    passes = client.get("/api/passes").json()
    found = next(p for p in passes if p["id"] == "random_event")
    assert found["trigger"]["type"] == "chance"

    found["trigger"]["probability"] = 0.4
    assert client.put(f"/api/passes/{found['id']}", json=found).status_code == 200

    again = next(p for p in client.get("/api/passes").json() if p["id"] == "random_event")
    assert again["trigger"]["probability"] == 0.4


def test_setting_it_to_zero_switches_it_off(client):
    passes = client.get("/api/passes").json()
    found = next(p for p in passes if p["id"] == "random_event")
    found["trigger"]["probability"] = 0.0
    client.put(f"/api/passes/{found['id']}", json=found)

    again = next(p for p in client.get("/api/passes").json() if p["id"] == "random_event")
    assert again["trigger"]["probability"] == 0.0
