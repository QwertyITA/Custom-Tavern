# UX audit

Everything on the roadmap is built. This is the pass over the finished thing,
looking for where it is bad rather than where it is missing.

Every item below was reproduced in a real browser at 412×900 (a phone) against
a running server, not read off the source. Where a number appears, it was
measured. Nothing here is a guess about what might be wrong.

Ordered by how much damage it does, not by how hard it is to fix.

**Status: all nine are fixed.** Each carries a note at the end saying how, and
each fix was re-measured in the browser afterwards rather than assumed.

**Three claims in the first draft of this file were wrong** and are corrected
in place below. In 1, the ⟳ button and the character-name button *are*
correctly disabled on a fresh install — the 404 I attributed to them was a
stale error value left over from the previous step of the same probe. In 6,
the world fields *do* carry `title`; I checked the leaf span and the attribute
is on its parent. Both findings still stand on their other legs, but each was
smaller than first written.

---

## 1. The first run is a dead end

**Reproduce:** `TAVERN_DATA_DIR=/tmp/fresh python3 -m uvicorn app.main:app` and
open it.

A brand-new install has no characters. What happens:

- A red banner says **"No characters. Drop a card in data/characters/ and
  restart."** That advice is wrong. The app can make a character
  (chats → New character) and import a card, neither of which needs a restart
  and neither of which is mentioned.
- `boot()` hits `return` after setting that error, so no chat is opened and
  `chatId` stays empty — but nothing on screen is disabled to match.
- The composer is fully live. It says *Say something…*, the send button works,
  and sending produces **`404 Not Found`** in the same red banner. The user
  typed into a box the app offered them and got an HTTP status code back.
- Dismissing the banner leaves an empty tavern illustration, a text box, and no
  indication that anything is wrong or what to do.

~~The ⟳ button is live too and also answers 404.~~ ~~The character-name button
does nothing.~~ **Both wrong.** Re-checked: `⟳` carries `:disabled="!chatId"`
and `.who` carries `:disabled="!character"`, so both are correctly dead on a
fresh install. The 404 I attributed to them was the error from the *send* in
the previous step of the same probe, still sitting in `error` when I read it.

So the one screen where a new user has no idea what they are doing is the one
screen with no guidance, one control that fails, and one instruction that is
incorrect.

**Fixed.** `boot()` no longer raises a red banner; a *Nobody here yet* empty
state sits where the conversation would be, with the two buttons that actually
work (*New character*, *Import a card*) — the same ones the chats panel
carries, not a second implementation. The composer, its **+** menu and Send are
disabled until there is somewhere to send to, and the placeholder says
*Nobody to talk to yet* rather than inviting a message it would throw away.
The four parts of the empty state arrive in sequence on `--ease-back`.

## 2. 419 invisible controls are in the tab order

**Measured:** with the Brain panel open, 446 focusable controls, **419 of them
invisible**. Thirteen closed `.fold` blocks contain 664 focusable elements
between them, and `.fold-clip` computes to `visibility: visible`.

`.fold` collapses with `grid-template-rows: 0fr` and `overflow: hidden`. That
hides things from the eye and from nothing else. Everything inside a collapsed
fold still takes focus (verified: `element.focus()` succeeds and
`document.activeElement` moves), still has a bounding box, and is still
announced by a screen reader.

The same is true of the closed menu row: tabbing forward from the composer
walks straight through *Model and engine*, *Appearance*, *Characters and chats*
and *Story state* while the row is shut.

In practice: anyone on a Bluetooth keyboard, or using switch access, or using
a screen reader, has to walk past every sampler for all eight passes — twice
over, since the sampling section itself is folded inside another fold — to get
from the top of the Brain panel to the Save button.

**Fixed.** `.fold:not(.open) > .fold-clip` and `.menu-wrap:not(.open) >
.menu-clip` go `visibility: hidden`, with a `transition-delay` matching the
collapse so the closing animation still plays in full — 300ms for a fold, 415ms
for the menu row, which has to cover the buttons' own staggered exit as well.
Verified: 662 controls inside closed folds, none of them focusable, and
`focus()` on the first no longer moves `document.activeElement`.

## 3. Deleting a character stays armed forever

**Reproduce:** chats panel → tap 🗑 on a character → tap anywhere else → the
button is still armed.

`CLAUDE.md` says destructive actions arm on the first tap and act on the
second. Six of them do exactly that and disarm after `CONFIRM_MS` (3s):
regex rules, custom prompt blocks, personas, backgrounds, and messages in two
places.

`deleteCharacter` and `deleteChat` set `confirmChar` / `confirmChat` and
**never clear them.** No timer, no cancel, no disarm on tapping elsewhere.

