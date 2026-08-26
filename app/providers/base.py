"""Provider abstraction: one interface over Ollama / OpenAI-compatible / Horde /
on-device llama.cpp / the built-in echo backend.

Tier, template and sampling are all per-pass settings (§17), so a provider takes
them per request rather than baking them in at construction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from ..config import BackendConfig
from ..models import Sampling
from . import templates

Message = dict[str, str]


def estimate_tokens(text: str) -> int:
    """Cheap token estimate — no tokenizer dependency on a phone.

    Only used when a backend does not report real usage. ~4 chars/token holds
    well enough for cost accounting (§14) to be meaningful.
    """
    if not text:
        return 0
    return max(1, round(len(text) / 4))


@dataclass
class GenRequest:
    system: str = ""
    messages: list[Message] = field(default_factory=list)
    sampling: Sampling = field(default_factory=Sampling)
    prefill: str = ""
    stream: bool = False
    # Base64 images to attach to the newest user turn (§19). Only ever filled
    # when the chosen provider declares it can see them.
    images: list[str] = field(default_factory=list)
    # Whether the caller wants JSON rather than prose. Backends that can
    # constrain output (Ollama's format, OpenAI's response_format) use it;
    # the rest ignore it. Declared by the pass, never guessed from the prompt.
    expects_json: bool = False
    # Overrides the backend's own thinking setting for this one request, when
    # the caller has a reason (§5.6): the retry after an empty reply asks for
    # no reasoning, because reasoning is what ate the first attempt.
    think: bool | None = None
    # Which pass this request belongs to. Real backends ignore it; it exists so
    # routing and mocks never have to infer intent from prompt wording.
    pass_id: str = ""

    def prompt_text(self, template: str, spec: dict | None = None) -> str:
        return templates.render(template, self.system, self.messages, self.prefill, spec)

    def estimated_input_tokens(self) -> int:
        body = self.system + "".join(m["content"] for m in self.messages)
        return estimate_tokens(body)


@dataclass
class GenResult:
    text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""
    provider: str = ""
    thinking: str = ""  # captured <think> block, never displayed inline (§5.6)
    raw: dict[str, Any] = field(default_factory=dict)


def _copy_into(source: GenResult, sink: GenResult) -> GenResult:
    for field_name in ("text", "tokens_in", "tokens_out", "model", "provider", "thinking", "raw"):
        setattr(sink, field_name, getattr(source, field_name))
    return sink


class ReasoningDelta(str):
    """A chunk of the model *reasoning*, not of what the character said (§5.6).

    Providers yield it from `stream()` in among the ordinary text deltas, for
    the backends that hand reasoning back on a channel of its own rather than
    inside the reply. Without it there is nothing to see while a reasoning
    model works: no visible token arrives until it stops thinking, so a model
    two minutes into a thought and a backend that never answered look exactly
    the same from the client.

    A `str` subclass rather than a second channel or a callback because
    `stream()` is a bare `AsyncIterator[str]` implemented by five providers and
    stubbed in half the tests; a wider signature would have to be threaded
    through all of them. Every consumer is in the scheduler, and each one
    checks for it before treating a delta as reply text.
    """

    __slots__ = ()


class ProviderError(RuntimeError):
    """Backend failed. The scheduler decides whether to retry or fall back."""


class Provider:
    kind = "base"
    # True when the backend speaks a native chat-message API and needs no
    # instruct template applied by us.
    native_chat = False
    # True when the backend can be sent an image and actually look at it (§19).
    # Declared rather than attempted: a backend that cannot see one usually
    # accepts the field and ignores it, so "send it and see" produces a reply
    # that reads as if nothing was attached.
    sees_images = False

    def __init__(self, config: BackendConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def model(self) -> str:
        return self.config.model

    def template(self) -> str:
        chosen = self.config.template
        if chosen == "auto":
            return "messages" if self.native_chat else templates.guess_template(self.model)
        return chosen

    def stop_strings(self, sampling: Sampling) -> list[str]:
        """Everything that should end a generation, most specific first.

        The pass's own, then the character's and the backend's, then whatever
        the instruct template needs. Order matters only for the backends that
        cap the list — the ones nearest the story survive the cut.
        """
        stops = list(sampling.stop)
        stops.extend(getattr(self.config, "stop", []) or [])
        template = self.template()
        if template != "messages":
            stops.extend(templates.stop_for(template, getattr(self.config, "template_spec", None)))
        return [s for s in dict.fromkeys(stops) if s]  # de-dupe, keep order

    async def generate(self, request: GenRequest) -> GenResult:
        raise NotImplementedError

    def cap(self, sampling: Sampling) -> int:
        """What a pass may generate here: what it asked for, under this
        backend's ceiling.

        The pass says how much it needs — the reply wants room for eight
        paragraphs, the expression pass wants sixty tokens — and the backend
        says how much it has. Neither is the answer on its own, which is why
        the ceiling lives on the backend and the request lives on the pass.
        """
        want = sampling.max_tokens or 0
        ceiling = int(getattr(self.config, "max_tokens", 0) or 0)
        if not ceiling:
            return want
        return min(want, ceiling) if want else ceiling

    async def context_limit(self) -> int | None:
        """How many tokens this backend can hold at once, prompt and reply
        together — or None when neither side has a way to say.

        The smaller of the two, when both are known. A number typed in is
        still what a backend with no way to answer, or one whose answer is
        wrong, is served by — that half of "configured beats asked" hasn't
        changed. What has: a real, working answer from the backend itself
        now wins over a *larger* configured number too, rather than being
        skipped outright the moment anything was typed in — asking for more
        than a backend actually holds is not ambitious, it is silently
        truncated somewhere out of sight, and a stale "32k" left over from
        switching models is exactly how that happens unnoticed. Configured
        still wins when it is the *smaller* of the two: someone deliberately
        asking for less than a backend could give them is a choice, not a
        mistake to correct.
        """
        configured = int(getattr(self.config, "context", 0) or 0)
        probed = await self._probe_context()
        if configured and probed:
            return min(configured, probed)
        return configured or probed

    async def _probe_context(self) -> int | None:
        """Ask the backend itself. Overridden by the kinds that can be asked."""
        return None

    async def list_models(self) -> list[str]:
        """Models this backend can actually serve right now.

        Typing a model name from memory is the easiest way to misconfigure a
        backend, and the failure surfaces much later as a confusing error from
        the provider. Every backend that can enumerate its models does.
        Returns [] when the backend has no way to say.
        """
        return []

    async def stream(
        self, request: GenRequest, sink: GenResult | None = None
    ) -> AsyncIterator[str]:
        """Yield text deltas; fill `sink` with the final text and usage.

        The sink exists because a generator cannot return a value the consumer
        can see, and the reply pass needs usage numbers for the HUD (§12) the
        moment the stream closes.
        """
        result = await self.generate(request)
        if sink is not None:
            _copy_into(result, sink)
        yield result.text

    async def aclose(self) -> None:
        return None
