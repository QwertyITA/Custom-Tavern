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
  captured and hidden (optionally shown in the HUD), never displayed inline. It is
  **kept**, on the variant it produced, and read back from the message's hold menu —
  "did it actually think, and what did it decide" is the question a reasoning model
  raises every single turn, and a think block that is counted and then dropped leaves
  it unanswerable a minute later. It arrives in one of two shapes: inline in the
  text as a `<think>` block, or — where the backend parses it for us, as Ollama
  does — on a separate reasoning channel that never enters the stream at all.
  Both are captured; a diagnosis that reads only the stream calls the second one
  "returned nothing". A third arrives with **no opening tag at all**: Ollama and
  llama.cpp serve models whose chat template writes the `<think>` itself, so the model
  emits only the closer and the reply begins mid-thought. A closer with nothing to
  close therefore means everything before it was reasoning — including whatever has
  already been streamed, which the client is told to take back off the screen.
- **Reasoning is separated as it arrives**, not once the reply is complete.
  Inline, that is what makes "never displayed inline" true of the live stream and
  not only of the stored message: the think block used to sit in the bubble for
  the length of the generation and disappear when the finished text landed.
  Either shape then raises a `reasoning` event carrying a character count and
  never the text, which is the only thing that can tell a model that is thinking
  from a backend that has not answered — while a model reasons, not one visible
  token arrives, so the two are identical from the client. The composing cue
  reads it: dots for waiting, a different cue entirely for thinking, deepening
  as the count grows.
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

**Known, accepted cost: assembly is O(chat length), not O(context window).**
Every turn calls `repo.list_messages` for the *whole* chat and walks it to find
the verbatim window and the pinned opening (§7.2) — there is no `LIMIT` at the
query. Measured on a synthetic chat: 2,000 messages costs ~17ms for the list and
~34ms for full assembly; 10,000 messages costs ~86ms and ~170ms, blocking the
event loop for the duration since SQLite access here is synchronous. Audited and
left as-is on 2026-08-21: it is fast enough at the sizes a real conversation
reaches, and a windowed query is a known, straightforward fix (`ORDER BY turn
DESC LIMIT N`, plus the opening row) if a chat ever grows large enough for it to
matter. Worth re-checking after §7.2's own change: eviction now waits for real
token pressure rather than a message count, so a chat under its budget keeps
growing in the verbatim table for longer than it used to before anything is
ever summarized out of it.

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

**It ships switched off.** The ladder starts at "the summary has covered this turn", so
with the pass off nothing is ever evicted and a chat under its context budget keeps
every word it actually said. That is the right default because a summarised message
leaves the prompt *permanently* — the stage is stored, and no later setting brings it
back — which makes the summary the only surviving account of everything it covers.
Written by the cheapest model in the stack it is routinely wrong about the premise and
about who did what, and then that is what the character knows. `memory` (§7.3) stays
on either way, because a durable fact is worth carrying forward whether or not
anything is being thrown away.

**Everything here answers pressure, not a count.** Three rules, and each one exists
because its absence was measured on a real chat holding 3788 tokens against a 32768
budget — an eighth of it — that had already lost its opening message:

- **The verbatim window is a floor, not a cap.** `verbatim_window` is the number of
  recent messages kept in full *at minimum*; above it the budget decides how far back
  the conversation goes. As a hard count it also slid by one message every turn once a
  chat passed it, which moved the start of the conversation and took the KV cache with
  it on every single turn.
- **The opening message is pinned.** It is the scenario — where everyone is, what the
  arrangement is, who these people are to each other — and nothing later in a chat
  restates any of it. Being turn 0 it was otherwise always the first thing evicted,
  and its absence is what turns a character into someone who does not know where they
  are. Neither the window, the trimmer nor the ladder may take it.
- **Nothing is evicted while the prompt still fits.** Eviction is permanent, so it
  waits until the last assembled prompt was at 85% of the budget. A caller that cannot
  say how full the prompt was evicts nothing.

The summary pass answers the same signal: its trigger is `over_budget`, and it covers
only messages that have actually left the window. Covering turns still in the prompt
was worse than useless — the summary sits *above* the conversation and reads as
established fact, so a cheap model's misreading contradicted the exchange itself a few
hundred tokens further down.

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

### 7.5 The writing library

