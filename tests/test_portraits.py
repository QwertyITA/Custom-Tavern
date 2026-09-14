"""A face beside every reply (§12).

Reported: no way to give a character a picture at all, and the ones that had
none showed nothing — a transcript of alternating boxes rather than of people
talking. The editor uploads one now, through the same endpoint the persona
avatars use, and every reply reserves the space whether or not there is a
picture to put in it.
"""

from __future__ import annotations

import json

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


def test_replacing_a_portrait_deletes_the_one_it_replaced(client, isolated_settings, isolated_avatars):
    """§KNOWN-ISSUES.md, 'Replacing or removing a portrait leaves the old
    file behind' — confirmCrop()/clearCharacterPfp() only ever overwrote
    pfp_set client-side; the character-deletion cleanup only ever looked at
    the *current* set, so a file a slot used to point at, before it was
    edited to point somewhere else, was never anyone's job to clean up."""
    created = client.post("/api/characters", json={"name": "Wren"}).json()
    first_url, first_path = _upload(client, isolated_avatars, name=b"first")
    client.put(
        f"/api/characters/{created['id']}", json={"name": "Wren", "pfp_set": {"neutral": first_url}}
    )
    second_url, second_path = _upload(client, isolated_avatars, name=b"second")
    response = client.put(
        f"/api/characters/{created['id']}", json={"name": "Wren", "pfp_set": {"neutral": second_url}}
    )
    assert response.json()["pfp_set"]["neutral"] == second_url
    assert not first_path.exists(), "the replaced file should be gone"
    assert second_path.exists(), "the one now in use must survive"


def test_clearing_a_portrait_deletes_it(client, isolated_settings, isolated_avatars):
    created = client.post("/api/characters", json={"name": "Wren"}).json()
    url, path = _upload(client, isolated_avatars)
    client.put(f"/api/characters/{created['id']}", json={"name": "Wren", "pfp_set": {"neutral": url}})

    client.put(f"/api/characters/{created['id']}", json={"name": "Wren", "pfp_set": {}})
    assert not path.exists()


def test_replacing_a_portrait_keeps_the_file_if_another_slot_still_uses_it(
    client, isolated_settings, isolated_avatars
):
    """The same file, in a different slot on the same character, is still a
    reason to keep it — the replace only orphans a value nothing points at
    any more, not every value that happens to have changed."""
    created = client.post("/api/characters", json={"name": "Wren"}).json()
    url, path = _upload(client, isolated_avatars)
    client.put(
        f"/api/characters/{created['id']}",
        json={"name": "Wren", "pfp_set": {"neutral": url, "happy": url}},
    )

    client.put(
        f"/api/characters/{created['id']}",
        json={"name": "Wren", "pfp_set": {"happy": url}},
    )
    assert path.exists()


def test_replacing_an_idle_loop_deletes_the_one_it_replaced(
    client, db, isolated_settings, isolated_avatar_idle
):
    """The talking-avatar idle loop shares the exact same gap portraits had,
    for the exact same reason: the upload endpoint overwrote the field with
    no delete of whatever it used to point at."""
    from app import repo

    created = client.post("/api/characters", json={"name": "Wren"}).json()
    first = client.post(
        f"/api/characters/{created['id']}/avatar-idle?filename=first.mp4", content=b"first clip"
    ).json()
    first_url = first["avatar_video"]["idle_video"]
    first_path = isolated_avatar_idle / first_url.rsplit("/", 1)[-1]
    assert first_path.is_file()

    second = client.post(
        f"/api/characters/{created['id']}/avatar-idle?filename=second.mp4", content=b"second clip"
    ).json()
    second_url = second["avatar_video"]["idle_video"]
    second_path = isolated_avatar_idle / second_url.rsplit("/", 1)[-1]

    assert not first_path.exists(), "the replaced clip should be gone"
    assert second_path.exists()
    assert repo.get_character(db, created["id"]).avatar_video.idle_video == second_url


# ----------------------------------------------- the face of someone who left
#
# Reported live as "a wrong picture against a message". The cause was that the
# transcript's faces were looked up in the chat's *membership*, which answers
# a different question: who can speak next. Remove somebody from a group and
# every line they ever said lost its face and silently borrowed the chat
# character's — and once the group was back down to one person, so did every
# line in it.


def _group(db, chat, *names):
    from app import groups
    from app.models import Character

    made = []
    for name in names:
        card = Character(id=name.lower(), name=name,
                         pfp_set={"neutral": f"/avatars/{name.lower()}.png"})
        repo.save_character(db, card)
        groups.add_member(db, chat["id"], card.id)
        made.append(card)
    return made


def test_a_departed_member_is_still_a_voice_in_the_chat(db, chat, character):
    from app import groups

    (bram,) = _group(db, chat, "Bram")
    repo.add_message(db, chat["id"], "assistant", "Bram says something.",
                     speaker_id=bram.id)
    groups.remove_member(db, chat["id"], bram.id)

    assert bram.id not in [m["character_id"] for m in groups.members(db, chat["id"])]
    voices = {v["character_id"]: v for v in groups.voices(db, chat["id"])}
    assert bram.id in voices, "his lines are still in the transcript"
    assert voices[bram.id]["pfp"] == "/avatars/bram.png"
    assert voices[bram.id]["name"] == "Bram"


