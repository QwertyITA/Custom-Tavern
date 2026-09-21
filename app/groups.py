"""Several characters in one conversation (§15, roadmap 8).

Membership, and the two questions that actually make a group chat work:
**who speaks next**, and **how the model is told who said what**.

The turn-order policy is deliberately not round-robin by default. Round-robin
is the arrangement where you say something to one person and the other one
answers, forever, and it is the single thing that makes group chats read as a
mechanism rather than as a room. The default policy is free — no model call —
and works in the order a person would expect:

1. **Addressed by name.** Everyone the message names answers, in the order it
   names them. It costs a substring search.
2. **Then whoever would speak up**, each member rolling against their own
   talkativeness, with the one who just spoke sitting this one out.

A turn can produce **several replies**, capped by the chat's own "how many
answer" setting. That is the difference between a room and a switchboard: the
first version of this picked exactly one speaker per message, so nobody could
ever react to what somebody else had just said, and naming two people got you
neither of them.

Muted characters never speak, but stay in the prompt: someone standing there
saying nothing is still in the scene, and dropping them from the context would
have the others talk as if the room were empty.
"""

from __future__ import annotations

import json

import random
import re
from typing import Any

from .db import Database, now

POLICIES: list[dict[str, str]] = [
    {"id": "natural", "label": "Whoever would answer",
     "note": "Named in your message, then whoever would speak up. Costs nothing."},
    {"id": "pooled", "label": "Everyone gets a turn",
     "note": "Nobody speaks twice until everybody has spoken once."},
    {"id": "round_robin", "label": "Take turns",
     "note": "Strict order. Predictable, and reads like a mechanism."},
    {"id": "manual", "label": "You choose",
     "note": "Pick who replies before each message."},
]

POLICY_IDS = tuple(policy["id"] for policy in POLICIES)
DEFAULT_POLICY = "natural"

# How many characters may answer one message. More than one is what makes a
# group read as a room — but every extra reply is another whole generation,
# and on a phone talking to the Horde that is another minute of waiting, so
# the ceiling is low and the default is two rather than "however many rolled".
DEFAULT_REPLIES_PER_TURN = 2
MAX_REPLIES_PER_TURN = 4

# How much each character is told about the others (§ cast_note). Names alone
# is what this shipped with, and it is why a group's characters wrote each
# other as whatever their names sounded like.
CAST_DETAIL = [
    {"id": "names", "label": "Just their names",
     "note": "Cheapest. They know who is here and nothing else."},
    {"id": "brief", "label": "A few lines each",
     "note": "The top of each card. Enough to not contradict them."},
    {"id": "full", "label": "Their whole card",
     "note": "Everything. Accurate, and the most expensive thing in the prompt."},
]
CAST_DETAIL_IDS = tuple(detail["id"] for detail in CAST_DETAIL)
DEFAULT_CAST_DETAIL = "brief"

# How the room's cards reach the prompt. SillyTavern's `generation_mode`
# (group-chats.js), and the reason it exists is the one nobody thinks of
# until they watch a phone do it: the prompt is rebuilt for whoever is
# speaking, and a prompt that changes at its *first* token cannot reuse a
# single byte of the backend's KV cache.
#
# "swap" is the obvious arrangement and what this shipped with — the speaker's
# own card at the top, everyone else summarised after it. Measured on a group
# of two with a thirty-message history: the two prompts share **8 characters**
# ("You are ") out of 9,114, so every speaker change re-reads the whole thing,
# card, writing blocks, transcript and all. Two characters taking turns means
# that happens on every single reply.
#
# "join" is SillyTavern's APPEND: every member's description, scenario and
# examples are concatenated in join order, so the prompt is byte-identical
# whoever is about to speak, and the only thing that says whose turn it is
# lives at the very end (§ turn_note — the same job ST's trailing "Name:"
# does). Same measurement: ~3,500 of 3,813 tokens shared.
#
# It is not free, and which way is better depends on the backend. Join sends
# every card every turn, so on the Horde — where each job goes to a different
# worker and no cache survives between them — it is simply a bigger prompt.
# On anything with a KV cache that lives between turns (Ollama, llama.cpp, the
# on-device tier) it is most of a turn's compute.
CARD_MODES = [
    {"id": "join", "label": "Everyone's, every time",
     "note": "One prompt for the whole room, so the backend can reuse it "
             "between speakers. Bigger, and cached."},
    {"id": "swap", "label": "Only whoever is speaking",
     "note": "Their card, and a summary of the others. Smaller, and re-read "
             "from the first word on every change of speaker."},
]
CARD_MODE_IDS = tuple(mode["id"] for mode in CARD_MODES)
DEFAULT_CARD_MODE = "join"
# What "a few lines" is worth in characters. Cut at a paragraph or sentence
# end inside this (§ _brief), never mid-word.
BRIEF_CHARS = 420


