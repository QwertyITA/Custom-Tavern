"""Starred/unstarred/killed reaction lines (§models.CharacterReactions,
app/character_reactions.py): generated once, never overwritten, retried
lazily after a reply when the first attempt did not land."""

from __future__ import annotations

import json
import time
from dataclasses import replace

from app import character_reactions as cr
from app import repo
from app.config import SETTINGS
from app.models import Character, CharacterReactions

from .conftest import sync, turn


def test_missing_keys_lists_every_empty_field():
    blank = Character(id="a", name="Blank")
    assert set(cr.missing_keys(blank)) == {"starred", "unstarred", "killed"}


def test_missing_keys_empty_once_all_three_are_set():
    full = Character(
        id="a",
        name="Full",
        reactions=CharacterReactions(starred="x", unstarred="y", killed="z"),
    )
    assert cr.missing_keys(full) == []


def test_fill_missing_generates_every_empty_line(db, character):
    updated = sync(cr.fill_missing(db, SETTINGS, character))
    assert updated is not None
    assert updated.reactions.starred
    assert updated.reactions.unstarred
    assert updated.reactions.killed
    # And it is actually saved, not just returned.
    stored = repo.get_character(db, character.id)
    assert stored.reactions.starred == updated.reactions.starred


def test_fill_missing_is_a_no_op_once_everything_is_there(db, character):
    first = sync(cr.fill_missing(db, SETTINGS, character))
    again = sync(cr.fill_missing(db, SETTINGS, first))
    assert again is None


def test_fill_missing_never_overwrites_an_existing_line(db):
    card = Character(
        id="c",
        name="Partial",
        reactions=CharacterReactions(starred="Already written by hand."),
    )
    repo.save_character(db, card)
    updated = sync(cr.fill_missing(db, SETTINGS, card))
    assert updated.reactions.starred == "Already written by hand."
    # The two empty ones did get filled in.
    assert updated.reactions.unstarred
    assert updated.reactions.killed


def test_fill_missing_force_overwrites_a_line_that_is_already_there(db, character):
    filled = sync(cr.fill_missing(db, SETTINGS, character))
    original = filled.reactions.starred
    assert original

    forced = sync(cr.fill_missing(db, SETTINGS, filled, force=["starred"]))
    assert forced is not None
    # The other two are untouched — force only names starred.
    assert forced.reactions.unstarred == filled.reactions.unstarred
    assert forced.reactions.killed == filled.reactions.killed
    stored = repo.get_character(db, character.id)
    assert stored.reactions.starred == forced.reactions.starred


def test_fill_missing_does_nothing_while_reactions_are_switched_off(db, character):
    off = replace(SETTINGS, feature_character_reactions=False)
    assert cr.missing_keys(character)
    assert sync(cr.fill_missing(db, off, character)) is None
    stored = repo.get_character(db, character.id)
    assert cr.missing_keys(stored) == ["starred", "unstarred", "killed"]


def test_reaction_lines_are_five_to_eight_words(db, character):
    updated = sync(cr.fill_missing(db, SETTINGS, character))
    for key in ("starred", "unstarred", "killed"):
        line = getattr(updated.reactions, key)
        words = line.split()
        assert 5 <= len(words) <= 8, f"{key!r} is {len(words)} words: {line!r}"


def test_spawn_swallows_a_crash_rather_than_raising(db, character, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("backend is on fire")

    monkeypatch.setattr(cr, "fill_missing", boom)
    sync(cr.spawn(db, SETTINGS, character))  # must not raise


def test_run_turn_backfills_missing_reactions_after_the_reply(sched, chat, character):
    assert cr.missing_keys(character)
    sync(turn(sched, chat["id"], "Hello there."))
    stored = repo.get_character(sched.db, character.id)
    assert not cr.missing_keys(stored)


def test_run_turn_does_nothing_extra_once_reactions_are_complete(sched, chat, character, monkeypatch):
    sync(cr.fill_missing(sched.db, SETTINGS, character))
    calls = []
    original = cr.spawn

    async def watch(*a, **k):
        calls.append(1)
        return await original(*a, **k)

    monkeypatch.setattr(cr, "spawn", watch)
    sync(turn(sched, chat["id"], "Hello again."))
    assert calls == []


def test_import_route_kicks_off_generation(client, isolated_avatars):
    card = {
        "name": "Imported One",
        "description": "Keeps to themself.",
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {"name": "Imported One", "description": "Keeps to themself."},
    }
    response = client.post(
        "/api/characters/import?filename=card.json",
        content=json.dumps(card).encode("utf-8"),
    )
    assert response.status_code == 200
    character_id = response.json()["id"]

    # Fire-and-forget: give the background task a moment to land rather than
    # asserting on the response itself, which the import deliberately does
    # not wait on.
    deadline = time.time() + 3
    reactions = None
    while time.time() < deadline:
        got = client.get(f"/api/characters/{character_id}").json()
        if not any(not v for v in got["reactions"].values()):
            reactions = got["reactions"]
            break
        time.sleep(0.05)
    assert reactions is not None, "reaction generation never landed"


# ---- the reactions/regenerate route (§ "Regenerate reactions" and the
# quiet backfill after a line is cleared by hand, both in static/app.js) ----


def test_regenerate_route_fills_every_blank_by_default(client, character):
    response = client.post(f"/api/characters/{character.id}/reactions/regenerate", json={})
    assert response.status_code == 200
    reactions = response.json()["reactions"]
    assert reactions["starred"] and reactions["unstarred"] and reactions["killed"]


def test_regenerate_route_leaves_existing_lines_alone_by_default(client, db):
    card = Character(
        id="reg-partial", name="Half",
        reactions=CharacterReactions(starred="Hand-written line."),
    )
    repo.save_character(db, card)
    response = client.post(f"/api/characters/{card.id}/reactions/regenerate", json={})
    assert response.status_code == 200
    assert response.json()["reactions"]["starred"] == "Hand-written line."


def test_regenerate_route_keys_forces_exactly_those_lines(client, db, character):
    filled = sync(cr.fill_missing(db, SETTINGS, character))
    assert filled.reactions.starred

    response = client.post(
        f"/api/characters/{character.id}/reactions/regenerate",
        json={"keys": ["starred"]},
    )
    assert response.status_code == 200
    reactions = response.json()["reactions"]
    # Regenerated regardless of already having something — and the other two,
    # not named in `keys`, are left exactly as they were.
    assert reactions["starred"]
    assert reactions["unstarred"] == filled.reactions.unstarred
    assert reactions["killed"] == filled.reactions.killed


def test_regenerate_route_404s_for_an_unknown_character(client):
    response = client.post("/api/characters/nope/reactions/regenerate", json={})
    assert response.status_code == 404


def test_regenerate_route_400s_for_a_key_that_is_not_a_real_reaction(client, character):
    response = client.post(
        f"/api/characters/{character.id}/reactions/regenerate",
        json={"keys": ["not-a-real-key"]},
    )
    assert response.status_code == 400


def test_regenerate_route_400s_while_reactions_are_switched_off(client, character, monkeypatch):
    from app import config

    monkeypatch.setattr(config.SETTINGS, "feature_character_reactions", False)
    response = client.post(f"/api/characters/{character.id}/reactions/regenerate", json={})
    assert response.status_code == 400
