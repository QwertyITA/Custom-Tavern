"""Replies that arrive with nothing usable in them.

Reported from a real run against a model on a PC: the character answered "…"
almost every turn. Nothing was wrong with the model — "…" was this app's own
placeholder for an empty reply, and it was applied silently, so a setup problem
looked like the character having nothing to say.

Two things are covered here. Text that *can* be recovered is recovered, and
text that genuinely cannot leaves a failed turn with a reason rather than a
fabricated message.

The echo backend always answers well-formed, which is exactly why none of this
was caught: these use stub providers that misbehave the way small local models
actually do.
"""

from __future__ import annotations

import pytest

from app.passes.contract import MARKER, SuffixStreamFilter, split_state_suffix
from app.passes.scheduler import _why_empty

PAYLOAD = '{"deltas": {"trust": 1}, "signals": {"narrative_drive": "minor"}}'
REPLY = '*She looks up.* "Sit wherever."'


def streamed(raw: str, chunk: int = 1) -> tuple[str, dict | None]:
    """Push text through the filter the way a provider would, worst case: one
    character at a time, so the marker straddles every possible boundary."""
    f = SuffixStreamFilter()
    visible = "".join(f.feed(raw[i : i + chunk]) for i in range(0, len(raw), chunk))
    tail, payload = f.finish()
    return visible + tail, payload


# ------------------------------------------------- the contract arriving first


def test_a_reply_after_the_state_block_is_not_thrown_away():
    """The contract asks for the marker last and small models put it first —
    it is the final instruction they read, so it is the most salient thing in
    the prompt. Taking only the text before it discarded the entire reply."""
    body, payload = split_state_suffix(f"{MARKER}{PAYLOAD}\n{REPLY}")
    assert REPLY in body
    assert payload["deltas"] == {"trust": 1}


def test_it_survives_the_stream_one_character_at_a_time():
    body, payload = streamed(f"{MARKER}{PAYLOAD}\n{REPLY}")
    assert REPLY in body
    assert payload is not None


@pytest.mark.parametrize("chunk", [1, 3, 7, 40])
def test_any_chunking_recovers_the_same_text(chunk):
    """A provider chooses its own chunk sizes and the marker lands across them
    differently each time."""
    body, _ = streamed(f"{MARKER}{PAYLOAD}\n{REPLY}", chunk)
    assert REPLY in body


def test_the_ordinary_way_round_still_works():
    body, payload = split_state_suffix(f"{REPLY}\n{MARKER}{PAYLOAD}")
    assert body.strip() == REPLY
    assert payload["deltas"] == {"trust": 1}


def test_prose_on_both_sides_is_kept():
    body, _ = split_state_suffix(f"before{MARKER}{PAYLOAD} after")
    assert "before" in body and "after" in body


def test_nested_braces_do_not_end_the_payload_early():
    """Signals are a nested object, so a scan that stops at the first `}` would
    treat half the JSON as reply text."""
    body, payload = split_state_suffix(f"{MARKER}{PAYLOAD} tail")
    assert body.strip() == "tail"
    assert payload["signals"]["narrative_drive"] == "minor"


def test_an_unparseable_payload_leaves_the_text_alone():
    body, payload = split_state_suffix(f"{REPLY}{MARKER}not json at all")
    assert payload is None
    assert body.strip() == REPLY


# ------------------------------------------------------- diagnosing an empty


def test_nothing_at_all_names_the_backend():
    assert "nothing at all" in _why_empty("", "", "")


def test_only_reasoning_names_max_tokens():
    """A reasoning model that ran out of room before it stopped thinking. The
    fix is a setting, so the message has to name the setting."""
    why = _why_empty("<think>hmm", "hmm", "")
    assert "Max tokens" in why


def test_only_the_state_block_says_so():
    why = _why_empty(f"{MARKER}{PAYLOAD}", "", "")
    assert "state block" in why


def test_everything_stripped_names_the_switch_that_did_it():
    why = _why_empty("\nYou: hello", "", "\nYou: hello")
    assert "strip user turn leakage" in why