Some prefix sections ship with their **own text** rather than filling a slot: how
prose reads, how people talk, what a character is allowed to know, how long a reply
runs. They are ordinary sections — reorderable inside the prefix, switchable, and
editable, with only the edit stored so an untouched block follows the app.

They live in the **prefix** because they never change, which is the same reason the
persona does: the KV cache is built on them once and reused (§7.1).

Adapted from a SillyTavern preset (*Freaky Frankenstein 5.2*, the Internal States /
BOLT setup), which is a stack of toggles a person picks from. Three kinds of block did
not port, for reasons that are structural rather than editorial:

- **State through the reply.** Internal-state HTML blocks, chain-of-thought gates,
  notebooks, inventories, relationship trackers. This engine tracks state in separate
  passes (§1, §5.5), and the reply pass staying ignorant of it is what makes the reply
  cheap. Asking the reply to print a state block would be the design this one is a
  reaction to.
- **Markup by instruction.** Coloured dialogue asks the model for `<font>` tags. Here
  dialogue colour is a render-time property of the markup tokenizer (§8), themed by the
  user, so the tags would arrive as visible text. What the app *does* have to state is
  the convention itself — quotes for speech, asterisks for everything else — and that
  is `craft:format`, the one block in the library that lives in the volatile band
  rather than the prefix. It is the rule the renderer depends on, and a small model
  follows the instruction it read last, which is worth the few tokens it costs to
  recompute each turn.
- **Refusal bypasses.** Not shipped as a default. The card's own system prompt and a
  custom block are both there.

### 7.6 Card size and compression

§7.1's rule — only the *middle* is ever trimmed, never the prefix a card supplies —
means a card whose own identity content is simply larger than a backend's budget is
not something assembly can fix at request time. Two things exist to make that
visible and, where it can be, fixable, instead of surfacing only as a character who
never seems to remember anything:

- **`Provider.context_limit()` clamps, it does not merely fall back.** A backend that
  can genuinely be asked what it holds — Ollama and llama.cpp probe the live server
  (`/api/ps`, `/api/show`, `/props`); Horde's `_probe_context` checks whether the
  currently-selected model(s) are named in a cached `/status/models` call and reports
  the smallest context any of them carries there, falling back to Horde's own hardcoded
  API ceiling when none is — now has that answer win over a *larger* configured
  number, not only stand in for a missing one. A configured number still wins when it
  is the *smaller* of the two: asking for less than a backend could give is a choice,
  not a mistake to correct. `assembly.fit_token_budget` is the shared arithmetic
  (`settings.token_budget` tightened to what a backend actually holds, reply cost and
  safety margin already spent) — used both by a live turn (`PassScheduler._fitted`) and
  by the two checks below, so a warning shown before anyone has sent a message means
  what a real turn would actually find out.
  A model must actually be selected for this to mean anything on Horde in the first
  place — with none picked, Horde's real API rejects the job outright rather than
  choosing one on its own, so at least one is required to Save a Horde backend at all
  (§config.py `_merge_secrets`), and `HordeProvider.generate` guards the same thing
  again for whatever reaches it some other way (an older settings file, one edited by
  hand). `Provider.list_models_detail` is what makes picking one worth doing: every
  backend reports at least a name, and Horde also reports each model's queue position
  and ETA — the estimated wait a job would actually see — and its own `parse_models`
  sorts by that ETA rather than worker count, quickest first, with a model reporting no
  ETA sorting after one that does rather than before it. `/status/models`'s documented
  schema is name/count/performance/queued/eta, not a context size, so the per-model
  context lookup above stays best-effort by design: real when a deployment's response
  happens to carry one of the common field names anyway, quietly absent rather than
  guessed at when it does not.
- **`assembly.mandatory_cost`** is a card's own floor — prefix plus the two volatile
  writing blocks that need no chat state (`craft:format`, `craft:length`) — computed
  with no conversation in it at all and no `db`/`chat` dependency, so it can be asked
  about any card at any time. `assembly.card_too_big` compares that floor against a
  live, fitted budget and flags a card once less than `MIN_CONVERSATION_HEADROOM`
  (§ assembly.py, ~400 tokens — roughly two or three ordinary exchanges) would be left
  for the conversation. `GET /api/characters/budget` runs this for the whole roster
  against whatever the Messages/blocking tier is actually configured to right now; the
  roster draws a warning badge on any character it flags.
