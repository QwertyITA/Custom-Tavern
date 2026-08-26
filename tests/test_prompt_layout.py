"""Which prompt sections are built, and where (§14).

The load-bearing rule here is that the three bands never move relative to each
other. Everything else — reordering, switching off, custom blocks — is allowed
precisely because it cannot reach across a band boundary and put changing text
in front of stable text (§7.1).
"""

from __future__ import annotations

import json

import pytest

from app import assembly, prompt_layout, repo
from app.config import Settings, build_settings
from app.models import Character


def ids(layout, band=None):
    return [s["id"] for s in layout if band is None or s["band"] == band]


# ------------------------------------------------------------- normalising


def test_nothing_stored_gives_every_section_in_the_state_it_ships_in():
    layout = prompt_layout.normalise(None)
    assert ids(layout) == [s["id"] for s in prompt_layout.BUILTIN]
    by_id = {s["id"]: s for s in layout}
    for base in prompt_layout.BUILTIN:
        assert by_id[base["id"]]["enabled"] is bool(base.get("default_enabled", True))


def test_every_slot_ships_switched_on():
    """Only the writing library gets to ship off, and only where it is a matter
    of taste. A structural section that arrived off would look like a bug."""
    assert all(s.get("default_enabled", True) for s in prompt_layout.STRUCTURAL)


# ------------------------------------------------------------ the library


def test_the_writing_blocks_carry_their_own_text():
    assert prompt_layout.WRITING
    for section in prompt_layout.WRITING:
        assert section["text"].strip(), section["id"]
        assert section["note"].strip(), section["id"]
        # Static text belongs in the cached band — with two deliberate
        # exceptions, both leaning on the same fact: a small model follows an
        # instruction it read last far more reliably than one buried earlier,
        # so it is worth the handful of tokens it costs to recompute every
        # turn. The house style is the rule the *renderer* depends on; the
        # length target is the constraint smaller models miss most often.
        exceptions = {"craft:format", "craft:length"}
        expected = "volatile" if section["id"] in exceptions else "prefix"
        assert section["band"] == expected, section["id"]


def test_the_library_sits_at_the_end_of_the_prefix():
    """After the card, before the conversation: the rules read as rules rather
    than as part of who the character is, and they are still inside the part of
    the prompt that stays cached between turns."""
    prefix = ids(prompt_layout.normalise(None), "prefix")
    first_block = min(
        prefix.index(s["id"]) for s in prompt_layout.WRITING if s["band"] == "prefix"
    )
    assert first_block > prefix.index("examples")


def test_the_shipped_prompt_stays_affordable():
    """Every block that ships on is paid for on every single turn. It caches,
    but it still has to fit next to a character card and a conversation."""
    from app.providers.base import estimate_tokens

    on = [s for s in prompt_layout.WRITING if s.get("default_enabled", True)]
    assert estimate_tokens("".join(s["text"] for s in on)) < 2500


def test_the_reply_budget_holds_what_the_length_block_asks_for():
    """The two are coupled and used not to be: the block asks for up to two
    hundred words by default, far more with the paragraph range turned up,
    and the state suffix goes after them. Under that the reply is cut
    mid-sentence and the suffix never arrives, which looks like the state
    engine having quietly died — so the pass budget stays generous even
    though the shipped default itself is short."""
    from app.passes.registry import CANONICAL_PASSES

    reply = next(p for p in CANONICAL_PASSES if p.id == "basic")
    assert reply.sampling.max_tokens >= 900


def test_an_edited_block_is_kept_and_a_cleared_one_falls_back():
    shipped = prompt_layout.BUILTIN_BY_ID["craft:length"]["text"]
    edited = prompt_layout.normalise([{"id": "craft:length", "enabled": True, "text": "Two lines."}])
    assert {s["id"]: s for s in edited}["craft:length"]["text"] == "Two lines."

    cleared = prompt_layout.normalise([{"id": "craft:length", "enabled": True, "text": ""}])
    assert {s["id"]: s for s in cleared}["craft:length"]["text"] == shipped


