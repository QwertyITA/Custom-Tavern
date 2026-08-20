# Animation suggestions

Research pass over what the app already does, what current practice says, and
what is worth adding.

**Status: everything here is built except §5** — View Transitions and
scroll-driven animations, which replace working code rather than fill a gap and
want the Chrome-only assumption confirmed first. Each section carries a note
saying what shipped and what it measured.

**Correction to the first pass.** §2.1 was reported as verified and was not.
The fade was measured on a `.body` appended straight to the chat column, which
has no `content-visibility: auto` above it; every real message row does, and
that skips style and layout for the whole subtree, so the animation existed as
a live object that never moved a pixel. Three later animations hit the same
wall. The row being animated is now marked explicitly — see §2.1 — and the
fade was re-measured inside a real row.

## What is already there

Counted at the time of the research: **20 `@keyframes`, 33 `animation:` rules,
43 `transition:` rules**, three easing tokens, and a hand-rolled FLIP in four
places (prompt sections, regex rules, character rows, chats). The rule that
nothing moves linearly was held everywhere except the two continuous rotations,
which is correct — easing a spinner makes it hesitate once per turn.

Those counts are the *before* picture. After this pass there are four easing
tokens, eight duration tokens, a motion dial, and linear is right in three
places rather than two.

So this is not a bare app getting its first motion pass. Most of what follows
is either a system-level upgrade or a surface that was never covered.

## Target browser

The deploy target is Chrome on Android, installed as a PWA. That is a single
known Chromium, which is unusually permissive — View Transitions,
scroll-driven animations and `linear()` are all available. **Worth confirming
you do not care about Firefox or desktop Safari** before leaning on the two
that are not Baseline.

---

## 0. A bug, before any of the suggestions

**The `prefers-reduced-motion` block has gone stale.** It is a hand-maintained
allowlist of **11 selectors** — `.menu-wrap`, `.bubble`, `.body`, `.msg-tools`
and seven more — against 33 `animation:` rules and 43 `transition:` rules. Every
animation added after it was written is uncovered: the action wheel, the sent
message flying up from the composer, the star pop, the attachment chips, and all
four of the ones added this session (empty state, retry pill, world line,
contrast warning).

Someone who has asked their phone to stop moving things currently gets about a
quarter of that.

**Fix:** invert it. Instead of naming what to stop, stop everything and name the
exceptions:

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 1ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 1ms !important;
    transition-delay: 0ms !important;
    scroll-behavior: auto !important;
  }
}
```

That is one rule that cannot go stale, and it covers every animation added
after it. Keep `.icon-btn.refresh.spinning` as a deliberate exception if you
want the spinner to keep turning — a progress indicator that stops indicating
is worse than one that moves.

This is a correctness gap, not a suggestion. I would do it before anything
below.

**Built.** Exactly the wildcard above, with two exceptions kept at their own
durations — the refresh spinner and the ambient pass pulse, because a progress
indicator that stops indicating is worse than one that moves, and neither
travels. A third exception was needed once §2.2 landed: the shimmer cannot
simply be stopped, because frozen it leaves the label painted with whatever
slice of its gradient it halted on, so under reduced motion the gradient is
removed and the text put back. Verified in both motion states — everything
1ms, the two indicators still turning.

---

## 1. The motion system

### 1.1 Duration tokens — the obvious missing half

There are three easing tokens in `:root` and **zero duration tokens**. The CSS
uses roughly two dozen distinct durations (140, 160, 170, 180, 200, 220, 240,
260, 280, 300, 320, 340, 415, 420ms…) and `app.js` carries eleven more as
`*_MS` constants. `app.js` even admits the problem in a comment: *"Kept in JS as
well as CSS because the sequence has to wait for each step; they must stay in
step with styles.css."*

Two numbers that must match, in two files, with nothing enforcing it.

**Suggestion:** a small scale beside the easings, and read it from JS rather
than duplicating it.

```css
:root {
  --dur-fast: 140ms;   /* colour, border, a tap responding      */
  --dur-base: 240ms;   /* most things moving or changing size   */
  --dur-slow: 340ms;   /* something large, or travelling far    */
}
```

```js
const ms = (name) =>
  parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
