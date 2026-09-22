"""The edit box was hard to read, unformatted, and taller than the screen.

Reported live, from a screenshot of the phone: a floating world-info pill
(§ .world-pill-float, styles.css) sitting on top of the first two lines of
the box; the raw `*asterisk*`/`"quote"` markup showing as literal characters
instead of the italics and colour the finished message renders in; and a box
that scrolled internally past what the screen could show, needing a second,
separate scroll of the chat underneath it just to reach the box's own first
line.

Three fixes, verified in a real browser (Chromium via Playwright) against a
seeded chat rather than read off the CSS alone:

- `pinEditingRow` scrolls the row being edited clear of the header and the
  floating pill, and — the case that actually matters, editing the newest
  message — manufactures room to do it in when the natural scroll range is
  too short to reach: measured live, `scrollTop` sitting exactly at
  `scrollHeight - clientHeight` and going no further, on a 40-message chat.
- The read-only body's own `Markup.schedule` call is reused, unmodified, on
  a `.edit-backdrop` layer sitting behind a transparent textarea — the same
  tokenizer parity §CLAUDE.md already requires between `app/markup.py` and
  `static/markup.js` means the preview can never disagree with how the
  message actually renders once the edit ends.
- `editBoxRoom` caps the box at the room actually left below its pinned row,
  recomputed on every `visualViewport` resize so the keyboard opening or
  closing re-adapts the box instead of leaving it too tall or too short.

Along the way: a 7px scroll desync between the textarea and its backdrop,
traced to a `<textarea>`'s default `display: inline-block` inflating
`.edit-wrap`'s auto-height without showing up in the box's own
`offsetHeight` (measured live: 221px box under a 228px wrapper); and the
row's own geometry reading as garbage on a genuinely scrolled chat because
`.msg`'s `content-visibility: auto` (§CLAUDE.md) was skipping layout for a
row outside the currently-rendered window.

Source-level, the same shape as tests/test_edit_portrait_conflict.py — no JS
harness here, and what these numbers came from was measuring a real page in
a real browser, not read off the source.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = (REPO / "static/app.js").read_text()
INDEX = (REPO / "static/index.html").read_text()
STYLES = (REPO / "static/styles.css").read_text()

_METHOD = re.compile(r"^    (?:async )?(?:get )?([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)


def method(name: str) -> str:
    marks = [(m.group(1), m.start()) for m in _METHOD.finditer(APP_JS)]
    for i, (found, start) in enumerate(marks):
        if found != name:
            continue
        end = marks[i + 1][1] if i + 1 < len(marks) else len(APP_JS)
        return APP_JS[start:end]
    raise AssertionError(f"no method {name}() in static/app.js")


def rule(selector: str, source: str = STYLES) -> str:
    start = source.index(selector)
    return source[start : source.index("}", start) + 1]


def edit_wrap_block() -> str:
    start = INDEX.index('<div class="edit-wrap"')
    end = INDEX.index("</div>", INDEX.index("</textarea>", start))
    return INDEX[start:end]


# --------------------------------------------------------- content-visibility


def test_the_row_being_edited_is_exempted_from_content_visibility_auto():
    """Without this, a row far enough down a scrolled chat to be outside the
    currently-rendered window reports zero/garbage geometry to
    `pinEditingRow`/`editBoxRoom` — style and layout are skipped for the
    whole subtree, not just paint (§CLAUDE.md)."""
    block = rule(".msg.editing,")
    assert "content-visibility: visible" in block


def test_the_editing_class_is_bound_on_the_message_row():
    assert "editing === m.id ? 'editing' : ''" in INDEX


# --------------------------------------------------------- live markup preview


def test_the_edit_backdrop_reuses_the_read_only_bodys_own_markup_call():
    """Not a second implementation of the tokenizer — the exact same
    `Markup.schedule` call the read-only `.body` makes, so tokenizer parity
    (§CLAUDE.md) is inherited automatically rather than needing to be kept
    in step by hand."""
    block = edit_wrap_block()
    assert 'class="edit-backdrop"' in block
    assert 'x-effect="Markup.schedule($el, editText)"' in block


def test_the_backdrop_draws_nothing_a_screen_reader_should_announce():
    """The textarea beside it already carries the real, editable text."""
    block = edit_wrap_block()
    assert 'aria-hidden="true"' in block


def test_the_backdrop_is_opaque_and_not_glassed():
    """Reported live as hard to read: the one busy background it sat over —
    the tavern photo — bled through a translucent panel under Glass."""
    block = rule(".edit-backdrop")
    assert "background: var(--panel)" in block
    assert "backdrop-filter" not in block


def test_the_textarea_itself_is_invisible_except_for_caret_and_selection():
    """What is actually read is the backdrop behind it; the textarea is only
    what is actually typed into."""
    block = rule(".edit-box")
    assert "color: transparent" in block
    assert "caret-color: var(--text)" in block
    selection = rule(".edit-box::selection")
    assert "color: var(--text)" in selection


def test_the_textarea_defaults_to_block_display():
    """A `<textarea>` is `display: inline-block` by default, which sits it on
    a text baseline and leaves a descender gap below it — invisible in the
    box's own `offsetHeight` but not in `.edit-wrap`'s auto-computed height,
    which the backdrop (sized to match the wrapper) then inherited. Measured
    live: a 221px box under a 228px wrapper, and the backdrop's own
    scrollable area 7px taller than the text underneath it needed."""
    assert "display: block;" in rule(".edit-box")


def test_the_preview_is_kept_in_step_with_the_boxs_own_internal_scroll():
    """A drag on the textarea's native scrollbar, a paste, or the caret
    moving past what is visible all scroll the box without touching the
    backdrop drawn behind it — without this the preview stayed put while
    the real text scrolled on underneath the transparent box."""
    block = edit_wrap_block()
    assert '@scroll="syncEditBackdrop($event.target)"' in block
    body = method("syncEditBackdrop")
    assert "backdrop.scrollTop = el.scrollTop" in body


# --------------------------------------------------------- dynamic, pinned height


def test_edit_box_room_measures_the_actual_room_left_not_a_flat_fraction():
    """The old cap was `viewportHeight() * 0.55` — either too little on a
    tall phone or, with the keyboard open, taller than what was actually
    still visible."""
    body = method("editBoxRoom")
    assert "this.scrollPort.getBoundingClientRect().bottom" in body
    assert "el.getBoundingClientRect().top" in body


def test_pin_targets_the_pill_when_it_is_floating_and_the_header_otherwise():
    body = method("pinEditingRow")
    assert 'document.querySelector(".world-pill-float")' in body
    assert 'getComputedStyle(pill).display !== "none"' in body
    assert "Math.max(portTop, pillBottom)" in body


def test_pin_manufactures_room_when_the_natural_scroll_range_is_too_short():
    """The case this exists for: editing the newest message, which has
    nothing below it to scroll past. `scrollTop` clamps at `scrollHeight -
    clientHeight` well short of the wanted position — confirmed live at
    exactly that maximum on a 40-message chat — so the shortfall is made up
    by growing `.chat-inner`'s own bottom padding before the scroll lands."""
    body = method("pinEditingRow")
    assert "const shortfall = wanted - (port.scrollHeight - port.clientHeight);" in body
    assert 'inner.style.paddingBottom = `${Math.ceil(shortfall)}px`;' in body
    assert "port.querySelector(\".chat-inner\")" in body


