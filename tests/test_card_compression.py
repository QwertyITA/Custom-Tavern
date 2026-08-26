"""AI-assisted card compression (§ app/card_compression.py)."""

from __future__ import annotations

from app import card_compression
from app.config import Settings
from app.models import Character

from .conftest import sync


def big_character(**overrides) -> Character:
    fields = {
        "persona": "She is quiet and careful. " * 40,
        "scenario": "A small apartment near the coast. " * 20,
        "example_dialogue": '{{user}}: Hi.\n{{char}}: "Hello." ' * 15,
        "system_prompt": "Stay in character and answer briefly. " * 10,
    }
    fields.update(overrides)
    return Character(id="big", name="Nyra", first_mes="Hi.", **fields)


# ------------------------------------------------------------ pure helpers


def test_eligible_fields_skips_blank_ones():
    character = Character(id="c", name="X", persona="Something.", scenario="", first_mes="Hi.")
    fields = card_compression.eligible_fields(character)
    assert fields == {"persona": "Something."}


def test_field_targets_splits_the_reduction_proportionally():
    from app.providers.base import estimate_tokens

    fields = {"a": "word " * 100, "b": "word " * 300}
    size_a, size_b = estimate_tokens(fields["a"]), estimate_tokens(fields["b"])
    targets = card_compression.field_targets(fields, reduce_by=40)
    assert targets["a"] < size_a and targets["b"] < size_b
    # The bigger field gives up more of the cut.
    assert (size_b - targets["b"]) > (size_a - targets["a"])


def test_field_targets_never_asks_below_the_keep_floor():
    from app.providers.base import estimate_tokens

    fields = {"a": "word " * 100}
    size = estimate_tokens(fields["a"])
    targets = card_compression.field_targets(fields, reduce_by=10000)
    assert targets["a"] >= int(size * card_compression._MIN_KEEP_FRACTION)


def test_field_targets_skips_fields_too_small_to_bother_with():
    fields = {"tiny": "word " * 5}  # well under _MIN_WORTH_COMPRESSING
    assert card_compression.field_targets(fields, reduce_by=1000) == {}


def test_field_targets_with_nothing_to_reduce_is_empty():
    fields = {"a": "word " * 200}
    assert card_compression.field_targets(fields, reduce_by=0) == {}


def test_clean_strips_a_wrapping_quote_but_not_an_internal_one():
    assert card_compression._clean('"She said hi."') == "She said hi."
    assert card_compression._clean('She said "hi" once.') == 'She said "hi" once.'


# ------------------------------------------------------------------ preview


def test_preview_with_nothing_to_reduce_changes_nothing():
    character = big_character()
    result = sync(card_compression.preview(Settings(), character, reduce_by=0))
    assert result["changed"] is False
    for field in result["fields"].values():
        assert field["before"] == field["after"]
        assert field["changed"] is False


def test_preview_never_accepts_a_result_longer_than_the_original():
    """The echo backend answers with a fixed, unrelated line — shorter than a
    long field, longer than a short one — so this also verifies the
    reject-if-longer guard against a real (if canned) provider response."""
    character = big_character(persona="Short.", scenario="Also short.")
    result = sync(card_compression.preview(Settings(), character, reduce_by=1000))
    for key in ("persona", "scenario"):
        field = result["fields"][key]
        assert field["after_tokens"] <= field["before_tokens"]


def test_preview_reports_per_field_token_counts():
    character = big_character()
    result = sync(card_compression.preview(Settings(), character, reduce_by=50))
    for key in card_compression.FIELDS:
        assert key in result["fields"]
        field = result["fields"][key]
        assert field["before_tokens"] > 0
        assert isinstance(field["changed"], bool)
