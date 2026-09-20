"""Group chats (roadmap 8): membership, and who speaks next.

The default turn policy is deliberately not round-robin. Round-robin is the
arrangement where you say something to one person and the other one answers,
forever — it is the single thing that makes a group chat read as a mechanism
rather than as a room. Most of what is protected here is that the free,
rule-based default behaves the way a person would expect.
"""

from __future__ import annotations

import random

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


def test_nobody_answers_themselves_while_somebody_else_could(db, chat, character):
    """The old rule was a 0.25 weight penalty, and a weight is not a rule: a
    group of three regularly read as one person talking to themselves. The one
    who just spoke now sits the next one out — unless you name them, or unless
    they are all there is."""
    harrow, = a_group(db, chat, "Harrow")
    picked = [
        pick(db, chat, user_text="hello", last_speaker=harrow.id, seed=i)["name"]
        for i in range(40)
    ]
    assert set(picked) == {character.name}


def test_naming_the_one_who_just_spoke_still_gets_them(db, chat, character):
    """Asking the same character a second question means you want them, not
    the person standing beside them."""
    harrow, = a_group(db, chat, "Harrow")
    assert pick(db, chat, user_text="Harrow, again?", last_speaker=harrow.id)["name"] == "Harrow"


def test_they_can_follow_their_own_line_when_it_is_switched_on(db, chat, character):
    """A room where a character can never follow their own line has its own
    tell, so the ban is a setting rather than a law."""
    harrow, = a_group(db, chat, "Harrow")
    picked = [
        groups.plan(
            groups.members(db, chat["id"]),
            user_text="hello",
            last_speaker=harrow.id,
            self_responses=True,
            replies=1,
            rng=random.Random(i),
        )[0]["name"]
        for i in range(60)
    ]
    assert "Harrow" in picked


def test_the_only_one_left_speaks_even_though_they_just_did(db, chat, character):
    """The ban is "while somebody else could", not "never"."""
    harrow, = a_group(db, chat, "Harrow")
    groups.update_member(db, chat["id"], character.id, muted=True)
    assert pick(db, chat, user_text="hello", last_speaker=harrow.id)["name"] == "Harrow"


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
    # The first reply, not the last: somebody else may well chime in after
    # them now (§ groups.plan), and being named is a claim on the front of
    # the queue rather than on the whole turn.
    replies = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
               if m["role"] == "assistant" and m["turn"] > 0]
    assert replies[0]["speaker_id"] == other


def test_the_members_endpoint_reports_the_room(client):
    _, chat_id = api_chat(client)
    body = client.get(f"/api/chats/{chat_id}/members").json()
    assert len(body["members"]) == 1
    assert body["policy"] == groups.DEFAULT_POLICY
    assert [p["id"] for p in body["policies"]] == list(groups.POLICY_IDS)
    assert all(p["note"] for p in body["policies"])


# ------------------------------------------------------ starting one as one


def test_creating_a_group_chat_adds_every_character_at_once(client):
    a = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    b = client.post("/api/characters", json={"name": "Anna"}).json()["id"]

    chat = client.post("/api/chats/group", json={"character_ids": [a, b]}).json()
    body = client.get(f"/api/chats/{chat['id']}/members").json()
    assert {m["character_id"] for m in body["members"]} == {a, b}


def test_a_group_chat_needs_at_least_two_characters(client):
    a = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    response = client.post("/api/chats/group", json={"character_ids": [a]})
    assert response.status_code == 400


def test_a_group_chat_rejects_an_unknown_character(client):
    a = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    response = client.post("/api/chats/group", json={"character_ids": [a, "not-a-real-character"]})
    assert response.status_code == 404


def test_a_group_chat_dedupes_a_repeated_id(client):
    """The same character twice is one character, not two — same reasoning
    as add_member's own ON CONFLICT DO NOTHING."""
    a = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    b = client.post("/api/characters", json={"name": "Anna"}).json()["id"]

    chat = client.post("/api/chats/group", json={"character_ids": [a, a, b]}).json()
    body = client.get(f"/api/chats/{chat['id']}/members").json()
    assert {m["character_id"] for m in body["members"]} == {a, b}


