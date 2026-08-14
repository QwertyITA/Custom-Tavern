"""Ollama backend — the blocking-fast tier over Tailscale (§3).

Uses /api/chat, which is chat-native, so no instruct template is applied unless
one is configured explicitly. Ollama reports real token counts, so cost
accounting (§14) is exact on this tier.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from ..models import Sampling
from . import templates
from .base import GenRequest, GenResult, Provider, ProviderError, _copy_into, estimate_tokens


def _options(sampling: Sampling, stop: list[str]) -> dict:
    return {
        "temperature": sampling.temp,
        "top_p": sampling.top_p,
        "top_k": sampling.top_k,
        "repeat_penalty": sampling.rep_penalty,
        "num_predict": sampling.max_tokens,
        "stop": stop,
    }


class OllamaProvider(Provider):
    kind = "ollama"
    native_chat = True

    def __init__(self, config) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            base = self.config.base_url or "http://127.0.0.1:11434"
            self._client = httpx.AsyncClient(
                base_url=base.rstrip("/"), timeout=self.config.timeout
            )
        return self._client

    def _payload(self, request: GenRequest, stream: bool) -> tuple[str, dict]:
        stop = self.stop_strings(request.sampling)
        template = self.template()
        if template == "messages":
            messages = []
            if request.system:
                messages.append({"role": "system", "content": request.system})
            messages.extend(request.messages)
            if request.prefill:
                messages.append({"role": "assistant", "content": request.prefill})
            payload = {
                "model": self.model,
                "messages": messages,
                "stream": stream,
                "options": _options(request.sampling, stop),
            }
        else:
            # Explicit template configured: drive the raw completion endpoint so
            # our framing is the one the model actually sees.
            payload = {
                "model": self.model,
                "prompt": request.prompt_text(template),
                "raw": True,
                "stream": stream,
                "options": _options(request.sampling, stop),
            }
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

    async def generate(self, request: GenRequest) -> GenResult:
        url, payload = self._payload(request, stream=False)
        try:
            response = await self.client().post(url, json=payload)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama: {exc}") from exc
        text = self._delta(data)
        return GenResult(
            text=text,
            tokens_in=data.get("prompt_eval_count") or request.estimated_input_tokens(),
            tokens_out=data.get("eval_count") or estimate_tokens(text),
            model=self.model,
            provider=self.name,
            raw=data,
        )

    async def stream(
        self, request: GenRequest, sink: GenResult | None = None
    ) -> AsyncIterator[str]:
        url, payload = self._payload(request, stream=True)
        collected: list[str] = []
        final: dict = {}
        try:
            async with self.client().stream("POST", url, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise ProviderError(f"ollama: {chunk['error']}")
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
                    raw=final,
                ),
                sink,
            )

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
        return "/completion", {
            "prompt": request.prompt_text(template),
            "temperature": sampling.temp,
            "top_p": sampling.top_p,
            "top_k": sampling.top_k,
            "repeat_penalty": sampling.rep_penalty,
            "n_predict": sampling.max_tokens,
            "stop": self.stop_strings(sampling),
            "stream": stream,
        }

    @staticmethod
    def _delta(chunk: dict) -> str:
        return chunk.get("content", "")

    async def stream(
        self, request: GenRequest, sink: GenResult | None = None
    ) -> AsyncIterator[str]:
        url, payload = self._payload(request, stream=True)
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
