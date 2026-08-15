"""Group chats (roadmap 8): membership, and who speaks next.

The default turn policy is deliberately not round-robin. Round-robin is the
arrangement where you say something to one person and the other one answers,
forever — it is the single thing that makes a group chat read as a mechanism
rather than as a room. Most of what is protected here is that the free,
rule-based default behaves the way a person would expect.
"""

from __future__ import annotations

import pytest

from app import groups, repo
from app.models import Character


def a_character(db, name: str, character_id: str = "") -> Character:
    card = Character(id=character_id or name.lower(), name=name, persona=f"{name} is here.")
    repo.save_character(db, card)
    return card


def a_group(db, chat, *names) -> list[Character]:
    made = []
    for name in names:
        card = a_character(db, name)
        groups.add_member(db, chat["id"], card.id)
        made.append(card)
    return made


# ------------------------------------------------------------- membership


def test_a_new_chat_already_has_its_character_in_it(db, chat, character):
    """A solo chat is a group of one, so there is never a chat with nobody in
    it to reply."""
    assert [m["character_id"] for m in groups.members(db, chat["id"])] == [character.id]
    assert groups.is_group(db, chat["id"]) is False


def test_a_second_character_makes_it_a_group(db, chat, character):
    a_group(db, chat, "Harrow")
    assert groups.is_group(db, chat["id"]) is True
    assert len(groups.members(db, chat["id"])) == 2


def test_adding_the_same_character_twice_is_harmless(db, chat, character):
    groups.add_member(db, chat["id"], character.id)
    assert len(groups.members(db, chat["id"])) == 1


def test_members_come_back_in_join_order(db, chat, character):
    a_group(db, chat, "Harrow", "Anna")
    assert [m["name"] for m in groups.members(db, chat["id"])][1:] == ["Harrow", "Anna"]


def test_talkativeness_is_clamped(db, chat, character):
    groups.update_member(db, chat["id"], character.id, talkativeness=99)
    assert groups.members(db, chat["id"])[0]["talkativeness"] == 2.0
    groups.update_member(db, chat["id"], character.id, talkativeness=-5)
    assert groups.members(db, chat["id"])[0]["talkativeness"] == 0.0


def test_ensure_member_repairs_a_chat_from_before_the_table(db, chat, character):
    groups.remove_member(db, chat["id"], character.id)
    assert groups.members(db, chat["id"]) == []
    groups.ensure_member(db, chat["id"], character.id)
    assert len(groups.members(db, chat["id"])) == 1


# --------------------------------------------------------- who speaks next


def pick(db, chat, **kw):
    return groups.choose_speaker(db, chat["id"], **kw)


def test_a_solo_chat_always_picks_its_one_character(db, chat, character):
    assert pick(db, chat, user_text="anything")["character_id"] == character.id


def test_being_named_wins(db, chat, character):
    """The way anyone would read it, and it costs a substring search."""
    harrow, = a_group(db, chat, "Harrow")
    assert pick(db, chat, user_text="Harrow, is the ferry running?")["name"] == "Harrow"
    assert pick(db, chat, user_text=f"{character.name}, what do you think?")["name"] == character.name


def test_a_name_matches_whole_words_only(db, chat, character):
    a_group(db, chat, "Wren")
    # "wrench" is not Wren, and neither is the middle of a longer word.
    chosen = pick(db, chat, user_text="pass me the wrench", seed=1)
    assert chosen is not None  # it fell through to weighted chance
    assert groups.addressed("pass me the wrench", groups.members(db, chat["id"])) is None


def test_the_longer_name_wins_when_one_contains_the_other(db, chat, character):
    a_group(db, chat, "Anna", "Anna Vale")
    members = groups.members(db, chat["id"])
    assert groups.addressed("Anna Vale, a word", members)["name"] == "Anna Vale"


