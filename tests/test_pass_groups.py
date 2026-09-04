"""The three groups of passes, as the panel presents them (§3).

"Blocking / foreground / background" says *when* a pass runs, which is not the
question someone opening the panel has. Grouped and named for what they are
for — Messages, Post-process, Secondary info generator — each group owns a
backend, a switch and its own settings, and the two that are not the reply
can be switched off or slowed down.
"""

from __future__ import annotations

from app import repo, state as state_mod
from app.config import TIER_GROUPS, Settings, build_settings
from app.passes.registry import CANONICAL_PASSES, all_passes
from app.passes.scheduler import PassScheduler

from .conftest import sync, turn


def _ctx(sched, chat, character, *, turn: int):
    """A turn context standing still, for asking a trigger a direct question."""
    from app.passes.scheduler import TurnContext

    return TurnContext(chat=chat, character=character, settings=sched.settings, turn=turn)


def statuses(sched) -> dict:
    return {
        r["pass_id"]: r["status"]
        for r in sched.db.query("SELECT pass_id, status FROM pass_runs")
    }


# ------------------------------------------------------------- the groups


def test_every_tier_is_a_group_with_a_name_and_a_reason():
    tiers = [g["tier"] for g in TIER_GROUPS]
    assert tiers == ["blocking", "foreground", "background"]
    for group in TIER_GROUPS:
        assert group["label"] and len(group["note"]) > 40


def test_only_the_reply_group_is_compulsory():
    required = [g["tier"] for g in TIER_GROUPS if g["required"]]
    assert required == ["blocking"]


def test_the_post_process_group_has_a_pass_in_it(db):
    """It is named after work it does, so it has to do some."""
    tiers = {p.id: p.model_tier for p in all_passes(db)}
    assert tiers["post_process"] == "foreground"
    assert tiers["basic"] == "blocking"
    assert tiers["summary"] == "background"


def test_the_state_auditor_and_expression_moved_to_background(db):
    """They used to be the whole reason the foreground group existed; now
    post_process owns it alone and both read-the-reply-back passes settled
    where the rest of the non-urgent work already lives."""
    tiers = {p.id: p.model_tier for p in all_passes(db)}
    assert tiers["state_auditor"] == "background"
    assert tiers["expression"] == "background"


def test_a_pass_that_moved_tier_is_moved_in_an_existing_database(db):
    """Seeding never clobbers a stored definition, so without the regroup an
    install from before this move keeps its auditor on the tier post_process
    now owns — and the two groups fight over the same backend."""
    from app.passes import registry

    row = db.query_one("SELECT data FROM pass_defs WHERE id='state_auditor'")
    stale = row["data"].replace('"model_tier":"background"', '"model_tier":"foreground"')
    db.write_sync(lambda conn: conn.execute(
        "UPDATE pass_defs SET data=? WHERE id='state_auditor'", (stale,)
    ))

    registry.seed(db)
    tiers = {p.id: p.model_tier for p in all_passes(db)}
    assert tiers["state_auditor"] == "background"


# ----------------------------------------------------------- switching off


def test_a_group_switched_off_runs_none_of_its_passes(db, chat, character):
    settings = Settings(tiers_off=["background"])
    sched = PassScheduler(db, settings)
    sync(turn(sched, chat["id"], "Cold out."))

    ran = statuses(sched)
    assert ran.get("basic") == "done", ran
    assert not any(p.id in ran for p in CANONICAL_PASSES if p.model_tier == "background")


def test_the_reply_still_arrives_with_both_others_off(db, chat, character):
    settings = Settings(tiers_off=["background", "foreground"])
    sched = PassScheduler(db, settings)
    events = sync(turn(sched, chat["id"], "hello"))

    assert [e for e in events if e["type"] == "reply"], events
    assert repo.list_messages(db, chat["id"])[-1]["role"] == "assistant"


def saved(**payload) -> Settings:
    """Through the real save path, which needs a backend to save against."""
    current = Settings()
    body = {"backends": [{"name": b.name, "kind": b.kind, "model": b.model}
                         for b in current.backends], **payload}
    return build_settings(body, current)


def test_the_reply_group_cannot_be_switched_off_by_editing_the_file():
    assert saved(tiers_off=["blocking", "background"]).tiers_off == ["background"]


def test_switching_a_group_off_leaves_the_slices_it_wrote_alone(db, chat, character):
    """Off means "stop paying for it", not "throw away what it already knew"."""
    on = PassScheduler(db, Settings())
    sync(turn(on, chat["id"], "Cold out."))
    before = state_mod.read_all_slices(db, chat["id"])

    off = PassScheduler(db, Settings(tiers_off=["background"]))
    sync(turn(off, chat["id"], "Still cold."))
    after = state_mod.read_all_slices(db, chat["id"])

    assert set(before) <= set(after)


# --------------------------------------------------------- how often (§5.2)


def test_a_spacing_of_one_is_every_time_its_trigger_fires(db, chat, character):
    sched = PassScheduler(db, Settings())
    sync(turn(sched, chat["id"], "Cold out."))
    assert "scene" in statuses(sched)