def test_a_relocated_id_lands_right_before_final_not_its_old_position():
    """craft:length has moved twice: prefix -> volatile, then from right
    before craft:format to right before the STRUCTURAL final slot. A settings
    file saved before either move has it stored somewhere that no longer
    matches, and normalise() has no record of where it used to sit, so
    without the migration it would inherit that stale position instead of
    landing right after craft:format where it belongs now."""
    base = [
        {"id": "craft:sim", "enabled": True},
        {"id": "craft:banned", "enabled": True},
        {"id": "state", "enabled": True},
        {"id": "setting", "enabled": True},
        {"id": "event", "enabled": True},
        {"id": "search", "enabled": True},
        {"id": "toggles", "enabled": True},
        {"id": "craft:format", "enabled": True},
        {"id": "final", "enabled": True},
    ]

    def stored_with_length_at(i):
        out = list(base)
        out.insert(i, {"id": "craft:length", "enabled": True})
        return out

    # Fully original: still among the prefix craft:* blocks.
    original = stored_with_length_at(1)
    volatile_ids = ids(prompt_layout.normalise(original), "volatile")
    assert volatile_ids.index("craft:length") == volatile_ids.index("craft:format") + 1
    assert volatile_ids[-1] == "final"

    # Intermediate: already in volatile, but only as far as the first move
    # (right before craft:format) would have left it — needs to move once
    # more, to right after it, rather than being trusted as already-settled.
    intermediate = stored_with_length_at(base.index({"id": "craft:format", "enabled": True}))
    assert ids(prompt_layout.normalise(intermediate), "volatile") == volatile_ids

    # And it stays put once it's actually there — the migration only fires on
    # a stored position that still looks like one of the earlier ones; a file
    # that already has craft:length in its current spot (including one where
    # a person has since dragged it somewhere else on purpose) is trusted
    # as-is.
    again = prompt_layout.to_storage(prompt_layout.normalise(original))
    assert ids(prompt_layout.normalise(again), "volatile") == volatile_ids


def test_a_relocated_id_still_keeps_its_saved_overrides():
    """The migration only sets the stale *position* aside — an off switch or
    a hand-edited line saved before the move is not something to lose."""
    stale = [{"id": "craft:length", "enabled": False, "text": "Two lines, always."}]
    by_id = {s["id"]: s for s in prompt_layout.normalise(stale)}
    assert by_id["craft:length"]["enabled"] is False
    assert by_id["craft:length"]["text"] == "Two lines, always."


def test_only_the_edit_is_stored():
    """Storing the shipped text would freeze this install's copy of it, and a
    later version's rewording would never arrive."""
    untouched = prompt_layout.to_storage(prompt_layout.normalise(None))
    assert set(next(s for s in untouched if s["id"] == "craft:prose")) == {"id", "enabled"}

    edited = prompt_layout.to_storage(
        prompt_layout.normalise([{"id": "craft:prose", "enabled": True, "text": "Be plain."}])
    )
    assert next(s for s in edited if s["id"] == "craft:prose")["text"] == "Be plain."


def test_a_section_added_in_a_later_version_arrives_switched_on():
    """An old settings file simply does not mention it. Absent must not mean
    off, or an upgrade would quietly remove part of everyone's prompt."""
    partial = [{"id": "instruction", "enabled": True}, {"id": "character", "enabled": False}]
    layout = prompt_layout.normalise(partial)
    assert ids(layout) == [s["id"] for s in prompt_layout.BUILTIN]
    by_id = {s["id"]: s for s in layout}
    assert by_id["character"]["enabled"] is False
    assert by_id["summary"]["enabled"] is True


def test_stored_order_is_kept_inside_a_band():
    stored = [{"id": "scenario", "enabled": True}, {"id": "character", "enabled": True}]
    assert ids(prompt_layout.normalise(stored), "prefix")[:2] == ["scenario", "character"]


