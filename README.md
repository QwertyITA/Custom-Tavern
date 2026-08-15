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

### Getting around

The header is one line — the menu, who you are talking to, and where they are,
each reading behind its own icon. Tapping **☰** eases a row of four
destinations down underneath it.

| | Destination | What is behind it |
|---|---|---|
| brain | Model & engine | tiers, backends and keys, context budget; sampling and the rest under *Advanced* |
| theme | Appearance | backdrop, palette presets, accent; every individual token under *Advanced* |
| chats | Characters & chats | one row per character — portrait, edit, export, new chat, history, delete |
| story | Story state | state bands, toggles, the rolling summary, and the pass HUD |

Tapping the character name in the header opens the same roster, with that
character's history already unrolled.

The **+** left of the text box holds the actions that are about the
conversation rather than about a message: regenerate the last reply,
impersonate, refresh the world line, start a new chat. Holding any message
opens a wheel with edit, copy and delete — slide onto one and let go, or let go
where you are and tap.

**Impersonate** writes your next message in your own voice and drops it in the
composer for you to edit before sending. It is in that **+** menu, and it is
also what appears if you pull the conversation up past its last message.

The world line — place, weather, time — is written by the `scene` pass, which
only runs when a reply suggests the setting moved. That is right nearly always,
and wrong exactly when you are looking at a stale line and can see it is stale,
so **⟳** at the end of the line runs the pass on demand. It is the ordinary
path with the trigger bypassed: same slice write, same run row, same HUD entry.

Characters can be written in the app — **chats → New character** — imported
from a card, or exported back to one. Cards are the SillyTavern format: a
`.json`, or a `.png` with the character embedded in it. An export carries the
v2 payload with the v1 fields mirrored at the top level, which is what
SillyTavern itself writes and what lets an older importer read it at all.
Editing only rewrites the text fields; portraits, backgrounds, lorebook and
state schema come from the card and are left alone. Deleting a character
deletes its chats with it, which is why both deletes take two taps.

### Stop strings

Sequences that end a generation. Two places, because they answer two different
questions. **Per backend** (☰ → brain) is for a model's own artefacts — a label
it keeps writing, a scene break it overuses; that is a property of the model,
not of the story. **Per character** (chats → edit) is for what *this* character
keeps doing.

Both are one per line. A line's trailing space is kept, because a trailing
space is exactly the kind of thing a stop string is — but a stop string cannot
*begin* with a newline when written that way, since lines are the unit.

They are merged most-specific-first with whatever the instruct template needs,
which matters for the backends that cap how many they accept: the ones nearest
the story survive the cut. Impersonate deliberately does not use the
character's — that is your line, and a sequence that ends their replies has no
business cutting off yours.

### Languages

Two fields under ☰ → brain: **they write in**, **you read in**. Leave both
blank for off. When they differ, every turn crosses the gap twice — your
message into their language before the model sees it, their reply into yours
before you do. There is no separate on/off switch, because a switch that could
disagree with the languages is a switch that will.

The original is never overwritten. What was actually written stays as the
message; the translation sits beside it. That matters in both directions: the
prompt keeps seeing one consistent language turn after turn, and you can still
check what a character really said rather than only what a second model call
made of it. Your own message is always shown to you as you typed it.

It costs two extra calls a turn on the foreground tier, so it is slower and
dearer. Nothing here detects a language — if you say they write Japanese, the
translator is told Japanese. A translation that fails leaves the original
readable rather than losing the turn.

### Web search

Off, and off twice over: the switch **☰ → story → Web search** starts off, and
even on it does nothing until you have given it somewhere to search. Both
halves are deliberate — a switch that quietly did nothing would look broken,
and a URL that searched without being asked would be your phone making requests
you never wanted.

Nothing ships with a search engine, because every free one either needs a key,
rate-limits a phone into uselessness, or is an HTML page whose shape changes
without warning. What there is instead is a URL template under **☰ → brain →
web search**, with `{q}` where the query goes:

    http://192.168.1.5:8888/search?q={q}&format=json     SearXNG, self-hosted
    https://api.search.brave.com/res/v1/web/search?q={q} with a key

A key, if yours needs one, is sent as both `Authorization: Bearer …` and
`X-Subscription-Token`, so you do not have to know which one your provider
wants. It is masked everywhere it is shown, like any other credential here.

