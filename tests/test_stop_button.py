"""The stop button, and the invariant that makes it work at all.

`stopGenerating()` (static/app.js) has exactly one lever: it aborts
`this.streamAbort`. Anything that streams and does not arm that lever is a
turn the button silently cannot stop — the button is still *there*, because
it shows on `streaming` and swaps in over the send icon, so it looks live,
takes the press and does nothing.

That is precisely how it shipped for Impersonate: the one streaming path of
four that never built an AbortController and never passed a signal to its
fetch. Pressing stop mid-draft kept the text arriving and left `streaming`
true, which then blocked send, swipe and continue behind it as well.

These are source-level checks rather than behavioural ones: there is no JS
test harness here, and the invariant is a structural property of the file —
which is also what makes it cheap to state and hard to drift away from.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
APP_JS = (REPO / "static/app.js").read_text()

# Methods of the one big Alpine object, which all sit at four-space indent.
_METHOD = re.compile(r"^    (?:async )?([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)


def methods() -> dict[str, str]:
    """Each method's source, keyed by name. Crude but exact enough: a chunk
    runs from its own signature to the next one's."""
    marks = [(m.group(1), m.start()) for m in _METHOD.finditer(APP_JS)]
    out: dict[str, str] = {}
    for i, (name, start) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(APP_JS)
        out[name] = APP_JS[start:end]
    return out


def streaming_methods() -> dict[str, str]:
    """Everything that claims the composer by setting `streaming` true — which
    is exactly what puts the stop button on screen in the first place."""
    return {
        name: body
        for name, body in methods().items()
        if "this.streaming = true" in body
    }


def test_there_are_streaming_methods_to_check():
    """A guard on the guard: if the object's shape ever changes enough that
    this stops finding anything, the checks below would pass vacuously."""
    found = streaming_methods()
    assert len(found) >= 3, f"only found {sorted(found)}"
    assert "impersonate" in found, "the one this was written for"


@pytest.mark.parametrize("name", sorted(streaming_methods()))
def test_every_streaming_path_can_be_stopped(name):
    """Arms the lever stopGenerating() pulls, and actually hands it to the
    request — an AbortController built and never passed to `fetch` aborts
    nothing."""
    body = streaming_methods()[name]
    assert "this.streamAbort = new AbortController()" in body, (
        f"{name} streams without arming streamAbort — the stop button would "
        f"take the press and do nothing"
    )
    assert "signal: this.streamAbort.signal" in body, (
        f"{name} builds an AbortController but never passes its signal to the "
        f"fetch, so aborting it cannot reach the request"
    )


@pytest.mark.parametrize("name", sorted(streaming_methods()))
def test_every_streaming_path_releases_the_lever_when_it_ends(name):
    """Left set, a finished stream's controller is what the *next* press
    would abort — a stop that lands on nothing, or worse on the wrong thing."""
    body = streaming_methods()[name]
    assert "this.streamAbort = null" in body, f"{name} never clears streamAbort"


@pytest.mark.parametrize("name", sorted(streaming_methods()))
def test_a_stop_is_never_reported_as_an_error(name):
    """Stopping is deliberate. A path that funnels AbortError into the error
    banner tells the person their own tap was a failure."""
    body = streaming_methods()[name]
    assert 'e.name === "AbortError"' in body, (
        f"{name} does not tell a deliberate stop apart from a real failure"
    )


def test_the_pacer_is_never_reached_from_outside_its_own_closure():
    """`pacer` is a `const` local to runStream (§ makePacer). Reaching for it
    from a sibling method throws ReferenceError instead of doing the work —
    which is what `continueReply`'s abort branch did, so stopping a Continue
    blew up on the way out instead of saying "Stopped" and reloading what the
    server had actually kept."""
    offenders = [
        name
        for name, body in methods().items()
        if re.search(r"\bpacer\.", body) and "const pacer" not in body
    ]
    assert not offenders, f"{offenders} use `pacer` without owning one"
