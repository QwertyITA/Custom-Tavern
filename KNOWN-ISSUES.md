# Known issues

Everything here was found during two audit passes over the running app, not
read off the source and guessed at. Where a number appears, it was measured;
where a behaviour is claimed, it was reproduced against a live server — most
recently on 2026-08-21, re-verified against the code as it stands after the
streaming and portrait-deletion fixes, not against an earlier snapshot.

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
"Failed to fetch" and losing a typed message when the server was unreachable.
None of those are repeated below.

---

## Medium

### Upload endpoints read the whole body before checking its size

`app/main.py`: card import, chat import, attachment upload, and both avatar
upload routes all do `payload = await request.body()` and only compare
`len(payload)` against the limit afterwards. Picking the wrong file — a video
instead of a character card, say — puts the whole thing in memory before it is
rejected. On the phone this is meant to run on, that is the resource that is
scarcest. `Content-Length` could be checked before the body is read; five call
sites would need it.

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

### `host` and `port` in settings do nothing

`app/config.py` validates and stores both (`1 <= port <= 65535` is enforced
on save) and `settings.example.json` documents them, but nothing binds to
either — `run.sh` reads `TAVERN_HOST` / `TAVERN_PORT` from the environment
instead, never from the saved settings file. Editing them in the settings UI
changes a value that is validated, persisted, and then never read. Either
`run.sh` should read them (it already shells out to inline Python once) or
they should be removed; both are a product call, not a bug fix.

### `newChat()` has no error handling

Every other network call in `static/app.js` sets `this.error` on failure;
this one is the sole exception (confirmed by re-grepping after the offline-
handling fix landed — every other bare `await api.*`/`await fetch(...)` call
this session was found to be wrapped except this one). A failed tap on "New
chat" does nothing at all and says nothing about why. Self-recovering — a
retry usually works — but silent.

### Two turns can run at once in one chat

Tested directly: `asyncio.gather` on two `run_turn` calls against the same
chat, including with an artificially slow backend so the runs actually
overlap. No corruption resulted — `next_turn()`'s `MAX(turn)+1` happened to
land on distinct numbers in every run tried, and each turn's messages stayed
attached to the right turn. What you get from two tabs or two devices open on
the same chat is two independent replies to two different prompts that never
saw each other, which is confusing, not corrupting. No lock exists to stop it
either way.

### `PassScheduler._pending` never removes an emptied chat entry

`_track()` does `self._pending.setdefault(chat_id, set())`; the per-task
done-callback only discards the finished task from that set, never removes
the set itself once it is empty. One dict entry, holding an empty set,
persists forever for every chat a background pass has ever run in. Bytes-
scale and never grows within a single chat — confirmed by reading the
callback, not sized in a running process.

### No cap on concurrent background passes per chat

`_launch_background` starts every eligible pass as its own task with nothing
bounding how many chats or how many passes-in-flight exist at once. A slow
background backend plus fast typing can pile up tasks and HTTP connections;
bounded in practice only by how fast someone types and by
`blocking_await_ms`, not by an explicit limit.

### Emotion sprites don't go through the cropper

The cropper introduced this session only writes `pfp_set.neutral`; a card
whose `happy`/`sad`/`angry`/… entries are square sprites gets each of them
framed by plain `object-fit` under a portrait (2:3) shape choice, or vice
versa. Cosmetic, and only visible on a card that ships more than one emotion
sprite and a shape mismatch between them.

### Replacing or removing a portrait leaves the old file behind

New this session, found by testing the deletion fix's actual boundary rather
than assuming it covered the whole feature. `confirmCrop()` and
`clearCharacterPfp()` both overwrite or clear `pfp_set.neutral` client-side
with no corresponding delete of whatever file it used to point at — and
because the character-deletion cleanup (`_forget_orphaned_avatars`) only ever
reads the character's *current* `pfp_set`, it has no way to know a *previous*
file existed once it has been overwritten. Confirmed live: upload, assign,
replace with a second upload, and the first file is still served with a 200
— deleting the character afterwards cleans up only the second file, the one
still referenced at the moment of deletion. Same class of issue as the
deletion gap this session actually fixed (disk-only, `data/avatars/` only
ever grows), but a different trigger, and the cropper likely makes
"replace the picture" a more common action than the old single-shot upload
ever was.

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

### The character roster keeps fixed round avatars regardless of `pfp_shape`

`.char-pfp` in `static/styles.css` is a fixed 38×38 circle with no binding to
a character's chosen shape at all — confirmed by re-reading the rule after
the shape feature shipped. Deliberate: a list of faces reads better uniform,
and the roster is a different surface from the conversation, where the shape
is drawn correctly. Flagged for whoever might want the roster to follow suit
instead.

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
