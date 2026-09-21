"""The corner button's three jobs, and the room carrying on without you.

Two reports, one composer. An empty message box in a group chat left the send
button greyed out, which is the wrong answer to "I want to hear what they say
to each other" — the right one is to hand the next line to the room. And the
"Next turn" row sat over the text box permanently, which reads as a decision
waiting to be made on every single turn when it is really a one-turn
override.

Source-level, the same shape as tests/test_cast_door.py: what is protected
here is that the icon, the label, the enabled state and what the tap actually
does all still agree with each other. They are four bindings on one element,
and the bug being fixed was precisely them disagreeing.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = (REPO / "static/app.js").read_text()
INDEX = (REPO / "static/index.html").read_text()
CSS = (REPO / "static/styles.css").read_text()

_METHOD = re.compile(r"^    (?:async )?(?:get )?([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)


def method(name: str) -> str:
    marks = [(m.group(1), m.start()) for m in _METHOD.finditer(APP_JS)]
    for i, (found, start) in enumerate(marks):
        if found != name:
            continue
        end = marks[i + 1][1] if i + 1 < len(marks) else len(APP_JS)
        return APP_JS[start:end]
    raise AssertionError(f"no method {name}() in static/app.js")


def send_button() -> str:
    start = INDEX.index('<button class="send"')
    return INDEX[start : INDEX.index("</button>", start)]


# ------------------------------------------------------------ the button


def test_the_button_has_one_place_that_decides_what_it_is():
    """Icon, label, class and tap all read the same thing. A disabled send
    that is really a working "carry on" is exactly the disagreement between
    them that was reported."""
    body = method("sendMode")
    assert '"stop"' in body and '"proceed"' in body and '"send"' in body


def test_an_empty_box_in_a_room_of_several_is_an_invitation_not_a_dead_end():
    body = method("sendMode")
    assert "this.draft.trim()" in body and "this.canProceed" in body
    assert "cast.length > 1" in method("canProceed")


def test_an_empty_box_in_a_solo_chat_is_still_dead():
    """There the tap really would do nothing — the character has nobody to be
    talking to but you, and Continue and Regenerate already cover a second
    reply to your last message better."""
    body = method("sendDisabled")
    assert 'this.sendMode === "send"' in body
    assert "!this.draft.trim()" in body and "!this.staged.length" in body


def test_a_staged_file_with_no_words_is_still_a_message():
    """"Look at this" with a picture and nothing typed has always been a real
    message, and must not become a "carry on"."""
    assert "!this.staged.length" in method("sendMode")


def test_the_button_draws_all_three_states():
    markup = send_button()
    assert "#i-send" in markup and "#i-continue" in markup and "#i-stop" in markup
    assert 'x-show="sendMode === \'proceed\'"' in markup
    assert ":disabled=\"sendDisabled\"" in markup


def test_the_button_says_which_one_it_is():
    markup = send_button()
    assert "sendLabel()" in markup
    body = method("sendLabel")
    assert "Stop generating" in body and "Let them carry on" in body and "Send" in body


def test_carrying_on_looks_different_from_sending():
    """The loud accent belongs to the thing you wrote; this is the room
    speaking without you."""
    rule = CSS[CSS.index(".send.carryon {") :]
    rule = rule[: rule.index("}") + 1]
    assert "background: transparent;" in rule
    assert "var(--accent)" in rule


def test_an_empty_send_routes_to_carrying_on():
    body = method("send")
    assert "return this.proceed();" in body
    # Before the early return that turns an empty composer away, or it never
    # gets there.
    assert body.index("this.proceed()") < body.index("if ((!text && !files.length)")


def test_carrying_on_asks_the_same_question_manual_asks_of_a_send():
    """Under "you choose" the policy is that you choose, and that does not
    stop being true because there is nothing typed."""
    assert 'this.policy === "manual"' in method("proceed")


def test_a_carry_on_that_never_landed_gives_the_pick_back():
    body = method("proceed")
    assert "const speaker = this.nextSpeaker;" in body
    assert "if (!went) this.nextSpeaker = speaker;" in body


def test_it_goes_through_the_same_stream_everything_else_does():
    """Same events, same pacing, same stop button — it is a turn, not a
    special case."""
    body = method("proceed")
    assert "this.runStream(" in body and "/proceed`" in body


# -------------------------------------------------------- the + menu's door


def test_the_menu_offers_both_new_things_only_where_they_exist():
    body = method("composerActions")
    assert '"Who answers next"' in body or "Who answers next" in body
    assert "Let them carry on" in body
    assert "hidden: !this.canProceed" in body


def test_a_hidden_action_is_removed_rather_than_greyed_out():
    """A greyed-out row is a promise that it will work later, and in a solo
    chat neither of these ever will."""
    assert ".filter((a) => !a.hidden)" in method("composerActions")


def test_the_drag_gesture_counts_the_list_the_menu_draws():
    """onPlusMove finds the item under the finger by its position among the
    rendered rows, and onPlusUp looks that index up in composerActions() — so
    the filtering has to happen inside composerActions(), not in the template."""
    assert 'x-for="(a, i) in composerActions()"' in INDEX
    assert "composerActions()[index]" in method("onPlusUp")
