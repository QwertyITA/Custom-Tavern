"""The pass engine (§4, §5): triggers, gating, concurrency, swipe branching."""

from __future__ import annotations

import asyncio

from app import repo, state as state_mod
from app.config import SETTINGS
from app.models import PassDef, Trigger
from app.passes import registry
from app.passes.scheduler import PassScheduler, TurnContext
from app.state import SLICE_VARS, read_slice

from .conftest import events_of, sync, turn


def context(chat, character, *, turn_no=1, signals=None) -> TurnContext:
    return TurnContext(
        chat=chat,
        character=character,
        settings=SETTINGS,
        turn=turn_no,
        signals=signals or {},
        schema=state_mod.load_schema(None),
    )


# --------------------------------------------------------------- triggers


def test_every_turn_always_fires(sched, chat, character):
    definition = PassDef(id="p", trigger=Trigger(type="every_turn"))
    assert sched.trigger_fires(definition, context(chat, character, turn_no=7))


def test_every_n_fires_on_multiples(sched, chat, character):
    definition = PassDef(id="p", trigger=Trigger(type="every_n", n=3))
    fired = [
        sched.trigger_fires(definition, context(chat, character, turn_no=t)) for t in range(1, 7)
    ]
    assert fired == [False, False, True, False, False, True]


def test_manual_never_fires_on_its_own(sched, chat, character):
    definition = PassDef(id="p", trigger=Trigger(type="manual"))
    assert not sched.trigger_fires(definition, context(chat, character))


def test_signal_trigger_gates_on_the_rubric(sched, chat, character):
    """This is the cost lever: expensive passes decide their own worth (§5.2)."""
    definition = PassDef(
        id="p", trigger=Trigger(type="on_signal", signal="scene_change", op=">=", threshold="minor")
    )
    assert not sched.trigger_fires(definition, context(chat, character, signals={"scene_change": "none"}))
    assert sched.trigger_fires(definition, context(chat, character, signals={"scene_change": "minor"}))
    assert sched.trigger_fires(definition, context(chat, character, signals={"scene_change": "major"}))


def test_missing_signal_reads_as_none(sched, chat, character):
    definition = PassDef(
        id="p", trigger=Trigger(type="on_signal", signal="absent", op=">=", threshold="minor")
    )
    assert not sched.trigger_fires(definition, context(chat, character))


def test_major_only_trigger_ignores_minor(sched, chat, character):
    definition = PassDef(
        id="p", trigger=Trigger(type="on_signal", signal="s", op=">=", threshold="major")
    )
    assert not sched.trigger_fires(definition, context(chat, character, signals={"s": "minor"}))


def test_eligibility_skips_the_reply_pass_and_disabled_passes(sched, chat, character):
    definitions = registry.all_passes(sched.db)
    ctx = context(chat, character, signals={"emotional_shift": "major", "scene_change": "major"})
    eligible = sched.eligible(definitions, ctx, disabled={"scene"})
    ids = {d.id for d in eligible}
    assert "basic" not in ids
    assert "scene" not in ids
    assert "state_auditor" in ids


def test_toggle_off_disables_its_pass(db, sched, chat, character):
    sync(registry.set_toggle(db, "scene_tracker", False))
    states = registry.toggle_states(db, character.id, chat["id"])
    assert "scene" in registry.passes_disabled_by_toggle(db, states)


# ------------------------------------------------------------------- turn


def test_turn_streams_a_reply_and_stores_it(sched, chat, character):
    events = sync(turn(sched, chat["id"], "Cold out."))
    assert events_of(events, "delta"), "reply should stream"
    reply = events_of(events, "reply")[0]
    assert reply["message"]["role"] == "assistant"

    messages = repo.list_messages(sched.db, chat["id"])
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["text"] == reply["message"]["text"]


def test_reply_text_never_contains_the_state_marker(sched, chat, character):
    events = sync(turn(sched, chat["id"], "Cold out."))
    text = events_of(events, "reply")[0]["message"]["text"]
    assert "<<<state>>>" not in text
    streamed = "".join(e["text"] for e in events_of(events, "delta"))
    assert "<<<state>>>" not in streamed


def test_provisional_state_commits_immediately(sched, chat, character):
    """The reply is never gated on downstream passes (§1)."""
    sync(turn(sched, chat["id"], "Cold out."))
    stored = read_slice(sched.db, chat["id"], SLICE_VARS)
    assert stored is not None
    assert stored["source_turn"] == 1


def test_nudges_run_before_any_model_pass(sched, chat, character):
    events = sync(turn(sched, chat["id"], "thanks for that"))
    fired = events_of(events, "nudges")
    assert fired and fired[0]["fired"] == ["willingness+1"]


def test_assembly_report_is_emitted_for_the_hud(sched, chat, character):
    events = sync(turn(sched, chat["id"], "hello"))
    report = events_of(events, "assembly")[0]
    assert report["total_tokens"] > 0
    assert "prefix" in report["sections"]