def test_the_greeting_comes_from_the_first_character_named(client):
    a = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    b = client.post("/api/characters", json={"name": "Anna"}).json()["id"]
    client.put(f"/api/characters/{a}", json={"first_mes": "Harrow nods once."})
    client.put(f"/api/characters/{b}", json={"first_mes": "Anna waves."})

    chat = client.post("/api/chats/group", json={"character_ids": [a, b]}).json()
    messages = client.get(f"/api/chats/{chat['id']}/messages").json()
    assert messages and messages[0]["text"] == "Harrow nods once."


def test_a_group_chat_reaches_every_members_own_history(client):
    """The point of starting one as one rather than growing into it: it
    belongs to everybody in it from the first line, not just the character
    it happens to be filed under."""
    a = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    b = client.post("/api/characters", json={"name": "Anna"}).json()["id"]
    chat = client.post("/api/chats/group", json={"character_ids": [a, b]}).json()

    for character_id in (a, b):
        listed = client.get(f"/api/chats?character_id={character_id}").json()
        assert chat["id"] in {c["id"] for c in listed}


def test_list_chats_reports_is_group_and_member_ids(client):
    a = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    b = client.post("/api/characters", json={"name": "Anna"}).json()["id"]
    group_chat = client.post("/api/chats/group", json={"character_ids": [a, b]}).json()
    solo_chat = client.post("/api/chats", json={"character_id": a}).json()

    listed = {c["id"]: c for c in client.get("/api/chats").json()}
    assert listed[group_chat["id"]]["is_group"] is True
    assert set(listed[group_chat["id"]]["member_ids"]) == {a, b}
    assert listed[solo_chat["id"]]["is_group"] is False
    assert listed[solo_chat["id"]]["member_ids"] == [a]


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


# ------------------------------------------------- a turn is not one reply
#
# The first version of this picked exactly one speaker per message. That is
# what made a group read as a switchboard: nobody could react to what
# somebody else had just said, and naming two people got you neither.


def members_of(db, chat):
    return groups.members(db, chat["id"])


def test_naming_two_people_gets_both_of_them(db, chat, character):
    """The old rule said a message naming two people had chosen neither, and
    fell through to a weighted guess. It had chosen both."""
    a_group(db, chat, "Harrow", "Anna")
    groups.update_member(db, chat["id"], character.id, talkativeness=0.0)
    spoke = groups.plan(
        members_of(db, chat), user_text="Harrow and Anna, listen", replies=3,
    )
    assert [m["name"] for m in spoke] == ["Harrow", "Anna"]


def test_they_answer_in_the_order_they_were_named(db, chat, character):
    a_group(db, chat, "Harrow", "Anna")
    groups.update_member(db, chat["id"], character.id, talkativeness=0.0)
    spoke = groups.plan(
        members_of(db, chat), user_text="Anna — and you too, Harrow", replies=3,
    )
    assert [m["name"] for m in spoke] == ["Anna", "Harrow"]


def test_how_many_answer_is_capped_by_the_setting(db, chat, character):
    a_group(db, chat, "Harrow", "Anna", "Wren")
    for cap in (1, 2, 3):
        spoke = groups.plan(
            members_of(db, chat),
            user_text="Harrow, Anna, Wren — all of you",
            replies=cap,
        )
        assert len(spoke) == cap


def test_the_cap_cannot_be_talked_past(db, chat, character):
    """A hand-edited chat settings row is not a way to spend six generations
    on one message."""
    a_group(db, chat, "Harrow", "Anna", "Wren")
    spoke = groups.plan(members_of(db, chat), user_text="everyone", replies=99)
    assert len(spoke) <= groups.MAX_REPLIES_PER_TURN


def test_nobody_answers_twice_in_one_turn(db, chat, character):
    a_group(db, chat, "Harrow", "Anna")
    for seed in range(30):
        spoke = groups.plan(
            members_of(db, chat), user_text="hello", replies=4,
            rng=random.Random(seed),
        )
        ids = [m["character_id"] for m in spoke]
        assert len(ids) == len(set(ids))


