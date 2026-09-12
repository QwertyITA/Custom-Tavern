"""Memory quality: the write gate, and the tidy-up (roadmap 41).

Reported live: a store filling with "the user said hello to the character".
The old pass did ask for durable facts only — and a model asked for facts
produces *something*, because producing something feels like doing the job.
What it lacked was a legitimate place to put small talk, and a number it had
to stand behind. So the shape of what is tested here is: the model labels and
rates, and this code — never the prompt — decides what that earns.
"""

from __future__ import annotations

import pytest

from app import memory as memory_store
from app.memory import CHATTER, COMPRESS_AT, COMPRESS_EVERY, KINDS, MIN_IMPORTANCE


def fact(text, kind="event", importance=3, keys=None):
    item = {"text": text, "kind": kind, "importance": importance}
    if keys is not None:
        item["keys"] = keys
    return item


# ------------------------------------------------------------- the gate


def test_small_talk_is_refused_however_it_is_worded(db, character):
    """The reported symptom, and the one the categories exist for: there is
    now somewhere honest to put a greeting, and it is not the store."""
    memory_store.store(db, character.id, [
        fact("The user said hello to Mira.", kind=CHATTER, importance=1),
        fact("The user greeted Mira warmly and she nodded back.", kind=CHATTER, importance=3),
    ])
    assert memory_store.list_all(db, character.id) == []


def test_a_fact_rated_below_the_floor_is_refused(db, character):
    memory_store.store(db, character.id, [fact("Mira glanced at the door.", importance=1)])
    assert memory_store.list_all(db, character.id) == []


def test_the_floor_is_applied_here_not_asked_for_in_the_prompt(db, character):
    """A model told in prose to hold a bar will rationalise its way under it.
    One held to a threshold after it has answered cannot."""
    kept = memory_store.store(db, character.id, [fact("A", importance=MIN_IMPORTANCE)])
    refused = memory_store.store(db, character.id, [fact("B", importance=MIN_IMPORTANCE - 1)])
    assert len(kept) == 1 and refused == []


def test_an_unrated_candidate_from_the_pass_is_refused(db, character):
    """The rating is the contract. A reply that skips it skipped the
    thinking, and storing it anyway would let the whole gate be bypassed by
    returning less rather than more."""
    assert memory_store.store(db, character.id, [{"text": "Something unlabelled."}]) == []


def test_an_unknown_kind_is_refused_rather_than_guessed_at(db, character):
    assert memory_store.store(db, character.id, [fact("X", kind="vibes", importance=5)]) == []


@pytest.mark.parametrize("kind", KINDS)
def test_every_real_kind_is_storable(db, character, kind):
    assert memory_store.store(db, character.id, [fact(f"A {kind} fact.", kind=kind)])


def test_a_fact_someone_typed_by_hand_is_never_gated(db, character):
    """They have already made the judgement this gate exists to make."""
    stored = memory_store.store(
        db, character.id, [{"text": "Something I want kept."}], source="manual")
    assert len(stored) == 1


def test_what_was_stored_keeps_its_label_and_rating(db, character):
    memory_store.store(db, character.id, [fact("Mira is a blacksmith.", "identity", 4)])
    [saved] = memory_store.list_all(db, character.id)
    assert saved["kind"] == "identity"
    assert saved["importance"] == 4
    assert saved["uses"] == 0


# -------------------------------------------------------- reinforcement


def test_retrieval_records_that_a_memory_was_actually_used(db, character):
    """The only honest evidence of usefulness this system can get."""
    memory_store.store(db, character.id, [fact("Mira owns a knife.", keys=["knife"])])
    memory_store.retrieve(db, character.id, "where is the knife", turn=7)
    [saved] = memory_store.list_all(db, character.id)
    assert saved["uses"] == 1
    row = db.query_one("SELECT last_used_turn FROM memories WHERE id=?", (saved["id"],))
    assert row["last_used_turn"] == 7


