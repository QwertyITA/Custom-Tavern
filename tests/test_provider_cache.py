"""The shared provider cache (§ providers/__init__.py) must outlive any one
caller. `get_provider`/`provider_for_tier` hand back one instance per backend
name specifically so its httpx client — and the connection pool underneath —
is reused across every pass and every turn, per the module's own docstring.

Two callers broke that contract: `main._effective_blocking_budget` (behind
`GET /api/characters/budget`, which the roster calls on nothing rarer than
the app loading) and `card_compression.preview` (behind
`POST /api/characters/{id}/compress`) both borrowed the shared blocking-tier
provider to ask it a question, then closed it in a `finally` on the way out.
The cache does not rebuild a closed instance — `get_provider` only replaces
one whose *config* changed — so the next real turn on that tier failed with
`RuntimeError("Cannot send a request, as the client has been closed.")`, and
kept failing until the process restarted. `echo` cannot reproduce this: it
has no real client for `aclose()` to do anything to (§ test_api.py's own
end-to-end version of these two tests, which only proves the route is
harmless in the app's default configuration). This uses `ollama` instead,
wired with a mock transport that has something to actually close.
"""

from __future__ import annotations

import httpx
import pytest

from app import card_compression, main, providers
from app.config import BackendConfig, Settings
from app.models import Character

from .conftest import sync


@pytest.fixture(autouse=True)
def clean_provider_cache():
    providers._cache.clear()
    yield
    providers._cache.clear()


def _wired_ollama(name="o") -> providers.base.Provider:
    """A provider whose client can report `.is_closed` truthfully, planted
    directly in the shared cache under `name` — the same place
    `get_provider` would have built and stored it itself."""
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"model": "glm4:latest", "context_length": 8192}
            ]})
        if request.url.path in ("/api/chat", "/api/generate"):
            return httpx.Response(200, json={
                "message": {"content": "Shorter now."},
                "response": "Shorter now.",
                "done": True,
                "prompt_eval_count": 10,
                "eval_count": 4,
            })
        return httpx.Response(404, json={})

    provider = providers.OllamaProvider(BackendConfig(name=name, kind="ollama", model="glm4:latest"))
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ollama.test"
    )
    providers._cache[name] = provider
    return provider


def _settings() -> Settings:
    return Settings(
        backends=[BackendConfig(name="o", kind="ollama", model="glm4:latest")],
        tiers={"blocking": "o", "foreground": "o", "background": "o"},
    )


def test_the_budget_check_leaves_the_shared_client_open(db):
    provider = _wired_ollama()
    settings = _settings()

    # The exact number is `assembly.fit_token_budget`'s arithmetic, not this
    # test's concern — only that the backend was actually asked (not None,
    # meaning "no answer") and that asking again lands on the same fit.
    limit = sync(main._effective_blocking_budget(db, settings))
    assert limit is not None
    assert not provider._client.is_closed
    # The real proof: the cache still hands back this exact instance, and it
    # can still actually be asked something — a `finally: aclose()` regressing
    # would fail this second call with "client has been closed", not merely
    # leave a stale flag somewhere unread.
    assert providers.get_provider("o", settings) is provider
    assert sync(main._effective_blocking_budget(db, settings)) == limit


def test_the_compress_preview_leaves_the_shared_client_open():
    provider = _wired_ollama()
    settings = _settings()
    character = Character(
        id="verbose", name="Verbose",
        persona="A very long persona. " * 300,
        scenario="A very long scenario. " * 300,
        first_mes="Hi.",
    )

    result = sync(card_compression.preview(settings, character, reduce_by=500))
    assert result["changed"] is True
    assert not provider._client.is_closed
    # Same instance, still answering — a second preview would raise on a
    # closed client if the bug were still there.
    result2 = sync(card_compression.preview(settings, character, reduce_by=500))
    assert result2["changed"] is True
