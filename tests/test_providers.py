"""Provider-specific request shaping (§13)."""

from __future__ import annotations

import pytest

from app.config import BackendConfig, KIND_DEFAULTS, VALID_KINDS, VALID_TEMPLATES
from app.models import Sampling
from app.providers import GenRequest, build
from app.providers.horde import LIMITS, HordeProvider
from app.providers.templates import TEMPLATES, guess_template, render


def horde_provider(**overrides) -> HordeProvider:
    return HordeProvider(BackendConfig(name="h", kind="horde", **overrides))


def params_for(**sampling) -> dict:
    request = GenRequest(
        system="s", messages=[{"role": "user", "content": "hi"}], sampling=Sampling(**sampling)
    )
    return horde_provider().build_payload(request)["params"]


# ------------------------------------------------------------------- horde


def test_horde_clamps_a_too_small_max_length():
    """The connection probe asked for 8 tokens and Horde 400'd — its floor is 16."""
    assert params_for(max_tokens=8)["max_length"] == LIMITS["max_length"][0]


def test_horde_clamps_a_too_large_max_length():
    assert params_for(max_tokens=99999)["max_length"] == LIMITS["max_length"][1]


def test_horde_rejects_zero_temperature_so_it_is_clamped():
    assert params_for(temp=0)["temperature"] >= LIMITS["temperature"][0]


@pytest.mark.parametrize(
    "field,sampling,low,high",
    [
        ("temperature", {"temp": 99}, *LIMITS["temperature"]),
        ("top_p", {"top_p": 0}, *LIMITS["top_p"]),
        ("top_p", {"top_p": 5}, *LIMITS["top_p"]),
        ("top_k", {"top_k": 9999}, *LIMITS["top_k"]),
        ("rep_pen", {"rep_penalty": 0.1}, *LIMITS["rep_pen"]),
        ("rep_pen", {"rep_penalty": 99}, *LIMITS["rep_pen"]),
    ],
)
def test_every_sampler_field_lands_inside_horde_limits(field, sampling, low, high):
    assert low <= params_for(**sampling)[field] <= high


def test_context_length_is_within_limits():
    value = params_for()["max_context_length"]
    assert LIMITS["max_context_length"][0] <= value <= LIMITS["max_context_length"][1]


def test_empty_stop_sequences_are_omitted_rather_than_sent_empty():
    provider = horde_provider(template="plain")
    request = GenRequest(system="s", messages=[], sampling=Sampling(stop=[]))
    params = provider.build_payload(request)["params"]
    assert params.get("stop_sequence") is None or all(params["stop_sequence"])


def test_stop_sequences_are_capped():
    provider = horde_provider(template="chatml")
    request = GenRequest(
        system="s", messages=[], sampling=Sampling(stop=[f"s{i}" for i in range(50)])
    )
    params = provider.build_payload(request)["params"]
    assert len(params["stop_sequence"]) <= LIMITS["stop_sequences"]


def test_horde_never_sends_the_messages_template():
    """Horde is a completion API — 'messages' would be sent as a literal."""
    provider = horde_provider(template="auto")
    payload = provider.build_payload(GenRequest(system="s", messages=[{"role": "user", "content": "x"}]))
    assert isinstance(payload["prompt"], str) and payload["prompt"]


def test_models_are_only_sent_when_configured():
    assert "models" not in horde_provider().build_payload(GenRequest())
    provider = horde_provider(models=["koboldcpp/x", ""])
    assert provider.build_payload(GenRequest())["models"] == ["koboldcpp/x"]


# ---------------------------------------------------------------- defaults


def test_every_kind_has_defaults():
    assert set(KIND_DEFAULTS) == set(VALID_KINDS)


@pytest.mark.parametrize("kind", VALID_KINDS)
def test_kind_defaults_are_valid_and_buildable(kind):
    """A backend created from its defaults must construct without editing."""
    defaults = {k: v for k, v in KIND_DEFAULTS[kind].items() if k != "note"}
    assert defaults["template"] in VALID_TEMPLATES
    provider = build(BackendConfig(name=kind, kind=kind, **defaults))
    assert provider.kind == kind


@pytest.mark.parametrize("kind", VALID_KINDS)
def test_kind_defaults_carry_an_explanation(kind):
    assert KIND_DEFAULTS[kind]["note"]


def test_only_horde_ships_a_default_key_and_it_is_the_anonymous_one():
    for kind, defaults in KIND_DEFAULTS.items():
        key = defaults.get("api_key", "")
        assert key == "" or (kind == "horde" and set(key) == {"0"})


def test_network_kinds_have_a_base_url():
    for kind in ("ollama", "llamacpp", "openai", "horde"):
        assert KIND_DEFAULTS[kind]["base_url"].startswith("http")


# --------------------------------------------------------------- templates


@pytest.mark.parametrize("name", ["chatml", "llama3", "mistral", "plain"])
def test_templates_include_system_and_turns(name):
    out = render(name, "SYSTEM", [{"role": "user", "content": "USER"}])
    assert "SYSTEM" in out and "USER" in out


def test_mistral_folds_system_into_the_first_instruction():
    """Mistral has no system turn; dropping it would silently lose the persona."""
    out = render("mistral", "PERSONA", [{"role": "user", "content": "hello"}])
    assert "PERSONA" in out and out.count("[INST]") == 1


def test_template_guessing_matches_model_families():
    assert guess_template("llama-3.1-8b-instruct") == "llama3"
    assert guess_template("mixtral-8x7b") == "mistral"
    assert guess_template("qwen2.5-3b-instruct") == "chatml"


def test_every_named_template_renders():
    for name in TEMPLATES:
        assert render(name, "s", [{"role": "user", "content": "u"}])
