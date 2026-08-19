# Personal Tavern — Design Document (v2)

> A personalized, phone-hosted SillyTavern alternative built around a conditional
> multi-pass engine. Working codename: **Personal Tavern** (rename freely).
> Status: design / pre-build. Single source of truth for the build.
> v2 changes: corrected concurrency model, inline-markup rendering (replaces the
> segments model), eviction ladder, memory store, swipe branching, prompt-assembly
> pipeline, per-backend templates + sampling, pass I/O contract, failure handling,
> cost accounting.

---

## 1. Core philosophy

**Director / actor split.** The main model only *performs* the reply. It never tracks
emotional/world state in the same prompt — that is what makes complex presets blunder.
State tracking is offloaded to separate passes; the actor's prompt stays small.

**Conditional multi-pass execution.** Any number of passes run beyond the reply, each
an independent job with its own trigger, model tier, sampling profile, and output
target. Passes do not all run every turn; expensive ones gate on cheap signals from
pass 1. This is the cost lever.

**Speculative execution, eventual consistency.** Pass 1 commits a *provisional* state
immediately so the reply isn't blocked; later passes audit/correct it before the next
turn, or whenever they land. Independent facts (weather, summary, scene) simply update
their own panel on arrival — order among them is irrelevant.

---

## 2. Feasibility check

All verified.

| Item | Verdict | Constraint / rule |
|------|---------|-------------------|
| FastAPI + uvicorn on Termux | Works | `pip install uvicorn` (skip `[standard]`) |
| Phone as server + client | Works | Access at `localhost:PORT` on-device, never by `IP:port` |
| Fullscreen "web wrapper" | Works | Installed PWA, `"display": "fullscreen"`; localhost http is installable. Fallback: TWA / WebView APK |
| Background pass survival | Conditional | Doze needs screen-off; our passes run screen-on. Inference for background passes must be **remote**, not on-phone |
| On-device small model (S24 Ultra) | Works | Foreground only; heavy/hot, throttled when backgrounded |
| AI Horde | Works | Network-only → safest background tier; slow, latency-tolerant only |

**Termux hardening:** `termux-wake-lock`, battery *Unrestricted*, `termux-notification`
foreground notice, run under `tmux`, raise phantom-process limit via ADB on Android 12+.

---

## 3. Topology & compute tiers

Everything lives on the S24 Ultra; only inference leaves the phone.

```
S24 Ultra
├─ PWA (fullscreen, localhost)          ← client
├─ FastAPI + uvicorn (localhost:PORT)   ← orchestrator + pass scheduler
├─ SQLite (WAL mode)                    ← chats, messages, state slices, memory, logs
└─ data/characters|music|backgrounds/   ← file assets
        ├── (blocking, fast)  → PC Ollama over Tailscale  /  OpenAI-compatible API
        ├── (foreground, mid) → on-device small model (llama.cpp in Termux)
        └── (background, free) → AI Horde
```

**Tier assignment by latency tolerance, not importance.** Blocking-fast → Ollama/API;
foreground-mid → on-device; background-free → Horde. Never fire-and-forget an on-device
model with the screen off.

---

## 4. Request flow (one turn)

```
1. User sends message.
2. Prompt assembly builds pass-1 context (§7.1) within token budget.
3. BLOCKING passes run in data-dependency order:
     Pass 1 "basic" streams the marked-up reply, then emits a <<<state>>> suffix
     block (§5.6) carrying rough deltas + signals (§5.2, rubric-based).
4. Reply renders live (inline markup parsed at display, §8). Suffix stripped +
     parsed. Provisional state committed. "typing…" → done.
5. NON-BLOCKING passes evaluated against triggers + signals. Eligible ones queue
     and run in parallel across tiers; each writes its own slice / panel on arrival.
6. On next send: any still-running blocking-relevant pass awaited briefly; else
     proceed on provisional and let corrections land later.
```

---

## 5. The pass system

### 5.1 Pass definition

```jsonc
{
  "id": "state_auditor",
  "kind": "canonical" | "custom",
  "trigger": { ... },              // §5.2
  "model_tier": "blocking" | "foreground" | "background",
  "sampling": { "temp": 0.2, "top_p": 0.9, "rep_penalty": 1.1 },  // per-pass
  "blocking": false,
  "prompt": "…task text…",
  "output": { ... },               // §5.4
  "depends_on": ["basic"],         // DATA dependency only (§5.5)
  "writes_slice": "state.emotional"
}
```

### 5.2 Triggers

`every_turn` · `every_n(k)` · `on_signal(name, op, threshold)` · `timer(seconds)` ·
`manual`. Signals come cheaply from pass 1 so costly passes decide their own worth.