def test_a_memory_that_does_not_surface_is_not_credited(db, character):
    memory_store.store(db, character.id, [fact("Mira owns a knife.", keys=["knife"])])
    memory_store.retrieve(db, character.id, "something else entirely")
    assert memory_store.list_all(db, character.id)[0]["uses"] == 0


def test_importance_breaks_a_tie_between_equally_relevant_facts(db, character):
    """Relevance still leads — importance decides among things that matched,
    which is what stops junk sharing one word from crowding out the fact
    that matters."""
    memory_store.store(db, character.id, [
        fact("The knife was mentioned once.", importance=2, keys=["knife"]),
        fact("Mira swore on the knife to avenge her brother.", importance=5, keys=["knife"]),
    ])
    hits = memory_store.retrieve(db, character.id, "the knife")
    assert "swore" in hits[0]["text"]


# ---------------------------------------------------------- when to tidy


# Deliberately unalike: near-identical filler trips store()'s own dedupe
# (Jaccard over content words), and a helper that quietly stored four rows
# when asked for forty would make every count below a lie.
_NOUNS = "knife lantern harbour ledger scar promise brother cellar rope coin".split()
_VERBS = "lost found owed buried sold burned hid mended traded broke".split()


def _fill(db, character_id, n, start=0):
    memory_store.store(
        db, character_id,
        [
            fact(
                f"{_VERBS[i % len(_VERBS)].capitalize()} the "
                f"{_NOUNS[(i // len(_VERBS)) % len(_NOUNS)]} number{i}.",
                keys=[f"k{i}"],
            )
            for i in range(start, start + n)
        ],
    )


def test_a_small_store_is_never_tidied(db, character):
    _fill(db, character.id, COMPRESS_AT - 1)
    assert not memory_store.needs_compression(db, character.id)


def test_a_big_store_that_has_not_changed_is_not_re_read(db, character):
    """Both conditions, never either: re-reading an unchanged store is paying
    to be told the same thing twice."""
    _fill(db, character.id, COMPRESS_AT + COMPRESS_EVERY)
    assert memory_store.needs_compression(db, character.id)
    memory_store.mark_compressed(db, character.id)
    assert not memory_store.needs_compression(db, character.id)


def test_it_becomes_due_again_once_enough_new_facts_arrive(db, character):
    _fill(db, character.id, COMPRESS_AT + COMPRESS_EVERY)
    memory_store.mark_compressed(db, character.id)
    _fill(db, character.id, COMPRESS_EVERY - 1, start=1000)
    assert not memory_store.needs_compression(db, character.id)
    _fill(db, character.id, 2, start=2000)
    assert memory_store.needs_compression(db, character.id)


# --------------------------------------------------------- what it does


def test_a_plan_can_drop_and_merge(db, character):
    ids = memory_store.store(db, character.id, [
        fact("Mira has a sister."),
        fact("Mira's sister is called Iris."),
        fact("Someone coughed."),
    ])
    result = memory_store.apply_compression(db, character.id, [
        {"id": ids[0], "action": "drop"},
        {"id": ids[1], "action": "merge", "text": "Mira's sister is called Iris."},
        {"id": ids[2], "action": "drop"},
    ])
    assert result == {"dropped": 2, "merged": 1, "refused": ""}
    remaining = [m["text"] for m in memory_store.list_all(db, character.id)]
    assert remaining == ["Mira's sister is called Iris."]


def test_a_memory_left_out_of_the_plan_is_kept(db, character):
    """Silence is not consent to delete. An id the model forgot to list would
    otherwise be deleted by omission — the worst failure available here."""
    ids = memory_store.store(db, character.id, [fact("A."), fact("B."), fact("C.")])
    memory_store.apply_compression(db, character.id, [{"id": ids[0], "action": "drop"}])
    assert len(memory_store.list_all(db, character.id)) == 2


