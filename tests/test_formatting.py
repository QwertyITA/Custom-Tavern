"""What the model gets wrong about markup, and what is done about it (§8, §13).

Every case here is taken from one real chat: a q4 flash model over Ollama,
thirteen turns, forty-seven variants. Half the replies on screen carried a
stray asterisk, two had the model's entire reasoning stored as the message,
and one printed its state payload.

The rule throughout is that the app repairs what it can prove is a mistake and
leaves alone what might be deliberate.
"""

from __future__ import annotations

import pytest

from app.markup import parse, repair_markup
from app.passes.contract import MARKER, split_state_suffix
from app.postprocess import ThinkStreamFilter, clean_reply, find_echoed_phrase, split_thinking


def strays(text: str) -> int:
    """Asterisks the tokenizer had to leave literal — what a reader sees."""
    return sum(run.text.count("*") for run in parse(text))


# ------------------------------------------------------- the stray asterisk

# The shape that dominates: an attribution is closed, then the narration after
# it is closed again, having never been opened.
DANGLING = '*she says through clenched teeth,* tail whipping once behind her leg.*'


def test_the_asterisk_that_closes_nothing_is_removed():
    assert strays(DANGLING) == 1
    repaired = repair_markup(DANGLING)
    assert strays(repaired) == 0
    assert repaired.endswith("behind her leg.")
    assert "*she says through clenched teeth,*" in repaired


@pytest.mark.parametrize(
    "text",
    [
        '"We were," *she confirms slowly,* then adds a sharp edge:* but yours now."',
        '*Kutra’s ears flatten.* "I can make them," *she says,* tail whipping.*',
        'unclosed *at the end of the line',
    ],
)
def test_no_reply_keeps_a_visible_asterisk(text):
    assert strays(repair_markup(text)) == 0


@pytest.mark.parametrize(
    "text",
    [
        "maths: 2 * 3 = 6",
        "a lone * standing between spaces",
        "*balanced action* and \"speech\"",
        "**emphasis** inside *an action*",
    ],
)
def test_an_asterisk_that_is_not_markup_is_left_alone(text):
    """The rule is the tokenizer's own: a marker that could have opened or
    closed something and found no partner is a mistake. One that could do
    neither was never markup."""
    assert repair_markup(text) == text


def test_the_repair_runs_on_every_stored_reply():
    assert strays(clean_reply(DANGLING)) == 0


# ------------------------------------------- reasoning with no opening tag

# Ollama and llama.cpp serve reasoning models whose chat template writes the
# opening <think> itself, so the model only ever emits the closer.
HEADLESS = (
    "thought_process:\n1. **Analyze Input**: she would refuse.\n"
    "</think>*Kutra’s ears snap backward.* \"Get away from us.\""
)


def test_a_closing_tag_with_no_opener_still_splits_the_reasoning_off():
    body, thinking = split_thinking(HEADLESS)
    assert "thought_process" not in body
    assert body.startswith("*Kutra’s ears snap backward.*")
    assert "thought_process" in thinking


def test_the_stream_takes_back_what_turned_out_to_be_reasoning():
    """It has already been sent by then — the reply looked like it had started
    — so the filter says so and the client is told to drop its copy."""
    watcher = ThinkStreamFilter()
    shown, thought = [], []
    for i in range(0, len(HEADLESS), 6):
        visible, reasoning = watcher.feed(HEADLESS[i : i + 6])
        shown.append(visible)
        thought.append(reasoning)
    visible, reasoning = watcher.finish()

    assert watcher.retracted is True
    assert "thought_process" in "".join(thought)
    # what the client keeps is only what arrived after the retraction
    assert "".join(shown[-3:]) + visible


def test_an_ordinary_reply_is_never_retracted():
    watcher = ThinkStreamFilter()
    for chunk in ("*She looks up.* ", '"Sit wherever." ', "*She turns away.*"):
        watcher.feed(chunk)
    watcher.finish()
    assert watcher.retracted is False


# ------------------------------------------------------ the mangled marker


@pytest.mark.parametrize(
    "suffix",
    [
        MARKER,
        " ***state>>>",          # seen in the wild; the payload was printed
        "\n\n** state **\n",
        "---state---",
    ],
)
def test_the_state_payload_is_found_however_the_marker_is_mangled(suffix):
    body, payload = split_state_suffix(
        f'*She looks up.* "Sit."{suffix}{{"deltas": {{"trust": 1}}}}'
    )
    assert payload == {"deltas": {"trust": 1}}
    assert clean_reply(body) == '*She looks up.* "Sit."'


@pytest.mark.parametrize(
    "text",
    [
        "a sentence about the state of the room",
        "the ***best*** state of affairs, she said",
        '*She looks up.* "The state of this place."',
    ],
)
def test_prose_about_a_state_is_not_mistaken_for_the_contract(text):
    body, payload = split_state_suffix(text)
    assert payload is None
    assert body == text


# ------------------------------------------------------------------- HTML


@pytest.mark.parametrize(
    "text,want",
    [
        ('*She looks up.*</b>', "*She looks up.*"),
        ('<img src="x.png" alt="y"> *She waits.*', "*She waits.*"),
        ("a < b and b > c", "a < b and b > c"),
        ("she counted 3 < 5 aloud", "she counted 3 < 5 aloud"),
    ],
)
def test_tags_this_app_cannot_render_are_taken_out(text, want):
    """Model output is drawn with textContent (§8), so a tag arrives on screen
    as its own source. A bare < in prose is not a tag and stays."""
    assert clean_reply(text) == want


# --------------------------------------------------- find_echoed_phrase


def test_finds_a_real_quoted_clause():
    """The measured shape (KNOWN-ISSUES.md): 1 of 47 real variants echoed a
    phrase from the message it was replying to."""
    user = "I still can't believe you forgot my birthday after everything I did for you."
    reply = '*She scoffs.* "You forgot my birthday after everything I did for you? Unbelievable."'
    found = find_echoed_phrase(reply, user)
    assert found == "you forgot my birthday after everything i did for you"


def test_ordinary_shared_phrasing_does_not_trip_it():
    user = "What do you mean by that? I don't understand."
    reply = '*She tilts her head.* "I don\'t know what you mean."'
    assert find_echoed_phrase(reply, user) == ""


def test_a_short_overlap_under_the_word_floor_does_not_trip_it():
    user = "Tell me about the harbourmaster and his debts."
    reply = "*He shrugs.* \"The harbourmaster owes half the town.\""
    assert find_echoed_phrase(reply, user, min_words=6) == ""


def test_no_overlap_at_all():
    assert find_echoed_phrase("The rain finally stops.", "Where were you last night?") == ""


def test_markup_around_the_echoed_words_does_not_break_the_match():
    user = "the old lighthouse keeper never once left his post"
    reply = '*narrows her eyes* "the old lighthouse keeper never once left his post."'
    assert find_echoed_phrase(reply, user) == "the old lighthouse keeper never once left his post"


def test_finds_the_longest_match_not_just_the_first():
    user = "a b c d e f g h and completely unrelated filler text here"
    reply = "z z z a b c d e f g h and something else"
    # Grows past the min_words floor to the full run both texts share.
    assert find_echoed_phrase(reply, user, min_words=6) == "a b c d e f g h and"


def test_empty_inputs_never_match():
    assert find_echoed_phrase("", "anything at all here really") == ""
    assert find_echoed_phrase("anything at all here really", "") == ""
