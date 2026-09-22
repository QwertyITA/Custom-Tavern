"""boot() used to leave a dead-server banner on screen until the person
noticed and reloaded by hand.

Reported live: on a phone, Android sometimes freezes the server's process
rather than killing it — tmux, the wake lock, the foreground notification,
all still there, the process just does not answer for a while. Confirmed by
watching a real session recover on its own after several minutes with
nothing done to it. A `TypeError`/`NetworkError` from `fetch` (§ errorText's
own check — this is the *only* thing that ever produces that message) is
exactly this case: the server was never reached at all, as opposed to a 4xx
or 5xx, which means it was reached and said no to something specific.

`retryBoot` chases the first kind and leaves the second alone: doubling the
wait between attempts (2s, 4s, 8s, … capped at 20s) rather than polling on a
fixed interval, quiet on every attempt but the last so a slow wake-up reads
as nothing happening rather than as repeated failures, and never more than
one loop running at a time.

Source-level, the same shape as tests/test_edit_portrait_conflict.py — no JS
harness here.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_JS = (REPO / "static/app.js").read_text()

_METHOD = re.compile(r"^    (?:async )?(?:get )?([A-Za-z_$][\w$]*)\s*\(", re.MULTILINE)


def method(name: str) -> str:
    marks = [(m.group(1), m.start()) for m in _METHOD.finditer(APP_JS)]
    for i, (found, start) in enumerate(marks):
        if found != name:
            continue
        end = marks[i + 1][1] if i + 1 < len(marks) else len(APP_JS)
        return APP_JS[start:end]
    raise AssertionError(f"no method {name}() in static/app.js")


def test_only_a_connectivity_failure_triggers_a_retry():
    """A 4xx/5xx means the server answered — retrying blindly would not fix
    that, and would turn one real error into a loop hammering it."""
    body = method("boot")
    assert 'if (e && (e.name === "TypeError" || e.name === "NetworkError")) {' in body
    assert "this.retryBoot();" in body


def test_the_banner_is_still_raised_once_before_retrying():
    """The retry is silent, not invisible — the first failure still says
    what happened; only the repeats of that same sentence are suppressed."""
    body = method("boot")
    error_set = body.index("this.error = errorText(e);")
    retry_call = body.index("this.retryBoot();")
    assert error_set < retry_call


def test_retry_backs_off_rather_than_polling_a_fixed_interval():
    body = method("retryBoot")
    assert "delay = Math.min(delay * 2, BOOT_RETRY_MAX_MS);" in body
    assert "let delay = BOOT_RETRY_START_MS;" in body


def test_only_one_retry_loop_runs_at_a_time():
    body = method("retryBoot")
    assert "if (this._bootRetrying) return;" in body
    assert "this._bootRetrying = true;" in body
    # Cleared exactly once, after the `while` loop — not once per branch
    # inside it, which would leave it stuck true whenever a `break` skipped
    # that branch's own cleanup.
    assert body.count("this._bootRetrying = false;") == 1
    while_line = body.index("while (this._bootRetrying) {")
    clear_line = body.index("this._bootRetrying = false;")
    assert while_line < clear_line
    # Nothing but the loop's own closing brace and the method's own sits
    # between the two — i.e. the clear is not nested inside `try`/`catch`.
    between = body[while_line:clear_line]
    assert between.count("break;") == 2  # the loop's two exit points, both still inside it


def test_a_real_error_on_retry_stops_the_loop_instead_of_retrying_forever():
    """The server answering with something other than silence — a 4xx, a
    5xx — is not a reason to keep asking; it is a reason to say so and
    stop, the same distinction boot()'s own first catch already draws."""
    body = method("retryBoot")
    assert 'if (!(e && (e.name === "TypeError" || e.name === "NetworkError"))) {' in body
    guard = body.index('if (!(e && (e.name === "TypeError"')
    break_after = body.index("break;", guard)
    assert break_after > guard


def test_success_clears_the_error_and_finishes_what_boot_started():
    """Not just a retried fetch — the same follow-on work boot() itself does
    once characters actually load: picking a character and loading chats, so
    the app ends up in exactly the state a successful first boot would have
    left it in, not a half-populated one."""
    body = method("retryBoot")
    assert "this.characterId = this.characters[0].id;" in body
    assert "this.chats = await api.get" in body
    assert "this.loadHomeToggles();" in body
    assert 'this.error = "";' in body


def test_retrying_does_nothing_when_there_are_still_no_characters():
    """A genuinely empty install (§ boot's own early return) is not an error
    to chase — success here means confirming the server answered, not that
    a roster appeared from nowhere."""
    body = method("retryBoot")
    assert "if (this.characters.length) {" in body