def test_a_spaced_pass_sits_out_the_turns_between(db, chat, character):
    """The trigger answers "is there anything to do"; this answers "is it worth
    paying for yet"."""
    sched = PassScheduler(db, Settings(pass_every={"scene": 3}))
    sync(turn(sched, chat["id"], "Cold out."))          # turn 1
    assert "scene" not in statuses(sched)

    sync(turn(sched, chat["id"], "Colder now."))        # turn 2
    assert "scene" not in statuses(sched)

    sync(turn(sched, chat["id"], "Snowing."))           # turn 3
    assert "scene" in statuses(sched)


def test_spacing_is_stored_only_when_it_is_not_the_default():
    assert saved(pass_every={"scene": 1, "summary": 4, "junk": "no"}).pass_every == {"summary": 4}


def test_an_absurd_spacing_is_clamped():
    assert saved(pass_every={"scene": 9999}).pass_every["scene"] == 50


# ------------------------------------- the summary waits for the context to fill


def test_the_summary_waits_for_the_prompt_to_fill(db, chat, character, sched):
    """It used to run every eight turns whatever the context was doing. A
    summary replaces messages permanently, so it is worth paying for when there
    is no longer room to keep them and worth nothing at all before that."""
    from app.passes import registry

    definition = registry.get_pass(db, "summary")
    assert definition.trigger.type == "over_budget"

    ctx = _ctx(sched, chat, character, turn=8)
    ctx.prompt_tokens = 3788  # an eighth of the default budget
    assert sched.trigger_fires(definition, ctx) is False

    ctx.prompt_tokens = int(sched.settings.token_budget * 0.95)
    assert sched.trigger_fires(definition, ctx) is True


def test_a_switched_off_pass_never_runs(db, chat, character, sched):
    from app.passes import registry

    definition = registry.get_pass(db, "scene")
    ctx = _ctx(sched, chat, character, turn=1)
    ctx.signals = {"scene_change": "major"}
    assert sched.eligible([definition], ctx, set())

    definition.enabled = False
    assert sched.eligible([definition], ctx, set()) == []


def test_an_unedited_shipped_prompt_is_brought_up_to_date(db):
    """Seeding never clobbers a stored definition, so an install seeded before
    a prompt was found to be wrong keeps the wrong one forever."""
    import sqlite3

    from app.models import PassDef
    from app.passes import registry

    stored = registry.get_pass(db, "summary")
    stored.prompt = registry.SUPERSEDED_PROMPTS["summary"][0]
    stored.trigger.type = "every_n"
    stored.trigger.n = 8
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE pass_defs SET data=? WHERE id='summary'", (stored.model_dump_json(),)
        )
    )

    registry.seed(db)

    fresh = registry.get_pass(db, "summary")
    assert fresh.trigger.type == "over_budget"
    assert "premise" in fresh.prompt


def test_an_unedited_shipped_trigger_is_brought_up_to_date(db):
    """Same mechanism as the prompt migration above, checked independently
    (§ SUPERSEDED_TRIGGERS, registry.py) — music_select's trigger changed
    with its prompt left untouched, so the prompt-matching check alone
    would never catch an install seeded on the old one."""
    from app.models import Trigger
    from app.passes import registry

    stored = registry.get_pass(db, "music_select")
    stored.trigger = registry.SUPERSEDED_TRIGGERS["music_select"][0]
    db.write_sync(
        lambda conn: conn.execute(
            "UPDATE pass_defs SET data=? WHERE id='music_select'", (stored.model_dump_json(),)
        )
    )

    registry.seed(db)

    fresh = registry.get_pass(db, "music_select")
    assert fresh.trigger.type == "on_text"
    assert fresh.trigger.pattern


def test_a_hand_picked_trigger_is_left_alone(db):
    """A trigger that happens to differ from every superseded shape — the
    same "unedited" test as prompts get — is trusted as a deliberate
    choice, not migrated out from under it."""
    from app.models import Trigger
    from app.passes import registry

    mine = registry.get_pass(db, "music_select")
    mine.trigger = Trigger(type="every_n", n=5)
    sync(registry.save_pass(db, mine))

    registry.seed(db)

    kept = registry.get_pass(db, "music_select")
    assert kept.trigger.type == "every_n"
    assert kept.trigger.n == 5


def test_a_prompt_someone_has_written_is_left_alone(db):
    from app.passes import registry

    mine = registry.get_pass(db, "summary")
    mine.prompt = "Summarise it in limericks."
    mine.trigger.type = "every_n"
    sync(registry.save_pass(db, mine))

    registry.seed(db)

    kept = registry.get_pass(db, "summary")
    assert kept.prompt == "Summarise it in limericks."
    assert kept.trigger.type == "every_n"


def test_the_switch_is_never_migrated(db):
    """However it came to be where it is, it is the user's."""
    from app.passes import registry

    stored = registry.get_pass(db, "summary")
    stored.prompt = registry.SUPERSEDED_PROMPTS["summary"][0]
    stored.enabled = True
    sync(registry.save_pass(db, stored))

    registry.seed(db)
    assert registry.get_pass(db, "summary").enabled is True
