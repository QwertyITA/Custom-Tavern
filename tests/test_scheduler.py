"""The pass engine (§4, §5): triggers, gating, concurrency, swipe branching."""

from __future__ import annotations

import asyncio
import json

from app import assembly, repo, state as state_mod
from app.config import SETTINGS
from app.models import PassDef, Trigger
from app.passes import registry
from app.passes.scheduler import PassScheduler, TurnContext
from app.state import SLICE_BACKGROUND, SLICE_VARS, read_slice, slice_for

from .conftest import drain, events_of, sync, turn


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
    stored = read_slice(sched.db, chat["id"], slice_for(SLICE_VARS, character.id))
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
    assert slice_for(SLICE_VARS, character.id) in slices, (
        'variables are namespaced per character (§15)'
    )


def test_auditor_correction_overwrites_the_provisional_write(sched, chat, character):
    async def scenario():
        for text in ["Cold out.", "You were there for the wreck.", "Harrow wants paying."]:
            await turn(sched, chat["id"], text)

    sync(scenario())
    stored = read_slice(sched.db, chat["id"], slice_for(SLICE_VARS, character.id))
    if stored["source_pass"] == "state_auditor":
        assert stored["provisional"] is False


def test_background_swap_picks_by_description_not_just_a_bare_id(sched, chat, character, monkeypatch):
    """§ _build_pass_input's background_swap branch — the prompt carries
    each background's own description, not just its filename, so the pass
    has something to reason with beyond a name. Global (§ Settings.
    background_meta, config.py) rather than per-character.

    `monkeypatch`, not a plain assignment: `sched.settings` is the process-
    wide `config.SETTINGS` (§ the `sched` fixture), which the `pristine_settings`
    autouse fixture does not actually reset between tests in this file — it
    rebinds `config.SETTINGS` itself, but the `SETTINGS` name this file
    imported at module load keeps pointing at the original object. A plain
    assignment here would leak into whichever test runs next.
    """
    monkeypatch.setattr(
        sched.settings, "background_meta",
        {"tavern.svg": {"description": "A cosy firelit common room."}},
    )
    definition = next(d for d in registry.all_passes(sched.db) if d.id == "background_swap")

    task, messages, handler = sched._build_pass_input(context(chat, character), definition)
    assert handler is not None
    body = task + " " + " ".join(m["content"] for m in messages)
    assert "tavern.svg" in body
    assert "cosy firelit common room" in body


def test_background_swap_excludes_an_image_marked_auto_false(sched, chat, character, monkeypatch):
    """The eye toggle (Theme → Backdrop) pulls an image out of the automatic
    pick without touching anything else about it. (§ monkeypatch note above.)"""
    monkeypatch.setattr(sched.settings, "background_meta", {"tavern.svg": {"auto": False}})
    definition = next(d for d in registry.all_passes(sched.db) if d.id == "background_swap")

    task, messages, handler = sched._build_pass_input(context(chat, character), definition)
    body = (task + " " + " ".join(m["content"] for m in messages)) if handler else ""
    assert "tavern.svg" not in body


def test_background_swap_makes_no_change_on_an_invalid_pick(sched, chat, character, monkeypatch):
    """The model naming a filename outside today's eligible set —
    hallucinated, or excluded/deleted since the prompt was built — must
    change nothing, per the prompt's own contract ('if nothing fits, repeat
    the current background') and the handler that actually enforces it
    (§ scheduler.py, _handler_generic's background_swap branch)."""
    from app.providers import echo as echo_provider

    # A clean slate (§ monkeypatch note on the test above) — nothing excluded,
    # so the pass has "tavern.svg" to offer and this is testing the pick
    # itself, not an empty allowed list.
    monkeypatch.setattr(sched.settings, "background_meta", {})
    monkeypatch.setattr(
        echo_provider, "_first_background_id", lambda request: "not-a-real-background"
    )
    definition = next(d for d in registry.all_passes(sched.db) if d.id == "background_swap")

    async def scenario():
        ctx = context(chat, character, signals={"scene_change": "major"})
        launched = sched._launch_background(ctx)
        assert "background_swap" in launched
        await sched.await_pending(chat["id"])

    sync(scenario())
    row = sched.db.query_one(
        "SELECT status FROM pass_runs WHERE chat_id=? AND pass_id='background_swap'",
        (chat["id"],),
    )
    assert row["status"] == "stale"
    assert read_slice(sched.db, chat["id"], slice_for(SLICE_BACKGROUND, character.id)) is None


