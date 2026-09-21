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
    repo.update_chat_settings(db, chat["id"], {"cards": "swap"})
    assert "Harrow rows the ferry." in built(db, chat, character).system


def test_just_their_names_is_still_an_option(db, chat, character):
    a_room(db, chat, "Harrow")
    repo.update_chat_settings(db, chat["id"], {"cards": "swap", "cast_detail": "names"})
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
    repo.update_chat_settings(db, chat["id"], {"cards": "swap"})
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


# ------------------------------------------------ the prompt's own first token
#
# Asked live, and correctly: "does the group chat change the prompt at the
# very beginning? I feel like it re-caches everything then answers."
#
# It did. The prompt is rebuilt for whoever is speaking, and it opened with
# "You are Mira" — so Mira's prompt and Harrow's prompt shared eight
# characters out of nine thousand, and a backend whose KV cache is a prefix
# match could reuse none of it. Two characters taking turns meant re-reading
# the whole prompt, card, writing blocks and transcript, on every reply.
#
# SillyTavern's answer is its APPEND generation mode: every member's cards
# are joined in member order, so the prompt comes out identical whoever is
# about to speak, and the only thing naming the speaker is at the very end.


def whole(assembled) -> str:
    return assembled.system + "\n".join(m["content"] for m in assembled.messages)


def shared_head(a: str, b: str) -> int:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return min(len(a), len(b))


def a_room_with_history(db, chat, character):
    harrow, = a_room(db, chat, "Harrow")
    repo.add_message(db, chat["id"], "assistant", "You're late.", turn=0, speaker_id=character.id)
    for i in range(1, 12):
        repo.add_message(
            db, chat["id"], "user" if i % 2 else "assistant", f"a line, number {i}. " * 6,
            turn=i, speaker_id="" if i % 2 else character.id,
        )
    return harrow


def test_a_joined_room_sends_the_same_prompt_whoever_speaks(db, chat, character):
    harrow = a_room_with_history(db, chat, character)
    mine, theirs = whole(built(db, chat, character)), whole(built(db, chat, harrow))
    # Everything but the volatile tail, which is where whose turn it is lives.
    assert shared_head(mine, theirs) > len(mine) * 0.85, (
        f"only {shared_head(mine, theirs)} of {len(mine)} characters shared"
    )


def test_swapping_is_still_there_and_still_diverges_at_the_name(db, chat, character):
    """Kept, because which way is better depends on the backend: a joined room
    sends every card every turn, and on the Horde — where each reply lands on a
    different worker and no cache survives — that is simply a bigger prompt."""
    harrow = a_room_with_history(db, chat, character)
    repo.update_chat_settings(db, chat["id"], {"cards": "swap"})
    mine, theirs = whole(built(db, chat, character)), whole(built(db, chat, harrow))
    assert shared_head(mine, theirs) < 100


def test_the_joined_instruction_names_nobody(db, chat, character):
    """It is the first thing in the prompt. The moment it says a name, every
    token behind it belongs to that character."""
    a_room_with_history(db, chat, character)
    system = built(db, chat, character).system
    opening = system[: system.index("##")] if "##" in system else system
    assert "You are Mira" not in opening
    assert "Mira" in system and "Harrow" in system


def test_whose_turn_it_is_is_still_said_last(db, chat, character):
    """Which is what makes naming nobody at the top safe — and is where
    SillyTavern puts it too, as the trailing `Name:` that primes the reply."""
    harrow = a_room_with_history(db, chat, character)
    assert "You are Mira" in built(db, chat, character).volatile
    assert "You are Harrow" in built(db, chat, harrow).volatile


def test_a_joined_room_describes_everyone_including_the_speaker(db, chat, character):
    a_room_with_history(db, chat, character)
    system = built(db, chat, character).system
    assert "### Mira" in system and "### Harrow" in system
    assert "Harrow rows the ferry." in system


def test_a_joined_room_has_no_second_list_of_who_else_is_here(db, chat, character):
    """Everyone is described in full above, so a cast note would be the same
    names twice — and the one block left in the prefix that still differed by
    speaker."""
    a_room_with_history(db, chat, character)
    assert "## Also here" not in built(db, chat, character).system


def test_the_join_order_is_the_room_not_the_speaker(db, chat, character):
    """Join order, not speaking order: the block has to come out identical
    whoever is about to speak, or the prefix diverges again."""
    harrow = a_room_with_history(db, chat, character)
    mine = built(db, chat, character).system
    theirs = built(db, chat, harrow).system
    assert mine[mine.index("### "):] .split("### ")[1][:5] == theirs[theirs.index("### "):].split("### ")[1][:5]


def test_one_scenario_shared_by_the_room_is_not_paid_for_twice(db, chat, character):
    """SillyTavern repeats it once per member; on a phone that is the same
    paragraph in the prompt as many times as there are people in the room."""
    for card in (character, repo.get_character(db, "harrow")):
        pass
    harrow = a_room_with_history(db, chat, character)
    for who in (character.id, harrow.id):
        card = repo.get_character(db, who)
        card.scenario = "A tavern on the coast road."
        repo.save_character(db, card)
    system = built(db, chat, character).system
    assert system.count("A tavern on the coast road.") == 1
    assert "### Mira, Harrow" in system


def test_a_solo_chat_joins_nothing(db, chat, character):
    """A room of one has nothing to join, and its prompt was already stable
    across turns — so it must come out exactly as it always did."""
    repo.add_message(db, chat["id"], "user", "hello")
    system = built(db, chat, character).system
    assert system.startswith("You are Mira.")
    assert "### Mira" not in system


def test_the_world_belongs_to_the_room_not_to_the_speaker(db, chat, character):
    """The last thing in the prefix still keyed to whoever was talking. One
    member carrying a constant lorebook entry and another not was enough to
    split the prompt in two again a hundred tokens in."""
    from app.models import LorebookEntry

    harrow = a_room_with_history(db, chat, character)
    card = repo.get_character(db, harrow.id)
    card.lorebook = [LorebookEntry(keys=["ferry"], content="The ferry runs at dawn.",
                                   constant=True, enabled=True)]
    repo.save_character(db, card)

    mine = built(db, chat, character).system
    theirs = built(db, chat, repo.get_character(db, harrow.id)).system
    assert "The ferry runs at dawn." in mine, "the speaker never reads the room's world"
    assert mine == theirs


def test_a_swapped_room_keeps_the_speakers_own_world(db, chat, character):
    from app.models import LorebookEntry

    harrow = a_room_with_history(db, chat, character)
    repo.update_chat_settings(db, chat["id"], {"cards": "swap"})
    card = repo.get_character(db, harrow.id)
    card.lorebook = [LorebookEntry(keys=["ferry"], content="The ferry runs at dawn.",
                                   constant=True, enabled=True)]
    repo.save_character(db, card)
    # The freshly stored card, not the stale object a_room handed back.
    harrow = repo.get_character(db, harrow.id)
    assert "The ferry runs at dawn." not in built(db, chat, character).system
    assert "The ferry runs at dawn." in built(db, chat, harrow).system