- **Compression previews a shorter card, it does not rewrite one.** `POST
  /api/characters/{id}/compress` (`app/card_compression.py`) asks the Messages-tier
  backend to shorten `persona`, `scenario`, `example_dialogue` and `system_prompt` —
  the same four fields `update_character` can already save, chosen specifically so
  applying a result never needs new machinery to write it back — by roughly however
  far under `MIN_CONVERSATION_HEADROOM` the card currently sits, split across fields
  in proportion to their own size and never asking a field below a third of itself.
  A field whose "compressed" answer comes back empty or no shorter than the original
  keeps the original untouched — this never makes a card bigger. Nothing is saved by
  the preview itself: the editor's own pinned Save is what commits whatever a person
  reviewing the result decides to keep, the same "generate, then the ordinary Save
  path" shape `character_reactions.py` already uses for anything else an AI writes
  onto a card. Lorebook entries are not offered here — they already have their own
  mechanical backstop that runs on every turn with no review step at all (below) and
  there is nowhere in the editor yet to save a rewritten entry back to.
- **A lorebook entry's own `token_budget` is an actual cap, not just an accounting
  figure.** `lorebook.render` cuts an entry's rendered content to `token_budget`
  (word-boundary safe) rather than emitting it in full — `scan`'s total-budget
  accounting always assumed that cap was real; it was the one thing that made it true.

### 7.7 The context meter ("What was sent")

The itemised prompt record (`repo.save_prompt_record`/`prompt_record`) now carries a
`budget` alongside its `parts`: the same fitted ceiling `PassScheduler._fitted` computed
for that turn via `assembly.fit_token_budget` — not the raw configured
`settings.token_budget`, and not a re-derivation, since the record is a record of what
actually happened, not a live re-assembly (the same reasoning §7.6 gives for why a
card's own headroom check reuses this arithmetic rather than inventing a second copy of
it). "What was sent" draws a stacked meter from it: total used vs. that budget, split
into the three §7.1 bands (`prefix`/`middle`/`volatile`) by summing the same per-part
rows the accordion below it already groups by band — one grouping function
(`sentBands()`) feeds both, so the meter and the row-by-row breakdown can never
disagree about which parts a band counted or what its label means. Stored as
`{"parts": [...], "budget": N}` rather than the bare list it used to be; a record
written before this shipped still parses (`budget: null`) and the meter is simply
omitted for it — every other row keeps working exactly as before. The three band
colours (`--band-prefix`/`--band-middle`/`--band-volatile`) are a dedicated triplet
rather than a reuse of the existing `--c-dialogue`/`--c-action`/`--c-strong` markup
colours: those already carry a different, adjacent-in-the-same-app meaning, and —
checked against `validate_palette.js`, not assumed — fail the CVD normal-vision floor
in every shipped theme. Only the Night preset needs its own dark-stepped values;
the other four share the light set.

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
- **Repaired before it is stored.** Failing soft is right for rendering and wrong to
  keep: an asterisk that could have opened or closed something and found no partner is
  a mistake, and `clean_reply` deletes it. The rule is the tokenizer's own, so the two
  cannot disagree, and a marker that could do neither — `2 * 3` — was never markup and
  stays. Measured over one real chat, 26 of 47 replies carried at least one.
- **The convention is stated in the prompt**, by the `craft:format` block, which lives
  in the volatile band. It was a clause of the `instruction` slot's fallback, so a card
  with its own system prompt replaced the only statement of it — and the twelve craft
  blocks between it and the conversation contain no asterisk between them.
- **Tags this app cannot draw are stripped**, from replies and from imported cards
  alike. Output is rendered with `textContent`, so an `<img>` arrives on screen as its
  own source and costs prompt tokens on every turn to do it.
- **The client redraws only the open paragraph, not the whole reply.** Markers only
  pair inside one paragraph (`_paragraphs` in both tokenizers), so nothing arriving
  after a `\n\n` can restyle text before it — the same rule that bounds a stray
  marker's damage also bounds what a new token can invalidate. `static/markup.js`'s
  `render()` uses this: once a paragraph closes it is drawn once and never touched
  again; only the still-open paragraph is re-parsed and redrawn each frame. It used to
  rebuild the whole message every frame, which is right for a finished message and
  quadratic for a streaming one — measured streaming an 800-word reply in the same
  browser, same steps: 4.8s total and 6.7ms for the last frame before, 137ms and
  0.1ms after. `render()`'s own comments carry the reasoning; there is no Python
  equivalent to keep in step, since only the client ever redraws incrementally.

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
Character   : id, name, version, pfp_set{emotion→img}, pfp_shape(portrait|square),
              pfp_effect(hue, saturate, brightness, contrast, sepia, grayscale),
              reactions(starred, unstarred, killed),
              avatar_video(enabled, idle_video, voice, prep_status) (§20),
              backgrounds[{img, metadata}],
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

