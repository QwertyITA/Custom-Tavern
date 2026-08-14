"""State model (§6) and write arbitration (§5.5)."""

from __future__ import annotations

from app.state import (
    DEFAULT_STATE_SCHEMA,
    SLICE_VARS,
    apply_deltas,
    apply_nudges,
    band_guidance,
    decay_step,
    initial_values,
    load_nudges,
    load_schema,
    read_slice,
    render_bands,
    rollback_turn,
    write_slice,
)

from .conftest import sync

SCHEMA = load_schema(DEFAULT_STATE_SCHEMA)


# ------------------------------------------------------------------ bands


def test_default_schema_covers_the_canonical_variables():
    assert set(SCHEMA) == {"willingness", "trust", "mood", "energy"}


def test_band_resolves_to_guidance_not_numbers():
    guidance = render_bands(SCHEMA, {"willingness": 2, "trust": 9, "mood": 5, "energy": 6})
    assert "guarded" in guidance and "open" in guidance
    # The whole point: no raw value ever reaches the prompt.
    assert "2" not in guidance and "9" not in guidance


def test_band_lookup_clamps_out_of_range_values():
    label = SCHEMA["willingness"].band_for(999).label
    assert label == "eager"
    assert SCHEMA["willingness"].band_for(-50).label == "guarded"


def test_band_guidance_uses_labels():
    rows = band_guidance(SCHEMA, initial_values(SCHEMA))
    assert ("Willingness", "neutral", SCHEMA["willingness"].bands[1].guidance) in rows


# ------------------------------------------------------------------ decay


def test_decay_pulls_toward_baseline_from_both_directions():
    high = decay_step(SCHEMA, {"willingness": 9})["willingness"]
    low = decay_step(SCHEMA, {"willingness": 1})["willingness"]
    assert high < 9 and low > 1


def test_decay_never_overshoots_the_baseline():
    baseline = SCHEMA["willingness"].baseline
    values = {"willingness": baseline + 0.05}
    assert decay_step(SCHEMA, values)["willingness"] == baseline


def test_decay_is_a_no_op_at_baseline():
    values = initial_values(SCHEMA)
    assert decay_step(SCHEMA, values) == values


def test_decay_preserves_unknown_variables():
    assert decay_step(SCHEMA, {"custom": 3})["custom"] == 3


# ----------------------------------------------------------------- nudges


def test_nudges_fire_on_keyword_and_clamp():
    rules = load_nudges([{"pattern": r"\bthanks\b", "variable": "willingness", "delta": 100}])
    values, fired = apply_nudges(rules, SCHEMA, initial_values(SCHEMA), "thanks for that", "user")
    assert fired == ["willingness+100"]
    assert values["willingness"] == SCHEMA["willingness"].max


def test_nudges_respect_role_scoping():
    rules = load_nudges([{"pattern": "x", "variable": "trust", "delta": 1, "applies_to": "assistant"}])
    _, fired = apply_nudges(rules, SCHEMA, initial_values(SCHEMA), "x", "user")
    assert fired == []


def test_malformed_nudge_rules_are_skipped_not_fatal():
    rules = load_nudges([{"nonsense": True}, {"pattern": "[", "variable": "trust", "delta": 1}])
    values, fired = apply_nudges(rules, SCHEMA, initial_values(SCHEMA), "anything", "user")
    assert fired == []  # the invalid regex is ignored rather than raising
    assert values == initial_values(SCHEMA)


def test_deltas_clamp_and_ignore_junk():
    values = apply_deltas(SCHEMA, {"trust": 4}, {"trust": 99, "mood": "banana"})
    assert values["trust"] == SCHEMA["trust"].max
    assert "mood" not in values or values["mood"] == 4


# ------------------------------------------------- slices & arbitration


def test_slice_write_and_read_round_trip(db, chat):
    result = sync(write_slice(db, chat["id"], SLICE_VARS, {"trust": 7}, source_turn=3))
    assert result.accepted
    stored = read_slice(db, chat["id"], SLICE_VARS)
    assert stored["value"] == {"trust": 7}
    assert stored["source_turn"] == 3


def test_older_turn_write_is_rejected_within_the_same_slice(db, chat):
    """The sole arbitration rule: same slice, older turn loses (§5.5)."""
    sync(write_slice(db, chat["id"], SLICE_VARS, {"trust": 7}, source_turn=5, source_pass="basic"))
    late = sync(
        write_slice(db, chat["id"], SLICE_VARS, {"trust": 2}, source_turn=4, source_pass="auditor")
    )
    assert not late.accepted
    assert "stale" in late.reason
    assert read_slice(db, chat["id"], SLICE_VARS)["value"] == {"trust": 7}


def test_same_turn_correction_is_accepted(db, chat):
    """The auditor correcting pass 1 within the same turn must win."""
    sync(write_slice(db, chat["id"], SLICE_VARS, {"trust": 7}, source_turn=5,
                     source_pass="basic", provisional=True))
    corrected = sync(
        write_slice(db, chat["id"], SLICE_VARS, {"trust": 5}, source_turn=5,
                    source_pass="state_auditor")
    )
    assert corrected.accepted
    stored = read_slice(db, chat["id"], SLICE_VARS)
    assert stored["value"] == {"trust": 5}
    assert stored["provisional"] is False


def test_different_slices_never_contend(db, chat):
    """Independent facts update on arrival; order among them is irrelevant."""
    assert sync(write_slice(db, chat["id"], "state.scene", {"place": "bar"}, source_turn=9)).accepted
    assert sync(write_slice(db, chat["id"], "state.weather", {"w": "rain"}, source_turn=2)).accepted
    assert read_slice(db, chat["id"], "state.weather")["value"] == {"w": "rain"}


# --------------------------------------------------------------- rollback


def test_rollback_restores_the_previous_value(db, chat):
    sync(write_slice(db, chat["id"], SLICE_VARS, {"trust": 4}, source_turn=1))
    sync(write_slice(db, chat["id"], SLICE_VARS, {"trust": 9}, source_turn=2, variant_id="v1"))

    assert sync(rollback_turn(db, chat["id"], 2, "v1")) == 1
    restored = read_slice(db, chat["id"], SLICE_VARS)
    assert restored["value"] == {"trust": 4}
    assert restored["source_turn"] == 1


def test_rollback_removes_a_slice_that_did_not_exist_before(db, chat):
    sync(write_slice(db, chat["id"], "state.scene", {"place": "bar"}, source_turn=2, variant_id="v1"))
    sync(rollback_turn(db, chat["id"], 2, "v1"))
    assert read_slice(db, chat["id"], "state.scene") is None


def test_rollback_is_idempotent(db, chat):
    sync(write_slice(db, chat["id"], SLICE_VARS, {"trust": 9}, source_turn=2, variant_id="v1"))
    assert sync(rollback_turn(db, chat["id"], 2, "v1")) == 1
    assert sync(rollback_turn(db, chat["id"], 2, "v1")) == 0


def test_rollback_only_touches_the_named_variant(db, chat):
    sync(write_slice(db, chat["id"], SLICE_VARS, {"trust": 1}, source_turn=2, variant_id="keep"))
    sync(write_slice(db, chat["id"], "state.scene", {"p": "x"}, source_turn=2, variant_id="drop"))
    sync(rollback_turn(db, chat["id"], 2, "drop"))
    assert read_slice(db, chat["id"], SLICE_VARS)["value"] == {"trust": 1}
    assert read_slice(db, chat["id"], "state.scene") is None
