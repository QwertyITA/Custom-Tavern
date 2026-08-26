"""The pass scheduler (§4, §5).

One turn runs like this:

  1. Rule-based nudges and decay adjust state before any model is called — the
     cheapest tier, zero tokens.
  2. The blocking `basic` pass streams the reply and emits its `<<<state>>>`
     suffix. Provisional state commits immediately so the reply is never gated
     on anything downstream.
  3. Non-blocking passes are evaluated against their triggers and the signals
     pass 1 just produced. Eligible ones run concurrently across tiers and each
     writes its own slice the moment it lands — order between different slices
     is irrelevant.

The only arbitration is per-slice and by source turn (§5.5): when the auditor
and pass 1 both write `state.vars`, an older-turn write loses. Nothing else is
ordered, and there is no global commit DAG.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

from .. import assembly
from .. import attachments
from .. import avatar_video
from .. import character_reactions
from .. import groups
from .. import macros, memory as memory_store, regex_rules, repo, state as state_mod
from .. import reply_length
from .. import translation
from .. import websearch
from ..config import Settings
from ..db import Database
from ..events import BUS
from ..markup import to_plain
from ..models import Character, PassDef, Sampling, VariableSchema
from ..postprocess import ThinkStreamFilter, clean_reply, split_thinking
from .. import reply_polish
from .. import worldline
from ..providers import (
    GenRequest,
    GenResult,
    ProviderError,
    ReasoningDelta,
    provider_for_tier,
)
from ..providers.base import estimate_tokens
from ..state import SLICE_SEARCH, SLICE_SIGNALS, SLICE_VARS, slice_for
from . import registry
from .contract import (
    MARKER,
    REPLY_SUFFIX_MARKER_HELP,
    SuffixStreamFilter,
    normalise_payload,
    parse_json_loose,
    signal_rank,
    split_state_suffix,
)

# Bounds how many background passes for one chat can actually be doing work
# (calling a backend) at the same time — the task itself is still created and
# tracked the instant a turn launches it, so `depends_on` ordering is
# unaffected; this only gates the part of it that consumes a connection (§
# PassScheduler._run_background). Sized to comfortably fit one turn's whole
# canonical set (scene, expression, background_swap, random_event, summary,
# memory, state_auditor — seven, most turns launch fewer) running together
# unthrottled, while still bounding what several turns sent faster than a
# slow background backend can keep up with would otherwise pile onto one
# chat at once (§KNOWN-ISSUES.md, "No cap on concurrent background passes
# per chat").
MAX_CONCURRENT_BACKGROUND_PASSES_PER_CHAT = 8


class ReasoningWatch:
    """Reasoning as it arrives, in both shapes a backend can send it (§5.6).

    Ollama and the OpenAI-shaped servers that reason parse the block themselves
    and hand it back on a channel of its own, which reaches here as
    `ReasoningDelta`. Everything else leaves it inline in a `<think>` block,
    which `ThinkStreamFilter` pulls back out. Either way the reply text comes
    out one side and the reasoning the other, and the client is told the moment
    any of it lands — a model that reasons emits no visible token until it has
    finished, so this is the only thing separating "thinking" from "the backend
    never answered".
    """

    def __init__(self) -> None:
        self._inline = ThinkStreamFilter()
        self._parts: list[str] = []
        self.chars = 0
        self._told = False

    def _keep(self, thought: str) -> str:
        if thought:
            self._parts.append(thought)
            self.chars += len(thought)
        return thought

    def feed(self, delta: str) -> tuple[str, str]:
        """One delta in; the reply text and the reasoning in it out."""
        if isinstance(delta, ReasoningDelta):
            return "", self._keep(str(delta))
        shown, thought = self._inline.feed(delta)
        return shown, self._keep(thought)

    def finish(self) -> str:
        """Flush the held-back tail and return the last of the reply text."""
        shown, thought = self._inline.finish()
        self._keep(thought)
        return shown

    def take_retraction(self) -> bool:
        """True once, when what has already been streamed turns out to be
        reasoning and the client has to be told to throw its copy away."""
        if self._inline.retracted and not self._told:
            self._told = True
            return True
        return False

    @property
    def text(self) -> str:
        return "".join(self._parts).strip()


@dataclass
class TurnContext:
    chat: dict
    character: Character
    settings: Settings
    turn: int
    message_id: str = ""
    variant_id: str = ""
    schema: dict[str, VariableSchema] = field(default_factory=dict)
    pre_values: dict[str, float] = field(default_factory=dict)
    signals: dict[str, str] = field(default_factory=dict)
    toggle_states: dict[str, bool] = field(default_factory=dict)
    reply_text: str = ""
    # What this turn's prompt actually cost. Eviction is permanent, so it only
    # happens under real pressure (§7.2) — and this is the only measurement of
    # that pressure anyone has.
    prompt_tokens: int = 0
    # Where the verbatim window starts. The summary pass covers what is before
    # it and nothing after, so it never describes a turn the model can read.
    window_from: int = 0

    @property
    def chat_id(self) -> str:
        return self.chat["id"]


# Moved to assembly.py (§ fit_token_budget) so the character roster's
# "is this card too big" check can share the exact same arithmetic without
# importing the whole scheduler. Re-exported under their old names here —
# nothing outside this module needs to change which module it imports them
# from.
CONTEXT_SAFETY = assembly.CONTEXT_SAFETY
MIN_CONTEXT = assembly.MIN_CONTEXT


class PassScheduler:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        # Background tasks per chat, so the next turn can wait briefly on them.
        self._pending: dict[str, set[asyncio.Task]] = {}
        # One turn-producing operation at a time per chat (§ _run_locked) —
        # two tabs or two devices sending into the same chat used to run two
        # independent replies to two different prompts, neither aware the
        # other had happened (§KNOWN-ISSUES.md, "Two turns can run at once in
        # one chat"). Never pruned: a chat that has ever generated keeps a
        # single Lock object here forever, which costs bytes, not the
        # unbounded-growth shape of the `_pending` issue right below this —
        # there is exactly one per chat that has ever run a turn, never one
        # per turn.
        self._chat_locks: dict[str, asyncio.Lock] = {}
        # How many of a chat's background passes may be doing work at once
        # (§ MAX_CONCURRENT_BACKGROUND_PASSES_PER_CHAT). Same "never pruned,
        # bytes-scale" reasoning as `_chat_locks` just above.
        self._background_slots: dict[str, asyncio.Semaphore] = {}

    # ------------------------------------------------------------- plumbing

    def _emit(self, chat_id: str, event: dict[str, Any]) -> None:
        BUS.publish(chat_id, event)

    def _track(self, chat_id: str, task: asyncio.Task) -> None:
        tasks = self._pending.setdefault(chat_id, set())
        tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            tasks.discard(t)
            # Drop the entry once it is empty, rather than leaving a dict key
            # with nothing in it for every chat that has ever run a
            # background pass (§KNOWN-ISSUES.md, "PassScheduler._pending
            # never removes an emptied chat entry"). The identity check
            # guards against a rare reordering: `_track` racing back in for
            # this chat between the last task finishing and this callback
            # running would already have replaced `self._pending[chat_id]`
            # with a fresh set, which this must leave alone rather than
            # delete out from under the new tasks it holds.
            if not tasks and self._pending.get(chat_id) is tasks:
                del self._pending[chat_id]

        task.add_done_callback(_done)

    async def _run_locked(
        self, chat_id: str | None, inner: AsyncIterator[dict]
    ) -> AsyncIterator[dict]:
        """Runs `inner` — an already-created turn generator, not yet iterated
        — while holding `chat_id`'s lock, or immediately yields a clear error
        instead if another one already holds it (§ _chat_locks). Never blocks
        waiting for the lock: a second request landing mid-turn is turned
        away outright, not queued to run once the first finishes, since by
        the time it would run the chat has moved on and it would be
        generating against a prompt that is no longer the latest thing said.

        `chat_id` is `None` when a caller could not resolve one (a swipe or
        continue on a message that turns out not to exist) — `inner` still
        runs, unlocked, to produce whatever ordinary "not found" error it
        already would have.
        """
        if chat_id is None:
            async for event in inner:
                yield event
            return
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            yield {"type": "error", "error": "a reply is already being generated in this chat"}
            return
        async with lock:
            async for event in inner:
                yield event

    def _background_slot(self, chat_id: str) -> asyncio.Semaphore:
        return self._background_slots.setdefault(
            chat_id, asyncio.Semaphore(MAX_CONCURRENT_BACKGROUND_PASSES_PER_CHAT)
        )

    async def await_pending(self, chat_id: str, timeout: float | None = None) -> int:
        """Wait briefly for in-flight passes before starting the next turn (§4.6).

        Deliberately a short wait, not a join: a slow background pass must never
        gate the next reply. Whatever has not landed lands later and corrects
        itself through the same stale-write rule.
        """
        tasks = {t for t in self._pending.get(chat_id, set()) if not t.done()}
        if not tasks:
            return 0
        budget = self.settings.blocking_await_ms / 1000 if timeout is None else timeout
        done, _ = await asyncio.wait(tasks, timeout=budget)
        return len(done)

    async def _translate(self, text: str, target: str, prompt: str) -> str:
        """One translation call. Returns "" on any failure.

        Empty on failure rather than raising: a translation that did not happen
        should leave the original readable, not lose the turn. The caller falls
        back to `text` everywhere.
        """
        if not text.strip() or not target.strip():
            return ""
        provider = provider_for_tier("foreground", self.settings)
        request = GenRequest(
            system=prompt.format(target=target),
            messages=[{"role": "user", "content": text}],
            sampling=Sampling(temp=0.2, top_p=0.9, max_tokens=800),
            expects_json=True,
            pass_id="translate",
        )
        try:
            result = await provider.generate(request)
        except (ProviderError, asyncio.TimeoutError, OSError):
            return ""
        payload = parse_json_loose(result.text) or {}
        return str(payload.get("text") or "").strip()

    def _rewrite_reply(self, reply: str) -> str:
        return regex_rules.apply(self.settings.regex_rules, reply, "output", "assistant")

    async def _consume_event(self, ctx: TurnContext) -> None:
        """Mark a pending random event as spent, once a reply has used it."""
        stored = state_mod.read_slice(self.db, ctx.chat_id, state_mod.SLICE_EVENT)
        if not stored or not isinstance(stored["value"], dict):
            return
        value = stored["value"]
        if value.get("used") or not str(value.get("event") or "").strip():
            return
        await state_mod.write_slice(
            self.db,
            ctx.chat_id,
            state_mod.SLICE_EVENT,
            {**value, "used": True},
            source_turn=ctx.turn,
            source_pass="consumed",
        )

    # ----------------------------------------------------------- pass runs

    def _record_run(
        self, ctx: TurnContext, definition: PassDef, status: str, **fields: Any
    ) -> str:
        run_id = fields.pop("run_id", None) or uuid.uuid4().hex
        columns = {
            "id": run_id,
            "chat_id": ctx.chat_id,
            "turn": ctx.turn,
            "pass_id": definition.id,
            "kind": definition.kind,
            "tier": definition.model_tier,
            "status": status,
            "variant_id": ctx.variant_id or None,
            **fields,
        }
        keys = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{k}=excluded.{k}" for k in columns if k != "id")
        values = tuple(columns.values())
        self.db.write_sync(
            lambda conn: conn.execute(
                f"INSERT INTO pass_runs({keys}) VALUES({placeholders}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                values,
            )
        )
        self._emit(
            ctx.chat_id,
            {
                "type": "pass_status",
                "turn": ctx.turn,
                "run": {
                    "id": run_id,
                    "pass_id": definition.id,
                    "kind": definition.kind,
                    "label": definition.label or definition.id,
                    "tier": definition.model_tier,
                    "status": status,
                    "animation": definition.resolved_animation,
                    "model": columns.get("model", ""),
                    "tokens_in": columns.get("tokens_in", 0),
                    "tokens_out": columns.get("tokens_out", 0),
                    "attempts": columns.get("attempts", 0),
                    "error": columns.get("error"),
                },
            },
        )
        return run_id

    # ------------------------------------------------------------- triggers

    def eligible(
        self, definitions: list[PassDef], ctx: TurnContext, disabled: set[str]
    ) -> list[PassDef]:
        out: list[PassDef] = []
        off = set(getattr(self.settings, "tiers_off", []) or [])
        spacing = getattr(self.settings, "pass_every", {}) or {}
        for definition in definitions:
            if definition.id == "basic" or not definition.enabled:
                continue
            if definition.id in disabled:
                continue
            # A whole group switched off in the panel (§3). Cheaper than
            # disabling its passes one at a time, and it is the switch someone
            # reaches for when a metered backend is doing the answering.
            if definition.model_tier in off:
                continue
            if not self._spaced_out(definition, ctx, spacing):
                continue
            if self.trigger_fires(definition, ctx):
                out.append(definition)
        return out

    @staticmethod
    def _spaced_out(definition: PassDef, ctx: TurnContext, spacing: dict) -> bool:
        """A floor on how often a pass may run, over and above its trigger.

        The trigger answers "is there anything to do"; this answers "is it
        worth paying for yet". Kept separate because they disagree usefully: a
        scene that changed on every turn of a chase still only needs writing
        down every third one.
        """
        every = spacing.get(definition.id, 1)
        try:
            every = int(every)
        except (TypeError, ValueError):
            return True
        return every <= 1 or ctx.turn % every == 0

    def trigger_fires(self, definition: PassDef, ctx: TurnContext) -> bool:
        trigger = definition.trigger
        if trigger.type == "manual":
            return False
        if trigger.type == "every_turn":
            return True
        if trigger.type == "every_n":
            return trigger.n > 0 and ctx.turn % trigger.n == 0
        if trigger.type == "over_budget":
            # Only once the prompt has actually run out of room. A summary is
            # the only account of everything it covers — a covered message
            # leaves the prompt for good — so it is worth paying for when there
            # is no longer space to keep the messages themselves, and worth
            # nothing before that.
            return assembly.under_pressure(ctx.prompt_tokens, self.settings)
        if trigger.type == "chance":
            # Free: a dice roll, no model call. The whole point of gating a pass
            # this way is that it costs nothing on the turns it does not fire.
            return random.random() < max(0.0, min(1.0, trigger.probability))
        if trigger.type == "on_signal":
            level = ctx.signals.get(trigger.signal, "none")
            threshold = trigger.threshold
            if isinstance(threshold, (int, float)):
                threshold = "minor" if threshold <= 0.5 else "major"
            return _compare(signal_rank(level), trigger.op, signal_rank(str(threshold)))
        if trigger.type == "timer":
            row = self.db.query_one(
                "SELECT MAX(finished_at) AS last FROM pass_runs "
                "WHERE chat_id=? AND pass_id=? AND status='done'",
                (ctx.chat_id, definition.id),
            )
            last = (row["last"] if row else None) or 0
            return (time.time() - last) >= trigger.seconds
        return False

    # ---------------------------------------------------------------- turn

    async def run_turn(
        self,
        chat_id: str,
        user_text: str,
        attachment_ids: list[str] | None = None,
        speaker_id: str = "",
    ) -> AsyncIterator[dict]:
        """Serialized per chat (§ _run_locked) — a second request against the
        same chat while one is already generating gets turned away with a
        clear error instead of running its own reply concurrently, neither
        turn aware the other ever happened (§KNOWN-ISSUES.md, 'Two turns can
        run at once in one chat')."""
        async for event in self._run_locked(chat_id, self._run_turn(
            chat_id, user_text, attachment_ids, speaker_id
        )):
            yield event

    async def _run_turn(
        self,
        chat_id: str,
        user_text: str,
        attachment_ids: list[str] | None = None,
        speaker_id: str = "",
    ) -> AsyncIterator[dict]:
        """Run one full turn, yielding events as they happen."""
        # Any pass still in flight from the previous turn gets a short grace
        # period; we then proceed on provisional state regardless (§4.6).
        awaited = await self.await_pending(chat_id)
        if awaited:
            yield {"type": "awaited_passes", "count": awaited}

        chat = repo.get_chat(self.db, chat_id)
        if chat is None:
            yield {"type": "error", "error": "unknown chat"}
            return
        # Who replies (roadmap 8). A solo chat is a group of one, so this runs
        # the same way for both and there is no second path to keep correct.
        groups.ensure_member(self.db, chat_id, chat["character_id"])
        policy = (chat.get("settings") or {}).get("policy") or groups.DEFAULT_POLICY
        chosen = groups.choose_speaker(
            self.db,
            chat_id,
            policy=policy,
            user_text=user_text,
            last_speaker=groups.last_speaker(self.db, chat_id),
            forced=speaker_id,
        )
        if chosen is None:
            yield {
                "type": "error",
                "error": "everyone here is muted" if groups.members(self.db, chat_id)
                else "nobody is in this chat",
            }
            return
        character = repo.get_character(self.db, chosen["character_id"])
        if character is None:
            yield {"type": "error", "error": "chat has no character"}
            return

        # Resolved before it is stored, like the greeting: what the user typed
        # is what gets recorded, and {{char}} in their own message should read
        # as the character's name in the transcript too.
        user_text = macros.substitute(
            user_text, assembly.macro_context(self.db, chat, character)
        )
        # Input-scope rules run before the message is stored (§16) — they are an
        # edit to the record by design, which is what separates them from the
        # display scope.
        user_text = regex_rules.apply(
            self.settings.regex_rules, user_text, "input", "user"
        )
        user_message = repo.add_message(self.db, chat_id, "user", user_text)
        turn = user_message["turn"]
        # Files were uploaded before this message existed; bind them now, so
        # the assembler below sees them on the turn they belong to (§19).
        if attachment_ids:
            user_message["attachments"] = attachments.claim(
                self.db, attachment_ids, user_message["id"]
            )

        # Into the character's language, before the prompt is built (roadmap
        # 23). Blocking by necessity: the model has to be handed one consistent
        # language, and there is no way to do that after the fact. Stored
        # beside the original rather than over it — you keep seeing your own
        # words, the model sees theirs.
        if translation.enabled(self.settings):
            crossed = await self._translate(
                user_text, self.settings.character_language, translation.IN_PROMPT
            )
            if crossed:
                translation.set_translation(self.db, user_message["variant_id"], crossed)
                user_message["translation"] = crossed

        async for event in self._answer(chat, character, user_message, user_text):
            yield event

    async def retry_turn(self, chat_id: str) -> AsyncIterator[dict]:
        """Serialized per chat, same as run_turn (§ _run_locked)."""
        async for event in self._run_locked(chat_id, self._retry_turn(chat_id)):
            yield event

    async def _retry_turn(self, chat_id: str) -> AsyncIterator[dict]:
        """Answer a user message that never got a reply.

        A turn whose reply failed — the backend was down, the phone lost the
        network — leaves the transcript ending on a question nobody answered.
        Sending it again would put the same words in twice, and regenerating
        re-rolls the previous reply rather than writing the missing one, so
        without this there is no way back except to live with the gap.

        Everything after the user message is the same as an ordinary turn, and
        runs through the same code — the split is exactly at the point where a
        normal turn has just stored what you typed.
        """
        awaited = await self.await_pending(chat_id)
        if awaited:
            yield {"type": "awaited_passes", "count": awaited}

        chat = repo.get_chat(self.db, chat_id)
        if chat is None:
            yield {"type": "error", "error": "unknown chat"}
            return

        history = repo.list_messages(self.db, chat_id)
        if not history or history[-1]["role"] != "user":
            # Nothing is dangling. Refusing is better than inventing a second
            # reply to a message that already has one.
            yield {"type": "error", "error": "nothing waiting for a reply"}
            return
        user_message = history[-1]

        groups.ensure_member(self.db, chat_id, chat["character_id"])
        chosen = groups.choose_speaker(
            self.db,
            chat_id,
            policy=(chat.get("settings") or {}).get("policy") or groups.DEFAULT_POLICY,
            user_text=user_message["text"],
            last_speaker=groups.last_speaker(self.db, chat_id),
        )
        character = repo.get_character(self.db, chosen["character_id"]) if chosen else None
        if character is None:
            yield {"type": "error", "error": "chat has no character"}
            return

        async for event in self._answer(
            chat, character, user_message, user_message["text"], announce=False
        ):
            yield event

    async def _answer(
        self,
        chat: dict,
        character: Character,
        user_message: dict,
        user_text: str,
        *,
        announce: bool = True,
    ) -> AsyncIterator[dict]:
        """Everything a turn does once the user's message exists.

        Shared by `run_turn` and `retry_turn` so a retry cannot drift from a
        first attempt — the state decay, the nudges, the search and the
        background passes all have to happen exactly once per answered message,
        whichever route got there.
        """
        chat_id = chat["id"]
        turn = user_message["turn"]
        ctx = TurnContext(
            chat=chat,
            character=character,
            settings=self.settings,
            turn=turn,
            schema=state_mod.load_schema(
                {k: v.model_dump() for k, v in character.state_schema.items()}
                if character.state_schema
                else None
            ),
        )
        ctx.toggle_states = registry.toggle_states(self.db, character.id, chat_id)

        yield {
            # A retry's message is already on screen, so it says so rather than
            # asking the frontend to append a second copy of it.
            "type": "turn_start" if announce else "turn_resume",
            "turn": turn,
            "message": user_message,
            # Who is about to answer, so the placeholder can carry their name
            # and portrait instead of the chat's nominal character.
            "speaker": {"id": character.id, "name": character.name},
        }

        # --- cheapest tier first: deterministic decay + regex nudges (§6) ---
        values = assembly.current_values(self.db, chat_id, ctx.schema, ctx.character.id)
        values = state_mod.decay_step(ctx.schema, values)
        nudges = state_mod.load_nudges(
            (chat.get("settings") or {}).get("nudges")
            or getattr(character, "nudges", None)
        )
        values, fired = state_mod.apply_nudges(nudges, ctx.schema, values, user_text, "user")
        ctx.pre_values = values
        if fired:
            yield {"type": "nudges", "fired": fired}

        # --- the one thing outside the model: a web search (roadmap 24) ---
        # Blocking, because results that arrive after the reply are results the
        # reply did not use. It is one HTTP request with a short timeout, and
        # it only happens at all when both the switch and a URL are present.
        async for event in self._run_search(ctx, user_text):
            yield event

        async for event in self._run_reply(ctx):
            yield event

        if not ctx.message_id:
            return  # the reply failed; nothing downstream is meaningful

        # --- non-blocking passes: parallel, write-on-arrival (§5.5) ---
        launched = self._launch_background(ctx)
        if launched:
            yield {"type": "background_queued", "passes": launched}
        # A character imported while its backend was unreachable, or created
        # blank, gets another try at its reaction lines here — queued after
        # the reply has already gone out, same as the background passes
        # above, and only when something is actually still missing.
        if character_reactions.missing_keys(ctx.character):
            task = asyncio.create_task(
                character_reactions.spawn(self.db, self.settings, ctx.character),
                name=f"reactions:{ctx.character.id}:{turn}",
            )
            self._track(chat_id, task)
        # A talking-video render for this reply, same fire-and-forget shape
        # and same reasoning as the reaction lines just above — queued after
        # the reply has already gone out, since a render can take longer
        # than the line it illustrates (§ app/avatar_video.py). No-ops
        # instantly when the character has no avatar video switched on.
        if ctx.character.avatar_video.enabled:
            task = asyncio.create_task(
                avatar_video.render_for_reply(
                    self.db, self.settings, ctx.character,
                    chat_id, ctx.message_id, ctx.reply_text,
                ),
                name=f"avatar_video:{ctx.character.id}:{turn}",
            )
            self._track(chat_id, task)
        yield {"type": "turn_end", "turn": turn}

    async def _run_search(self, ctx: TurnContext, user_text: str) -> AsyncIterator[dict]:
        """Look the message up, if asked to and if there is somewhere to ask.

        Nothing is yielded on the turns this does not run, so a chat with the
        switch off never sees a trace of the feature — including in the event
        stream, where a `search_start` with no `search_done` after it would be
        a spinner that never stops.
        """
        if not ctx.toggle_states.get("web_search"):
            return
        if not websearch.configured(self.settings) or not user_text.strip():
            return
        yield {"type": "search_start", "query": user_text.strip()[:200]}
        results = await websearch.search(self.settings, user_text)
        # Written even when empty: the slice is read by source turn, so an
        # empty write is what stops the previous turn's results from being
        # offered again as though they answered this message.
        await state_mod.write_slice(
            self.db,
            ctx.chat_id,
            SLICE_SEARCH,
            {"query": user_text.strip()[:200], "results": results},
            source_turn=ctx.turn,
            source_pass="web_search",
        )
        yield {
            "type": "search_done",
            "count": len(results),
            "sources": [r["url"] for r in results if r["url"]],
        }

    async def _run_reply(self, ctx: TurnContext) -> AsyncIterator[dict]:
        definition = registry.get_pass(self.db, "basic")
        if definition is None:
            definition = registry.CANONICAL_PASSES[0]

        injections = registry.active_injections(self.db, ctx.toggle_states, "basic")
        # The provider is resolved before assembly, not after: whether it can
        # see images decides how an attached picture is written into the prompt
        # (§19), so the assembler has to be told first.
        provider = provider_for_tier(definition.model_tier, self.settings)
        fitted = await self._fitted(provider, definition)
        assembled = assembly.build_reply_context(
            self.db, ctx.chat, ctx.character, fitted,
            toggle_injections=injections,
            sees_images=provider.sees_images,
        )
        # Decided now, before a single token streams: whether post_process is
        # going to run this turn is what decides whether the raw draft is
        # shown live or held back (§ the "delta" yields below), and that
        # cannot be changed once the first one has already reached the
        # client.
        polish_definition = self._polish_pass()
        hold_for_polish = self._polish_enabled(polish_definition)

        ctx.prompt_tokens = assembled.total_tokens
        ctx.window_from = assembled.window_from
        contract = _suffix_instructions(ctx)
        system = assembled.system + "\n\n" + contract
        request = GenRequest(
            system=system,
            messages=assembled.messages,
            sampling=_with_character_stops(definition.sampling, ctx.character),
            pass_id=definition.id,
            images=assembled.images,
        )

        run_id = self._record_run(
            ctx,
            definition,
            "running",
            model=provider.model,
            started_at=time.time(),
            tokens_in=assembled.total_tokens,
            attempts=1,
        )
        # What was actually sent, kept so the message can be asked about later
        # (§15). Recorded before the stream rather than after: a reply that
        # fails or is stopped is exactly the one someone wants to look at.
        repo.save_prompt_record(
            self.db, run_id, ctx.chat_id, _itemised(assembled, contract),
            budget=fitted.token_budget,
        )
        yield {
            "type": "assembly",
            "sections": assembled.sections,
            "total_tokens": assembled.total_tokens,
            "trimmed": assembled.trimmed,
            "lore_hits": assembled.lore_hits,
            "memories": [m["text"] for m in assembled.memories],
        }

        sink = GenResult()
        suffix = SuffixStreamFilter()
        watch = ReasoningWatch()
        collected: list[str] = []
        try:
            async for delta in provider.stream(request, sink):
                shown, thought = watch.feed(delta)
                # The reply had not started after all. Everything sent so far
                # was reasoning behind an opening tag the chat template wrote
                # itself, so the client is told to drop it and the reply starts
                # again from here.
                if watch.take_retraction():
                    collected.clear()
                    suffix = SuffixStreamFilter()
                    yield {"type": "reply_reset"}
                # Only that it is happening, and how far in — the reasoning
                # itself is not for the message stream (§5.6). It is enough to
                # tell a thinking model from a silent backend, and to let the
                # cue deepen as the thought runs on.
                if thought:
                    yield {"type": "reasoning", "chars": watch.chars}
                visible = suffix.feed(shown) if shown else ""
                if visible:
                    collected.append(visible)
                    # Held back rather than shown live: post_process gets the
                    # only look anyone has at this draft before it is either
                    # rewritten or shown untouched (§ below, once the reply is
                    # fully in hand).
                    if not hold_for_polish:
                        yield {"type": "delta", "text": visible}
        except asyncio.CancelledError:
            # The reader hung up — the user pressed stop. Whatever arrived is
            # what the character said, so it is kept: throwing away a reply
            # someone stopped because they had already read enough of it is
            # the opposite of what stopping means. No state is written, since
            # the suffix that carries it never arrived.
            partial = clean_reply(
                split_thinking("".join(collected))[0],
                strip_leakage=self.settings.strip_user_turn_leakage,
                user_names=("You", "{{user}}"),
            ).strip()
            if partial:
                kept = repo.add_message(
                    self.db, ctx.chat_id, "assistant", partial, turn=ctx.turn,
                    provider=sink.provider or provider.name,
                    model=sink.model or provider.model,
                    speaker_id=ctx.character.id,
                    # Kept for the same reason the text is (§5.6). A reply
                    # someone stopped is one of the likeliest to be asked
                    # about, and the reasoning behind it arrived in full long
                    # before the stop — the sink never lands on this path, so
                    # without this it would be the one reply that thought and
                    # cannot say so.
                    thinking=watch.text,
                )
                # So the run points at the message it produced. A stopped reply
                # is one of the likeliest things to be asked about afterwards,
                # and without this its prompt record would be unreachable.
                ctx.message_id = kept["id"]
                ctx.variant_id = kept["variant_id"]
            self._record_run(
                ctx, definition, "stopped", run_id=run_id,
                tokens_in=sink.tokens_in or assembled.total_tokens,
                tokens_out=sink.tokens_out, finished_at=time.time(), attempts=1,
            )
            raise
        except (ProviderError, asyncio.TimeoutError, OSError) as exc:
            self._record_run(
                ctx, definition, "failed", run_id=run_id, error=str(exc), finished_at=time.time()
            )
            yield {"type": "error", "error": f"reply failed: {exc}", "pass_id": "basic"}
            return

        # Whatever the think filter was still holding back when the stream
        # ended, through the suffix filter as usual — the state suffix can be
        # sitting in it.
        held = watch.finish()
        if held:
            visible = suffix.feed(held)
            if visible:
                collected.append(visible)
                if not hold_for_polish:
                    yield {"type": "delta", "text": visible}
        tail, payload = suffix.finish()
        if tail:
            collected.append(tail)
            if not hold_for_polish:
                yield {"type": "delta", "text": tail}

        raw_reply = "".join(collected)
        body, thinking = split_thinking(raw_reply)
        # Three places it can be, and it has to end up stored wherever it came
        # from (§5.6): pulled out of the stream as it arrived, left inline for
        # split_thinking to find in text that never streamed, or handed back on
        # the result by a backend that parses the block itself. Without all
        # three the commonest failure a reasoning model has is reported as
        # "returned nothing at all", which names the wrong fix.
        thinking = thinking or watch.text or sink.thinking
        # A model that ignored the suffix contract may still have emitted it
        # inside a think block or after it; check the full text once more.
        if payload is None:
            body, payload = split_state_suffix(body)
        reply = clean_reply(
            body,
            strip_leakage=self.settings.strip_user_turn_leakage,
            user_names=("You", "{{user}}"),
        )
        # Output-scope rules, before it is stored (§16). After clean_reply, so a
        # rule is written against the text a person would have read rather than
        # against artefacts the postprocessor was about to remove anyway.
        reply = self._rewrite_reply(reply)
        if thinking:
            yield {"type": "thinking", "text": thinking}

        # An empty reply used to be stored as "…" and shown as if the character
        # had said it. That is the worst possible answer: nothing is wrong with
        # the *conversation*, something is wrong with the *setup*, and a silent
        # ellipsis hides which. Worse, it looks like the model's own doing, so
        # the setting that would fix it never gets touched.
        #
        # It is a failed turn instead, with the reason. The transcript is left
        # ending on the user's message, which the retry affordance already
        # knows how to answer.
        if not reply.strip():
            # One more go before giving up. The commonest cause by a distance
            # is a reasoning model that thought its way past its own budget and
            # never started the answer, so the retry asks for no reasoning
            # rather than repeating the request that just failed — and it does
            # not stream, because there is nothing to watch arrive and a second
            # empty bubble is worse than a pause.
            second = await self._one_more_go(provider, request, definition)
            if second.strip():
                reply = self._rewrite_reply(
                    clean_reply(
                        second,
                        strip_leakage=self.settings.strip_user_turn_leakage,
                        user_names=("You", "{{user}}"),
                    )
                )
                if payload is None:
                    reply, payload = split_state_suffix(reply)
                if not hold_for_polish:
                    yield {"type": "delta", "text": reply}

        if not reply.strip():
            reason = _why_empty(
                raw_reply, thinking, body,
                used=sink.tokens_out,
                budget=definition.sampling.max_tokens or 0,
            )
            self._record_run(
                ctx, definition, "error", run_id=run_id,
                finished_at=time.time(), error=reason,
            )
            yield {"type": "error", "error": reason, "pass_id": "basic"}
            return

        # The copy-edit, when the tier wants one (§ app/reply_polish.py) —
        # before the length backstop below, so the backstop judges what
        # post_process actually produced rather than second-guessing a draft
        # it is about to replace.
        draft_text = ""
        if hold_for_polish:
            try:
                reply, draft_text = await self._polish_reply(
                    ctx, polish_definition, reply, assembled
                )
            except asyncio.CancelledError:
                # Stopped while post_process was still working. Nothing was
                # ever shown live (§ hold_for_polish above), so the draft it
                # was given is what "the character said" means here — kept
                # exactly as generated, the same as stopping during the raw
                # stream itself keeps whatever had arrived by then, rather
                # than losing the turn to an exception with nothing stored.
                kept = repo.add_message(
                    self.db, ctx.chat_id, "assistant", reply, turn=ctx.turn,
                    provider=sink.provider or provider.name,
                    model=sink.model or provider.model,
                    speaker_id=ctx.character.id,
                    thinking=thinking,
                )
                ctx.message_id = kept["id"]
                ctx.variant_id = kept["variant_id"]
                self._record_run(
                    ctx, definition, "stopped", run_id=run_id,
                    tokens_in=sink.tokens_in or assembled.total_tokens,
                    tokens_out=sink.tokens_out, finished_at=time.time(), attempts=1,
                )
                raise

        # A hard backstop for craft:length, when the toggle wants one (§
        # reply_length.py) — after the empty-reply retry above, so it never
        # cuts against a reply that is about to be thrown away, and before
        # everything below, so state, memory and translation all judge the
        # reply the person is actually going to see rather than the tail end
        # nobody will.
        reply, full_text = reply_length.cut(reply, self.settings)

        # Nothing was shown while post_process ran, so this is the reply's
        # first appearance on screen — one delta rather than none, so the
        # client's own pacer (§ static/app.js makePacer) still animates it in
        # rather than the bubble simply materialising with text already in it.
        if hold_for_polish:
            yield {"type": "delta", "text": reply}

        message = repo.add_message(
            self.db,
            ctx.chat_id,
            "assistant",
            reply,
            turn=ctx.turn,
            provider=sink.provider or provider.name,
            model=sink.model or provider.model,
            speaker_id=ctx.character.id,
            # Kept with the reply it produced (§5.6). It used to be counted,
            # streamed to the HUD and dropped, which left no way to answer
            # "did it actually think?" a minute later — the question a
            # reasoning model raises on every single turn.
            thinking=thinking,
            full_text=full_text,
            draft_text=draft_text,
        )
        ctx.message_id = message["id"]
        ctx.variant_id = message["variant_id"]
        ctx.reply_text = reply

        self._record_run(
            ctx,
            definition,
            "done",
            run_id=run_id,
            model=sink.model or provider.model,
            tokens_in=sink.tokens_in or assembled.total_tokens,
            tokens_out=sink.tokens_out,
            finished_at=time.time(),
            attempts=1,
        )

        # Provisional state commits now so the next turn is never blocked (§1).
        normalised = normalise_payload(payload)
        ctx.signals = normalised["signals"]
        values = state_mod.apply_deltas(ctx.schema, ctx.pre_values, normalised["deltas"])
        await state_mod.write_slice(
            self.db,
            ctx.chat_id,
            slice_for(SLICE_VARS, ctx.character.id),
            values,
            source_turn=ctx.turn,
            source_pass="basic",
            variant_id=ctx.variant_id,
            provisional=True,
        )
        await state_mod.write_slice(
            self.db,
            ctx.chat_id,
            slice_for(SLICE_SIGNALS, ctx.character.id),
            ctx.signals,
            source_turn=ctx.turn,
            source_pass="basic",
            variant_id=ctx.variant_id,
            provisional=True,
        )

        # And back into your language (roadmap 23). After the stream rather
        # than during it: a translation cannot be produced a token at a time,
        # so the original arrives live and settles into the translation a
        # moment later — the same way a display rule does.
        if translation.enabled(self.settings):
            back = await self._translate(
                reply, self.settings.reading_language, translation.OUT_PROMPT
            )
            if back:
                translation.set_translation(self.db, message["variant_id"], back)
                message["translation"] = back

        # The event has now been written into a reply, so it stops being
        # pending. Marked rather than deleted: a swipe on this same turn should
        # see the same intrusion, and the write only loses to a *newer* turn.
        await self._consume_event(ctx)

        yield {
            "type": "reply",
            "message": {**message, "text": reply},
            "signals": ctx.signals,
            "state": _state_view(ctx.schema, values, provisional=True),
        }

    # -------------------------------------------------------- background

    def _launch_background(self, ctx: TurnContext) -> list[str]:
        definitions = registry.all_passes(self.db)
        disabled = registry.passes_disabled_by_toggle(self.db, ctx.toggle_states)
        eligible = self.eligible(definitions, ctx, disabled)
        if not eligible:
            return []

        by_id = {d.id: d for d in eligible}
        completion: dict[str, asyncio.Event] = {d.id: asyncio.Event() for d in eligible}

        # One run row per pass per turn: created pending here, then moved
        # through running → done/failed under the same id so the HUD shows a
        # single row changing state rather than a pile of them.
        run_ids = {d.id: self._record_run(ctx, d, "pending") for d in eligible}

        for definition in eligible:
            # depends_on is a DATA dependency: wait only for dependencies that
            # are actually running this turn. A dependency that isn't running
            # simply means its slice already holds what we need.
            waits = [completion[dep] for dep in definition.depends_on if dep in by_id]
            task = asyncio.create_task(
                self._run_background(
                    ctx, definition, waits, completion[definition.id], run_ids[definition.id]
                ),
                name=f"pass:{definition.id}:{ctx.chat_id}:{ctx.turn}",
            )
            self._track(ctx.chat_id, task)
        return [d.id for d in eligible]

    async def _run_background(
        self,
        ctx: TurnContext,
        definition: PassDef,
        waits: list[asyncio.Event],
        done_event: asyncio.Event,
        run_id: str,
    ) -> None:
        try:
            if waits:
                await asyncio.wait(
                    [asyncio.create_task(event.wait()) for event in waits],
                    timeout=self.settings.pass_timeout,
                )
            # Waiting on a dependency above costs nothing; only the actual
            # call to a backend does, so only that part waits on a slot (§
            # MAX_CONCURRENT_BACKGROUND_PASSES_PER_CHAT). A pass that is
            # merely queued behind a slot still shows as "running" on the
            # HUD rather than a fourth status meaning "waiting to run" —
            # accurate enough: it has started, in the sense that matters to
            # someone watching, and the wait itself is normally instant at
            # this chat's actual pass volume.
            async with self._background_slot(ctx.chat_id):
                await self._execute(ctx, definition, run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken pass must not take the turn with it
            self._record_run(
                ctx, definition, "failed", run_id=run_id, error=repr(exc), finished_at=time.time()
            )
        finally:
            done_event.set()

    async def _execute(
        self, ctx: TurnContext, definition: PassDef, run_id: str | None = None
    ) -> None:
        provider = provider_for_tier(definition.model_tier, self.settings)
        task_prompt, messages, post = self._build_pass_input(ctx, definition)
        if post is None:  # nothing for this pass to do this turn
            self._record_run(
                ctx, definition, "skipped", run_id=run_id, finished_at=time.time()
            )
            return

        request = GenRequest(
            system=task_prompt,
            messages=messages,
            sampling=definition.sampling,
            expects_json=definition.expects_json,
            pass_id=definition.id,
        )
        retries = definition.retries if definition.retries is not None else self.settings.background_retries
        run_id = self._record_run(
            ctx,
            definition,
            "running",
            run_id=run_id,
            model=provider.model,
            started_at=time.time(),
        )

        last_error = ""
        for attempt in range(1, retries + 2):
            try:
                result = await asyncio.wait_for(
                    provider.generate(request), timeout=self.settings.pass_timeout
                )
            except (ProviderError, asyncio.TimeoutError, OSError) as exc:
                last_error = str(exc)
                if attempt <= retries:
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                break

            text, _thinking = split_thinking(result.text)
            payload = parse_json_loose(text) if definition.expects_json else {"text": text}
            if payload is None:
                last_error = "unparseable output"
                if attempt <= retries:
                    continue
                break

            accepted = await post(payload)
            self._record_run(
                ctx,
                definition,
                "done" if accepted else "stale",
                run_id=run_id,
                model=result.model or provider.model,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                attempts=attempt,
                finished_at=time.time(),
            )
            return

        self._record_run(
            ctx,
            definition,
            "failed",
            run_id=run_id,
            error=last_error or "unknown error",
            attempts=retries + 1,
            finished_at=time.time(),
        )

    # --------------------------------------------------- per-pass wiring

    def _build_pass_input(self, ctx: TurnContext, definition: PassDef):
        """Return (system prompt, messages, async result handler).

        A handler of None means the pass has nothing to do this turn.
        """
        settings = self.settings
        character = ctx.character
        extra = ""

        if definition.id == "expression":
            emotions = sorted(character.pfp_set.keys()) or [
                "neutral", "happy", "sad", "angry", "surprised", "thoughtful"
            ]
            extra = f"Allowed emotions: {', '.join(emotions)}"
        elif definition.id == "background_swap":
            options = [b.get("id") or b.get("img", "") for b in character.backgrounds]
            if not options:
                return "", [], None
            scene = assembly.scene_line(self.db, ctx.chat_id)
            extra = f"Allowed backgrounds: {', '.join(options)}\nCurrent scene: {scene or 'unknown'}"
        elif definition.id == "state_auditor":
            bands = state_mod.render_bands(ctx.schema, ctx.pre_values)
            extra = (
                f"Character personality:\n{character.persona.strip() or character.name}\n\n"
                f"State before this turn:\n{bands}\n\n"
                f"Provisional deltas from the reply pass: "
                f"{json.dumps(_deltas_between(ctx.pre_values, assembly.current_values(self.db, ctx.chat_id, ctx.schema, ctx.character.id)))}"
            )
        elif definition.id == "summary":
            pending, covered = assembly.pending_summary_text(
                self.db, ctx.chat_id, character.name, before_turn=ctx.window_from
            )
            if not pending:
                return "", [], None
            existing = repo.get_summary(self.db, ctx.chat_id)["text"]
            task = (
                f"{definition.prompt}\n\nBudget: about {settings.summary_budget} tokens."
            )
            messages = [
                {
                    "role": "user",
                    "content": (
                        f"## Existing summary\n{existing or '(none yet)'}\n\n"
                        f"## New messages\n{pending}"
                    ),
                }
            ]
            return task, messages, self._handler_summary(ctx, covered)
        elif definition.id == "memory":
            if not character.memory_enabled:
                # Off means off — no extraction — but coverage still has to
                # advance, or eviction (§assembly.apply_eviction) waits
                # forever on a memory pass that will never run again for
                # this character, and old messages stop dropping at all.
                assembly.set_memory_covered_turn(self.db, ctx.chat_id, ctx.turn)
                return "", [], None
            history = repo.list_messages(self.db, ctx.chat_id, include_dropped=False)
            covered = assembly.memory_covered_turn(self.db, ctx.chat_id)
            fresh = [m for m in history if m["turn"] > covered]
            if not fresh:
                return "", [], None
            label = assembly.speaker_label(character.name)
            transcript = "\n".join(
                f"{'User' if m['role'] == 'user' else label}: {to_plain(m['text'])}"
                for m in fresh
            )
            return (
                definition.prompt,
                [{"role": "user", "content": f"## New messages\n{transcript}"}],
                self._handler_memory(ctx, fresh[-1]["turn"]),
            )

        task, messages = assembly.build_pass_context(
            self.db, ctx.chat, character, settings, task=definition.prompt, extra=extra
        )
        return task, messages, self._handler_generic(ctx, definition)

    def _handler_generic(self, ctx: TurnContext, definition: PassDef):
        async def handle(payload: dict) -> bool:
            output = definition.output
            slice_name = definition.writes_slice or output.target

            if output.type == "state_modifier" and slice_name == SLICE_VARS:
                normalised = normalise_payload(payload)
                corrected = state_mod.apply_deltas(
                    ctx.schema, ctx.pre_values, normalised["deltas"]
                )
                write = await state_mod.write_slice(
                    self.db,
                    ctx.chat_id,
                    slice_for(SLICE_VARS, ctx.character.id),
                    corrected,
                    source_turn=ctx.turn,
                    source_pass=definition.id,
                    variant_id=ctx.variant_id,
                    provisional=False,
                )
                if write.accepted:
                    self._emit(
                        ctx.chat_id,
                        {
                            "type": "state",
                            "turn": ctx.turn,
                            "state": _state_view(ctx.schema, corrected, provisional=False),
                            "reason": payload.get("reason", ""),
                            "source": definition.id,
                        },
                    )
                return write.accepted

            if not slice_name:
                return True

            value = _clean_panel_payload(payload)
            if slice_name == state_mod.SLICE_SCENE:
                # One word each (§10). The prompt asks; this makes sure — the
                # instruction that gives "Rainy" nine times gives "Rainy, with
                # the wind picking up" on the tenth.
                value = worldline.shorten(value)
            # A pass that came back with nothing usable has nothing to say, and
            # writing its empty result would destroy whatever the slice already
            # held. That is not hypothetical: the random-event pass returning
            # no JSON wiped the `used` flag off a pending event, which made the
            # same knock at the door arrive on every turn afterwards.
            if not value:
                return False
            write = await state_mod.write_slice(
                self.db,
                ctx.chat_id,
                slice_name,
                value,
                source_turn=ctx.turn,
                source_pass=definition.id,
                variant_id=ctx.variant_id,
            )
            if write.accepted and output.type == "gui_panel":
                self._emit(
                    ctx.chat_id,
                    {
                        "type": "panel",
                        "panel": output.target or slice_name,
                        "value": value,
                        "turn": ctx.turn,
                        "source": definition.id,
                    },
                )
            return write.accepted

        return handle

    def _handler_summary(self, ctx: TurnContext, covered_turn: int):
        async def handle(payload: dict) -> bool:
            text = str(payload.get("summary") or payload.get("text") or "").strip()
            if not text:
                return False
            repo.set_summary(self.db, ctx.chat_id, text, covered_turn)
            assembly.apply_eviction(
                self.db, ctx.chat_id, self.settings, prompt_tokens=ctx.prompt_tokens
            )
            self._emit(
                ctx.chat_id,
                {"type": "summary", "text": text, "covered_turn": covered_turn},
            )
            return True

        return handle

    def _handler_memory(self, ctx: TurnContext, covered_turn: int):
        async def handle(payload: dict) -> bool:
            items = payload.get("memories") or payload.get("items") or []
            if isinstance(items, dict):
                items = [items]
            inserted = memory_store.store(
                self.db,
                ctx.character.id,
                [i for i in items if isinstance(i, (dict, str))],
                chat_id=ctx.chat_id,
                turn=ctx.turn,
            )
            # Coverage advances even when nothing was extracted: the pass looked
            # at those turns, so the eviction ladder may now move past them.
            assembly.set_memory_covered_turn(self.db, ctx.chat_id, covered_turn)
            assembly.apply_eviction(
                self.db, ctx.chat_id, self.settings, prompt_tokens=ctx.prompt_tokens
            )
            if inserted:
                self._emit(
                    ctx.chat_id,
                    {
                        "type": "memories",
                        "count": len(inserted),
                        "memories": memory_store.list_all(self.db, ctx.character.id)[:5],
                    },
                )
            return True

        return handle

    # -------------------------------------------------------------- swipes

    async def run_continue(self, message_id: str) -> AsyncIterator[dict]:
        """Serialized per chat, same as run_turn (§ _run_locked) — resolved
        from the message being continued, since the caller only has that."""
        message = repo.get_message(self.db, message_id)
        chat_id = message["chat_id"] if message and message["role"] == "assistant" else None
        async for event in self._run_locked(chat_id, self._run_continue(message_id)):
            yield event

    async def _run_continue(self, message_id: str) -> AsyncIterator[dict]:
        """Extend a reply that stopped early, in place.

        Not a swipe: a swipe asks for a different reply and branches, while
        this asks for *more of the same one*. So the text is appended to the
        variant that is already showing rather than becoming a new one — there
        is nothing to choose between, and a 1/2 counter on a message that was
        merely finished would be a lie about what happened.

        The model is given the reply so far as the start of its own turn and
        asked to carry straight on, which is what stops it restarting the
        sentence or greeting the user again.
        """
        message = repo.get_message(self.db, message_id)
        if message is None or message["role"] != "assistant":
            yield {"type": "error", "error": "can only continue a reply"}
            return

        chat = repo.get_chat(self.db, message["chat_id"])
        character = repo.get_character(self.db, chat["character_id"]) if chat else None
        if chat is None or character is None:
            yield {"type": "error", "error": "unknown chat"}
            return

        existing = message["text"] or ""
        ctx = TurnContext(
            chat=chat,
            character=character,
            settings=self.settings,
            turn=message["turn"],
            message_id=message_id,
            variant_id=message["variant_id"],
            schema=state_mod.load_schema(
                {k: v.model_dump() for k, v in character.state_schema.items()}
                if character.state_schema
                else None
            ),
        )
        ctx.toggle_states = registry.toggle_states(self.db, character.id, chat["id"])
        ctx.pre_values = assembly.current_values(self.db, chat["id"], ctx.schema, character.id)

        definition = registry.get_pass(self.db, "basic") or registry.CANONICAL_PASSES[0]
        injections = registry.active_injections(self.db, ctx.toggle_states, "basic")
        assembled = assembly.build_reply_context(
            self.db, chat, character, self.settings,
            toggle_injections=injections,
            exclude_message_id=message_id,
        )
        ctx.prompt_tokens = assembled.total_tokens
        ctx.window_from = assembled.window_from
        messages = list(assembled.messages)
        messages.append({"role": "assistant", "content": existing})
        request = GenRequest(
            system=(
                f"{assembled.system}\n\n## This turn\n"
                f"{character.name}'s reply below was cut off. Continue it from "
                "exactly where it stops — same sentence if it stops mid-sentence, "
                "same scene, same voice. Do not repeat any of it and do not start "
                "again."
            ),
            messages=messages,
            sampling=_with_character_stops(definition.sampling, character),
            pass_id="continue",
        )

        provider = provider_for_tier(definition.model_tier, self.settings)
        run_id = self._record_run(
            ctx, definition, "running", model=provider.model, started_at=time.time()
        )

        sink = GenResult()
        suffix = SuffixStreamFilter()
        watch = ReasoningWatch()
        collected: list[str] = []
        try:
            async for delta in provider.stream(request, sink):
                shown, thought = watch.feed(delta)
                if watch.take_retraction():
                    collected.clear()
                    suffix = SuffixStreamFilter()
                    yield {"type": "reply_reset"}
                # As in the reply pass: the count, never the text (§5.6).
                if thought:
                    yield {"type": "reasoning", "chars": watch.chars}
                visible = suffix.feed(shown) if shown else ""
                if visible:
                    collected.append(visible)
                    yield {"type": "delta", "text": visible}
        except asyncio.CancelledError:
            self._append_continuation(ctx, message, existing, collected)
            self._record_run(ctx, definition, "stopped", run_id=run_id, finished_at=time.time())
            raise
        except (ProviderError, asyncio.TimeoutError, OSError) as exc:
            self._record_run(
                ctx, definition, "failed", run_id=run_id, error=str(exc), finished_at=time.time()
            )
            yield {"type": "error", "error": f"continue failed: {exc}"}
            return

        held = watch.finish()
        if held:
            visible = suffix.feed(held)
            if visible:
                collected.append(visible)
                yield {"type": "delta", "text": visible}
        tail, _payload = suffix.finish()
        if tail:
            collected.append(tail)
            yield {"type": "delta", "text": tail}

        full = self._append_continuation(ctx, message, existing, collected)
        self._record_run(
            ctx, definition, "done", run_id=run_id,
            model=sink.model or provider.model,
            tokens_in=sink.tokens_in or assembled.total_tokens,
            tokens_out=sink.tokens_out, finished_at=time.time(), attempts=1,
        )
        yield {"type": "continued", "message_id": message_id, "text": full}

    def _append_continuation(
        self, ctx: TurnContext, message: dict, existing: str, collected: list[str]
    ) -> str:
        """Join the new text onto the old and store it on the same variant."""
        addition = clean_reply(
            split_thinking("".join(collected))[0],
            strip_leakage=self.settings.strip_user_turn_leakage,
            user_names=("You", "{{user}}"),
        ).strip()
        if not addition:
            return existing
        # A space only where the seam needs one: a reply cut mid-word must not
        # gain a gap, and one cut after a full stop must not lose it.
        joiner = "" if (not existing or existing[-1].isspace()) else " "
        full = f"{existing}{joiner}{addition}"
        # edited=False: the character carried on, which is not the same as
        # someone rewriting them, and the pencil marker means the latter.
        repo.update_variant_text(self.db, message["variant_id"], full, edited=False)
        return full

    async def run_swipe(self, message_id: str) -> AsyncIterator[dict]:
        """Serialized per chat, same as run_turn (§ _run_locked) — resolved
        from the message being regenerated, since the caller only has that."""
        message = repo.get_message(self.db, message_id)
        chat_id = message["chat_id"] if message and message["role"] == "assistant" else None
        async for event in self._run_locked(chat_id, self._run_swipe(message_id)):
            yield event

    async def _run_swipe(self, message_id: str) -> AsyncIterator[dict]:
        """Generate an alternative reply as a branch (§9).

        The current variant's state writes are rolled back before generating,
        so state never accumulates from variants the user did not land on.
        """
        message = repo.get_message(self.db, message_id)
        if message is None or message["role"] != "assistant":
            yield {"type": "error", "error": "can only swipe an assistant message"}
            return

        chat = repo.get_chat(self.db, message["chat_id"])
        character = repo.get_character(self.db, chat["character_id"]) if chat else None
        if chat is None or character is None:
            yield {"type": "error", "error": "unknown chat"}
            return

        rolled = await state_mod.rollback_turn(
            self.db, chat["id"], message["turn"], message["variant_id"]
        )
        yield {"type": "rollback", "writes": rolled, "turn": message["turn"]}

        ctx = TurnContext(
            chat=chat,
            character=character,
            settings=self.settings,
            turn=message["turn"],
            message_id=message_id,
            schema=state_mod.load_schema(
                {k: v.model_dump() for k, v in character.state_schema.items()}
                if character.state_schema
                else None
            ),
        )
        ctx.toggle_states = registry.toggle_states(self.db, character.id, chat["id"])
        ctx.pre_values = assembly.current_values(self.db, chat["id"], ctx.schema, character.id)

        definition = registry.get_pass(self.db, "basic") or registry.CANONICAL_PASSES[0]
        injections = registry.active_injections(self.db, ctx.toggle_states, "basic")
        provider = provider_for_tier(definition.model_tier, self.settings)
        fitted = await self._fitted(provider, definition)
        assembled = assembly.build_reply_context(
            self.db,
            chat,
            character,
            fitted,
            toggle_injections=injections,
            exclude_message_id=message_id,
            sees_images=provider.sees_images,
        )
        # Same decision as the first attempt, made the same way and this
        # early for the same reason (§ _run_reply above).
        polish_definition = self._polish_pass()
        hold_for_polish = self._polish_enabled(polish_definition)

        ctx.prompt_tokens = assembled.total_tokens
        ctx.window_from = assembled.window_from
        contract = _suffix_instructions(ctx)
        request = GenRequest(
            system=assembled.system + "\n\n" + contract,
            messages=assembled.messages,
            sampling=_with_character_stops(definition.sampling, character),
            pass_id=definition.id,
            images=assembled.images,
        )
        run_id = self._record_run(
            ctx, definition, "running", model=provider.model, started_at=time.time()
        )
        # A re-roll is its own prompt, assembled after whatever the last attempt
        # changed, so it gets its own record rather than sharing the first
        # attempt's (§9).
        repo.save_prompt_record(
            self.db, run_id, chat["id"], assembled.parts, budget=fitted.token_budget
        )

        sink = GenResult()
        suffix = SuffixStreamFilter()
        watch = ReasoningWatch()
        collected: list[str] = []
        try:
            async for delta in provider.stream(request, sink):
                shown, thought = watch.feed(delta)
                if watch.take_retraction():
                    collected.clear()
                    suffix = SuffixStreamFilter()
                    yield {"type": "reply_reset"}
                # As in the reply pass: the count, never the text (§5.6).
                if thought:
                    yield {"type": "reasoning", "chars": watch.chars}
                visible = suffix.feed(shown) if shown else ""
                if visible:
                    collected.append(visible)
                    if not hold_for_polish:
                        yield {"type": "delta", "text": visible}
        except (ProviderError, asyncio.TimeoutError, OSError) as exc:
            self._record_run(
                ctx, definition, "failed", run_id=run_id, error=str(exc), finished_at=time.time()
            )
            yield {"type": "error", "error": f"swipe failed: {exc}"}
            return

        held = watch.finish()
        if held:
            visible = suffix.feed(held)
            if visible:
                collected.append(visible)
                if not hold_for_polish:
                    yield {"type": "delta", "text": visible}
        tail, payload = suffix.finish()
        if tail:
            collected.append(tail)
            if not hold_for_polish:
                yield {"type": "delta", "text": tail}

        body, thinking = split_thinking("".join(collected))
        thinking = thinking or watch.text or sink.thinking
        if payload is None:
            body, payload = split_state_suffix(body)
        reply = self._rewrite_reply(
            clean_reply(body, strip_leakage=self.settings.strip_user_turn_leakage)
        )

        # Exactly what a first attempt gets (§5.6). This path used to store the
        # ellipsis instead, which is how a reasoning model that ran out of room
        # produced nine variants of "…" — every one of them a turn that failed
        # silently and looked like the character having nothing to say.
        if not reply.strip():
            second = await self._one_more_go(provider, request, definition)
            if second.strip():
                reply = self._rewrite_reply(
                    clean_reply(second, strip_leakage=self.settings.strip_user_turn_leakage)
                )
                if payload is None:
                    reply, payload = split_state_suffix(reply)
                if not hold_for_polish:
                    yield {"type": "delta", "text": reply}

        if not reply.strip():
            reason = _why_empty(
                "".join(collected), thinking, body,
                used=sink.tokens_out,
                budget=definition.sampling.max_tokens or 0,
            )
            self._record_run(
                ctx, definition, "error", run_id=run_id,
                finished_at=time.time(), error=reason,
            )
            # No variant: a swipe that produced nothing must leave the one you
            # were reading in place, not replace it with a blank.
            yield {"type": "error", "error": reason, "pass_id": "basic"}
            return

        # Same copy-edit as the first attempt, before the same backstop below
        # (§ _run_reply above).
        draft_text = ""
        if hold_for_polish:
            try:
                reply, draft_text = await self._polish_reply(
                    ctx, polish_definition, reply, assembled
                )
            except asyncio.CancelledError:
                # Same reasoning as the reply pass's own version of this (§
                # _run_reply above) — a variant, not a message, since a swipe
                # that produced nothing must leave the one being read in
                # place rather than replace it with a blank.
                variant = repo.add_variant(
                    self.db, message_id, reply,
                    provider=provider.name, model=sink.model or provider.model,
                    thinking=thinking,
                )
                ctx.variant_id = variant["id"]
                self._record_run(
                    ctx, definition, "stopped", run_id=run_id,
                    tokens_in=sink.tokens_in or assembled.total_tokens,
                    tokens_out=sink.tokens_out, finished_at=time.time(),
                )
                raise

        # Same hard backstop as the initial reply (§ reply_length.py) — a
        # swipe is judged and stored exactly the same way a first attempt is.
        reply, full_text = reply_length.cut(reply, self.settings)

        # Same single reveal as the first attempt (§ _run_reply above) —
        # nothing was shown live while post_process ran.
        if hold_for_polish:
            yield {"type": "delta", "text": reply}

        variant = repo.add_variant(
            self.db,
            message_id,
            reply,
            provider=provider.name,
            model=sink.model or provider.model,
            thinking=thinking,
            full_text=full_text,
            draft_text=draft_text,
        )
        ctx.variant_id = variant["id"]
        self._record_run(
            ctx,
            definition,
            "done",
            run_id=run_id,
            model=sink.model or provider.model,
            tokens_in=sink.tokens_in or assembled.total_tokens,
            tokens_out=sink.tokens_out,
            finished_at=time.time(),
        )

        normalised = normalise_payload(payload)
        ctx.signals = normalised["signals"]
        values = state_mod.apply_deltas(ctx.schema, ctx.pre_values, normalised["deltas"])
        await state_mod.write_slice(
            self.db,
            chat["id"],
            slice_for(SLICE_VARS, ctx.character.id),
            values,
            source_turn=ctx.turn,
            source_pass="basic",
            variant_id=variant["id"],
            provisional=True,
        )

        if thinking:
            yield {"type": "thinking", "text": thinking}
        yield {
            "type": "variant",
            "message_id": message_id,
            "variant": variant,
            "state": _state_view(ctx.schema, values, provisional=True),
        }

        # Only the variant the user lands on commits background passes (§9).
        launched = self._launch_background(ctx)
        if launched:
            yield {"type": "background_queued", "passes": launched}
        yield {"type": "turn_end", "turn": ctx.turn}

    async def run_impersonate(self, chat_id: str) -> AsyncIterator[dict]:
        """Draft the user's next message in their own voice.

        It borrows the reply pass's context — the character, the history, the
        state — because writing the user's line convincingly needs to know
        exactly what the character has just said and what has happened. What
        it does not borrow is the state contract: this never becomes a message,
        never writes a slice and never advances the turn, so there is nothing
        for the `<<<state>>>` suffix to carry and asking for one would only
        give the model something else to get wrong. The text lands in the
        composer, where the user can rewrite it before sending.
        """
        chat = repo.get_chat(self.db, chat_id)
        character = repo.get_character(self.db, chat["character_id"]) if chat else None
        if chat is None or character is None:
            yield {"type": "error", "error": "unknown chat"}
            return

        definition = registry.get_pass(self.db, "basic") or registry.CANONICAL_PASSES[0]
        toggle_states = registry.toggle_states(self.db, character.id, chat_id)
        injections = registry.active_injections(self.db, toggle_states, "basic")
        assembled = assembly.build_reply_context(
            self.db, chat, character, self.settings, toggle_injections=injections
        )

        system = (
            f"{assembled.system}\n\n"
            "## This turn\n"
            f"Write the USER's next message, not {character.name}'s. You are "
            "drafting the user's side of the conversation for them: stay in "
            "their voice as it appears in the transcript, keep it to the length "
            "they usually write, and move the scene forward.\n"
            f"Write only the message. No name prefix, no quotation marks around "
            f"the whole thing, and nothing from {character.name}."
        )
        request = GenRequest(
            system=system,
            messages=assembled.messages,
            # Not the character's stop strings: this is the *user's* line, and
            # a sequence that ends the character's replies has no business
            # cutting off the user's.
            sampling=definition.sampling,
            pass_id="impersonate",
        )

        provider = provider_for_tier(definition.model_tier, self.settings)
        collected: list[str] = []
        sink = GenResult()
        try:
            async for delta in provider.stream(request, sink):
                # Impersonation writes *your* line, so a backend that reasons
                # out loud on its own channel has nothing to contribute to it.
                if isinstance(delta, ReasoningDelta):
                    continue
                collected.append(delta)
                yield {"type": "delta", "text": delta}
        except (ProviderError, asyncio.TimeoutError, OSError) as exc:
            yield {"type": "error", "error": f"impersonate failed: {exc}"}
            return

        body, _thinking = split_thinking("".join(collected))
        # The model was told not to prefix a name; models do it anyway.
        text = clean_reply(body, strip_leakage=False, user_names=()).strip()
        for prefix in ("You:", "User:", "{{user}}:"):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix):].lstrip()
        yield {"type": "impersonated", "text": text}

    async def _fitted(self, provider, definition):
        """Settings with the context budget cut to what this backend can hold.

        Prompt and reply share one window. Asking for 32k of context and 5000
        tokens of reply from a model serving 8k does not get you either — the
        far end of the prompt is dropped somewhere inside the backend, quietly,
        and the first anyone knows of it is a character who has forgotten the
        last hour. So the backend is asked what it can serve and the budget is
        fitted to it, reply first: the answer is the thing being paid for.

        A backend with no way to say keeps the configured budget, which is the
        behaviour this had before it could ask at all.
        """
        settings = self.settings
        try:
            limit = await provider.context_limit()
        except Exception:  # a backend that cannot answer must not fail a turn
            limit = None
        # What this backend will actually be asked for, not what the pass
        # would like: a 512-token Horde worker and a 5000-token Ollama need
        # different amounts of the window left over.
        reply = provider.cap(definition.sampling) if hasattr(provider, "cap") else 0
        reply = reply or definition.sampling.max_tokens or 0
        fitted = assembly.fit_token_budget(settings, limit, reply)
        return settings if fitted == settings.token_budget else replace(settings, token_budget=fitted)

    # ------------------------------------------------------- post_process

    def _polish_pass(self) -> PassDef:
        return registry.get_pass(self.db, "post_process") or next(
            p for p in registry.CANONICAL_PASSES if p.id == "post_process"
        )

    def _polish_enabled(self, definition: PassDef) -> bool:
        """Whether post_process should run this turn — checked once, before
        the reply even starts streaming, because the answer decides whether
        the raw text is shown live or held back (§ _run_reply/_run_swipe).
        Not signal-gated the way state_auditor/expression are: it is the only
        thing left on the foreground tier, and its job is consistency across
        every reply rather than a response to something unusual happening.
        """
        return definition.enabled and "foreground" not in (self.settings.tiers_off or [])

    async def _polish_reply(
        self, ctx: TurnContext, definition: PassDef, reply: str, assembled
    ) -> tuple[str, str]:
        """Runs post_process on a finished, cleaned reply.

        Returns `(final, draft)` — `draft` is `""` when nothing changed
        (post_process decided the draft needed nothing, or it failed and fell
        back), which is also what the caller stores as `draft_text`, so
        "Restore original draft" only ever shows for a message that is
        actually different from what the model first wrote.
        """
        provider = provider_for_tier(definition.model_tier, self.settings)
        run_id = self._record_run(
            ctx, definition, "running", model=provider.model, started_at=time.time()
        )
        try:
            edited = await reply_polish.run(
                provider, definition, reply, ctx.character.name, assembled.parts,
                self.settings.pass_timeout,
            )
        except asyncio.CancelledError:
            # The reader hung up while this was still working. Its own run
            # would otherwise sit at "running" forever — nothing ever marks a
            # pass done once its coroutine stops being awaited — so this is
            # marked before the cancellation is let through to the caller,
            # which has its own cleanup to do for the reply itself (§ the
            # try/except around this call in _run_reply/_run_swipe).
            self._record_run(ctx, definition, "stopped", run_id=run_id, finished_at=time.time())
            raise
        self._record_run(
            ctx, definition, "done", run_id=run_id,
            model=provider.model, finished_at=time.time(),
        )
        return (edited, reply) if edited != reply else (reply, "")

    async def _one_more_go(self, provider, request, definition) -> str:
        """A second attempt at a reply that came back with nothing usable.

        Reported against GLM-4.7-flash on Ollama, and true of every small
        reasoning model: every few turns it reasons and then stops, or emits
        the state block and stops, and the turn arrives empty. The setup is
        fine — the same prompt works the next time — so failing the turn asks
        someone to fix something that is not broken.

        Deliberately not the same request again. Reasoning is switched off for
        this one, which is the difference that makes it likely to work, and it
        is unstreamed: nobody watches a recovery, and the text arriving in one
        piece is paced by the client anyway.
        """
        retry = replace(request, stream=False, think=False)
        try:
            result = await asyncio.wait_for(
                provider.generate(retry), timeout=self.settings.pass_timeout
            )
        except (ProviderError, asyncio.TimeoutError, OSError):
            return ""
        body, _thinking = split_thinking(result.text)
        return body

    def run_pass_now(self, chat_id: str, pass_id: str) -> dict:
        """Run one pass on demand, outside the turn cycle.

        The trigger is the only thing bypassed. Everything else is the ordinary
        path — same context, same slice write, same run row, same pass_status
        events — so a hand-refreshed panel is indistinguishable from one the
        scheduler decided to refresh, and arbitration still applies. Slice
        writes are rejected only by a *newer* source turn (§5.5), so refreshing
        at the turn already on screen is a real refresh, not a no-op.

        Launched rather than awaited: a background-tier pass can take as long
        as any other, and the caller follows it over the event stream that
        already drives the refreshing indicator.
        """
        chat = repo.get_chat(self.db, chat_id)
        character = repo.get_character(self.db, chat["character_id"]) if chat else None
        if chat is None or character is None:
            return {"ok": False, "error": "unknown chat"}

        definition = registry.get_pass(self.db, pass_id)
        if definition is None or not definition.enabled:
            return {"ok": False, "error": f"unknown pass {pass_id!r}"}

        messages = repo.list_messages(self.db, chat_id, include_dropped=False)
        if not messages:
            return {"ok": False, "error": "nothing said yet"}
        last = messages[-1]

        ctx = TurnContext(
            chat=chat,
            character=character,
            settings=self.settings,
            turn=last["turn"],
            message_id=last["id"],
            variant_id=last.get("variant_id", ""),
            schema=state_mod.load_schema(
                {k: v.model_dump() for k, v in character.state_schema.items()}
                if character.state_schema
                else None
            ),
        )
        ctx.toggle_states = registry.toggle_states(self.db, character.id, chat_id)
        ctx.pre_values = assembly.current_values(self.db, chat_id, ctx.schema, ctx.character.id)

        run_id = self._record_run(ctx, definition, "pending")
        task = asyncio.create_task(
            self._run_background(ctx, definition, [], asyncio.Event(), run_id),
            name=f"manual:{pass_id}:{chat_id}",
        )
        self._track(chat_id, task)
        return {"ok": True, "run_id": run_id, "pass_id": pass_id}

    async def reaudit(self, message_id: str) -> dict:
        """Re-run the auditor against an edited message (§9)."""
        message = repo.get_message(self.db, message_id)
        if message is None:
            return {"ok": False, "error": "unknown message"}
        chat = repo.get_chat(self.db, message["chat_id"])
        character = repo.get_character(self.db, chat["character_id"]) if chat else None
        definition = registry.get_pass(self.db, "state_auditor")
        if chat is None or character is None or definition is None:
            return {"ok": False, "error": "auditor unavailable"}

        ctx = TurnContext(
            chat=chat,
            character=character,
            settings=self.settings,
            turn=message["turn"],
            message_id=message_id,
            variant_id=message["variant_id"],
            schema=state_mod.load_schema(
                {k: v.model_dump() for k, v in character.state_schema.items()}
                if character.state_schema
                else None
            ),
        )
        ctx.toggle_states = registry.toggle_states(self.db, character.id, chat["id"])
        ctx.pre_values = assembly.current_values(self.db, chat["id"], ctx.schema, character.id)
        await self._execute(ctx, definition)
        return {"ok": True}


