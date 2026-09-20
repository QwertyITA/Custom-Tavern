"""What a group chat actually sends the model.

The complaint this was written against was "the AI doesn't understand a
conversation with several characters in it", and the reason was structural
rather than a matter of prompting harder: every character's reply went into
the prompt as an unlabelled `assistant` turn. A four-way conversation
reached the model as one undivided voice, so it answered as one undivided
voice — mixing the cast into a single person, answering a question that had
been put to somebody else, contradicting a line it had itself written two
messages earlier.

SillyTavern labels every message with who said it, for exactly this reason
(`formatMessageHistoryItem`, and the `names_behavior` DEFAULT branch in
openai.js, which prefixes `name: ` whenever a group is selected). So does
this now. The rest of the file guards the things that labelling then makes
necessary: the others' descriptions in the prompt so a speaker knows who
they are talking to, a last-read instruction that the reply is one person's,
stop sequences at somebody else's label, and cleaning a label back off a
reply that wrote one anyway.
"""

from __future__ import annotations

from app import assembly, config, groups, repo
from app.models import Character, Sampling
from app.passes.scheduler import _cast_names, _with_character_stops
from app.postprocess import clean_reply


def a_room(db, chat, *names) -> list[Character]:
    made = []
    for name in names:
        card = Character(id=name.lower(), name=name, persona=f"{name} rows the ferry.")
        repo.save_character(db, card)
        groups.add_member(db, chat["id"], card.id)
        made.append(card)
    return made


def built(db, chat, character, **kw):
    return assembly.build_reply_context(
        db, repo.get_chat(db, chat["id"]), character, config.SETTINGS, **kw
    )


def said(assembled) -> list[str]:
    return [m["content"] for m in assembled.messages if m["role"] in ("user", "assistant")]


# --------------------------------------------------------- who said what


def test_a_solo_chat_is_still_sent_exactly_as_it_was(db, chat, character):
    """No labels anywhere: a chat with one character in it has no ambiguity
    to resolve, and a prompt that changed shape for every existing chat would
    be a rewrite of every existing chat's behaviour."""
    repo.add_message(db, chat["id"], "user", "hello")
    repo.add_message(db, chat["id"], "assistant", "Hello yourself.", speaker_id=character.id)
    assert said(built(db, chat, character)) == ["hello", "Hello yourself."]


def test_every_line_in_a_group_says_who_said_it(db, chat, character):
    harrow, = a_room(db, chat, "Harrow")
    repo.add_message(db, chat["id"], "assistant", "You're late.", speaker_id=character.id)
    repo.add_message(db, chat["id"], "user", "Sorry.")
    repo.add_message(db, chat["id"], "assistant", "Not in this wind.", speaker_id=harrow.id)

    assert said(built(db, chat, character)) == [
        "Mira: You're late.",
        "You: Sorry.",
        "Harrow: Not in this wind.",
    ]


def test_your_own_lines_are_labelled_with_your_persona(db, chat, character):
    a_room(db, chat, "Harrow")
    repo.save_persona(db, {"id": "p", "name": "Casimir", "description": "A traveller."})
    repo.set_chat_persona(db, chat["id"], "p")
    repo.add_message(db, chat["id"], "user", "Evening.")
    assert said(built(db, chat, character)) == ["Casimir: Evening."]


def test_a_line_from_somebody_who_has_left_keeps_their_name(db, chat, character):
    """Membership answers who can speak next; a transcript needs who already
    did (§ groups.voices). Relabelling a departed character's lines with
    whoever is left would be a worse lie than leaving them unlabelled."""
    harrow, = a_room(db, chat, "Harrow")
    repo.add_message(db, chat["id"], "assistant", "Not in this wind.", speaker_id=harrow.id)
    groups.remove_member(db, chat["id"], harrow.id)
    assert said(built(db, chat, character)) == ["Harrow: Not in this wind."]


def test_the_labels_are_counted_as_part_of_what_is_sent(db, chat, character):
    """A token or two per message is a section of the prompt by the time a
    chat is long, and the eviction ladder decides what to throw away from
    this number."""
    a_room(db, chat, "Harrow")
    for i in range(6):
        repo.add_message(db, chat["id"], "user", f"message {i}")
    assembled = built(db, chat, character)
    bare = sum(len(m["text"]) for m in repo.list_messages(db, chat["id"]))
    assert assembled.sections["verbatim"] > bare / 4


# ------------------------------------------------ who the others actually are


def test_the_others_arrive_with_a_description_not_just_a_name(db, chat, character):
    """A character told only that "Harrow" is present writes Harrow as
    whatever the name sounds like, and contradicts his card two lines later.
    SillyTavern's join-cards mode exists for this."""
    a_room(db, chat, "Harrow")
    assert "Harrow rows the ferry." in built(db, chat, character).system


def test_just_their_names_is_still_an_option(db, chat, character):
    a_room(db, chat, "Harrow")
    repo.update_chat_settings(db, chat["id"], {"cast_detail": "names"})
    system = built(db, chat, character).system
    assert "**Harrow**" in system
    assert "rows the ferry" not in system


def test_a_long_card_is_cut_at_a_sentence_for_the_short_version(db, chat, character):
    long_card = Character(
        id="anna", name="Anna",
        persona="She keeps the light. " + "A sentence about her. " * 60,
    )
    repo.save_character(db, long_card)
    groups.add_member(db, chat["id"], long_card.id)
    brief = groups.profile_for(long_card, "brief")
    assert len(brief) <= groups.BRIEF_CHARS
    assert brief.startswith("She keeps the light.")
    assert not brief.endswith("A sent")