def test_talkativeness_is_a_chance_to_speak_not_a_share_of_one_slot(db, chat, character):
    """SillyTavern's roll, and it is the right shape: a character at 1.0 joins
    in every time, and one at 0 never volunteers."""
    harrow, anna = a_group(db, chat, "Harrow", "Anna")
    groups.update_member(db, chat["id"], harrow.id, talkativeness=1.0)
    groups.update_member(db, chat["id"], anna.id, talkativeness=0.0)
    groups.update_member(db, chat["id"], character.id, talkativeness=0.0)

    spoke = [
        [m["name"] for m in groups.plan(
            members_of(db, chat), user_text="hello", replies=4, rng=random.Random(s)
        )]
        for s in range(25)
    ]
    assert all("Harrow" in names for names in spoke)
    assert not any("Anna" in names for names in spoke)


def test_everyone_silent_still_gets_one_answer(db, chat, character):
    """Talkativeness at zero all round means nobody volunteers, and a message
    that goes unanswered is worse than an unlikely answer."""
    harrow, = a_group(db, chat, "Harrow")
    for member in members_of(db, chat):
        groups.update_member(db, chat["id"], member["character_id"], talkativeness=0.0)
    assert len(groups.plan(members_of(db, chat), user_text="hello")) == 1


def test_take_turns_takes_the_next_ones_in_order(db, chat, character):
    harrow, anna = a_group(db, chat, "Harrow", "Anna")
    spoke = groups.plan(
        members_of(db, chat), policy="round_robin", last_speaker=character.id, replies=2,
    )
    assert [m["name"] for m in spoke] == ["Harrow", "Anna"]


def test_pooled_lets_everybody_speak_before_anybody_repeats(db, chat, character):
    harrow, anna = a_group(db, chat, "Harrow", "Anna")
    spoke = groups.plan(
        members_of(db, chat), policy="pooled", replies=1,
        spoken_since_user=(character.id, harrow.id), rng=random.Random(3),
    )
    assert [m["name"] for m in spoke] == ["Anna"]


def test_pooled_refills_once_everyone_has_had_a_turn(db, chat, character):
    harrow, = a_group(db, chat, "Harrow")
    spoke = groups.plan(
        members_of(db, chat), policy="pooled", replies=1,
        spoken_since_user=(character.id, harrow.id), rng=random.Random(1),
    )
    assert len(spoke) == 1


def test_manual_still_answers_with_exactly_who_was_asked_for(db, chat, character):
    harrow, = a_group(db, chat, "Harrow")
    spoke = groups.plan(
        members_of(db, chat), policy="manual", forced=harrow.id, replies=4,
    )
    assert [m["name"] for m in spoke] == ["Harrow"]


def test_the_pool_of_who_has_spoken_resets_at_your_own_message(db, chat, character):
    harrow, = a_group(db, chat, "Harrow")
    repo.add_message(db, chat["id"], "assistant", "one", speaker_id=character.id)
    repo.add_message(db, chat["id"], "user", "your turn")
    repo.add_message(db, chat["id"], "assistant", "two", speaker_id=harrow.id)
    assert groups.spoken_since_user(db, chat["id"]) == (harrow.id,)


# ------------------------------------------------------- the group settings


def test_the_settings_have_defaults_that_do_not_need_storing(db, chat, character):
    config = groups.settings_for(chat)
    assert config["policy"] == groups.DEFAULT_POLICY
    assert config["replies_per_turn"] == groups.DEFAULT_REPLIES_PER_TURN
    assert config["self_responses"] is False
    assert config["cast_detail"] == groups.DEFAULT_CAST_DETAIL


def test_a_hand_broken_settings_row_still_produces_a_usable_group():
    config = groups.settings_for(
        {"settings": {"policy": "nonsense", "replies_per_turn": "lots",
                      "cast_detail": "everything"}}
    )
    assert config["policy"] == groups.DEFAULT_POLICY
    assert config["replies_per_turn"] == groups.DEFAULT_REPLIES_PER_TURN
    assert config["cast_detail"] == groups.DEFAULT_CAST_DETAIL