The reply is read leniently. Search APIs disagree about almost everything —
`results` vs `web.results` vs `items`, `content` vs `snippet` vs `description` —
so rather than a shape per provider there is one reader that looks where
results are usually found. A shape it cannot read comes back empty, which
reads as "found nothing" rather than as an error, because that is what it means
to the person reading the chat. So does an engine that is down: the turn goes
ahead without the results.

Two decisions worth knowing. There is **no model call** — the snippets go
straight into the prompt with their addresses attached, so the character can
say where something came from; distilling them through a model first would
double the cost of the feature in order to make the results shorter. And the
results are bound to the turn that asked for them: they appear once and then
stop, because an answer looked up three turns ago is worse than no answer, the
model having no way to tell that it is old.

The cost is one HTTP request, before the reply starts. A slow engine is a slow
turn — while it runs the cue says *looking it up*.

### Unplanned things

Under ☰ → story. Occasionally the world does something on its own — a knock at
the door, the rain starting, a stranger sitting down. **Chance per turn** sets
how often; at zero it never happens, which is the whole off switch (there is no
second flag that could disagree with the frequency).

Two things about how it is built. The gate is a dice roll, not a model call, so
on the turns it does not fire it costs nothing at all. And when it does fire,
the event is invented in the background *after* a turn and woven into the
**next** one — so the reply never waits for it, and the model gets a whole turn
to work it in rather than having it dropped on the turn it was invented.

An event is used exactly once. The pass is told to introduce something the
world does rather than something either person decides, never to resolve it,
and to return nothing if nothing would plausibly intrude.

### Group chats

Several characters in one conversation. **☰ → story → Who is here** adds them,
mutes them, and sets how readily each speaks up. A solo chat is a group of one,
so nothing changes until you add somebody.

**Whose turn it is** picks the policy:

| Policy | What it does |
| --- | --- |
| **Whoever would answer** (default) | Name someone and they answer; otherwise weighted chance, with whoever just spoke pushed down |
| **Take turns** | Strict order |
| **You choose** | Pick before each message |

The default is deliberately not take-turns. Round-robin is the arrangement
where you say something to one person and the other one answers, forever — it
is the single thing that makes a group chat read as a mechanism rather than as
a room. The default costs nothing: naming someone is a substring search, and
everything else is a weighted roll. Whoever just spoke is pushed down rather
than blocked, because a room where a character can never follow their own line
has its own tell.

**Muting** keeps someone in the scene but silent. They still appear in every
other character's prompt — someone standing there saying nothing is still in
the room, and dropping them would have the others talk as if it were empty.
The last member cannot be removed; mute is what that is for.

Each character keeps their own trust, mood and expression, so two people in one
room hold separate opinions of you. The scene — place, weather, time — is
shared, because they are in the same room.

### Attachments

The **+** by the composer → **Attach a file**. Images (png, jpg, webp, gif) or
text (txt, md, json, csv, log, yaml). Chips appear above the text box; tap the ×
to drop one. A message with a picture and no words is a real message.

The two kinds behave differently on purpose:

**Text files** are read once, at upload, and their text travels into the prompt
with the message — like anything else you said. The file itself is not kept,
because the text *is* the file. Up to 8,000 characters of it are used; a
dropped-in document can be enormous, and spending the whole context on it
silently would be worse than using part of it.

**Images** are stored and shown in the bubble. They reach the model only where
the backend can actually see one — Ollama and OpenAI-compatible backends take
them; llama.cpp's `/completion` endpoint and Horde have nowhere to put one. On a
backend that cannot see it, the message still says a picture is attached and
names it, because a reply that ignores a picture you clearly meant something by
is worse than one that says it cannot see it.

Only the newest turn's images are sent. Re-sending every image in the window on
every turn would be the single most expensive thing this app does.

Limits are 8MB an image and 512KB a text file. Files picked but never sent are
swept after an hour, and deleting a message or a chat removes its images from
disk — the database cascade drops the rows but not the files.

### Favourites

The star on a character row. Starred characters sort to the top; everything
else stays in name order, deliberately not in order of use — a roster that
rearranges itself as you use it is one you have to re-read every time. The row
travels to its new place rather than reappearing there.

There are no tags and no folders, on purpose. One flag answers the question
anyone actually has of a roster this size ("which of these do I use"), and a
taxonomy for a dozen characters is more work to maintain than to scroll past.

### Chats: naming, finding, moving them around

Under ☰ → characters & chats.

**Search** sits at the top and looks inside the messages, not just the titles —
the reason to open this panel with something in mind is almost always "which
chat was that in". Each result shows the line that matched, under the character
and chat name.

