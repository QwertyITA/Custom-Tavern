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

## Quick start

```bash
pip install -r requirements.txt
python3 -m uvicorn app.main:app --port 8787
# open http://localhost:8787
```

A fresh clone runs end to end with **no network, no keys and no Ollama**: the
built-in `echo` backend answers every pass deterministically, so the engine,
streaming, panels, HUD and cost accounting are all live immediately. Point a
tier at a real backend when you have one:

```bash
cp data/settings.example.json data/settings.json   # then edit
```

On the phone, use the launcher — it applies the Termux hardening from DESIGN §2
(wake lock, foreground notification, tmux):

```bash
./run.sh          # start      ./run.sh logs   # follow      ./run.sh stop
```

Install as a fullscreen PWA from Chrome's menu ("Install app"). Reach it at
`localhost:PORT` on-device only — `IP:port` is not an installable origin, and
there is deliberately no auth layer.

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