```

Now `TEXT_FADE_MS` and its CSS counterpart cannot drift, because there is only
one of them. Current practice is to standardise on three or four durations
exactly like this ([techqware](https://www.techqware.com/blog/motion-design-micro-interactions-what-users-expect)).

This is the highest-value item on the list and the cheapest. It also makes
everything below easier to write.

**Built**, and it caught a live drift on the way in: `.msg.sending` ran 420ms
while `MESSAGE_SEND_MS` said 460. There are now three scale tokens plus five
named ones for the durations JavaScript waits on, and `app.js` reads them
through `dur(name)` — cached, since they cannot change without a stylesheet
edit. The old constants are gone rather than kept in sync.

### 1.2 Real springs via `linear()`

`--ease-back` is `cubic-bezier(.34,1.56,.64,1)` — a single overshoot, which is
as close to a spring as a bezier can get. A cubic-bezier has two control points
and physically cannot express a settle with more than one bounce.

`linear()` can. It takes a list of stops and approximates any curve, including a
real damped spring, and it has been **Baseline since December 2023** — no build
step, no library, which matters here
([Chrome for Developers](https://developer.chrome.com/docs/css-ui/css-linear-easing-function),
[Josh Comeau](https://www.joshwcomeau.com/animation/linear-timing-function/)).

```css
/* A damped spring: overshoots, comes back, settles. Generated once and
   pasted — this is what --ease-back wanted to be. */