def test_the_group_endpoint_takes_one_setting_at_a_time(client):
    _, chat_id = api_chat(client)
    other = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    client.post(f"/api/chats/{chat_id}/members", json={"character_id": other})

    client.put(f"/api/chats/{chat_id}/group", json={"replies_per_turn": 3})
    body = client.put(f"/api/chats/{chat_id}/group", json={"self_responses": True}).json()
    # The first one is still there: a body naming one key must not reset the rest.
    assert body["replies_per_turn"] == 3
    assert body["self_responses"] is True
    assert body["policy"] == groups.DEFAULT_POLICY


def test_the_group_endpoint_refuses_what_it_cannot_honour(client):
    _, chat_id = api_chat(client)
    assert client.put(f"/api/chats/{chat_id}/group", json={"policy": "vibes"}).status_code == 400
    assert client.put(f"/api/chats/{chat_id}/group", json={"replies_per_turn": 0}).status_code == 400
    assert client.put(f"/api/chats/{chat_id}/group", json={"replies_per_turn": 99}).status_code == 400
    assert client.put(f"/api/chats/{chat_id}/group", json={"cast_detail": "all"}).status_code == 400


def test_the_members_endpoint_carries_every_control_the_panel_draws(client):
    _, chat_id = api_chat(client)
    body = client.get(f"/api/chats/{chat_id}/members").json()
    for key in ("policy", "policies", "replies_per_turn", "self_responses",
                "cast_detail", "cast_details", "max_replies_per_turn"):
        assert key in body, key


def test_two_characters_can_both_answer_one_message(client):
    """End to end, through a real turn: the whole point of the change."""
    mira, chat_id = api_chat(client)
    other = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    client.post(f"/api/chats/{chat_id}/members", json={"character_id": other})
    client.put(f"/api/chats/{chat_id}/group", json={"replies_per_turn": 2})

    send(client, chat_id, "Mira, Harrow — both of you, then.")
    replies = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
               if m["role"] == "assistant" and m["turn"] > 0]
    assert len(replies) == 2
    assert {m["speaker_id"] for m in replies} == {mira, other}
    # Same turn, in the order they were planned — the transcript has to read
    # as one exchange rather than as two.
    assert {m["turn"] for m in replies} == {1}


def test_the_second_speaker_is_announced_before_they_start(client):
    import json as _json

    _, chat_id = api_chat(client)
    other = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    client.post(f"/api/chats/{chat_id}/members", json={"character_id": other})
    client.put(f"/api/chats/{chat_id}/group", json={"replies_per_turn": 2})

    events = []
    with client.stream("POST", f"/api/chats/{chat_id}/send",
                       json={"text": "Mira, Harrow — both of you."}) as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(_json.loads(line[5:]))

    start = next(e for e in events if e["type"] == "turn_start")
    assert [s["name"] for s in start["speakers"]] == ["Mira", "Harrow"]
    second = next(e for e in events if e["type"] == "speaker_start")
    assert second["speaker"]["id"] == other
    # And it arrives after the first reply landed, never before it: the client
    # tears its streaming bubble down on this event.
    assert events.index(second) > events.index(
        next(e for e in events if e["type"] == "reply")
    )


# ------------------------------------- re-rolling the right character's line
#
# Every one of these paths resolved `chat["character_id"]` — the chat's
# *nominal* character, the one it was created from — rather than the speaker
# of the message being worked on. In a group that is very often somebody
# else, so re-rolling Harrow's reply rewrote it as Mira, in Mira's voice,
# against Mira's state schema, and stored Mira's state writes for it.


def a_two_reply_turn(client) -> tuple[str, str, str, list[dict]]:
    """A group chat where both characters have answered one message."""
    mira, chat_id = api_chat(client)
    harrow = client.post("/api/characters", json={"name": "Harrow"}).json()["id"]
    client.post(f"/api/chats/{chat_id}/members", json={"character_id": harrow})
    client.put(f"/api/chats/{chat_id}/group", json={"replies_per_turn": 2})
    send(client, chat_id, "Mira, Harrow — both of you, then.")
    replies = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
               if m["role"] == "assistant" and m["turn"] > 0]
    return mira, harrow, chat_id, replies


