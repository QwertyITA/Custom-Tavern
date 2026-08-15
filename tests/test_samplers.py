"""Samplers: one catalogue, three consumers (§17).

Two rules are what these protect. A sampler is only sent once it has been moved
off neutral, and support is declared rather than attempted — Horde rejects the
whole request on a parameter it does not know, so "send it and see" is not an
option there.
"""

from __future__ import annotations

import pytest

from app import samplers
from app.config import BackendConfig, VALID_KINDS
from app.models import Sampling
from app.providers import GenRequest, build
from app.providers.horde import HordeProvider
from app.providers.ollama import LlamaCppProvider, OllamaProvider
from app.providers.openai_compat import OpenAICompatProvider


def request_with(**over) -> GenRequest:
    return GenRequest(
        system="S", messages=[{"role": "user", "content": "hi"}], sampling=Sampling(**over)
    )


def payload_for(cls, kind, **over) -> dict:
    provider = cls(BackendConfig(name="b", kind=kind, model="m"))
    out = provider._payload(request_with(**over), stream=False)
    return out[1] if isinstance(out, tuple) else out


# --------------------------------------------------------------- catalogue


def test_every_sampler_has_a_group_that_exists():
    assert all(s.group in samplers.GROUP_IDS for s in samplers.SAMPLERS)


def test_every_sampler_has_a_label_and_a_plain_language_note():
    for sampler in samplers.SAMPLERS:
        assert sampler.label and sampler.note, sampler.key
        assert not sampler.note.endswith(("min_p", "top_k")), "explain, do not restate"


def test_every_neutral_sits_inside_its_own_range():
    for sampler in samplers.SAMPLERS:
        assert sampler.lo <= sampler.neutral <= sampler.hi, sampler.key


def test_every_sampler_is_a_field_on_sampling():
    fields = set(Sampling.model_fields)
    assert {s.key for s in samplers.SAMPLERS} <= fields


def test_every_backend_name_maps_to_a_real_sampler():
    """A mapping entry for a key that does not exist would silently send
    nothing, and look like the backend ignoring the setting."""
    for kind, mapping in samplers.BACKEND_PARAMS.items():
        for key in mapping:
            assert key in samplers.BY_KEY, f"{kind} maps unknown sampler {key}"


def test_every_backend_kind_is_covered():
    """A kind with no entry accepts nothing, which must be a decision rather
    than an omission."""
    assert set(samplers.BACKEND_PARAMS) >= set(VALID_KINDS)


def test_the_catalogue_is_handed_out_as_copies():
    book = samplers.catalogue()
    book["groups"][0]["label"] = "clobbered"
    assert samplers.GROUPS[0]["label"] != "clobbered"


# ------------------------------------------------------- neutral means off


def test_a_neutral_sampler_is_not_sent():
    options = payload_for(OllamaProvider, "ollama")["options"]
    for key in ("min_p", "typical_p", "tfs_z", "seed", "presence_penalty"):
        assert key not in options, f"{key} is at neutral and should be absent"


def test_a_moved_sampler_is_sent_under_the_backend_s_own_name():
    options = payload_for(OllamaProvider, "ollama", min_p=0.05, tfs=0.95)["options"]
    assert options["min_p"] == 0.05
    assert options["tfs_z"] == 0.95, "ollama calls it tfs_z"


def test_the_conventional_defaults_are_still_sent():
    """They are not at neutral, and moving them there would change everyone's
    output on upgrade for no reason anybody asked for."""
    options = payload_for(OllamaProvider, "ollama")["options"]
    assert options["temperature"] == 0.8
    assert options["top_p"] == 0.95
    assert options["top_k"] == 40
    assert options["repeat_penalty"] == 1.1


def test_a_sampler_moved_back_to_neutral_stops_being_sent():
    assert "min_p" not in payload_for(OllamaProvider, "ollama", min_p=0.0)["options"]
    assert "top_k" not in payload_for(OllamaProvider, "ollama", top_k=0)["options"]


def test_a_seed_of_minus_one_means_do_not_pin_it():
    assert "seed" not in payload_for(OllamaProvider, "ollama")["options"]
    assert payload_for(OllamaProvider, "ollama", seed=7)["options"]["seed"] == 7


# -------------------------------------------------- support is declared


def test_horde_is_never_sent_a_sampler_it_does_not_document():
    """It validates its parameters and 400s on an unknown one, so this is not
    a tidiness question."""
    provider = HordeProvider(BackendConfig(name="h", kind="horde"))
    params = provider.build_payload(
        request_with(dry_multiplier=2.0, xtc_probability=0.5, seed=9, presence_penalty=1.0)
    )["params"]
    for forbidden in ("dry_multiplier", "xtc_probability", "seed", "presence_penalty"):
        assert forbidden not in params


def test_horde_uses_its_own_names():
    provider = HordeProvider(BackendConfig(name="h", kind="horde"))
    params = provider.build_payload(request_with(typical_p=0.9, rep_range=256))["params"]
    assert params["typical"] == 0.9, "kobold calls it typical, not typical_p"
    assert params["rep_pen_range"] == 256


