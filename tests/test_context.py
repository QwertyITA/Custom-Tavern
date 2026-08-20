"""Memory, lorebook, prompt assembly and the eviction ladder (§7)."""

from __future__ import annotations

from app import assembly, lorebook, memory as memory_store, repo
from app.config import Settings
from app.models import LorebookEntry
from app.state import SLICE_SCENE, load_schema
from app.state import write_slice

from .conftest import sync


# --------------------------------------------------------------- lorebook


def entry(**kwargs) -> LorebookEntry:
    return LorebookEntry(**{"keys": ["k"], "content": "c", **kwargs})


def test_constant_entries_always_inject():
    kept = lorebook.scan([entry(keys=["never-said"], constant=True, content="always")], ["hello"])
    assert [e.content for e in kept] == ["always"]


def test_triggered_entry_needs_its_key():
    entries = [entry(keys=["Harrow"], content="harbourmaster")]
    assert lorebook.scan(entries, ["who is Harrow?"])
    assert not lorebook.scan(entries, ["nobody mentioned him"])


def test_matching_is_word_bounded():
    """'art' must not fire on 'start' — the failure that makes lorebooks feel random."""
    entries = [entry(keys=["art"], content="x")]
    assert not lorebook.scan(entries, ["let us start"])
    assert lorebook.scan(entries, ["the art of it"])


def test_scan_depth_limits_how_far_back_keys_are_looked_for():
    entries = [entry(keys=["Harrow"])]
    history = ["Harrow was here"] + ["filler"] * 5
    assert not lorebook.scan(entries, history, scan_depth=2)


def test_token_budget_drops_the_overflow():
    entries = [entry(keys=["k"], content="word " * 200) for _ in range(5)]
    kept = lorebook.scan(entries, ["k"], total_budget=300)
    assert 0 < len(kept) < 5


def test_constants_win_the_budget_over_keyword_hits():
    entries = [
        entry(keys=["k"], content="triggered " * 100),
        entry(keys=["x"], constant=True, content="constant text"),
    ]
    kept = lorebook.scan(entries, ["k"], total_budget=60)
    assert [e.content for e in kept] == ["constant text"]


def test_disabled_entries_are_skipped():
    assert not lorebook.scan([entry(constant=True, enabled=False)], ["k"])


# ----------------------------------------------------------------- memory


def test_store_dedupes_restated_facts(db, character):
    memory_store.store(db, character.id, [{"text": "The user's sister is Anna.", "keys": ["sister"]}])
    memory_store.store(db, character.id, [{"text": "the user's sister is anna"}])
    assert len(memory_store.list_all(db, character.id)) == 1


def test_store_keeps_genuinely_different_facts(db, character):
    memory_store.store(
        db,
        character.id,
        [
            {"text": "The user's sister is Anna."},
            {"text": "Mira promised to return the knife."},
        ],
    )
    assert len(memory_store.list_all(db, character.id)) == 2


def test_retrieval_is_keyword_first(db, character):
    memory_store.store(db, character.id, [{"text": "Mira promised to return the knife.",
                                           "keys": ["knife", "promise"]}])
    memory_store.store(db, character.id, [{"text": "The harbour freezes in winter.",
                                           "keys": ["harbour", "winter"]}])
    hits = memory_store.retrieve(db, character.id, "what about that knife?")
    assert hits and "knife" in hits[0]["text"]


def test_retrieval_returns_nothing_when_no_keys_match(db, character):
    memory_store.store(db, character.id, [{"text": "Something unrelated.", "keys": ["zzz"]}])
    assert memory_store.retrieve(db, character.id, "completely different subject") == []


def test_memories_are_scoped_per_character(db, character):
    memory_store.store(db, character.id, [{"text": "A fact about knives.", "keys": ["knives"]}])
    assert memory_store.retrieve(db, "someone-else", "knives") == []


def test_derive_keys_falls_back_to_content_words():
    keys = memory_store.derive_keys("The harbourmaster owes Mira money", None)
    assert "harbourmaster" in keys
    assert "the" not in keys


def test_empty_items_are_ignored(db, character):
    assert memory_store.store(db, character.id, [{"text": "  "}, {}]) == []


# --------------------------------------------------------------- assembly


def build(db, chat, character, **kwargs):
    return assembly.build_reply_context(db, chat, character, Settings(), **kwargs)