def test_a_plan_that_would_empty_the_store_is_refused(db, character):
    """Far more likely a confused model than a character with nothing worth
    remembering."""
    ids = memory_store.store(db, character.id, [fact("A."), fact("B.")])
    result = memory_store.apply_compression(
        db, character.id, [{"id": i, "action": "drop"} for i in ids])
    assert result["refused"]
    assert len(memory_store.list_all(db, character.id)) == 2


def test_what_you_wrote_yourself_is_never_dropped(db, character):
    mine = memory_store.store(
        db, character.id, [{"text": "A fact I typed in."}], source="manual")
    theirs = memory_store.store(db, character.id, [fact("One the pass found.")])
    memory_store.apply_compression(db, character.id, [
        {"id": mine[0], "action": "drop"},
        {"id": theirs[0], "action": "drop"},
    ])
    kept = [m["text"] for m in memory_store.list_all(db, character.id)]
    assert kept == ["A fact I typed in."]


def test_what_you_edited_yourself_is_never_rewritten(db, character):
    """Editing marks it yours (§ memory.update): correcting a fact is the
    same judgement the tidy-up is trying to approximate, so it must not be
    overruled later."""
    ids = memory_store.store(db, character.id, [fact("Wrong."), fact("Filler.")])
    memory_store.update(db, ids[0], "Corrected by hand.")
    memory_store.apply_compression(db, character.id, [
        {"id": ids[0], "action": "merge", "text": "Something the model preferred."},
    ])
    texts = [m["text"] for m in memory_store.list_all(db, character.id)]
    assert "Corrected by hand." in texts
    assert "Something the model preferred." not in texts


def test_a_merge_with_no_wording_is_not_a_disguised_drop(db, character):
    ids = memory_store.store(db, character.id, [fact("A."), fact("B.")])
    memory_store.apply_compression(
        db, character.id, [{"id": ids[0], "action": "merge", "text": "  "}])
    assert len(memory_store.list_all(db, character.id)) == 2


def test_a_merged_wording_gets_fresh_lookup_keys(db, character):
    ids = memory_store.store(db, character.id, [fact("Mira lost a knife.", keys=["knife"]),
                                                fact("Filler.")])
    memory_store.apply_compression(db, character.id, [
        {"id": ids[0], "action": "merge", "text": "Mira lost a lantern at the harbour."},
    ])
    saved = memory_store.get(db, ids[0])
    assert "lantern" in saved["keys"] and "knife" not in saved["keys"]


def test_tidying_resets_the_clock_on_when_to_tidy_again(db, character):
    _fill(db, character.id, COMPRESS_AT + COMPRESS_EVERY)
    assert memory_store.needs_compression(db, character.id)
    ids = [m["id"] for m in memory_store.list_all(db, character.id)]
    memory_store.apply_compression(db, character.id, [{"id": ids[0], "action": "drop"}])
    assert not memory_store.needs_compression(db, character.id)


# ------------------------------------------------ the pass, end to end


def test_the_tidy_pass_never_fires_on_a_schedule(sched, chat, character):
    """A timer would pay for this on quiet characters and still be late on
    busy ones. The store's own shape is the only honest trigger."""
    from app.passes import registry

    from .test_scheduler import context

    definition = registry.get_pass(sched.db, "memory_compress")
    assert definition.trigger.type == "manual"
    assert not sched.trigger_fires(definition, context(chat, character))


def test_the_tidy_pass_is_skipped_when_there_is_nothing_to_compare(sched, chat, character):
    """One memory cannot duplicate, contradict or be merged into anything."""
    from app.passes import registry

    from .test_scheduler import context

    memory_store.store(sched.db, character.id, [fact("Only the one fact.")])
    definition = registry.get_pass(sched.db, "memory_compress")
    _task, _messages, handler = sched._build_pass_input(context(chat, character), definition)
    assert handler is None