def settings_for(chat: dict | None) -> dict[str, Any]:
    """The group's own settings, from whatever the chat has stored.

    Flat on `chat.settings` rather than nested under a "group" key: `policy`
    has lived there since roadmap 8 and moving it would mean a migration for
    a saving of nothing. Every value is clamped here, so a hand-edited
    database cannot put the planner into a state the UI has no way out of.
    """
    raw = (chat or {}).get("settings") or {}
    policy = str(raw.get("policy") or "")
    detail = str(raw.get("cast_detail") or "")
    try:
        replies = int(raw.get("replies_per_turn") or DEFAULT_REPLIES_PER_TURN)
    except (TypeError, ValueError):
        replies = DEFAULT_REPLIES_PER_TURN
    return {
        "policy": policy if policy in POLICY_IDS else DEFAULT_POLICY,
        "replies_per_turn": max(1, min(replies, MAX_REPLIES_PER_TURN)),
        "self_responses": bool(raw.get("self_responses", False)),
        "cast_detail": detail if detail in CAST_DETAIL_IDS else DEFAULT_CAST_DETAIL,
        "cards": cards if (cards := str(raw.get("cards") or "")) in CARD_MODE_IDS
        else DEFAULT_CARD_MODE,
    }


def _brief(text: str) -> str:
    """The top of a card, cut where it stops making a sentence."""
    text = (text or "").strip()
    if len(text) <= BRIEF_CHARS:
        return text
    head = text[:BRIEF_CHARS]
    for end in ("\n\n", ". ", "\n"):
        cut = head.rfind(end)
        if cut > BRIEF_CHARS // 3:
            return head[: cut + (1 if end == ". " else 0)].strip()
    return head.rsplit(" ", 1)[0].strip() + "…"


def profile_for(character: Any, detail: str) -> str:
    """One member's line in somebody else's cast note (§ cast_note)."""
    if detail == "names":
        return ""
    persona = (getattr(character, "persona", "") or "").strip()
    return persona if detail == "full" else _brief(persona)


def members(db: Database, chat_id: str) -> list[dict[str, Any]]:
    """Everyone in this chat, in join order, with their card details."""
    rows = db.query(
        "SELECT m.character_id, m.muted, m.talkativeness, m.joined_at, c.name, c.data "
        "FROM chat_members m JOIN characters c ON c.id = m.character_id "
        "WHERE m.chat_id=? ORDER BY m.joined_at, m.rowid",
        (chat_id,),
    )
    return [
        {
            "character_id": row["character_id"],
            "name": row["name"],
            "muted": bool(row["muted"]),
            "talkativeness": float(row["talkativeness"]),
            "joined_at": row["joined_at"],
            # So a row in a group chat can carry the right face. Neutral only:
            # the expression slice is per chat, not per member, and a list of
            # faces nobody is showing is a list of files to load.
            "pfp": _neutral_pfp(row["data"]),
            # And the shape it is drawn in: two members of one group can be
            # framed differently, and each row has to know which.
            "pfp_shape": _pfp_shape(row["data"]),
            # Same for a colour treatment — it belongs to the member, not to
            # the room they are standing in.
            "pfp_effect": _pfp_effect(row["data"]),
        }
        for row in rows
    ]


