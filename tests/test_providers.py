"""Provider-specific request shaping (§13)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import BackendConfig, KIND_DEFAULTS, VALID_KINDS, VALID_TEMPLATES
from app.models import Sampling
from app.providers import GenRequest, build
from app.providers.horde import LIMITS, HordeProvider
from app.providers.templates import (
    CUSTOM_FIELDS,
    CUSTOM_KEYS,
    CUSTOM_PRESETS,
    TEMPLATES,
    custom_fields,
    custom_presets,
    custom_spec,
    guess_template,
    render,
    stop_for,
)
from tests.conftest import sync


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


# ------------------------------------------------------- custom template


SAMPLE = [
    {"role": "user", "content": "U1"},
    {"role": "assistant", "content": "A1"},
    {"role": "user", "content": "U2"},
]


@pytest.mark.parametrize("name", ["chatml", "llama3", "plain"])
def test_a_preset_reproduces_its_named_template_exactly(name):
    """The presets are the point: they prove the eight boxes are enough to
    express a real template, so filling them in is not a lesser path."""
    assert render("custom", "SYS", SAMPLE, "pre", CUSTOM_PRESETS[name]) == render(
        name, "SYS", SAMPLE, "pre"
    )


def test_the_mistral_preset_is_the_unfolded_variant():
    """Built-in `mistral` folds the system prompt into the first instruction —
    a rule, not a pair of strings, so the boxes cannot say it. The preset is
    the other common shape, and the difference is deliberate."""
    out = render("custom", "SYS", SAMPLE, spec=CUSTOM_PRESETS["mistral"])
    assert out.startswith("<s>[INST] SYS [/INST]")
    assert out != render("mistral", "SYS", SAMPLE)


def test_custom_stops_come_from_the_boxes():
    stops = stop_for("custom", CUSTOM_PRESETS["chatml"])
    assert "<|im_end|>" in stops
    assert any(s.startswith("<|im_start|>") for s in stops)


def test_custom_stops_are_stripped_of_layout_whitespace():
    """`<|im_end|>\\n` as a stop string waits for a newline the model may never
    emit, so the reply runs on past the marker that was supposed to end it."""
    assert all(s == s.strip() for s in stop_for("custom", CUSTOM_PRESETS["chatml"]))
    assert "[/INST]" in stop_for("custom", CUSTOM_PRESETS["mistral"])


def test_custom_stops_drop_empty_boxes():
    spec = dict(CUSTOM_PRESETS["plain"])
    assert "" not in stop_for("custom", spec)


def test_a_half_filled_spec_still_renders():
    """Someone will type into two boxes and hit send. It has to produce a
    prompt, not a KeyError."""
    out = render("custom", "SYS", SAMPLE, spec={"user_prefix": "### "})
    assert "SYS" in out and "U1" in out and "A1" in out


@pytest.mark.parametrize("junk", [None, {}, [], "nonsense", 3])
def test_custom_spec_survives_junk(junk):
    spec = custom_spec(junk)
    assert set(spec) == set(CUSTOM_KEYS)
    assert all(value == "" for value in spec.values())


def test_every_preset_fills_every_box():
    """A preset that omits a key would silently clear that box when picked."""
    for name, spec in CUSTOM_PRESETS.items():
        assert set(spec) == set(CUSTOM_KEYS), name


def test_the_field_list_matches_the_keys():
    """The editor draws itself from CUSTOM_FIELDS; a key with no field would be
    invisible and unfillable."""
    assert tuple(f["key"] for f in custom_fields()) == CUSTOM_KEYS
    assert all(f["label"] and f["hint"] for f in custom_fields())


def test_presets_and_fields_are_handed_out_as_copies():
    """They are module constants; the API serialises them every request."""
    custom_presets()["chatml"]["reply_start"] = "clobbered"
    custom_fields()[0]["label"] = "clobbered"
    assert CUSTOM_PRESETS["chatml"]["reply_start"] != "clobbered"
    assert CUSTOM_FIELDS[0]["label"] != "clobbered"


def test_a_backend_on_the_custom_template_stops_on_its_own_markers():
    """The whole path: config -> provider.stop_strings -> the boxes."""
    provider = horde_provider(template="custom", template_spec=CUSTOM_PRESETS["llama3"])
    assert "<|eot_id|>" in provider.stop_strings(Sampling())


def test_a_backend_renders_through_its_own_spec():
    provider = horde_provider(template="custom", template_spec=CUSTOM_PRESETS["chatml"])
    request = GenRequest(system="SYS", messages=[{"role": "user", "content": "hi"}])
    assert request.prompt_text(provider.template(), provider.config.template_spec) == render(
        "chatml", "SYS", [{"role": "user", "content": "hi"}]
    )


def test_an_unknown_spec_key_is_dropped_rather_than_rendered():
    """Old settings files and hand-edited JSON both arrive here."""
    spec = {**CUSTOM_PRESETS["plain"], "nonsense": "SHOULD NOT APPEAR"}
    assert "SHOULD NOT APPEAR" not in render("custom", "SYS", SAMPLE, spec=spec)


def test_custom_is_a_selectable_template():
    assert "custom" in VALID_TEMPLATES


# ------------------------------------------------------- model discovery


def test_ollama_parses_its_tag_list():
    data = {"models": [{"name": "llama3.1:8b"}, {"name": "qwen2.5:3b"}, {"name": "llama3.1:8b"}]}
    from app.providers.ollama import OllamaProvider

    assert OllamaProvider.parse_models(data) == ["llama3.1:8b", "qwen2.5:3b"]


def test_openai_parses_its_model_list():
    from app.providers.openai_compat import OpenAICompatProvider

    data = {"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}]}
    assert OpenAICompatProvider.parse_models(data) == ["gpt-4o", "gpt-4o-mini"]


def test_horde_ranks_models_by_worker_count():
    """A model with no workers queues forever, so availability is the order."""
    data = [
        {"name": "quiet/model", "count": 0},
        {"name": "busy/model", "count": 12},
        {"name": "some/model", "count": 3},
    ]
    assert HordeProvider.parse_models(data) == ["busy/model", "some/model", "quiet/model"]


@pytest.mark.parametrize("junk", [None, {}, {"models": None}, {"data": None}, [], "nonsense"])
def test_model_parsers_survive_junk(junk):
    from app.providers.ollama import LlamaCppProvider, OllamaProvider
    from app.providers.openai_compat import OpenAICompatProvider

    for parser in (
        OllamaProvider.parse_models,
        LlamaCppProvider.parse_models,
        OpenAICompatProvider.parse_models,
        HordeProvider.parse_models,
    ):
        try:
            assert isinstance(parser(junk), list)
        except (AttributeError, TypeError):
            pytest.fail(f"{parser.__qualname__} crashed on {junk!r}")


def test_horde_uses_the_model_field_when_no_models_list_is_set():
    """`model` and `models` are the same choice; the GUI only edits one."""
    provider = horde_provider(model="koboldcpp/Mistral-7B")
    assert provider.build_payload(GenRequest())["models"] == ["koboldcpp/Mistral-7B"]


def test_an_explicit_models_list_wins_over_the_model_field():
    provider = horde_provider(model="ignored", models=["a", "b"])
    assert provider.build_payload(GenRequest())["models"] == ["a", "b"]


# --------------------------------------------------- ollama: reasoning models
#
# Reported from a real run: a thinking model on Ollama answered every turn with
# "the model returned nothing at all". Nothing was broken — Ollama had parsed
# the reasoning onto `message.thinking`, the reply pass's whole token budget
# went into it, and `message.content` came back empty over a 200.


def ollama_provider(**overrides):
    from app.providers.ollama import OllamaProvider

    return OllamaProvider(BackendConfig(name="o", kind="ollama", model="glm4:latest", **overrides))


def ollama_payload(**overrides) -> dict:
    request = GenRequest(messages=[{"role": "user", "content": "hi"}])
    return ollama_provider(**overrides)._payload(request, stream=False)[1]


def wired(handler, **overrides):
    """A provider whose HTTP goes to `handler` instead of a machine."""
    provider = ollama_provider(**overrides)
    provider._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ollama.test"
    )
    return provider


def ndjson(rows: list[dict]):
    async def body():
        for row in rows:
            yield (json.dumps(row) + "\n").encode()

    return body()


def test_ollama_asks_for_no_thinking_by_default():
    """The default has to be the one that answers. A reasoning model left to
    think spends the reply budget on reasoning and returns an empty turn."""
    assert ollama_payload()["think"] is False


def test_auto_keeps_the_field_off_the_wire_entirely():
    """"Whatever the model does by default" means saying nothing, not saying
    false — an Ollama old enough to have no switch must still work."""
    assert "think" not in ollama_payload(think="auto")


def test_thinking_can_be_asked_for():
    assert ollama_payload(think="on")["think"] is True


def test_the_reasoning_is_captured_and_never_streamed():
    """It is not what the character said (§5.6), but losing it makes "it only
    reasoned" indistinguishable from "it returned nothing"."""
    from app.providers import GenResult

    rows = [
        {"message": {"role": "assistant", "thinking": "she would ", "content": ""}},
        {"message": {"role": "assistant", "thinking": "be curt", "content": ""}},
        {"message": {"role": "assistant", "content": '"Sit."'}},
        {"message": {"content": ""}, "done": True, "eval_count": 7, "prompt_eval_count": 3},
    ]
    provider = wired(lambda request: httpx.Response(200, content=ndjson(rows)))
    sink = GenResult()

    async def run():
        return [delta async for delta in provider.stream(GenRequest(), sink)]

    assert "".join(sync(run())) == '"Sit."'
    assert sink.thinking == "she would be curt"
    assert sink.tokens_out == 7


