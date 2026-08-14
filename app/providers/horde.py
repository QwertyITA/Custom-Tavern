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

# Ranges the AI Horde API enforces. Anything outside them is a 400, not a
# clamped value, so we clamp before sending.
LIMITS = {
    "temperature": (0.01, 5.0),
    "top_p": (0.001, 1.0),
    "top_k": (0, 100),
    "rep_pen": (1.0, 3.0),
    "max_length": (16, 512),
    "max_context_length": (80, 32000),
    "stop_sequences": 8,
}

DEFAULT_CONTEXT = 4096


def _clamp(value: float, low: float, high: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, value))


def _reason(response: httpx.Response) -> str:
    """Pull Horde's own explanation out of an error response."""
    try:
        body = response.json()
    except ValueError:
        return f"{response.status_code} {response.text[:200]}"
    message = body.get("message") or body.get("error") or ""
    errors = body.get("errors")
    if isinstance(errors, dict) and errors:
        detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
        message = f"{message} ({detail})" if message else detail
    return f"{response.status_code} {message or response.text[:200]}"


class HordeProvider(Provider):
    kind = "horde"
    native_chat = False

    def __init__(self, config) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        self.context_length = DEFAULT_CONTEXT

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

    def build_payload(self, request: GenRequest) -> dict:
        """Horde validates every sampler field and 400s on anything outside its
        range, so per-pass sampling has to be clamped rather than passed through.

        A cheap pass asking for 8 tokens at temperature 0 is perfectly
        reasonable for Ollama and simply rejected here — that is provider
        knowledge, so it belongs in the provider.
        """
        template = self.template()
        if template == "messages":
            template = "chatml"
        sampling = request.sampling

        stops = [s for s in self.stop_strings(sampling) if s][:LIMITS["stop_sequences"]]
        params = {
            "temperature": _clamp(sampling.temp, *LIMITS["temperature"]),
            "top_p": _clamp(sampling.top_p, *LIMITS["top_p"]),
            "top_k": int(_clamp(sampling.top_k, *LIMITS["top_k"])),
            "rep_pen": _clamp(sampling.rep_penalty, *LIMITS["rep_pen"]),
            "max_length": int(_clamp(sampling.max_tokens, *LIMITS["max_length"])),
            "max_context_length": int(_clamp(self.context_length, *LIMITS["max_context_length"])),
            "n": 1,
        }
        if stops:
            params["stop_sequence"] = stops

        payload = {"prompt": request.prompt_text(template), "params": params}
        # Horde selects by a models *list*; `model` is the single-model field
        # every other backend uses. Treat one as shorthand for the other so the
        # settings screen behaves the same way for every kind.
        wanted = [m for m in (self.config.models or []) if m] or (
            [self.model] if self.model else []
        )
        if wanted:
            payload["models"] = wanted
        return payload

    @staticmethod
    def parse_models(data) -> list[str]:
        """Active text models, busiest first — worker count is availability.

        A model with no workers will sit in the queue indefinitely, so ordering
        by worker count is the difference between a reply and a timeout.
        """
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("models") or []
        else:
            items = []
        ranked = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if name:
                ranked.append((int(item.get("count") or 0), str(name)))
        ranked.sort(key=lambda pair: (-pair[0], pair[1]))
        return [name for _count, name in ranked]

    async def list_models(self) -> list[str]:
        try:
            response = await self.client().get("/status/models", params={"type": "text"})
            response.raise_for_status()
            return self.parse_models(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"horde: could not list models: {exc}") from exc

    async def generate(self, request: GenRequest) -> GenResult:
        payload = self.build_payload(request)
        client = self.client()
        try:
            submit = await client.post("/generate/text/async", json=payload)
            submit.raise_for_status()
            job_id = submit.json()["id"]
        except httpx.HTTPStatusError as exc:
            # Horde says exactly which field it rejected, in the body. Without
            # it the error is just "400 Bad Request", which is unactionable.
            raise ProviderError(f"horde: submit rejected: {_reason(exc.response)}") from exc
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
