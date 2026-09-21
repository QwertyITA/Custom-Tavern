"""Which face and which name a transcript draws, and the one-turn speaker pick.

Two reports, one root: the chat's *membership* was being asked a question it
does not answer. Membership is who can speak next; a transcript needs to know
who already did, and the two stop agreeing the moment somebody is removed from
a group. Reading the cast for a face meant a departed member's lines quietly
borrowed the chat character's picture and lost their name — and with the group
back down to one person, so did every line in it.

The same confusion is why there was no way to say "let them answer this one":
the server has always honoured a named speaker under any policy
(`groups.choose_speaker`'s `forced`), but the only control for it was bound to
the "you choose" policy, so using it once meant switching the whole chat over.

Source-level checks, the same shape as tests/test_stop_button.py — there is no
JS harness here, and what is protected is structural.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = (REPO / "static/app.js").read_text()
INDEX = (REPO / "static/index.html").read_text()

_METHOD = re.compile(r"^    (?:async )?(?:get )?([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)


def composer_actions() -> list[str]:
    """Each entry of composerActions(), split on its own `id:` line."""
    body = method("composerActions")
    return ["id:" + chunk for chunk in body.split("id:")[1:]]


def method(name: str) -> str:
    marks = [(m.group(1), m.start()) for m in _METHOD.finditer(APP_JS)]
    for i, (found, start) in enumerate(marks):
        if found != name:
            continue
        end = marks[i + 1][1] if i + 1 < len(marks) else len(APP_JS)
        return APP_JS[start:end]
    raise AssertionError(f"no method {name}() in static/app.js")


# --------------------------------------------------------------- the lookup


def test_the_face_lookup_asks_who_spoke_not_who_is_here():
    body = method("portraitFor")
    assert "voiceFor(" in body
    assert "this.cast" not in body, "the cast is who can speak next, not who did"


def test_the_frame_and_the_colour_ask_the_same_question():
    for name in ("portraitShape", "portraitEffect"):
        body = method(name)
        assert "voiceFor(" in body, name
        assert "this.cast" not in body, name


def test_the_speaker_label_asks_the_same_question():
    body = method("speakerName")
    assert "voiceFor(" in body
    assert "this.cast" not in body


def test_the_lookup_reads_an_index_that_covers_both():
    body = method("voiceFor")
    assert "this.voiceIndex" in body


def test_an_unknown_speaker_draws_nothing_rather_than_someone_else():
    """A card deleted outright has no face left to find. The blank placeholder
    is the honest answer; the chat character's picture is a lie."""
    body = method("portraitFor")
    assert 'return who ? pfpUrl(who.pfp || "") : "";' in body


def test_a_recorded_speaker_never_falls_back_to_the_chat_character():
    """Only a message with no speaker at all — the greeting, and anything from
    before speaker ids existed — is the chat's own character by definition."""
    body = method("_ownPortrait")
    assert "return !message.speaker_id;" in body


def test_one_face_in_the_chat_still_gets_the_expression_slice():
    """The expression is per chat, so it can only mean anything where there is
    exactly one face for it to mean it about."""
    body = method("_ownPortrait")
    assert "!this.manyVoices" in body
    assert "this.character.id" in body


def test_many_voices_counts_everyone_who_has_spoken_not_the_room():
    body = method("manyVoices")
    assert "this.voices" in body and "this.cast" in body
    assert "ids.size > 1" in body


def test_the_talking_clip_is_off_wherever_there_is_more_than_one_face():
    assert "if (this.manyVoices) return \"\";" in method("liveVideoFor")


def test_the_name_label_shows_on_voices_not_on_membership():
    row = re.search(r'<span class="said-by"[^>]*>', INDEX).group(0)
    assert "manyVoices" in row
    assert "cast.length" not in row


# ------------------------------------------------------------ the index feed