A `pfp_set` entry is either the card's own bundled art (a bare relative path, served
from the tracked static tree, never touched by the app) or a picture cropped through
the app itself (`/avatars/<file>`, written into the gitignored `data/avatars/`, shared
with persona pictures through the same upload endpoint). Deleting a character deletes
the second kind — but only once nothing else still points at the same file, since the
directory is shared and a filename surviving the character it was cropped for is not
proof nothing wants it.

`pfp_effect` is a CSS filter recipe (hue, saturate, brightness, contrast, sepia,
grayscale) for a colour **ring around** that same picture, not a filter over the
picture itself — a hue-rotated element hue-rotates everything it renders, so the ring
is its own element (`.pfp-glow`) sitting behind the photo, never sharing a box with it.
Same "belongs to the character, not the app" reasoning as `pfp_shape`, and drawn
everywhere the shape is (the roster, the chat, the enlarged view). `reactions` holds
three 5–8 word in-character lines (a reaction to being starred, unstarred, and
permanently deleted) that the story never asks for but the app needs; starring or
unstarring a character shows its line as a speech bubble over that character's own
roster row rather than the plain toast every other action gets, when the line exists yet
— see `showReactionBubble` in `static/app.js`. `app/character_reactions.py` generates
whichever of the three is empty, using the backend behind the Messages tier, and never
overwrites one that already has something in it — generated or typed by hand.
Generation is attempted once at card import and, for whatever is still missing, once
more after every reply a character sends (queued after the reply, never blocking it) —
both fire-and-forget, so an unreachable backend at import time is not a failed import,
just a retry that has not landed yet.

---

## 12. GUI specification

Fullscreen PWA, phone layout, custom colours for every element.

**Layout (top → bottom):** (1) pinned **header** — the menu button and who you're
talking to. (1b) **world-info pill** — place · weather · time and their refresh button,
its own rounded chip directly below the header rather than sharing its line; time
generic ("early afternoon", never "14:56"), per-field refresh animation. Hidden outright
(not merely covered) while the header's own menu is open, so it never bleeds through
the opening buttons under glass. (2) **chat area** — messages as rectangles, AI pfp on
its messages, inline markup styled per §8. The portrait is framed to the character's own
`pfp_shape` — 2:3, the shape a card is drawn in, or square — chosen when the picture is
uploaded and cropped to there and then, ringed with whatever `pfp_effect` colour the
character has (§11) — both apply everywhere the picture does: the roster, the chat, the
enlarged view. Tapping one enlarges it and takes the width from the bubble beside it;
enlarged, it offers the whole screen. The one exception: a character with a talking
avatar switched on (§20) shows a lip-synced video instead, over the one reply it was
just rendered for, and only there — every other appearance of the same picture stays a
still image. (3) **edit button** on AI messages; **swipe** for variants (§9). (4) composer.

**Characters roster:** characters listed above personas ("You"), not below — the roster
is what the panel is for. Tapping a row opens the chat that character was last active in
(falling back to a new chat if it has none) — the roster is a contact list, not an index
you have to open a sub-menu from. A starred character's whole card carries a soft
animated gold glow, the roster's one purely celebratory touch. Starring or unstarring
one shows its own reaction line (§11) as a WhatsApp-style speech bubble over that row,
pointing at its portrait, for a few seconds — the plain toast every other action gets is
the fallback for whichever direction that character has no line for yet. Deleting a
character is the one destructive action in the app that gets a modal (everywhere else,
two taps):
the second tap on an already-armed delete opens it, and the character is gone only once
"Delete" has been held for seven full seconds, filling a track underneath it — release
early and it resets rather than closing, so a second attempt is another hold, not two
more taps on the row behind it.