**Rename** is the pencil on a chat row. Enter saves, Escape abandons. Renaming
deliberately does not move the chat to the top of the list: the list is ordered
by when the story last moved, and renaming is not the story moving.

**Export** writes the chat as plain JSON — every field named, readable in a text
editor. Messages, all their swipe variants, the rolling summary and the state
all travel. The character does not: it has its own card export, it is usually
shared between several chats, and copying it into each one would let an import
silently fork it. **Import chat** puts one back as a *new* chat, so importing an
export of something you still have gives you a second copy rather than
overwriting the first.

An import binds to a character that is already here — by id, then by name. If
neither is found it says whose chat it is and stops, rather than guessing.

### Find and replace

Under ☰ → brain. Rules run in order on every message, and each one picks a
scope. The scopes differ in what they destroy:

| Scope | What it does | Undoable |
| --- | --- | --- |
| **How it looks** | Rewrites only what is drawn | Yes — the message is untouched |
| **What you send** | Rewrites your message before it is stored | No |
| **What it replies** | Rewrites the reply before it is stored | No |

Reach for the display scope. It is a lens: turn the rule off and the original
comes back, because the original was never overwritten. A bubble that a display
rule is rewriting carries a faint left rule, so "why doesn't this match what I
copied" has an answer. The other two are edits to the record — change a bad
rule afterwards and the damage is already in the database.

Each rule has a **Try it on** box that runs the pattern server-side, by the
same code that will run it for real, and shows the result and the match count
as you type. Use it. A wrong pattern in one of the permanent scopes is not a
recoverable position.

**About slow patterns.** Rules are your own regular expressions running in the
one process serving your phone, and Python cannot interrupt a regex once it has
started — a pattern that backtracks catastrophically would hang the app with no
way out. So every pattern is timed against four short adversarial strings when
it is saved, and one that is slow there is refused with an explanation. That
catches the usual shapes (`(a+)+`, `(a|a)*`, `([a-z]+)+$` and friends); it is a
filter rather than a proof, so a message longer than 40,000 characters is also
left alone as a second line of defence.

Streaming text is not rewritten as it arrives — a rule matching across a chunk
boundary cannot be applied to half a match — so a display rule settles the
moment the reply finishes.

### Samplers

Under ☰ → brain, per pass. Temperature, top-p and the length cap are on the
front; **More samplers** opens the rest: min-p, top-k, typical-p, tail-free,
the three repetition penalties, DRY, XTC and a seed.

Two things govern all of them.

**Neutral means off.** Every sampler has a value at which it does nothing, and
while it sits there it is left out of the request entirely rather than sent as
a no-op. So a backend is never handed a stack of parameters nobody set — which
is how output starts changing for reasons no one can account for. The badge on
*More samplers* counts what is actually going out; **Turn the extras off**
returns them to neutral, and deliberately leaves temperature, top-p, top-k and
the repetition penalty alone, since each pass ships with its own tuned values
for those.

**Support is declared, not attempted.** A sampler the pass's backend does not
accept is dimmed and says so, rather than being sent hopefully. Horde validates
its parameters and fails the whole request on an unknown one, and the official
OpenAI API 400s on `top_k`, so "send it and see" is not available. Change which
backend a tier points at and the dimming follows.

Only llama.cpp's own server takes DRY and XTC today. Ollama, Horde and
OpenAI-compatible backends each take their own subset — the panel is the
authority on which.

If you are reaching for one of these for the first time: **min-p** is the one.
It scales with how confident the model is, which is what top-p and top-k both
fail to do, and it usually replaces both.

### What was sent

Hold a reply, pick **What was sent**. It breaks that reply's prompt into its
sections with a token count and a share for each, and any section opens to show
the text it actually contained.

It is the record of that generation, not a re-assembly. Rebuilding it now would
use today's state, today's memories and today's layout, and quietly answer a
different question than the one being asked. A re-roll keeps its own record for
the same reason (§9).

Two numbers appear when they differ: the rows always add up to the total shown,
and *backend counted* is what the model's own tokeniser made of the same
prompt. Our estimate rounds per section, so they land a token or two apart.

The conversation shows its size and message count but not a copy of itself —
it is already on screen, and storing it once per turn would grow the database
with the square of the chat. For the same reason only the last 20 turns keep
their breakdown; older messages say so rather than showing a guess.

### What goes into the prompt

Under ☰ → brain. The prompt is built in three groups, always in this order:

