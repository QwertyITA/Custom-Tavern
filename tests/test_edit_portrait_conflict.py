"""Editing a message and enlarging its portrait both want the same row's
width, and they used to fight over it.

Reported live: "when editing a message and the pfp is expanded, things get a
bit messed up." Measured in a real browser: tapping a portrait to enlarge it
while that same message was being edited left the edit box rendered past the
right edge of the screen — 438px on a 412px viewport, clipped rather than
scrolled to.

The cause is two features pinning the same `.bubble` to two different
widths. `startEdit` (§ its own comment: "editing must not make the bubble
jump") reads the bubble's current rendered width and pins it there with an
inline `min-width`, so the edit box — `width: 100%` — does not collapse when
it replaces the message body. An enlarged portrait narrows that same bubble
by CSS (`.msg:has(.pfp-slot.big) .bubble { max-width: calc(var(--bubble-max)
- 130px) }`). A `min-width` wider than that narrowed `max-width` wins
outright per spec, so the bubble refuses to narrow, the portrait's 148px and
the still-full-width bubble no longer fit the row between them, and the row
overflows.

The fix makes the two states mutually exclusive for one row at a time:
editing a message shrinks its enlarged portrait first (measuring the
settled, un-narrowed width to pin to), and enlarging a portrait is refused
for whichever message is currently being edited.

Source-level, the same shape as tests/test_speaker_faces.py — there is no JS
harness here, and what these numbers came from was measuring real rows in a
real browser (Chromium via Playwright), not read off the CSS.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = (REPO / "static/app.js").read_text()
INDEX = (REPO / "static/index.html").read_text()

_METHOD = re.compile(r"^    (?:async )?(?:get )?([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)


def method(name: str) -> str:
    marks = [(m.group(1), m.start()) for m in _METHOD.finditer(APP_JS)]
    for i, (found, start) in enumerate(marks):
        if found != name:
            continue
        end = marks[i + 1][1] if i + 1 < len(marks) else len(APP_JS)
        return APP_JS[start:end]
    raise AssertionError(f"no method {name}() in static/app.js")


def transcript_pfp_slot() -> str:
    """The portrait slot inside the message-list `x-for`, not the cue's own
    stand-in row (§ test_speaker_faces.py's `cue_row`) or any of the other
    `.pfp-slot` templates (persona, roster, home cards)."""
    start = INDEX.index('x-if="m.role === \'assistant\'"')
    start = INDEX.index('<span class="pfp-slot"', start)
    return INDEX[start : INDEX.index(">", INDEX.index("togglePfp(m)", start)) + 1]


# --------------------------------------------------------- enlarging while editing


def test_enlarging_the_portrait_is_refused_for_the_message_being_edited():
    body = method("togglePfp")
    assert 'if (this.editing === message.id) return;' in body


def test_the_refusal_comes_before_the_toggle_itself():
    body = method("togglePfp")
    guard = body.index("this.editing === message.id")
    toggle = body.index("this.bigPfp =")
    assert guard < toggle


def test_the_portrait_does_not_even_look_tappable_while_its_message_is_edited():
    """Not just a silent no-op on click — the affordance itself has to agree,
    or the row still reads as something a tap will do."""
    slot = transcript_pfp_slot()
    assert "editing !== m.id" in slot
    assert "tappable: !!portraitFor(m) && editing !== m.id" in slot
    assert ':tabindex="portraitFor(m) && editing !== m.id ? 0 : -1"' in slot


# --------------------------------------------------------- editing while enlarged


def test_starting_to_edit_shrinks_an_already_enlarged_portrait():
    body = method("startEdit")
    assert 'const wasBig = bubble && this.bigPfp === message.id;' in body
    assert 'if (wasBig) this.bigPfp = "";' in body


def test_the_shrink_is_measured_after_it_actually_lands():
    """Not a plain synchronous measurement: the shrink is a CSS transition on
    *two* elements (the bubble's own max-width, and the portrait's width),
    and both were found to still be mid-animation when read too early —
    the portrait especially, since the row is a flex line and the bubble's
    available space depends on how much room the picture has actually given
    back at that instant, not on the bubble's own max-width alone."""
    body = method("startEdit")
    assert "this.$nextTick(() => {" in body
    assert 'bubble.style.transition = "none";' in body
    assert 'slot.style.transition = "none";' in body
    assert "bubble.offsetHeight" in body, "forces the untransitioned layout to land"


def test_both_transitions_are_disabled_before_the_reflow_that_measures():
    """The portrait's own transition has to be off too, or the flex row still
    computes the bubble's available space against its not-yet-shrunk width —
    this was measured landing at 236px pinned instead of the eventual 350px
    until both were disabled together, not just the bubble's."""
    body = method("startEdit")
    off_bubble = body.index('bubble.style.transition = "none"')
    off_slot = body.index('slot.style.transition = "none"')
    reflow = body.index("void bubble.offsetHeight;")
    assert off_bubble < reflow and off_slot < reflow


def test_both_transitions_are_restored_afterwards():
    """Only ever disabled for the one measurement — every other tap of the
    portrait still animates."""
    body = method("startEdit")
    assert body.count('bubble.style.transition = ""') == 1
    assert body.count('slot.style.transition = ""') == 1


def test_an_ordinary_edit_is_not_slowed_down_by_any_of_this():
    """The whole detour — $nextTick, disabling two transitions, forcing a
    reflow — only exists for the one case that needs it. A message whose
    portrait was never enlarged measures and opens synchronously, exactly as
    it always did."""
    body = method("startEdit")
    assert "if (wasBig) {" in body and "} else {" in body
    branch = body[body.index("if (wasBig) {") : body.index("} else {")]
    outside = body[body.index("} else {") :]
    assert "measureAndBegin();" in outside
    assert "$nextTick" in branch  # only the wasBig path pays for it


def test_the_measurement_and_focus_logic_were_not_duplicated():
    """Split into measureAndBegin/beginEditFocus so the two entry paths (was
    big, was not) share one implementation rather than each carrying its own
    copy that could drift apart."""
    assert "measureAndBegin" in method("startEdit")
    assert "beginEditFocus" in method("startEdit")
    assert method("beginEditFocus")  # exists as its own method