def test_naming_two_people_chooses_neither(db, chat, character):
    """That message has not chosen between them, and picking the first would be
    arbitrary in a way the person would notice."""
    a_group(db, chat, "Harrow", "Anna")
    members = groups.members(db, chat["id"])
    assert groups.addressed("Harrow and Anna, listen", members) is None


def test_naming_matches_regardless_of_case(db, chat, character):
    a_group(db, chat, "Harrow")
    members = groups.members(db, chat["id"])
    assert groups.addressed("harrow, hello", members)["name"] == "Harrow"


def test_a_name_with_regex_characters_is_matched_literally(db, chat, character):
    a_character(db, "R. Vale (the elder)", "rv")
    groups.add_member(db, chat["id"], "rv")
    members = groups.members(db, chat["id"])
    assert groups.addressed("R. Vale (the elder), a word", members) is not None


def test_a_muted_character_never_speaks(db, chat, character):
    harrow, = a_group(db, chat, "Harrow")
    groups.update_member(db, chat["id"], harrow.id, muted=True)
    for _ in range(20):
        assert pick(db, chat, user_text="hello")["character_id"] == character.id


def test_naming_a_muted_character_does_not_wake_them(db, chat, character):
    harrow, = a_group(db, chat, "Harrow")
    groups.update_member(db, chat["id"], harrow.id, muted=True)
    assert pick(db, chat, user_text="Harrow, say something")["character_id"] == character.id


def test_everyone_muted_means_nobody_replies(db, chat, character):
    harrow, = a_group(db, chat, "Harrow")
    for member in groups.members(db, chat["id"]):
        groups.update_member(db, chat["id"], member["character_id"], muted=True)
    assert pick(db, chat, user_text="anyone?") is None


def test_talkativeness_shifts_the_odds(db, chat, character):
    """Not a guarantee for any one turn — a weight is not a rule."""
    harrow, = a_group(db, chat, "Harrow")
    groups.update_member(db, chat["id"], character.id, talkativeness=0.0)
    groups.update_member(db, chat["id"], harrow.id, talkativeness=2.0)

    picked = [pick(db, chat, user_text="hello", seed=i)["name"] for i in range(60)]
    assert picked.count("Harrow") > picked.count(character.name) * 3


def test_the_last_speaker_is_pushed_down_but_not_out(db, chat, character):
    """A room where a character can never follow their own line has its own
    tell, so the penalty is a weight rather than a ban."""
    harrow, = a_group(db, chat, "Harrow")
    picked = [
        pick(db, chat, user_text="hello", last_speaker=harrow.id, seed=i)["name"]
        for i in range(80)
    ]
    assert picked.count(character.name) > picked.count("Harrow")
    assert "Harrow" in picked, "pushed down, not banned"


def test_round_robin_goes_round(db, chat, character):
    harrow, = a_group(db, chat, "Harrow")
    first = pick(db, chat, policy="round_robin", last_speaker="")
    second = pick(db, chat, policy="round_robin", last_speaker=first["character_id"])
    third = pick(db, chat, policy="round_robin", last_speaker=second["character_id"])
    assert first["character_id"] != second["character_id"]
    assert third["character_id"] == first["character_id"]


def test_round_robin_skips_the_muted(db, chat, character):
    harrow, anna = a_group(db, chat, "Harrow", "Anna")
    groups.update_member(db, chat["id"], harrow.id, muted=True)
    names = {pick(db, chat, policy="round_robin", last_speaker=s)["name"]
             for s in ("", character.id, anna.id)}
    assert "Harrow" not in names


def test_round_robin_recovers_when_the_last_speaker_has_left(db, chat, character):
    a_group(db, chat, "Harrow")
    assert pick(db, chat, policy="round_robin", last_speaker="someone-who-left") is not None


def test_manual_waits_to_be_told(db, chat, character):
    """Inventing a speaker would defeat the point of the policy."""
    a_group(db, chat, "Harrow")
    assert pick(db, chat, policy="manual", user_text="hello") is None