def voices(db: Database, chat_id: str) -> list[dict[str, Any]]:
    """Everyone who has a line in this chat, whether or not they are still in
    it — the same card details `members` returns, for the same reason.

    Membership answers "who can speak next"; this answers "who said that",
    and the two stop agreeing the moment somebody is removed from a group.
    Their lines stay in the transcript, and a transcript where a departed
    character's lines wear whoever is left's face is not a record of the
    conversation that happened. Reported live as the wrong picture showing
    up against a message.

    Anyone whose card has since been deleted outright is simply absent — the
    JOIN drops them — and the frontend then draws the blank placeholder,
    which is the honest answer to a face nothing knows any more.
    """
    rows = db.query(
        "SELECT DISTINCT m.speaker_id, c.name, c.data "
        "FROM messages m JOIN characters c ON c.id = m.speaker_id "
        "WHERE m.chat_id=? AND m.speaker_id != '' ORDER BY c.name",
        (chat_id,),
    )
    return [
        {
            "character_id": row["speaker_id"],
            "name": row["name"],
            "pfp": _neutral_pfp(row["data"]),
            "pfp_shape": _pfp_shape(row["data"]),
            "pfp_effect": _pfp_effect(row["data"]),
        }
        for row in rows
    ]


def _card(raw: Any) -> dict:
    try:
        card = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return card if isinstance(card, dict) else {}


def _neutral_pfp(raw: Any) -> str:
    pfp_set = _card(raw).get("pfp_set") or {}
    if not isinstance(pfp_set, dict):
        return ""
    return str(pfp_set.get("neutral") or next(iter(pfp_set.values()), "") or "")


def _pfp_shape(raw: Any) -> str:
    return "square" if _card(raw).get("pfp_shape") == "square" else "portrait"


def _pfp_effect(raw: Any) -> dict:
    effect = _card(raw).get("pfp_effect")
    return effect if isinstance(effect, dict) else {}


def is_group(db: Database, chat_id: str) -> bool:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM chat_members WHERE chat_id=?", (chat_id,)
    )
    return bool(row and row["n"] > 1)


def add_member(
    db: Database, chat_id: str, character_id: str, *, talkativeness: float = 1.0
) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "INSERT INTO chat_members(chat_id, character_id, muted, talkativeness, "
            "joined_at) VALUES(?,?,0,?,?) ON CONFLICT(chat_id, character_id) DO NOTHING",
            (chat_id, character_id, _clamp(talkativeness), now()),
        )
    )


def remove_member(db: Database, chat_id: str, character_id: str) -> None:
    db.write_sync(
        lambda conn: conn.execute(
            "DELETE FROM chat_members WHERE chat_id=? AND character_id=?",
            (chat_id, character_id),
        )
    )


def update_member(
    db: Database,
    chat_id: str,
    character_id: str,
    *,
    muted: bool | None = None,
    talkativeness: float | None = None,
) -> None:
    sets, values = [], []
    if muted is not None:
        sets.append("muted=?")
        values.append(int(muted))
    if talkativeness is not None:
        sets.append("talkativeness=?")
        values.append(_clamp(talkativeness))
    if not sets:
        return
    values.extend([chat_id, character_id])
    db.write_sync(
        lambda conn: conn.execute(
            f"UPDATE chat_members SET {', '.join(sets)} "
            "WHERE chat_id=? AND character_id=?",
            tuple(values),
        )
    )


