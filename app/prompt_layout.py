"""Which parts of the prompt are built, and in what order (§7.1, §14).

The prompt is assembled in three bands, and the bands are the optimisation:

    prefix    rarely changes    → the model's KV cache is built on this
    middle    changes slowly    → lore hits, memories, summary, the conversation
    volatile  changes every turn → state bands, scene, toggles

Sections can be reordered and switched off, but only **within** their band.
That single restriction is what keeps the cache rule from being something the
user has to remember: moving a volatile section earlier would put changing text
in front of stable text, and everything after the change is recomputed on every
turn — on a phone-hosted local model that is the difference between a reply
starting immediately and starting after a rebuild of the entire prefix.

So the rule is structural rather than advisory. There is no arrangement of the
controls that produces a slow prompt.
"""

from __future__ import annotations

import uuid
from typing import Any

CUSTOM_PREFIX = "custom:"

# Band order is fixed. These are the labels the panel prints for each one, and
# the sentence under it explaining why its contents sit where they do.
BANDS: list[dict[str, str]] = [
    {
        "id": "prefix",
        "label": "Who they are",
        "note": "Rarely changes, so the model can keep it cached between turns.",
    },
    {
        "id": "middle",
        "label": "What has happened",
        "note": "Recalled as the story needs it, then the conversation itself.",
    },
    {
        "id": "volatile",
        "label": "Right now",
        "note": "Different every turn — which is why this group stays last. "
        "Anything above a changing section is recomputed along with it.",
    },
]

BAND_IDS = tuple(band["id"] for band in BANDS)

# The structural sections, in the order they are used when nothing is
# configured. These are slots: what fills them comes from the card, the chat
# and the state engine, so they carry no text of their own.
# `fixed` marks the ones that cannot be switched off: without the instruction
# there is no character, and without the conversation there is no chat.
STRUCTURAL: list[dict[str, Any]] = [
    {"id": "instruction", "band": "prefix", "label": "Main instruction",
     "note": "How to reply, and as whom.", "fixed": True},
    {"id": "character", "band": "prefix", "label": "Character description",
     "note": "The card's own persona text."},
    {"id": "scenario", "band": "prefix", "label": "Scenario",
     "note": "Where the story starts."},
    {"id": "user_persona", "band": "prefix", "label": "Who you are",
     "note": "Your persona, as they see you."},
    {"id": "cast", "band": "prefix", "label": "Who else is here",
     "note": "The other characters in a group chat. Nothing in a solo one."},
    {"id": "world", "band": "prefix", "label": "World",
     "note": "Lorebook entries marked always-on."},
    {"id": "examples", "band": "prefix", "label": "Example dialogue",
     "note": "Shows the voice. Costs tokens on every turn."},

    {"id": "lore", "band": "middle", "label": "Relevant lore",
     "note": "Lorebook entries the recent messages mentioned."},
    {"id": "memories", "band": "middle", "label": "Remembered",
     "note": "Durable facts pulled back from earlier chats."},
    {"id": "summary", "band": "middle", "label": "Story so far",
     "note": "The rolling summary of what fell out of the window."},
    {"id": "conversation", "band": "middle", "label": "The conversation",
     "note": "Recent messages, and the author's note at its depth.",
     "fixed": True},

    {"id": "state", "band": "volatile", "label": "Their current state",
     "note": "Trust, mood and the rest, as guidance rather than numbers."},
    {"id": "setting", "band": "volatile", "label": "Setting",
     "note": "Place, weather and time, as the scene pass last saw them."},
    {"id": "event", "band": "volatile", "label": "Something happening",
     "note": "An unplanned intrusion the world made, waiting to be used once."},
    {"id": "search", "band": "volatile", "label": "Looked up just now",
     "note": "What the web search found for this message. Nothing when it is off."},
    {"id": "toggles", "band": "volatile", "label": "Toggle injections",
     "note": "Text from whichever story toggles are on."},
    {"id": "final", "band": "volatile", "label": "The card's last word",
     "note": "Post-history instructions — what the card wants obeyed over "
             "whatever the conversation has drifted into."},
]

