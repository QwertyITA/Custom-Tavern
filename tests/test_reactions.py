"""Message reactions: the fixed emoji set, the react/clear endpoint, and
message_reaction — the background-tier pass that generates a short
in-character line noticing one, launched by react_to_message outside the
normal turn cycle (same tracked path run_pass_now already uses)."""

from __future__ import annotations

from app import main as main_module
from app import repo
from app.passes import registry

from .conftest import sync, turn
from .test_scheduler import context


def _reply(sched, chat) -> dict:
    """A real assistant message, via the echo backend, to react to."""
    sync(turn(sched, chat["id"], "hello there"))
    messages = repo.list_messages(sched.db, chat["id"])
    return next(m for m in reversed(messages) if m["role"] == "assistant")


# --------------------------------------------------------------------- repo


def test_a_reaction_round_trips_through_list_and_get(db, chat, character):
    reply = _reply_direct(db, chat, character)
    repo.set_reaction(db, reply["variant_id"], "❤️")
    repo.set_reaction_ack(db, reply["variant_id"], "Warmth spreads through them, unspoken.")

    fetched = repo.get_message(db, reply["id"])
    assert fetched["user_reaction"] == "❤️"
    assert fetched["reaction_ack"] == "Warmth spreads through them, unspoken."

    listed = next(m for m in repo.list_messages(db, chat["id"]) if m["id"] == reply["id"])
    assert listed["user_reaction"] == "❤️"
    assert listed["reaction_ack"] == "Warmth spreads through them, unspoken."


def test_a_reaction_defaults_to_empty(db, chat, character):
    reply = _reply_direct(db, chat, character)
    assert reply["user_reaction"] == ""
    assert reply["reaction_ack"] == ""
    assert repo.get_message(db, reply["id"])["user_reaction"] == ""


def _reply_direct(db, chat, character) -> dict:
    return repo.add_message(db, chat["id"], "assistant", "Hi there.")


# ---------------------------------------------------------------- the pass


def test_message_reaction_never_fires_on_its_own(sched, chat, character):
    definition = next(d for d in registry.all_passes(sched.db) if d.id == "message_reaction")
    assert definition.trigger.type == "manual"
    assert not sched.trigger_fires(definition, context(chat, character))


def test_react_to_message_runs_tracked_and_writes_the_ack(sched, chat, character):
    reply = _reply(sched, chat)

    # react_to_message calls asyncio.create_task internally (same as
    # _launch_background elsewhere in this suite), which needs a loop
    # already running — hence the wrapper, rather than calling it bare.
    async def scenario():
        result = sched.react_to_message(chat["id"], reply["id"], "❤️")
        assert result["ok"] is True
        await sched.await_pending(chat["id"])

    sync(scenario())

    row = sched.db.query_one(
        "SELECT status FROM pass_runs WHERE chat_id=? AND pass_id='message_reaction'",
        (chat["id"],),
    )
    assert row["status"] == "done"
    fetched = repo.get_message(sched.db, reply["id"])
    assert fetched["reaction_ack"], "the echo backend's own reply text, whatever it wrote"


def test_react_to_message_reports_an_unknown_message(sched, chat):
    result = sched.react_to_message(chat["id"], "not-a-real-message", "❤️")
    assert result["ok"] is False


def test_react_to_message_reports_an_unknown_chat(sched):
    result = sched.react_to_message("not-a-real-chat", "not-a-real-message", "❤️")
    assert result["ok"] is False


# ------------------------------------------------------------------- the API


def test_reacting_sets_the_emoji_and_launches_the_ack(client, chat, sched):
    # `sched` only creates the reply to react to; the endpoint itself runs
    # against main.SCHEDULER (a separate PassScheduler the test client's own
    # lifespan builds against the same db), so waiting for its background
    # task has to go through that instance, not this one.
    reply = _reply(sched, chat)

    response = client.post(f"/api/messages/{reply['id']}/react", json={"emoji": "😂"})
    assert response.status_code == 200
    assert response.json()["user_reaction"] == "😂"
    assert repo.get_message(sched.db, reply["id"])["user_reaction"] == "😂"

    sync(main_module.SCHEDULER.await_pending(chat["id"]))
    row = sched.db.query_one(
        "SELECT status FROM pass_runs WHERE chat_id=? AND pass_id='message_reaction'",
        (chat["id"],),
    )
    assert row is not None, "setting a reaction should launch message_reaction"


def test_tapping_the_same_emoji_again_clears_it(client, chat, sched):
    reply = _reply(sched, chat)
    client.post(f"/api/messages/{reply['id']}/react", json={"emoji": "😢"})
    sync(main_module.SCHEDULER.await_pending(chat["id"]))

    response = client.post(f"/api/messages/{reply['id']}/react", json={"emoji": "😢"})
    assert response.json()["user_reaction"] == ""
    assert repo.get_message(sched.db, reply["id"])["user_reaction"] == ""


def test_clearing_a_reaction_does_not_spend_a_model_call(client, chat, sched):
    reply = _reply(sched, chat)
    client.post(f"/api/messages/{reply['id']}/react", json={"emoji": "😮"})
    sync(main_module.SCHEDULER.await_pending(chat["id"]))

    client.post(f"/api/messages/{reply['id']}/react", json={"emoji": None})
    sync(main_module.SCHEDULER.await_pending(chat["id"]))

    count = sched.db.query_one(
        "SELECT COUNT(*) AS c FROM pass_runs WHERE chat_id=? AND pass_id='message_reaction'",
        (chat["id"],),
    )
    assert count["c"] == 1, "the clear itself must not launch a second run"


def test_an_unknown_emoji_is_rejected(client, chat, sched):
    reply = _reply(sched, chat)
    response = client.post(f"/api/messages/{reply['id']}/react", json={"emoji": "🥑"})
    assert response.status_code == 400


def test_reacting_to_an_unknown_message_is_a_404(client):
    response = client.post("/api/messages/not-a-real-message/react", json={"emoji": "❤️"})
    assert response.status_code == 404


def test_only_a_reply_can_be_reacted_to(client, db, chat, character):
    mine = repo.add_message(db, chat["id"], "user", "Hello!")
    response = client.post(f"/api/messages/{mine['id']}/react", json={"emoji": "❤️"})
    assert response.status_code == 400
