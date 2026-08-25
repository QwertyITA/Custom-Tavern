"""Prompt assembly pipeline and eviction ladder (§7.1, §7.2).

Assembly order is fixed, and the ordering *is* the optimisation:

    [stable prefix]   system / persona / constant lorebook   ← rarely changes
    [dynamic middle]  lorebook hits, memories, summary, recent messages
    [volatile suffix] state bands, toggle injections          ← changes every turn

State bands and toggles change on every single turn. Putting them anywhere but
last would invalidate the KV cache of everything after them, so on a local
Ollama the whole prefix would be recomputed each turn. Last is therefore not a
stylistic choice, it is the cache rule.

Trimming happens through the eviction ladder rather than by cutting the prefix:
verbatim → summarized → compressed → dropped, and a message is only ever
dropped once the summary and memory passes have covered it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings
from .db import Database
from .lorebook import render as render_lore
from .lorebook import scan as scan_lore
from .markup import to_plain
from . import attachments
from . import groups
from . import macros
from . import prompt_layout
from . import translation
from . import websearch
from . import worldline
from .models import AuthorsNote, Character, VariableSchema
from .providers.base import estimate_tokens
from .state import SLICE_EVENT, SLICE_SCENE, SLICE_SEARCH, SLICE_VARS
from .state import initial_values, load_schema, read_slice
from .state import slice_for
from .state import render_bands
from . import memory as memory_store
from . import repo

Message = dict[str, str]

MEMORY_COVERED_KEY = "memory_covered:{chat_id}"

# "The memory pass has never run", as distinct from "it has covered turn 0".
# Those were the same number, and turn 0 is the opening message — the scenario,
# every time — so the one message carrying the premise was droppable on a chat
# where memory had never run once. No test caught it because `add_message`
# numbers turns from 1 and nothing but the greeting is ever turn 0.
MEMORY_NEVER = -1

# How full the prompt has to be before anything is thrown out of it. Eviction
# used to be driven by a message count alone, so a chat sitting at an eighth of
# its context budget still lost its opening to make room that was not needed.
# Nothing leaves while there is room for it.
EVICTION_PRESSURE = 0.85

# Left over the top of the budget for what assembly cannot see: the output
# contract, which the scheduler appends after this runs, and four characters to
# a token being close rather than exact.
BUDGET_SLACK = 320


@dataclass
class Assembled:
    system: str = ""
    messages: list[Message] = field(default_factory=list)
    volatile: str = ""
    sections: dict[str, int] = field(default_factory=dict)
    lore_hits: list[str] = field(default_factory=list)
    memories: list[dict] = field(default_factory=list)
    trimmed: int = 0  # messages cut by the token budget this turn
    # The turn of the oldest message actually sent, not counting the pinned
    # opening. Everything before it has left the prompt, and that — rather than
    # a turn count — is what the summary pass is for.
    window_from: int = 0
    # Base64 images from the newest turn's attachments (§19). Filled only when
    # the backend that will receive this can actually see them.
    images: list[str] = field(default_factory=list)
    # Every built section in prompt order, with the text it contributed (§15).
    # `sections` above is the same information collapsed to a total per kind,
    # which is what the live HUD wants; this is what "show me what was sent"
    # wants, and the two are filled from the same place so they cannot drift.
    parts: list[dict] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return sum(self.sections.values())


def _section(assembled: Assembled, name: str, text: str) -> None:
    if text:
        assembled.sections[name] = estimate_tokens(text)


def _part(assembled: Assembled, section: dict, text: str) -> str:
    """Record one built section and hand the text back for assembly."""
    if text:
        assembled.parts.append({
            "id": section["id"],
            "label": section["label"],
            "band": section["band"],
            "custom": bool(section.get("custom")),
            "tokens": estimate_tokens(text),
            "text": text,
        })
    return text


def current_values(
    db: Database,
    chat_id: str,
    schema: dict[str, VariableSchema],
    character_id: str = "",
) -> dict[str, float]:
    """This character's variables in this chat (§15).

    Namespaced by character, so two characters in one room hold their own
    opinion of you rather than sharing and overwriting one.
    """
    stored = read_slice(db, chat_id, slice_for(SLICE_VARS, character_id))
    values = initial_values(schema)
    if stored and isinstance(stored["value"], dict):
        values.update({k: v for k, v in stored["value"].items() if isinstance(v, (int, float))})
    return values


def macro_context(db: Database, chat: dict, character: Character) -> macros.MacroContext:
    """The values `{{...}}` resolves to for this chat.

    Seeded on the chat id so `{{pick}}` lands on the same option every turn —
    a character described with one scar should not grow a different one each
    time the prompt is rebuilt.
    """
    persona = repo.active_persona(db, chat)
    history = repo.list_messages(db, chat["id"], include_dropped=True)
    last_at = history[-1].get("created_at") if history else None
    return macros.context_from(
        character,
        persona,
        seed=chat["id"],
        idle_seconds=macros.idle_since(last_at),
    )


def authors_note_for(chat: dict, character: Character) -> AuthorsNote:
    """The note in force: the chat's own if it has one, else the character's.

    A chat overrides wholesale rather than field by field. Half a note — this
    chat's text at the character's depth — is not something anyone means, and
    it would make "why is it not where I put it" impossible to answer.
    """
    override = (chat.get("settings") or {}).get("authors_note")
    if isinstance(override, dict) and str(override.get("text") or "").strip():
        try:
            return AuthorsNote.model_validate(override)
        except ValueError:
            pass
    return character.authors_note


def pending_event(db: Database, chat_id: str) -> str:
    """An unplanned thing the world is about to do, if one is waiting.

    Written by a background pass after the previous turn, so it costs the reply
    no latency at all — and the model gets a whole turn to weave it in rather
    than having it dropped on the turn it was invented.
    """
    stored = read_slice(db, chat_id, SLICE_EVENT)
    if not stored or not isinstance(stored["value"], dict):
        return ""
    value = stored["value"]
    if value.get("used"):
        return ""
    return str(value.get("event") or "").strip()


def search_block(db: Database, chat_id: str, turn: int) -> str:
    """What the web search found, but only for the turn that asked (roadmap 24).

    Results go stale immediately — an answer looked up three turns ago is worse
    than no answer, because the model has no way to tell it is old. Binding
    them to their own turn means they appear once and then stop, without
    anything having to remember to clear them.
    """
    stored = read_slice(db, chat_id, SLICE_SEARCH)
    if not stored or not isinstance(stored["value"], dict):
        return ""
    if stored["source_turn"] != turn:
        return ""
    results = stored["value"].get("results")
    return websearch.render(results if isinstance(results, list) else [])


def scene_line(db: Database, chat_id: str) -> str:
    scene = read_slice(db, chat_id, SLICE_SCENE)
    if not scene or not isinstance(scene["value"], dict):
        return ""
    value = worldline.shorten(scene["value"])
    parts = [value.get("place", ""), value.get("weather", ""), value.get("time", "")]
    parts = [p for p in parts if p]
    return " · ".join(parts)


def build_reply_context(
    db: Database,
    chat: dict,
    character: Character,
    settings: Settings,
    *,
    toggle_injections: list[str] | None = None,
    upto_turn: int | None = None,
    exclude_message_id: str | None = None,
    sees_images: bool = False,
) -> Assembled:
    """Assemble the pass-1 context for one turn."""
    assembled = Assembled()
    schema = load_schema(
        {k: v.model_dump() for k, v in character.state_schema.items()}
        if character.state_schema
        else None
    )

    # Card text is written with {{char}} and {{user}} in it and is resolved
    # here rather than when it was stored: the active persona can change
    # between turns and {{time}} is different every turn (§7.1).
    macro_ctx = macro_context(db, chat, character)
    def expand(text: str) -> str:
        return macros.substitute(text, macro_ctx)

    # Which sections are built, and in what order inside each band (§14). The
    # bands themselves never move, which is what keeps the cache rule (§7.1)
    # structural rather than a warning the user is asked to remember.
    layout = prompt_layout.normalise(settings.prompt_sections)

    def block_text(section: dict) -> str:
        """A section that carries its own words: a custom block, or one of the
        shipped writing blocks (§14). Both are headed with their own label, so
        what the panel calls a thing is what the prompt calls it."""
        body = expand(section.get("text") or "").strip()
        return f"## {section['label']}\n{body}" if body else ""

    # ---- stable prefix -------------------------------------------------
    # No word about markup here any more. This is the *fallback* for a card
    # with no system prompt of its own, and a card that has one replaces it
    # outright — which is how the one statement of the app's own rendering
    # convention disappeared for every imported card that carried a prompt.
    # It lives in the `craft:format` block now, in the volatile band, where
    # nothing can replace it and the model reads it last.
    default_instruction = (
        f"You are {character.name}. Stay in character and reply only as "
        f"{character.name}, in prose."
    )
    # Who the character is talking to. In the stable prefix because it changes
    # about as often as the character does — switching persona mid-chat costs
    # one cache rebuild, which is the right price for a rare deliberate act.
    persona = repo.active_persona(db, chat)
    constant_lore = [e for e in character.lorebook if e.constant and e.enabled]
    # Everyone else in the room (roadmap 8). Empty for a solo chat, so the
    # prompt is byte-identical to what it was before groups existed.
    cast = groups.cast_note(groups.members(db, chat["id"]), character.id)

    prefix_parts: dict[str, str] = {
        "instruction": expand(character.system_prompt).strip()
        if character.system_prompt
        else default_instruction,
        "character": f"## {character.name}\n{expand(character.persona).strip()}"
        if character.persona
        else "",
        "scenario": f"## Scenario\n{expand(character.scenario).strip()}"
        if character.scenario
        else "",
        "user_persona": f"## {persona['name']}\n{expand(persona['description']).strip()}"
        if persona and (persona.get("description") or "").strip()
        else "",
        "world": "## World\n" + expand(render_lore(constant_lore)) if constant_lore else "",
        "examples": f"## Example dialogue\n{expand(character.example_dialogue).strip()}"
        if character.example_dialogue
        else "",
        "cast": cast,
    }

    prefix = [
        _part(
            assembled,
            s,
            block_text(s) if prompt_layout.has_text(s) else prefix_parts.get(s["id"], ""),
        )
        for s in prompt_layout.order_for(layout, "prefix")
    ]

    assembled.system = "\n\n".join(p for p in prefix if p)
    _section(assembled, "prefix", assembled.system)

    # ---- dynamic middle -------------------------------------------------
    history = [
        m
        for m in repo.list_messages(db, chat["id"], include_dropped=False)
        if m["id"] != exclude_message_id
        and not m["hidden"]        # on screen, deliberately out of the prompt
        and (upto_turn is None or m["turn"] <= upto_turn)
    ]
    verbatim = [m for m in history if m["stage"] == "verbatim"]
    # The conversation is sized last, once every other section has been built
    # and its cost is known — "as much of the story as the budget allows,
    # without losing anything else". Nothing here needs the window to be
    # decided first: lore is scanned over the newest few messages by its own
    # setting, and everything else reads the newest turn.
    current_turn = verbatim[-1]["turn"] if verbatim else 0
    recent_texts = [m["text"] for m in verbatim[-max(1, settings.lorebook_scan_depth) :]]
    latest_user = next(
        (m["text"] for m in reversed(verbatim) if m["role"] == "user"), ""
    )

    middle_order = prompt_layout.order_for(layout, "middle")
    wanted = {s["id"] for s in middle_order}
    middle_parts: dict[str, str] = {}

    # Only scanned if the section is on: the scan itself costs time on a phone,
    # and a section that is switched off has nothing to show for it.
    if "lore" in wanted:
        triggered = [
            e
            for e in scan_lore(
                character.lorebook,
                recent_texts,
                scan_depth=settings.lorebook_scan_depth,
                total_budget=settings.lorebook_total_budget,
            )
            if not e.constant
        ]
        if triggered:
            text = render_lore(triggered)
            middle_parts["lore"] = f"## Relevant lore\n{text}"
            assembled.lore_hits = [e.keys[0] if e.keys else "" for e in triggered]
            _section(assembled, "lorebook", text)

    # Off for this character (§Character.memory_enabled) means nothing
    # stored ever comes back either, not just that nothing new is being
    # extracted — a person who turned this off wants a character with no
    # memory, not one still quietly recalling what it learned before.
    if "memories" in wanted and character.memory_enabled:
        memories = memory_store.retrieve(
            db, character.id, latest_user or "\n".join(recent_texts[-2:]),
            limit=settings.memory_max_injected,
        )
        if memories:
            text = memory_store.render(memories)
            middle_parts["memories"] = f"## Remembered\n{text}"
            assembled.memories = memories
            _section(assembled, "memory", text)

    if "summary" in wanted:
        summary = repo.get_summary(db, chat["id"])
        if summary["text"]:
            middle_parts["summary"] = f"## Story so far\n{summary['text']}"
            _section(assembled, "summary", summary["text"])

    # ---- the volatile band, built early and appended last ----------------
    #
    # It goes at the end of the prompt (the cache rule, §7.1) but its size has
    # to be known before the conversation is sized, or the conversation would
    # be filled with room the state block is about to need.
    values = current_values(db, chat["id"], schema, character.id)
    bands = render_bands(schema, values)
    volatile_parts: dict[str, str] = {
        "state": f"## {character.name}'s current state\n{bands}" if bands else "",
        "setting": f"## Setting\n{scene}" if (scene := scene_line(db, chat["id"])) else "",
        "search": search_block(db, chat["id"], current_turn),
        "toggles": "\n\n".join(toggle_injections or []),
        "event": f"## Something is happening\n{event}\n"
        "Work it into your reply as it happens. Do not resolve it in one line."
        if (event := pending_event(db, chat["id"]))
        else "",
        # The card's own last word. It belongs after the history — that is the
        # whole point of the field, and where a card puts the instruction it
        # wants obeyed over whatever the conversation has drifted into.
        "final": expand(character.post_history_instructions).strip()
        if character.post_history_instructions.strip()
        else "",
    }
    volatile_order = prompt_layout.order_for(layout, "volatile")
    volatile_text = {
        s["id"]: (
            block_text(s) if prompt_layout.has_text(s) else volatile_parts.get(s["id"], "")
        )
        for s in volatile_order
    }
    volatile_cost = sum(estimate_tokens(t) for t in volatile_text.values() if t)

    # ---- the conversation, filling whatever is left ----------------------
    middle_cost = sum(
        estimate_tokens(
            block_text(s) if prompt_layout.has_text(s) else middle_parts.get(s["id"], "")
        )
        for s in middle_order
        if s["id"] != "conversation"
    )
    spent = assembled.sections.get("prefix", 0) + middle_cost + volatile_cost
    # `reinserted` is true only when the opening had to be *put back* over a
    # gap. Whether it is there at all is a different question, and the answer to
    # that is what the trimmer must not undo.
    window, reinserted = _window(verbatim, settings, spent=spent)
    opening_present = bool(window) and window[0]["id"] == verbatim[0]["id"]
    # Where the conversation now starts, which is what the summary pass covers
    # up to. A re-inserted opening does not count: it is still in the prompt,
    # and summarising it would be describing a message the model can read.
    body = window[1:] if reinserted else window
    assembled.window_from = body[0]["turn"] if body else 0

    # Everything before the conversation goes into one system message; anything
    # a user has dragged *below* it follows the transcript. Being able to put a
    # block after the history and before the volatile band is most of the
    # reason to have custom blocks at all.
    before_conversation: list[str] = []
    after_conversation: list[str] = []
    bucket = before_conversation
    conversation_at: int | None = None
    for section in middle_order:
        if section["id"] == "conversation":
            bucket = after_conversation
            conversation_at = len(assembled.parts)
            continue
        text = (
            block_text(section)
            if prompt_layout.has_text(section)
            else middle_parts.get(section["id"], "")
        )
        if _part(assembled, section, text):
            bucket.append(text)
    if conversation_at is None:  # only reachable from a hand-broken layout
        conversation_at = len(assembled.parts)

    if before_conversation:
        assembled.messages.append(
            {"role": "system", "content": "\n\n".join(before_conversation)}
        )

    # Attachments ride with the message they were attached to (§19). Text is
    # quoted into the turn; images are named here and sent alongside only when
    # the backend can see them.
    attached = attachments.for_chat(db, chat["id"])
    turn_messages: list[Message] = []
    for message in window:
        role = message["role"] if message["role"] in ("user", "assistant") else "system"
        # The character's language, where there is one (roadmap 23). Falls
        # back to the original, so a translation that failed leaves the turn
        # readable in the wrong language rather than missing entirely.
        content = translation.for_prompt(message)
        items = attached.get(message["id"]) or []
        if items:
            extra = attachments.prompt_suffix(items, sees_images)
            content = f"{content}\n\n{extra}" if content else extra
            _section(assembled, "attachments", extra)
        turn_messages.append({"role": role, "content": content})

    if sees_images:
        # The newest message that actually carries images, not simply the last
        # one in the window — the last is usually the reply, and the picture
        # was attached to the turn before it. Only that one message's images:
        # re-sending every image in the window on every turn would be the
        # single most expensive thing this app does.
        newest = next(
            (m for m in reversed(window) if attached.get(m["id"])), None
        )
        if newest is not None:
            assembled.images = attachments.images_for(db, attached[newest["id"]])
    _section(assembled, "verbatim", "".join(m["text"] for m in window))

    # The author's note goes *inside* the recent history, `depth` messages from
    # the end. That placement is the whole feature: at the top it is buried
    # under everything since, and at the very end it reads as the newest thing
    # said rather than as a standing condition.
    note = authors_note_for(chat, character)
    current_turn = window[-1]["turn"] if window else 0
    note_text = ""
    if note.active_on(current_turn):
        note_text = expand(note.text).strip()
        if note_text:
            position = max(0, len(turn_messages) - max(0, note.depth))
            turn_messages.insert(position, {"role": "system", "content": note_text})
            _section(assembled, "authors_note", note_text)
    assembled.messages.extend(turn_messages)

    # The conversation itself, and the note buried inside it, take their place
    # in the itemisation where they take it in the prompt. The transcript's own
    # text is deliberately not copied in: it is already on screen, and storing
    # it once per turn would grow the database with the square of the chat.
    conversation_part = {
        "id": "conversation",
        "label": prompt_layout.BUILTIN_BY_ID["conversation"]["label"],
        "band": "middle",
        "custom": False,
        "tokens": assembled.sections.get("verbatim", 0),
        "text": "",
        "count": len(window),
    }
    inserted = [conversation_part]
    if note_text:
        inserted.append({
            "id": "authors_note", "label": "Author's note", "band": "middle",
            "custom": False, "tokens": estimate_tokens(note_text), "text": note_text,
            "depth": note.depth,
        })
    assembled.parts[conversation_at:conversation_at] = inserted

    if after_conversation:
        assembled.messages.append(
            {"role": "system", "content": "\n\n".join(after_conversation)}
        )

    # ---- volatile suffix (LAST — the cache rule) -------------------------
    # Built further up, where its size could still be counted; recorded and
    # appended here, where it belongs in the prompt.
    volatile = [_part(assembled, s, volatile_text[s["id"]]) for s in volatile_order]

    assembled.volatile = "\n\n".join(v for v in volatile if v)
    if assembled.volatile:
        assembled.messages.append({"role": "system", "content": assembled.volatile})
        _section(assembled, "volatile", assembled.volatile)

    _trim_to_budget(assembled, settings, protect=1 if opening_present else 0)
    return assembled


def _window(
    verbatim: list[dict], settings: Settings, *, spent: int
) -> tuple[list[dict], bool]:
    """The messages sent in full: the setting as a floor, then whatever else fits.

    `verbatim_window` used to be a hard cap, and a count-based one, so a chat
    holding 3.8k tokens against a 32k budget still sent only its last 24
    messages and lost the beginning of its own story to make room that was
    never needed. It is a floor now — that many always, however tight things
    are — and above it the only limit is the budget.

    `spent` is what the rest of the prompt actually costs, measured rather than
    guessed: every other section is built before this runs, so the conversation
    gets all the room they leave and no more. There is no cap on the number of
    messages. A message that fits is sent.

    The opening message is always included wherever it has got to. It is the
    scenario, nothing later restates it, and it is the one message whose
    absence turns a character into someone who does not know where they are.
    """
    if not verbatim:
        return [], False
    floor = max(0, settings.verbatim_window)
    room = settings.token_budget - spent - BUDGET_SLACK

    kept: list[dict] = []
    total = 0
    for message in reversed(verbatim):
        cost = estimate_tokens(message["text"])
        if len(kept) >= floor and (room <= 0 or total + cost > room):
            break
        kept.append(message)
        total += cost
    kept.reverse()

    opening = verbatim[0]
    if kept and kept[0]["id"] != opening["id"]:
        kept.insert(0, opening)
        return kept, True
    return kept, False


def _trim_to_budget(assembled: Assembled, settings: Settings, *, protect: int = 0) -> None:
    """Drop the oldest verbatim messages until the budget is met.

    Only the middle gives way: the prefix is what the cache is built on and the
    suffix is what the model needs to act correctly this turn.

    `protect` is how many of the oldest messages are not the trimmer's to take
    — one, for the pinned opening, which is the scenario and is worth more than
    any single exchange that would be kept in its place.
    """
    budget = settings.token_budget
    if budget <= 0:
        return
    while assembled.total_tokens > budget:
        turns = [
            i for i, m in enumerate(assembled.messages) if m["role"] in ("user", "assistant")
        ]
        # The oldest one the trimmer is allowed to take. When the protected
        # ones are all that is left there is nothing more it can do.
        index = turns[protect] if len(turns) > protect else None
        if index is None:
            break
        removed = assembled.messages.pop(index)
        cost = estimate_tokens(removed["content"])
        assembled.sections["verbatim"] = max(
            0, assembled.sections.get("verbatim", 0) - cost
        )
        assembled.trimmed += 1


def build_pass_context(
    db: Database,
    chat: dict,
    character: Character,
    settings: Settings,
    *,
    task: str,
    window: int = 6,
    extra: str = "",
) -> tuple[str, list[Message]]:
    """Context for a secondary pass: small by design.

    Secondary passes get the task, a little recent transcript and whatever
    slice they need — never the actor's full prompt. Keeping the actor's prompt
    small is the whole point of the director/actor split (§1), and the same
    logic applies to every other pass.
    """
    history = repo.list_messages(db, chat["id"], include_dropped=False)[-window:]
    transcript = "\n".join(
        f"{'User' if m['role'] == 'user' else character.name}: {to_plain(m['text'])}"
        for m in history
    )
    body = f"## Recent exchange\n{transcript}" if transcript else ""
    if extra:
        body = f"{extra}\n\n{body}" if body else extra
    return task, [{"role": "user", "content": body or "(no transcript yet)"}]


# ------------------------------------------------------------ eviction ladder


def memory_covered_turn(db: Database, chat_id: str) -> int:
    """The last turn the memory pass looked at, or MEMORY_NEVER if it never has."""
    stored = repo.get_meta(db, MEMORY_COVERED_KEY.format(chat_id=chat_id), "")
    if not stored:
        return MEMORY_NEVER
    try:
        return int(stored)
    except ValueError:
        return MEMORY_NEVER


def set_memory_covered_turn(db: Database, chat_id: str, turn: int) -> None:
    repo.set_meta(db, MEMORY_COVERED_KEY.format(chat_id=chat_id), str(turn))


def under_pressure(prompt_tokens: int, settings: Settings) -> bool:
    """Whether the prompt is close enough to its budget to start throwing away.

    `prompt_tokens` is what the last assembled turn actually cost. Nothing is
    known about the next one, and that is fine: eviction is permanent, so the
    right time to do it is once rather than early.
    """
    budget = settings.token_budget
    if budget <= 0:
        return False
    return prompt_tokens >= budget * EVICTION_PRESSURE


def apply_eviction(
    db: Database, chat_id: str, settings: Settings, *, prompt_tokens: int = 0
) -> dict[str, int]:
    """Advance messages down the ladder. Never drops uncovered messages.

    verbatim → summarized  once the prompt is near its budget, the summary pass
                           has covered the turn, and the message has fallen out
                           of the verbatim window
    summarized → dropped   only once the memory pass has covered it too, so a
                           durable fact was given its chance to be promoted

    Both steps are permanent — a summarized message is out of the prompt for
    good, and no later setting brings it back — so neither happens while the
    prompt still fits comfortably. Passing no `prompt_tokens` therefore evicts
    nothing, which is the safe way round for a caller that does not know.

    The opening message is never touched. It is the scenario: it says where
    everyone is, what the arrangement is and who these people are to each
    other, and nothing later in the chat restates any of it. It was also, being
    turn 0, always the first thing out of the door.
    """
    messages = repo.list_messages(db, chat_id)
    if not messages or not under_pressure(prompt_tokens, settings):
        return {"summarized": 0, "dropped": 0}

    opening = messages[0]["id"]
    window_ids = {
        m["id"] for m in [m for m in messages if m["stage"] == "verbatim"][-settings.verbatim_window :]
    }
    summary_covered = repo.get_summary(db, chat_id)["covered_turn"]
    memory_covered = memory_covered_turn(db, chat_id)

    to_summarize = [
        m["id"]
        for m in messages
        if m["stage"] == "verbatim"
        and m["id"] not in window_ids
        and m["id"] != opening
        and m["turn"] <= summary_covered
    ]
    to_drop = [
        m["id"]
        for m in messages
        if m["stage"] == "summarized"
        and m["id"] != opening
        and memory_covered != MEMORY_NEVER
        and m["turn"] <= min(summary_covered, memory_covered)
    ]

    if to_summarize or to_drop:

        def _apply(conn) -> None:
            if to_summarize:
                conn.executemany(
                    "UPDATE messages SET stage='summarized' WHERE id=?",
                    [(i,) for i in to_summarize],
                )
            if to_drop:
                conn.executemany(
                    "UPDATE messages SET stage='dropped' WHERE id=?", [(i,) for i in to_drop]
                )

        db.write_sync(_apply)

    return {"summarized": len(to_summarize), "dropped": len(to_drop)}


def speaker_label(name: str) -> str:
    """A character's name, as a name rather than as a card title.

    Cards are titled for a browse page — "Kutra - your assigned Coboldgirl" —
    and that whole string was being used as the speaker label in the transcript
    the summary and memory passes read. Every line of it then began with a
    sentence about the reader, which is not a name, and the summary that came
    back had swapped who said what.
    """
    # No colon: "Doctor: Elias" puts the name on the wrong side of it.
    for separator in (" - ", " — ", " – ", " | ", " (", ", the "):
        head = name.split(separator)[0].strip()
        if head and head != name:
            name = head
    return name.strip() or "them"


def pending_summary_text(
    db: Database, chat_id: str, character_name: str, *, before_turn: int | None = None
) -> tuple[str, int]:
    """Messages the summary pass has not folded in yet, and the turn they reach.

    `before_turn` is where the verbatim window now starts, and it is what stops
    the summary describing turns the model can still read for itself. Covering
    them was worse than useless: the summary sits *above* the conversation and
    reads as established fact, so a cheap model's misreading of an exchange
    contradicted the exchange itself a few hundred tokens further down. It also
    made the summary permanent far too early, since a covered message is one
    the eviction ladder may take.
    """
    summary = repo.get_summary(db, chat_id)
    messages = [
        m
        for m in repo.list_messages(db, chat_id, include_dropped=False)
        if m["turn"] > summary["covered_turn"]
        and (before_turn is None or m["turn"] < before_turn)
    ]
    if not messages:
        return "", summary["covered_turn"]
    label = speaker_label(character_name)
    transcript = "\n".join(
        f"{'User' if m['role'] == 'user' else label}: {to_plain(m['text'])}"
        for m in messages
    )
    return transcript, messages[-1]["turn"]