def test_a_swipe_re_rolls_the_character_who_said_it(client):
    mira, harrow, chat_id, replies = a_two_reply_turn(client)
    theirs = next(m for m in replies if m["speaker_id"] == harrow)

    with client.stream("POST", f"/api/messages/{theirs['id']}/swipe") as response:
        assert response.status_code == 200
        for _ in response.iter_lines():
            pass

    after = next(m for m in client.get(f"/api/chats/{chat_id}/messages").json()
                 if m["id"] == theirs["id"])
    assert after["speaker_id"] == harrow
    assert after["variant_count"] == 2
    # The row's speaker was never the bug — it is stored on the message and a
    # swipe does not touch it. What was wrong is who the re-roll was actually
    # *written as*, which only the prompt it was written from can answer.
    prompt = client.get(f"/api/messages/{theirs['id']}/prompt").json()
    turn_note = next(p for p in prompt["parts"] if p["id"] == "turn")
    assert "You are Harrow" in turn_note["text"]


def test_a_continue_carries_on_in_the_voice_that_stopped(client):
    mira, harrow, chat_id, replies = a_two_reply_turn(client)
    theirs = next(m for m in replies if m["speaker_id"] == harrow)

    with client.stream("POST", f"/api/messages/{theirs['id']}/continue") as response:
        assert response.status_code == 200
        for _ in response.iter_lines():
            pass

    after = next(m for m in client.get(f"/api/chats/{chat_id}/messages").json()
                 if m["id"] == theirs["id"])
    assert after["speaker_id"] == harrow


def test_re_rolling_the_first_reply_does_not_show_it_the_answer_to_itself(db, chat, character):
    """A turn can hold several replies now, so re-rolling the first of them is
    no longer the same as re-rolling the last thing said."""
    from app import assembly, config

    harrow, = a_group(db, chat, "Harrow")
    repo.add_message(db, chat["id"], "user", "both of you")
    mine = repo.add_message(
        db, chat["id"], "assistant", "Mira's first attempt.", turn=1, speaker_id=character.id
    )
    repo.add_message(
        db, chat["id"], "assistant", "Harrow answering her.", turn=1, speaker_id=harrow.id
    )

    assembled = assembly.build_reply_context(
        db, repo.get_chat(db, chat["id"]), character, config.SETTINGS,
        exclude_message_id=mine["id"],
    )
    sent = "\n".join(m["content"] for m in assembled.messages)
    assert "Mira's first attempt." not in sent
    assert "Harrow answering her." not in sent


def test_a_two_reply_turn_only_summarises_it_once(client):
    """A turn answered by two characters is still one turn. The passes that
    are about the conversation — the weather, the summary, whether the world
    intrudes — have one answer for it, and running them per reply would cost
    twice the tokens to say the same thing twice."""
    from app.db import get_db
    from app.passes.scheduler import CHAT_SCOPED_PASSES

    _, _, chat_id, replies = a_two_reply_turn(client)
    assert len(replies) == 2

    runs = get_db().query(
        "SELECT pass_id, COUNT(*) AS n FROM pass_runs WHERE chat_id=? AND turn=1 "
        "GROUP BY pass_id", (chat_id,)
    )
    twice = [r["pass_id"] for r in runs if r["n"] > 1 and r["pass_id"] in CHAT_SCOPED_PASSES]
    assert not twice, f"ran once per speaker instead of once per turn: {twice}"


def test_the_per_character_passes_do_run_for_each_speaker(client):
    """Each of them has their own mood, their own expression and their own
    memory of what just happened (§15 namespacing) — that is the whole reason
    namespacing came before group chats."""
    from app.db import get_db

    _, _, chat_id, replies = a_two_reply_turn(client)
    runs = get_db().query(
        "SELECT pass_id, COUNT(*) AS n FROM pass_runs WHERE chat_id=? AND turn=1 "
        "AND pass_id='basic' GROUP BY pass_id", (chat_id,)
    )
    assert runs and runs[0]["n"] == 2