def test_their_whole_card_is_an_option_too(db, chat, character):
    long_card = Character(id="anna", name="Anna", persona="x" * 900)
    repo.save_character(db, long_card)
    assert len(groups.profile_for(long_card, "full")) == 900


def test_a_muted_character_is_still_described(db, chat, character):
    """Someone standing there saying nothing is still in the scene."""
    harrow, = a_room(db, chat, "Harrow")
    groups.update_member(db, chat["id"], harrow.id, muted=True)
    assert "Harrow rows the ferry." in built(db, chat, character).system


# ------------------------------------------------------------- whose line


def test_the_last_thing_read_is_whose_line_this_is(db, chat, character):
    a_room(db, chat, "Harrow")
    volatile = built(db, chat, character).volatile
    assert "## Your turn" in volatile
    assert "You are Mira" in volatile
    assert "Harrow" in volatile


def test_a_solo_chat_has_no_turn_note_at_all(db, chat, character):
    assert "## Your turn" not in built(db, chat, character).volatile


def test_the_turn_note_sits_after_the_story_and_before_the_cards_last_word(db, chat, character):
    """Recency is the whole point: a rule about staying in one voice, read
    before three hundred tokens of house style, is a rule the model has
    stopped thinking about by the time it answers."""
    a_room(db, chat, "Harrow")
    ids = [p["id"] for p in built(db, chat, character).parts if p["band"] == "volatile"]
    assert ids[-1] == "turn" or ids[ids.index("turn") + 1] == "final"


# ---------------------------------------------- keeping a reply one person's


def test_somebody_elses_label_ends_the_reply(db, chat, character):
    """The cheap half of refusing the invitation a labelled transcript makes;
    clean_reply below is the half that works when a backend ignores stops."""
    a_room(db, chat, "Harrow")
    sampling = _with_character_stops(
        Sampling(), character, _cast_names(db, repo.get_chat(db, chat["id"]), character)
    )
    assert "\nHarrow:" in sampling.stop


def test_the_stored_pass_definition_is_never_the_thing_that_grows_stops(db, chat, character):
    """`definition.sampling` is shared across every chat, so appending to it
    would leak one room's names into everybody else's replies."""
    a_room(db, chat, "Harrow")
    shared = Sampling()
    _with_character_stops(
        shared, character, _cast_names(db, repo.get_chat(db, chat["id"]), character)
    )
    assert shared.stop == Sampling().stop


def test_the_cast_for_stops_leaves_the_speaker_themselves_out(db, chat, character):
    a_room(db, chat, "Harrow")
    names = _cast_names(db, repo.get_chat(db, chat["id"]), character)
    assert "Harrow" in names and "Mira" not in names


def test_a_solo_chat_gains_no_stop_sequences(db, chat, character):
    assert _cast_names(db, repo.get_chat(db, chat["id"]), character) == ()


def test_a_reply_that_labels_itself_has_the_label_taken_off():
    """The bubble already carries the name and the portrait. The label
    arriving a second time reads as a script rather than as a room."""
    assert clean_reply("Mira: She looks up.", speaker="Mira") == "She looks up."
    assert clean_reply("**Mira:** She looks up.", speaker="Mira") == "She looks up."
    assert clean_reply("**Mira**: She looks up.", speaker="Mira") == "She looks up."


def test_taking_the_label_off_does_not_eat_the_emphasis_after_it():
    """The first version matched a trailing `**` that belonged to the line,
    and turned `**Mira:** *She looks up.*` into `** *She looks up.*`."""
    assert clean_reply(
        "**Mira:** *She looks up.*", speaker="Mira"
    ) == "*She looks up.*"


def test_a_reply_that_carried_on_into_somebody_elses_line_is_cut_there():
    assert clean_reply(
        "She looks up.\nHarrow: And he does not.",
        speaker="Mira", cast_names=("Harrow",),
    ) == "She looks up."


def test_a_name_inside_a_sentence_is_left_alone():
    """Only a line that *starts* as somebody else's turn is one. Deleting
    every mention of a name would eat the dialogue about them."""
    text = "She looks up. Harrow: he said that too, apparently."
    assert clean_reply(text, speaker="Mira", cast_names=("Harrow",)) == text


# ------------------------------------------------ the line with no owner


def test_the_greeting_carries_the_speaker_who_said_it(client):
    """It was stored blank, which nothing noticed in a solo chat — the
    fallback everything has is the chat's own character, and in a solo chat
    that is always right. In a group it is the first thing said and the only
    line with no owner."""
    character_id = client.get("/api/characters").json()[0]["id"]
    chat_id = client.post("/api/chats", json={"character_id": character_id}).json()["id"]
    greeting = client.get(f"/api/chats/{chat_id}/messages").json()[0]
    assert greeting["turn"] == 0
    assert greeting["speaker_id"] == character_id


def test_a_line_with_no_speaker_recorded_is_not_given_the_current_one(db, chat, character):
    """A message from before `speaker_id` existed belongs to the chat's own
    character. Labelling it with whoever is answering now would put their name
    on somebody else's line — and in a group, "whoever is answering now" is a
    different person on every reply of the same turn."""
    harrow, = a_room(db, chat, "Harrow")
    repo.add_message(db, chat["id"], "assistant", "From before the column existed.")
    assert said(built(db, chat, harrow)) == ["Mira: From before the column existed."]