def _clamp(value: Any) -> float:
    try:
        return max(0.0, min(2.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def ensure_member(db: Database, chat_id: str, character_id: str) -> None:
    """Make sure a chat's own character is in its member list.

    Every chat has one from creation; this exists for the ones that predate the
    table, so opening an old chat quietly makes it a group of one rather than a
    group of none — which would have nobody to reply.
    """
    row = db.query_one(
        "SELECT 1 FROM chat_members WHERE chat_id=? AND character_id=?",
        (chat_id, character_id),
    )
    if row is None:
        add_member(db, chat_id, character_id)


# ------------------------------------------------------------ who speaks


def mentions(text: str, candidates: list[dict]) -> list[dict]:
    """Everyone named in this message, in the order they are first named.

    Two details that are easy to get wrong and both show up immediately in a
    real room:

    * Boundaries are lookarounds, not `\\b`. A name ending in punctuation —
      "R. Vale (the elder)" — has no word boundary after the bracket, so `\\b`
      simply never matches it.
    * Longest name first, blanking what it matched. Otherwise "Anna Vale"
      also matches "Anna" standing beside her and the message looks like it
      named two people when it named one. Blanking keeps the string's length,
      so the offsets collected here still point into the original text and
      can be sorted back into reading order.
    """
    if not text:
        return []
    remaining = text
    hits: list[tuple[int, dict]] = []
    for person in sorted(candidates, key=lambda c: -len(c["name"] or "")):
        name = person["name"]
        if not name:
            continue
        found = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", remaining, re.IGNORECASE)
        if not found:
            continue
        hits.append((found.start(), person))
        remaining = (
            remaining[: found.start()]
            + " " * (found.end() - found.start())
            + remaining[found.end() :]
        )
    return [person for _, person in sorted(hits, key=lambda hit: hit[0])]


def addressed(text: str, candidates: list[dict]) -> dict | None:
    """The character named in this message, if exactly one is.

    Kept for the single-speaker question — "who is this message for" — which
    is not the same question the turn planner asks. A message naming two
    people has not chosen between them, and picking the first would be
    arbitrary; `plan` lets both of them answer instead, which is what a
    person naming two people actually meant.
    """
    named = mentions(text, candidates)
    return named[0] if len(named) == 1 else None


def plan(
    available: list[dict],
    *,
    policy: str = DEFAULT_POLICY,
    user_text: str = "",
    last_speaker: str = "",
    forced: str = "",
    replies: int = DEFAULT_REPLIES_PER_TURN,
    self_responses: bool = False,
    spoken_since_user: tuple[str, ...] = (),
    is_user_input: bool = True,
    rng: Any = None,
) -> list[dict]:
    """Who answers this turn, in the order they speak. Possibly nobody.

    A room does not take it in turns to say one thing each. The old version
    of this picked exactly one speaker per message, which is why naming two
    people got you neither, why a character could never react to what another
    one had just said, and why a group of four read as a switchboard. This
    returns a *list*, capped by the chat's own "how many answer" setting.

    `forced` wins outright — it is either the manual policy's choice or a
    deliberate "let them answer" — as long as that character is here and not
    muted. Asking a muted character to speak is a contradiction worth
    ignoring rather than honouring.

    `self_responses` decides whether the character who spoke last may follow
    their own line. Off by default — but *only when nothing was said to them
    in between*, which is the whole of the rule and the half that is easy to
    get wrong. Getting it wrong is worse than not having it: a blanket ban
    in a room of two makes the pair take strict turns forever, which is the
    round-robin mechanism this module's whole first paragraph is against, and
    it means the obvious thing — saying hello and having them both answer —
    cannot happen at all.

    So the ban is `is_user_input`-gated, exactly as SillyTavern's is
    (`!isUserInput && …`, activateNaturalOrder). You spoke, so everyone in
    the room may answer you, including whoever spoke last. Nobody spoke and
    the room is carrying on by itself (§ scheduler._run_proceed) — then the
    one who just finished talking sits this one out, because otherwise
    "carry on" is one character monologuing. Naming them lifts it either
    way: if you ask the same character something twice, you meant them.
    """
    picker = rng if rng is not None else random
    available = [m for m in available if not m["muted"]]
    if not available:
        return []
    if forced:
        chosen = next((m for m in available if m["character_id"] == forced), None)
        return [chosen] if chosen else []
    if policy == "manual":
        # Nothing was chosen, so nobody speaks. The UI asks before sending;
        # reaching here means the request did not say, and inventing a speaker
        # would defeat the point of the policy.
        return []
    if len(available) == 1:
        return available[:1]

    room = max(1, min(int(replies or 1), MAX_REPLIES_PER_TURN))
    # Nobody follows their own line while somebody else could — but only when
    # nothing was said to them in between (§ the docstring above). Lifted for
    # an explicit mention below, and never applied when they are all there is.
    banned = "" if (self_responses or is_user_input) else last_speaker

    if policy == "round_robin":
        names = [m["character_id"] for m in available]
        start = names.index(last_speaker) + 1 if last_speaker in names else 0
        return [available[(start + i) % len(available)] for i in range(min(room, len(available)))]

    if policy == "pooled":
        # Everyone gets a turn before anyone gets a second one — the policy
        # for a scene where nobody should be left standing silent in the
        # corner for twenty messages. Ported from SillyTavern's pooled order.
        waiting = [m for m in available if m["character_id"] not in spoken_since_user]
        rest = [m for m in available if m["character_id"] in spoken_since_user]
        picker.shuffle(waiting)
        picker.shuffle(rest)
        ordered = waiting + rest
        if banned and len(ordered) > 1 and ordered[0]["character_id"] == banned:
            ordered.append(ordered.pop(0))
        return ordered[:room]

    # natural
    picked: list[dict] = []
    for person in mentions(user_text, available):
        # Being named beats the self-response ban: asking the same character
        # a second question means you want them, not the person beside them.
        if person not in picked:
            picked.append(person)

    rolling = [
        m for m in available
        if m not in picked and m["character_id"] != banned
    ]
    picker.shuffle(rolling)
    for person in rolling:
        # SillyTavern's roll, and it is the right shape: talkativeness is the
        # chance of speaking up unprompted, not a share of a single slot that
        # somebody has to win. 1.0 always joins in, 0 never does.
        if person["talkativeness"] >= picker.random():
            picked.append(person)

    if not picked:
        # Nobody rolled in. Somebody still has to answer, so fall back to one
        # weighted pick — the old behaviour, now only the floor rather than
        # the whole mechanism.
        pool = [m for m in available if m["character_id"] != banned] or available
        weights = [max(0.01, m["talkativeness"]) for m in pool]
        picked = picker.choices(pool, weights=weights, k=1)
    return picked[:room]


def plan_turn(
    db: Database,
    chat_id: str,
    *,
    chat: dict | None = None,
    user_text: str = "",
    forced: str = "",
    is_user_input: bool = True,
    seed: Any = None,
) -> list[dict]:
    """`plan`, with everything it needs read off the chat (§ settings_for).

    `is_user_input` is false for the one caller that answers nothing — the
    room carrying on by itself (§ scheduler._run_proceed). It is not the same
    question as "is `user_text` empty": a message can be an attachment with
    no words in it and still be you speaking.
    """
    config = settings_for(chat if chat is not None else {})
    return plan(
        members(db, chat_id),
        policy=config["policy"],
        user_text=user_text,
        last_speaker=last_speaker(db, chat_id),
        forced=forced,
        replies=config["replies_per_turn"],
        self_responses=config["self_responses"],
        spoken_since_user=spoken_since_user(db, chat_id),
        is_user_input=is_user_input,
        rng=random.Random(seed) if seed is not None else random,
    )


def choose_speaker(
    db: Database,
    chat_id: str,
    *,
    policy: str = DEFAULT_POLICY,
    user_text: str = "",
    last_speaker: str = "",
    forced: str = "",
    seed: Any = None,
) -> dict | None:
    """The first of `plan`'s speakers, for the callers that only want one.

    Everything that answers a *turn* goes through `plan_turn`; this is what
    a single-reply path (a retry, a nudge) asks when it needs one name.
    """
    speakers = plan(
        members(db, chat_id),
        policy=policy,
        user_text=user_text,
        last_speaker=last_speaker,
        forced=forced,
        replies=1,
        # A caller asking for one name has a message in hand — this is the
        # single-speaker shape of an ordinary turn, never of a continuation.
        is_user_input=True,
        spoken_since_user=spoken_since_user(db, chat_id),
        rng=random.Random(seed) if seed is not None else random,
    )
    return speakers[0] if speakers else None


def last_speaker(db: Database, chat_id: str) -> str:
    row = db.query_one(
        "SELECT speaker_id FROM messages WHERE chat_id=? AND role='assistant' "
        "AND speaker_id != '' ORDER BY turn DESC, created_at DESC LIMIT 1",
        (chat_id,),
    )
    return row["speaker_id"] if row else ""


def spoken_since_user(db: Database, chat_id: str) -> tuple[str, ...]:
    """Who has already spoken since the last thing the person said.

    What the pooled policy runs on, and the only reason it can promise that
    everybody gets a turn: the pool empties as the scene goes and refills the
    moment you say something yourself.
    """
    rows = db.query(
        "SELECT role, speaker_id FROM messages WHERE chat_id=? "
        "ORDER BY turn DESC, created_at DESC LIMIT 40",
        (chat_id,),
    )
    seen: list[str] = []
    for row in rows:
        if row["role"] == "user":
            break
        if row["role"] == "assistant" and row["speaker_id"]:
            seen.append(row["speaker_id"])
    return tuple(seen)


# ------------------------------------------------------------ the prompt


def cast_note(
    members_here: list[dict],
    speaking: str,
    *,
    profiles: dict[str, str] | None = None,
) -> str:
    """Who else is in the room, for the speaker's prompt.

    Names alone were not enough, and that is most of what "the model does not
    understand a group chat" actually was. A character told only that "Harrow
    and Anna" are present has no idea who they are, so it writes them as
    whatever the name sounds like — and then contradicts their own cards two
    lines later. SillyTavern's join-cards mode puts every member's
    description in the prompt for exactly this reason; `profiles` is that,
    per character, sized by the chat's own setting (§ settings_for).

    Muted characters are listed too. Someone standing there saying nothing is
    still in the scene, and leaving them out would have the others talk as if
    the room were empty.
    """
    others = [m for m in members_here if m["character_id"] != speaking]
    if not others:
        return ""
    profiles = profiles or {}
    lines = []
    for other in others:
        about = (profiles.get(other["character_id"]) or "").strip()
        lines.append(f"**{other['name']}**" + (f" — {about}" if about else ""))
    return (
        "## Also here\n"
        + "\n".join(lines)
        + "\nThey are present and may be spoken to or about, but you write only "
        "your own words — never theirs."
    )


def joined_cards(entries: list[tuple[str, str]], heading: str) -> str:
    """Every member's text for one field, in join order, under one heading.

    SillyTavern's `collectField` (group-chats.js), with its join prefix and
    suffix fixed here rather than exposed as two more text boxes — the shape
    below is what its default templates produce anyway, and a room whose
    members are told apart by a heading is the thing being bought.

    Join order, not speaking order, and that is the whole point: the block has
    to come out byte-identical whoever is about to speak, or the prompt
    diverges again and the cache it was written to save is gone.
    """
    seen: list[str] = []
    for name, text in entries:
        body = (text or "").strip()
        if not body:
            continue
        # A scenario shared by everybody in the room is one scenario, not
        # three. SillyTavern repeats it; on a phone that is the same paragraph
        # paid for once per member, every turn.
        block = f"### {name}\n{body}"
        if body in [b.split("\n", 1)[1] for b in seen]:
            seen[-1] = f"{seen[-1].split(chr(10), 1)[0]}, {name}\n{body}"
            continue
        seen.append(block)
    return f"## {heading}\n" + "\n\n".join(seen) if seen else ""


def room_instruction(names: list[str]) -> str:
    """The default main instruction for a joined room.

    Names nobody, on purpose. This is the first thing in the prompt, and the
    moment it says "You are Mira" the whole prefix belongs to Mira and nothing
    behind it can be reused for Harrow. Who is speaking is the last thing the
    model reads instead (§ turn_note), which is where SillyTavern puts it too.
    """
    room = ", ".join(names)
    return (
        f"This is a conversation between several characters: {room}. "
        "You write one of them at a time — the one named at the end of this "
        "prompt — and never the others. Stay in character, and reply in prose."
    )


def turn_note(speaking: str, others: list[str]) -> str:
    """The last thing the model reads before it answers: whose line this is.

    In the volatile band on purpose (§7.1). A group chat's prompt is already
    rebuilt per speaker — the persona in the prefix is a different person's —
    so this costs no cache that was not already spent, and recency is the
    whole point: the rule that keeps a reply to one voice has to be the most
    recent instruction, not the first.
    """
    if not others:
        return ""
    room = ", ".join(others)
    return (
        f"## Your turn\n"
        f"The transcript above labels each line with who said it. Those "
        f"labels are how you read it; they are not how you answer.\n"
        f"You are {speaking}. Write {speaking}'s next line only — do not "
        f"write a line for {room}, do not narrate what they say or do next, "
        f"and do not put a name label on your own reply."
    )
