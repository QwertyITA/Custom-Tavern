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


def _why(exc: Exception) -> str:
    """A reason, even from an exception that carries no message.

    httpx's timeout and connection errors routinely stringify to nothing at
    all — `str(httpx.ReadTimeout())` is `""` — which is how a real debug
    export came back full of `horde: submit failed: ` and
    `horde: poll failed: ` with nothing after the colon, the one detail that
    would have said which of the two it was.
    """
    text = str(exc).strip()
    return text or exc.__class__.__name__


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
        # Same caching reasoning, for `/status/workers` (§ _worker_caps). A
        # separate slot rather than one shared "status" cache: the two
        # endpoints answer different questions and one being unavailable
        # must not blank the other.
        self._workers_cache: tuple[int, int] | None | str = "unfetched"

    async def _worker_caps(self) -> tuple[int, int] | None:
        """The `(context, reply length)` the most capable worker actually
        serving one of the selected models offers right now — or None when
        Horde cannot be asked, or answers with nothing usable.

        This is the one question that decides whether a job is pickable at
        all, and `/status/models` cannot answer it: it carries name, count,
        performance, queued and eta, and no size of any kind (§
        `_probe_context`, which only ever got lucky when a deployment
        happened to include one anyway). `/status/workers` does carry both,
        per worker, alongside the models that worker serves — so it is the
        only place the real ceiling can be read.

        Taken from a *single* worker, not as the best of each field
        separately: a pool holding one worker at 8k context/256 tokens and
        another at 4k/512 can serve neither 8k/512 nor anything else the
        two only manage between them. Each worker is scored on what it
        could actually do, and the best single one wins — largest context
        first, then largest reply, since context is what a long chat runs
        out of and a request that overshoots it is the one that goes
        unpicked.

        Cached like `_status_models` above: this is a few hundred rows on a
        busy day, and asking once per backend is the difference between one
        download and one per turn.
        """
        if self._workers_cache != "unfetched":
            return self._workers_cache  # type: ignore[return-value]
        self._workers_cache = None
        wanted = set(_wanted_models(self.config))
        if not wanted:
            return None
        try:
            response = await self.client().get("/status/workers", params={"type": "text"})
            response.raise_for_status()
            rows = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(rows, list):
            return None

        best: tuple[int, int] | None = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            # Explicitly off or explicitly in maintenance, only: a worker row
            # that says neither is assumed available, the same way every
            # other field here is read defensively rather than demanded.
            if row.get("online") is False or row.get("maintenance_mode") is True:
                continue
            served = row.get("models")
            if not isinstance(served, list) or not wanted.intersection(
                str(name) for name in served
            ):
                continue
            try:
                context = int(row.get("max_context_length") or 0)
                length = int(row.get("max_length") or 0)
            except (TypeError, ValueError):
                continue
            if context <= 0 or length <= 0:
                continue
            if best is None or (context, length) > best:
                best = (context, length)
        self._workers_cache = best
        return best

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
        """What a real worker can hold, falling back to what the model list
        happens to say, falling back to Horde's own API ceiling (a worker
        may hold more, but the API refuses a request that asks for more
        than this).

        The workers are asked first (§ `_worker_caps`) because they are the
        only ones who actually know — and because the prompt this sizes and
        the job `generate` submits have to agree: a prompt assembled for
        32k that is then submitted asking for 8k is a prompt the worker
        will silently cut the far end off.

        The model list is the older, weaker answer kept underneath it:
        `/status/models` is documented to carry
        `name`/`count`/`performance`/`queued`/`eta`, not a context size, so
        it only ever improves on the flat ceiling if a deployment's
        response happens to carry one of the common field names anyway —
        never a promise, just not thrown away if it is there. The minimum
        across selected models, not the first or the biggest: several
        workers can serve the same model name at different windows, and the
        smallest is the only one that is honest about what every one of
        them can actually hold.
        """
        caps = await self._worker_caps()
        if caps:
            return caps[0]
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
        # Cut to fit an actual worker (§ _worker_caps), whenever what was
        # configured asks for more than the best one online can serve.
        #
        # Horde matches workers against the *requested* `max_context_length`
        # and `max_length` themselves, not against how much of either the job
        # turns out to use — so a context set above every online worker's own
        # window means no worker can ever be matched to the job, no matter
        # how long the timeout runs. From the outside that is indistinguishable
        # from the whole backend being down: the job simply sits there, and
        # every attempt ends in "timed out waiting for a worker". Reported
        # live, from a real debug export: context 32000 (Horde's own API
        # ceiling, which almost no volunteer worker offers) against a 22B
        # model, failing exactly that way every single time.
        #
        # Only ever lowers what was asked for, so the worst this can do when
        # the pool cannot be read at all is nothing: a smaller configured
        # value stays a deliberate choice, and an unreadable or unreported
        # pool leaves the request exactly as it was built.
        caps = await self._worker_caps()
        params = payload["params"]
        context_cap = caps[0] if caps else await self._probe_context()
        if context_cap and context_cap < params["max_context_length"]:
            params["max_context_length"] = int(
                _clamp(context_cap, *LIMITS["max_context_length"])
            )
        if caps and caps[1] and caps[1] < params["max_length"]:
            params["max_length"] = int(_clamp(caps[1], *LIMITS["max_length"]))
        client = self.client()
        try:
            submit = await client.post("/generate/text/async", json=payload)
            submit.raise_for_status()
            job_id = submit.json()["id"]
        except httpx.HTTPStatusError as exc:
            # Horde says exactly which field it rejected, in the body. Without
            # it the error is just "400 Bad Request", which is unactionable.
            raise ProviderError(f"horde: submit rejected: {_reason(exc.response)}") from exc
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ProviderError(f"horde: submit failed: {_why(exc)}") from exc

        deadline = asyncio.get_running_loop().time() + self.config.timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProviderError("horde: timed out waiting for a worker")
            await asyncio.sleep(min(POLL_INTERVAL, remaining))
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise ProviderError("horde: timed out waiting for a worker")
            try:
                # Bounded to whatever is left of the deadline above, not the
                # full configured timeout again on every single poll — reported
                # live as a "stuck" backend: one hung poll request, on its own
                # full timeout, could run past the deadline that was supposed
                # to be the whole wait, and only the *next* iteration ever
                # re-checked it. A floor under it so the last poll before the
                # deadline is not cut down to something too short to complete
                # at all.
                check = await client.get(
                    f"/generate/text/status/{job_id}", timeout=max(remaining, 5.0)
                )
                check.raise_for_status()
                status = check.json()
            except httpx.HTTPError as exc:
                raise ProviderError(f"horde: poll failed: {_why(exc)}") from exc
            except ValueError as exc:
                raise ProviderError(f"horde: poll returned unreadable data: {_why(exc)}") from exc
            if status.get("faulted"):
                raise ProviderError("horde: job faulted")
            # NOT `is_possible` here, on purpose, even though Horde reports
            # it. Tried once, reverted the same day: it reads as "nothing
            # online can ever serve this" but is really just a snapshot of
            # who happens to be connected *right now* — Horde's pool is
            # volunteer workers cycling on and off continuously, so a model
            # with nobody online this second routinely has someone pick it
            # up a minute later, well inside the timeout below. Failing the
            # instant one poll came back false broke exactly the setups this
            # was meant to help: reported live, unchanged settings that had
            # been working, now failing immediately every time, because the
            # snapshot at that one poll happened to catch a quiet moment.
            # Whatever genuinely never resolves still ends in the plain
            # timeout below — slower, but never wrong the way a false
            # "impossible" is.
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
