"""Every field the page binds is a field the component actually declares.

Written after a bad afternoon. A one-line edit to `static/app.js` — deleting
a single state field that had become unused — was made with a hand-rolled
string slice that cut from the blank line *above* it, and took 167 lines of
component state with it: `draft`, `stick`, `scrollPort`, `panel`,
`panelOpen`, `menu`, `streamingParagraphsShown`, `editing`, `regenId` and
everything between. The chat layout collapsed into a column of avatars with
no bubbles, and the composer stopped working.

The whole suite stayed green, because nothing in it looks at the page the way
a browser does. That is the gap this closes, and Alpine is what makes the gap
expensive: binding an undefined field is not an error there. `x-show="stick"`
against a field nobody declares is simply false, forever, silently — so the
symptom turns up on a phone rather than in a stack trace.

Source-level, like tests/test_cast_door.py and tests/test_stop_button.py:
there is no JS harness here, and evaluating app.js for real needs a DOM and
runs into its own boot loop. What can be checked without one is that the two
files agree — which is exactly what went wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = (REPO / "static/app.js").read_text()
INDEX = (REPO / "static/index.html").read_text()

# Bindings whose whole value is one bare identifier. The rest are expressions
# — method calls, property paths, comparisons — and resolving those properly
# would mean parsing JavaScript. These are the ones a typo or a bad edit
# breaks silently, and there are seventy of them.
BARE_BINDING = ("x-model", "x-show", "x-text", "x-if", "x-html")


def component() -> str:
    """The `tavern()` component, which is the rest of the file."""
    return APP_JS[APP_JS.index("function tavern()") :]


def declared() -> set[str]:
    """Everything the component carries: state fields, methods and getters.

    Four spaces exactly, so a key inside a nested object literal is not
    mistaken for one of the component's own — the same discriminator
    test_cast_door.py uses to find a method.
    """
    region = component()
    return (
        set(re.findall(r"^    ([A-Za-z_$][\w$]*)\s*[:(]", region, re.MULTILINE))
        | set(re.findall(r"^    (?:async |get )([A-Za-z_$][\w$]*)\s*\(", region, re.MULTILINE))
    )


def loop_variables() -> set[str]:
    """Names `x-for` introduces. They belong to the template, not the
    component, so they are the one kind of bare binding that should *not*
    resolve here."""
    out: set[str] = set()
    for value in re.findall(r'x-for="([^"]+)"', INDEX):
        head = value.split(" in ")[0].strip().strip("()")
        out |= set(re.findall(r"[A-Za-z_$][\w$]*", head))
    return out


def bare_bindings() -> set[str]:
    out: set[str] = set()
    for attribute in BARE_BINDING:
        for value in re.findall(rf'{attribute}="([^"]+)"', INDEX):
            candidate = value.strip().lstrip("!").strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", candidate):
                out.add(candidate)
    return out


def test_every_bare_binding_resolves_to_something_the_component_has():
    unresolved = sorted(bare_bindings() - declared() - loop_variables())
    assert not unresolved, (
        "static/index.html binds fields static/app.js does not declare — Alpine "
        f"renders these as permanently false or empty, with no error: {unresolved}"
    )


def test_the_check_is_actually_looking_at_something():
    """A regex that quietly stops matching would make the test above pass by
    finding nothing to check."""
    assert len(bare_bindings()) > 50
    assert len(declared()) > 200


def test_the_component_still_has_the_state_the_transcript_is_drawn_from():
    """The exact fields the deletion took. Named rather than left to the
    binding sweep above, because some of them are only ever read from
    JavaScript or from an expression this file deliberately does not parse —
    `scrollPort` and `regenId` among them — so nothing else here would have
    noticed them going."""
    have = declared()
    for field in (
        "draft", "editing", "editText", "editHeight", "editingEl",
        "regenId", "regenPrevious", "streamingParagraphsShown", "fadingId",
        "stick", "scrollPort", "menu", "pillOpen", "panel", "panelOpen",
        "confirmDiscardOpen", "staged", "cast", "voices", "voiceIndex",
        "streamAbort", "sendingId", "wheel", "composerMenu", "impersonating",
    ):
        assert field in have, f"static/app.js no longer declares `{field}`"