def test_manual_honours_the_choice(db, chat, character):
    harrow, = a_group(db, chat, "Harrow")
    assert pick(db, chat, policy="manual", forced=harrow.id)["name"] == "Harrow"


def test_a_forced_choice_beats_being_named(db, chat, character):
    harrow, = a_group(db, chat, "Harrow")
    chosen = pick(db, chat, user_text=f"{character.name}, hello", forced=harrow.id)
    assert chosen["name"] == "Harrow"


def test_forcing_a_muted_character_is_ignored(db, chat, character):
    """Asking someone silent to speak is a contradiction worth ignoring."""
    harrow, = a_group(db, chat, "Harrow")
    groups.update_member(db, chat["id"], harrow.id, muted=True)
    assert pick(db, chat, forced=harrow.id) is None


def test_forcing_someone_who_is_not_here_is_ignored(db, chat, character):
    assert pick(db, chat, forced="a-stranger") is None


def test_an_empty_chat_has_nobody_to_speak(db, chat, character):
    groups.remove_member(db, chat["id"], character.id)
    assert pick(db, chat, user_text="hello") is None


# ------------------------------------------------------------- the prompt


def test_the_cast_note_names_the_others(db, chat, character):
    a_group(db, chat, "Harrow", "Anna")
    note = groups.cast_note(groups.members(db, chat["id"]), character.id)
    assert "Harrow" in note and "Anna" in note
    assert character.name not in note, "you are not also here"
    assert "only your own words" in note


def test_a_muted_character_is_still_in_the_room(db, chat, character):
    """Someone standing there saying nothing is still in the scene; leaving
    them out would have the others talk as if the room were empty."""
    harrow, = a_group(db, chat, "Harrow")
    groups.update_member(db, chat["id"], harrow.id, muted=True)
    assert "Harrow" in groups.cast_note(groups.members(db, chat["id"]), character.id)


def test_a_solo_chat_has_no_cast_note(db, chat, character):
    """So its prompt is byte-identical to what it was before groups existed."""
    assert groups.cast_note(groups.members(db, chat["id"]), character.id) == ""


def test_the_cast_reaches_the_prompt(db, chat, character):
    from app import assembly
    from app.config import Settings

    a_group(db, chat, "Harrow")
    out = assembly.build_reply_context(db, chat, character, Settings())
    assert "Harrow" in out.system


def test_the_cast_can_be_switched_off_like_any_section(db, chat, character):
    from app import assembly
    from app.config import Settings

    a_group(db, chat, "Harrow")
    settings = Settings(prompt_sections=[{"id": "cast", "enabled": False}])
    assert "Harrow" not in assembly.build_reply_context(db, chat, character, settings).system


# ----------------------------------------------------------- through a turn


def send(client, chat_id: str, text: str, speaker: str = "") -> None:
    body = {"text": text}
    if speaker:
        body["speaker_id"] = speaker
    with client.stream("POST", f"/api/chats/{chat_id}/send", json=body) as response:
        assert response.status_code == 200
        for _ in response.iter_lines():
            pass


def api_chat(client) -> tuple[str, str]:
    character_id = client.get("/api/characters").json()[0]["id"]
    return character_id, client.post(
        "/api/chats", json={"character_id": character_id}
    ).json()["id"]


def test_a_reply_records_who_said_it(client):
    _, chat_id = api_chat(client)
    send(client, chat_id, "hello")
    replies = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
               if m["role"] == "assistant" and m["turn"] > 0]
    assert replies and all(m["speaker_id"] for m in replies)


def test_the_turn_announces_who_will_answer(client):
    """So the placeholder can carry their name instead of the chat's nominal
    character."""
    import json as _json

    _, chat_id = api_chat(client)
    events = []
    with client.stream("POST", f"/api/chats/{chat_id}/send", json={"text": "hi"}) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(_json.loads(line[5:]))
    start = next(e for e in events if e["type"] == "turn_start")
    assert start["speaker"]["name"]


