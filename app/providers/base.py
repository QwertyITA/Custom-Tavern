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
    # Whether the caller wants JSON rather than prose. Backends that can
    # constrain output (Ollama's format, OpenAI's response_format) use it;
    # the rest ignore it. Declared by the pass, never guessed from the prompt.
    expects_json: bool = False
    # Which pass this request belongs to. Real backends ignore it; it exists so
    # routing and mocks never have to infer intent from prompt wording.
    pass_id: str = ""

    def prompt_text(self, template: str) -> str:
        return templates.render(template, self.system, self.messages, self.prefill)

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


class ProviderError(RuntimeError):
    """Backend failed. The scheduler decides whether to retry or fall back."""


class Provider:
    kind = "base"
    # True when the backend speaks a native chat-message API and needs no
    # instruct template applied by us.
    native_chat = False

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
            stops.extend(templates.stop_for(template))
        return [s for s in dict.fromkeys(stops) if s]  # de-dupe, keep order

    async def generate(self, request: GenRequest) -> GenResult:
        raise NotImplementedError

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
