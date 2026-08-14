"""Pass I/O contract (§5.6): the delimited suffix and rubric signals."""

from __future__ import annotations

import pytest

from app.passes.contract import (
    MARKER,
    SuffixStreamFilter,
    coerce_signal,
    normalise_payload,
    parse_json_loose,
    signal_rank,
    split_state_suffix,
)


def test_split_separates_reply_from_payload():
    body, payload = split_state_suffix(
        '*She nods.* "Fine."\n' + MARKER + '{"deltas": {"trust": 1}}'
    )
    assert body.strip() == '*She nods.* "Fine."'
    assert payload == {"deltas": {"trust": 1}}


def test_split_without_suffix_returns_whole_text():
    body, payload = split_state_suffix("no suffix here")
    assert body == "no suffix here"
    assert payload is None


def test_loose_json_survives_code_fences_and_preamble():
    assert parse_json_loose('Sure! ```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('Here you go: {"a": 1} hope that helps') == {"a": 1}
    assert parse_json_loose("not json at all") is None


@pytest.mark.parametrize(
    "value,expected",
    [
        ("major", "major"), ("MINOR", "minor"), ("none", "none"),
        ("high", "major"), ("low", "minor"), ("no", "none"),
        (0.0, "none"), (0.3, "minor"), (0.9, "major"),
        (True, "major"), (False, "none"),
        ("nonsense", None),
    ],
)
def test_signals_coerce_onto_the_rubric(value, expected):
    """Models answer with floats and synonyms; the ladder absorbs both (§5.2)."""
    assert coerce_signal(value) == expected


def test_signal_rank_orders_the_rubric():
    assert signal_rank("none") < signal_rank("minor") < signal_rank("major")


def test_normalise_drops_junk_and_keeps_extras():
    result = normalise_payload(
        {
            "deltas": {"trust": "2", "bogus": "not a number"},
            "signals": {"scene_change": "MAJOR", "nonsense": "banana"},
            "reason": "because",
        }
    )
    assert result["deltas"] == {"trust": 2.0}
    assert result["signals"] == {"scene_change": "major"}
    assert result["extra"]["reason"] == "because"


def test_normalise_handles_missing_payload():
    assert normalise_payload(None) == {"deltas": {}, "signals": {}, "extra": {}}


# ------------------------------------------------------- streaming filter


def feed_all(chunks: list[str]) -> tuple[str, dict | None]:
    filt = SuffixStreamFilter()
    emitted = "".join(filt.feed(chunk) for chunk in chunks)
    tail, payload = filt.finish()
    return emitted + tail, payload


def test_stream_filter_hides_the_marker():
    text, payload = feed_all(['"Fine."', "\n", MARKER, '{"deltas": {"trust": 1}}'])
    assert MARKER not in text
    assert payload == {"deltas": {"trust": 1}}


def test_marker_split_across_chunks_is_still_caught():
    """The marker usually arrives in pieces — that is the whole point."""
    chunks = ['"Fine."', "\n<<<", "sta", "te>>", '>{"deltas": {"trust": 1}}']
    text, payload = feed_all(chunks)
    assert MARKER not in text
    assert "<<<" not in text
    assert payload == {"deltas": {"trust": 1}}


def test_one_character_at_a_time_still_works():
    source = 'hello' + MARKER + '{"signals": {"scene_change": "major"}}'
    text, payload = feed_all(list(source))
    assert text == "hello"
    assert payload == {"signals": {"scene_change": "major"}}


def test_stream_without_suffix_emits_everything():
    text, payload = feed_all(["all ", "of ", "the ", "text"])
    assert text == "all of the text"
    assert payload is None


def test_text_resembling_the_marker_is_not_swallowed():
    text, payload = feed_all(["a <<<b>>> c"])
    assert text == "a <<<b>>> c"
    assert payload is None
