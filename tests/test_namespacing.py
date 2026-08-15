"""Per-character state namespacing (§15) — the prerequisite for group chats.

Trust and mood are held *by someone*; the weather is not. Without this split,
two characters in one room would share a single opinion of you and overwrite
each other turn by turn, which is the failure the whole feature exists to
prevent.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app import assembly, repo, state as state_mod
from app.config import Settings
from app.db import Database
from app.models import Character
from app.state import (
    PER_CHARACTER_SLICES,
    SHARED_SLICES,
    SLICE_EXPRESSION,
    SLICE_SCENE,
    SLICE_SIGNALS,
    SLICE_VARS,
    read_slice,
    slice_for,
    split_slice,
)
from tests.conftest import sync


# --------------------------------------------------------------- the split


def test_the_split_is_by_who_holds_the_thing():
    """A character holds an opinion of you; nobody holds the weather."""
    assert SLICE_VARS in PER_CHARACTER_SLICES
    assert SLICE_EXPRESSION in PER_CHARACTER_SLICES
    assert SLICE_SIGNALS in PER_CHARACTER_SLICES
    assert SLICE_SCENE in SHARED_SLICES
    assert not PER_CHARACTER_SLICES & SHARED_SLICES


def test_a_per_character_slice_is_namespaced():
    assert slice_for(SLICE_VARS, "abc123") == "state.vars:abc123"


def test_a_shared_slice_is_not():
    assert slice_for(SLICE_SCENE, "abc123") == SLICE_SCENE


def test_no_character_means_no_namespace():
    """Passes may write any slice name, and one with no owner is still valid."""
    assert slice_for(SLICE_VARS, "") == SLICE_VARS


def test_an_unknown_slice_name_is_left_alone():
    assert slice_for("something.custom", "abc") == "something.custom"


@pytest.mark.parametrize("character_id", ["abc123", "0", "a" * 32])
def test_the_split_round_trips(character_id):
    stored = slice_for(SLICE_VARS, character_id)
    assert split_slice(stored) == (SLICE_VARS, character_id)


def test_splitting_an_unnamespaced_name_gives_no_owner():
    assert split_slice(SLICE_VARS) == (SLICE_VARS, "")
    assert split_slice(SLICE_SCENE) == (SLICE_SCENE, "")


def test_splitting_leaves_an_unrelated_colon_alone():
    """A pass may write any name it likes, including one with a colon in it."""
    assert split_slice("my.pass:thing") == ("my.pass:thing", "")


# ----------------------------------------------------------------- in use


def test_two_characters_hold_separate_variables(db, chat, character):
    """The whole point. Before this they shared one row and clobbered it."""
    other = Character(id="other", name="Harrow", persona="Blunt.")
    repo.save_character(db, other)

    sync(state_mod.write_slice(
        db, chat["id"], slice_for(SLICE_VARS, character.id),
        {"trust": 9}, source_turn=1, source_pass="test",
    ))
    sync(state_mod.write_slice(
        db, chat["id"], slice_for(SLICE_VARS, other.id),
        {"trust": 1}, source_turn=1, source_pass="test",
    ))

    schema = state_mod.load_schema(None)
    assert assembly.current_values(db, chat["id"], schema, character.id)["trust"] == 9
    assert assembly.current_values(db, chat["id"], schema, other.id)["trust"] == 1


def test_the_scene_is_shared(db, chat, character):
    """Two people in one room are in the same weather."""
    sync(state_mod.write_slice(
        db, chat["id"], slice_for(SLICE_SCENE, character.id),
        {"place": "the pier"}, source_turn=1, source_pass="test",
    ))
    assert read_slice(db, chat["id"], SLICE_SCENE)["value"]["place"] == "the pier"


def test_a_turn_writes_under_the_namespaced_name(sched, chat, character):
    from tests.conftest import turn

    sync(turn(sched, chat["id"], "Cold out."))
    slices = state_mod.read_all_slices(sched.db, chat["id"])
    assert slice_for(SLICE_VARS, character.id) in slices
    assert SLICE_VARS not in slices, "nothing writes the bare name any more"


def test_stale_write_rejection_still_works_per_character(db, chat, character):
    """Arbitration is per slice by source turn (§5.5), and a namespaced slice
    is still one slice."""
    name = slice_for(SLICE_VARS, character.id)
    sync(state_mod.write_slice(
        db, chat["id"], name, {"trust": 5}, source_turn=5, source_pass="new",
    ))
    sync(state_mod.write_slice(
        db, chat["id"], name, {"trust": 1}, source_turn=2, source_pass="old",
    ))
    assert read_slice(db, chat["id"], name)["value"]["trust"] == 5


def test_one_character_s_write_does_not_lose_to_another_s_turn(db, chat, character):
    """The failure this exists to prevent: before namespacing, Harrow writing
    on turn 4 would beat Mira's turn-3 write and replace her state with his."""
    other = Character(id="other", name="Harrow", persona="Blunt.")
    repo.save_character(db, other)

    sync(state_mod.write_slice(
        db, chat["id"], slice_for(SLICE_VARS, character.id),
        {"trust": 8}, source_turn=3, source_pass="reply",
    ))
    sync(state_mod.write_slice(
        db, chat["id"], slice_for(SLICE_VARS, other.id),
        {"trust": 2}, source_turn=4, source_pass="reply",
    ))
    assert read_slice(db, chat["id"], slice_for(SLICE_VARS, character.id))["value"]["trust"] == 8