def test_volatile_state_is_the_last_message(db, chat, character):
    """The cache rule (§7.1): volatile content goes last or the prefix cache dies."""
    repo.add_message(db, chat["id"], "user", "hello")
    assembled = build(db, chat, character)
    last = assembled.messages[-1]
    assert last["role"] == "system"
    assert "current state" in last["content"]
    assert assembled.volatile == last["content"]


def test_persona_lives_in_the_stable_prefix(db, chat, character):
    assembled = build(db, chat, character)
    assert character.persona in assembled.system
    assert "The tavern is the Long Wait." in assembled.system  # constant lore
    # A triggered entry must not be in the prefix, or the prefix stops being stable.
    assert "harbourmaster" not in assembled.system.lower()


def test_triggered_lore_lands_in_the_dynamic_middle(db, chat, character):
    repo.add_message(db, chat["id"], "user", "tell me about Harrow")
    assembled = build(db, chat, character)
    middle = assembled.messages[0]["content"]
    assert "Harrow is the harbourmaster." in middle
    assert assembled.lore_hits == ["Harrow"]


def test_toggle_injections_ride_in_the_volatile_suffix(db, chat, character):
    assembled = build(db, chat, character, toggle_injections=["BE DIFFICULT"])
    assert "BE DIFFICULT" in assembled.messages[-1]["content"]


def test_scene_slice_reaches_the_prompt(db, chat, character):
    sync(write_slice(db, chat["id"], SLICE_SCENE,
                     {"place": "the back bar", "weather": "rain", "time": "dusk"}, source_turn=1))
    assembled = build(db, chat, character)
    # Cut down on the way out as well as on the way in (§10), so a chat that
    # predates the shortening reads short immediately.
    assert "Back bar · Rainy · Dusk" in assembled.messages[-1]["content"]


def test_the_default_budget_is_a_whole_local_model_window():
    """4096 was the on-device number and it was applied to every backend, so a
    PC model with a 32k window was fed an eighth of it and the far end of a
    long chat was dropped for no reason. Ollama sizes a local model from VRAM
    and lands on 32768 on an ordinary card; that is the number to default to."""
    assert Settings().token_budget == 32768


def test_the_example_settings_file_agrees_with_the_defaults():
    """It is the file people copy to settings.json. A stale number in it is a
    default nobody chose, applied to every fresh install."""
    import json
    from pathlib import Path as _Path

    from app.config import REPO_ROOT

    example = json.loads((_Path(REPO_ROOT) / "data" / "settings.example.json").read_text())
    defaults = Settings()
    for key in ("token_budget", "verbatim_window", "summary_budget",
                "lorebook_scan_depth", "lorebook_total_budget", "memory_max_injected"):
        assert example[key] == getattr(defaults, key), key


def bare_layout() -> list[dict]:
    """Every section on except the shipped writing blocks (§14).

    They are around 1700 tokens of prefix, which is nothing against the default
    32k budget and everything against the deliberately tiny ones these trimming
    tests use — with them on, the budget is spent before the first message.
    """
    from app import prompt_layout

    return [
        {"id": s["id"], "enabled": not s.get("shipped")}
        for s in prompt_layout.normalise(None)
    ]


def test_token_budget_trims_the_oldest_messages_first(db, chat, character):
    """Oldest first — but never the opening, which is pinned and is the one
    message whose absence leaves a character not knowing where they are."""
    for i in range(30):
        repo.add_message(db, chat["id"], "user", f"message number {i} " + "padding " * 40)
    tight = Settings(token_budget=900, verbatim_window=30, prompt_sections=bare_layout())
    assembled = assembly.build_reply_context(db, chat, character, tight)
    assert assembled.trimmed > 0
    kept = [m["content"] for m in assembled.messages if m["role"] == "user"]
    assert any("message number 0 " in c for c in kept), "the opening is pinned"
    assert not any("message number 1 " in c for c in kept), "the next oldest goes first"
    assert any("message number 29 " in c for c in kept)


def test_budget_never_cuts_the_prefix_or_the_suffix(db, chat, character):
    for i in range(30):
        repo.add_message(db, chat["id"], "user", "padding " * 60)
    assembled = assembly.build_reply_context(
        db, chat, character, Settings(token_budget=400, verbatim_window=30)
    )
    assert character.persona in assembled.system
    assert assembled.messages[-1]["content"] == assembled.volatile