def test_a_band_cannot_be_made_to_jump():
    """The one rule. A hand-edited file that interleaves the bands is sorted
    back into prefix / middle / volatile, because a volatile section sitting
    in front of a stable one costs a cache rebuild on every single turn."""
    stored = [
        {"id": "state", "enabled": True},        # volatile, listed first
        {"id": "instruction", "enabled": True},  # prefix
        {"id": "summary", "enabled": True},      # middle
    ]
    bands = [s["band"] for s in prompt_layout.normalise(stored)]
    assert bands == sorted(bands, key=prompt_layout.BAND_IDS.index)


def test_a_fixed_section_cannot_be_switched_off_by_editing_the_file():
    stored = [{"id": "instruction", "enabled": False}, {"id": "conversation", "enabled": False}]
    by_id = {s["id"]: s for s in prompt_layout.normalise(stored)}
    assert by_id["instruction"]["enabled"] is True
    assert by_id["conversation"]["enabled"] is True


def test_an_unknown_section_is_dropped():
    layout = prompt_layout.normalise([{"id": "not_a_section", "enabled": True}])
    assert "not_a_section" not in ids(layout)


@pytest.mark.parametrize("junk", [None, {}, "nonsense", 3, [None, 4, "x"], [{"nope": 1}]])
def test_normalise_survives_junk(junk):
    assert ids(prompt_layout.normalise(junk)) == [s["id"] for s in prompt_layout.BUILTIN]


def test_a_duplicated_id_is_kept_once():
    stored = [{"id": "summary", "enabled": False}, {"id": "summary", "enabled": True}]
    layout = prompt_layout.normalise(stored)
    assert ids(layout).count("summary") == 1
    assert {s["id"]: s for s in layout}["summary"]["enabled"] is False


# ----------------------------------------------------------- custom blocks


def custom(**over):
    return {"id": prompt_layout.new_custom_id(), "band": "prefix", "label": "Rules",
            "text": "Be brief.", "enabled": True, **over}


def test_a_custom_block_survives_normalising():
    block = custom()
    layout = prompt_layout.normalise([block])
    found = next(s for s in layout if s["custom"])
    assert found["label"] == "Rules" and found["text"] == "Be brief."


def test_a_custom_block_with_a_bad_band_lands_in_the_prefix():
    block = custom(band="nowhere")
    assert next(s for s in prompt_layout.normalise([block]) if s["custom"])["band"] == "prefix"


def test_a_custom_block_gets_a_name_even_if_it_was_left_blank():
    block = custom(label="   ")
    assert next(s for s in prompt_layout.normalise([block]) if s["custom"])["label"]


def test_custom_ids_do_not_collide():
    assert len({prompt_layout.new_custom_id() for _ in range(200)}) == 200


def test_storage_keeps_the_blocks_and_drops_the_labels_of_builtins():
    stored = prompt_layout.to_storage(prompt_layout.normalise([custom()]))
    builtin = next(s for s in stored if s["id"] == "instruction")
    assert set(builtin) == {"id", "enabled"}, "labels and notes are code, not config"
    block = next(s for s in stored if prompt_layout.is_custom(s["id"]))
    assert block["text"] == "Be brief."


def test_storage_round_trips():
    layout = prompt_layout.normalise([custom(), {"id": "examples", "enabled": False}])
    again = prompt_layout.normalise(prompt_layout.to_storage(layout))
    assert ids(again) == ids(layout)
    assert [s["enabled"] for s in again] == [s["enabled"] for s in layout]


# ---------------------------------------------------------------- assembly


def build(db, chat, character, settings=None, **kw):
    return assembly.build_reply_context(
        db, chat, character, settings or Settings(), **kw
    )


def test_the_default_layout_builds_the_prompt_it_always_did(db, chat, character):
    out = build(db, chat, character)
    assert character.persona in out.system
    assert out.system.index(character.name) < out.system.index("Example dialogue") if (
        character.example_dialogue
    ) else True


def test_switching_a_section_off_removes_it(db, chat, character):
    character.example_dialogue = "User: hi\nMira: hm."
    character.scenario = "A tavern by the water."
    repo.save_character(db, character)

    on = build(db, chat, character)
    assert "A tavern by the water." in on.system

    settings = Settings(prompt_sections=[{"id": "scenario", "enabled": False}])
    off = build(db, chat, character, settings)
    assert "A tavern by the water." not in off.system
    # And only that one: the rest of the prefix is untouched.
    assert character.persona in off.system


