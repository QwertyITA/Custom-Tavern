# UX audit

Everything on the roadmap is built. This is the pass over the finished thing,
looking for where it is bad rather than where it is missing.

Every item below was reproduced in a real browser at 412×900 (a phone) against
a running server, not read off the source. Where a number appears, it was
measured. Nothing here is a guess about what might be wrong.

Ordered by how much damage it does, not by how hard it is to fix.

**Status:** 1, 2, 3, 4, 7 and 8 are fixed — see the note at the end of each.
5, 6 and 9 are still open.

**Two claims in the first draft of this file were wrong** and are corrected
below, in 1. The ⟳ button and the character-name button *are* correctly
disabled on a fresh install; the 404 I attributed to them was a stale error
value left over from the previous step of the same probe. The finding still
stands on its other three legs, but it was smaller than first written.

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

**Fix:** these are almost all padding. `.glyph-btn`, `.icon-btn` and `.p-move
button` to a 44px minimum box with the glyph unchanged, `.link`-style buttons
to a tapped row rather than a text run, and native checkboxes replaced with the
switch used elsewhere.

## 6. The world line truncates to nothing

**Measured** on a 412px screen, all three fields clipped:

- `the tavern common room` → **the tavern com…** (109px)
- `still and cold` → **still a…** (52px)
- `late night` → **late…** (38px)

Three ellipses in a row, and **none of the three carries a `title`**, so the
full text is not reachable by hover, by tap, or by long press. The `scene` pass
runs, costs a model call, writes a result, and the result cannot be read.

**Fix:** either a `title` and a tap-to-expand, or show one field at a time and
cycle, or drop to the two that fit. Anything except three truncations.

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