def test_the_tidy_pass_is_shown_the_rating_and_the_use_count(sched, chat, character):
    """It is being asked to judge what to drop — the ratings and the use
    counts are the evidence, and a list of bare sentences would make it
    guess from wording alone."""
    from app.passes import registry

    from .test_scheduler import context

    memory_store.store(sched.db, character.id, [
        fact("Mira is a blacksmith.", "identity", 4, keys=["blacksmith"]),
        fact("Someone coughed once.", "event", 2, keys=["cough"]),
    ])
    memory_store.retrieve(sched.db, character.id, "blacksmith", turn=3)
    definition = registry.get_pass(sched.db, "memory_compress")
    _task, messages, _handler = sched._build_pass_input(context(chat, character), definition)
    body = " ".join(m["content"] for m in messages)
    assert "identity" in body and "importance 4" in body and "used 1x" in body


def test_a_real_tidy_up_runs_through_the_pass_machinery(sched, chat, character):
    """Through _execute like every other pass, so it lands in pass_runs and
    on the cost dashboard rather than being an invisible model call."""
    from app.providers import echo as echo_provider

    from .conftest import sync

    ids = memory_store.store(sched.db, character.id, [
        fact("Mira has a sister."),
        fact("Her sister is called Iris."),
        fact("Filler that survives."),
    ])
    monkey = [
        {"id": ids[0], "action": "drop"},
        {"id": ids[1], "action": "merge", "text": "Mira's sister is called Iris."},
    ]
    original = echo_provider._compression_plan
    echo_provider._compression_plan = lambda request: monkey
    try:
        result = sync(sched.tidy_memories_now(character.id))
    finally:
        echo_provider._compression_plan = original

    assert result == {"error": ""}
    texts = [m["text"] for m in memory_store.list_all(sched.db, character.id)]
    assert "Mira's sister is called Iris." in texts
    assert "Mira has a sister." not in texts
    row = sched.db.query_one(
        "SELECT status FROM pass_runs WHERE chat_id=? AND pass_id='memory_compress'",
        (chat["id"],),
    )
    assert row["status"] == "done"


def test_the_shipped_stand_in_never_deletes_a_store_just_by_running(sched, chat, character):
    """A default plan that dropped things would make every unrelated test
    that happens to trigger a tidy-up quietly destructive."""
    from .conftest import sync

    memory_store.store(sched.db, character.id, [fact("A."), fact("B."), fact("C.")])
    sync(sched.tidy_memories_now(character.id))
    assert len(memory_store.list_all(sched.db, character.id)) == 3


# ------------------------------------------------------------ the button


def test_the_tidy_endpoint_reports_what_it_changed(client, db, character, chat):
    from app.providers import echo as echo_provider

    ids = memory_store.store(db, character.id, [
        fact("Mira has a sister."), fact("Her sister is called Iris."), fact("Filler.")])
    original = echo_provider._compression_plan
    echo_provider._compression_plan = lambda request: [{"id": ids[0], "action": "drop"}]
    try:
        body = client.post(f"/api/characters/{character.id}/memories/tidy").json()
    finally:
        echo_provider._compression_plan = original
    assert body["ok"] is True
    assert body["before"] == 3 and body["after"] == 2
    assert len(body["memories"]) == 2


def test_the_tidy_endpoint_404s_on_an_unknown_character(client):
    assert client.post("/api/characters/nobody/memories/tidy").status_code == 404


def test_tidying_a_character_with_no_chats_says_so_rather_than_failing(client, db, character):
    """There is no chat to hang a pass run on. A plain reason beats a 500."""
    memory_store.store(db, character.id, [fact("A."), fact("B.")])
    body = client.post(f"/api/characters/{character.id}/memories/tidy").json()
    assert body["ok"] is False and body["error"]


def test_the_panel_can_see_what_each_memory_was_judged_to_be(client, db, character):
    memory_store.store(db, character.id, [fact("Mira is a blacksmith.", "identity", 4)])
    [row] = client.get(f"/api/characters/{character.id}/memories").json()
    assert row["kind"] == "identity" and row["importance"] == 4 and row["uses"] == 0