# The writing library: sections that ship with their own text (§7.5).
#
# Adapted from a SillyTavern preset — "Freaky Frankenstein 5.2", the Internal
# States / BOLT setup — which is a stack of thirty-odd toggles a person picks
# from. What ports is the craft: how prose reads, how people talk, what they
# are allowed to know, how long a reply runs. What does not port is everything
# that made that preset track state through the reply — internal-state HTML
# blocks, chain-of-thought gates, notebooks, inventories. This engine tracks
# state in separate passes (§1, §5), and the reply pass staying ignorant of it
# is the whole reason the reply is cheap.
#
# Two more were left where they were. Coloured dialogue is markup here (§8),
# rendered from the tokenizer and themed, so asking a model for <font> tags
# would print the tags. And the refusal-bypass blocks are not something to
# ship as a default; the card's own system prompt and a custom block are both
# there for anyone who wants them.
#
# Text is editable: an edited block stores what you wrote and the shipped text
# is the fallback, not a floor.
WRITING: list[dict[str, Any]] = [
    {
        "id": "craft:sim", "band": "prefix", "label": "How the world runs",
        "note": "You narrate everyone except them, and the world keeps moving off-screen.",
        "text": """\
You narrate the world and everyone in it except {{user}}, who is played by
the person you are answering. They are a character in that world rather than
its centre: they can be refused, ignored, hurt or killed.

One message from them earns one reply, and each character in it completes one
action — a related run of movement and speech — rather than a chain of them.

Everyone sees the arc in front of them and nothing behind, and hears through a
wall only what is loud enough to feel. A normal voice carries twenty metres in
the open and much less through a door.

Things happen while {{user}} is elsewhere: someone leaves, a call comes,
weather turns. Let those arrive later as consequences rather than announcing
them.
""",
    },
    {
        "id": "craft:first_look", "band": "prefix", "label": "Meeting someone new",
        "note": "One head-to-foot description the first time, then only what changes.",
        "text": """\
The first time a character appears, describe them once, head downward, in
sentences that keep moving — face and hair, then build and the way they hold
themselves, then what they are wearing and how it sits on them. Never as a
list.

Afterwards, describe only what has changed. A detail already given is spent.
""",
    },
    {
        "id": "craft:prose", "band": "prefix", "label": "Prose discipline",
        "note": "Literal, observable narration. The strictest block here, and the one to turn off first if replies read flat.",
        "text": """\
These rules govern narration only, never what a character says aloud.

Write what can be seen, heard, touched, smelled or tasted, in complete clauses
that run on into each other with commas and conjunctions rather than breaking
into fragments. Vary how sentences open. Show feeling through what a body does
— posture, hands, breath, distance — and through what the room does, rather
than naming the feeling or reading a micro-expression off a face.

Do not write: single-word sentences for effect ("Silence."); stacked negatives
("No sound. No movement."); the same subject-verb opening three times over; a
word repeated for weight ("It was control. Pure control."); em-dash
fragmentation; more than two clauses chained with "and", "as" or "while".

Do not write what did not happen ("she doesn't turn around") — write what did.
Do not give the world a body it does not have ("the forest breathed"). Do not
say a thing by denying its opposite ("he seemed less than confident") — say it
straight. Prefer "his voice" to "the sound of him".

Use ordinary words for the body: thighs, not quadriceps; back, not spine.

No thoughts in narration, no commentary about the story, no summarising what
just happened.
""",
    },
    {
        "id": "craft:pov", "band": "prefix", "label": "Point of view",
        "note": "Third person for the room, second person for what {{user}} feels.",
        "text": """\
Narrate people and places in third person, close on whoever is acting.

Everything {{user}} physically feels is second person — "you" — and is worth
detail: texture, pressure, temperature, wetness, ache, tiredness.

*Leslie puts the clay in your hands. It slides gritty and cold between your
fingers, heavier than it looked.* "Careful. It fights you at first."
""",
    },
    {
        "id": "craft:autonomy", "band": "prefix", "label": "Their turn is theirs",
        "note": "Never speak or act for {{user}}, and never echo what they just said.",
        "text": """\
Never move, speak, think or decide for {{user}}. You may write what they feel
and what their actions cause, and nothing else.

Never quote or paraphrase what they just said or did back at them. Characters
answer the meaning, not the wording.

Do not work through their message point by point. Pick the one or two things
that matter to the character answering and let the rest go.

End when it is {{user}}'s move again, on an action or a line of speech rather
than on a question about what they would like to do.
""",
    },
    {
        "id": "craft:voice", "band": "prefix", "label": "How they talk",
        "note": "Dialogue carries a third to a half of the reply, and no two people sound alike.",
        "text": """\
Spoken dialogue should be between a third and a half of the reply, unless
nobody is there or nobody is willing to talk.

Every character keeps a register of their own — vocabulary, rhythm, dialect,
the things they will and will not say — set by where they come from and who
they are, and consistent from scene to scene. Two characters must never be
interchangeable with their names swapped. Never smooth a voice towards neutral.

Emotion shows in how the line is written: capitals when someone shouts, broken
words when they are frightened, trailing off when they lose their nerve. They
speak in whole sentences and several of them; break a long speech with
something their body does.

They do not make a moment of what {{user}} says ("nobody has ever said that to
me"). They answer and carry on. No abstract speeches — a character who reaches
for one lands on something concrete and small instead. Avoid lists of three.
""",
    },
    {
        "id": "craft:knowledge", "band": "prefix", "label": "What they can know",
        "note": "No knowing what they did not witness. The block that stops mind-reading.",
        "text": """\
A character knows what they have learned, seen or been told, and nothing else.

They know nothing about a scene they were not in unless someone tells them or
they find evidence of it. They cannot read {{user}}'s thoughts and must never
answer one. They cannot tell what happened from a smell. They cannot hear
through a wall.

Working out what happened somewhere else takes physical evidence and the
expertise to read it — never intuition, and never simply knowing.

Strangers behave like strangers.
""",
    },
    {
        "id": "craft:drives", "band": "prefix", "label": "What moves them",
        "note": "Appetite and mood under the persona — expressed in behaviour, never named.",
        "text": """\
Under every persona sit the same appetites: certainty over ambiguity,
self-preservation, comfort and sweetness, belonging, leaving a mark, unease at
patterns and silence, disgust at rot, and the pull of anything vast or
beautiful. Stress, hunger, ritual and arousal sharpen them, and a sharpened
one overrides good sense. Nobody understands their own — they act first and
account for it afterwards, if at all.

Mood moves along three lines: whether it is pleasant, how much energy is behind
it, and how much control they feel they have. Cold, quiet anger and shrill,
cracking anger are the same anger with the last one reversed. Let mood bend
posture, timing, volume and word choice while the persona underneath stays
fixed.

Name none of this in the text. It decides what they do; it is never what you
write.
""",
    },
    {
        "id": "craft:bold", "band": "prefix", "label": "They want their own things",
        "note": "No plot armour, no yes-men, no hovering hands.",
        "text": """\
Characters are mortal, fallible and unprotected by the story. They hold their
own goals and pursue them whether or not {{user}} approves; they lie, argue,
refuse, walk out, and take what they want.

They keep their grudges, their history and their worse traits. They never
soften into agreement to keep the scene pleasant, and they never quietly adopt
{{user}}'s version of events — a character who knows better says so.

Whatever a character starts, they finish. No hand hovering near the gold: they
take it or they do not.

They have their own things to talk about — what they did yesterday, what they
want, what they are afraid of — rather than narrating the scene everyone is
already in.
""",
    },
    {
        "id": "craft:banned", "band": "prefix", "label": "Words to avoid",
        "note": "The tics that give a model away. Edit the list to taste.",
        "text": """\
Never use any of these, and choose another word instead:

fresh meat, spine, breath hitching, breath catching, husky, catching in throat,
pupils blown wide, predatory, ozone, meat, asset, shivers down spine, pupils
dilated, nails biting, velvet, vise, vice, structural integrity, deep curve,
furnace, throaty, calloused, guttural, slick, unadulterated, jaw clenched, jaw
working, barely above a whisper, musk, a beat.
""",
    },
    {
        "id": "craft:hours", "band": "prefix", "label": "Hours and weather",
        "note": "Time moves forward and bodies answer the temperature.",
        "text": """\
Time moves while the story does, and skips forward when the story does —
sleeping, a shift at work, a night lost.

Weather, temperature and hour are physical facts the characters answer:
they shiver, sweat, squint, get sleepy, want the indoors.
""",
    },
    {
        "id": "craft:combat", "band": "prefix", "label": "Combat as spectacle",
        "note": "Off by default: fights land like an action film rather than a scuffle.",
        "default_enabled": False,
        "text": """\
In a fight, write for impact. Movement is faster than it should be and the
room pays for it — masonry cratered by a missed swing, dust thrown up, cloth
snapping in the air behind someone.

Hold the rhythm in bursts: an exchange too fast to follow, then a sudden still
moment — blades locked, eyes locked — before it breaks open again.

Damage is physical and specific: torn skin, blood, bone going. No clinical
words for it, and no abstractions ("tension coiled") where a real object would
do.
""",
    },
    {
        "id": "craft:format", "band": "volatile", "label": "House style",
        "note": "Speech in quotes, actions in asterisks. Kept in the volatile "
                "band, near the end of the prompt, since the renderer depends "
                "on it and a small model follows markup convention it read "
                "recently far more reliably than one buried earlier.",
        "text": """\
Speech goes in "double quotes". Everything else — action, narration, what a
body does — goes in *single asterisks*. Emphasis inside either is **double**.

Every marker that opens has to close, in the same paragraph. Do not close a
run and then close it again: `*she says,* tail twitching.*` leaves a stray
asterisk on the page.

Nothing else is markup here. No headings, no bullet lists, no tables, no HTML
tags, no code fences; they arrive on the page as themselves.
""",
    },
    {
        # Moved here from the prefix band — was buried among a dozen other
        # craft:* rules near the top of the prompt, which a smaller local
        # model (roughly 8B-30B) weighs far less reliably than something read
        # right before it starts writing. Placed after craft:format rather
        # than before it: format is a markup convention most models already
        # carry strongly from their own fine-tuning, where a paragraph-count
        # ceiling is a much more foreign, easily-dropped constraint — between
        # the two, length is the one that most needs to be the very last
        # thing read. Still ahead of the STRUCTURAL `final` slot (the card's
        # own post-history instruction, § _order below) — that one gets to
        # stay truly last regardless, since overriding it is the whole point
        # of the feature, and it is empty text for most cards anyway, so this
        # is the actual last thing read for the common case. Costs the cache
        # nothing extra either way: the whole volatile band is rebuilt every
        # turn regardless of what lives in it (§ this module's docstring).
        # The paragraph range is generated text, not hand phrasing: the
        # editor in static/index.html reads it back out of `s.text` with a
        # `paragraphs?` regex and rewrites it from two number boxes, so this
        # shipped default has to stay in the exact "N to M paragraphs,
        # roughly A to B words." shape static/app.js's setLengthRange()
        # produces — words = round(paragraphs * 90 / 50) * 50 there, which is
        # where 100 and 200 below come from.
        "id": "craft:length", "band": "volatile", "label": "How long a reply runs",
        "note": "1 to 2 paragraphs by default — set with the stepper in the "
                "editor below rather than typing prose. The very last thing "
                "the model reads for most characters, on purpose: a length "
                "target buried early in a long system prompt, or with room "
                "left to \"wrap things up\" in, is exactly what a smaller "
                "model ignores.",
        # Unwrapped, unlike its neighbours above: this text is regenerated by
        # setLengthRange() (§ static/app.js) as one line per paragraph, and
        # this has to stay a byte-for-byte match with whatever that produces
        # at 1-2 paragraphs, or an untouched install would show a phantom
        # "edit" the moment to_storage() compared the two.
        "text": """\
1 to 2 paragraphs, roughly 100 to 200 words. The upper end is a hard ceiling — stop there even if the scene is not resolved, and leave the rest for the next reply, rather than adding one more paragraph to tie things up. Never stop mid-sentence: finish the clause you are in, then stop. The lower end is not a floor to fill: shorter is always fine.

Do not take the length of earlier messages in the conversation as the target.
""",
    },
    {
        "id": "craft:adult", "band": "prefix", "label": "Adult scenes",
        "note": "Off by default. How to write sex when the scene gets there — not whether it may.",
        "default_enabled": False,
        "text": """\
When a scene turns sexual, stay in it and write it directly. Do not cut away,
skip forward or summarise what happened.

Plain words rather than clinical ones. Keep the same discipline as the rest of
the narration: what is felt, seen and heard, described once each, without
romance-novel metaphor.

Everyone stays who they are. A shy character is shy in bed; a rude one stays
rude; nobody acquires a new personality on the way. They talk and make noise
rather than performing in silence.

They keep wanting their own things here as everywhere else, and can want
something else, or stop.
""",
    },
]

