"""A face beside every reply (§12).

Reported: no way to give a character a picture at all, and the ones that had
none showed nothing — a transcript of alternating boxes rather than of people
talking. The editor uploads one now, through the same endpoint the persona
avatars use, and every reply reserves the space whether or not there is a
picture to put in it.
"""

from __future__ import annotations

import pytest

from app import repo


def test_a_picture_can_be_set_from_the_editor(client, db, character):
    body = client.put(
        f"/api/characters/{character.id}",
        json={"name": character.name, "pfp_set": {"neutral": "/avatars/mira.png"}},
    ).json()
    assert body["pfp_set"]["neutral"] == "/avatars/mira.png"
    assert repo.get_character(db, character.id).pfp_set["neutral"] == "/avatars/mira.png"


def test_a_card_that_shipped_its_own_file_still_works(client, db, character):
    body = client.put(
        f"/api/characters/{character.id}", json={"pfp_set": {"neutral": "mira.png"}}
    ).json()
    assert body["pfp_set"] == {"neutral": "mira.png"}


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)",
    "../../etc/passwd",
    "https://elsewhere.example/pic.png",
    "/etc/passwd",
    "//evil.example/pic.png",
])
def test_a_picture_that_is_not_one_of_ours_is_refused(client, db, character, bad):
    """The value is written straight into an `src`."""
    body = client.put(f"/api/characters/{character.id}", json={"pfp_set": {"neutral": bad}}).json()
    assert body["pfp_set"] == {}


def test_editing_the_text_leaves_the_picture_alone(client, db, character):
    client.put(f"/api/characters/{character.id}", json={"pfp_set": {"neutral": "/avatars/m.png"}})
    client.put(f"/api/characters/{character.id}", json={"persona": "Changed."})
    assert repo.get_character(db, character.id).pfp_set["neutral"] == "/avatars/m.png"


def test_the_members_of_a_chat_carry_their_own_face(client, db, chat, character):
    """A group chat puts the right one on each row, so each member has to bring
    it — the chat's own portrait is only right when there is one of them."""
    client.put(f"/api/characters/{character.id}", json={"pfp_set": {"neutral": "/avatars/m.png"}})
    members = client.get(f"/api/chats/{chat['id']}/members").json()["members"]
    assert members[0]["pfp"] == "/avatars/m.png"


def test_a_character_with_no_picture_says_so_rather_than_erroring(client, db, chat, character):
    client.put(f"/api/characters/{character.id}", json={"pfp_set": {}})
    members = client.get(f"/api/chats/{chat['id']}/members").json()["members"]
    assert members[0]["pfp"] == ""


# ------------------------------------------------- the shape is the card's (§8)


def test_the_shape_defaults_to_the_one_a_card_is_drawn_in():
    """A card is a standing figure far more often than a face, and a square
    crop of one is a picture of somebody's midriff."""
    from app.models import Character

    assert Character(id="c", name="Wren").pfp_shape == "portrait"


def test_the_shape_survives_the_api(client, isolated_settings):
    from app import repo
    from app.db import get_db

    created = client.post("/api/characters", json={"name": "Wren"}).json()
    client.put(f"/api/characters/{created['id']}", json={"name": "Wren", "pfp_shape": "square"})
    assert repo.get_character(get_db(), created["id"]).pfp_shape == "square"

    # and nonsense is refused rather than stored
    client.put(f"/api/characters/{created['id']}", json={"name": "Wren", "pfp_shape": "hexagon"})
    assert repo.get_character(get_db(), created["id"]).pfp_shape == "square"


def test_the_roster_carries_the_shape(client, isolated_settings):
    """Every list that draws a face needs it, or the roster frames a standing
    figure as a square while the conversation beside it does not."""
    created = client.post("/api/characters", json={"name": "Wren"}).json()
    client.put(f"/api/characters/{created['id']}", json={"name": "Wren", "pfp_shape": "square"})

    row = next(c for c in client.get("/api/characters").json() if c["id"] == created["id"])
    assert row["pfp_shape"] == "square"


def test_a_group_member_carries_its_own_shape(db, character):
    """Two members of one group can be framed differently."""
    from app import groups, repo
    from app.models import Character

    other = Character(id="other", name="Kes", pfp_shape="square")
    repo.save_character(db, other)
    chat = repo.create_chat(db, character.id, "group")
    groups.add_member(db, chat["id"], character.id)
    groups.add_member(db, chat["id"], other.id)

    shapes = {m["name"]: m["pfp_shape"] for m in groups.members(db, chat["id"])}
    assert shapes["Kes"] == "square"
    assert shapes[character.name] == "portrait"
