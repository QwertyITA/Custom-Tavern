"""The way in to a group chat's own settings.

Every control for editing a group — mute someone, remove them, add someone
else, how readily each speaks, whose turn it is — already existed, and had
since roadmap 8. All of it sat in the Story panel under "Who is here", which
is three taps in (☰ → Story) and then a scroll past Quick options and the
whole toggle list. Reported as the feature being missing, which is what a
control nobody can find amounts to.

So this guards the *door*, not the room: the header button that opens Story
and puts that section under the thumb, and the fact that the section it
lands on still holds the controls it promises. Source-level checks, the same
shape as tests/test_stop_button.py — there is no JS harness here, and what
is being protected is structural.
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


# ------------------------------------------------------------------ the door


def test_the_header_carries_a_button_into_the_cast():
    tag = cast_button()
    assert "openCast()" in tag


def test_it_only_shows_where_there_is_a_room_to_edit():
    """A solo chat has nothing here to change, and a button opening a list of
    one is worse than no button. The homepage has no chat at all."""
    tag = cast_button()
    show = re.search(r'x-show="([^"]+)"', tag).group(1)
    assert "cast.length > 1" in show
    assert "!showHome" in show


def test_it_is_cloaked_like_every_other_conditional_header_control():
    """Without x-cloak it flashes on for one frame before Alpine boots — on a
    homepage where there is no chat, which is the one place it must not be."""
    assert "x-cloak" in cast_button()


def test_the_badge_counts_who_can_actually_answer():
    """Not cast.length: the number that matters while reading a scene is how
    many of them are not muted."""
    tag = cast_button()
    assert "cast.filter(m => !m.muted).length" in tag


def test_the_button_is_an_svg_glyph_not_an_emoji():
    assert '<use href="#i-people"/>' in cast_button()


def test_it_says_what_it_does():
    tag = cast_button()
    assert "aria-label" in tag and ":title=" in tag
    assert "castTitle()" in tag


# --------------------------------------------------------------- openCast()


def test_open_cast_opens_story():
    assert 'openPanel("story")' in method("openCast")


def test_open_cast_does_not_close_an_already_open_story():
    """openPanel is a toggle — calling it with the panel already on `story`
    closes the very panel this is meant to reach."""
    body = method("openCast")
    assert 'this.panel !== "story"' in body
    assert "!this.panelOpen" in body


def test_open_cast_lands_on_the_section():
    body = method("openCast")
    assert "$refs.castSection" in body
    assert "scrollIntoView" in body


def test_open_cast_waits_for_the_panel_to_render():
    """The section does not exist in the DOM until `panel === 'story'` has
    rendered, so a scroll in the same tick finds nothing."""
    assert "$nextTick" in method("openCast")


def test_the_landing_mark_clears_itself():
    body = method("openCast")
    assert "this.castLanded = true" in body
    assert "this.castLanded = false" in body
    assert "clearTimeout(this._castLandTimer)" in body


def test_cast_landed_starts_off():
    assert re.search(r"^    castLanded: false,", APP_JS, re.MULTILINE)


def test_the_heading_is_the_scroll_target_and_wears_the_mark():
    heading = re.search(r"<h3[^>]*castSection[^>]*>", INDEX).group(0)
    assert 'x-ref="castSection"' in heading
    assert "castLanded" in heading
    assert "Who is here" in INDEX[INDEX.index(heading) : INDEX.index(heading) + 400]


# ------------------------------------------------------------------ the room
#
# The door is only worth anything if what it opens onto still works.


def test_the_section_still_holds_every_control_the_button_promises():
    start = INDEX.index('x-ref="castSection"')
    section = INDEX[start : INDEX.index("Whose turn it is", start)]
    assert "toggleMuted(m)" in section, "the on/off switch"
    assert "dropMember(m)" in section, "removing someone"
    assert "addMember($event.target.value)" in section, "adding someone"
    assert "charactersNotHere()" in section, "who there is left to add"
    assert "setTalkativeness(m," in section


def test_the_last_person_keeps_their_remove_button_hidden():
    """The server refuses to empty a chat; the UI should not offer it either."""
    start = INDEX.index('x-ref="castSection"')
    section = INDEX[start : INDEX.index("Whose turn it is", start)]
    remove = section[section.index("dropMember(m)") - 200 : section.index("dropMember(m)") + 300]
    assert 'x-show="cast.length > 1"' in remove


# ------------------------------------------------------------------- styling


def test_the_badge_sits_on_the_button():
    assert ".cast-btn { position: relative; flex: none; }" in CSS
    assert ".cast-count {" in CSS


def test_the_badge_reads_against_the_accent_it_sits_on():
    badge = CSS[CSS.index(".cast-count {") : CSS.index("}", CSS.index(".cast-count {"))]
    assert "background: var(--accent);" in badge
    assert "color: var(--on-accent);" in badge


def test_the_landing_mark_eases_both_ways_from_tokens():
    """A heading that snapped on and off would read as a glitch, and a bezier
    written inline would be the one curve nothing physical follows."""
    on = CSS[CSS.index(".sheet-body h3.landed {") :]
    on = on[: on.index("}") + 1]
    assert "var(--ease-out)" in on and "var(--dur-base)" in on
    assert "cubic-bezier" not in on
    off = ".sheet-body h3 { transition: color var(--dur-slow) var(--ease-in-out); }"
    assert off in CSS


def test_the_landing_mark_never_springs():
    """--ease-spring overshoots past 1 on purpose, and an overshoot on a
    colour extrapolates past the target and clamps per channel."""
    on = CSS[CSS.index(".sheet-body h3.landed {") :]
    on = on[: on.index("}") + 1]
    assert "--ease-spring" not in on


def test_a_scrolled_to_heading_keeps_its_breathing_room():
    """scrollIntoView lands the border box flush against the top of the
    scroller and ignores the margin above it — live, the heading arrived
    with its ascenders shaved against the sheet header's rule."""
    block = CSS[CSS.index(".sheet-body h3 {") :]
    block = block[: block.index("}") + 1]
    assert "scroll-margin-top" in block
