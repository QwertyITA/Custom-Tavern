"""Which prompt sections are built, and where (§14).

The load-bearing rule here is that the three bands never move relative to each
other. Everything else — reordering, switching off, custom blocks — is allowed
precisely because it cannot reach across a band boundary and put changing text
in front of stable text (§7.1).
"""

from __future__ import annotations

import pytest

from app import assembly, prompt_layout, repo
from app.config import Settings, build_settings
from app.models import Character


def ids(layout, band=None):
    return [s["id"] for s in layout if band is None or s["band"] == band]


# ------------------------------------------------------------- normalising


def test_nothing_stored_gives_every_section_switched_on():
    layout = prompt_layout.normalise(None)
    assert ids(layout) == [s["id"] for s in prompt_layout.BUILTIN]
    assert all(s["enabled"] for s in layout)


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