def test_reordering_moves_the_text(db, chat, character):
    character.scenario = "SCENARIO-MARK"
    repo.save_character(db, character)

    normal = build(db, chat, character)
    assert normal.system.index(character.persona) < normal.system.index("SCENARIO-MARK")

    settings = Settings(prompt_sections=[
        {"id": "instruction", "enabled": True},
        {"id": "scenario", "enabled": True},
        {"id": "character", "enabled": True},
    ])
    swapped = build(db, chat, character, settings)
    assert swapped.system.index("SCENARIO-MARK") < swapped.system.index(character.persona)


def test_the_shipped_writing_blocks_reach_the_prompt(db, chat, character):
    """The point of shipping them: a fresh install writes the way the preset
    they came from writes, with nothing to paste in first."""
    out = build(db, chat, character)
    assert "## Prose discipline" in out.system
    assert "## How they talk" in out.system


def test_a_shipped_block_that_ships_off_stays_out(db, chat, character):
    out = build(db, chat, character)
    assert "## Combat as spectacle" not in out.system
    assert "## Adult scenes" not in out.system


def test_the_writing_blocks_come_after_the_card(db, chat, character):
    """They are instructions about how to write this character, so they read
    after the character rather than in front of them."""
    out = build(db, chat, character)
    assert out.system.index(character.persona) < out.system.index("## Prose discipline")


def test_a_shipped_block_expands_macros(db, chat, character):
    """They are written with {{user}} in them, and a prompt that says
    "{{user}}" out loud is worse than one that never mentioned them."""
    out = build(db, chat, character)
    assert "{{user}}" not in out.system


def test_an_edited_shipped_block_is_what_gets_built(db, chat, character):
    settings = Settings(prompt_sections=[
        {"id": "craft:prose", "enabled": True, "text": "Write it plainly."}
    ])
    out = build(db, chat, character, settings)
    assert "Write it plainly." in out.system
    assert "apophasis" not in out.system.lower()


def test_a_custom_prefix_block_reaches_the_prompt(db, chat, character):
    settings = Settings(prompt_sections=[custom(label="House rules", text="No dragons.")])
    out = build(db, chat, character, settings)
    assert "## House rules" in out.system and "No dragons." in out.system


def test_a_custom_block_expands_macros(db, chat, character):
    settings = Settings(prompt_sections=[custom(text="Speak as {{char}}.")])
    out = build(db, chat, character, settings)
    assert f"Speak as {character.name}." in out.system
    assert "{{char}}" not in out.system


def test_a_disabled_custom_block_is_not_built(db, chat, character):
    settings = Settings(prompt_sections=[custom(text="INVISIBLE", enabled=False)])
    assert "INVISIBLE" not in build(db, chat, character, settings).system


def test_an_empty_custom_block_adds_no_heading(db, chat, character):
    """Someone presses add and does not fill it in. That must not leave a bare
    '## New block' sitting in the prompt."""
    settings = Settings(prompt_sections=[custom(label="New block", text="   ")])
    assert "New block" not in build(db, chat, character, settings).system


def test_a_volatile_custom_block_lands_in_the_suffix(db, chat, character):
    settings = Settings(prompt_sections=[
        custom(band="volatile", label="Length", text="Under 80 words.")
    ])
    out = build(db, chat, character, settings)
    assert "Under 80 words." in out.volatile
    assert "Under 80 words." not in out.system


def test_the_volatile_block_is_still_the_last_message(db, chat, character):
    """Whatever the layout says, the cache rule holds: the part that changes
    every turn is the last thing in the prompt."""
    settings = Settings(prompt_sections=[
        custom(band="volatile", text="LAST"),
        custom(band="prefix", text="FIRST"),
        custom(band="middle", text="MIDDLE"),
    ])
    out = build(db, chat, character, settings)
    assert out.messages[-1]["content"] == out.volatile
    assert "LAST" in out.messages[-1]["content"]


