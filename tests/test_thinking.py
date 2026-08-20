"""Reasoning: captured, kept, and never shown inline (§5.6).

A thinking model raises the same question every turn — did it actually think,
and what did it decide — and until this existed the app threw away the only
thing that could answer it. The think block was split out of the reply, counted
for the HUD, and dropped.

Two shapes arrive here and both have to end up in the same place: a `<think>`
block inside the stream, which is what a raw completion endpoint gives, and a
separate reasoning channel on the result, which is what Ollama gives once it
has parsed the model itself.
"""

from __future__ import annotations

import pytest

from app import repo
from app.providers import GenResult, ReasoningDelta

from .conftest import drain, events_of, sync, turn

REPLY = '*She looks up.* "Sit wherever."'
THOUGHT = "She is guarded but not hostile. Answer short, do not warm up yet."


class Thinker:
    """A provider that reasons, either out loud or on its own channel."""

    name = "stub"
    model = "stub-1"
    sees_images = False

    def __init__(self, text: str, *, inline: str = "", channel: str = "") -> None:
        self.text = text
        self.inline = inline
        self.channel = channel

    async def stream(self, request, sink=None):
        if sink is not None:
            sink.provider, sink.model = self.name, self.model
            sink.thinking = self.channel
        # The channel arrives as it is thought and lands on the sink at the
        # end, which is what Ollama does — the sink alone would say nothing
        # until the whole thought was over.
        for i in range(0, len(self.channel), 7):
            yield ReasoningDelta(self.channel[i : i + 7])
        # The blank line after the block is what a real one leaves behind, and
        # it is part of what has to be taken away with it.
        body = f"<think>{self.inline}</think>\n\n{self.text}" if self.inline else self.text
        for i in range(0, len(body), 7):
            yield body[i : i + 7]

    async def generate(self, request):
        return GenResult(text=self.text, provider=self.name, model=self.model,
                         thinking=self.channel)

    async def aclose(self):
        return None


@pytest.fixture
def reasoning(monkeypatch):
    """Only the reply pass thinks; every other pass stays on echo."""
    from app.passes import scheduler as sched_mod

    real = sched_mod.provider_for_tier

    def use(**kwargs):
        def fake(tier, settings=None):
            return Thinker(REPLY, **kwargs) if tier == "blocking" else real(tier, settings)

        monkeypatch.setattr(sched_mod, "provider_for_tier", fake)

    return use


# ------------------------------------------------------------- it is kept


def test_a_channel_thought_is_stored_with_the_reply(db, chat, character, sched, reasoning):
    """Ollama parses the model itself and hands the reasoning back beside the
    reply rather than in it."""
    reasoning(channel=THOUGHT)
    sync(turn(sched, chat["id"], "hello"))

    last = repo.list_messages(db, chat["id"])[-1]
    assert last["role"] == "assistant"
    assert repo.thinking_for(db, last["id"])["thinking"] == THOUGHT


def test_an_inline_think_block_is_stored_the_same_way(db, chat, character, sched, reasoning):
    """A raw completion endpoint gives it in the stream instead."""
    reasoning(inline=THOUGHT)
    sync(turn(sched, chat["id"], "hello"))

    last = repo.list_messages(db, chat["id"])[-1]
    assert THOUGHT in repo.thinking_for(db, last["id"])["thinking"]


def test_the_reasoning_never_reaches_the_reply(db, chat, character, sched, reasoning):
    """The whole reason it is hidden: it is not what the character said."""
    reasoning(inline=THOUGHT, channel=THOUGHT)
    sync(turn(sched, chat["id"], "hello"))

    last = repo.list_messages(db, chat["id"])[-1]
    assert "guarded" not in last["text"]
    assert last["text"].strip() == REPLY


def test_a_reply_with_no_reasoning_stores_none(db, chat, character, sched, reasoning):
    reasoning()
    sync(turn(sched, chat["id"], "hello"))

    last = repo.list_messages(db, chat["id"])[-1]
    assert repo.thinking_for(db, last["id"])["thinking"] == ""
    assert last["has_thinking"] is False


def test_the_flag_says_so_without_carrying_the_text(db, chat, character, sched, reasoning):
    """Reasoning routinely runs longer than the reply it produced. The
    transcript carries a boolean; the text is fetched only when asked for."""
    reasoning(channel=THOUGHT)
    sync(turn(sched, chat["id"], "hello"))

    last = repo.list_messages(db, chat["id"])[-1]
    assert last["has_thinking"] is True
    assert THOUGHT not in str(last)


