# Personal Tavern

A personalised, phone-hosted roleplay frontend built around a **conditional
multi-pass engine**. The full architecture lives in [DESIGN.md](DESIGN.md);
this file is how to run it and where things are.

The central idea (DESIGN §1): the main model only *performs* the reply. It never
tracks emotional or world state in the same prompt — that is what makes complex
presets blunder. State tracking is offloaded to separate passes, each with its
own trigger, model tier and sampling profile, and expensive passes gate on cheap
signals emitted by the reply pass. That gating is the cost lever, and the cost
dashboard exists to prove it works.

## Install on Termux

Install Termux **from F-Droid or GitHub, not the Play Store** — the Play Store
build is years stale and its package repo is dead. Then:

```bash
pkg update && pkg upgrade -y
pkg install -y python git tmux termux-api
```

`termux-api` is what lets the launcher take a wake lock and post the foreground
notification. Also install the **Termux:API** app itself (same source as
Termux) — the `termux-api` package alone is only the CLI half.

Now get the code and start it:

```bash
git clone https://github.com/QwertyITA/Custom-Tavern
cd Custom-Tavern
./start.sh
```

`start.sh` installs the Python dependencies on first run, then starts the
server and prints `http://localhost:8787`. Open that in Chrome.

**If pip fails on `pydantic-core`:** PyPI ships no Android wheel for it, so it
compiles from Rust source. Install the toolchain and run the script again —
expect ten minutes or so, once:

```bash
pkg install -y rust binutils
./start.sh
```

Two Termux-specific things `start.sh` handles for you, so install Rust through
`pkg` rather than rustup:

- maturin derives the Rust target triple from Python's SOABI and gets
  `aarch64-unknown-linux-android`, which rustup does not recognise. Termux uses
  `aarch64-linux-android`. `start.sh` exports `CARGO_BUILD_TARGET` to match your
  architecture before calling pip.
- If Rust is missing entirely, maturin tries to bootstrap rustup into a temp
  directory and fails on that same unsupported triple, which is why the error
  says "Rust not found" even though installing rustup would not have helped.

### Keep it alive

Android will kill a long-running server unless you tell it not to. The launcher
takes a wake lock, posts a foreground notification and runs under tmux, but two
things need doing by hand, once:

1. **Battery: Unrestricted.** Android Settings → Apps → Termux → Battery →
   Unrestricted. Without this nothing else matters.
2. **Phantom process killer** (Android 12+). Android reaps background child
   processes after a while, which kills the server out from under tmux. Turn it
   off over ADB from a computer, once:
   ```
   adb shell settings put global settings_enable_monitor_phantom_procs false
   ```

### Install as an app

In Chrome, open `http://localhost:8787` → menu → **Install app**. It installs
as a fullscreen PWA with its own icon and no browser chrome.

Reach it at **`localhost:PORT` on the phone itself** — not `IP:port`. Only
localhost counts as a secure origin for PWA install over plain http, and there
is deliberately no auth layer, so the port is not meant to be exposed.

### Home-screen button

Install the **Termux:Widget** app, then:

```bash
./start.sh --widget
```

That puts "Personal Tavern" and "Personal Tavern (stop)" in `~/.shortcuts`.
Add the Termux:Widget widget to your home screen and the app is one tap away —
it updates and starts.

## Everyday use

```bash
./start.sh              # update, then run in this console   ← the one to tap
./start.sh -b           # same, but detach into tmux and give the prompt back
./start.sh --no-update  # start without pulling (offline, or pinning this code)
./start.sh stop         # stop
./start.sh logs         # follow the log
```

The server runs **in the console by default**, with the request log in front of
you; Ctrl+C stops it cleanly and releases the wake lock. A server you cannot
see is one whose errors you find out about much later.

`-b` detaches into tmux instead, so closing Termux does not take the server
down — worth it once you are just *using* the app rather than changing it.
Reattach with `tmux attach -t tavern`. Either mode stops the other first, so
the two can never fight over the port.

