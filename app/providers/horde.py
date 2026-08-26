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

from .. import samplers
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

# Field names a `/status/models` row's context size might arrive under —
# undocumented (the endpoint's own schema promises name/count/performance/
# queued/eta, not this), so read defensively rather than committing to one.
_CONTEXT_KEYS = ("max_context_length", "context_length", "max_length")


def _context_field(row: dict) -> int:
    for key in _CONTEXT_KEYS:
        try:
            value = int(row.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return 0


def _wanted_models(config) -> list[str]:
    """The models a request is actually restricted to — `models` is the list
    Horde's own API takes; `model` is the single-name field every other
    backend uses, honoured as shorthand so the settings screen behaves the
    same way for every kind (§ build_payload's own comment)."""
    return [m for m in (config.models or []) if m] or ([config.model] if config.model else [])


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
        # Per (base_url, sorted models) — cleared with the client, same
        # "cannot change without a reload, and asking on every turn would add
        # a round trip to every reply" reasoning as Ollama's _CONTEXT_CACHE
        # (§ providers/ollama.py), just keyed on the models list rather than
        # a single model name since that is what actually selects the worker
        # pool here.
        self._status_cache: list[dict] | None = None

    async def _status_models(self) -> list[dict]:
        """The raw `/status/models` rows, fetched once and reused — both
        `_probe_context` and `list_models_detail` read the same call rather
        than each making their own. Raises on a real failure; it is each
        caller's own business whether that means "nothing extra to go on"
        (`_probe_context`, which has a ceiling to fall back on) or a real
        error to show (`list_models`/`list_models_detail`, discovering
        models *is* the point of that call)."""
        if self._status_cache is not None:
            return self._status_cache
        response = await self.client().get("/status/models", params={"type": "text"})
        response.raise_for_status()
        data = response.json()
        rows = data if isinstance(data, list) else (data.get("models") or []) if isinstance(data, dict) else []
        self._status_cache = [r for r in rows if isinstance(r, dict)]
        return self._status_cache

    async def _probe_context(self) -> int | None:
        """The smallest context any currently-selected model reports, if
        Horde's own model list happens to say — falling back to Horde's own
        API ceiling (a worker may hold more, but the API refuses a request
        that asks for more than this) when it does not.

        Best-effort on purpose: `/status/models` is documented to carry
        `name`/`count`/`performance`/`queued`/`eta`, not a context size, so
        this only ever improves on the flat ceiling if a deployment's
        response happens to carry one of the common field names anyway —
        never a promise, just not thrown away if it is there. The minimum
        across selected models, not the first or the biggest: several
        workers can serve the same model name at different windows, and the
        smallest is the only one that is honest about what every one of
        them can actually hold.
        """
        wanted = _wanted_models(self.config)
        if wanted:
            try:
                rows = await self._status_models()
            except (httpx.HTTPError, ValueError):
                rows = []
            found = [
                size
                for row in rows
                if str(row.get("name") or "") in wanted
                for size in [_context_field(row)]
                if size
            ]
            if found:
                return min(found)
        return LIMITS["max_context_length"][1]

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
        # Only the samplers Horde's schema documents, only when moved off
        # neutral (§17). An undocumented key is not ignored here, it fails the
        # whole request.
        params = samplers.params_for(self.kind, sampling)
        for name, bounds in LIMITS.items():
            if name in params and isinstance(bounds, tuple):
                params[name] = type(params[name])(_clamp(params[name], *bounds))
        # The value actually sent to Horde's queue, distinct from
        # `context_limit()` above: that one only fits the *app's own* prompt
        # budget to what a backend can hold, a different code path that a
        # configured `context` already reached. This is what tells Horde
        # which workers are even eligible to pick the job up — the fewer
        # tokens asked for, the more (smaller-window) workers qualify, which
        # is the actual lever behind "a smaller context answers faster."
        # Configured wins when set; unset falls back to a modest default
        # rather than Horde's full 32000 ceiling, so a backend nobody has
        # tuned does not silently shut out most of the pool.
        context_length = int(self.config.context) or DEFAULT_CONTEXT
        params.update({
            "max_length": int(_clamp(self.cap(sampling), *LIMITS["max_length"])),
            "max_context_length": int(_clamp(context_length, *LIMITS["max_context_length"])),
            "n": 1,
        })
        if stops:
            params["stop_sequence"] = stops

        payload = {"prompt": request.prompt_text(template, self.config.template_spec), "params": params}
        # Horde selects by a models *list*; `model` is the single-model field
        # every other backend uses. Treat one as shorthand for the other so
        # the settings screen behaves the same way for every kind. Left
        # unset rather than raised on here when neither is configured: a
        # caller building a payload just to inspect its other fields (every
        # sampler-clamping test in this suite among them) has no model to
        # give it, and building the payload itself is not the network call —
        # `generate` is where "Horde will reject this outright" actually
        # belongs (§ its own guard, just below).
        wanted = _wanted_models(self.config)
        if wanted:
            payload["models"] = wanted
        return payload

    @staticmethod
    def parse_models_detail(data) -> list[dict]:
        """Every active text model Horde is reporting right now, quickest
        ETA first — the wait a job would actually see, which is a more
        direct answer to "which one will answer fastest" than worker count
        ever was (that was this function's own order before ETA was read at
        all). Ties, and rows with no ETA reported, fall back to worker
        count and then name — stable rather than reshuffling between two
        calls for no visible reason. A context size rides along under
        `context` wherever `_context_field` finds one, undocumented as it
        is (§ _probe_context's own comment) — present when the API happens
        to say, absent rather than guessed at when it does not.
        """
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("models") or []
        else:
            items = []
        rows: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            try:
                count = int(item.get("count") or 0)
            except (TypeError, ValueError):
                count = 0
            try:
                eta = int(item.get("eta"))
            except (TypeError, ValueError):
                eta = None
            try:
                queued = int(item.get("queued") or 0)
            except (TypeError, ValueError):
                queued = 0
            row = {"name": str(name), "count": count, "eta": eta, "queued": queued}
            performance = item.get("performance")
            if performance not in (None, ""):
                row["performance"] = performance
            context = _context_field(item)
            if context:
                row["context"] = context
            rows.append(row)
        # No ETA reported sorts last, not first — "unknown wait" is not "no wait".
        rows.sort(key=lambda r: (r["eta"] if r["eta"] is not None else 10**9, -r["count"], r["name"]))
        return rows

    @classmethod
    def parse_models(cls, data) -> list[str]:
        """Just the names, in the same quickest-ETA-first order."""
        return [row["name"] for row in cls.parse_models_detail(data)]

    async def list_models_detail(self) -> list[dict]:
        try:
            rows = await self._status_models()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"horde: could not list models: {exc}") from exc
        return self.parse_models_detail(rows)

    async def list_models(self) -> list[str]:
        return [row["name"] for row in await self.list_models_detail()]

    async def generate(self, request: GenRequest) -> GenResult:
        # Horde's real API rejects a job with no model named at all rather
        # than picking one on its own — caught here, before a submit that
        # would otherwise 400 with a message about "models" that says
        # nothing about what to actually do differently. §config.py's
        # settings validation catches the same thing earlier, at Save; this
        # is what stands between a config that slipped through some other
        # way (an older settings file, one edited by hand) and a confusing
        # failure deep inside a poll loop instead of a clear one before the
        # first request even goes out.
        if not _wanted_models(self.config):
            raise ProviderError(
                "horde: no model selected — pick at least one on the Backends "
                "tab; Horde does not accept a job with none named"
            )
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