def test_the_window_is_a_floor_and_the_budget_is_the_cap(db, chat, character):
    """It used to be a hard count, so a chat holding an eighth of its context
    budget still sent only its last few messages. With room to spare the story
    is sent whole; the setting is what survives when there is no room."""
    for i in range(20):
        repo.add_message(db, chat["id"], "user", f"m{i}")

    roomy = assembly.build_reply_context(
        db, chat, character, Settings(verbatim_window=5, token_budget=100000)
    )
    assert len([m for m in roomy.messages if m["role"] in ("user", "assistant")]) == 20


def test_a_tight_budget_still_sends_the_recent_messages(db, chat, character):
    """The floor is what survives when the budget has nothing left to give."""
    body = "word " * 200  # ~250 tokens each
    for i in range(20):
        repo.add_message(db, chat["id"], "user", f"m{i} {body}")
    tight = assembly.build_reply_context(
        db, chat, character,
        Settings(verbatim_window=3, token_budget=3000, prompt_sections=bare_layout()),
    )
    kept = [m for m in tight.messages if m["role"] in ("user", "assistant")]
    assert 3 <= len(kept) < 20
    assert kept[-1]["content"].startswith("m19")


def test_a_long_chat_keeps_its_opening(db, chat, character):
    """The first message is the scenario. Under the old count-based window it
    was the first thing to go, whatever the budget."""
    body = "word " * 200
    repo.add_message(db, chat["id"], "assistant", "the-scenario", turn=0)
    for i in range(40):
        repo.add_message(db, chat["id"], "user", f"m{i} {body}")
    assembled = assembly.build_reply_context(
        db, chat, character,
        Settings(verbatim_window=3, token_budget=3000, prompt_sections=bare_layout()),
    )
    body = "".join(m["content"] for m in assembled.messages)
    assert "the-scenario" in body
    assert "m39" in body and "m5" not in body


# --------------------------------------------------------- eviction ladder

# Eviction is permanent — a summarized message is out of the prompt for good —
# so every one of these has to say how full the prompt was. `squeezed` is a
# prompt at its budget; the default of nothing at all evicts nothing.

SQUEEZED = {"prompt_tokens": 10**6}


def test_nothing_is_evicted_before_the_summary_covers_it(db, chat, character):
    for i in range(10):
        repo.add_message(db, chat["id"], "user", f"m{i}")
    result = assembly.apply_eviction(
        db, chat["id"], Settings(verbatim_window=2), **SQUEEZED
    )
    assert result == {"summarized": 0, "dropped": 0}


def test_nothing_is_evicted_while_the_prompt_still_fits(db, chat, character):
    """The bug this exists to stop: a chat at an eighth of its context budget
    losing its opening to make room nobody needed. Eviction answers pressure."""
    for i in range(10):
        repo.add_message(db, chat["id"], "user", f"m{i}")
    repo.set_summary(db, chat["id"], "a summary", covered_turn=10)
    assembly.set_memory_covered_turn(db, chat["id"], 10)
    settings = Settings(verbatim_window=2, token_budget=32768)

    assert assembly.apply_eviction(db, chat["id"], settings, prompt_tokens=3788) == {
        "summarized": 0,
        "dropped": 0,
    }
    assert assembly.apply_eviction(db, chat["id"], settings, prompt_tokens=31000)[
        "summarized"
    ]


def test_covered_messages_outside_the_window_become_summarized(db, chat, character):
    for i in range(10):
        repo.add_message(db, chat["id"], "user", f"m{i}")
    repo.set_summary(db, chat["id"], "a summary", covered_turn=10)
    result = assembly.apply_eviction(
        db, chat["id"], Settings(verbatim_window=2), **SQUEEZED
    )
    assert result["summarized"] == 7  # the opening is pinned, so not that one
    assert result["dropped"] == 0  # memory has not looked at them yet


def test_dropping_waits_for_the_memory_pass(db, chat, character):
    """A message is only dropped once its durable facts had a chance to be promoted."""
    for i in range(10):
        repo.add_message(db, chat["id"], "user", f"m{i}")
    repo.set_summary(db, chat["id"], "a summary", covered_turn=10)
    settings = Settings(verbatim_window=2)
    assembly.apply_eviction(db, chat["id"], settings, **SQUEEZED)

    assembly.set_memory_covered_turn(db, chat["id"], 10)
    result = assembly.apply_eviction(db, chat["id"], settings, **SQUEEZED)
    assert result["dropped"] == 7
    # the window, plus the opening that is never evicted
    assert len(repo.list_messages(db, chat["id"], include_dropped=False)) == 3