def test_a_reply_that_is_all_reasoning_arrives_as_reasoning_and_not_as_silence():
    from app.providers import GenResult

    rows = [
        {"message": {"thinking": "thinking about it", "content": ""}},
        {"message": {"content": ""}, "done": True},
    ]
    provider = wired(lambda request: httpx.Response(200, content=ndjson(rows)))
    sink = GenResult()

    async def run():
        return [delta async for delta in provider.stream(GenRequest(), sink)]

    assert sync(run()) == []
    assert sink.thinking == "thinking about it"


def test_generate_captures_the_reasoning_too():
    provider = wired(
        lambda request: httpx.Response(
            200, json={"message": {"thinking": "hmm", "content": "hi"}, "eval_count": 2}
        )
    )
    result = sync(provider.generate(GenRequest()))
    assert (result.text, result.thinking) == ("hi", "hmm")


def test_an_ollama_that_rejects_the_think_field_is_retried_without_it():
    """Older Ollamas, and some builds asked about a model with no reasoning at
    all, 400 the field itself. The request is fine without it."""
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if "think" in payload:
            return httpx.Response(400, json={"error": 'model does not support "think"'})
        return httpx.Response(200, json={"message": {"content": "hi"}})

    provider = wired(handler)
    assert sync(provider.generate(GenRequest())).text == "hi"
    assert len(seen) == 2 and "think" not in seen[1]

    # And the discovery is paid for once, not on every turn.
    sync(provider.generate(GenRequest()))
    assert len(seen) == 3 and "think" not in seen[2]


def test_the_streamed_path_retries_without_think_as_well():
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if "think" in payload:
            return httpx.Response(400, json={"error": 'unknown field "think"'})
        return httpx.Response(200, content=ndjson([{"message": {"content": "hi"}, "done": True}]))

    provider = wired(handler)

    async def run():
        return [delta async for delta in provider.stream(GenRequest())]

    assert sync(run()) == ["hi"]
    assert len(seen) == 2


def test_any_other_error_is_still_an_error():
    """The retry is for one specific rejection. Swallowing the rest would turn
    a wrong model name into an empty reply with no explanation."""
    from app.providers.base import ProviderError

    provider = wired(lambda request: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ProviderError):
        sync(provider.generate(GenRequest()))


def test_thinking_is_a_backend_setting_the_gui_can_offer():
    from app.config import VALID_THINK

    assert set(VALID_THINK) == {"off", "auto", "on"}
    assert KIND_DEFAULTS["ollama"]["think"] == "off"
