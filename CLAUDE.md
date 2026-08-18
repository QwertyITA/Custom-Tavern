# Project instructions

## This repository is PUBLIC — never commit a credential

Everything pushed here is world-readable, immediately and permanently. A key
committed and then removed in a later commit is **still leaked**: it stays in
the history, in forks, and in GitHub's event feed, which is scraped
continuously. Deleting it afterwards is not a fix; rotating it is.

**Never write a real credential into any tracked file.** That includes API
keys, tokens, passwords, private keys, cookies, session ids, and URLs with
credentials embedded (`user:pass@host` style).

Real credentials belong in **`data/settings.json`**, which is gitignored.
`data/settings.example.json` is the tracked template and must only ever contain
empty strings or obvious placeholders. The one exception already in the repo is
`"api_key": "0000000000"` — the AI Horde anonymous key, which is published in
their documentation and is not a secret.

Also never:

- put a credential in a commit message, a branch name, or a PR description;
- paste a key into a test fixture, a docstring, or an example in the README —
  use `sk-EXAMPLE...` or `<your-key>` shapes that the guard recognises;
- add a real `base_url` that embeds a token in the path or query string;
- `git add -f` anything matched by the secrets block in `.gitignore`.

If a credential does reach a commit, **rotate the key first**, then worry about
the history. Tell the user immediately and plainly.

### The guard

`.githooks/pre-commit` blocks commits containing credential-shaped content. It
is enabled automatically by `start.sh`, or by hand:

```bash
git config core.hooksPath .githooks
```

It checks two things: paths that must never be tracked (`data/settings.json`,
`.env`, `*.pem`, `id_rsa*`, …), and secret-shaped strings in the staged diff.
Placeholders, empty values and all-zero values pass. `--no-verify` bypasses it
— only use that after confirming the match is genuinely false, and never to
push a real key.

`tests/test_secrets.py` guards the ignore rules themselves, so the protection
cannot regress silently.

## What this project is

A phone-hosted roleplay frontend built on a conditional multi-pass engine.
**`DESIGN.md` is the source of truth for the architecture** — read the relevant
section before changing engine behaviour, and keep the `§` references in code
comments accurate when you touch the code they describe.

The load-bearing ideas, each of which has a test protecting it:

- The reply pass never tracks state; separate passes do (§1).
- Expensive passes gate on cheap rubric signals from pass 1 — this is the cost
  lever, not an optimisation detail (§5.2).
- Volatile prompt content goes **last** so the stable prefix's KV cache
  survives across turns (§7.1).
- Raw numbers never reach a prompt; bands resolve to guidance text (§6).
- Write arbitration is per-slice by source turn only. No global commit DAG
  (§5.5).
- Messages are dropped only after summary *and* memory have covered them (§7.2).
- State binds only to the swipe variant you land on (§9).
- The markup tokenizer fails soft on unbalanced markup (§8).

## Conventions

- **Dependencies:** the deploy target is a phone. Adding a dependency means
  someone compiles it on an Android CPU — `pydantic-core` already costs ten
  minutes of Rust. Do not add one without a clear reason, and never anything
  needing a build step on the frontend.
- **Tests:** `python3 -m pytest`. Hermetic — no network, no extra dev
  dependencies. The `echo` backend answers every pass deterministically; use it
  rather than mocking providers.
- **Tokenizer parity:** `app/markup.py` and `static/markup.js` must stay
  behaviourally identical. `tests/fixtures/markup_cases.json` is the contract;
  regenerate it if the rules change, and check both sides against it.
- **Frontend:** vanilla JS + Alpine, no build step. Alpine is vendored at
  `static/vendor/`. Render model output with `textContent`, never `innerHTML`.