# ------------------------------------------------------- per variant (§9)


def test_a_swipe_keeps_its_own_reasoning(db, chat, character, sched, reasoning):
    """Same rule as the prompt record: a re-roll thought its own way to its own
    answer, and showing the first attempt's reasoning under the third
    attempt's text would be worse than showing nothing."""
    reasoning(channel=THOUGHT)
    sync(turn(sched, chat["id"], "hello"))
    message = repo.list_messages(db, chat["id"])[-1]

    reasoning(channel="Different this time: she has warmed up.")
    events = sync(drain(sched.run_swipe(message["id"])))
    assert events_of(events, "variant"), events

    assert "warmed up" in repo.thinking_for(db, message["id"])["thinking"]


def test_walking_back_to_the_earlier_variant_walks_back_to_its_thoughts(
    client, db, chat, character, sched, reasoning
):
    """The flag has to follow the variant too, or the option offers the wrong
    reply's reasoning after a swipe back."""
    reasoning(channel=THOUGHT)
    sync(turn(sched, chat["id"], "hello"))
    message = repo.list_messages(db, chat["id"])[-1]

    reasoning()  # this one does not think
    sync(drain(sched.run_swipe(message["id"])))
    assert repo.get_message(db, message["id"])["has_thinking"] is False

    first = client.get(f"/api/messages/{message['id']}/variants").json()[0]
    back = client.post(f"/api/messages/{message['id']}/variants/{first['id']}").json()
    assert back["has_thinking"] is True


# --------------------------------------------------------------- the API


def test_the_endpoint_serves_it(client, db, chat, character, sched, reasoning):
    reasoning(channel=THOUGHT)
    sync(turn(sched, chat["id"], "hello"))
    message = repo.list_messages(db, chat["id"])[-1]

    body = client.get(f"/api/messages/{message['id']}/thinking").json()
    assert body["ok"] is True
    assert body["thinking"] == THOUGHT
    assert body["model"] == "stub-1"


def test_the_endpoint_says_why_when_there_is_nothing(client, db, chat, character, sched, reasoning):
    """"No reasoning" has two causes and the message names both, because the
    fix is different: the model does not think, or Thinking is switched off."""
    reasoning()
    sync(turn(sched, chat["id"], "hello"))
    message = repo.list_messages(db, chat["id"])[-1]

    body = client.get(f"/api/messages/{message['id']}/thinking").json()
    assert body["ok"] is False
    assert "Thinking is off" in body["reason"]


def test_an_unknown_message_is_a_404(client, db, chat):
    assert client.get("/api/messages/nope/thinking").status_code == 404


# ------------------------------------------- watching it happen, live (§5.6)

# A reasoning model emits no visible token until it has stopped thinking, so
# for however long that takes it is indistinguishable from a backend that never
# answered — the same three dots either way. These events are what tells them
# apart. They carry a count and never the text: the reasoning is still not for
# the message stream, only the fact that it is happening.


def test_it_says_it_is_thinking_before_a_word_of_the_reply_arrives(db, chat, character, sched,
                                                                  reasoning):
    reasoning(channel=THOUGHT)
    events = sync(turn(sched, chat["id"], "hello"))
    kinds = [e["type"] for e in events if e["type"] in ("reasoning", "delta")]

    assert "reasoning" in kinds
    assert kinds.index("reasoning") < kinds.index("delta")


def test_the_count_grows_as_the_thought_does(db, chat, character, sched, reasoning):
    """What the cue deepens on. A thought that has run for two paragraphs and
    one that has just started should not look the same."""
    reasoning(channel=THOUGHT)
    counts = [e["chars"] for e in events_of(sync(turn(sched, chat["id"], "hello")), "reasoning")]

    assert counts == sorted(counts)
    assert counts[-1] == len(THOUGHT)


def test_the_events_never_carry_the_reasoning_itself(db, chat, character, sched, reasoning):
    reasoning(channel=THOUGHT, inline=THOUGHT)
    for event in events_of(sync(turn(sched, chat["id"], "hello")), "reasoning"):
        assert "guarded" not in str(event)


def test_an_inline_block_is_reported_the_same_way(db, chat, character, sched, reasoning):
    """The other shape: no separate channel, a `<think>` block in the stream."""
    reasoning(inline=THOUGHT)
    events = sync(turn(sched, chat["id"], "hello"))

    assert events_of(events, "reasoning")
    assert events_of(events, "reasoning")[-1]["chars"] == len(THOUGHT)