def test_a_middle_block_can_be_placed_after_the_conversation(db, chat, character):
    """Dragging a block below 'the conversation' is most of the reason to have
    custom blocks in the middle band at all."""
    from tests.conftest import sync, turn
    from app.passes.scheduler import PassScheduler
    from app.config import SETTINGS

    sync(turn(PassScheduler(db, SETTINGS), chat["id"], "hello there"))

    settings = Settings(prompt_sections=[
        {"id": "conversation", "enabled": True},
        custom(band="middle", label="Reminder", text="AFTER-MARK"),
    ])
    out = build(db, chat, character, settings)
    roles = [m["content"] for m in out.messages]
    after = next(i for i, c in enumerate(roles) if "AFTER-MARK" in c)
    last_turn = max(
        i for i, m in enumerate(out.messages) if m["role"] in ("user", "assistant")
    )
    assert after > last_turn


def test_switching_off_lore_skips_the_scan(db, chat, character):
    """The scan costs time on a phone; a section that is off must not pay it."""
    settings = Settings(prompt_sections=[{"id": "lore", "enabled": False}])
    out = build(db, chat, character, settings)
    assert out.lore_hits == []
    assert "lorebook" not in out.sections


def test_the_conversation_cannot_be_switched_off(db, chat, character):
    settings = Settings(prompt_sections=[{"id": "conversation", "enabled": False}])
    layout = prompt_layout.normalise(settings.prompt_sections)
    assert {s["id"]: s for s in layout}["conversation"]["enabled"] is True


# ------------------------------------------------------------- through the API


def test_settings_validation_normalises_the_layout():
    current = Settings()
    settings = build_settings(
        {
            "backends": [{"name": "echo", "kind": "echo"}],
            "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
            "prompt_sections": [
                {"id": "state", "enabled": True},          # volatile listed first
                {"id": "instruction", "enabled": False},   # fixed, off
                {"id": "bogus", "enabled": True},
            ],
        },
        current,
    )
    layout = prompt_layout.normalise(settings.prompt_sections)
    assert "bogus" not in ids(layout)
    assert {s["id"]: s for s in layout}["instruction"]["enabled"] is True
    assert [s["band"] for s in layout] == sorted(
        (s["band"] for s in layout), key=prompt_layout.BAND_IDS.index
    )


def test_the_api_hands_out_the_full_layout(client):
    body = client.get("/api/settings").json()
    assert [b["id"] for b in body["prompt_bands"]] == list(prompt_layout.BAND_IDS)
    assert all(s["label"] and s["band"] for s in body["prompt_sections"])
    assert set(body["prompt_fixed"]) == set(prompt_layout.FIXED_IDS)
    assert all(b["note"] for b in body["prompt_bands"]), "each band says why it is where it is"


def test_saving_a_layout_keeps_it(client, isolated_settings):
    payload = {
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "prompt_sections": [
            {"id": "scenario", "enabled": True},
            {"id": "character", "enabled": True},
            {"id": "examples", "enabled": False},
            custom(band="volatile", label="Length", text="Under 80 words."),
        ],
    }
    body = client.put("/api/settings", json=payload).json()
    assert body["ok"] is True

    # The response is the same shape the panel reads, not the sparse stored one.
    sections = body["settings"]["prompt_sections"]
    assert all("band" in s and "label" in s for s in sections)
    assert ids(sections, "prefix")[:2] == ["scenario", "character"]
    assert {s["id"]: s for s in sections}["examples"]["enabled"] is False

    # And it survives a fresh read.
    again = client.get("/api/settings").json()["prompt_sections"]
    block = next(s for s in again if s["custom"])
    assert block["text"] == "Under 80 words." and block["band"] == "volatile"


# ------------------------------------------------- itemisation (§15)


def send(client, chat_id: str, text: str) -> None:
    with client.stream("POST", f"/api/chats/{chat_id}/send", json={"text": text}) as response:
        assert response.status_code == 200
        for _ in response.iter_lines():
            pass