`start.sh` pulls the latest version before starting, and reinstalls
dependencies only when `requirements.txt` actually changed. Three things it
will not do: touch your data (`data/tavern.db` and `data/settings.json` are
gitignored, so a pull cannot overwrite chats, characters or settings), stop the
app because an update failed (no signal still starts what you have), or discard
local edits silently (a dirty worktree skips the pull and tells you).

## Quick start (desktop, for development)

```bash
pip install -r requirements.txt
python3 -m uvicorn app.main:app --port 8787
```

A fresh clone runs end to end with **no network, no keys and no Ollama**: the
built-in `echo` backend answers every pass deterministically, so the engine,
streaming, panels, HUD and cost accounting are all live immediately. Point a
tier at a real backend when you have one:

```bash
cp data/settings.example.json data/settings.json   # then edit
```

## Credentials

Set backends and keys in the app: **menu (☰) → settings & API keys**. Assign
each tier a backend, fill in the URL/model/key, and use *test connection* to
check one before relying on it. Everything is written to `data/settings.json`
on the device.

The key handling is deliberate:

- Saved keys are **never sent back to the browser**. A read returns `***`, and
  submitting `***` unchanged means "keep the stored value" — so you can change
  a model without retyping a key, and the page never holds one to leak.
- The file is written **atomically and `0600`**, created with those permissions
  rather than chmod-ed afterwards, so the key is never briefly world-readable.
- A failed connection test **masks the key out of the error text**, since a
  `base_url` can carry a token.

**This repository is public.** Real API keys go in `data/settings.json`, which
is gitignored; `data/settings.example.json` is the tracked template and holds
only placeholders. Anything committed here is world-readable the moment it is
pushed, and stays in the history and in forks afterwards — removing it later
does not un-leak it, only rotating the key does.

Two things enforce this rather than relying on memory:

- `.githooks/pre-commit` blocks commits containing credential-shaped content —
  tokens, private keys, `user:pass@host` URLs, and non-placeholder `api_key`
  assignments — as well as paths like `data/settings.json` and `.env` even when
  forced past `.gitignore` with `git add -f`. `start.sh` enables it on first
  launch; by hand it is `git config core.hooksPath .githooks`.
- `tests/test_secrets.py` checks the ignore rules, the key masking on
  `/api/settings`, and the hook's own verdicts, so none of it can regress
  quietly.

## Tests

```bash
python3 -m pytest        # 145 tests, hermetic, no network, no extra deps
```

The JS tokenizer is checked against the same fixtures as the Python one:

```bash
node -e '
  const M = require("./static/markup.js");
  const cases = require("./tests/fixtures/markup_cases.json");
  const bad = cases.filter(c => JSON.stringify(M.parse(c.input)) !== JSON.stringify(c.runs));
  console.log(bad.length ? bad : "JS/PY parity OK");'
```

## What a turn actually does

```
user sends
  ↓
decay + regex nudges          zero tokens, cheapest tier (§6)
  ↓
pass 1 "basic" [BLOCKING]     streams the reply, then a <<<state>>> suffix
  ↓                           carrying rough deltas + rubric signals (§5.6)
reply renders live            markup parsed at display; suffix stripped
provisional state commits     the next turn is never gated on what follows
  ↓
triggers evaluated            expensive passes gate on pass 1's signals (§5.2)
  ↓
eligible passes run in parallel across tiers, each writing its own slice
on arrival — order between different slices is irrelevant (§5.5)
```

Watch it happen: open the ⚙ HUD. Every pass this turn shows its tier, model,
status and token counts, and the cost panel breaks spend down per pass.

## Layout