def test_pass_runs_are_logged_for_cost_accounting(sched, chat, character):
    sync(turn(sched, chat["id"], "Cold out."))
    rows = sched.db.query("SELECT pass_id, status, tokens_in, tokens_out FROM pass_runs")
    basic = [r for r in rows if r["pass_id"] == "basic"]
    assert basic and basic[0]["status"] == "done"
    assert basic[0]["tokens_in"] > 0 and basic[0]["tokens_out"] > 0


def test_each_pass_gets_one_run_row_per_turn(sched, chat, character):
    sync(turn(sched, chat["id"], "You were there for the wreck."))
    rows = sched.db.query(
        "SELECT pass_id, COUNT(*) c FROM pass_runs WHERE turn=1 GROUP BY pass_id"
    )
    assert all(row["c"] == 1 for row in rows), "a pass must not leave duplicate HUD rows"


def test_background_passes_complete_and_write_their_slices(sched, chat, character):
    async def scenario():
        # Force every signal high so the gated passes all fire.
        for text in ["Cold out.", "You were there for the wreck.", "Harrow wants paying."]:
            await turn(sched, chat["id"], text)

    sync(scenario())
    rows = {
        r["pass_id"]: r["status"]
        for r in sched.db.query("SELECT pass_id, status FROM pass_runs WHERE status != 'pending'")
    }
    assert "failed" not in rows.values(), rows
    slices = state_mod.read_all_slices(sched.db, chat["id"])
    assert SLICE_VARS in slices


def test_auditor_correction_overwrites_the_provisional_write(sched, chat, character):
    async def scenario():
        for text in ["Cold out.", "You were there for the wreck.", "Harrow wants paying."]:
            await turn(sched, chat["id"], text)

    sync(scenario())
    stored = read_slice(sched.db, chat["id"], SLICE_VARS)
    if stored["source_pass"] == "state_auditor":
        assert stored["provisional"] is False


def test_a_failing_pass_does_not_break_the_turn(db, sched, chat, character):
    broken = PassDef(
        id="broken",
        trigger=Trigger(type="every_turn"),
        model_tier="background",
        prompt="x",
        writes_slice="state.broken",
        retries=0,
    )
    sync(registry.save_pass(db, broken))

    async def scenario():
        original = sched._build_pass_input

        def explode(ctx, definition):
            if definition.id == "broken":
                raise RuntimeError("boom")
            return original(ctx, definition)

        sched._build_pass_input = explode
        return await turn(sched, chat["id"], "Cold out.")

    events = sync(scenario())
    assert events_of(events, "reply"), "the reply must survive a broken background pass"
    row = sched.db.query_one("SELECT status, error FROM pass_runs WHERE pass_id='broken'")
    assert row["status"] == "failed"
    assert "boom" in row["error"]


def test_await_pending_returns_promptly_when_nothing_is_running(sched, chat):
    assert sync(sched.await_pending(chat["id"])) == 0


# ----------------------------------------------------------------- swipes


def test_swipe_adds_a_variant_without_a_new_message(sched, chat, character):
    async def scenario():
        await turn(sched, chat["id"], "Cold out.")
        message = repo.list_messages(sched.db, chat["id"])[-1]
        events = [e async for e in sched.run_swipe(message["id"])]
        await sched.await_pending(chat["id"], timeout=20)
        return message, events

    message, events = sync(scenario())
    assert events_of(events, "variant")
    assert len(repo.list_messages(sched.db, chat["id"])) == 2
    assert len(repo.list_variants(sched.db, message["id"])) == 2


def test_swipe_rolls_back_the_discarded_variant_state(sched, chat, character):
    """State must bind only to the variant you land on (§9)."""
    async def scenario():
        await turn(sched, chat["id"], "Cold out.")
        message = repo.list_messages(sched.db, chat["id"])[-1]
        before = read_slice(sched.db, chat["id"], SLICE_VARS)
        events = [e async for e in sched.run_swipe(message["id"])]
        await sched.await_pending(chat["id"], timeout=20)
        return before, events

    before, events = sync(scenario())
    rollback = events_of(events, "rollback")
    assert rollback and rollback[0]["writes"] >= 1

    after = read_slice(sched.db, chat["id"], SLICE_VARS)
    # One turn's worth of change, not two — discarded swipes never accumulate.
    assert after["source_turn"] == before["source_turn"] == 1


def test_repeated_swipes_do_not_accumulate_state(sched, chat, character):
    async def scenario():
        await turn(sched, chat["id"], "Cold out.")
        message = repo.list_messages(sched.db, chat["id"])[-1]
        values = []
        for _ in range(3):
            async for _event in sched.run_swipe(message["id"]):
                pass
            await sched.await_pending(chat["id"], timeout=20)
            values.append(dict(read_slice(sched.db, chat["id"], SLICE_VARS)["value"]))
        return values

    values = sync(scenario())
    assert values[0] == values[1] == values[2]


def test_swipe_on_a_user_message_is_refused(sched, chat, character):
    async def scenario():
        await turn(sched, chat["id"], "Cold out.")
        user_message = repo.list_messages(sched.db, chat["id"])[0]
        return [e async for e in sched.run_swipe(user_message["id"])]

    events = sync(scenario())
    assert events_of(events, "error")