These are the two most destructive actions in the app — deleting a character
takes every chat it has with it, and the button's own tooltip says so. They are
the only two that stay live indefinitely. Tap delete, get distracted, scroll,
come back, tap what looks like a fresh first tap, and it fires.

**Fixed.** Both go through one `arm()` helper that sets the same
`CONFIRM_MS` timeout the other six use, and disarms the other row while it is
at it — a character row and a chat row can be on screen together, and arming
one should visibly cancel the other rather than leaving two buttons that both
look ready.

## 4. A failed turn cannot be retried

**Reproduce:** point the blocking tier at a dead backend, send a message.

The message is stored correctly and the error is readable — *"reply failed:
ollama: All connection attempts failed"*. That part is right. What is missing is
any way forward.

The chat now ends on a user message with no reply. There is no *try again*
anywhere. The nearest-looking action, **Regenerate** in the **+** menu, is
enabled and does something actively misleading: it re-rolls the *previous*
reply, the one that already worked, and leaves your unanswered message
untouched at the bottom. No warning that it did something other than what you
asked.

The only real recovery is to type the message a second time, which leaves the
first one sitting in the transcript forever.

**Fixed.** `POST /api/chats/{id}/retry` answers the dangling message without
sending it again, and a quiet dashed *No reply came — try again* pill appears
where the reply would have been. The route reuses the reply half of `run_turn`
— extracted as `_answer()` — so a retry cannot drift from a first attempt: same
state decay, same nudges, same web search, same background passes. It refuses
when nothing is waiting rather than inventing a second reply. Twelve tests in
`tests/test_retry.py`, including one pinning that the message is not stored
twice.

## 5. Touch targets are too small, nearly everywhere

The deploy target is a phone. Apple's minimum is 44pt, Material's is 48dp.
Measured, at 412px wide:

| Control | Size | Where |
| --- | --- | --- |
| Move section up/down | **32×19** | prompt layout, ×2 per section, 17 sections |
| Leave out *section* | 40×23 | prompt layout, ×17 |
| **Load** (fetch model list) | **27×14** | every backend |
| Remove backend | 45×14 | every backend |
| Test connection | 83×14 | every backend |
| Delete this rule | 80×14 | every regex rule |
| Native checkboxes | **13×13** | regex options, mute |
| Close the sheet (✕) | 26×33 | every panel |
| Edit / delete / star / export / history | 34×30 | every message, every character row |
| Menu, ⟳ | 32×32 | header |
| Sliders | 388×**16** | every number field |
| Colour swatches | 46×30 | theme, ×16 |
| Send | 40×40 | composer |

The prompt-layout reorder arrows at **19 pixels tall** are the worst: a control
whose entire job is repeated tapping, at under half the minimum, with its twin
directly beneath it. The character row puts *six* 34×30 buttons side by side —
star, edit, export, new chat, history, delete — with delete at the end.

`Send` at 40×40 is the near miss; everything above it is a real one.

**Fixed**, and re-measured: the main screen now has nothing under 40px in
either axis, and neither do the chats or theme panels. Three techniques, chosen
per case rather than applied uniformly:

- **Real height** where there was room: `.icon-btn` and `.glyph-btn` to 44,
  `.link` buttons to a 44px flex row (the underline still marks where the words
  are; the padding is what the finger gets), form fields, `.wide` buttons and
  the search box likewise. The message tool row's gap went 4→6px, because
  widening the buttons any further would have put two targets on top of each
  other and a tap meant for edit would have landed on delete.
- **Turned on its side** for the reorder arrows. Stacked, each got 19px — the
  worst target in the app, on the control whose entire job is repeated tapping,
  with its twin directly beneath. Side by side both clear 44 *and* the section
  row is shorter than it was, because the column no longer sets its height.
- **An invisible 44×44 overlay** for `.p-switch` and the colour swatches, which
  are drawings that would become slabs at 44. Only safe because nothing else is
  within 22px of them — verified by scanning every other control against each
  overlay's box. It needed `z-index: 1`: without it the overlay paints under
  the next positioned box and the extra area hit-tests to *that*, which looks
  identical and works not at all. Confirmed by `elementFromPoint` at ±14px and
  ±21px off centre, and by checking the switch still toggles from there.

`.who` was going to use the overlay too, but the header clips a pseudo-element
that reaches past its row — the extra area hit-tested to the bar. It got real
height instead, which there is room for now that the icons beside it are 44.

The native checkboxes are still 20×20, deliberately: the whole `.toggle` row is
the label and the whole row is 44px, so the box only has to be big enough to
see. It also scales down 12% on press, because a checkbox that changes state
instantly is the one control where the eye has nothing to follow from the
finger to the result.

## 6. The world line truncates to nothing