# ------------------------------------------------------------------ helpers


def _with_character_stops(sampling: Sampling, character: Character) -> Sampling:
    """The pass's sampling plus whatever this character keeps saying.

    Copied rather than mutated: `definition.sampling` is the stored pass
    definition, shared across every chat, and appending to it would leak one
    character's stop strings into everybody else's replies.
    """
    if not character.stop_strings:
        return sampling
    merged = sampling.model_copy(deep=True)
    merged.stop = list(dict.fromkeys([*merged.stop, *character.stop_strings]))
    return merged


def _compare(left: int, op: str, right: int) -> bool:
    return {
        ">=": left >= right,
        ">": left > right,
        "==": left == right,
        "!=": left != right,
        "<": left < right,
        "<=": left <= right,
    }.get(op, False)


def _deltas_between(before: dict[str, float], after: dict[str, float]) -> dict[str, float]:
    return {
        name: round(after.get(name, value) - value, 3)
        for name, value in before.items()
        if abs(after.get(name, value) - value) > 1e-9
    }


def _state_view(
    schema: dict[str, VariableSchema], values: dict[str, float], *, provisional: bool
) -> dict:
    """What the GUI shows: bands and labels, never raw numbers in the prompt."""
    return {
        "provisional": provisional,
        "values": values,
        "bands": [
            {"variable": label, "band": band, "guidance": guidance}
            for label, band, guidance in state_mod.band_guidance(schema, values)
        ],
    }