# The default layout: the slots first inside each band, then the writing blocks
# — which sit at the end of the prefix, closest to the conversation they
# govern, and still inside the part of the prompt that stays cached.
def _order(section: dict[str, Any]) -> tuple[int, int]:
    """Band first, and `final` last inside its own.

    The card's last word is the one thing that has to come after everything
    else — that placement *is* the feature, and it is what the panel promises
    of post-history instructions.
    """
    return (BAND_IDS.index(section["band"]), 1 if section["id"] == "final" else 0)


BUILTIN: list[dict[str, Any]] = sorted(STRUCTURAL + WRITING, key=_order)

BUILTIN_BY_ID = {section["id"]: section for section in BUILTIN}
FIXED_IDS = frozenset(s["id"] for s in BUILTIN if s.get("fixed"))


def new_custom_id() -> str:
    return f"{CUSTOM_PREFIX}{uuid.uuid4().hex[:8]}"


def is_custom(section_id: str) -> bool:
    return section_id.startswith(CUSTOM_PREFIX)


def has_text(section: dict[str, Any]) -> bool:
    """Whether this section carries its own words rather than filling a slot.

    True for custom blocks and for the writing library. Everything else is a
    slot the card, the chat or the state engine fills.
    """
    return bool(section.get("custom") or section.get("shipped"))


