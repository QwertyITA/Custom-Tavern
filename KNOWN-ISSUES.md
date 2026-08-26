# Known issues

Everything here was found during two audit passes over the running app, not
read off the source and guessed at. Where a number appears, it was measured;
where a behaviour is claimed, it was reproduced against a live server — most
recently on 2026-08-25, re-verified against the code as it stands after this
session's bug-fixing pass, not against an earlier snapshot.

This file exists so a real defect does not have to be rediscovered before it
can be weighed against everything else waiting for attention, and so a
deliberate "not now" is written down as a decision rather than left looking
like an oversight.

**Severity** is about *consequence*, not effort to fix:

- **High** — real breakage or resource exhaustion reachable in ordinary use.
- **Medium** — a genuine defect, but narrow, rare, or with a self-recovery path.
- **Low** — cosmetic, disk-only, or negligible at the scale this app runs at.
- **Not a defect** — model behaviour the app cannot fix, or a design choice
  made on purpose and flagged for the person who might disagree with it.

Fixed this session: the streaming renderer's quadratic redraw and a real
correctness bug in the first attempt at fixing it (§markup.js, DESIGN.md §8);
a character's portrait was never deleted with the character (§repo.py,
app/main.py, DESIGN.md §11); a regex rule guard that let the textbook
catastrophic-backtracking pattern straight through (`(a+)+$`); the app saying
"Failed to fetch" and losing a typed message when the server was unreachable;
the character roster's fixed round avatars regardless of `pfp_shape` (§below,
now the same `.pfp-slot` the conversation itself draws with); the armed
delete glyph reading almost the same colour as its own background under
glass, a CSS specificity bug (`:root.glass .glyph-btn.danger` beating
`.glyph-btn.armed` on nothing but selector count). None of those are
repeated below.

Fixed in the mobile-UX pass on 2026-08-24: the composer `+` hold-drag
selecting text instead of picking an item, below —
`user-select: none`, `-webkit-user-select: none` and `-webkit-touch-callout:
none` added to `.composer-sheet`/`.composer-menu` (inherited by
`.composer-item`), the same three `.crop-stage` already carried, plus the
same three on `.kill-sheet` for the character-deletion hold, which shared the
exact gap. Still not independently reproduced live — a phone's native
long-press text-selection remains untriggerable from this headless setup —
so this is the diagnosis in the removed entry below acted on, not a
confirmed-fixed repro; genuinely verifying it wants a real touchscreen.
Also found and fixed while in the area, not from a prior report: `.pfp-glow`
across all five places it is drawn (message rows, the roster, the character
editor, the enlarged-portrait view, the header) used `x-show`, whose hide
path was not reliably clearing the element's `display` on these — the ring
could stay visibly rendered regardless of whether the character's
`pfp_effect` was actually on, and dropping in and out across a long chat as
other reactive updates raced it. Switched to `x-if` (full mount/unmount
instead of a style toggle) on all five, which does not exhibit the bug in
the same testing that reproduced it on `x-show`. §pinCurrentCharacter and
the roster/header rework are new features from that session's own request
list, not bugfixes, and are not recorded here.

