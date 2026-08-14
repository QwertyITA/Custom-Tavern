"""Inline markup tokenizer (§8).

The rules that matter: markup nests and interleaves, and unbalanced markup —
which models emit constantly — must degrade without miscolouring the rest of
the message.
"""

from __future__ import annotations

import json

import pytest

from app.markup import ACTION, DIALOGUE, STRONG, parse, parse_to_dicts, to_plain

from .conftest import FIXTURES


def styles_of(text: str) -> list[tuple[str, tuple[str, ...]]]:
    return [(run.text, run.styles) for run in parse(text)]


def test_plain_text_is_one_default_run():
    assert styles_of("just narration") == [("just narration", ())]


def test_dialogue_keeps_its_quotes():
    runs = styles_of('"Hello," she said.')
    assert runs[0] == ('"Hello,"', (DIALOGUE,))
    assert runs[1] == (" she said.", ())


def test_action_markers_are_consumed():
    assert styles_of("*She sighs.*") == [("She sighs.", (ACTION,))]


def test_bold_is_distinct_from_action():
    runs = dict((text, styles) for text, styles in styles_of("**bold** and *action*"))
    assert runs["bold"] == (STRONG,)
    assert runs["action"] == (ACTION,)


def test_dialogue_nested_in_action_carries_both_styles():
    runs = styles_of('*action "quote" more*')
    assert ('"quote"', (DIALOGUE, ACTION)) in runs
    assert ("action ", (ACTION,)) in runs


def test_emphasis_nested_in_dialogue_carries_both_styles():
    runs = styles_of('"speech *emphasis*"')
    assert ("emphasis", (DIALOGUE, ACTION)) in runs


@pytest.mark.parametrize(
    "text",
    [
        "unbalanced *stray and more text",
        'unbalanced "quote and more text',
        "trailing asterisk*",
        "*",
        '"',
        "**",
    ],
)
def test_unbalanced_markup_degrades_to_plain(text):
    """Fail soft: nothing gets styled, and no characters are lost."""
    runs = parse(text)
    assert all(run.styles == () for run in runs)
    assert "".join(run.text for run in runs) == text


def test_stray_marker_does_not_leak_across_paragraphs():
    text = "para one *open\n\npara two* close"
    assert all(run.styles == () for run in parse(text))


def test_arithmetic_asterisk_is_literal():
    assert styles_of("2 * 3 = 6") == [("2 * 3 = 6", ())]


def test_same_style_nests():
    runs = styles_of("a *b *c* d* e")
    assert ("b c d", (ACTION,)) in runs
    assert "".join(t for t, _ in runs) == "a b c d e"


def test_curly_quotes_pair():
    runs = styles_of("“curly” plain")
    assert runs[0] == ("“curly”", (DIALOGUE,))


def test_to_plain_strips_markup_but_keeps_quotes():
    assert to_plain('*She nods.* "Fine."') == 'She nods. "Fine."'


def test_empty_input():
    assert parse("") == []


def test_matches_shared_fixture_contract():
    """The same fixtures the JS tokenizer is checked against."""
    cases = json.loads((FIXTURES / "markup_cases.json").read_text())
    assert cases, "fixture file is empty"
    for case in cases:
        assert parse_to_dicts(case["input"]) == case["runs"], case["input"]