def _clean_panel_payload(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool))}


def _itemised(assembled: assembly.Assembled, contract: str) -> list[dict]:
    """The assembled sections plus the one the scheduler adds itself.

    The state contract is bolted onto the system prompt here rather than in
    assembly, so without this it would be the one part of the prompt that the
    "what was sent" view could not account for — and a breakdown that does not
    add up to what was charged is worse than no breakdown, because it is
    believed.
    """
    parts = list(assembled.parts)
    if not contract.strip():
        return parts
    # It is appended to the system prompt, so it belongs at the end of the
    # prefix run — not at the end of the list, which would show it sitting
    # after the volatile block it actually precedes.
    after_prefix = len([p for p in parts if p["band"] == "prefix"])
    parts.insert(after_prefix, {
        "id": "state_contract",
        "label": "How to report state",
        "band": "prefix",
        "custom": False,
        "tokens": estimate_tokens(contract),
        "text": contract,
    })
    return parts


def _why_empty(
    raw: str, thinking: str, body: str, *, used: int = 0, budget: int = 0
) -> str:
    """Why a reply came back with nothing in it, in words that name the fix.

    Every branch here is something a real local model does, and every one of
    them used to arrive as a single "…" with no way to tell them apart.

    `used` and `budget` turn the commonest one from a guess into a fact: a
    reasoning model that spent the whole allowance thinking is not a maybe, it
    is a number, and the message can say so.
    """
    # Reasoning first: a backend that parses the think block itself streams
    # nothing at all when the model never stops thinking, so testing the raw
    # text first would answer "nothing arrived" when something did.
    if thinking.strip() and not body.strip():
        spent = (
            f" It spent all {budget} tokens reasoning."
            if budget and used >= budget * 0.9
            else ""
        )
        return (
            "The model produced only its reasoning and never reached the "
            f"reply.{spent} Turn Thinking off for this backend under Settings "
            "\u2192 Backends, or raise Max tokens for the Reply pass under "
            "Brain \u2192 Sampling."
        )
    if not raw.strip():
        if used:
            # The backend counted tokens it never handed over. That is not a
            # quiet model — something between the model and here ate them, and
            # on Ollama it is always its own parser holding a reasoning
            # model's think block back.
            return (
                f"The backend generated {used} tokens and handed back none of "
                "them as text. Something between the model and here kept them "
                "\u2014 on Ollama that is its parser holding back a reasoning "
                "model's thinking. Set Thinking to off for this backend under "
                "Settings \u2192 Backends; if it already is, set Template to "
                "anything other than Messages, which bypasses the parser."
            )
        return (
            "The model returned nothing at all. Check that the model is loaded "
            "and that the stop strings for this backend or character are not "
            "matching immediately."
        )
    if MARKER in raw:
        return (
            "The model wrote the state block and no reply. It usually settles "
            "after a turn or two; if it does not, the model is too small to "
            "follow the output contract."
        )
    return (
        "The whole reply was removed as a continuation of your own turn. Turn "
        "off \"strip user turn leakage\" in the Brain panel if that was wrong."
    )


def _suffix_instructions(ctx: TurnContext) -> str:
    variables = ", ".join(ctx.schema.keys()) or "none"
    return (
        f"## Output contract\n"
        f"Tracked variables: {variables}.\n{REPLY_SUFFIX_MARKER_HELP}"
    )