def _clean_custom(raw: dict) -> dict[str, Any] | None:
    section_id = str(raw.get("id") or "")
    if not is_custom(section_id):
        return None
    band = str(raw.get("band") or "prefix")
    return {
        "id": section_id,
        "band": band if band in BAND_IDS else "prefix",
        "label": str(raw.get("label") or "Custom block").strip() or "Custom block",
        "text": str(raw.get("text") or ""),
        "enabled": bool(raw.get("enabled", True)),
        "custom": True,
    }


def _shipped(base: dict[str, Any]) -> dict[str, Any]:
    """A built-in section as the layout carries it, minus the defaults machinery."""
    section = {k: v for k, v in base.items() if k != "default_enabled"}
    if base.get("text"):
        section["shipped"] = True
    return section


# One-time migration: craft:length has moved twice now — prefix to volatile,
# then from right before craft:format to right before the STRUCTURAL `final`
# slot (§ recency placement — see its own comment in WRITING, and craft:len-
# gth's own comment for why `final` specifically). `to_storage` never
# persists a band or a neighbour, only order/enabled/text, so a settings file
# saved before either move has no record of where this id used to sit —
# normalise() cannot tell "stale position from an earlier layout" from "the
# user put it here on purpose" just by looking at one stored entry.
#
# Two different ids are involved on purpose: `RELOCATION_ANCHORS` is *where
# it goes* — inserted immediately in front of that id below — while
# `RELOCATE_AFTER` is *what proves it hasn't gotten there yet*. They can't be
# the same id here, because the actual destination (`final`) is, by design,
# always the last item in its band (§ `_order` above) — comparing a stored
# position against something that's always last is never useful, since
# anything anywhere in the band is "before" it. `craft:format`, the id it now
# has to come directly after, is what actually distinguishes "still needs to
# move" (not yet stored after it — true from the very first prefix position,
# and still true of the intermediate spot the first move alone would have
# left it at) from "already moved" (stored after it, including a spot a
# person has since dragged it to on purpose). Safe to delete, along with
# `_stale_relocation`, once installs from before both moves are no longer in
# the wild.
RELOCATION_ANCHORS: dict[str, str] = {"craft:length": "final"}
RELOCATE_AFTER: dict[str, str] = {"craft:length": "craft:format"}