def test_background_swap_writes_the_global_backdrop_not_a_chat_slice(
    sched, chat, character, monkeypatch, isolated_settings
):
    """The fix for "reopening a chat shows the wrong background" — the pick
    has to live in Settings.background, the same field the Theme panel's
    own manual picker writes and the one thing every chat reads, not a
    per-chat slice nothing ever restores when a chat is reopened."""
    monkeypatch.setattr(sched.settings, "background_meta", {})
    # Different from what echo is about to pick ("tavern.svg", the only
    # shipped image — § _first_background_id), or the write is correctly a
    # no-op (§ the "already showing that one" guard) rather than the real
    # change this test means to exercise.
    monkeypatch.setattr(sched.settings, "background", "none")

    async def scenario():
        ctx = context(chat, character, signals={"scene_change": "major"})
        launched = sched._launch_background(ctx)
        assert "background_swap" in launched
        await sched.await_pending(chat["id"])

    sync(scenario())
    row = sched.db.query_one(
        "SELECT status FROM pass_runs WHERE chat_id=? AND pass_id='background_swap'",
        (chat["id"],),
    )
    assert row["status"] == "done"
    assert sched.settings.background == "tavern.svg"
    assert read_slice(sched.db, chat["id"], slice_for(SLICE_BACKGROUND, character.id)) is None
    assert json.loads(isolated_settings.read_text())["background"] == "tavern.svg"


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


def test_track_drops_the_chat_entry_once_its_tasks_finish(sched, chat):
    """§KNOWN-ISSUES.md, 'PassScheduler._pending never removes an emptied
    chat entry' — a dict key with nothing in it, kept forever for every chat
    that has ever run a background pass."""
    async def scenario():
        task = asyncio.ensure_future(asyncio.sleep(0))
        sched._track(chat["id"], task)
        assert chat["id"] in sched._pending
        await task
        # The done-callback runs on the loop's next pass, not synchronously
        # the instant the task finishes — give it that pass before checking.
        await asyncio.sleep(0)

    sync(scenario())
    assert chat["id"] not in sched._pending


def test_background_passes_are_capped_per_chat(sched, chat, character, monkeypatch):
    """§KNOWN-ISSUES.md, 'No cap on concurrent background passes per chat' —
    more passes queued for one chat than the cap allows must still never
    have more than the cap actually running (calling a backend) at once."""
    from app.passes.scheduler import MAX_CONCURRENT_BACKGROUND_PASSES_PER_CHAT

    concurrent = 0
    peak = 0

    async def fake_execute(ctx, definition, run_id):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.01)
        concurrent -= 1

    monkeypatch.setattr(sched, "_execute", fake_execute)
    ctx = context(chat, character)
    over_the_cap = MAX_CONCURRENT_BACKGROUND_PASSES_PER_CHAT + 5

    async def scenario():
        tasks = [
            asyncio.create_task(sched._run_background(
                ctx, PassDef(id=f"pass{i}"), [], asyncio.Event(), f"run{i}"
            ))
            for i in range(over_the_cap)
        ]
        await asyncio.gather(*tasks)

    sync(scenario())
    assert peak == MAX_CONCURRENT_BACKGROUND_PASSES_PER_CHAT