- **Motion:** nothing moves linearly. Use `--ease-out`, `--ease-in-out`,
  `--ease-back` or `--ease-spring` from `:root`; do not write a bezier inline.
  A linear transition is the one curve nothing physical follows, and it reads
  as a slide show rather than as something moving. Anything that appears,
  moves, grows or leaves gets a transition — including on the way out, which
  usually means keeping the element mounted until it has finished.
  - `--ease-spring` is a real damped oscillator built with `linear()`, not a
    bezier. Use it where something **lands** and comes to rest — the wheel, a
    sent message, a switch knob, a released gesture. A bezier can pass its mark
    once; this crosses back and settles, which is the difference between
    arriving and landing. Never on a fold: a panel that bounces open reads as
    broken.
  - **Durations come from tokens too**: `--dur-fast` / `--dur-base` /
    `--dur-slow`, plus five named ones the JS waits on. If JavaScript needs a
    duration it calls `dur("name")`, which reads the token — never a literal.
    The literals drifted last time: a 420ms animation was being waited on for
    460ms.
  - **Linear is right in exactly three places**, all continuous with no start
    or end to ease between: the refresh spinner, the composing-label shimmer
    and the skeleton sweep. Easing any of them makes it hesitate once per
    cycle.
  - **`content-visibility: auto` on a message row silently kills animations
    inside it.** Not just painting — style and layout for the whole subtree, so
    `getAnimations()` hands back a live animation whose effect is never
    computed and nothing moves. Add the row to the `content-visibility:
    visible` list *before* the animation starts; a `:has()` rule is too late,
    because the animation begins in the frame the subtree is still skipped in.
    Also: every bubble holds two `.body` elements and the first is the hidden
    regeneration cue, so `querySelector(".body")` finds the one with no box.
  - **Reduced motion is a wildcard, not a list.** `@media
    (prefers-reduced-motion: reduce)` stops everything with `*` and then names
    the few exceptions. Do not add per-selector entries — the allowlist version
    fell behind by two dozen animations before it was noticed.
- **Every interaction answers.** Nothing tappable stays still when touched.
  Three responses, by what the control is: `press` (a scale-down, for buttons),
  `lift` (a background wash, for rows and tiles too big to scale), `ring`
  (`:focus-visible`, for anyone not using a finger). Adding a control means
  adding its response.
- **Glass is a layer, not a palette.** `--surface*` tokens are what every
  frostable surface reads; `:root.glass` redefines them with `color-mix` against
  transparent, so it only ever changes how *solid* a surface is and never what
  colour. Never give glass colours of its own — it has to work over all nine
  presets and any hand-picked set. Text, borders and icons stay fully opaque:
  blurring what someone is reading is the one place this hurts.
  - **Transparency is not what reads as glass.** The first version was
    translucent and blurred and still looked like a pale rectangle, because the
    backdrop is flat vector art — blurring an even colour field returns the
    same even colour field. What the eye actually uses is the **rim, the shadow
    and the sheen**: a lit top edge, a drop shadow proving the pane floats, and
    a highlight raking across the face. Those work over flat art as well as
    over a photograph.
  - **The slider runs frosted → clear, and blur runs *down* as it opens.**
    Frosted glass is opaque and heavily diffused; clear glass is transparent
    and sharp. Raising transparency and blur together — the obvious first
    guess — gives a thin sheet of fog, which is neither of them.
  - **The thinner the pane, the less of our colour reaches the eye.** Rim,
    sheen, inset highlight and text halo all scale by `--glass-keep`; at full
    transparency only the drop shadow survives, because a pane you can see
    straight through still casts one and that is the last cue that it is a pane.
    A rim sized for a near-solid card is a white smear on a thin one.
  - `--glass-solid` is a **unitless number**, not a percentage — a percentage
    cannot be subtracted from 1, and doing it anyway silently voids every rule
    derived from it with no error anywhere.
  - The text halo scales the opposite way to everything else: nothing at the
    frosted end, real work at the clear one. A frosted pane already separates
    the words from the room and a halo on top of that reads as a bloom filter;
    a clear pane separates nothing, and then the halo is all that holds the
    words together, the way a subtitle is set over a film.
  - Anything drawn in `--muted` disappears over a photograph. Under glass the
    tools, arrows and world line climb towards `--text` — but `:not(.danger)`,
    because delete is red for a reason.
  - Keep `backdrop-filter` saturation near 1. Pushing it with a brightness lift
    turns a warm photograph orange; the room should look like the room.
  - The switch eases both ways, which needs the class **added before** the
    values move and **removed after** they finish. Otherwise `backdrop-filter`
    jumps between `none` and a value in one frame. Dragging the slider does not
    animate — the value already tracks the finger.
- **Icons:** one SVG sprite at the top of `index.html`, referenced with
  `<use href="#i-name">`. Never an emoji: it is drawn by whichever font the
  phone happens to ship, so a row of them arrives in several weights and will
  not take the theme colour.
- **Destructive actions** arm on the first tap and act on the second, and say
  so. A modal over a sheet on a phone is its own problem.
- **Secrets in logs:** `Settings.to_dict()` masks `api_key`, and the
  `/api/settings` endpoint relies on that. Keep it masked.