| Group | Holds | Why there |
| --- | --- | --- |
| **Who they are** | instruction, character, scenario, your persona, always-on lore, examples | Rarely changes, so the model keeps it cached between turns. |
| **What has happened** | recalled lore, memories, the summary, the conversation | Changes as the story does. |
| **Right now** | their state, the setting, what happened, what was looked up, toggles, the card's last word | Different every turn. |

Each section can be switched off, and moved **within its group**. The groups
themselves do not move, and that is the one restriction worth explaining: a
model caches the prompt from the front, and everything after the first changed
byte is recomputed. The last group changes on every turn — so anything you put
above it would be recomputed with it, every reply, forever. On a phone hosting
its own model that is the difference between a reply starting now and a reply
starting after the whole prefix is rebuilt.

Sorting them into groups rather than warning about it means there is no
arrangement of these controls that produces a slow prompt. Two sections cannot
be switched off at all — the main instruction and the conversation — and the
switch says so rather than being hidden.

**Add a block here** puts your own text into a group. It takes a title and a
body, `{{char}}` and `{{user}}` work in it like anywhere else, and it can go
anywhere in its group — including below *the conversation*, which is how you
get a standing instruction that the model reads after the transcript rather
than before it.

Layout is global, not per character: it describes how you like prompts built.

### Writing your own instruct template

Most models are covered by picking `chatml`, `llama3`, `mistral` or `plain` in
☰ → brain, and `auto` guesses from the model name. Pick **custom** and eight
boxes appear instead — the strings that go around each turn:

| Box | Chatml, as an example |
| --- | --- |
| Very beginning of the prompt | *(blank)* |
| Before / after the instructions | `<\|im_start\|>system\n` / `<\|im_end\|>\n` |
| Before / after your message | `<\|im_start\|>user\n` / `<\|im_end\|>\n` |
| Before / after the reply | `<\|im_start\|>assistant\n` / `<\|im_end\|>\n` |
| Start of the new reply | `<\|im_start\|>assistant\n` |

Newlines are typed and shown as `\n`, because a marker almost always ends in
one and a text box that swallows it is unusable.

Nothing here is a lesser path: the four presets fill the same eight boxes, and
`chatml`, `llama3` and `plain` come back out byte-identical to the built-in
versions. So the way in is to press the preset closest to your model and change
the one thing it disagrees about. (`mistral` is the exception — the built-in one
folds the system prompt into the first user instruction, which is a *rule* and
not a pair of strings. The preset is the other common Mistral shape, with the
system prompt as an instruction of its own. Both work.)

**Show me a real prompt** renders a short made-up exchange through the boxes.
It is drawn by the same function that runs for real, not a second
implementation of it — being believed is the only thing it is for. It also
lists the stop sequences your boxes imply, which are taken from the three that
would end a reply: the marker closing the character's turn, and the two opening
yours.

### Author's note

A standing instruction, under **☰ → story**. Unlike the character's own text it
goes *inside* the recent conversation rather than at the top of the prompt, and
that placement is the whole feature: at the top it is buried under everything
said since. Use it to steer — *keep the pace slow*, *she is hiding something* —
rather than to describe the character.

**How far back** is how many messages from the end it sits. At the end it
carries most weight and costs least; further back it reads as a standing
condition rather than as something just said, but everything after it has to be
re-read by the model each turn (§7.1). **How often** trades cost for presence.

A note written here belongs to this chat. A character card can carry its own as
a default, editable under **chats → edit**; writing one here overrides it
wholesale, and clearing the text falls back to the card's.

### Hiding a message

**Hide**, in a message's hold-wheel, keeps it on screen and takes it out of the
prompt. It is what an out-of-character aside wants, or a reply that went
somewhere the story should not remember — deleting would lose it, and editing
it to nothing is not the same as it never having been said. A hidden message is
dimmed, dashed and marked with a struck-through eye, so which ones the model
can no longer see is never a guess. Reversible from the same place.

It is a flag of its own rather than a message *stage*: the eviction ladder owns
stage and moves messages through it, so a hidden message expressed that way
would be quietly promoted back into the prompt.

### Continuing a reply

A reply that stopped early — because you stopped it, or because the model hit
its token limit — can be carried on. **Continue** is in the composer **+** menu
and in a reply's hold-wheel. It extends the message *in place*: no new variant,
no counter going to 1/2, and no "edited" pencil, because the character carried
on rather than someone rewriting them. The model is handed the reply so far as
the start of its own turn, which is what stops it starting the sentence again.

