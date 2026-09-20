"""The way in to a group chat's own settings.

Every control for editing a group — mute someone, remove them, add someone
else, how readily each speaks, whose turn it is — already existed, and had
since roadmap 8. All of it sat in the Story panel under "Who is here", which
is three taps in (☰ → Story) and then a scroll past Quick options and the
whole toggle list. Reported as the feature being missing, which is what a
control nobody can find amounts to.

Roadmap 48's answer was a header button that opened Story and scrolled that
section under the thumb. It was still a panel opening, an animation, and a
landing somewhere in the middle of a long list of unrelated switches — and
there was nowhere to put the settings a group actually needed (how many
answer, self-replies, what they know about each other) that would not have
made the scroll longer. So the room moved: the controls live in a sheet of
their own now, the header button opens it directly, and Story carries a
button into the same sheet rather than a second copy of it.

Source-level checks, the same shape as tests/test_stop_button.py — there is
no JS harness here, and what is being protected is structural.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = (REPO / "static/app.js").read_text()
INDEX = (REPO / "static/index.html").read_text()
CSS = (REPO / "static/styles.css").read_text()

_METHOD = re.compile(r"^    (?:async )?([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)


def method(name: str) -> str:
    marks = [(m.group(1), m.start()) for m in _METHOD.finditer(APP_JS)]
    for i, (found, start) in enumerate(marks):
        if found != name:
            continue
        end = marks[i + 1][1] if i + 1 < len(marks) else len(APP_JS)
        return APP_JS[start:end]
    raise AssertionError(f"no method {name}() in static/app.js")


def cast_button() -> str:
    """The header button's own tag, opening brace to closing bracket."""
    start = INDEX.index('class="icon-btn cast-btn"')
    return INDEX[INDEX.rindex("<button", 0, start) : INDEX.index("</button>", start)]


def sheet() -> str:
    """The group sheet's markup, from its own modal down to its Done button."""
    start = INDEX.index('x-show="castOpen"')
    start = INDEX.rindex("<div", 0, start)
    return INDEX[start : INDEX.index('@click="castOpen = false">Done', start)]


# ------------------------------------------------------------------ the door


def test_the_header_carries_a_button_into_the_cast():
    assert "openCast()" in cast_button()


def test_it_only_shows_where_there_is_a_room_to_edit():
    """A solo chat has nothing here to change, and a button opening a list of
    one is worse than no button. The homepage has no chat at all."""
    show = re.search(r'x-show="([^"]+)"', cast_button()).group(1)
    assert "cast.length > 1" in show
    assert "!showHome" in show


def test_it_is_cloaked_like_every_other_conditional_header_control():
    """Without x-cloak it flashes on for one frame before Alpine boots — on a
    homepage where there is no chat, which is the one place it must not be."""
    assert "x-cloak" in cast_button()


def test_the_badge_counts_who_can_actually_answer():
    """Not cast.length: the number that matters while reading a scene is how
    many of them are not muted."""
    assert "cast.filter(m => !m.muted).length" in cast_button()


def test_the_button_is_an_svg_glyph_not_an_emoji():
    assert '<use href="#i-people"/>' in cast_button()


def test_it_says_what_it_does():
    tag = cast_button()
    assert "aria-label" in tag and ":title=" in tag
    assert "castTitle()" in tag


# --------------------------------------------------------------- openCast()


def test_open_cast_opens_the_sheet_and_nothing_else():
    """No panel, no scroll, no animation to wait out — the controls are one
    tap away or they are not reachable."""
    body = method("openCast")
    assert "this.castOpen = true" in body
    assert "openPanel" not in body
    assert "scrollIntoView" not in body


def test_open_cast_fills_the_room_first():
    """Story is not necessarily where this was opened from any more, so the
    membership it draws cannot be assumed already loaded."""
    assert "loadCast()" in method("openCast")


def test_the_sheet_starts_closed():
    assert re.search(r"^    castOpen: false,", APP_JS, re.MULTILINE)