# --------------------------------------------------------------- migration


def _legacy_database(tmp_path) -> tuple[Database, str, str]:
    """A database carrying pre-namespacing rows, built by hand.

    Written through a raw connection rather than through the repo, because the
    repo no longer produces this shape — which is the point.
    """
    path = tmp_path / "legacy.db"
    db = Database(path)
    character = Character(id="mira", name="Mira", persona="Dry.")
    repo.save_character(db, character)
    chat = repo.create_chat(db, character.id, "old chat")

    def _write(conn: sqlite3.Connection) -> None:
        for name, value in (
            (SLICE_VARS, {"trust": 7}),
            (SLICE_EXPRESSION, {"expression": "wry"}),
            (SLICE_SCENE, {"place": "the pier"}),
        ):
            conn.execute(
                "INSERT INTO state_slices(chat_id, slice_name, value, source_turn, "
                "source_pass, provisional, updated_at) VALUES(?,?,?,?,?,?,?)",
                (chat["id"], name, json.dumps(value), 1, "legacy", 0, 0.0),
            )
        conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")

    db.write_sync(_write)
    return db, chat["id"], character.id


def test_the_migration_renames_existing_rows(tmp_path):
    db, chat_id, character_id = _legacy_database(tmp_path)
    db.migrate()  # as it would run on the next launch

    slices = state_mod.read_all_slices(db, chat_id)
    assert slice_for(SLICE_VARS, character_id) in slices
    assert SLICE_VARS not in slices
    assert slices[slice_for(SLICE_VARS, character_id)]["value"]["trust"] == 7
    db.close()


def test_the_migration_leaves_shared_slices_alone(tmp_path):
    db, chat_id, _ = _legacy_database(tmp_path)
    db.migrate()

    slices = state_mod.read_all_slices(db, chat_id)
    assert SLICE_SCENE in slices
    assert slices[SLICE_SCENE]["value"]["place"] == "the pier"
    db.close()


def test_the_migration_moves_the_write_log_too(tmp_path):
    """It is the audit trail and the swipe rollback source (§9); leaving it
    under the old name would make a rolled-back swipe find nothing."""
    db, chat_id, character_id = _legacy_database(tmp_path)

    def _write(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO state_writes(chat_id, slice_name, value, source_turn, "
            "source_pass, created_at) VALUES(?,?,?,?,?,?)",
            (chat_id, SLICE_VARS, json.dumps({"trust": 7}), 1, "legacy", 0.0),
        )
        conn.execute("UPDATE meta SET value='6' WHERE key='schema_version'")

    db.write_sync(_write)
    db.migrate()

    names = {r["slice_name"] for r in db.query("SELECT slice_name FROM state_writes")}
    assert slice_for(SLICE_VARS, character_id) in names
    assert SLICE_VARS not in names
    db.close()


def test_the_migration_runs_once(tmp_path):
    """A second pass must not produce state.vars:mira:mira."""
    db, chat_id, character_id = _legacy_database(tmp_path)
    db.migrate()
    db.migrate()

    slices = state_mod.read_all_slices(db, chat_id)
    assert slice_for(SLICE_VARS, character_id) in slices
    assert not any(name.count(":") > 1 for name in slices)
    db.close()


def test_the_old_values_are_still_readable_through_the_normal_path(tmp_path):
    """The test that matters to a person: their character still remembers what
    she thought of them."""
    db, chat_id, character_id = _legacy_database(tmp_path)
    db.migrate()

    chat = repo.get_chat(db, chat_id)
    character = repo.get_character(db, character_id)
    out = assembly.build_reply_context(db, chat, character, Settings())
    assert "trust" in str(out.volatile).lower() or out.volatile
    schema = state_mod.load_schema(None)
    assert assembly.current_values(db, chat_id, schema, character_id)["trust"] == 7
    db.close()