def test_every_reason_is_a_sentence_a_person_can_act_on():
    """The point of all four is that they name a next step. A reason that only
    describes the symptom is the "…" this replaced."""
    for raw, thinking, body in [("", "", ""), ("<think>x", "x", ""),
                                (f"{MARKER}{{}}", "", ""), ("\nYou: x", "", "\nYou: x")]:
        why = _why_empty(raw, thinking, body)
        assert why.endswith(".") and len(why) > 40
        assert "…" not in why


# ------------------------------------------------------- through a whole turn


class Misbehaving:
    """A provider that answers the way a small local model does."""

    name = "stub"
    model = "stub-1"
    sees_images = False

    def __init__(self, output: str, thinking: str = "") -> None:
        self.output = output
        # Backends that parse reasoning themselves put it here rather than in
        # the stream — Ollama does, so an empty reply from a thinking model
        # arrives with nothing streamed at all.
        self.thinking = thinking

    async def stream(self, request, sink=None):
        if sink is not None:
            sink.provider, sink.model = self.name, self.model
            sink.thinking = self.thinking
        # Awkward chunking on purpose: the marker straddles a boundary.
        for i in range(0, len(self.output), 5):
            yield self.output[i : i + 5]

    async def generate(self, request):
        from app.providers import GenResult

        return GenResult(text=self.output, provider=self.name, model=self.model)

    async def aclose(self):
        return None


@pytest.fixture
def speaking(monkeypatch):
    """Swap the provider the reply pass resolves, leaving every other pass on
    echo so the rest of the turn behaves normally."""
    from app.passes import scheduler as sched_mod

    real = sched_mod.provider_for_tier

    def use(output, thinking=""):
        def fake(tier, settings=None):
            if tier == "blocking":
                return Misbehaving(output, thinking)
            return real(tier, settings)

        monkeypatch.setattr(sched_mod, "provider_for_tier", fake)

    return use


def test_a_front_loaded_contract_still_produces_a_message(db, chat, character, sched, speaking):
    """The reported symptom, end to end: the reply is now stored instead of
    being replaced by an ellipsis."""
    from app import repo
    from tests.conftest import events_of, sync, turn

    speaking(f"{MARKER}{PAYLOAD}\n{REPLY}")
    events = sync(turn(sched, chat["id"], "hello"))

    assert events_of(events, "reply"), events
    last = repo.list_messages(db, chat["id"])[-1]
    assert last["role"] == "assistant"
    assert "Sit wherever" in last["text"]
    assert last["text"].strip() != "…"


def test_an_empty_reply_fails_the_turn_rather_than_saying_nothing(
    db, chat, character, sched, speaking
):
    """No message is written at all. The transcript ends on the user's line,
    which is what the retry affordance answers."""
    from app import repo
    from tests.conftest import events_of, sync, turn

    speaking("<think>I should say something dry and in character here")
    events = sync(turn(sched, chat["id"], "hello"))

    assert not events_of(events, "reply")
    errors = events_of(events, "error")
    assert errors, events
    assert "Max tokens" in errors[0]["error"]

    history = repo.list_messages(db, chat["id"])
    assert history[-1]["role"] == "user", "nothing was invented to fill the gap"
    assert not any(m["text"].strip() == "…" for m in history)


def test_reasoning_off_the_stream_is_still_reported_as_reasoning(
    db, chat, character, sched, speaking
):
    """Ollama never streams the think block — it hands it back on the result.
    Reading only the stream made the commonest failure a reasoning model has
    ("it thought until the budget ran out") report as "nothing at all", which
    sends someone to check the model is loaded when the model was fine."""
    from tests.conftest import events_of, sync, turn

    speaking("", thinking="I should say something dry and in character here")
    events = sync(turn(sched, chat["id"], "hello"))

    errors = events_of(events, "error")
    assert errors, events
    assert "reasoning" in errors[0]["error"]
    assert "nothing at all" not in errors[0]["error"]


def test_tokens_generated_but_none_handed_over_says_so():
    """The hardest one to diagnose: Ollama answers 200, reports six hundred
    tokens of work, and returns an empty string, because its own parser kept
    the reasoning and the reply never started. "Nothing at all" sends someone
    to check the model is loaded — the count proves it was."""
    why = _why_empty("", "", "", used=600, budget=600)
    assert "600 tokens" in why
    assert "nothing at all" not in why
    assert "Thinking" in why


