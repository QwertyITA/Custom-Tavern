"""Fitting the prompt and the reply into the window the backend actually has.

Prompt and reply share one window. Asking for 32k of context and 5000 tokens of
reply from a model serving 8k does not get you either: the far end of the
prompt is dropped inside the backend, silently, and the first anyone knows of
it is a character who has forgotten the last hour.

So the backend is asked what it can serve, and the budget is fitted to the
answer — reply first, because the answer is the thing being paid for.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import BackendConfig, Settings
from app.models import PassDef, Sampling
from app.passes.scheduler import CONTEXT_SAFETY, MIN_CONTEXT, PassScheduler
from app.providers import GenRequest
from app.providers.ollama import _CONTEXT_CACHE, LlamaCppProvider, OllamaProvider

from .conftest import sync


@pytest.fixture(autouse=True)
def clean_cache():
    _CONTEXT_CACHE.clear()
    yield
    _CONTEXT_CACHE.clear()


def wired(handler, kind=OllamaProvider, **overrides):
    provider = kind(BackendConfig(name="o", kind=kind.kind, model="glm4:latest", **overrides))
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ollama.test"
    )
    return provider


# --------------------------------------------------------------- asking


def test_a_loaded_model_reports_what_it_was_loaded_with():
    """`/api/ps` is the number that matters: Ollama sizes the window from VRAM
    and it is routinely smaller than the model's own maximum."""
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"name": "glm4:latest", "context_length": 32768}
            ]})
        return httpx.Response(404, json={})

    assert sync(wired(handler).context_limit()) == 32768


def test_a_model_that_is_not_loaded_falls_back_to_its_own_maximum():
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"model_info": {
                "general.architecture": "deepseek2",
                "deepseek2.context_length": 202752,
            }})
        return httpx.Response(404, json={})

    assert sync(wired(handler).context_limit()) == 202752


def test_a_backend_that_cannot_say_says_nothing():
    """None is not zero: it means keep the configured budget."""
    assert sync(wired(lambda r: httpx.Response(404, json={})).context_limit()) is None


def test_it_is_asked_once():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"model": "glm4:latest", "context_length": 8192}
            ]})
        return httpx.Response(404, json={})

    provider = wired(handler)
    assert sync(provider.context_limit()) == 8192
    assert sync(provider.context_limit()) == 8192
    assert calls.count("/api/ps") == 1


def test_llama_cpp_reads_its_own_props():
    def handler(request):
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 4096}})
        return httpx.Response(404, json={})

    assert sync(wired(handler, kind=LlamaCppProvider, template="chatml").context_limit()) == 4096


def test_horde_knows_its_own_ceiling():
    from app.providers.horde import LIMITS, HordeProvider

    provider = HordeProvider(BackendConfig(name="h", kind="horde"))
    assert sync(provider.context_limit()) == LIMITS["max_context_length"][1]


# ------------------------------------------------------------- clamping


def small_window(limit: int):
    def handler(request):
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": [
                {"model": "glm4:latest", "context_length": limit}
            ]})
        return httpx.Response(200, json={"message": {"content": "hi"}})

    return wired(handler)


def payload_for(provider, request):
    return sync(_payload(provider, request))


async def _payload(provider, request):
    return provider._payload(request, stream=False, limit=await provider.context_limit())[1]


def test_the_reply_is_cut_to_what_is_left_of_the_window():
    provider = small_window(4096)
    request = GenRequest(
        system="x" * 8000,  # ~2000 tokens
        sampling=Sampling(max_tokens=5000),
        think=False,
    )
    predict = payload_for(provider, request)["options"]["num_predict"]
    assert predict < 5000
    assert predict <= 4096 - 2000


def test_a_window_with_room_to_spare_changes_nothing():
    provider = small_window(200000)
    request = GenRequest(system="hello", sampling=Sampling(max_tokens=5000), think=False)
    assert payload_for(provider, request)["options"]["num_predict"] == 5000


def test_a_full_window_still_asks_for_a_sentence():
    """Cut to nothing is worse than cut short: both end mid-word, and only one
    of them says anything first."""
    provider = small_window(2048)
    request = GenRequest(system="x" * 40000, sampling=Sampling(max_tokens=5000), think=False)
    assert payload_for(provider, request)["options"]["num_predict"] >= 256


# ------------------------------------------------- the budget, before assembly


class Sized:
    """A backend with a window and nothing else."""

    name = "sized"
    model = "sized-1"
    sees_images = False

    def __init__(self, limit):
        self.limit = limit

    async def context_limit(self):
        return self.limit


def reply_pass(max_tokens=5000):
    return PassDef(id="basic", kind="canonical", label="Reply",
                   sampling=Sampling(max_tokens=max_tokens))


def test_the_context_budget_is_fitted_to_the_window(db):
    sched = PassScheduler(db, Settings(token_budget=32768))
    fitted = sync(sched._fitted(Sized(8192), reply_pass()))
    assert fitted.token_budget == 8192 - 5000 - CONTEXT_SAFETY


def test_a_backend_with_a_bigger_window_keeps_the_configured_budget(db):
    sched = PassScheduler(db, Settings(token_budget=32768))
    fitted = sync(sched._fitted(Sized(200000), reply_pass()))
    assert fitted.token_budget == 32768


def test_a_backend_that_will_not_say_keeps_the_configured_budget(db):
    """Which is what this did before it could ask at all."""
    sched = PassScheduler(db, Settings(token_budget=32768))
    assert sync(sched._fitted(Sized(None), reply_pass())).token_budget == 32768


def test_a_window_smaller_than_the_reply_still_leaves_a_prompt(db):
    sched = PassScheduler(db, Settings(token_budget=32768))
    fitted = sync(sched._fitted(Sized(4096), reply_pass(max_tokens=5000)))
    assert fitted.token_budget == MIN_CONTEXT


def test_a_backend_that_raises_does_not_take_the_turn_with_it(db):
    class Broken(Sized):
        async def context_limit(self):
            raise RuntimeError("no")

    sched = PassScheduler(db, Settings(token_budget=4096))
    assert sync(sched._fitted(Broken(None), reply_pass())).token_budget == 4096


def test_the_settings_object_itself_is_left_alone(db):
    """Fitted per turn, per backend. Writing the smaller number back would
    shrink the configured budget permanently the first time a small model
    answered one message."""
    settings = Settings(token_budget=32768)
    sched = PassScheduler(db, settings)
    sync(sched._fitted(Sized(8192), reply_pass()))
    assert settings.token_budget == 32768
    assert sched.settings.token_budget == 32768


def test_the_reply_ships_asking_for_five_thousand():
    from app.passes.registry import CANONICAL_PASSES

    reply = next(p for p in CANONICAL_PASSES if p.id == "basic")
    assert reply.sampling.max_tokens == 5000