### Stopping a reply

While a reply is coming, the send button becomes a stop button — the same
corner, a different verb, because that is the only thing worth doing with it
mid-stream. Stopping keeps what had already arrived: it means "I have read
enough", not "throw that away". No state is written, since the `<<<state>>>`
suffix that carries it never got there, and the run is recorded as *stopped*
rather than failed.

### Who you are

A **persona** is your side of the conversation: a name, a description and a
portrait. `{{user}}` resolves to the name, and the description is given to the
model the same way the character's own is. Set them up under **chats → You**.

Three places are asked, most specific first — the chat's own choice, then the
persona this character is usually played with, then the global default marked
with the star. Each falls through if it names something deleted, so removing a
persona degrades old chats to the default rather than breaking them. With no
personas at all the character simply calls you "You".

Tapping a persona plays as them *in the current chat*; the star is what changes
who new chats get.

### Opening messages

A card may offer several. They arrive as **swipe variants of the opening
message**, so choosing between them is the gesture that already exists rather
than a picker that appears once and never again. A new chat opens on the card's
own `first_mes`; swipe left for the alternates, and past the last one to have
the model write a fresh opening.

A card's **final instruction** (`post_history_instructions`) is read *after*
the conversation, which is the point of the field — it is where a card puts the
rule it wants obeyed over whatever the scene has drifted into. Both are
editable under **chats → edit**.

### Macros

Cards are written with placeholders, because a card is meant to be portable
between people. They are resolved before anything reaches the model:

| | |
|---|---|
| `{{char}}` `{{bot}}` | the character's name |
| `{{user}}` | you — the active persona's name |
| `{{persona}}` | your own description |
| `{{description}}` `{{personality}}` `{{scenario}}` | the character's |
| `{{time}}` `{{date}}` `{{weekday}}` `{{isotime}}` `{{isodate}}` `{{utctime}}` | now |
| `{{idle_duration}}` | how long since the last message |
| `{{random:a,b,c}}` | a different one every turn |
| `{{pick:a,b,c}}` | one, chosen once and kept for that chat |
| `{{roll:d20}}` `{{roll:2d6}}` | dice |
| `{{newline}}` `{{trim}}` `{{comment:...}}` | typography |

Card text — persona, scenario, system prompt, lorebook — is resolved **every
turn**, because `{{time}}` moves and the persona can be switched. Messages are
resolved **once, when they are written**: a message is a record of something
that was said, and rewriting an old greeting because a persona was renamed
would falsify the transcript.

An unrecognised macro is left on screen rather than blanked, so a typo is
something you can see and fix.

### Backdrop

A tavern scene sits behind the chat by default. It is original vector art
(`static/backgrounds/tavern.svg`) rather than a stock photo — this repository
is public, so an image with no licence question attached is worth more than a
photograph, and a few kilobytes of SVG stays sharp on any screen where a JPEG
would not.

Change it under **☰ → theme**: the backdrops are shown as pictures, not
filenames, and *Add an image* uploads your own. Uploads go to
`data/backgrounds/`, which is gitignored — `static/backgrounds/` is tracked and
ships with the app, so putting personal images there would make every `git
pull` a possible conflict. Both folders are enumerated, so a file dropped into
either appears in the list. Only uploads can be deleted. A backdrop set by the
`background_swap` pass takes precedence while it is active.

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

Set backends and keys in the app: **☰ → brain**. Assign each tier a backend,
fill in the URL/model/key, and use *test connection* to check one before
relying on it. Everything is written to `data/settings.json` on the device.

Nothing has to be typed from memory. Picking a **kind** fills in the URL,
template, timeout and a sensible model, with a note on what that backend is
for. **load** next to the model field asks the backend what it can actually
serve — pulled models from Ollama, `/v1/models` from an OpenAI-compatible
endpoint, active text models from Horde ordered by worker count, since a model
with no workers queues forever. The field stays typeable, so a model you are
about to pull still works.

The key handling is deliberate:

- Saved keys are **never sent back to the browser**. A read returns `***`, and
  submitting `***` unchanged means "keep the stored value" — so you can change
  a model without retyping a key, and the page never holds one to leak. The
  search key is handled the same way, for the same reason.
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
python3 -m pytest        # 757 tests, hermetic, no network, no extra deps
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

Watch it happen: **☰ → story → Pass HUD & cost**. Every pass this turn shows
its tier, model, status and token counts, and the cost panel breaks spend down
per pass.

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