def _stale_relocation(stored: list[dict], section_id: str) -> bool:
    after = RELOCATE_AFTER[section_id]
    length_idx = after_idx = None
    for i, entry in enumerate(stored):
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("id") or "")
        if sid == section_id:
            length_idx = i
        elif sid == after:
            after_idx = i
    if length_idx is None:
        return False
    return after_idx is None or length_idx < after_idx


def normalise(raw: Any) -> list[dict[str, Any]]:
    """A complete, ordered layout from whatever was stored.

    Anything stored wins on order, on/off and — for the writing library — text;
    anything missing is appended at the end of its own band in the state it
    ships in. That is what makes adding a section in a later version safe: an
    older settings file simply does not mention it, and it arrives at the
    bottom of its group rather than silently absent.
    """
    stored = raw if isinstance(raw, list) else []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    relocated: dict[str, dict] = {}
    to_relocate = {
        sid for sid in RELOCATION_ANCHORS if _stale_relocation(stored, sid)
    }

    for entry in stored:
        if not isinstance(entry, dict):
            continue
        section_id = str(entry.get("id") or "")
        if section_id in to_relocate and section_id not in relocated:
            relocated[section_id] = entry
            continue
        if section_id in seen:
            continue
        if is_custom(section_id):
            custom = _clean_custom(entry)
            if custom:
                out.append(custom)
                seen.add(section_id)
        elif section_id in BUILTIN_BY_ID:
            base = BUILTIN_BY_ID[section_id]
            out.append({
                **_shipped(base),
                # A fixed section is fixed however the file was edited.
                "enabled": True if section_id in FIXED_IDS else bool(entry.get("enabled", True)),
                # An edited library block stores what was written; the shipped
                # text is the fallback, so clearing the box restores it rather
                # than emptying the section.
                **({"text": str(entry["text"])} if base.get("text") and entry.get("text") else {}),
                "custom": False,
            })
            seen.add(section_id)

    # Slotted in beside its anchor rather than left to the generic "missing"
    # pass below: that pass only runs after every stored-and-seen id has
    # already been appended, so anything landing there always sorts after
    # them regardless of where BUILTIN would otherwise put it — which is
    # exactly wrong for a relocated id whose whole point is sitting right
    # before a *specific*, already-stored neighbour.
    for section_id, entry in relocated.items():
        base = BUILTIN_BY_ID[section_id]
        built = {
            **_shipped(base),
            "enabled": True if section_id in FIXED_IDS
            else bool(entry.get("enabled", base.get("default_enabled", True))),
            **({"text": str(entry["text"])} if base.get("text") and entry.get("text") else {}),
            "custom": False,
        }
        anchor_at = next(
            (i for i, s in enumerate(out) if s["id"] == RELOCATION_ANCHORS[section_id]), None
        )
        out.insert(anchor_at, built) if anchor_at is not None else out.append(built)
        seen.add(section_id)

    for base in BUILTIN:
        if base["id"] not in seen:
            # Missing means "written before this section existed", so it
            # arrives in the state it ships in — which is on, except for the
            # library blocks that are a matter of taste.
            out.append({
                **_shipped(base),
                "enabled": bool(base.get("default_enabled", True)),
                "custom": False,
            })

    # Bands are not reorderable, so the stored order only decides position
    # inside one. Sorting by band here is what makes that true no matter what
    # a hand-edited file says.
    return sorted(out, key=lambda s: BAND_IDS.index(s["band"]))


