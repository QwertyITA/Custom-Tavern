"""OpenAI-compatible chat completions — the blocking-fast fallback (§18.2).

Covers anything speaking /v1/chat/completions: hosted APIs, LM Studio,
text-generation-webui's OpenAI extension, vLLM.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx

from .. import samplers
from .base import GenRequest, GenResult, Provider, ProviderError, _copy_into, estimate_tokens


class OpenAICompatProvider(Provider):
    kind = "openai"
    native_chat = True

    def __init__(self, config) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            base = self.config.base_url or "https://api.openai.com/v1"
            headers = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            self._client = httpx.AsyncClient(
                base_url=base.rstrip("/"), headers=headers, timeout=self.config.timeout
            )
        return self._client

    def _payload(self, request: GenRequest, stream: bool) -> dict:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(request.messages)
        if request.prefill:
            messages.append({"role": "assistant", "content": request.prefill})
        sampling = request.sampling
        # The official parameter set only (§17). Plenty of local servers wearing
        # this API accept top_k and min_p, but sending them to the real one is a
        # 400 — and a backend failing on a setting you cannot see is the worst
        # version of this.
        payload = {
            "model": self.model,
            "messages": messages,
            **samplers.params_for(self.kind, sampling),
            "max_tokens": sampling.max_tokens,
            "stream": stream,
        }
        stop = self.stop_strings(sampling)
        if stop:
            payload["stop"] = stop[:4]  # the API caps stop sequences at four
        if request.expects_json:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def generate(self, request: GenRequest) -> GenResult:
        try:
            response = await self.client().post(
                "/chat/completions", json=self._payload(request, stream=False)
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"openai: {exc}") from exc
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"openai: unexpected response shape: {data}") from exc
        usage = data.get("usage") or {}
        return GenResult(
            text=text,
            tokens_in=usage.get("prompt_tokens") or request.estimated_input_tokens(),
            tokens_out=usage.get("completion_tokens") or estimate_tokens(text),
            model=data.get("model", self.model),
            provider=self.name,
            raw=data,
        )

    async def stream(
        self, request: GenRequest, sink: GenResult | None = None
    ) -> AsyncIterator[str]:
        collected: list[str] = []
        usage: dict = {}
        try:
            async with self.client().stream(
                "POST", "/chat/completions", json=self._payload(request, stream=True)
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        chunk = json.loads(body)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    if delta:
                        collected.append(delta)
                        yield delta
        except httpx.HTTPError as exc:
            raise ProviderError(f"openai: {exc}") from exc

        if sink is not None:
            text = "".join(collected)
            _copy_into(
                GenResult(
                    text=text,
                    tokens_in=usage.get("prompt_tokens") or request.estimated_input_tokens(),
                    tokens_out=usage.get("completion_tokens") or estimate_tokens(text),
                    model=self.model,
                    provider=self.name,
                ),
                sink,
            )

    @staticmethod
    def parse_models(data) -> list[str]:
        rows = (data.get("data") if isinstance(data, dict) else None) or []
        out = [str(m["id"]) for m in rows if isinstance(m, dict) and m.get("id")]
        return sorted(set(out))

    async def list_models(self) -> list[str]:
        try:
            response = await self.client().get("/models")
            response.raise_for_status()
            return self.parse_models(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"openai: could not list models: {exc}") from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