def test_the_sheet_closes_the_way_every_other_one_does():
    markup = sheet()
    assert "@keydown.escape.window=\"castOpen = false\"" in markup
    assert 'x-transition:enter="sheet-modal-enter"' in markup


def test_story_keeps_a_door_rather_than_a_second_copy():
    """One implementation. Someone who goes looking in Story, where it used
    to be, gets sent to the same sheet."""
    start = INDEX.index("Who is here</h3>")
    section = INDEX[start : INDEX.index("Unplanned things", start)]
    assert "openCast()" in section
    assert "toggleMuted(m)" not in section, "that control lives in the sheet"
    assert "setTalkativeness(m," not in section


# ------------------------------------------------------------------ the room
#
# The door is only worth anything if what it opens onto actually works.


def test_the_sheet_holds_every_per_member_control():
    markup = sheet()
    assert "toggleMuted(m)" in markup, "the on/off switch"
    assert "dropMember(m)" in markup, "removing someone"
    assert "addMember($event.target.value)" in markup, "adding someone"
    assert "charactersNotHere()" in markup, "who there is left to add"
    assert "setTalkativeness(m," in markup


def test_the_sheet_holds_every_group_setting():
    """The four that decide how a group reads, all in the same place: nothing
    here should send anyone back to a different panel."""
    markup = sheet()
    assert "setPolicy(p.id)" in markup, "whose turn it is"
    assert "setReplies($event.target.value)" in markup, "how many answer"
    assert "setSelfResponses(" in markup, "following their own line"
    assert "setCastDetail(d.id)" in markup, "what they know about each other"


def test_every_setting_explains_itself():
    markup = sheet()
    assert "policyNote()" in markup
    assert "castDetailNote()" in markup
    assert "repliesLabel()" in markup


def test_the_last_person_keeps_their_remove_button_hidden():
    """The server refuses to empty a chat; the UI should not offer it either."""
    markup = sheet()
    remove = markup[markup.index("dropMember(m)") - 200 : markup.index("dropMember(m)") + 300]
    assert 'x-show="cast.length > 1"' in remove


def test_the_choices_that_mean_nothing_under_manual_are_hidden():
    """"You choose" answers "how many" and "can they follow themselves" by
    being what it is, so offering both would be offering a control that does
    nothing."""
    markup = sheet()
    assert markup.count("policy !== 'manual'") >= 2


def test_the_sheet_uses_the_icon_sprite_not_emoji():
    markup = sheet()
    assert "<use href=" in markup
    assert not re.search(r"[\U0001F300-\U0001FAFF]", markup)


# ------------------------------------------------------------ the settings


def test_one_writer_for_all_four_settings():
    """Four optimistic switches with four copies of the revert logic is four
    chances to forget one."""
    for name in ("setPolicy", "setCastDetail", "setSelfResponses", "setReplies"):
        assert "saveGroup(" in method(name), name
    body = method("saveGroup")
    assert "/group`" in body
    assert "applyGroupSettings(previous)" in body, "puts itself back on refusal"


def test_the_room_reads_every_setting_back():
    """A panel that read three of them and forgot the fourth is how a control
    silently stops working."""
    body = method("applyGroupSettings")
    for key in ("policy", "replies_per_turn", "self_responses", "cast_detail"):
        assert key in body, key


def test_the_replies_slider_is_debounced_like_the_other_one():
    """A dragged slider would otherwise write once per step."""
    assert "PREVIEW_DEBOUNCE_MS" in method("setReplies")


# ------------------------------------------------------------------- styling


def test_the_badge_sits_on_the_button():
    assert ".cast-btn { position: relative; flex: none; }" in CSS
    assert ".cast-count {" in CSS


def test_the_badge_reads_against_the_accent_it_sits_on():
    badge = CSS[CSS.index(".cast-count {") : CSS.index("}", CSS.index(".cast-count {"))]
    assert "background: var(--accent);" in badge
    assert "color: var(--on-accent);" in badge
