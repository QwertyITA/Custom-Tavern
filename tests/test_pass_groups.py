"""The three groups of passes, as the panel presents them (§3).

"Blocking / foreground / background" says *when* a pass runs, which is not the
question someone opening the panel has. Grouped and named for what they are
for — Messages, Refiner, Secondary info generator — each group owns a backend,
a switch and its own settings, and the two that are not the reply can be
switched off or slowed down.
"""

from __future__ import annotations

from app import repo, state as state_mod
from app.config import TIER_GROUPS, Settings, build_settings
from app.passes.registry import CANONICAL_PASSES, all_passes
from app.passes.scheduler import PassScheduler

from .conftest import sync, turn


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


def test_the_refiner_group_has_passes_in_it(db):
    """It is named after work it does, so it has to do some. The auditor and
    the expression pass moved onto it — both read the reply back."""
    tiers = {p.id: p.model_tier for p in all_passes(db)}
    assert tiers["state_auditor"] == "foreground"
    assert tiers["expression"] == "foreground"
    assert tiers["basic"] == "blocking"
    assert tiers["summary"] == "background"


def test_a_pass_that_moved_tier_is_moved_in_an_existing_database(db):
    """Seeding never clobbers a stored definition, so without the regroup an
    install from before the groups were named keeps its auditor in the wrong
    one — and the panel offers a Refiner with nothing in it."""
    from app.passes import registry

    row = db.query_one("SELECT data FROM pass_defs WHERE id='state_auditor'")
    stale = row["data"].replace('"model_tier":"foreground"', '"model_tier":"background"')
    db.write_sync(lambda conn: conn.execute(
        "UPDATE pass_defs SET data=? WHERE id='state_auditor'", (stale,)
    ))

    registry.seed(db)
    tiers = {p.id: p.model_tier for p in all_passes(db)}
    assert tiers["state_auditor"] == "foreground"


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