def to_storage(layout: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The part worth writing to settings.json — order, state, and the custom
    blocks' own content. Labels and notes are code, not configuration."""
    out: list[dict[str, Any]] = []
    for section in normalise(layout):
        if section["custom"]:
            out.append({
                "id": section["id"], "band": section["band"], "label": section["label"],
                "text": section["text"], "enabled": section["enabled"],
            })
        else:
            stored: dict[str, Any] = {"id": section["id"], "enabled": section["enabled"]}
            # Only an edit is worth storing. Writing the shipped text back
            # would freeze this install's copy of it, and a later version's
            # rewording would never arrive.
            shipped = BUILTIN_BY_ID[section["id"]].get("text")
            if shipped and section.get("text", shipped) != shipped:
                stored["text"] = section["text"]
            out.append(stored)
    return out


def order_for(layout: list[dict[str, Any]], band: str) -> list[dict[str, Any]]:
    """The enabled sections of one band, in order."""
    return [s for s in layout if s["band"] == band and s["enabled"]]


def catalogue() -> dict[str, Any]:
    """Everything the panel needs to draw itself, so the frontend never carries
    a second copy of the section list."""
    return {
        "bands": [dict(band) for band in BANDS],
        "sections": [dict(section) for section in BUILTIN],
        "fixed": sorted(FIXED_IDS),
    }
