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
from . import macros
from .models import Character, VariableSchema
from .providers.base import estimate_tokens
from .state import SLICE_SCENE, SLICE_VARS, initial_values, load_schema, read_slice
from .state import render_bands
from . import memory as memory_store
from . import repo

Message = dict[str, str]

MEMORY_COVERED_KEY = "memory_covered:{chat_id}"


@dataclass
class Assembled:
    system: str = ""
    messages: list[Message] = field(default_factory=list)
    volatile: str = ""
    sections: dict[str, int] = field(default_factory=dict)
    lore_hits: list[str] = field(default_factory=list)
    memories: list[dict] = field(default_factory=list)
    trimmed: int = 0  # messages cut by the token budget this turn

    @property
    def total_tokens(self) -> int:
        return sum(self.sections.values())


def _section(assembled: Assembled, name: str, text: str) -> None:
    if text:
        assembled.sections[name] = estimate_tokens(text)


def current_values(
    db: Database, chat_id: str, schema: dict[str, VariableSchema]
) -> dict[str, float]:
    stored = read_slice(db, chat_id, SLICE_VARS)
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


def scene_line(db: Database, chat_id: str) -> str:
    scene = read_slice(db, chat_id, SLICE_SCENE)
    if not scene or not isinstance(scene["value"], dict):
        return ""
    value = scene["value"]
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

    # ---- stable prefix -------------------------------------------------
    prefix: list[str] = []
    if character.system_prompt:
        prefix.append(expand(character.system_prompt).strip())
    else:
        prefix.append(
            f"You are {character.name}. Stay in character and reply only as "
            f"{character.name}, in prose. Use \"quotes\" for speech and *asterisks* for "
            "actions and narration."
        )
    if character.persona:
        prefix.append(f"## {character.name}\n{expand(character.persona).strip()}")
    if character.scenario:
        prefix.append(f"## Scenario\n{expand(character.scenario).strip()}")

    # Who the character is talking to. In the stable prefix because it changes
    # about as often as the character does — switching persona mid-chat costs
    # one cache rebuild, which is the right price for a rare deliberate act.
    persona = repo.active_persona(db, chat)
    if persona and (persona.get("description") or "").strip():
        prefix.append(
            f"## {persona['name']}\n{expand(persona['description']).strip()}"
        )

    constant_lore = [e for e in character.lorebook if e.constant and e.enabled]
    if constant_lore:
        prefix.append("## World\n" + expand(render_lore(constant_lore)))
    if character.example_dialogue:
        prefix.append(f"## Example dialogue\n{expand(character.example_dialogue).strip()}")

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
    window = verbatim[-settings.verbatim_window :] if settings.verbatim_window else verbatim
    recent_texts = [m["text"] for m in window]
    latest_user = next(
        (m["text"] for m in reversed(window) if m["role"] == "user"), ""
    )

    middle: list[str] = []

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
        middle.append(f"## Relevant lore\n{text}")
        assembled.lore_hits = [e.keys[0] if e.keys else "" for e in triggered]
        _section(assembled, "lorebook", text)

    memories = memory_store.retrieve(
        db, character.id, latest_user or "\n".join(recent_texts[-2:]),
        limit=settings.memory_max_injected,
    )
    if memories:
        text = memory_store.render(memories)
        middle.append(f"## Remembered\n{text}")
        assembled.memories = memories
        _section(assembled, "memory", text)

    summary = repo.get_summary(db, chat["id"])
    if summary["text"]:
        middle.append(f"## Story so far\n{summary['text']}")
        _section(assembled, "summary", summary["text"])

    if middle:
        assembled.messages.append({"role": "system", "content": "\n\n".join(middle)})

    for message in window:
        role = message["role"] if message["role"] in ("user", "assistant") else "system"
        assembled.messages.append({"role": role, "content": message["text"]})
    _section(assembled, "verbatim", "".join(m["text"] for m in window))

    # ---- volatile suffix (LAST — the cache rule) -------------------------
    volatile: list[str] = []
    values = current_values(db, chat["id"], schema)
    bands = render_bands(schema, values)
    if bands:
        volatile.append(f"## {character.name}'s current state\n{bands}")
    if scene := scene_line(db, chat["id"]):
        volatile.append(f"## Setting\n{scene}")
    for injection in toggle_injections or []:
        volatile.append(injection)
    # The card's own last word. It belongs after the history — that is the
    # whole point of the field, and where a card puts the instruction it wants
    # obeyed over whatever the conversation has drifted into — and it is stable
    # per character, so it sits at the end of the volatile block rather than
    # before the parts that change every turn.
    if character.post_history_instructions.strip():
        volatile.append(expand(character.post_history_instructions).strip())

    assembled.volatile = "\n\n".join(volatile)
    if assembled.volatile:
        assembled.messages.append({"role": "system", "content": assembled.volatile})
        _section(assembled, "volatile", assembled.volatile)

    _trim_to_budget(assembled, settings)
    return assembled


def _trim_to_budget(assembled: Assembled, settings: Settings) -> None:
    """Drop the oldest verbatim messages until the budget is met.

    Only the middle gives way: the prefix is what the cache is built on and the
    suffix is what the model needs to act correctly this turn.
    """
    budget = settings.token_budget
    if budget <= 0:
        return
    while assembled.total_tokens > budget:
        index = next(
            (
                i
                for i, m in enumerate(assembled.messages)
                if m["role"] in ("user", "assistant")
            ),
            None,
        )
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
    return int(repo.get_meta(db, MEMORY_COVERED_KEY.format(chat_id=chat_id), "0") or 0)


def set_memory_covered_turn(db: Database, chat_id: str, turn: int) -> None:
    repo.set_meta(db, MEMORY_COVERED_KEY.format(chat_id=chat_id), str(turn))


def apply_eviction(db: Database, chat_id: str, settings: Settings) -> dict[str, int]:
    """Advance messages down the ladder. Never drops uncovered messages.

    verbatim → summarized  once the summary pass has covered the turn and the
                           message has fallen out of the verbatim window
    summarized → dropped   only once the memory pass has covered it too, so a
                           durable fact was given its chance to be promoted
    """
    messages = repo.list_messages(db, chat_id)
    if not messages:
        return {"summarized": 0, "dropped": 0}

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
        and m["turn"] <= summary_covered
    ]
    to_drop = [
        m["id"]
        for m in messages
        if m["stage"] == "summarized" and m["turn"] <= min(summary_covered, memory_covered)
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


def pending_summary_text(db: Database, chat_id: str, character_name: str) -> tuple[str, int]:
    """Messages the summary pass has not folded in yet, and the turn they reach."""
    summary = repo.get_summary(db, chat_id)
    messages = [
        m
        for m in repo.list_messages(db, chat_id, include_dropped=False)
        if m["turn"] > summary["covered_turn"]
    ]
    if not messages:
        return "", summary["covered_turn"]
    transcript = "\n".join(
        f"{'User' if m['role'] == 'user' else character_name}: {to_plain(m['text'])}"
        for m in messages
    )
    return transcript, messages[-1]["turn"]
