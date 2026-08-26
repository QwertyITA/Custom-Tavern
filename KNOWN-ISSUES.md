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

Fixed on 2026-08-26 — the AI Horde quick-setup presets (Mini/Standard/Max,
`static/app.js`) set a `context` far above what they were meant to: 4096 /
8192 / 32000, against a stated design of roughly 1-2k / 2-3k / 4-5k. Measured
with the app's own assembly code against two real, moderately detailed
character cards and a 20-exchange conversation: on the *old* numbers Mini
still sent 25 of 41 messages (Nyami) or 33 of 41 (Kutra) — workable, but
already trimming meaningfully more than a 4k-context tier should need to, and
the two larger tiers weren't trimming anything, which is a second problem —
see below. Presets now set 1536 / 2560 / 4608, and the two "not a matter of
tier" blocks (`craft:format`, `craft:length`) aside, the writing-library
selection was retuned around correctness over polish at the tighter budgets
(Mini: only `craft:autonomy` + `craft:knowledge`, the two rules whose absence
reads as broken rather than merely plain), the card's own example dialogue
— often the single most expensive optional prefix section — now switches
off below Max, and `lorebook_total_budget`/`memory_max_injected` scale down
with the tier the same way. All within the existing mechanism (a one-shot
settings edit under the Save bar), nothing auto-truncated.

That surfaced a second, unfixed thing, recorded below under Not a defect: a
verbose card's own identity content can, on its own, exceed even the fixed
budgets. Nothing here changes that — see that entry for the measurement and
why the fix is the card, not the preset.

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
Standard, and so did Max. The card's own mandatory identity (system prompt +
persona + scenario, before a single writing rule or example is added) is
already ~1360-2100 tokens for these two; Nyami's alone, with every optional
section switched off, still runs to ~1620 tokens against Mini's ~980-token
effective budget once the reply and safety margin are taken out. This is not
something a preset can tune away: §7.1's rule is that only the *middle*
(the conversation) is ever trimmed, never the prefix a card supplies — doing
that automatically would mean silently rewriting someone's character, which
is a worse failure than a short-lived amnesia the person can see and correct
for. A character that reads as having no memory of the last several
messages on Mini or Standard, with a card this size, is that rule working
as designed, not a bug in it. The two available fixes both live outside the
app: trim the card itself (Nyami's `system_prompt` and `mes_example`, or
Kutra's lorebook — 38 entries totalling ~22.5k tokens, of which the app
already budget-caps what any one turn can inject, but a smaller book still
leaves more of that cap for the entries that actually matter), or run a
detailed card like this on Standard/Max with a non-Horde backend instead,
where the context is large enough that identity and conversation are not
competing for the same few hundred tokens.

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