def a_chat(client) -> tuple[str, str]:
    character_id = client.get("/api/characters").json()[0]["id"]
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    return character_id, chat_id


def last_reply(client, chat_id: str) -> dict:
    messages = client.get(f"/api/chats/{chat_id}/messages").json()
    return [m for m in messages if m["role"] == "assistant"][-1]


def test_a_reply_records_what_was_sent(client):
    _, chat_id = a_chat(client)
    send(client, chat_id, "Is the ferry running?")
    body = client.get(f"/api/messages/{last_reply(client, chat_id)['id']}/prompt").json()

    assert body["ok"] is True
    assert body["model"] and body["tokens_in"] > 0
    ids_seen = [p["id"] for p in body["parts"]]
    assert "instruction" in ids_seen and "conversation" in ids_seen


def test_the_itemisation_is_in_prompt_order(client):
    _, chat_id = a_chat(client)
    send(client, chat_id, "Hello there.")
    parts = client.get(f"/api/messages/{last_reply(client, chat_id)['id']}/prompt").json()["parts"]
    bands = [p["band"] for p in parts]
    assert bands == sorted(bands, key=prompt_layout.BAND_IDS.index)


def test_every_part_carries_a_token_count_and_a_name(client):
    _, chat_id = a_chat(client)
    send(client, chat_id, "Hello there.")
    parts = client.get(f"/api/messages/{last_reply(client, chat_id)['id']}/prompt").json()["parts"]
    assert all(p["label"] and isinstance(p["tokens"], int) for p in parts)


def test_the_conversation_carries_a_count_rather_than_a_copy(client):
    """Storing the transcript once per turn would grow the database with the
    square of the chat, and it is already on screen."""
    _, chat_id = a_chat(client)
    send(client, chat_id, "Hello there.")
    parts = client.get(f"/api/messages/{last_reply(client, chat_id)['id']}/prompt").json()["parts"]
    conversation = next(p for p in parts if p["id"] == "conversation")
    assert conversation["text"] == ""
    assert conversation["count"] >= 1
    assert conversation["tokens"] > 0


def test_the_rows_add_up_to_the_total_shown(client):
    """A breakdown that does not add up is worse than none, because it is
    believed."""
    _, chat_id = a_chat(client)
    send(client, chat_id, "Hello there.")
    body = client.get(f"/api/messages/{last_reply(client, chat_id)['id']}/prompt").json()
    assert sum(p["tokens"] for p in body["parts"]) == body["total_tokens"]
    # And that total is the same prompt the backend was charged for, give or
    # take our per-section rounding.
    assert abs(body["total_tokens"] - body["tokens_in"]) <= 0.02 * body["tokens_in"]


def test_the_record_carries_the_fitted_budget(client):
    """The context meter needs a ceiling to measure usage against — the same
    fitted budget assembly targeted this turn (§ assembly.fit_token_budget),
    not the raw configured one, and not the raw parts total."""
    _, chat_id = a_chat(client)
    send(client, chat_id, "Hello there.")
    body = client.get(f"/api/messages/{last_reply(client, chat_id)['id']}/prompt").json()
    assert isinstance(body["budget"], int) and body["budget"] > 0


def test_an_older_record_with_no_budget_still_reads(client, db):
    """Before the context meter, a prompt record on disk was a bare list of
    parts. Reading one back should not break just because there is nothing
    to draw a meter against — it should just draw no meter."""
    _, chat_id = a_chat(client)
    send(client, chat_id, "Hello there.")
    reply = last_reply(client, chat_id)
    current = repo.prompt_record(db, reply["id"])
    run_id = db.query_one(
        "SELECT id FROM pass_runs WHERE chat_id=? AND prompt IS NOT NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (chat_id,),
    )["id"]
    db.write_sync(lambda conn: conn.execute(
        "UPDATE pass_runs SET prompt=? WHERE id=?",
        (json.dumps(current["parts"]), run_id),
    ))
    record = repo.prompt_record(db, reply["id"])
    assert record["budget"] is None
    assert record["parts"] == current["parts"]


