"""AI Horde — the background-free tier (§3).

Network-only, so it is the one tier that survives the screen going off: nothing
is computed on the phone. It is also slow and queue-bound, which is exactly why
it is reserved for latency-tolerant background passes and never for the reply.

Submit → poll → collect. Polling is deliberately unhurried; a background pass
landing a minute late is fine under eventual consistency (§1).
"""

from __future__ import annotations

import asyncio

import httpx

from .base import GenRequest, GenResult, Provider, ProviderError, estimate_tokens

ANON_KEY = "0000000000"
POLL_INTERVAL = 4.0


class HordeProvider(Provider):
    kind = "horde"
    native_chat = False

    def __init__(self, config) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            base = self.config.base_url or "https://aihorde.net/api/v2"
            self._client = httpx.AsyncClient(
                base_url=base.rstrip("/"),
                headers={
                    "apikey": self.config.api_key or ANON_KEY,
                    "Client-Agent": "personal-tavern:0.1:local",
                },
                timeout=self.config.timeout,
            )
        return self._client

    async def generate(self, request: GenRequest) -> GenResult:
        template = self.template()
        if template == "messages":
            template = "chatml"
        sampling = request.sampling
        payload = {
            "prompt": request.prompt_text(template),
            "params": {
                "temperature": sampling.temp,
                "top_p": sampling.top_p,
                "top_k": sampling.top_k,
                "rep_pen": sampling.rep_penalty,
                "max_length": min(sampling.max_tokens, 512),
                "max_context_length": 4096,
                "stop_sequence": self.stop_strings(sampling),
            },
            "trusted_workers": False,
        }
        if self.config.models:
            payload["models"] = self.config.models

        client = self.client()
        try:
            submit = await client.post("/generate/text/async", json=payload)
            submit.raise_for_status()
            job_id = submit.json()["id"]
        except (httpx.HTTPError, KeyError) as exc:
            raise ProviderError(f"horde: submit failed: {exc}") from exc

        deadline = asyncio.get_running_loop().time() + self.config.timeout
        while True:
            if asyncio.get_running_loop().time() > deadline:
                raise ProviderError("horde: timed out waiting for a worker")
            await asyncio.sleep(POLL_INTERVAL)
            try:
                check = await client.get(f"/generate/text/status/{job_id}")
                check.raise_for_status()
                status = check.json()
            except httpx.HTTPError as exc:
                raise ProviderError(f"horde: poll failed: {exc}") from exc
            if status.get("faulted"):
                raise ProviderError("horde: job faulted")
            if status.get("done"):
                break

        generations = status.get("generations") or []
        if not generations:
            raise ProviderError("horde: job completed with no generations")
        text = generations[0].get("text", "")
        return GenResult(
            text=text,
            tokens_in=request.estimated_input_tokens(),
            tokens_out=estimate_tokens(text),
            model=generations[0].get("model", self.model or "horde"),
            provider=self.name,
            raw=status,
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