def test_every_members_answer_goes_through_one_place():
    """Adding somebody has to reach the index too: they have no lines yet, so
    the served `voices` does not carry them, and their first reply would
    otherwise draw a blank."""
    assert APP_JS.count("this.cast = body.members") == 1, "only applyMembers assigns it"
    assert "this.cast = body.members" in method("applyMembers")
    for name in ("loadCast", "addMember", "dropMember"):
        assert "applyMembers(" in method(name), name


def test_the_index_prefers_the_row_that_knows_about_muting():
    body = method("applyMembers")
    assert "[...this.voices, ...this.cast]" in body


def test_an_answer_without_voices_keeps_the_ones_it_has():
    """add/remove answer with members only — they must not wipe the index."""
    assert "if (body.voices) this.voices = body.voices;" in method("applyMembers")


# ------------------------------------------------- "let them answer this one"


def who_row() -> str:
    start = INDEX.index('class="who-row"')
    return INDEX[INDEX.rindex("<div", 0, start) : INDEX.index("</div>", start)]


def test_the_speaker_pick_is_reachable_under_every_policy():
    """Roadmap 50: the server has always honoured a named speaker whatever the
    policy, but the control for it was bound to "you choose", so using it once
    meant switching the whole chat over. Still true now that the row hides
    itself — under "you choose" it is always up because the pick is required
    there, and under every other policy the + menu opens it."""
    show = re.search(r'x-show="([^"]+)"', who_row()).group(1).strip()
    assert show == "whoRowShown", show
    shown = method("whoRowShown")
    assert 'this.policy === "manual"' in shown and "this.whoRowOpen" in shown

    door = next(a for a in composer_actions() if "whoRowOpen = true" in a)
    assert 'this.policy === "manual"' in door, (
        "under that one policy the row is already up, so the menu item would "
        "open something that is not closed"
    )
    assert "cast.length <= 1" in door, "a solo chat has no question to answer"


def test_the_row_is_out_of_the_way_until_it_is_asked_for():
    """A row of names standing over the text box on every single turn reads as
    a decision waiting to be made, which is not what a one-turn override is."""
    assert re.search(r"^    whoRowOpen: false,", APP_JS, re.MULTILINE)


def test_picking_somebody_puts_the_row_away_again():
    body = method("pickNextSpeaker")
    assert 'if (this.policy !== "manual") this.whoRowOpen = false;' in body


def test_the_row_says_which_of_the_two_things_it_is():
    row = who_row()
    assert "who-row-label" in row
    assert "Who answers" in row and "Next turn" in row


def test_a_muted_member_is_not_offered():
    assert "cast.filter(c => !c.muted)" in who_row()


def test_picking_goes_through_the_method_that_says_what_happened():
    assert "pickNextSpeaker(m)" in who_row()


def test_the_pick_toggles_off():
    body = method("pickNextSpeaker")
    assert 'this.nextSpeaker = already ? "" : member.character_id;' in body


def test_the_pick_is_announced_only_where_it_is_an_override():
    """Under "you choose" picking is the policy and a toast on every single
    turn is noise; under the others a highlighted name could just as easily
    read as "from now on"."""
    body = method("pickNextSpeaker")
    assert 'if (this.policy === "manual") return;' in body
    assert "flashHint" in body


def test_the_pick_is_spent_when_the_message_goes():
    """One turn only — it must never quietly become a policy."""
    body = method("send")
    assert "const speaker = this.nextSpeaker;" in body
    assert 'this.nextSpeaker = "";' in body
    assert "speaker_id: speaker" in body


def test_a_send_that_never_landed_gives_the_pick_back():
    assert "this.nextSpeaker = speaker;" in method("send")


# ----------------------------------------------------------- the streaming row


def test_the_placeholder_bubble_knows_who_is_answering():
    """The server has always sent turn_start's speaker id for exactly this;
    only the name was ever read, so in a group the reply streamed under the
    chat's nominal character's face and swapped when it finished."""
    body = method("runStream")
    assert 'let replySpeaker = "";' in body
    assert 'replySpeaker = event.speaker.id || "";' in body
    assert "speaker_id: replySpeaker," in body