**Signals use rubrics, not raw floats.** Models self-score numbers inconsistently.
`narrative_drive` reports `none | minor | major`, not `0.0–1.0`. Far more stable.

### 5.3 Canonical vs custom passes + animation

- **Canonical** passes are predefined and each carries a **bespoke animation**.
  Initial set: `basic` (reply), `weather`, `summary`, `background_swap`,
  `expression` (pfp emotion), `memory` (§7.3).
- **Custom** passes default animation by type: blocking → **cogs**; background →
  **ambient** panel-refresh indicator.
- **Failure** has its own state on every pass: a distinct failure animation on the
  HUD row and the target panel (muted/error indicator), with retry (§5.5).

Resolution: `canonical ? own : (blocking ? cogs : ambient)`; overlaid by run status
(pending / running / done / **failed**).

### 5.4 Output targets

`none` (prompt influence only) · `state_modifier(slice)` · `gui_panel(id)` (renders to
a panel, never the message stream) · `action_card(type)` (confirmation card in chat; future).

### 5.5 Concurrency, ordering & safety

The corrected model — simpler than a global commit DAG:

- **Parallel execution.** Eligible passes fire concurrently across tiers.
- **Independent write-on-arrival.** Passes writing to *different* slices
  (`state.scene`, `state.summary`, `state.weather`, `memory`) update the moment they
  land — order among them is irrelevant, whoever finishes first just updates its panel.
- **`depends_on` = data dependency only.** Used only when a pass needs another's
  *output as input* (e.g. `background_swap` consumes the scene slice). Not a write-order
  mechanism.
- **Stale-write rejection — only within a shared slice.** When two passes target the
  *same* slice (e.g. pass 1's provisional `state.emotional` vs the auditor's corrected
  one), each write is stamped with its source turn; an older-turn write is rejected.
  This is the sole arbitration, and it's per-slice.
- **DB writes:** SQLite in **WAL mode**, all writes funneled through a single queue to
  avoid "database is locked" under parallel passes.
- **Failure/retry:** blocking reply fails → fall back a tier or surface a graceful
  error; background fails → retry N times then mark `failed` in the HUD.

### 5.6 Pass I/O contract (streaming vs structure)

Resolved: dialogue-vs-action is **render styling, not structured output** (§8), so pass 1
streams clean prose and structure rides in a suffix.

- **Default — delimited suffix.** Model streams the marked-up reply, then emits
  `<<<state>>>{…json…}` . The stream renders live; on close, the suffix is parsed for
  deltas/signals and stripped from display.
- **Fallback — two-call.** For models that won't emit the suffix reliably: stream the
  reply, then a cheap second call extracts signals. Doubles blocking latency; used only
  when needed.
- **Reasoning models:** if a pass runs a local `<think>` model, the think block is
  captured and hidden (optionally shown in the HUD), never displayed inline. It
  arrives in one of two shapes: inline in the text, or — where the backend parses
  it for us, as Ollama does — on a separate channel that never enters the stream
  at all. Both are captured; a diagnosis that reads only the stream calls the
  second one "returned nothing".
- **Reasoning is spent from the reply's budget**, so a thinking model can use a
  whole pass reasoning and answer with an empty message over a successful
  request. Where the backend has a switch for it, it is per-backend and **off by
  default** (§13).

---

## 6. State & variable model

**Static — in the character card.** Variable schemas + behavioral rules, defined once:

```jsonc
"state_schema": {
  "willingness": {
    "min": 0, "max": 10, "baseline": 5, "decay": 0.15,
    "bands": [
      { "range": [0,3],  "label": "guarded", "guidance": "resistant, deflects, needs convincing" },
      { "range": [4,6],  "label": "neutral", "guidance": "engages if asked, won't volunteer" },
      { "range": [7,10], "label": "eager",   "guidance": "leans in, initiates, generous" }
    ]
  }
}
```

**Dynamic — in the session (SQLite).** Live values are per-chat, per-turn, per-slice,
versioned by source turn. `willingness = 3 at turn 40` belongs to the conversation.

**Band interpretation.** Never inject raw numbers; resolve the band in code and inject
the *guidance text*. Decay toward baseline runs deterministically each turn (zero LLM
cost). Rule-based keyword/regex nudges adjust values before any model pass — cheapest tier.

---

## 7. Memory & context management

### 7.1 Prompt assembly pipeline (the "advanced prompt manager")

Context is assembled each turn in a fixed order, trimmed to a token budget:

```
[stable prefix — cache-friendly, rarely changes]
  system / persona
  lorebook static entries
[dynamic middle]
  triggered lorebook hits
  relevant memories (§7.3)
  rolling summary (§7.2)
  recent verbatim messages (eviction window)
[volatile suffix — changes every turn, placed LAST to preserve KV cache]
  current state bands
  active toggle injections
```