def test_a_memory_pass_that_never_ran_does_not_count_as_covering_turn_zero(
    db, chat, character
):
    """"Never run" and "covered turn 0" were the same stored number, and turn 0
    is the opening message every time — so the one message carrying the premise
    was droppable on a chat where the memory pass had never run once."""
    repo.add_message(db, chat["id"], "assistant", "the scenario", turn=0)
    for i in range(10):
        repo.add_message(db, chat["id"], "user", f"m{i}")
    repo.set_summary(db, chat["id"], "a summary", covered_turn=10)
    settings = Settings(verbatim_window=2)

    assert assembly.memory_covered_turn(db, chat["id"]) == assembly.MEMORY_NEVER
    assembly.apply_eviction(db, chat["id"], settings, **SQUEEZED)
    result = assembly.apply_eviction(db, chat["id"], settings, **SQUEEZED)
    assert result["dropped"] == 0


def test_the_opening_message_is_never_evicted(db, chat, character):
    """It is the scenario — where everyone is, what the arrangement is, who
    these people are to each other — and nothing later in the chat restates any
    of it. Being turn 0 it was also always the first thing out of the door."""
    opening = repo.add_message(db, chat["id"], "assistant", "the-scenario", turn=0)
    for i in range(20):
        repo.add_message(db, chat["id"], "user", f"m{i}")
    repo.set_summary(db, chat["id"], "a summary", covered_turn=20)
    assembly.set_memory_covered_turn(db, chat["id"], 20)
    settings = Settings(verbatim_window=2)

    assembly.apply_eviction(db, chat["id"], settings, **SQUEEZED)
    assembly.apply_eviction(db, chat["id"], settings, **SQUEEZED)

    stages = {m["id"]: m["stage"] for m in repo.list_messages(db, chat["id"])}
    assert stages[opening["id"]] == "verbatim"
    assembled = assembly.build_reply_context(db, chat, character, settings)
    assert "the-scenario" in "".join(m["content"] for m in assembled.messages)


def test_dropped_messages_leave_the_prompt(db, chat, character):
    for i in range(10):
        repo.add_message(db, chat["id"], "user", f"unique-marker-{i}")
    repo.set_summary(db, chat["id"], "a summary", covered_turn=10)
    assembly.set_memory_covered_turn(db, chat["id"], 10)
    settings = Settings(verbatim_window=2)
    assembly.apply_eviction(db, chat["id"], settings, **SQUEEZED)
    assembly.apply_eviction(db, chat["id"], settings, **SQUEEZED)

    assembled = assembly.build_reply_context(db, chat, character, settings)
    body = "".join(m["content"] for m in assembled.messages)
    assert "unique-marker-1" not in body
    assert "unique-marker-9" in body


def test_pending_summary_only_covers_new_turns(db, chat, character):
    repo.add_message(db, chat["id"], "user", "old news")
    repo.set_summary(db, chat["id"], "summary", covered_turn=1)
    repo.add_message(db, chat["id"], "user", "fresh news")
    text, covered = assembly.pending_summary_text(db, chat["id"], character.name)
    assert "fresh news" in text and "old news" not in text
    assert covered == 2


# ------------------------------------------------- the summary ships off (§7.2)


def test_the_summary_pass_ships_switched_off(db):
    """It is not free context: a message the summary covers leaves the prompt
    for good, so the summary becomes the *only* account of it — written by the
    cheapest model in the stack, eight turns at a time. Off until asked for."""
    from app.passes import registry

    by_id = {p.id: p for p in registry.all_passes(db)}
    assert by_id["summary"].enabled is False
    assert by_id["memory"].enabled is True, "durable facts still get carried forward"


def test_a_pass_that_ships_off_is_seeded_off(db):
    """The enabled column is what the scheduler reads. It was seeded as a
    hardcoded 1, so a canonical pass could not ship switched off at all."""
    from app.passes import registry

    rows = {
        row["id"]: row["enabled"]
        for row in db.query("SELECT id, enabled FROM pass_defs")
    }
    assert rows["summary"] == 0
    assert rows["basic"] == 1


def test_nothing_is_evicted_while_the_summary_is_off(db, chat, character):
    """The eviction ladder starts at "the summary has covered this turn", so
    with no summary running a chat keeps every word it actually said."""
    for i in range(30):
        repo.add_message(db, chat["id"], "user", f"m{i}")
    result = assembly.apply_eviction(db, chat["id"], Settings(verbatim_window=4))
    assert result == {"summarized": 0, "dropped": 0}