def test_a_voice_carries_the_same_card_details_a_member_does(db, chat, character):
    from app import groups

    (bram,) = _group(db, chat, "Bram")
    repo.add_message(db, chat["id"], "assistant", "A line.", speaker_id=bram.id)
    voice = next(v for v in groups.voices(db, chat["id"]) if v["character_id"] == bram.id)
    member = next(m for m in groups.members(db, chat["id"]) if m["character_id"] == bram.id)
    for field in ("name", "pfp", "pfp_shape", "pfp_effect"):
        assert voice[field] == member[field]


def test_someone_who_has_not_spoken_is_not_a_voice(db, chat, character):
    from app import groups

    (bram,) = _group(db, chat, "Bram")
    assert bram.id not in [v["character_id"] for v in groups.voices(db, chat["id"])]


def test_a_deleted_card_leaves_no_face_to_borrow(db, chat, character):
    """Not a fallback to somebody else: a face nothing knows any more is the
    blank placeholder, which is the honest answer."""
    from app import groups

    (bram,) = _group(db, chat, "Bram")
    repo.add_message(db, chat["id"], "assistant", "A line.", speaker_id=bram.id)
    repo.delete_character(db, bram.id)
    assert bram.id not in [v["character_id"] for v in groups.voices(db, chat["id"])]


def test_the_users_own_messages_are_not_voices(db, chat, character):
    from app import groups

    repo.add_message(db, chat["id"], "user", "Hello.")
    assert [v["character_id"] for v in groups.voices(db, chat["id"])] == []


def test_each_voice_appears_once_however_many_lines_they_have(db, chat, character):
    from app import groups

    (bram,) = _group(db, chat, "Bram")
    for i in range(4):
        repo.add_message(db, chat["id"], "assistant", f"Line {i}.", speaker_id=bram.id)
    ids = [v["character_id"] for v in groups.voices(db, chat["id"])]
    assert ids.count(bram.id) == 1


def test_the_members_endpoint_serves_the_voices_too(client):
    made = client.post("/api/characters", json={"name": "Mira"}).json()["id"]
    other = client.post("/api/characters", json={"name": "Bram"}).json()["id"]
    chat_id = client.post("/api/chats/group", json={"character_ids": [made, other]}).json()["id"]
    client.post(f"/api/chats/{chat_id}/send", json={"text": "hello", "speaker_id": other})

    body = client.get(f"/api/chats/{chat_id}/members").json()
    assert other in [v["character_id"] for v in body["voices"]]

    client.delete(f"/api/chats/{chat_id}/members/{other}")
    body = client.get(f"/api/chats/{chat_id}/members").json()
    assert other not in [m["character_id"] for m in body["members"]]
    assert other in [v["character_id"] for v in body["voices"]], (
        "his lines are still there, so his face has to be findable"
    )


def test_a_freshly_added_message_reports_its_own_speaker(db, chat, character):
    """The copy `add_message` hands back is what the `reply` event carries, and
    it is what the client swaps its streaming bubble for. It was the one
    message object in the app with no speaker on it — stored correctly the
    whole time, simply not reported — so in a group the finished reply lost
    its face and its name to the chat's nominal character until the chat was
    reopened."""
    made = repo.add_message(db, chat["id"], "assistant", "A line.",
                            speaker_id=character.id)
    assert made["speaker_id"] == character.id
    assert repo.get_message(db, made["id"])["speaker_id"] == character.id


def test_the_reported_speaker_matches_every_read_path(db, chat, character):
    made = repo.add_message(db, chat["id"], "assistant", "A line.",
                            speaker_id=character.id)
    listed = next(m for m in repo.list_messages(db, chat["id"]) if m["id"] == made["id"])
    assert made["speaker_id"] == listed["speaker_id"] == repo.get_message(db, made["id"])["speaker_id"]


def test_a_message_with_no_speaker_reports_an_empty_one(db, chat, character):
    made = repo.add_message(db, chat["id"], "user", "Hello.")
    assert made["speaker_id"] == ""


def test_the_reply_event_carries_the_speaker(client):
    """End to end, over the stream the client actually reads."""
    mira = client.post("/api/characters", json={"name": "Mira"}).json()["id"]
    bram = client.post("/api/characters", json={"name": "Bram"}).json()["id"]
    chat_id = client.post("/api/chats/group",
                          json={"character_ids": [mira, bram]}).json()["id"]
    body = client.post(f"/api/chats/{chat_id}/send",
                       json={"text": "hello", "speaker_id": bram}).text

    replies = [json.loads(line[6:]) for line in body.splitlines()
               if line.startswith("data: ") and '"reply"' in line]
    assert replies, "no reply event in the stream"
    assert replies[-1]["message"]["speaker_id"] == bram
