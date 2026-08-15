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
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .. import assembly
from .. import macros, memory as memory_store, repo, state as state_mod
from ..config import Settings
from ..db import Database
from ..events import BUS
from ..markup import to_plain
from ..models import Character, PassDef, Sampling, VariableSchema
from ..postprocess import clean_reply, split_thinking
from ..providers import GenRequest, GenResult, ProviderError, provider_for_tier
from ..providers.base import estimate_tokens
from ..state import SLICE_SIGNALS, SLICE_VARS
from . import registry
from .contract import (
    REPLY_SUFFIX_MARKER_HELP,
    SuffixStreamFilter,
    normalise_payload,
    parse_json_loose,
    signal_rank,
    split_state_suffix,
)


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

    @property
    def chat_id(self) -> str:
        return self.chat["id"]


class PassScheduler:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        # Background tasks per chat, so the next turn can wait briefly on them.
        self._pending: dict[str, set[asyncio.Task]] = {}

    # ------------------------------------------------------------- plumbing

    def _emit(self, chat_id: str, event: dict[str, Any]) -> None:
        BUS.publish(chat_id, event)

    def _track(self, chat_id: str, task: asyncio.Task) -> None:
        tasks = self._pending.setdefault(chat_id, set())
        tasks.add(task)
        task.add_done_callback(lambda t: tasks.discard(t))

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
        for definition in definitions:
            if definition.id == "basic" or not definition.enabled:
                continue
            if definition.id in disabled:
                continue
            if self.trigger_fires(definition, ctx):
                out.append(definition)
        return out

    def trigger_fires(self, definition: PassDef, ctx: TurnContext) -> bool:
        trigger = definition.trigger
        if trigger.type == "manual":
            return False
        if trigger.type == "every_turn":
            return True
        if trigger.type == "every_n":
            return trigger.n > 0 and ctx.turn % trigger.n == 0
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

    async def run_turn(self, chat_id: str, user_text: str) -> AsyncIterator[dict]:
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
        character = repo.get_character(self.db, chat["character_id"])
        if character is None:
            yield {"type": "error", "error": "chat has no character"}
            return

        # Resolved before it is stored, like the greeting: what the user typed
        # is what gets recorded, and {{char}} in their own message should read
        # as the character's name in the transcript too.
        user_text = macros.substitute(
            user_text, assembly.macro_context(self.db, chat, character)
        )
        user_message = repo.add_message(self.db, chat_id, "user", user_text)
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

        yield {"type": "turn_start", "turn": turn, "message": user_message}

        # --- cheapest tier first: deterministic decay + regex nudges (§6) ---
        values = assembly.current_values(self.db, chat_id, ctx.schema)
        values = state_mod.decay_step(ctx.schema, values)
        nudges = state_mod.load_nudges(
            (chat.get("settings") or {}).get("nudges")
            or getattr(character, "nudges", None)
        )
        values, fired = state_mod.apply_nudges(nudges, ctx.schema, values, user_text, "user")
        ctx.pre_values = values
        if fired:
            yield {"type": "nudges", "fired": fired}

        async for event in self._run_reply(ctx):
            yield event

        if not ctx.message_id:
            return  # the reply failed; nothing downstream is meaningful

        # --- non-blocking passes: parallel, write-on-arrival (§5.5) ---
        launched = self._launch_background(ctx)
        if launched:
            yield {"type": "background_queued", "passes": launched}
        yield {"type": "turn_end", "turn": turn}

    async def _run_reply(self, ctx: TurnContext) -> AsyncIterator[dict]:
        definition = registry.get_pass(self.db, "basic")
        if definition is None:
            definition = registry.CANONICAL_PASSES[0]

        injections = registry.active_injections(self.db, ctx.toggle_states, "basic")
        assembled = assembly.build_reply_context(
            self.db, ctx.chat, ctx.character, self.settings, toggle_injections=injections
        )

        contract = _suffix_instructions(ctx)
        system = assembled.system + "\n\n" + contract
        request = GenRequest(
            system=system,
            messages=assembled.messages,
            sampling=_with_character_stops(definition.sampling, ctx.character),
            pass_id=definition.id,
        )

        provider = provider_for_tier(definition.model_tier, self.settings)
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
            self.db, run_id, ctx.chat_id, _itemised(assembled, contract)
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
        collected: list[str] = []
        try:
            async for delta in provider.stream(request, sink):
                visible = suffix.feed(delta)
                if visible:
                    collected.append(visible)
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

        tail, payload = suffix.finish()
        if tail:
            collected.append(tail)
            yield {"type": "delta", "text": tail}

        raw_reply = "".join(collected)
        body, thinking = split_thinking(raw_reply)
        # A model that ignored the suffix contract may still have emitted it
        # inside a think block or after it; check the full text once more.
        if payload is None:
            body, payload = split_state_suffix(body)
        reply = clean_reply(
            body,
            strip_leakage=self.settings.strip_user_turn_leakage,
            user_names=("You", "{{user}}"),
        )
        if thinking:
            yield {"type": "thinking", "text": thinking}
        if not reply.strip():
            reply = "…"

        message = repo.add_message(
            self.db,
            ctx.chat_id,
            "assistant",
            reply,
            turn=ctx.turn,
            provider=sink.provider or provider.name,
            model=sink.model or provider.model,
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
            SLICE_VARS,
            values,
            source_turn=ctx.turn,
            source_pass="basic",
            variant_id=ctx.variant_id,
            provisional=True,
        )
        await state_mod.write_slice(
            self.db,
            ctx.chat_id,
            SLICE_SIGNALS,
            ctx.signals,
            source_turn=ctx.turn,
            source_pass="basic",
            variant_id=ctx.variant_id,
            provisional=True,
        )

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
                f"{json.dumps(_deltas_between(ctx.pre_values, assembly.current_values(self.db, ctx.chat_id, ctx.schema)))}"
            )
        elif definition.id == "summary":
            pending, covered = assembly.pending_summary_text(
                self.db, ctx.chat_id, character.name
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
            history = repo.list_messages(self.db, ctx.chat_id, include_dropped=False)
            covered = assembly.memory_covered_turn(self.db, ctx.chat_id)
            fresh = [m for m in history if m["turn"] > covered]
            if not fresh:
                return "", [], None
            transcript = "\n".join(
                f"{'User' if m['role'] == 'user' else character.name}: {to_plain(m['text'])}"
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
                    SLICE_VARS,
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
            assembly.apply_eviction(self.db, ctx.chat_id, self.settings)
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
            assembly.apply_eviction(self.db, ctx.chat_id, self.settings)
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
        ctx.pre_values = assembly.current_values(self.db, chat["id"], ctx.schema)

        definition = registry.get_pass(self.db, "basic") or registry.CANONICAL_PASSES[0]
        injections = registry.active_injections(self.db, ctx.toggle_states, "basic")
        assembled = assembly.build_reply_context(
            self.db, chat, character, self.settings,
            toggle_injections=injections,
            exclude_message_id=message_id,
        )
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
        collected: list[str] = []
        try:
            async for delta in provider.stream(request, sink):
                visible = suffix.feed(delta)
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
        ctx.pre_values = assembly.current_values(self.db, chat["id"], ctx.schema)

        definition = registry.get_pass(self.db, "basic") or registry.CANONICAL_PASSES[0]
        injections = registry.active_injections(self.db, ctx.toggle_states, "basic")
        assembled = assembly.build_reply_context(
            self.db,
            chat,
            character,
            self.settings,
            toggle_injections=injections,
            exclude_message_id=message_id,
        )
        contract = _suffix_instructions(ctx)
        request = GenRequest(
            system=assembled.system + "\n\n" + contract,
            messages=assembled.messages,
            sampling=_with_character_stops(definition.sampling, character),
            pass_id=definition.id,
        )
        provider = provider_for_tier(definition.model_tier, self.settings)
        run_id = self._record_run(
            ctx, definition, "running", model=provider.model, started_at=time.time()
        )
        # A re-roll is its own prompt, assembled after whatever the last attempt
        # changed, so it gets its own record rather than sharing the first
        # attempt's (§9).
        repo.save_prompt_record(self.db, run_id, chat["id"], assembled.parts)

        sink = GenResult()
        suffix = SuffixStreamFilter()
        collected: list[str] = []
        try:
            async for delta in provider.stream(request, sink):
                visible = suffix.feed(delta)
                if visible:
                    collected.append(visible)
                    yield {"type": "delta", "text": visible}
        except (ProviderError, asyncio.TimeoutError, OSError) as exc:
            self._record_run(
                ctx, definition, "failed", run_id=run_id, error=str(exc), finished_at=time.time()
            )
            yield {"type": "error", "error": f"swipe failed: {exc}"}
            return

        tail, payload = suffix.finish()
        if tail:
            collected.append(tail)
            yield {"type": "delta", "text": tail}

        body, thinking = split_thinking("".join(collected))
        if payload is None:
            body, payload = split_state_suffix(body)
        reply = clean_reply(body, strip_leakage=self.settings.strip_user_turn_leakage) or "…"

        variant = repo.add_variant(
            self.db, message_id, reply, provider=provider.name, model=sink.model or provider.model
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
            SLICE_VARS,
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
        ctx.pre_values = assembly.current_values(self.db, chat_id, ctx.schema)

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
        ctx.pre_values = assembly.current_values(self.db, chat["id"], ctx.schema)
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


def _suffix_instructions(ctx: TurnContext) -> str:
    variables = ", ".join(ctx.schema.keys()) or "none"
    return (
        f"## Output contract\n"
        f"Tracked variables: {variables}.\n{REPLY_SUFFIX_MARKER_HELP}"
    )