def test_naming_someone_gets_them(client):
    _, chat_id = api_chat(client)
    other = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    client.post(f"/api/chats/{chat_id}/members", json={"character_id": other})

    send(client, chat_id, "Harrow, is the ferry running?")
    reply = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
             if m["role"] == "assistant"][-1]
    assert reply["speaker_id"] == other


def test_the_members_endpoint_reports_the_room(client):
    _, chat_id = api_chat(client)
    body = client.get(f"/api/chats/{chat_id}/members").json()
    assert len(body["members"]) == 1
    assert body["policy"] == groups.DEFAULT_POLICY
    assert [p["id"] for p in body["policies"]] == list(groups.POLICY_IDS)
    assert all(p["note"] for p in body["policies"])


def test_muting_through_the_api_sticks(client):
    character_id, chat_id = api_chat(client)
    client.patch(f"/api/chats/{chat_id}/members/{character_id}", json={"muted": True})
    assert client.get(f"/api/chats/{chat_id}/members").json()["members"][0]["muted"] is True


def test_the_last_person_cannot_be_removed(client):
    """A chat with nobody in it has nobody to reply, and the way back is not
    obvious from the UI."""
    character_id, chat_id = api_chat(client)
    response = client.delete(f"/api/chats/{chat_id}/members/{character_id}")
    assert response.status_code == 400
    assert "mute" in response.json()["detail"]


def test_someone_can_be_removed_once_there_are_two(client):
    character_id, chat_id = api_chat(client)
    other = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    client.post(f"/api/chats/{chat_id}/members", json={"character_id": other})
    assert client.delete(f"/api/chats/{chat_id}/members/{other}").status_code == 200
    assert len(client.get(f"/api/chats/{chat_id}/members").json()["members"]) == 1


def test_the_policy_can_be_changed_and_sticks(client):
    _, chat_id = api_chat(client)
    assert client.put(f"/api/chats/{chat_id}/policy", json={"policy": "round_robin"}).json()["ok"]
    assert client.get(f"/api/chats/{chat_id}/members").json()["policy"] == "round_robin"


def test_an_unknown_policy_is_refused(client):
    _, chat_id = api_chat(client)
    assert client.put(f"/api/chats/{chat_id}/policy", json={"policy": "chaos"}).status_code == 400


def test_manual_needs_a_speaker_and_says_so(client):
    import json as _json

    _, chat_id = api_chat(client)
    other = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    client.post(f"/api/chats/{chat_id}/members", json={"character_id": other})
    client.put(f"/api/chats/{chat_id}/policy", json={"policy": "manual"})

    events = []
    with client.stream("POST", f"/api/chats/{chat_id}/send", json={"text": "hi"}) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(_json.loads(line[5:]))
    assert any(e["type"] == "error" for e in events)

    send(client, chat_id, "hi again", speaker=other)
    reply = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
             if m["role"] == "assistant"][-1]
    assert reply["speaker_id"] == other


def test_adding_a_character_that_does_not_exist_is_a_404(client):
    _, chat_id = api_chat(client)
    response = client.post(f"/api/chats/{chat_id}/members", json={"character_id": "nope"})
    assert response.status_code == 404


def test_two_characters_keep_separate_state_through_real_turns(client):
    """The whole reason namespacing came first (§15)."""
    from app.db import get_db
    from app.state import SLICE_VARS, read_slice, slice_for

    character_id, chat_id = api_chat(client)
    other = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    client.post(f"/api/chats/{chat_id}/members", json={"character_id": other})

    send(client, chat_id, f"Harrow, hello")
    send(client, chat_id, "Mira, hello")

    db = get_db()
    stored = {
        who: read_slice(db, chat_id, slice_for(SLICE_VARS, who))
        for who in (character_id, other)
    }
    assert all(v is not None for v in stored.values()), stored