**Cache rule:** volatile content (state, toggles) goes *last* so the stable prefix's
KV cache survives across turns on local Ollama. Token budget is enforced by the
eviction ladder, not by hard-cutting the prefix.

### 7.2 Eviction ladder (context decay)

Not flat FIFO — a tiered decay so dropped messages condense rather than vanish:

```
verbatim window   → last N messages, full text
      ↓ (age out)
summarized        → collapsed into the rolling summary by the summary pass
      ↓ (summary grows)
compressed        → summary itself re-summarized as it exceeds its own budget
      ↓
dropped           → only after important facts are promoted to Memory (§7.3)
```

The summary pass is what makes eviction safe: durable facts leave the verbatim window
as memories, not as loss.

### 7.3 Memory store (non-blocking `memory` pass)

A background pass extracts durable facts ("user's sister is Anna", "character promised
to return the knife") into a persistent store that survives the eviction ladder and is
injected back during assembly.

- **Retrieval:** keyword-match first (reuses lorebook machinery, zero new infra).
  Embedding similarity is a later upgrade if recall disappoints. *[changeable]*
- **Scope:** per-character. *[changeable — shared-across-chats is the alternative]*
- **Lifecycle:** extract → dedupe → store; promoted before source messages are dropped.

### 7.4 Lorebook

Keyword-triggered context injection (the classic "World Info"; distinct from the
Scene Tracker in §10). Spec: trigger keys, insertion depth (where in assembly),
scan depth (how many recent messages scanned for keys), per-entry and total token cap,
constant vs triggered entries. Character greetings (`first_mes`, example dialogue) load
here at chat start.

---

## 8. Message rendering & inline markup

Dialogue vs action is **not** a stored message type — it's inline markup parsed at
render time from one prose string. Rules, each with a custom colour:

| Markup | Meaning | Style |
|--------|---------|-------|
| `"text"` | spoken dialogue | dialogue colour |
| `*text*` | action / narration | action colour (italic) |
| `**text**` | emphasis | bold |
| plain | default narration | default colour |

Markup **nests and interleaves freely** within a message (`*action "quote" more*`,
`"speech *emphasis*"`). Requirements:

- A real **tokenizer**, not naive regex — nested and mixed runs must colour correctly.
- **Graceful degradation** on unbalanced markup (stray/unclosed `*` or `"`, which
  models produce): fail soft, never miscolour the rest of the message.

Custom colours attach to these parser rules. The `Message` model stores **raw text
only** — no `segments` field.

---

## 9. Swipe / regeneration / edit

Each swipe is a **branch**, and state binds only to the accepted variant.

- **Swipe** generates an alternative reply. The current variant + its provisional
  state slices are **stashed**; generating the new variant **rolls back** those slices
  first. Only the variant you **land on** commits state and triggers background passes.
- **Edit** (manual edit of the AI message): text updated in place; optionally re-run
  the auditor pass against the edited text.
- Never let state from discarded swipes accumulate — that is exactly the corruption the
  versioning model exists to prevent.

---

## 10. Toggle system

Declarative objects; a new behavior = a new object.

```jsonc
{ "id": "avoid_yes_person", "label": "Avoid yes-person",
  "target_pass": 1, "injection": "…own agenda, not agreeable by default…",
  "output": "none", "scope": "global" | "per_character" | "per_chat" }
```

| Toggle | Pass | Task | Output |
|--------|------|------|--------|
| Avoid yes-person | 1 | anti-sycophancy | none |
| Anti-slop | 1 | no repetition, no user-turn continuation, vary phrasing | none |
| Scene Tracker | background | describe setting / weather / time | gui_panel(scene) |
| State auditor | background | validate deltas vs personality + context | state_modifier |

**Naming:** "Scene Tracker" is the setting/weather/time generator. "World Info" = the
**Lorebook** (§7.4). Kept distinct on purpose.

---

## 11. Data models

```
Character   : id, name, version, pfp_set{emotion→img}, backgrounds[{img, metadata}],
              persona, state_schema, lorebook_ref, default_toggles[]
Chat        : id, character_id, version, created_at, settings(colours, toggle overrides)
Message     : id, chat_id, turn, role, text(raw markup), variants[], active_variant, edited
StateSlice  : chat_id, turn, slice_name, value(json), source_turn
Memory      : id, character_id, text, keys[], created_turn, source
PassDef     : (§5.1) — global library + per-character enable/config
PassRun     : id, chat_id, turn, pass_id, tier, model,
              status(pending|running|done|failed|stale), tokens_in, tokens_out,
              started_at, finished_at     ← powers HUD + cost accounting
Toggle      : (§10)
Lorebook    : id, entries[{keys[], content, insertion_depth, constant}]
```

Cards live as files in `data/characters/`; v2/v3 `.png` import maps into this schema.
`version` fields on Character/Chat drive schema migrations (§17).

---

## 12. GUI specification

Fullscreen PWA, phone layout, custom colours for every element.

**Layout (top → bottom):** (1) pinned **world-info bar** — place · weather · time,
time generic ("early afternoon", never "14:56"), per-field refresh animation.
(2) **chat area** — messages as rectangles, AI pfp on its messages, inline markup styled
per §8. (3) **edit button** on AI messages; **swipe** for variants (§9). (4) composer.

**Animation modes:** *Composing* (blocking pass gating reply) → "typing…" for pass 1,
cogs for other blocking passes. *Ambient* (background pass) → subtle panel indicator,
no character-thinking cue. Canonical passes use their own animation; **failed** passes
show the failure indicator.

**Pass-status HUD (debug, toggleable):** every pass this turn with status / tier /
model / token counts.

**Canonical GUI passes:** `background_swap` (metadata-tagged backgrounds swapped on
scene change) and `expression` (emotion→pfp sprite selection) — both background,
`gui_panel` output.

---

## 13. Backends, templates & sampling

- **Per-backend instruct templates.** ChatML / Llama3 / Mistral formatting differ;
  each backend/model gets its prompt template or output degrades. Provider abstraction
  selects the template.
- **Per-pass sampling profiles** (§5.1): auditor = low temp / near-deterministic;
  actor = creative. Set independently of tier.
- **Thinking is a per-backend switch** (`think`: off / auto / on), off by default,
  for the backends that expose one. Reasoning is not free output — it comes out of
  the pass's token budget (§5.6) — so leaving it on by default makes a working
  setup look broken. `auto` sends nothing and leaves it to the model's template,
  which is also what an older backend that rejects the field falls back to.
- **Output post-processing:** a regex/cleanup stage on the reply before display —
  strip artifacts, trailing user-turn leakage. Part of anti-slop lives here, not only
  in the prompt.

---

## 14. Observability & cost

The conditional-pass thesis is about cost, so measure it. `PassRun` logs tokens in/out
per pass; a dashboard shows spend per pass/turn/chat and proves the gating works. Same
observability instinct as a GPU overlay, applied to token spend.

---

## 15. Future features (after basics)

- **Proactive actions.** A pass emits an `action_card`: *"ABC asks you to play Def.
  Listen?"* → accept plays a local file from `data/music/`. Generalizes to any
  proactive action.
- **Image generation.** ComfyUI at `:8188` over Tailscale as a pass output; images
  inline or in panels.
- **Group chats.** Turn-taking (who replies), per-character state slices, whose
  expression/background wins, shared vs private world state. State namespacing must go
  per-character before this is built. *(Stubbed for now, by decision.)*

---

## 16. Build roadmap

- **Phase 0 — skeleton.** Termux server, SQLite (WAL), fullscreen PWA shell, one
  blocking reply pass, one backend (Ollama via Tailscale). Chat end to end.
- **Phase 1 — engine.** Pass scheduler + triggers, state slices (versioned) +
  stale-write rejection, band interpretation, toggle engine, card schema + import,
  inline markup parser, suffix I/O contract.
- **Phase 2 — memory & tiers.** Prompt assembly + eviction ladder, summary pass,
  memory store, lorebook, canonical passes (weather/scene, auditor, expression,
  background), three tiers (Horde + on-device), per-backend templates, Termux hardening.
- **Phase 3 — GUI polish.** Animation system (incl. failure), world-info bar, markup
  colours, swipe/edit + state rollback, pass HUD, cost dashboard.
- **Phase 4 — future.** Action cards (music), ComfyUI images, group chats.

---

## 17. Stack, hygiene & migrations

- **Backend:** FastAPI + uvicorn, SSE streaming. **DB:** SQLite (WAL), single write queue.
- **Frontend:** vanilla JS + Alpine, **no build step**; installed as a fullscreen PWA.
- **Inference:** provider abstraction over Ollama / OpenAI-compatible / Horde /
  on-device llama.cpp; tier + template + sampling are per-pass settings.
- **Export** presets and character cards (portable backups — chat histories are
  disposable by decision; only install files + presets are backed up).
- **Schema versioning:** `version` on cards + DB; migration steps so new variables
  don't break old chats.
- **`.claudeignore`** at repo root (data/, assets, models, venv) for future work sessions.

---

## 18. Still open

1. Initial canonical variable set per character (willingness, trust, mood, energy…?).
2. OpenAI-compatible API(s) for the blocking-fast fallback.
3. On-device model choice (Qwen2.5-3B vs Llama-3.2-3B).
4. Colour system: per-character themes vs one global palette with per-element overrides.
5. Memory retrieval/scope confirmed as keyword-first + per-character *(veto if wrong)*.
