"""What the screen does while a reply is arriving.

Two reports, both about being shown the wrong thing during a stream.

A long reply dragged the reader down a line at a time for the whole
generation — the view followed the bottom, so the only thing on screen was
the last line and the cursor, and you could not read the reply until it had
stopped being written. It follows for the first few lines now and then lets
go, exactly the way it lets go when somebody scrolls up.

And the bubble was the wrong width. It was pinned to the full column from
its first token, so a short answer streamed in a box a third too big and
snapped shut when it landed (measured: 368px while streaming, 284px after);
and the pin was a percentage of the *row*, while the bubble only gets the
part of the flex line the portrait leaves, so a wrapped reply overflowed its
own row by 18px and shrank back at the end.

Source-level, like tests/test_motion.py — there is no JS harness here. The
numbers above came from measuring real rows in a real browser.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = (REPO / "static/app.js").read_text()
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


def rule(selector: str) -> str:
    start = CSS.index(selector)
    return CSS[start : CSS.index("}", start) + 1]


# ------------------------------------------------------- following the reply


def test_the_view_lets_go_once_the_reply_runs_on():
    body = method("pinBottom")
    assert "this.outgrownTheFollow()" in body
    assert "this.stick = false;" in body


def test_letting_go_is_the_same_state_as_scrolling_up():
    """Not a third mode. `stick` false is what the scroll-to-bottom button
    reads, and coming back to the bottom resumes following, as it always
    did — so the way back is one that already existed."""
    assert "if (this.nearBottom()) { this.stick = true; return; }" in APP_JS


def test_the_budget_is_how_far_this_reply_pushed_the_bottom_down():
    """Not how tall the row is: a regeneration streams into a bubble that is
    already several lines tall, and that is not the same question."""
    body = method("outgrownTheFollow")
    assert "row.offsetHeight - (this._streamFrom || 0)" in body
    assert "STREAM_FOLLOW_LINES" in body


def test_the_starting_height_is_taken_when_the_row_starts_streaming():
    assert "this._streamFrom = row ? row.offsetHeight : 0;" in method("markStreamingRow")


def test_the_budget_is_a_few_lines_not_a_screen():
    found = re.search(r"^const STREAM_FOLLOW_LINES = (\d+);", APP_JS, re.MULTILINE)
    assert found, "no STREAM_FOLLOW_LINES"
    assert 3 <= int(found.group(1)) <= 10


def test_the_budget_is_measured_in_the_rows_own_line_height():
    """A theme or a font-size change moves what "a few lines" means, and the
    number is in lines."""
    assert 'getComputedStyle(row).lineHeight' in method("outgrownTheFollow")


# --------------------------------------------------------- the bubble's width


def test_the_bubble_is_only_pinned_once_the_reply_wraps():
    assert ".msg.streaming.wrapped .bubble" in CSS
    assert ".msg.streaming .bubble:not([style*=" not in CSS, (
        "pinned from the first token again — a short reply then streams at "
        "the full width of the column and snaps shut when it lands"
    )


def test_it_grows_into_the_line_rather_than_claiming_a_percentage():
    """A percentage resolves against the row; the bubble only gets what the
    portrait and the gap leave of the flex line."""
    pinned = rule(".msg.streaming.wrapped .bubble")
    assert "flex-grow: 1;" in pinned
    assert "min-width" not in pinned.split("{", 1)[1]


def test_the_regeneration_pin_still_opts_out():
    """A swipe grows out of the cue at its own pace and must not be stretched."""
    assert '.msg.streaming.wrapped .bubble:not([style*="min-width"])' in CSS


def test_the_mark_goes_on_once_and_comes_off_with_the_row():
    marking = method("markFlowing")
    assert 'row.classList.contains("wrapped")' in marking, "set once, not re-tested away"
    assert 'this.hasWrapped(row)' in marking
    assert '"animating", "streaming", "flowing", "wrapped"' in method("markStreamingRow")


def test_wrapping_is_measured_on_the_body_that_has_a_box():
    """Every bubble holds two bodies and the first is the hidden regeneration
    cue (§CLAUDE.md), which has no box to measure."""
    body = method("hasWrapped")
    assert '".body:not(.regen)"' in body


def test_a_second_paragraph_is_itself_a_wrap():
    """In "separate paragraphs" there is one body per paragraph, so a second
    one existing answers the question without measuring anything."""
    body = method("hasWrapped")
    assert "bodies.length > 1" in body


def test_one_line_has_room_for_a_descender():
    found = re.search(r"^const ONE_LINE = ([\d.]+);", APP_JS, re.MULTILINE)
    assert found and 1.1 <= float(found.group(1)) <= 2.0