def test_a_backend_that_reports_no_work_is_still_the_old_answer():
    """Zero tokens really is "check the model is loaded"."""
    assert "nothing at all" in _why_empty("", "", "", used=0, budget=600)


class Flaky:
    """Nothing the first time, a reply the second — a small reasoning model on
    an ordinary turn."""

    name = "flaky"
    model = "flaky-1"
    sees_images = False

    def __init__(self, output: str) -> None:
        self.output = output
        self.asked = []

    async def stream(self, request, sink=None):
        if sink is not None:
            sink.provider, sink.model = self.name, self.model
            sink.thinking = "thinking, and then forgetting to answer"
        self.asked.append(request)
        return
        yield ""  # pragma: no cover - makes this an async generator

    async def generate(self, request):
        from app.providers import GenResult

        self.asked.append(request)
        return GenResult(text=self.output, provider=self.name, model=self.model)

    async def aclose(self):
        return None


@pytest.fixture
def flaky(monkeypatch):
    from app.passes import scheduler as sched_mod

    real = sched_mod.provider_for_tier
    made = {}

    def use(output):
        provider = Flaky(output)
        made["provider"] = provider

        def fake(tier, settings=None):
            return provider if tier == "blocking" else real(tier, settings)

        monkeypatch.setattr(sched_mod, "provider_for_tier", fake)
        return provider

    return use


def test_an_empty_first_attempt_is_tried_once_more(db, chat, character, sched, flaky):
    """Reported against GLM-4.7-flash: every few turns it reasons and stops.
    The setup is fine — the same prompt works next time — so failing the turn
    sends someone to fix something that is not broken."""
    from app import repo
    from tests.conftest import events_of, sync, turn

    provider = flaky(REPLY)
    events = sync(turn(sched, chat["id"], "hello"))

    assert events_of(events, "reply"), events
    assert "Sit wherever" in repo.list_messages(db, chat["id"])[-1]["text"]


def test_the_second_attempt_asks_for_no_reasoning(db, chat, character, sched, flaky):
    """Not the same request again: reasoning is what ate the first one, and a
    retry that changes nothing is a retry that fails the same way."""
    from tests.conftest import sync, turn

    provider = flaky(REPLY)
    sync(turn(sched, chat["id"], "hello"))

    assert len(provider.asked) == 2
    assert provider.asked[0].think is None
    assert provider.asked[1].think is False
    assert provider.asked[1].stream is False


def test_two_empty_attempts_still_fail_the_turn(db, chat, character, sched, flaky):
    from app import repo
    from tests.conftest import events_of, sync, turn

    flaky("")
    events = sync(turn(sched, chat["id"], "hello"))

    assert not events_of(events, "reply")
    assert events_of(events, "error")
    assert repo.list_messages(db, chat["id"])[-1]["role"] == "user"


def test_the_failed_turn_is_retryable(db, chat, character, sched, speaking):
    """The two halves have to fit together: an empty reply leaves exactly the
    state `retry_turn` exists to resolve — a user message with nothing after
    it. What is checked is that the retry *takes up* that message, not that it
    succeeds; the stub is still broken, so failing again is correct."""
    from tests.conftest import drain, events_of, sync, turn

    speaking("")
    sync(turn(sched, chat["id"], "hello"))
    events = sync(drain(sched.retry_turn(chat["id"])))

    assert events_of(events, "turn_resume"), "it found the message to answer"
    assert not any(
        "nothing waiting" in e["error"] for e in events_of(events, "error")
    ), events


def test_a_budget_that_was_fully_spent_is_stated_as_fact():
    """"Raise max tokens" is a guess until the numbers say so. When the model
    used the whole allowance, the message stops hedging."""
    why = _why_empty("<think>" + "x" * 900, "x" * 900, "", used=597, budget=600)
    assert "all 600 tokens" in why


def test_a_budget_with_room_left_does_not_claim_it_ran_out():
    """The model stopped early for some other reason — saying it ran out would
    send someone to change a setting that was not the problem."""
    why = _why_empty("<think>x", "x", "", used=30, budget=600)
    assert "tokens reasoning" not in why
    assert "Max tokens" in why