def test_two_turns_in_one_chat_do_not_run_at_once(sched, chat, character, monkeypatch):
    """§KNOWN-ISSUES.md, 'Two turns can run at once in one chat' — reproduced
    the same way it was originally found: two run_turn calls against the same
    chat via asyncio.gather, with the backend slowed enough that they
    genuinely overlap rather than one finishing before the other starts."""
    import app.providers.echo as echo_mod

    original_generate = echo_mod.EchoProvider.generate

    async def slow_generate(self, request):
        await asyncio.sleep(0.05)
        return await original_generate(self, request)

    monkeypatch.setattr(echo_mod.EchoProvider, "generate", slow_generate)

    async def scenario():
        return await asyncio.gather(
            drain(sched.run_turn(chat["id"], "first")),
            drain(sched.run_turn(chat["id"], "second")),
        )

    first, second = sync(scenario())
    ran = [events for events in (first, second) if events_of(events, "reply")]
    turned_away = [events for events in (first, second) if not events_of(events, "reply")]
    # Exactly one of the two actually generated a reply — the other was
    # turned away outright rather than running its own reply concurrently,
    # each oblivious to the other's prompt.
    assert len(ran) == 1
    assert turned_away == [[
        {"type": "error", "error": "a reply is already being generated in this chat"}
    ]]
    # And the turned-away one left no trace: one user message stored, not two.
    assert len(repo.list_messages(sched.db, chat["id"])) == 2


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
        before = read_slice(sched.db, chat["id"], slice_for(SLICE_VARS, character.id))
        events = [e async for e in sched.run_swipe(message["id"])]
        await sched.await_pending(chat["id"], timeout=20)
        return before, events

    before, events = sync(scenario())
    rollback = events_of(events, "rollback")
    assert rollback and rollback[0]["writes"] >= 1

    after = read_slice(sched.db, chat["id"], slice_for(SLICE_VARS, character.id))
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
            values.append(dict(read_slice(sched.db, chat["id"], slice_for(SLICE_VARS, character.id))["value"]))
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


# --------------------------------------------------------------- echoed replies
#
# The `echo` backend's own reply literally is `"You said: {user}"` — so any
# user message six words or longer is, by construction, echoed back in full,
# with nothing to monkeypatch (§ providers/echo.py, ISSUES-TRIAGE.md #15).

LONG_MESSAGE = "I still cannot believe you forgot my birthday this year."
SHORT_MESSAGE = "Cold out."


def test_a_first_reply_is_flagged_when_it_echoes_the_user(sched, chat):
    events = sync(turn(sched, chat["id"], LONG_MESSAGE))
    reply = events_of(events, "reply")[0]["message"]
    assert reply["echoes_user"]
    assert "forgot my birthday" in reply["echoes_user"]

    stored = repo.get_message(sched.db, reply["id"])
    assert stored["echoes_user"] == reply["echoes_user"]


def test_a_short_user_message_is_never_flagged(sched, chat):
    """Under find_echoed_phrase's own word floor either way, whatever the
    reply does — nothing here needs the echo backend's exact wording."""
    events = sync(turn(sched, chat["id"], SHORT_MESSAGE))
    reply = events_of(events, "reply")[0]["message"]
    assert reply["echoes_user"] == ""


def test_a_swipe_is_flagged_independently_of_the_first_reply(sched, chat):
    async def scenario():
        await turn(sched, chat["id"], LONG_MESSAGE)
        message = repo.list_messages(sched.db, chat["id"])[-1]
        events = [e async for e in sched.run_swipe(message["id"])]
        await sched.await_pending(chat["id"], timeout=20)
        return message, events

    message, events = sync(scenario())
    variant = events_of(events, "variant")[0]
    assert variant["variant"]["echoes_user"]

    variants = repo.list_variants(sched.db, message["id"])
    assert all(v["echoes_user"] for v in variants)


# ------------------------------------------------------------------ memory


def test_memory_pass_builds_input_when_enabled(sched, chat, character):
    repo.add_message(sched.db, chat["id"], "user", "a secret")
    definition = next(d for d in registry.all_passes(sched.db) if d.id == "memory")
    _prompt, messages, handler = sched._build_pass_input(
        context(chat, character, turn_no=1), definition
    )
    assert messages and handler is not None


def test_memory_pass_skips_extraction_when_disabled_for_the_character(sched, chat, character):
    """Off means off — no call to a backend at all — but coverage still has
    to move, or eviction waits forever on a memory pass that will never run
    again for this character (§app/assembly.py apply_eviction)."""
    disabled = character.model_copy(update={"memory_enabled": False})
    repo.add_message(sched.db, chat["id"], "user", "a secret")
    definition = next(d for d in registry.all_passes(sched.db) if d.id == "memory")

    assert assembly.memory_covered_turn(sched.db, chat["id"]) == assembly.MEMORY_NEVER
    _prompt, messages, handler = sched._build_pass_input(
        context(chat, disabled, turn_no=5), definition
    )
    assert messages == [] and handler is None
    assert assembly.memory_covered_turn(sched.db, chat["id"]) == 5
