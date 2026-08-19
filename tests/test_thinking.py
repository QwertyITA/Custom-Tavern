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
from app.providers import GenResult

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
        body = f"<think>{self.inline}</think>{self.text}" if self.inline else self.text
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