def test_a_reply_that_does_not_reason_says_nothing(db, chat, character, sched, reasoning):
    """Silence is the signal for "waiting on the backend", and a cue that fires
    on every reply distinguishes nothing."""
    reasoning()
    assert events_of(sync(turn(sched, chat["id"], "hello")), "reasoning") == []


def test_an_inline_block_never_reaches_the_bubble(db, chat, character, sched, reasoning):
    """It used to sit in the bubble for the whole generation and then vanish
    when the finished text landed — visible for exactly as long as it took to
    read, which is the one thing §5.6 says must not happen."""
    reasoning(inline=THOUGHT)
    streamed = "".join(e["text"] for e in events_of(sync(turn(sched, chat["id"], "hello")),
                                                    "delta"))

    assert "guarded" not in streamed
    assert streamed.strip() == REPLY


def test_the_reply_does_not_stream_in_below_the_thought_it_replaced(db, chat, character, sched,
                                                                   reasoning):
    """`</think>\\n\\n` is part of what split_thinking removes. Left in, the
    reply arrives two blank lines down and then jumps up when the stored text
    lands."""
    reasoning(inline=THOUGHT)
    deltas = events_of(sync(turn(sched, chat["id"], "hello")), "delta")

    assert deltas and not deltas[0]["text"][:1].isspace()


def test_a_stopped_reply_keeps_the_reasoning_that_produced_it(db, chat, character, sched,
                                                              monkeypatch):
    """The sink never lands on the stop path — it is filled when the stream
    closes, and a stopped stream does not close. Without the reasoning kept as
    it arrived, a reply someone stopped would be the one reply that thought and
    cannot say so, and it is among the likeliest to be asked about."""
    import asyncio

    from app.passes import scheduler as sched_mod

    class SlowThinker(Thinker):
        async def stream(self, request, sink=None):
            for i in range(0, len(THOUGHT), 7):
                yield ReasoningDelta(THOUGHT[i : i + 7])
            for i in range(0, len(REPLY), 7):
                yield REPLY[i : i + 7]
                await asyncio.sleep(0.05)

    real = sched_mod.provider_for_tier
    monkeypatch.setattr(
        sched_mod, "provider_for_tier",
        lambda tier, settings=None: SlowThinker(REPLY) if tier == "blocking" else real(tier, settings),
    )

    async def run():
        seen: list[dict] = []

        async def consume():
            async for event in sched.run_turn(chat["id"], "hello"):
                seen.append(event)

        task = asyncio.create_task(consume())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if sum(1 for e in seen if e["type"] == "delta") >= 2:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return seen

    seen = sync(run())
    assert events_of(seen, "reasoning"), "it never said it was thinking"

    last = repo.list_messages(db, chat["id"])[-1]
    assert last["role"] == "assistant" and last["text"].strip()
    assert repo.thinking_for(db, last["id"])["thinking"] == THOUGHT


# ---------------------------------------------- splitting it out as it arrives


def chunked(text: str, size: int = 7) -> tuple[str, str]:
    """Run text through the stream filter in chunks, as a backend would."""
    from app.postprocess import ThinkStreamFilter

    watcher = ThinkStreamFilter()
    shown, thought = [], []
    for i in range(0, len(text), size):
        visible, reasoning = watcher.feed(text[i : i + size])
        shown.append(visible)
        thought.append(reasoning)
    visible, reasoning = watcher.finish()
    return "".join(shown) + visible, "".join(thought) + reasoning


@pytest.mark.parametrize(
    "text",
    [
        "plain reply, no tags anywhere",
        "<think>only reasoning, never closed",
        "before <think>the thought</think> after",
        "<THINK>upper case tags</THINK>the reply",
        "a < b and 3 < 4, which is not a tag",
        "<think>one</think>middle<think>two</think>end",
    ],
)
def test_the_stream_filter_splits_where_split_thinking_would(text):
    """Same two halves either way. The filter runs a chunk at a time and
    split_thinking runs on the whole reply, and a reply that changes when it
    lands is the bug this is here to keep out."""
    from app.postprocess import split_thinking

    body, thought = chunked(text)
    want_body, want_thought = split_thinking(text)
    assert (body.strip(), thought.strip()) == (want_body.strip(), want_thought.strip())


@pytest.mark.parametrize("size", [1, 2, 3, 5, 11, 400])
def test_a_tag_split_across_chunks_is_still_a_tag(size):
    """`<think>` arrives in pieces far more often than it arrives whole."""
    body, thought = chunked("<think>hidden</think>said out loud", size)
    assert (body.strip(), thought) == ("said out loud", "hidden")
