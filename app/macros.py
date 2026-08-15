"""`{{macro}}` substitution (§7.1).

Character cards are written with placeholders — `{{char}}` for the character,
`{{user}}` for whoever is reading — because a card is meant to be portable
between people. Without substitution an imported card is not merely cosmetically
wrong: the model is told, in its system prompt, that it is talking to someone
literally named "{{user}}", and it will say so.

Where this runs matters. Card text is substituted at **assembly** time, not
when it is stored, because the answers change: the active persona can be
switched between turns and `{{time}}` is different every turn. Message text is
substituted **once, when the message is created**, because a message is a
record of something that was said — rewriting yesterday's greeting because the
persona was renamed today would be falsifying the transcript.

Unknown macros are left alone rather than blanked. A typo that survives to the
screen is a bug someone can see; one that silently deletes the sentence around
it is not.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

# `{{ name : argument }}` — the name is a word, the argument is everything up to
# the closing braces. Nothing inside may contain braces, which is what stops a
# nested macro from being swallowed by its parent's argument on the first pass;
# the inner one resolves first and the outer sees plain text on the next.
MACRO = re.compile(r"\{\{\s*([A-Za-z_][\w-]*)\s*(?::\s*([^{}]*?))?\s*\}\}")

# Nesting is resolved by substituting repeatedly. The cap is not a guess at how
# deep anyone nests — it is what stops a macro that expands to itself from
# hanging the turn.
MAX_PASSES = 6


@dataclass
class MacroContext:
    """Everything the macros can resolve to.

    Deliberately plain data: assembly builds one of these per prompt, and the
    tests build one by hand. Nothing in here reaches out to the database.
    """

    char: str = ""
    user: str = "You"
    persona: str = ""          # the user's own description
    description: str = ""      # the character's
    scenario: str = ""
    personality: str = ""
    # Seconds since the last message, for {{idle_duration}}. None when there is
    # no previous message to be idle since.
    idle_seconds: float | None = None
    # Seeded per chat so {{pick}} is stable: the same card text picks the same
    # option every turn, which is the difference between a character who has one
    # scar and one who has a different scar every time they are described.
    seed: str = ""
    extra: dict[str, str] = field(default_factory=dict)


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}{'' if count == 1 else 's'}"


def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return _plural(seconds, "second")
    if seconds < 3600:
        return _plural(seconds // 60, "minute")
    if seconds < 86400:
        return _plural(seconds // 3600, "hour")
    return _plural(seconds // 86400, "day")


def _options(argument: str) -> list[str]:
    """Split a comma-separated macro argument, keeping empty choices out."""
    return [part.strip() for part in argument.split(",") if part.strip()]


def _roll(argument: str, rng: random.Random) -> str:
    """`{{roll:d20}}`, `{{roll:2d6}}`, or `{{roll:20}}` for a plain 1..n."""
    text = argument.strip().lower().replace(" ", "")
    match = re.fullmatch(r"(\d*)d(\d+)", text)
    if match:
        count = int(match.group(1) or 1)
        sides = int(match.group(2))
    elif text.isdigit():
        count, sides = 1, int(text)
    else:
        return ""
    if sides < 1 or count < 1 or count > 100:
        return ""
    return str(sum(rng.randint(1, sides) for _ in range(count)))


def _handlers(ctx: MacroContext, rng: random.Random) -> dict[str, Callable[[str], str]]:
    now = datetime.now()
    return {
        # Who is in the room.
        "char": lambda _: ctx.char,
        "bot": lambda _: ctx.char,
        "user": lambda _: ctx.user,
        "persona": lambda _: ctx.persona,
        "description": lambda _: ctx.description,
        "personality": lambda _: ctx.personality or ctx.description,
        "scenario": lambda _: ctx.scenario,
        # When it is. Local time, because the reader is in it.
        "time": lambda _: now.strftime("%H:%M"),
        "date": lambda _: now.strftime("%d %B %Y"),
        "weekday": lambda _: now.strftime("%A"),
        "isotime": lambda _: now.strftime("%H:%M:%S"),
        "isodate": lambda _: now.strftime("%Y-%m-%d"),
        "utctime": lambda _: datetime.now(timezone.utc).strftime("%H:%M"),
        "idle_duration": lambda _: (
            _duration(ctx.idle_seconds) if ctx.idle_seconds is not None else "no time at all"
        ),
        # Chance. `random` re-rolls every turn; `pick` is stable for the chat,
        # so a detail chosen once stays chosen.
        "random": lambda arg: (_options(arg) or [""])[rng.randrange(len(_options(arg)) or 1)],
        "pick": lambda arg: _pick(arg, ctx),
        "roll": lambda arg: _roll(arg, rng),
        # Typography and escapes.
        "newline": lambda _: "\n",
        "trim": lambda _: "\x01TRIM\x01",
        "noop": lambda _: "",
        "comment": lambda _: "",
        "//": lambda _: "",
    }


def _pick(argument: str, ctx: MacroContext) -> str:
    options = _options(argument)
    if not options:
        return ""
    # Seeded on the chat *and* the choice list, so two different {{pick}}s in
    # one card do not both land on their first option.
    stable = random.Random(f"{ctx.seed}\x00{argument}")
    return options[stable.randrange(len(options))]


def substitute(text: str, ctx: MacroContext | None = None) -> str:
    """Resolve every macro in `text`. Unknown ones are left as they are."""
    if not text or "{{" not in text:
        return text
    ctx = ctx or MacroContext()
    rng = random.Random()

    result = text
    for _ in range(MAX_PASSES):
        handlers = _handlers(ctx, rng)

        def replace(match: re.Match[str]) -> str:
            name = match.group(1).lower()
            argument = match.group(2) or ""
            if name in ctx.extra:
                return ctx.extra[name]
            handler = handlers.get(name)
            if handler is None:
                return match.group(0)   # not ours; leave it for someone to see
            return handler(argument)

        replaced = MACRO.sub(replace, result)
        if replaced == result:
            break
        result = replaced

    return _apply_trim(result)


def _apply_trim(text: str) -> str:
    """`{{trim}}` eats the whitespace either side of where it stood.

    It is the one macro whose job is to remove text rather than add it, so it
    leaves a marker during substitution and is resolved here, once, when
    neighbouring macros have already produced whatever they were going to.
    """
    if "\x01TRIM\x01" not in text:
        return text
    return re.sub(r"\s*\x01TRIM\x01\s*", "", text)


def context_from(
    character: Any = None,
    persona: Any = None,
    *,
    seed: str = "",
    idle_seconds: float | None = None,
) -> MacroContext:
    """Build a context from a Character and a persona row.

    Both are optional: a pass that only needs `{{time}}` should not have to
    invent a character, and a chat with no persona chosen still resolves
    `{{user}}` to something a model can read.
    """
    ctx = MacroContext(seed=seed, idle_seconds=idle_seconds)
    if character is not None:
        ctx.char = getattr(character, "name", "") or ""
        ctx.description = getattr(character, "persona", "") or ""
        ctx.personality = ctx.description
        ctx.scenario = getattr(character, "scenario", "") or ""
    if persona:
        name = persona.get("name") if isinstance(persona, dict) else getattr(persona, "name", "")
        text = (
            persona.get("description") if isinstance(persona, dict)
            else getattr(persona, "description", "")
        )
        ctx.user = (name or "").strip() or ctx.user
        ctx.persona = (text or "").strip()
    return ctx


def idle_since(last_message_at: float | None) -> float | None:
    return None if not last_message_at else max(0.0, time.time() - last_message_at)