def test_horde_still_clamps_what_it_does_accept():
    provider = HordeProvider(BackendConfig(name="h", kind="horde"))
    params = provider.build_payload(request_with(temp=0.0))["params"]
    assert params["temperature"] >= 0.01, "horde rejects a zero temperature"


def test_openai_is_not_sent_top_k():
    """Local servers wearing this API often take it; the real one 400s."""
    payload = payload_for(OpenAICompatProvider, "openai", top_k=20, min_p=0.05)
    assert "top_k" not in payload and "min_p" not in payload
    assert payload["temperature"] == 0.8


def test_openai_takes_the_penalties_and_the_seed():
    payload = payload_for(OpenAICompatProvider, "openai", presence_penalty=0.5, seed=3)
    assert payload["presence_penalty"] == 0.5 and payload["seed"] == 3


def test_only_llamacpp_takes_dry_and_xtc():
    """It is the one backend here whose server exposes them."""
    payload = payload_for(
        LlamaCppProvider, "llamacpp", dry_multiplier=0.8, xtc_probability=0.5
    )
    assert payload["dry_multiplier"] == 0.8
    assert payload["xtc_probability"] == 0.5
    assert payload["dry_base"] == 1.75, "its companions come along once it is on"

    for kind in ("ollama", "horde", "openai"):
        assert "dry_multiplier" not in samplers.supported(kind), kind


def test_dry_companions_stay_out_while_dry_is_off():
    """dry_base and the rest have non-zero neutrals of their own, so they must
    not leak into a request that never turned DRY on."""
    payload = payload_for(LlamaCppProvider, "llamacpp")
    for key in ("dry_multiplier", "dry_base", "dry_allowed_length", "dry_penalty_last_n"):
        assert key not in payload


def test_xtc_threshold_stays_out_while_xtc_is_off():
    assert "xtc_threshold" not in payload_for(LlamaCppProvider, "llamacpp")


# --------------------------------------------------------------- cleaning


def test_a_value_past_the_end_of_the_range_is_clamped():
    assert samplers.params_for("ollama", Sampling(min_p=9.0))["min_p"] == 0.5
    assert samplers.params_for("ollama", Sampling(top_k=9999))["top_k"] == 200


def test_an_integer_sampler_is_sent_as_an_integer():
    assert samplers.BY_KEY["rep_range"].clean(128.7) == 129
    value = samplers.params_for("ollama", Sampling(rep_range=128))["repeat_last_n"]
    assert isinstance(value, int) and value == 128


@pytest.mark.parametrize("junk", [None, "nonsense", float("nan")])
def test_junk_in_a_sampler_field_does_not_reach_a_backend(junk):
    class Loose:
        pass

    loose = Loose()
    for sampler in samplers.SAMPLERS:
        setattr(loose, sampler.key, junk)
    params = samplers.params_for("llamacpp", loose)
    assert all(isinstance(v, (int, float)) for v in params.values())


def test_an_unknown_backend_kind_is_sent_nothing():
    assert samplers.params_for("not-a-backend", Sampling(min_p=0.1)) == {}


def test_the_echo_backend_accepts_nothing_and_that_is_fine():
    assert samplers.supported("echo") == set()
    provider = build(BackendConfig(name="e", kind="echo", model="echo-1"))
    assert provider.stop_strings(Sampling()) is not None


# ------------------------------------------------------------ through the API


def test_the_api_hands_out_the_catalogue(client):
    body = client.get("/api/samplers").json()
    keys = [s["key"] for s in body["samplers"]]
    assert "min_p" in keys and "dry_multiplier" in keys
    assert all(s["group"] in [g["id"] for g in body["groups"]] for s in body["samplers"])
    assert "dry_multiplier" in body["supported"]["llamacpp"]
    assert "dry_multiplier" not in body["supported"]["horde"]


def test_a_pass_keeps_its_samplers(client):
    passes = client.get("/api/passes").json()
    basic = next(p for p in passes if p["id"] == "basic")
    basic["sampling"]["min_p"] = 0.07
    basic["sampling"]["dry_multiplier"] = 0.8
    assert client.put(f"/api/passes/{basic['id']}", json=basic).status_code == 200

    again = next(p for p in client.get("/api/passes").json() if p["id"] == "basic")
    assert again["sampling"]["min_p"] == 0.07
    assert again["sampling"]["dry_multiplier"] == 0.8


def test_an_old_pass_definition_gains_the_new_samplers_at_neutral(db):
    """Pass definitions are stored as JSON and predate every one of these."""
    from app.models import PassDef

    definition = PassDef.model_validate({"id": "old", "sampling": {"temp": 0.7}})
    assert definition.sampling.min_p == 0.0
    assert definition.sampling.seed == -1
    assert samplers.params_for("llamacpp", definition.sampling)["temperature"] == 0.7
