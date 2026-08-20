"""Ollama backend — the blocking-fast tier over Tailscale (§3).

Uses /api/chat, which is chat-native, so no instruct template is applied unless
one is configured explicitly. Ollama reports real token counts, so cost
accounting (§14) is exact on this tier.

Reasoning models are the one sharp edge. Ollama parses a thinking model's
output itself and returns the reasoning under `message.thinking`, leaving
`message.content` empty until the model stops thinking — so a reply pass with a
few hundred tokens of budget spends all of them reasoning and returns nothing
at all, over a request that looks completely successful from the outside. Hence
`think` (§5.6): off by default, and the reasoning that does arrive is captured
rather than dropped, so the empty-reply message can say which of the two
happened.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from ..models import Sampling
from . import templates
from .. import samplers
from .base import GenRequest, GenResult, Provider, ProviderError, _copy_into, estimate_tokens


# How much room a model that reasons is given for the reasoning, on top of the
# reply's own budget. Doubling is the honest reading of what a pass asked for:
# `max_tokens` is how long the *answer* may be, and thinking is not the answer.
#
# Reported, with the numbers: GLM-4.7-flash on a 1000-token budget spent all
# thousand working out what to say and stopped mid-sentence before saying any
# of it. The retry recovers that turn; this is what stops it happening.
THINKING_HEADROOM = 1.0
THINKING_HEADROOM_CAP = 1200

# Kept back from the window when fitting a reply into it: the token estimate is
# four characters to a token, which is close and never exact.
_WINDOW_SLACK = 128
# However little room is left, asking for a handful of tokens is worse than
# asking for none — the reply is cut off mid-word either way, and this at least
# gets a sentence.
_MIN_PREDICT = 256


def _options(
    sampling: Sampling, stop: list[str], kind: str = "ollama", *, thinking: bool = False
) -> dict:
    """Ollama's `options` block: the samplers it takes, under its own names.

    Only what has been moved off neutral (§17) — Ollama ignores an option it
    does not know, but a request carrying a dozen parameters nobody set is how
    output changes for reasons nobody can account for.
    """
    predict = sampling.max_tokens
    if thinking and predict > 0:
        predict += min(int(predict * THINKING_HEADROOM), THINKING_HEADROOM_CAP)
    return {
        **samplers.params_for(kind, sampling),
        "num_predict": predict,
        "stop": stop,
    }


# (base_url, model) -> tokens. A loaded model's window cannot change without a
# reload, so this is asked once rather than on every turn.
_CONTEXT_CACHE: dict[tuple[str, str], int] = {}


class OllamaProvider(Provider):
    kind = "ollama"
    native_chat = True
    # Ollama takes `images` on a chat message; whether the loaded model can
    # actually see them is the model's business, but the wire format is there.
    sees_images = True

    def __init__(self, config) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        # Set once an Ollama that rejects `think` outright has told us so, so
        # the retry below happens at most once per process rather than on every
        # single request (§5.6).
        self._no_think_field = False

    def think(self, request: GenRequest | None = None) -> bool | None:
        """None means "send nothing and let the model's template decide"."""
        if self._no_think_field:
            return None
        # A request that has made up its own mind wins over the backend's
        # setting — nothing else can say "not this time".
        if request is not None and request.think is not None:
            return request.think
        mode = getattr(self.config, "think", "auto")
        if mode == "auto":
            return None
        return mode == "on"

    async def context_limit(self) -> int | None:
        """What this model is actually serving, prompt and reply together.

        Two sources, in order. `/api/ps` knows what a *loaded* model was loaded
        with, which is the number that matters — Ollama sizes it from VRAM and
        it is routinely smaller than the model's own maximum. `/api/show` knows
        the model's architectural limit, which is the right answer for a model
        that is not loaded yet.

        Cached per URL and model: it cannot change without a reload, and asking
        on every turn would put two extra round trips in front of every reply.
        """
        key = (self.config.base_url, self.model)
        if key in _CONTEXT_CACHE:
            return _CONTEXT_CACHE[key]
        limit = await self._ask_context()
        if limit:
            _CONTEXT_CACHE[key] = limit
        return limit

    async def _ask_context(self) -> int | None:
        try:
            running = await self.client().get("/api/ps")
            if running.status_code == 200:
                for row in (running.json() or {}).get("models") or []:
                    if row.get("name") == self.model or row.get("model") == self.model:
                        found = int(row.get("context_length") or 0)
                        if found:
                            return found
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        try:
            shown = await self.client().post("/api/show", json={"model": self.model})
            if shown.status_code != 200:
                return None
            info = (shown.json() or {}).get("model_info") or {}
        except (httpx.HTTPError, ValueError, TypeError):
            return None
        for name, value in info.items():
            # `llama.context_length`, `deepseek2.context_length`, and so on: the
            # architecture prefixes it, and the architecture is whatever this
            # model happens to be.
            if name.endswith(".context_length"):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            base = self.config.base_url or "http://127.0.0.1:11434"
            self._client = httpx.AsyncClient(
                base_url=base.rstrip("/"), timeout=self.config.timeout
            )
        return self._client

    def _payload(
        self, request: GenRequest, stream: bool, limit: int | None = None
    ) -> tuple[str, dict]:
        stop = self.stop_strings(request.sampling)
        template = self.template()
        think = self.think(request)
        # `None` is "the model's template decides", which for a reasoning model
        # means it will. Anything but an explicit no gets the headroom.
        options = _options(request.sampling, stop, thinking=think is not False)
        if limit:
            # The last word on how much room there is. The scheduler has
            # already fitted the *prompt* to this window; this is the other
            # half — asking for more reply than the window has left does not
            # produce more reply, it produces a truncated prompt inside Ollama
            # where nobody can see it.
            room = limit - request.estimated_input_tokens() - _WINDOW_SLACK
            options["num_predict"] = max(_MIN_PREDICT, min(options["num_predict"], room))
        if template == "messages":
            messages = []
            if request.system:
                messages.append({"role": "system", "content": request.system})
            messages.extend(request.messages)
            if request.images:
                # On the newest user turn, which is the one they were attached
                # to. Ollama wants bare base64 with no data: prefix.
                for message in reversed(messages):
                    if message["role"] == "user":
                        message["images"] = list(request.images)
                        break
            if request.prefill:
                messages.append({"role": "assistant", "content": request.prefill})
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": stream,
                "options": options,
            }
        else:
            # Explicit template configured: drive the raw completion endpoint so
            # our framing is the one the model actually sees.
            payload = {
                "model": self.model,
                "prompt": request.prompt_text(template, self.config.template_spec),
                "raw": True,
                "stream": stream,
                "options": options,
            }
        if think is not None:
            payload["think"] = think
        if request.expects_json:
            # Constrained decoding beats asking politely for JSON, especially
            # from the small models on the background tier.
            payload["format"] = "json"
        return ("/api/chat" if template == "messages" else "/api/generate"), payload

    @staticmethod
    def _delta(chunk: dict) -> str:
        if "message" in chunk:
            return chunk["message"].get("content", "")
        return chunk.get("response", "")

    @staticmethod
    def _think_delta(chunk: dict) -> str:
        """The reasoning channel, which Ollama returns beside the content.

        Never yielded to the caller — it is not what the character said (§5.6)
        — but kept, because "it only reasoned" and "it returned nothing" are
        different faults with different fixes and look identical without it.
        """
        if "message" in chunk:
            return chunk["message"].get("thinking") or ""
        return chunk.get("thinking") or ""

    def _rejects_think(self, response: httpx.Response, payload: dict, body: str) -> bool:
        """Whether this Ollama refused the request *because* of `think`.

        Ollama versions before the reasoning switch existed reject the field
        outright, and some builds reject it for a model with no thinking
        capability even when it is set to false. Either way the request is
        fine without it, so it is worth one silent retry — and remembering,
        so the cost is one wasted round trip per process and not per turn.
        """
        if "think" not in payload or response.status_code != 400:
            return False
        return "think" in body.lower()

    async def _post(self, url: str, payload: dict) -> dict:
        response = await self.client().post(url, json=payload)
        if self._rejects_think(response, payload, response.text):
            self._no_think_field = True
            payload.pop("think", None)
            response = await self.client().post(url, json=payload)
        response.raise_for_status()
        return response.json()

    async def generate(self, request: GenRequest) -> GenResult:
        url, payload = self._payload(request, stream=False, limit=await self.context_limit())
        try:
            data = await self._post(url, payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama: {exc}") from exc
        text = self._delta(data)
        return GenResult(
            text=text,
            tokens_in=data.get("prompt_eval_count") or request.estimated_input_tokens(),
            tokens_out=data.get("eval_count") or estimate_tokens(text),
            model=self.model,
            provider=self.name,
            thinking=self._think_delta(data),
            raw=data,
        )

    async def stream(
        self, request: GenRequest, sink: GenResult | None = None
    ) -> AsyncIterator[str]:
        url, payload = self._payload(request, stream=True, limit=await self.context_limit())
        collected: list[str] = []
        thought: list[str] = []
        final: dict = {}
        try:
            async for chunk in self._stream_chunks(url, payload):
                if chunk.get("error"):
                    raise ProviderError(f"ollama: {chunk['error']}")
                reasoning = self._think_delta(chunk)
                if reasoning:
                    thought.append(reasoning)
                delta = self._delta(chunk)
                if delta:
                    collected.append(delta)
                    yield delta
                if chunk.get("done"):
                    final = chunk
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama: {exc}") from exc

        if sink is not None:
            text = "".join(collected)
            _copy_into(
                GenResult(
                    text=text,
                    tokens_in=final.get("prompt_eval_count") or request.estimated_input_tokens(),
                    tokens_out=final.get("eval_count") or estimate_tokens(text),
                    model=self.model,
                    provider=self.name,
                    thinking="".join(thought),
                    raw=final,
                ),
                sink,
            )

    async def _stream_chunks(self, url: str, payload: dict) -> AsyncIterator[dict]:
        """NDJSON chunks, with the one retry `think` may need.

        The retry is only ever taken on the status line, before a single chunk
        has been handed on, so nothing can arrive twice.
        """
        while True:
            async with self.client().stream("POST", url, json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors="replace")
                    if self._rejects_think(response, payload, body):
                        self._no_think_field = True
                        payload.pop("think", None)
                        continue
                    response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    yield chunk
            return

    @staticmethod
    def parse_models(data) -> list[str]:
        """/api/tags → the models actually pulled on that machine.

        Defensive about shape: a base_url pointing at something that is not
        Ollama still returns *some* JSON, and that should read as "no models",
        not as a crash.
        """
        out = []
        for item in (data.get("models") if isinstance(data, dict) else None) or []:
            name = item.get("name") or item.get("model") if isinstance(item, dict) else item
            if name:
                out.append(str(name))
        return sorted(set(out))

    async def list_models(self) -> list[str]:
        try:
            response = await self.client().get("/api/tags")
            response.raise_for_status()
            return self.parse_models(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"ollama: could not list models: {exc}") from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class LlamaCppProvider(OllamaProvider):
    """On-device llama.cpp server in Termux (§3, foreground-mid tier).

    llama.cpp's server exposes an OpenAI-ish chat API, but the on-device model
    is small and template-sensitive, so this defaults to explicit templating
    through the completion endpoint.
    """

    kind = "llamacpp"
    native_chat = False

    async def context_limit(self) -> int | None:
        """`/props` reports what the server was started with, which for
        llama.cpp is the whole story: `-c` is the window and there is no
        per-request resizing."""
        key = (self.config.base_url, self.model)
        if key in _CONTEXT_CACHE:
            return _CONTEXT_CACHE[key]
        try:
            response = await self.client().get("/props")
            if response.status_code != 200:
                return None
            body = response.json() or {}
        except (httpx.HTTPError, ValueError, TypeError):
            return None
        found = body.get("n_ctx") or (body.get("default_generation_settings") or {}).get("n_ctx")
        try:
            found = int(found or 0)
        except (TypeError, ValueError):
            return None
        if found:
            _CONTEXT_CACHE[key] = found
        return found or None
    # The /completion endpoint this drives takes a prompt string and nothing
    # else, so there is nowhere to put an image.
    sees_images = False

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            base = self.config.base_url or "http://127.0.0.1:8080"
            self._client = httpx.AsyncClient(
                base_url=base.rstrip("/"), timeout=self.config.timeout
            )
        return self._client

    def _payload(self, request: GenRequest, stream: bool) -> tuple[str, dict]:
        template = self.template()
        if template == "messages":
            template = templates.guess_template(self.model)
        sampling = request.sampling
        # Flat, not nested in `options` — and this is the one backend that
        # takes DRY and XTC, which llama.cpp gained well before Ollama exposed
        # them.
        return "/completion", {
            "prompt": request.prompt_text(template, self.config.template_spec),
            **samplers.params_for(self.kind, sampling),
            "n_predict": sampling.max_tokens,
            "stop": self.stop_strings(sampling),
            "stream": stream,
        }

    @staticmethod
    def _delta(chunk: dict) -> str:
        return chunk.get("content", "")

    @staticmethod
    def parse_models(data) -> list[str]:
        """llama.cpp serves one model at a time, via the OpenAI-shaped route."""
        rows = (data.get("data") if isinstance(data, dict) else None) or []
        out = [str(m["id"]) for m in rows if isinstance(m, dict) and m.get("id")]
        return sorted(set(out))

    async def list_models(self) -> list[str]:
        try:
            response = await self.client().get("/v1/models")
            response.raise_for_status()
            return self.parse_models(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"llamacpp: could not list models: {exc}") from exc

    async def stream(
        self, request: GenRequest, sink: GenResult | None = None
    ) -> AsyncIterator[str]:
        url, payload = self._payload(request, stream=True, limit=await self.context_limit())
        collected: list[str] = []
        try:
            async with self.client().stream("POST", url, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        chunk = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    delta = self._delta(chunk)
                    if delta:
                        collected.append(delta)
                        yield delta
        except httpx.HTTPError as exc:
            raise ProviderError(f"llamacpp: {exc}") from exc
        if sink is not None:
            text = "".join(collected)
            _copy_into(
                GenResult(
                    text=text,
                    tokens_in=request.estimated_input_tokens(),
                    tokens_out=estimate_tokens(text),
                    model=self.model,
                    provider=self.name,
                ),
                sink,
            )