**Composer `+` menu:** a tap opens it and a second tap closes it, same as any other
sheet; a press that keeps going and drags onto an item selects that item on release,
mirroring the message-bubble hold gesture (§9) at a smaller scale. Both are the same
press — only what the finger does after it lands decides which one happens.

**Header menu:** three destinations — **Brain**, **Theme**, **Characters**. Story used
to be a fourth; it is now a tab inside Brain (below), because it was one settings screen
among several rather than a peer of "where the work is sent" and "what it looks like".

**Brain panel:** a tab bar of four icon-labelled categories rather than one long
scroll, chosen because the old flat layout mixed "configure a backend" with "which
passes run" with "what this story remembers" with "advanced numeric knobs" in one
list with no way to jump between them.
- **Backends** — the backend CRUD block, web search, and which backend each pass
  group is assigned to (moved out of Passes so "what does the work" lives in one
  place with "where the work goes").
- **Passes** — pass editing: the three canonical groups always shown, the reply
  group's on/off switch drawn muted and disabled rather than as a live control
  (there is no reply without it, so it cannot actually be turned off, and it
  should not look like it can be), and a stub "Add a pass" button (not
  implemented). "What goes into the prompt" (band/section layout) lives here
  too; it moved as-is, not redesigned — its own layout is a separate, open
  discussion, flagged in place rather than fixed on the side.
- **Story options** — the former standalone Story panel, unchanged, including that
  it saves each field immediately rather than through the panel's Save button
  (which is why that button is hidden while this tab is open).
- **Advanced** — languages, find-and-replace, and the remaining advanced numeric
  fields: everything left over once the other three categories claimed their own.

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

---

## 20. Talking avatar (AVATAR-VIDEO-CONTRACT.md)

A lip-synced video plays over a character's portrait for the one reply it
was rendered for, from a service the user runs themselves — a real-time
lip-sync model (MuseTalk and similar) needs a GPU, which this app's deploy
target (a phone) never has (§2). `AVATAR-VIDEO-CONTRACT.md` at the repo
root is the full HTTP contract; this section is why it is shaped the way
it is and why it sits outside every mechanism described above rather than
reusing one of them.

**Not a provider.** §13's provider abstraction (Ollama / OpenAI-compatible
/ Horde / on-device) exists for one shape of call: a prompt and a sampling
profile in, text out, against a backend assigned to a tier. An avatar
render has none of that — no prompt, no sampling, no tier, and its request/
response shape (submit a loop or a line of text, poll a job, get back a
video URL) doesn't fit `GenRequest`/`GenResult`. It follows the *other*
existing shape for "an external, self-hosted, non-LLM service you point a
URL at" instead — `app/websearch.py`'s, config held as flat `_url`/`_key`
fields on `Settings` rather than a `BackendConfig`, off until both a URL
and a per-character switch are on, silent on failure.

**Not a pass.** §5's pass system exists to decide, per turn, whether a
prompt-shaped call is worth making, against rubric signals and a trigger.
An avatar render has no such decision to make — it always happens, once,
right after a reply whose character has the switch on — and it never
writes a state slice. It follows `app/character_reactions.py`'s shape
instead: fire-and-forget from the scheduler, right after `_run_reply`,
same as the reaction-line retry sitting beside it.

**Two phases, one asset.** `Character.avatar_video` (§11) holds `enabled`,
`idle_video` (an uploaded loop, same "belongs to the character, lives in
`data/`, not the tracked static tree" reasoning as a portrait) and
`prep_status`. `prepare()` is the slow, one-time step — the service's own
face-detection/parsing/encoding pass over the idle loop — kicked off once
per upload; `render_for_reply()` is the fast, per-line step that assumes
`prep_status == "ready"` and does nothing otherwise. Skipping the split
and re-preparing on every line would cost the entire reason a real-time
lip-sync model is worth using at all.

**One video at a time, and never a replay.** Only the message a render just
landed for ever shows a `<video>` — every other row, in every context
(scrollback, the roster, the enlarged portrait, group chats), always shows
the ordinary static portrait `pfp_set` already draws (§11, §12). The moment
that one clip ends, the row reverts and nothing about it is remembered: no
video URL is persisted on the `Message`, so scrolling back to an old reply
never re-fetches or replays anything. This is a phone-performance decision
as much as a design one — at most one decode/playback is ever live, which
matters for exactly the reasons `.msg`'s `content-visibility` scroll
optimisation does (§12's GUI note on the message list).