--ease-spring: linear(
  0, 0.006, 0.025 2.8%, 0.101 6.1%, 0.539 18.9%, 0.721 25.3%, 0.849 31.5%,
  0.937 38.1%, 0.968 41.8%, 0.991 45.7%, 1.006 50.1%, 1.015 55%, 1.017 63.9%,
  1.001
);
```

Generators: [kvin.me/css-springs](https://www.kvin.me/css-springs/how-to-use),
[spring-easing](https://spring-easing.okikio.dev/).

**Where it would actually help** — not everywhere, only where something lands:
the action wheel opening, the sent message arriving, the switch knob, the star
pop. Leave `--ease-out` alone for the folds; a panel that bounces open is a
panel that looks broken.

**Built**, on exactly those five. The curve was generated from an actual damped
oscillator rather than pasted from a preset — zeta 0.62, omega 13, 31 evenly
spaced samples, chosen by modelling four dampings and reading off the
overshoot. It peaks 8.3% past target, about what `--ease-back` does, so nothing
looks bouncier; the difference is that it crosses back. Measured on the switch
knob: **two crossings of its resting place** where `--ease-back` gave one.

### 1.3 Motion tokens on the theme panel

The theme panel has 16 colour tokens and no motion control. One slider —
*Motion*, from **off** through **calm** to **full** — writing a
`--motion-scale` multiplier on `:root` would let someone who finds it busy turn
it down without turning it off, which the binary `prefers-reduced-motion` does
not offer.

**Built.** Every duration token is now `max(1ms, calc(<n>ms * var(--motion)))`,
so one dial scales the whole interface rather than switching individual things
off. Two details it needed: the tokens had to be registered with `@property`
so they compute (an unregistered custom property hands JavaScript back the
literal `calc(...)` string, which is exactly what `dur()` reads), and at zero
the handful of animations that repeat forever have to be stopped by name —
1ms a cycle is not "off", it is a strobe. The OS setting still wins.
Measured at 100 / 50 / 0: CSS and JS both read 240 / 120 / 1ms, and the
shimmer becomes solid text at zero. Twelve tests in `tests/test_motion.py`,
including one pinning that 0 survives the round trip rather than being read as
"unset".

---

## 2. The chat surface — where the eye actually is

### 2.1 Streaming text arrives with no motion at all

`target.text = buffer` on every delta. Each token pops in, fully opaque, the
instant it arrives. This is the single most-watched surface in the app and it is
the least animated one.

**Suggestion:** fade each arriving chunk in over ~180ms. The tokenizer already
splits the reply into spans for colouring, so the hook exists — give newly
appended spans an entrance animation and let the older text sit still.

The reason this matters beyond looks: a reply that fades in reads as *arriving*,
while one that pops reads as *jumping*, and a local model on a phone delivers
tokens unevenly enough that the difference is visible.

Careful: animate only the new spans, never re-run it over the whole bubble, or
every token re-animates the entire reply and the phone will cook.

**Built.** `Markup.render` takes a reveal offset and splits the run that
straddles it, so a sentence arriving inside dialogue that started three frames
ago is marked correctly rather than classed one way or the other. `schedule`
decides the offset with `startsWith` rather than a length comparison, which
matters because Alpine reuses containers: only text that literally continues
what is on screen counts as arriving — an edit, a swipe or a reused element is
a rewrite and appears without a flourish. Opacity only, deliberately: text that
slides is text you cannot read while it moves. Measured over a simulated token
stream — each chunk marked exactly once, **12 distinct opacity steps** on a
decelerating curve, nothing left marked once the reply settles.

**And then it did not work.** That measurement was taken on an element appended
directly to the chat column. Real messages sit in a `.msg` row carrying
`content-visibility: auto`, which does not merely skip painting — it skips
style and layout for the subtree, so `getAnimations()` returned a running
animation whose effect was never computed. `runStream` now marks the row it is
streaming into for the length of the stream, and there is exactly one at a
time. Re-measured **inside a real row**: 12 distinct opacity steps, same curve.

**And then it still did not work, for a much stupider reason.** Both
measurements were taken with text being written to an element under test.
In the real send path the reply is written to `target.text`, and `target` was
the object *pushed into* `messages` — not the proxy Alpine stores in its place.
Writes to the raw object are invisible to the reactive system, so nothing on
screen changed until the `reply` event replaced the message wholesale at the
end of the stream. Measured against a live reply: `Markup.schedule` ran **three
times for a whole generation** — once for the user's message, once for the
first token, once at the end. The bubble sat at the width of the first token
for the entire reply and then snapped open in a single frame. The per-token
animation was never the problem; nothing was streaming at all. `runStream`
reads the message back out of the array now. Re-measured: **25 renders** over
the same reply, text growing 3 → 145 characters. The swipe path never had this,
because `find()` hands back the proxy already.

Two layout faults were sitting behind it, invisible while the text arrived in
one lump:

- **The tools row was `x-show="!streaming"`.** `x-show` sets `display: none`,
  so every message on screen lost its edit/delete row when a turn started and
  got it back when the turn ended — the whole transcript shrank and grew, twice
  a turn. It fades in place now and keeps its box.
- **A trailing newline is a real line under `pre-wrap`.** Models stream one
  mid-paragraph constantly; the server's cleaned copy does not have it. The
  bubble stood a line too tall for the length of the stream and lost that line
  at the moment the reply settled. What is *shown* is trimmed at the end; the
  buffer is not.

Measured end to end afterwards, the bubble grows line by line as the text does
and the final frame changes its height by **zero pixels**.

### 2.1a The cursor, and text that resolves rather than fades

**Built**, replacing the plain fade. Each arriving chunk comes in out of focus
and over-bright — `blur(3px)` and a glow the colour of the text itself — and
resolves over `--dur-token-in`. Still no transform, for the reason the fade had
none: text that moves is text you cannot read while it moves, and blur and glow
do not reflow anything, so a chunk resolving never pushes the words after it
around.

A block cursor sits at the end of the streaming body (`::after` on
`.msg.streaming`), accent-coloured, **zero-width and zero-height** so it can
never push the last word into a wrap and then unwrap it when the stream ends.
It is solid while chunks are arriving and blinks once they stop, which is the
difference between a model that is working and one that has gone quiet.

The blink is on `steps(1, end)`, not an easing and not a linear ramp: a
terminal cursor is on or off, and a fade between the two reads as a pulse,
which is something else saying something else. Four things in this app do not
ease now — the spinner, the composing shimmer, the skeleton sweep, and this —
and this is the only one that is not continuous.

This is the trap to know about in this codebase. Two rules already existed for
it (`.msg:has(.bubble.leaving)`, `.msg:has(.bubble.clipping)`) and `:has()` is
not enough on its own — the animation starts in the frame the subtree is still
being skipped in, so the class has to be on the row *before* the animation
begins.

### 2.1b Paced, and open sideways first

Two things the measurement above made obvious once the text was actually
arriving. A local model on a good card writes faster than anyone reads, in
bursts, so the reply alternated between a wall of text and nothing; and the
bubble widened with the text, which re-wraps every line already placed and
moves the words a reader is halfway through sideways under them.

`makePacer` keeps one buffer and hands it out at **60% of the rate it arrives
at**, measured continuously rather than assumed. Once the source closes,
whatever is left is cleared inside 1.5s — a paced tail is pleasant, a paced
tail four seconds after the backend went quiet is the app looking broken. The
finished reply is not swapped in until the backlog has drained, or the last of
it would skip into place.

The bubble takes its full width in one movement at the start of the stream
instead of growing into it: one re-wrap rather than one per token, and the only
direction the text then travels is down.

### 2.2 A shimmer on the composing cue

The cue is three bouncing dots and a label. Current practice for AI chat has
moved to a **shimmer travelling through the label text** — "Typing…", "Looking
it up…", "Reading…" — because a moving gradient reads as *working* where a
static label reads as *stuck*
([thefrontkit](https://thefrontkit.com/blogs/ai-chat-ui-best-practices)).

This app has more to say here than most: the label already names the pass. A
shimmer on it would make the multi-pass engine visible as motion rather than as
a HUD you have to open.

```css
.composing-label {
  background: linear-gradient(90deg, var(--muted) 40%, var(--text) 50%, var(--muted) 60%)
              0 0 / 300% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 1.8s linear infinite;   /* linear is right: it is continuous */
}
@keyframes shimmer { to { background-position: -200% 0; } }
```

**Built**, with two guards the sketch above lacks. It sits inside an
`@supports` for `background-clip: text`, because the effect rests on `color:
transparent` — without the clip that is not a label missing a shimmer, it is no
label at all. And it is overridden under reduced motion rather than merely
stopped, since a frozen gradient leaves the text painted with whatever slice it
halted on. Verified: transparent fill and a 2s sweep normally, solid muted text
with the animation stopped under `reduce`.

### 2.2a The cue that says *which* kind of waiting

The shimmer says something is happening. It cannot say what, and for a
reasoning model the difference matters more than anywhere else in the app: no
visible token arrives until the model stops thinking, which on a local one is
routinely a minute, and three bouncing dots for a minute read as a dead
connection.

**Built.** The dots now mean only "the backend has not answered". Once
reasoning starts arriving the cue becomes a different object — a ring sweeping
one way, three sparks orbiting the other and firing in sequence, a core
breathing underneath — and the label becomes "Thinking…". The two are stacked
in one box and cross-fade, so the change is something you watch happen rather
than a glyph swapped between two frames, and the hidden one is paused rather
than left running where nobody can see it.

The ring's arc is driven by `--think`, a 0→1 saturating measure of how much
reasoning has landed. Saturating on purpose: nothing knows how long a thought
will run, so it must never look like a progress bar about to complete. It says
"a while", not "80%".

Counter-rotation earns its place: three dots pinned in a triangle read as a
face at 22px, and the first version did. Two more notes for anyone touching it.
The sparks state their orbit position as a static transform as well as in the
keyframes — under reduced motion the animation is cut to one 1ms iteration and
the element falls back to its unanimated state, which without that put all
three at the centre in a pile. And `--think` is declared on the cue box, not on
the element that reads it: a custom property set on an element beats the one it
would inherit, so the fallback silently pinned every thought at zero.

### 2.3 Switching chats has no transition and no loading state

`openChat()` replaces `messages` wholesale. The old conversation vanishes and
the new one appears in one frame, and if the fetch is slow there is nothing on
screen at all. No skeleton, no shimmer, no crossfade — I checked.

**Suggestion, in order of ambition:**

- **Cheap:** fade the column out and the new one in, staggered from the bottom
  (newest first, since that is where the eye is).
- **Better:** a skeleton — three to five grey bars at decreasing widths,
  shimmering. Reported to cut *perceived* load time by ~40% against a blank
  panel ([groovyweb](https://www.groovyweb.co/blog/ui-ux-design-trends-ai-apps-2026)).
  On a phone hosting its own database this is a real wait, not a hypothetical.
- **Ambitious:** a View Transition (see 5.1) so the header name and portrait
  morph between chats instead of cutting.

**Built** to the middle option. Four bubbles alternating sides with uneven line
lengths — a skeleton that does not match what replaces it is just a different
kind of flicker — sweeping on a linear gradient, each row a beat behind the one
above. The shape is fixed rather than random: a placeholder that reshuffles
draws attention to itself, and the one thing it must not do is look like
content arriving. Raised only when the chat is actually changing, so reopening
the one already on screen does not blank it. The View Transition version is
still open.

### 2.4 The state bands never move  — **built**

Trust and mood change every turn and the bands just re-render with new text.
Since §6 says raw numbers never reach a prompt but the *panel* may show them,
a band that slides between its old and new position — with a brief accent
flash on the one that moved — would make the engine's work visible. Right now
the most distinctive thing the app does is invisible unless you are reading
carefully.

### 2.5 Small ones on the same surface

- **Message delete** — **built.** The bubble already faded; the row it sat in
  kept its height until the element left the DOM, so everything below still
  snapped up in one frame at the end. The row now collapses, and the column gap
  with it, which height alone does not touch. Measured 93px to 0 over 14
  distinct heights.
- **Variant swipe** — **built.** Forward arrives from the right, back from the
  left, 14px of travel: a step, not a page turn, and text that travels far is
  text you cannot read on the way. Driven from JavaScript rather than a class,
  because a class has a cascade fight to lose against `.body.regen` and a
  subtree to be skipped in; the easing and duration still come from the tokens.
  Measured 14px to 0 over 14 distinct positions.
  - Worth knowing: every bubble holds **two** `.body` elements, and the first
    is the hidden regeneration cue. `querySelector('.body')` finds the one with
    no box.
- **Scroll-to-bottom** — still open.

---

## 3. Panels and lists

### 3.1 Stagger the sheet contents

The sheet slides up as one block. Its sections arriving 40–50ms apart would
make it read as a panel assembling rather than a slab landing. The empty state
added this session already does this — it is the pattern to copy, not to invent.

**Built**, at two levels down rather than one: each panel's body is wrapped in
a single div by its own `x-if`, so `.sheet-body > *` is that one wrapper and
there is nothing there to stagger. Five steps, then everything below arrives
together — past the fold there is nothing to stagger for. Measured: 18 frames
where the sections sit at different opacities.

### 3.2 The lists that reorder are hand-rolled FLIP

Four places measure `getBoundingClientRect()`, mutate, invert and release. It
works and it is well commented. But **View Transitions do this natively** and
handle the cases the manual version does not — an element that changes size
while moving, or one that enters and leaves in the same frame (5.1).

Not urgent. Worth knowing the manual code has a replacement when it next needs
touching.

### 3.3 Number fields have no feedback — **built**

Dragging a slider changes a number with no motion on the value. A brief scale or
colour pulse on the readout confirms the drag registered — this is the
"every action responds within 100ms" rule, and 19 sliders × 8 passes is a lot of
surface with nothing acknowledging the touch.

---

## 4. Gesture physics

### 4.1 Pull-to-impersonate ignores velocity

`setReveal((pullFrom - clientY) / PULL_DISTANCE)` — position only. Release below
the threshold and it returns to zero on a fixed curve regardless of whether the
finger was still moving.

A **flick** should complete the action even if it did not travel far enough,
because that is what the gesture meant. Track `dy/dt` across the last few
`touchmove` events and treat velocity above a threshold as arming. This is the
one place where springs genuinely beat curves — the settle should carry the
finger's momentum, not restart from rest
([Google Chrome modern-web-guidance](https://github.com/GoogleChrome/modern-web-guidance/blob/main/skills/modern-web-guidance/guides/ui-behaviors/physics-based-easing.md)).

**Built.** The last five touch samples are kept; a release above 520px/s
commits, provided the pull had already passed 35% — a fast flick from a
standing start at the bottom of the chat is a scroll that had nowhere to go,
not a gesture. Samples older than 160ms are discarded, so a finger that moved
fast and then held still counts as held. Releasing without committing now
springs shut instead of snapping: `--reveal` is registered with `@property` so
it can be interpolated at all, and the panel stays mounted through the settle,
which is the "keep it mounted until it has finished" tax `CLAUDE.md` warns
about. Measured: identical 52px pulls, **fast commits and slow does not**, and
the settle traces 12 distinct heights with a visible rebound.

### 4.2 Haptics are used twice and could be used well

`navigator.vibrate` appears exactly twice — the wheel arming and one other.
Worth adding, sparingly: the reply's first token landing, a destructive action
arming, a swipe committing to a new variant. Android honours `vibrate`; iOS
ignores it, which is fine since the target is Termux.

Rule of thumb: haptics for **state changes you did not look at**, never for
ordinary taps.

**Built**, through one `buzz()` helper so the rule lives with the code rather
than in six call sites: a reply's first token (6ms), a band moving (8ms), a
variant landing (8ms), a destructive action arming (14ms), the wheel arming
(14ms). Silent when the page is not in front of you — a buzz from an app you
are not looking at is a notification, and this is not one — and silent when the
motion dial is at zero.

---

## 5. Platform APIs worth adopting

### 5.1 View Transitions

Same-document transitions are **Baseline Newly available** — Chrome 111+,
Safari 18+, Firefox 144+
([web.dev](https://web.dev/blog/same-document-view-transitions-are-now-baseline-newly-available),
[MDN](https://developer.mozilla.org/en-US/docs/Web/API/View_Transition_API)).
The API wraps a DOM mutation and animates between the before and after states:

```js
document.startViewTransition(() => { /* mutate as normal */ });
```

Best fits here: switching chats (portrait and name morph rather than cut),
opening a character from the roster into the editor, and the four reordering
lists. `view-transition-class` lets you style a group without naming each
element.

Caveat: it needs `!document.startViewTransition` fallbacks, and it does not
compose with the manual FLIP — it would replace it, not sit beside it.

### 5.2 Scroll-driven animations

`animation-timeline: view()` runs off the main thread, driven by scroll position
rather than time
([Chrome](https://developer.chrome.com/docs/css-ui/scroll-driven-animations),
[WebKit](https://webkit.org/blog/17101/a-guide-to-scroll-driven-animations-with-just-css/)).
**Not Baseline** — Firefox stable has not shipped it — but irrelevant if the
target really is Chrome on Android.

Fits: messages fading in as they scroll into view, the header condensing as the
transcript scrolls, a reading-progress hairline on long chats. Off the main
thread matters on a phone that is simultaneously running the model.

### 5.3 `@starting-style` and `transition-behavior: allow-discrete`

These let an element transition **from** `display: none` without JS. Directly
relevant: the sheet, the wheel and the composer menu all currently need JS to
keep an element mounted until its exit finishes — `CLAUDE.md` calls this out as
a standing cost ("which usually means keeping the element mounted until it has
finished"). This is the CSS feature that removes it.

---

## 6. Deliberately not suggested

Recorded so they are not proposed again:

- **An animation library.** The deploy target compiles dependencies on an
  Android CPU. `linear()`, View Transitions and WAAPI cover everything above
  with no build step and no dependency.
- **Page-load or splash animation.** It is a PWA opened dozens of times a day;
  anything ceremonial on launch becomes an obstacle by the third time.
- **Animating the backdrop.** It sits behind text at 70% fade. Motion behind
  reading text is the one place motion actively hurts.
- **Parallax anywhere.** Costs main-thread work on a phone already running a
  model, and is the first thing reduced-motion users turn off.

---

## If you only do three

1. **§0** — invert the reduced-motion block. It is a bug and it is four lines.
2. **§1.1** — duration tokens, read from CSS in JS. Kills a documented
   two-file sync hazard and makes everything else easier to write.
3. **§2.1** — fade in streaming tokens. Most-watched surface in the app,
   currently the least animated.
