"""Macro substitution: the thing that stops an imported card addressing {{user}}."""

from __future__ import annotations

import re

from app.macros import MacroContext, context_from, substitute


def ctx(**kwargs) -> MacroContext:
    base = {"char": "Mira", "user": "Tomas", "persona": "A quiet cartographer."}
    base.update(kwargs)
    return MacroContext(**base)


def test_the_two_that_matter():
    text = "{{char}} looks at {{user}}."
    assert substitute(text, ctx()) == "Mira looks at Tomas."


def test_names_are_case_and_space_insensitive():
    assert substitute("{{ CHAR }} and {{User}}", ctx()) == "Mira and Tomas"


def test_bot_is_char_and_persona_is_the_readers_own_description():
    assert substitute("{{bot}}", ctx()) == "Mira"
    assert substitute("{{persona}}", ctx()) == "A quiet cartographer."


def test_an_unknown_macro_is_left_alone():
    """Visible beats silently swallowed — a typo should be findable."""
    assert substitute("{{charr}} waits", ctx()) == "{{charr}} waits"
    assert substitute("{{ nonsense : 3 }}", ctx()) == "{{ nonsense : 3 }}"


def test_text_with_no_macros_is_returned_untouched():
    assert substitute("Nothing to do here.", ctx()) == "Nothing to do here."
    assert substitute("", ctx()) == ""


def test_time_and_date_resolve_to_something_readable():
    assert re.fullmatch(r"\d{2}:\d{2}", substitute("{{time}}", ctx()))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", substitute("{{isodate}}", ctx()))
    assert substitute("{{weekday}}", ctx()) in (
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
    )


def test_idle_duration_reads_as_a_phrase():
    assert substitute("{{idle_duration}}", ctx(idle_seconds=45)) == "45 seconds"
    assert substitute("{{idle_duration}}", ctx(idle_seconds=60)) == "1 minute"
    assert substitute("{{idle_duration}}", ctx(idle_seconds=7200)) == "2 hours"
    assert substitute("{{idle_duration}}", ctx(idle_seconds=None)) == "no time at all"


def test_random_chooses_from_the_list():
    seen = {substitute("{{random: rain, snow, fog }}", ctx()) for _ in range(60)}
    assert seen <= {"rain", "snow", "fog"}
    assert len(seen) > 1, "60 rolls of three options should not all match"


def test_pick_is_stable_for_a_chat_but_differs_between_them():
    """A detail chosen once should stay chosen — that is the whole point."""
    one = ctx(seed="chat-a")
    assert len({substitute("{{pick:a,b,c,d,e}}", one) for _ in range(20)}) == 1

    choices = {substitute("{{pick:a,b,c,d,e}}", ctx(seed=f"chat-{i}")) for i in range(30)}
    assert len(choices) > 1, "different chats should not all pick the same option"


def test_two_picks_in_one_text_are_independent():
    text = "{{pick:a,b,c,d,e,f,g,h}} {{pick:one,two,three,four,five,six,seven,eight}}"
    resolved = [substitute(text, ctx(seed=f"c{i}")).split() for i in range(12)]
    # Both halves vary; if pick were seeded on the chat alone they would move
    # together, and if it ignored the argument they would be identical.
    assert len({r[0] for r in resolved}) > 1
    assert len({r[1] for r in resolved}) > 1


def test_dice():
    for _ in range(40):
        assert 1 <= int(substitute("{{roll:d6}}", ctx())) <= 6
        assert 2 <= int(substitute("{{roll:2d6}}", ctx())) <= 12
        assert 1 <= int(substitute("{{roll:20}}", ctx())) <= 20
    assert substitute("{{roll:nonsense}}", ctx()) == ""
    assert substitute("{{roll:0d6}}", ctx()) == ""


def test_newline_and_comments():
    assert substitute("a{{newline}}b", ctx()) == "a\nb"
    assert substitute("keep{{comment: drop me }}this", ctx()) == "keepthis"


def test_trim_eats_the_space_around_it():
    assert substitute("before {{trim}} after", ctx()) == "beforeafter"
    assert substitute("a\n\n{{trim}}\n\nb", ctx()) == "ab"


def test_nested_macros_resolve_inside_out():
    resolved = substitute("{{random:{{char}},{{user}}}}", ctx())
    assert resolved in ("Mira", "Tomas")


def test_a_self_referential_macro_terminates():
    """A macro that expands to itself must not hang the turn."""
    ctx_with_loop = ctx()
    ctx_with_loop.extra = {"loop": "{{loop}}"}
    assert substitute("{{loop}}", ctx_with_loop) == "{{loop}}"


def test_extra_values_win_over_builtins():
    custom = ctx()
    custom.extra = {"char": "Overridden"}
    assert substitute("{{char}}", custom) == "Overridden"


def test_context_from_a_character_and_a_persona():
    class Card:
        name = "Mira"
        persona = "Keeps the lamp lit."
        scenario = "A tavern in the rain."

    built = context_from(Card(), {"name": "Tomas", "description": "A cartographer."}, seed="c1")
    assert substitute("{{char}} / {{user}} / {{persona}} / {{scenario}}", built) == (
        "Mira / Tomas / A cartographer. / A tavern in the rain."
    )


def test_a_missing_persona_still_leaves_user_readable():
    """A prompt must never contain a literal {{user}}, persona or not."""
    class Card:
        name = "Mira"
        persona = ""
        scenario = ""

    built = context_from(Card(), None)
    assert substitute("{{user}}", built) == "You"
    assert "{{" not in substitute("{{char}} greets {{user}}", built)