| Path | What it is |
|------|-----------|
| `app/passes/scheduler.py` | the engine — triggers, gating, parallel execution, swipe branching |
| `app/passes/registry.py` | canonical pass + toggle library |
| `app/passes/contract.py` | `<<<state>>>` suffix contract, including the streaming filter |
| `app/state.py` | slices, bands, decay, nudges, stale-write rejection, rollback |
| `app/assembly.py` | prompt assembly pipeline + eviction ladder |
| `app/markup.py` | inline markup tokenizer (mirrored in `static/markup.js`) |
| `app/memory.py`, `app/lorebook.py` | memory store, World Info |
| `app/providers/` | Ollama · OpenAI-compatible · Horde · llama.cpp · echo |
| `app/cards.py` | v2/v3 card import (JSON + PNG), export |
| `static/` | the PWA — vanilla JS + Alpine, no build step |

## Design decisions worth knowing

**Volatile content goes last.** State bands and toggle injections change every
turn; the persona and constant lorebook do not. Putting the volatile block last
keeps the stable prefix's KV cache alive across turns on local Ollama. This is
the reason for the assembly order, not a stylistic choice.

**Raw numbers never reach a prompt.** A value resolves to a band in code and the
band's *guidance text* is injected. `willingness: 2` becomes "resistant,
deflects, needs convincing".

**Signals are rubrics, not floats.** Models self-score `none | minor | major`
far more consistently than `0.0–1.0`. Floats that arrive anyway are mapped onto
the ladder rather than rejected.

**Arbitration is per-slice and by source turn only.** Two passes writing
*different* slices never contend — whoever lands first updates its own panel.
Two passes writing the *same* slice are ordered by the turn they came from, and
an older-turn write loses. There is no global commit DAG.

**`depends_on` is a data dependency, not a write order.** `background_swap`
waits for `scene` because it consumes the scene slice, and only when `scene` is
actually running this turn.

**A message is only dropped once it has been covered.** The eviction ladder is
verbatim → summarized → dropped, and dropping waits for *both* the summary and
memory passes, so durable facts are promoted before their source disappears.

**State binds only to the swipe you land on.** Generating a variant first rolls
back the previous variant's state writes, using the `prev_value` recorded in the
write log. Discarded swipes never accumulate.

**The markup tokenizer fails soft.** Unbalanced markup — which models emit
constantly — degrades to literal text and never miscolours the rest of the
message. Marker pairing is also bounded to a paragraph, so one stray `*` cannot
capture everything after it.

## Choices made against DESIGN §18 (open questions)

| # | Question | Decision |
|---|----------|----------|
| 1 | Canonical variable set | `willingness`, `trust`, `mood`, `energy` — overridable per card |
| 2 | Blocking-fast fallback | any OpenAI-compatible `/v1/chat/completions` endpoint |
| 3 | On-device model | Qwen2.5-3B-Instruct (ChatML), via llama.cpp's server |
| 4 | Colour system | one global palette in CSS variables, per-character overrides |
| 5 | Memory retrieval/scope | keyword-first, per-character — as specified, veto still open |

## Build status against DESIGN §16

- **Phase 0 — skeleton.** Done.
- **Phase 1 — engine.** Done: scheduler, triggers, versioned slices,
  stale-write rejection, bands, toggles, card schema + import, markup parser,
  suffix contract.
- **Phase 2 — memory & tiers.** Done: assembly pipeline, eviction ladder,
  summary/memory passes, lorebook, all canonical passes, four providers,
  per-backend templates, Termux hardening.
- **Phase 3 — GUI polish.** Functional, not yet polished: animations (including
  failure state), world-info bar, markup colours, swipe/edit with rollback, pass
  HUD, cost dashboard all work. The visual design is deliberately undercooked
  and is the next thing to iterate on.
- **Phase 4 — future.** Not started: action cards, ComfyUI images, group chats.

## Not built yet

- Group chats — state namespacing must go per-character first (§15).
- Action cards and image generation (§15).
- Embedding-based memory retrieval — the keyword path is the agreed starting
  point (§7.3).
- Preset export/import beyond character cards (§17).