Fixed in the bug-sweep on 2026-08-25 — every open item below except "The
craft library is mostly not being followed" (a prompt-tuning call, not a
code defect, left for the person whose library it is) and "A reply can
quote the user's own turn back" (the fix risks being worse than the
problem): upload endpoints now reject an oversized `Content-Length` before
reading the body at all, across all six routes that accept a raw upload
(`_reject_oversized`, app/main.py); `newChat()` now sets `this.error` like
every other network call in static/app.js; a per-chat `asyncio.Lock`
(`PassScheduler._chat_locks`/`_run_locked`) now turns away a second
run_turn/retry_turn/run_swipe/run_continue against a chat already
generating, rather than letting two run to completion against two different
prompts neither knew about the other; `_track()`'s done-callback now drops
the `_pending` entry once its task set is empty instead of leaving a bare
dict key behind forever; a per-chat `asyncio.Semaphore`
(`MAX_CONCURRENT_BACKGROUND_PASSES_PER_CHAT`, 8) now bounds how many
background passes can be calling a backend at once, gating only the actual
call rather than the bookkeeping around it; the character editor can now
crop-replace or remove any `pfp_set` entry, not only `neutral` — each other
sprite is held to the shape `neutral` already settled on rather than free to
introduce a mismatch of its own; replacing or clearing a portrait
(`update_character`) or a talking-avatar idle loop (`upload_avatar_idle`)
now deletes the file that slot used to point at, once nothing else still
wants it; and `run.sh` now reads `host`/`port` out of `data/settings.json`,
`TAVERN_HOST`/`TAVERN_PORT` still winning when actually set at the command
line. The five server-side fixes (upload size, the turn lock, the `_pending`
cleanup, the background-pass cap, the portrait/idle-loop cleanup) carry new
pytest coverage (`tests/test_api.py`, `tests/test_cards.py`,
`tests/test_chat_files.py`, `tests/test_portraits.py`,
`tests/test_scheduler.py`); the three frontend/shell ones — `newChat()`, the
emotion-sprite cropper, and `run.sh` — have none, this repo having neither a
JS nor a shell test harness, and were each instead checked by hand:
`newChat()` and the cropper in a browser (Alpine's own state read back after
each step, and screenshots for the cropper's UI); `run.sh` at a shell,
against all three host/port precedence cases (settings only, env override,
neither).

Fixed on 2026-08-26 — a triggered (keyed) lorebook entry never ran through
macro expansion, so `{{char}}`/`{{user}}` inside one reached the model as
the literal, unresolved placeholder text rather than a name (`app/assembly.py`
build_reply_context, the "lore" middle-band section). Constant entries — the
"World" prefix section — always expanded correctly; only the scanned/keyed
path was missed. Reproduced directly: a lorebook entry reading `"{{char}} is
secretly a wolfboy who likes {{user}}."`, once its key was mentioned, was
sent to the model exactly like that rather than with the character's and
persona's actual names substituted in. Found while investigating a real,
large card (Kutra, `character_book`, 38 entries) whose keyed entries — most
of them describing entirely different named characters (Wira, Isiya, Emas,
Sarpint, and more, not Kutra) — write `{{char}}` to refer to themselves; on
the broken code, triggering one of those keys mid-conversation (species
words like "wolfboy"/"catgirl", or the other characters' own names) sent the
model that other character's raw JSON sheet with the literal `{{char}}`
token left in it. Fixed by expanding it the same way the constant path
already did (`expand(render_lore(triggered))`); covered by
`test_triggered_lore_expands_macros_same_as_constant_lore`
(`tests/test_context.py`).

**This fix does not, on its own, make a card like that read as consistent.**
Once expanded, `{{char}}` resolves to whichever character is actually in the
chat — so a keyed entry written for a *different* character now attributes
that character's traits to the active one fluently and confidently instead
of visibly-broken raw template syntax, which is a worse failure to read
even though it is a strictly more correct implementation of what macro
expansion is supposed to do. The entries themselves are the actual defect,
not something either version of this code path can fix: a keyed lorebook
entry that is not about the chat's own character should never write
`{{char}}` for itself in the first place, constant or triggered — it should
name that other character literally, the way the app's own constant/prefix
convention already implicitly assumes every `{{char}}` in the book means
"the character this chat is about." Recorded, not fixed, in the entries
themselves — see the card-content note below.

Fixed on 2026-08-26 — a lorebook entry's own `token_budget` (its per-entry
cap, DESIGN.md §7.4: "per-entry and total token cap") was only ever used to
decide whether the entry *fit* the total budget during selection
(`lorebook.scan`'s `cost = min(estimate_tokens(entry.content),
entry.token_budget)`) — `render` then emitted the entry's full, uncapped
content regardless, so an oversized entry was charged its declared budget
but sent several times that. Reproduced against a real card (Kutra): one
lorebook entry with `token_budget: 200` rendered at **591 tokens** — over a
third of the Mini preset's entire 1536-token prompt budget spent on one
entry that was supposed to cost 200. Fixed by actually cutting the rendered
content to `token_budget` (word-boundary safe) in `lorebook.render`;
covered by `test_render_caps_an_entry_to_its_own_token_budget` and
`test_render_leaves_a_short_entry_untouched` (`tests/test_context.py`).

Fixed on 2026-08-26 — the AI Horde quick-setup presets (Mini/Standard/Max,
`static/app.js`) were conflating two different numbers under one name.
`backend.context` (Horde's `max_context_length`, a worker-eligibility floor
told to Horde's queue — asking for less does not shrink the prompt, it only
widens which workers qualify) was set to 4096 / 8192 / 32000 per tier, and
that same number was standing in for the thing actually meant: how much
*prompt* — card, craft library and conversation together — the app itself
writes before `assembly.py` starts trimming. Those never had separate
numbers. Fixed by pulling them apart: `backend.context` and the per-request
reply cap now sit at Horde's own ceiling (32000 / 512) on every tier, since
asking Horde for less than it allows was never the point — and
`settings.token_budget`, which `assembly.py` actually trims against, now
carries the real Mini/Standard/Max number (1536 / 2560 / 4608, i.e. 1-2k /
2-3k / 4-5k of prompt), set independently via the same one-shot settings
edit the writing-library toggles already used. `PassScheduler._fitted` only
ever tightens a configured `token_budget` toward what a backend can hold,
never loosens it, so a generous `backend.context` alongside a small
`token_budget` is exactly "give Horde's queue its normal run, but only ever
write this much prompt" rather than a contradiction between the two.

The writing-library selection was also retuned around correctness over
polish at the tighter prompt budgets (Mini: only `craft:autonomy` +
`craft:knowledge`, the two rules whose absence reads as broken rather than
merely plain), the card's own example dialogue — often the single most
expensive optional prefix section — now switches off below Max, and
`lorebook_total_budget`/`memory_max_injected` scale down with the tier the
same way. The two "not a matter of tier" blocks (`craft:format`,
`craft:length`) are untouched either way. All within the existing mechanism,
nothing auto-truncated.

Measured with the app's own assembly code against two real, moderately
detailed character cards and a 20-exchange conversation, on the fixed
numbers: Mini and Standard both send only the pinned opening message for
either card — none of the actual back-and-forth — while Max sends 21 of 41
(Nyami) or 30 of 41 (Kutra). That surfaced a second, unfixed thing, recorded
below under Not a defect: a verbose card's own identity content can, on its
own, still exceed a 1-2k or 2-3k prompt budget. Nothing here changes that —
see that entry for the full measurement and why the fix is the card, not the
preset.

**Correction, same day.** The fix above was itself still wrong about which
number the 1-2k/2-3k/4-5k target actually describes. `settings.token_budget`
caps the *whole* assembled prompt — prefix, lorebook, memories, summary and
conversation together (§7.1/§7.2) — not the card-and-writing-rules prefix
alone, so setting it to 1536/2560/4608 per tier capped the conversation right
along with the prefix: exactly the "no memory of what was just said" failure
this was supposed to fix, just moved one layer down. `settings.token_budget`
now sits at the same ceiling as `backend.context` on every tier, same as
`backend.context` itself — neither is where Mini/Standard/Max differ. The
1-2k/2-3k/4-5k figures are what they were always meant to be: a target the
*writing-library selection* (`HORDE_WRITING_*`/`HORDE_STRUCTURAL_*`,
`static/app.js`) holds itself to when choosing how much optional prefix
content each tier turns on — never a number written into a setting. The
`lorebook_total_budget`/`memory_max_injected` scaling from the fix above is
also gone: shrinking those was the same mistake in miniature, pre-emptively
narrowing what the *conversation's own supporting content* could use before
there was any real pressure on it to justify that.

Fixed on 2026-08-26 — AI Horde's real API rejects a job with no model named
at all rather than picking one on its own, and the app had no guard for
that: `HordeProvider.build_payload` quietly omitted the `models` field when
none was configured, so a Horde backend saved with nothing picked failed
only once someone actually tried to talk to it, with an unhelpful "models"
400 from Horde itself. Fixed with two guards: `config.py`'s Save validation
now rejects a Horde backend with neither `models` nor the singular `model`
set, and `HordeProvider.generate` checks the same thing again right before
submitting, for a config that reaches that point some other way (an older
settings file, one edited by hand) — `build_payload` itself stays
permissive, since every sampler-clamping test in `tests/test_providers.py`
builds a payload with no model at all and is testing something else
entirely. The settings screen's model picker (previously names only) now
shows Horde's own queue ETA next to each one and sorts fastest-first —
`Provider.list_models_detail`, `HordeProvider.parse_models`'s new sort key
— and applying an AI Horde quick-setup preset now opens the backend's model
picker and, when nothing is chosen yet, auto-loads and picks the fastest
one rather than leaving a backend that cannot be saved. A model actually
being selected also makes `context_limit()`'s per-backend probing mean more
for Horde specifically: `_probe_context` now checks whether `/status/models`
happens to report a context size for the selected model(s) (best-effort —
that endpoint's documented schema is name/count/performance/queued/eta, not
a context field, so this is a bonus when a deployment's response carries
one anyway, not a promise) before falling back to Horde's flat ceiling.

Fixed on 2026-08-26 — the Brain > Passes "which backend does each group's
work" `<select>` never actually displayed the tier's real backend, for any
tier, whether it was set by hand or by a quick-setup preset. Reported
against a preset (picking Mini after every tier pointed at a manually
configured "PC" backend appeared to leave the assignment on "PC"), but
verified with Playwright to be unrelated to presets at all: with no preset
involved, freshly loading a settings file that already had every tier
saved to "PC," the select's rendered `.value` still showed the *first*
backend in the list at every delay checked (100ms out to 2.5s) — the
underlying `settings.tiers` data was correct the whole time, only the
`<select>` never caught up. Root cause is an Alpine.js DOM-walk-order race:
Alpine applies an element's own directives (`x-model` here) while walking
*down* to that element, before it descends into a child `<template
x-for>` to actually create that element's `<option>`s — so the select's
initial value-sync runs with no matching `<option>` yet to select, the
browser silently falls back to the first one, and because Alpine only
re-fires a binding when the *bound value itself* changes again (not when a
sibling options list changes), that wrong display then persists
indefinitely. Confirmed a bare `x-effect` doing the same assignment hits
the identical race on its own first run — the fix needed is deferring past
Alpine's current DOM update, not just switching directives: `x-effect="…
&& $nextTick(() => $el.value = settings.tiers[g.tier])"` on the same
`<select>`, so the assignment lands after the `x-for` below it has
actually run, and re-applies whenever the backend list or the assignment
changes again afterwards. The per-backend model picker (`<select
x-model="b.model">`) was checked and is not affected — `modelOptions()`
already guarantees a matching `<option>` synchronously via its own
current-value fallback, so it has nothing to race.

Fixed on 2026-08-26 — moving `state_auditor`/`expression` onto the
background tier (§ post_process taking over foreground, replacing the
Refiner group) had a side effect on the Standard Horde preset nobody
decided on purpose: Standard turns background on but leaves foreground off,
and those two passes used to live on foreground specifically — which was
the one thing that made Max distinct from Standard, "reads the reply back
and corrects it" versus not. Once they moved, Standard picked them up for
free just for sharing a tier with scene/summary/memory, since a whole-tier
switch can't tell one background pass from another. Confirmed live: a
single turn under Standard's exact settings fired `state_auditor`, `scene`
and `expression` concurrently, which also made Standard's own tagline wrong
— it promises "the secondary-info pass is its own queued Horde request"
(singular), not three. Fixed with a second axis alongside `tiers_off`'s
whole-tier switch: `HORDE_AUDIT_PASSES` + each preset's own `auditsState`
flag (`static/app.js`) sets `state_auditor`/`expression`'s own `enabled`
directly through `PUT /api/passes/{id}` — the same per-pass toggle the
Brain panel's own switches use — independent of whatever else is happening
on background. `false` for Mini and Standard, `true` for Max, restoring the
exact three-way split the tier move had erased.

---

## Medium

### The craft library is mostly not being followed

Measured against one real chat (glm-4.7-flash, q4): the shipped writing
blocks that ship on by default total **1,759 tokens — 46% of the prompt** —
and ask for four to eight paragraphs, 400–600 words. The chat's actual
replies had a **median of 69 words and 2 paragraphs**. This is not a code
defect; it is a prompt tuned for capability the backend in that chat did not
have, and it is the user's writing library to trim, not something to fix out
from under them. Recorded here because "half the prompt budget is being
spent on instructions this model cannot hold to" is worth knowing, whichever
way it gets resolved.

---

## Low

### A reply can quote the user's own turn back

Measured over the same real chat: 1 of 47 stored variants echoed a phrase
from the message it was replying to mid-reply. Fixing it needs comparing the
new reply against the user's last message, and a character legitimately
repeating a phrase on purpose is a real thing that would be caught by the
same check — the fix risks being worse than the problem, so it is left alone
rather than guessed at.

---

## Not a defect

### Character names corrupt under a low-quantization backend

Observed in the same real chat: a character's name drifted across a
conversation (Kutra → Kuta, Kstra, Kruta …) under a q4 model. This is the
backend, not the app — nothing server-side rewrites names — and keeping the
scenario in the prompt (this session's context-window fix) should reduce how
often it happens without being able to prevent it outright.

### A large card can still eat an entire Horde budget on its own

Measured directly against two real cards (Nyami, Kutra) with the fixed
presets above and a 20-exchange conversation: on Mini both sent only the
pinned opening message — none of the actual back-and-forth — and so did
Standard. Max recovers substantially (21 of 41 for Nyami, 30 of 41 for
Kutra) now that its prompt budget is not also being quietly shrunk by the
reply cap and safety margin the old, conflated `context` number carried —
but Mini and Standard, at 1-2k and 2-3k of prompt, still cannot fit both a
card this size and any conversation. The card's own mandatory identity
(system prompt + persona + scenario, before a single writing rule or example
is added) is already ~1360-2100 tokens for these two; Nyami's alone, with
every optional section switched off, still runs to ~1620 tokens against
Mini's 1536-token prompt budget — the card's identity by itself is already
past the ceiling. This is not something a preset can tune away: §7.1's rule
is that only the *middle* (the conversation) is ever trimmed, never the
prefix a card supplies — doing that automatically would mean silently
rewriting someone's character, which is a worse failure than a short-lived
amnesia the person can see and correct for. A character that reads as having
no memory of the last several messages on Mini or Standard, with a card this
size, is that rule working as designed, not a bug in it. The two available
fixes both live outside the app: trim the card itself (Nyami's
`system_prompt` and `mes_example`, or Kutra's lorebook — 38 entries
totalling ~22.5k tokens, of which the app already budget-caps what any one
turn can inject, but a smaller book still leaves more of that cap for the
entries that actually matter), or run a detailed card like this on
Standard/Max instead, where — Max especially, now that its real prompt
budget isn't being silently eaten by the reply/safety margin — identity and
conversation are no longer competing for the same few hundred tokens.

**Confirmed live, not just in synthetic testing.** Reported symptom: a real
Kutra chat (11 turns / 23 stored messages) started "narrating the story
from the beginning" once the conversation got long enough. Replayed the
exact stored history against the real card: right before the turn where
this happened, Mini's assembled prompt held the pinned opening and *nothing
else of the conversation* — not even the immediately preceding turn — and
Standard was the same; Max held only the last two turns. A model handed
[vivid, detailed arrival scene] + [a single recent line, no bridge between
them] has almost nothing to continue *except* the arrival scene, which is
exactly what every one of that turn's four regenerated variants did,
independently, in the real transcript. Two real bugs surfaced and were
fixed in the process of tracing this (the macro-expansion and
lorebook-token_budget entries above; the second alone was quietly spending
591 of Mini's 1536 tokens on one lorebook entry meant to cost 200) — fixing
both recovers some room but does not change the outcome for this card on
Mini or Standard, because the card's own identity content was already over
budget before either bug. The fix for this specific symptom is the same as
above: Max (where real headroom now exists) or a smaller card.

### A card's lorebook can misattribute another character's traits

Found while investigating the macro-expansion fix above, against a real card
(Kutra). Of its 38 `character_book` entries, roughly 15 write `{{char}}` to
refer to *themselves* while describing a completely different named
character — Wira, Isiya, Emas, Sarpint, and others, each keyed on their own
name and species ("wolfboy", "catgirl", "deergirl", …), not Kutra's. In this
or any solo chat `{{char}}` always resolves to the chat's own character, so
the moment the conversation says one of those keys — plausible on its own
given the card's premise, an owner with several demi-humans, and at least
three of the roster (Zamj, Aese, Myval) are legitimately part of Kutra's own
greetings — the model is handed that other character's full sheet with
every `{{char}}` now reading "Kutra": a card built to misattribute a wolfboy
or a lamia's traits to her, fluently, the moment the topic comes up. This is
almost certainly a bigger source of "the character is inconsistent" than
anything on this page that the app controls. It is not a code defect —
nothing server-side rewrites lorebook content — and not something the app
can safely correct on its own either: telling the difference between "this
entry is about a different character on purpose" (legitimate — a shared
world where several demi-humans recur) and "this entry was never about this
character at all" needs reading each entry, which only the person who wrote
or assembled the book can actually judge. The fix is the book: an entry
about a character other than the one the chat is about should name that
character literally, never `{{char}}`.

### The pre-pass ("would the character say yes") is not built

Discussed and specified in an earlier round of this session — a director
pass, gated on whether the character would refuse, running before the reply
rather than only auditing it afterwards. Not started. Two open questions
before it can be: which model tier it runs on, and how its output is worded
so the actor plays the finding rather than narrating it.

---

## Documented elsewhere, not tracked here

**Prompt assembly is O(chat length), not O(context window)** — every turn
loads the whole chat's messages with no `LIMIT`. Measured (2,000 messages:
~17ms list / ~34ms assembly; 10,000: ~86ms / ~170ms, blocking the event
loop). Audited and deliberately left as-is: fast enough at the sizes a real
conversation reaches, with the fix (`ORDER BY turn DESC LIMIT N` plus the
pinned opening) recorded for if that ever changes. This is a decision, not an
oversight — see `DESIGN.md` §7.1 for the full note and the reasoning for
why it stays.