def test_both_the_first_turn_and_a_retry_set_it():
    body = method("runStream")
    assert body.count('if (event.speaker) replySpeaker = event.speaker.id || "";') == 2


# ------------------------------------------------------------------- the cue
#
# Reported live: "X is typing…" under Y's picture, and then no telling which
# of the two was actually going to answer. The label had followed the real
# speaker since roadmap 49; the face beside it had not, because the cue is
# the one assistant row on screen with no message behind it to ask.


def cue_row() -> str:
    """The composing cue's own <article>, portrait slot included."""
    start = INDEX.index('x-show="composing && !regenId"')
    start = INDEX.rindex("<article", 0, start)
    return INDEX[start : INDEX.index("</article>", start)]


def test_the_cue_draws_the_face_of_whoever_is_about_to_answer():
    markup = cue_row()
    assert "portraitFor(cueRow)" in markup
    assert 'x-if="portrait &&' not in markup, "that is the chat's nominal character"


def test_the_cues_frame_and_colour_follow_the_same_speaker():
    """Two members of one group can be framed and tinted differently, and the
    cue has to wear the right one of each — not the chat character's."""
    markup = cue_row()
    assert "portraitShape(cueRow)" in markup
    assert "portraitEffect(cueRow)" in markup
    assert "character.pfp_effect" not in markup


def test_the_cue_resolves_its_face_the_same_way_every_other_row_does():
    """One implementation of "whose face is this", so the cue and the reply
    that replaces it cannot disagree."""
    body = method("cueRow")
    assert 'role: "assistant"' in body
    assert "speaker_id: this.composingSpeakerId" in body


def test_the_speakers_id_is_tracked_wherever_their_name_is():
    """A name is not something a portrait can be looked up by, and the two
    going out of step is exactly the reported bug."""
    body = method("runStream")
    assert body.count("this.composingSpeaker = ") == body.count("this.composingSpeakerId = ")
    assert re.search(r"^    composingSpeakerId: \"\",", APP_JS, re.MULTILINE)


def test_every_speaker_of_a_turn_updates_the_cue_not_just_the_first():
    """A turn can be answered by several characters in order (§ groups.plan),
    and each of them gets the cue before their own bubble opens."""
    body = method("runStream")
    assert 'case "speaker_start":' in body
    start = body.index('case "speaker_start":')
    block = body[start : body.index("break;", start)]
    assert "this.composingSpeaker = " in block
    assert "this.composingSpeakerId = " in block


def test_a_solo_chat_still_wears_its_expression():
    """An empty speaker has to resolve the way it always did — the chat's own
    character, with whatever face the expression pass last chose — or every
    one-character chat loses its expressions on the cue."""
    body = method("portraitFor")
    assert "this._ownPortrait(message)" in body and "return this.portrait" in body
    assert "return !message.speaker_id;" in method("_ownPortrait")


# ------------------------------------------------------------- the composer bar

CSS = (REPO / "static/styles.css").read_text()


def test_the_composer_wraps_its_full_width_rows():
    """Two of its children are rows, not controls: the staged attachments and
    the speaker pick. Without a wrap they were squeezed onto the text box's own
    line, leaving it about wide enough for one letter."""
    block = CSS[CSS.index(".composer {") :]
    block = block[: block.index("}") + 1]
    assert "flex-wrap: wrap;" in block


def test_both_rows_claim_a_whole_line():
    for selector in (".staged {", ".who-row {"):
        block = CSS[CSS.index(selector) :]
        block = block[: block.index("}") + 1]
        assert "width: 100%;" in block, selector


def test_the_text_box_cannot_be_pushed_onto_a_line_of_its_own():
    block = CSS[CSS.index(".composer textarea {") :]
    block = block[: block.index("}") + 1]
    assert "min-width: 0;" in block
