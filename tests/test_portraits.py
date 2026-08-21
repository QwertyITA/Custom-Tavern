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


# ------------------------------------- the picture leaves with the character


def _upload(client, isolated_avatars, name=b"a portrait"):
    response = client.post("/api/avatars?filename=mine.png", content=name)
    assert response.status_code == 200
    url = response.json()["url"]
    path = isolated_avatars / url.rsplit("/", 1)[-1]
    assert path.is_file()
    return url, path


def test_deleting_a_character_deletes_its_portrait(client, isolated_settings, isolated_avatars):
    """Nothing else in the app ever looks at data/avatars/ to see what is
    still wanted, so before this the directory only ever grew — one file left
    behind for every character anyone ever tried and deleted."""
    created = client.post("/api/characters", json={"name": "Wren"}).json()
    url, path = _upload(client, isolated_avatars)
    client.put(f"/api/characters/{created['id']}", json={"name": "Wren", "pfp_set": {"neutral": url}})

    client.delete(f"/api/characters/{created['id']}")
    assert not path.exists()


def test_bundled_card_art_is_never_touched(client, isolated_settings, isolated_avatars):
    """Only entries this app itself wrote, which always begin "/avatars/" —
    never a card's own shipped art, served from the tracked static tree."""
    created = client.post("/api/characters", json={"name": "Wren"}).json()
    client.put(
        f"/api/characters/{created['id']}",
        json={"name": "Wren", "pfp_set": {"neutral": "wren/neutral.png"}},
    )
    # Nothing to assert on the filesystem — there is no data/avatars/ file
    # for this at all — only that deletion does not error trying to find one.
    response = client.delete(f"/api/characters/{created['id']}")
    assert response.status_code == 200


def test_a_shared_avatar_survives_if_a_persona_still_uses_it(
    client, db, isolated_settings, isolated_avatars
):
    """data/avatars/ is shared with personas through the same upload
    endpoint. A filename outliving the character it was cropped for is not
    proof nothing wants it."""
    from app import repo

    created = client.post("/api/characters", json={"name": "Wren"}).json()
    url, path = _upload(client, isolated_avatars)
    filename = url.rsplit("/", 1)[-1]
    client.put(f"/api/characters/{created['id']}", json={"name": "Wren", "pfp_set": {"neutral": url}})
    repo.save_persona(db, {"id": "p1", "name": "Me", "avatar": filename})

    client.delete(f"/api/characters/{created['id']}")
    assert path.exists()


def test_a_shared_avatar_survives_if_another_character_still_uses_it(
    client, isolated_settings, isolated_avatars
):
    a = client.post("/api/characters", json={"name": "Wren"}).json()
    b = client.post("/api/characters", json={"name": "Kes"}).json()
    url, path = _upload(client, isolated_avatars)
    for character in (a, b):
        client.put(
            f"/api/characters/{character['id']}",
            json={"name": character["name"], "pfp_set": {"neutral": url}},
        )

    client.delete(f"/api/characters/{a['id']}")
    assert path.exists(), "the surviving character still needs it"
    client.delete(f"/api/characters/{b['id']}")
    assert not path.exists()


def test_deleting_an_unknown_character_does_not_error(client, isolated_settings):
    response = client.delete("/api/characters/does-not-exist")
    assert response.status_code == 200