**Measured** on a 412px screen, all three fields clipped:

- `the tavern common room` → **the tavern com…** (109px)
- `still and cold` → **still a…** (52px)
- `late night` → **late…** (38px)

Three ellipses in a row, and **none of the three carries a `title`**, so the
full text is not reachable by hover, by tap, or by long press. The `scene` pass
runs, costs a model call, writes a result, and the result cannot be read.

~~None of the three carries a `title`.~~ **Wrong** — they all do; I read
`title` off the leaf `.field-value` span and the attribute is on its parent
`.field`. It does not change the finding: `title` is a hover affordance and a
phone has no hover, so on the device this is built for the text was still
unreachable.

**Fixed.** The line is a button now. Tapping it eases the setting open
underneath the header in full, one labelled row per field, through the same
fold mechanism as the menu row — measured opening over 19 distinct heights on a
decelerating curve. It closes on every chat change, so it never carries the
previous room's weather into a new one, and it only lists fields that have a
value, because an empty row would say less than no row.

## 7. Missing portraits fire a 404 on every render

**Observed in the server log:**

```
GET /avatars/ HTTP/1.1" 404
GET /static/characters/ HTTP/1.1" 404
```

Note the empty filename. `x-show` sets `display: none`; it does not stop the
browser fetching a bound `:src`. So every character without a portrait and
every persona without an avatar requests the *directory* — a guaranteed 404 —
each time the list renders. Four such requests on a single page load here.

The `@error` handler hides the broken image afterwards, which is why nothing
looks wrong. It is still a wasted request per row per render, on a phone, plus
a log full of 404s that will hide a real one.

**Fixed.** `<template x-if>` instead of `x-show` in all five places —
character rows, persona rows, the persona editor, and both message portraits.
The last two were worse than the rest: `portrait` can be empty, and an empty
`src` is not "no image", it is the current page, which the browser re-fetches
once per bubble. A fresh load now makes zero 404s.

## 8. Raw HTTP status codes reach the user

**`404 Not Found`** is shown verbatim in the error banner. So is anything else
`api.*` throws. The backend failure message — *"reply failed: ollama: All
connection attempts failed"* — shows the effort that went into the ones that
were written deliberately, which makes the bare ones stand out more.

**Fixed.** `apiError` falls back to a sentence per status, and to one of two
generic sentences otherwise. Anything the server explained itself still travels
through untouched — those messages were written on purpose and are better than
these. `runStream` was formatting its own `${status} ${statusText}` and is now
routed through the same function; it was the one path where a bare code still
reached the screen after the others were given sentences.

## 9. Sixteen colour pickers and no contrast check

The theme panel offers sixteen individual colour inputs with no warning when a
combination is unreadable. Text and its background can be set to the same
value. `luminance()` already exists in `app.js`, so the arithmetic is there.

This is recoverable — *Reset appearance to defaults* is right there — so it is
last on the list. But a small live warning against the pair being edited would
cost almost nothing.

**Fixed.** A quiet dashed panel in the Colour section names the pairs that
actually sit on top of each other — text on background, text on bars, muted
text, and the three markup colours in a bubble — with their ratio, whenever one
falls under 4.5:1. Live against the form rather than what is saved, so it warns
while the colour is being chosen. It only warns: a palette that fails a
standard but reads fine to the person who made it is their call, and this is a
personal theme on a personal phone. What it will not do is let the app go
unreadable in silence.

Writing it turned up that **the shipped default palette failed its own check**
in two places — muted text at 3.7:1 and the emphasis colour at 4.1:1, both on
11–12px text. Rather than soften the threshold, the two colours were darkened
along their own hue until they cleared it: `--muted` `#8b7d84` → `#7d6f76`
(4.5 on the background, 4.8 on the bars) and `--c-strong` `#a9722c` →
`#9a6828` (4.5 and 4.8). The default palette now warns about nothing.

---

## Not problems

Checked and found correct, recorded so they are not re-investigated:

- **Long unbroken text.** A 120-character word and a long URL both wrap. No
  bubble overflows, and the page never scrolls horizontally.
- **A failed send does not lose your message.** It is stored before the reply
  is attempted, so it survives in the transcript. (The composer clears, but
  nothing is lost.)
- **The closed sheet is `display: none`,** so unlike the folds it does not leak
  focus.
- **Every icon-only button has both `aria-label` and `title`.** There is not one
  unnamed control in the app.
- **No inputs are missing labels.**
- **No JavaScript errors** on load, on any panel, or through a full turn.
- **The animations are correct.** Spot-checked again during this pass: the fold
  added for the web-search hint grows through 18 distinct heights on a
  decelerating curve (8.9, 7.6, 6.1, 4.5, 3.3, …), not a linear ramp.
