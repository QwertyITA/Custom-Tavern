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