def test_the_manufactured_spacer_is_tracked_so_it_can_be_given_back():
    body = method("pinEditingRow")
    assert "this._editSpacerEl = inner;" in body


def test_end_edit_removes_the_spacer():
    """Only ever there to make the newest row reachable — not a real part of
    the transcript's layout, so it does not outlive the edit."""
    body = method("endEdit")
    assert 'this._editSpacerEl.style.paddingBottom = "";' in body
    assert "this._editSpacerEl = null;" in body


def test_the_box_is_repinned_once_more_after_it_reaches_its_real_height():
    """Focusing a textarea that then grows taller is exactly the shape
    Chrome's own "keep the focused control in view" heuristic reacts to —
    measured scrolling a further ~490px past the pin on a tall message, on
    top of anything this code itself asked for. The first pin (needed for
    `editBoxRoom` to have something to measure against) is not enough on its
    own; a second pin after the box's real height lands is what nothing but
    the browser can be trusted to leave alone."""
    body = method("beginEditFocus")
    assert body.count("this.pinEditingRow(box)") == 2
    first = body.index("this.pinEditingRow(box)")
    second = body.index("this.pinEditingRow(box)", first + 1)
    raf = body.index("requestAnimationFrame(() => {")
    assert first < raf < second, "the second pin runs inside the rAF, after autosize"
    inside_raf = body[raf:second]
    assert "this.autosizeEditBox(box);" in inside_raf


def test_the_first_pin_happens_before_focus_opens_the_keyboard():
    """Scrolling the row into place while the keyboard animates on top of it
    read as two separate jumps instead of one settled move."""
    body = method("beginEditFocus")
    pin = body.index("this.pinEditingRow(box)")
    focus = body.index("box.focus()")
    assert pin < focus


def test_keyboard_open_or_close_recomputes_the_cap_via_visualviewport():
    body = method("beginEditFocus")
    assert "window.visualViewport.addEventListener(\"resize\"" in body
    assert "this.autosizeEditBox(box)" in body


# --------------------------------------------------------- stick suppression


def test_starting_an_edit_suppresses_auto_follow_to_bottom():
    """Growing the box as you type must not drag the row you are reading
    back down to the bottom the moment it needs one more line — that undid
    `pinEditingRow` on the very next keystroke the one time this went
    untested."""
    body = method("startEdit")
    assert "this._stickBeforeEdit = this.stick;" in body
    assert "this.stick = false;" in body


def test_ending_an_edit_resumes_following_only_if_it_was_following_before():
    body = method("endEdit")
    assert "if (this._stickBeforeEdit) this.scrollDown();" in body
    assert "this._stickBeforeEdit = false;" in body


def test_scrolling_near_the_bottom_while_editing_does_not_rearm_stick():
    """The pin's own manufactured-room scroll can legitimately land exactly
    at the (spacer-extended) bottom when the row being edited is the newest
    message. Re-arming `stick` there fed the next ResizeObserver tick — the
    box finishing its own growth — straight into `pinBottom`, which jumped
    to the true bottom instead of the pinned one: a ~490px overshoot past
    the row, measured on the case this exists for."""
    assert 'if (!this.editing && this.nearBottom()) { this.stick = true; return; }' in APP_JS