def test_the_state_contract_is_accounted_for(client):
    """The scheduler bolts it onto the system prompt, so it is the one part
    the assembler never sees — and the one that would silently unbalance the
    breakdown."""
    _, chat_id = a_chat(client)
    send(client, chat_id, "Hello there.")
    parts = client.get(f"/api/messages/{last_reply(client, chat_id)['id']}/prompt").json()["parts"]
    contract = next(p for p in parts if p["id"] == "state_contract")
    assert contract["tokens"] > 0
    # At the end of the prefix, which is where it actually sits in the prompt.
    assert [p["band"] for p in parts].index("middle") > parts.index(contract)


def test_a_switched_off_section_is_absent_from_the_itemisation(client, isolated_settings):
    client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "prompt_sections": [{"id": "examples", "enabled": False}],
    })
    _, chat_id = a_chat(client)
    send(client, chat_id, "Hello there.")
    parts = client.get(f"/api/messages/{last_reply(client, chat_id)['id']}/prompt").json()["parts"]
    assert "examples" not in [p["id"] for p in parts]


def test_a_custom_block_shows_up_by_its_own_name(client, isolated_settings):
    client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "prompt_sections": [custom(band="volatile", label="Length", text="Under 80 words.")],
    })
    _, chat_id = a_chat(client)
    send(client, chat_id, "Hello there.")
    parts = client.get(f"/api/messages/{last_reply(client, chat_id)['id']}/prompt").json()["parts"]
    block = next(p for p in parts if p["custom"])
    assert block["label"] == "Length" and "Under 80 words." in block["text"]


def test_a_reroll_records_its_own_prompt(client):
    """A re-roll is assembled after whatever the last attempt changed, so
    showing the first attempt's breakdown next to the third's text would be
    worse than showing nothing (§9)."""
    _, chat_id = a_chat(client)
    send(client, chat_id, "Tell me about the wreck.")
    message = last_reply(client, chat_id)

    with client.stream("POST", f"/api/messages/{message['id']}/swipe", json={}) as response:
        assert response.status_code == 200
        for _ in response.iter_lines():
            pass

    after = client.get(f"/api/chats/{chat_id}/messages").json()
    rerolled = next(m for m in after if m["id"] == message["id"])
    assert rerolled["variant_count"] > 1

    body = client.get(f"/api/messages/{message['id']}/prompt").json()
    assert body["ok"] is True, "the variant on screen has a record of its own"


def test_an_old_turn_says_its_prompt_was_not_kept(client, monkeypatch):
    from app import repo as repo_mod

    monkeypatch.setattr(repo_mod, "PROMPT_HISTORY_TURNS", 1)
    _, chat_id = a_chat(client)
    send(client, chat_id, "One.")
    first = last_reply(client, chat_id)
    for text in ("Two.", "Three.", "Four."):
        send(client, chat_id, text)

    body = client.get(f"/api/messages/{first['id']}/prompt").json()
    assert body["ok"] is False
    assert str(body["kept_turns"]) in body["reason"]
    assert "too far back" in body["reason"]


def test_recent_turns_keep_theirs_while_old_ones_are_pruned(client, monkeypatch):
    from app import repo as repo_mod

    monkeypatch.setattr(repo_mod, "PROMPT_HISTORY_TURNS", 1)
    _, chat_id = a_chat(client)
    for text in ("One.", "Two.", "Three."):
        send(client, chat_id, text)
    assert client.get(f"/api/messages/{last_reply(client, chat_id)['id']}/prompt").json()["ok"]


def test_asking_about_a_message_that_does_not_exist_is_a_404(client):
    assert client.get("/api/messages/nope/prompt").status_code == 404


def test_the_greeting_has_no_prompt_because_none_was_sent(client):
    """It came off the card. Saying 'not kept' would be a lie, but so would
    inventing a breakdown for a generation that never happened."""
    _, chat_id = a_chat(client)
    greeting = last_reply(client, chat_id)
    assert client.get(f"/api/messages/{greeting['id']}/prompt").json()["ok"] is False
