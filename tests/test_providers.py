"""Provider-specific request shaping (§13)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import BackendConfig, KIND_DEFAULTS, VALID_KINDS, VALID_TEMPLATES
from app.models import Sampling
from app.providers import GenRequest, ProviderError, build
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


def _max_context(**overrides) -> int:
    provider = horde_provider(**overrides)
    request = GenRequest(system="s", messages=[{"role": "user", "content": "hi"}])
    return provider.build_payload(request)["params"]["max_context_length"]


def test_a_configured_context_reaches_the_actual_request():
    """This is the value that decides which workers are even eligible to pick
    the job up — distinct from context_limit(), which only fits the app's own
    prompt budget and already honoured a configured context. This one used to
    stay hardcoded regardless of it."""
    assert _max_context(context=4096) == 4096


def test_an_unconfigured_context_falls_back_to_the_safe_default():
    from app.providers.horde import DEFAULT_CONTEXT

    assert _max_context(context=0) == DEFAULT_CONTEXT


def test_a_configured_context_past_the_ceiling_still_clamps():
    assert _max_context(context=999999) == LIMITS["max_context_length"][1]


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


def test_horde_ranks_models_by_eta():
    """The estimated wait a job would actually see, not a proxy for it."""
    data = [
        {"name": "slow/model", "count": 12, "eta": 120},
        {"name": "quick/model", "count": 1, "eta": 5},
        {"name": "medium/model", "count": 3, "eta": 40},
    ]
    assert HordeProvider.parse_models(data) == ["quick/model", "medium/model", "slow/model"]


def test_horde_falls_back_to_worker_count_with_no_eta_reported():
    """A model with no workers queues forever, so availability is the order —
    unchanged from before ETA was read at all, for the case there is none."""
    data = [
        {"name": "quiet/model", "count": 0},
        {"name": "busy/model", "count": 12},
        {"name": "some/model", "count": 3},
    ]
    assert HordeProvider.parse_models(data) == ["busy/model", "some/model", "quiet/model"]


def test_horde_a_missing_eta_sorts_after_a_reported_one():
    """Unknown wait is not the same claim as no wait."""
    data = [
        {"name": "no-eta/model", "count": 50},
        {"name": "has-eta/model", "count": 1, "eta": 999},
    ]
    assert HordeProvider.parse_models(data) == ["has-eta/model", "no-eta/model"]


def test_horde_model_detail_carries_eta_and_context_when_reported():
    data = [{"name": "m", "count": 2, "eta": 7, "queued": 500,
             "performance": "12.3", "max_context_length": 8192}]
    [row] = HordeProvider.parse_models_detail(data)
    assert row == {"name": "m", "count": 2, "eta": 7, "queued": 500,
                    "performance": "12.3", "context": 8192}


def test_horde_model_detail_omits_context_when_not_reported():
    """Undocumented field, so absence is not filled in with a guess."""
    data = [{"name": "m", "count": 2, "eta": 7}]
    [row] = HordeProvider.parse_models_detail(data)
    assert "context" not in row


def test_horde_refuses_to_generate_with_no_model_selected():
    provider = horde_provider()
    with pytest.raises(ProviderError, match="no model selected"):
        sync(provider.generate(GenRequest()))


def test_horde_build_payload_stays_usable_with_no_model_selected():
    """The payload itself is not the network call — every sampler-clamping
    test above builds one with no model configured at all, and none of them
    are testing model selection."""
    provider = horde_provider()
    payload = provider.build_payload(GenRequest())
    assert "models" not in payload


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


def test_ollama_leaves_thinking_to_the_model_by_default():
    """`auto` sends nothing: the model's own template decides. Safe as a
    default now that a reply which never gets past its reasoning is recovered
    rather than reported (§5.6)."""
    assert "think" not in ollama_payload()


def test_off_asks_the_model_not_to_reason():
    assert ollama_payload(think="off")["think"] is False


def test_thinking_can_be_asked_for():
    assert ollama_payload(think="on")["think"] is True


def test_the_reasoning_is_captured_and_kept_apart_from_the_reply():
    """It is not what the character said (§5.6), but losing it makes "it only
    reasoned" indistinguishable from "it returned nothing".

    It does reach the caller — a reasoning model emits no visible token until
    it stops thinking, so the client has nothing to show otherwise — but as
    `ReasoningDelta`, which no consumer can mistake for reply text.
    """
    from app.providers import GenResult, ReasoningDelta

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

    deltas = sync(run())
    reply = [d for d in deltas if not isinstance(d, ReasoningDelta)]
    thought = [d for d in deltas if isinstance(d, ReasoningDelta)]
    assert "".join(reply) == '"Sit."'
    assert "".join(thought) == "she would be curt"
    assert sink.thinking == "she would be curt"
    assert sink.tokens_out == 7


def test_a_reply_that_is_all_reasoning_arrives_as_reasoning_and_not_as_silence():
    from app.providers import GenResult, ReasoningDelta

    rows = [
        {"message": {"thinking": "thinking about it", "content": ""}},
        {"message": {"content": ""}, "done": True},
    ]
    provider = wired(lambda request: httpx.Response(200, content=ndjson(rows)))
    sink = GenResult()

    async def run():
        return [delta async for delta in provider.stream(GenRequest(), sink)]

    deltas = sync(run())
    assert [d for d in deltas if not isinstance(d, ReasoningDelta)] == []
    assert [str(d) for d in deltas] == ["thinking about it"]
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
        # The context probe goes to its own endpoints and is not part of this.
        if not request.url.path.endswith(("/api/chat", "/api/generate")):
            return httpx.Response(404, json={})
        payload = json.loads(request.content)
        seen.append(payload)
        if "think" in payload:
            return httpx.Response(400, json={"error": 'model does not support "think"'})
        return httpx.Response(200, json={"message": {"content": "hi"}})

    provider = wired(handler, think="off")
    assert sync(provider.generate(GenRequest())).text == "hi"
    assert len(seen) == 2 and "think" not in seen[1]

    # And the discovery is paid for once, not on every turn.
    sync(provider.generate(GenRequest()))
    assert len(seen) == 3 and "think" not in seen[2]


def test_the_streamed_path_retries_without_think_as_well():
    seen = []

    def handler(request):
        if not request.url.path.endswith(("/api/chat", "/api/generate")):
            return httpx.Response(404, json={})
        payload = json.loads(request.content)
        seen.append(payload)
        if "think" in payload:
            return httpx.Response(400, json={"error": 'unknown field "think"'})
        return httpx.Response(200, content=ndjson([{"message": {"content": "hi"}, "done": True}]))

    provider = wired(handler, think="off")

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


def test_a_thinking_model_gets_room_to_think_on_top_of_the_reply():
    """`max_tokens` is how long the answer may be. Ollama counts reasoning
    against the same number, so a model that thinks spends the answer's budget
    working out what the answer is — reported with the numbers: a thousand
    tokens of reasoning and no reply at all."""
    request = GenRequest(messages=[{"role": "user", "content": "hi"}],
                         sampling=Sampling(max_tokens=1000))
    thinking = ollama_provider(think="on")._payload(request, stream=False)[1]
    assert thinking["options"]["num_predict"] == 2000


def test_the_headroom_is_capped():
    request = GenRequest(sampling=Sampling(max_tokens=4000))
    payload = ollama_provider(think="on")._payload(request, stream=False)[1]
    assert payload["options"]["num_predict"] == 5200


def test_auto_gets_the_headroom_too():
    """"The model's template decides" means a reasoning model will."""
    request = GenRequest(sampling=Sampling(max_tokens=500))
    assert ollama_provider()._payload(request, stream=False)[1]["options"]["num_predict"] == 1000


def test_thinking_off_asks_for_exactly_what_the_pass_wanted():
    request = GenRequest(sampling=Sampling(max_tokens=500))
    payload = ollama_provider(think="off")._payload(request, stream=False)[1]
    assert payload["options"]["num_predict"] == 500


def test_a_request_that_turns_thinking_off_loses_the_headroom_with_it():
    """The retry after an empty reply: no reasoning, so no room for it."""
    request = GenRequest(sampling=Sampling(max_tokens=500), think=False)
    payload = ollama_provider(think="on")._payload(request, stream=False)[1]
    assert payload["options"]["num_predict"] == 500
    assert payload["think"] is False


def test_thinking_is_a_backend_setting_the_gui_can_offer():
    """Three modes, in the order the buttons sit in, and a default per kind:
    auto everywhere except Horde, where the GPU belongs to a volunteer."""
    from app.config import VALID_THINK, default_think

    assert VALID_THINK == ("on", "auto", "off")
    for kind in KIND_DEFAULTS:
        assert KIND_DEFAULTS[kind]["think"] == default_think(kind)
    assert default_think("horde") == "off"
    assert default_think("ollama") == "auto"


# --------------------------------------------------------------------- echo


def echo_provider(**overrides):
    return build(BackendConfig(name="e", kind="echo", model="echo-1", **overrides))


def test_echo_does_not_reason_unless_it_is_asked_to():
    """`auto` is every kind's default, and a stand-in that reasoned by default
    would put a thinking cue in front of every reply on a fresh clone — which
    is the one thing the cue exists to tell apart."""
    from app.providers import ReasoningDelta

    async def run(provider):
        return [d async for d in provider.stream(GenRequest(messages=[
            {"role": "user", "content": "hello"}]))]

    for mode in ("auto", "off"):
        assert not [d for d in sync(run(echo_provider(think=mode)))
                    if isinstance(d, ReasoningDelta)]


def test_echo_reasons_on_demand_and_before_it_says_anything():
    """So the whole thinking path — provider, scheduler, cue — can be watched
    end to end with no model and no network."""
    from app.providers import GenResult, ReasoningDelta

    provider = echo_provider(think="on")
    sink = GenResult()

    async def run():
        return [d async for d in provider.stream(
            GenRequest(messages=[{"role": "user", "content": "hello"}]), sink)]

    deltas = sync(run())
    kinds = [isinstance(d, ReasoningDelta) for d in deltas]
    assert any(kinds), "it was asked to reason and did not"
    # Every reasoning delta before every text one, which is the shape that
    # makes the cue possible: nothing visible arrives until it stops thinking.
    assert kinds == sorted(kinds, reverse=True)
    assert sink.thinking == "".join(str(d) for d in deltas if isinstance(d, ReasoningDelta))
