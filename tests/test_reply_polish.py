"""post_process's own module: the LLM copy-edit of a finished reply
(§ app/reply_polish.py) — the module in isolation, its scheduler wiring
(holding deltas back until it is done, for both a new reply and a swipe),
and draft_text's repo/API round-trip (the "Restore original draft" undo,
independent of full_text's own "Restore full length").
"""

from __future__ import annotations

from dataclasses import replace

from app import repo, reply_polish
from app.config import SETTINGS, BackendConfig, Settings
from app.passes import registry
from app.providers.base import GenRequest, GenResult, ProviderError
from app.providers.echo import EchoProvider

from .conftest import events_of, sync, turn

POST_PROCESS = next(p for p in registry.CANONICAL_PASSES if p.id == "post_process")


def echo_provider() -> EchoProvider:
    return EchoProvider(BackendConfig(name="echo", kind="echo", model="echo-1"))


class _Broken:
    """A backend that always fails — the shape reply_polish has to survive."""

    name = "broken"
    model = "broken-1"

    async def generate(self, request: GenRequest) -> GenResult:
        raise ProviderError("the backend is not answering")


# --------------------------------------------------------------- run()


def test_an_empty_draft_is_returned_untouched():
    result = sync(reply_polish.run(echo_provider(), POST_PROCESS, "", "Mira", [], 5.0))
    assert result == ""


def test_a_provider_failure_falls_back_to_the_draft():
    """Best-effort only — polish must never be the reason a turn fails."""
    draft = "She frowns and crosses her arms. " * 20
    result = sync(reply_polish.run(_Broken(), POST_PROCESS, draft, "Mira", [], 5.0))
    assert result == draft


def test_a_response_too_different_in_size_is_rejected():
    """The echo backend answers with a fixed wrapper around the whole prompt
    it was given — for a short draft that wrapper dwarfs it, well past the
    growth this treats as "still an edit" rather than "wrote something
    else"."""
    draft = "Hi."
    result = sync(reply_polish.run(echo_provider(), POST_PROCESS, draft, "Mira", [], 5.0))
    assert result == draft


def test_a_response_within_bounds_is_accepted():
    """Long enough that the same fixed wrapper is a small fraction of the
    whole, which is what makes it read as an edit rather than a rewrite."""
    draft = "She frowns and crosses her arms. " * 20
    result = sync(reply_polish.run(echo_provider(), POST_PROCESS, draft, "Mira", [], 5.0))
    assert result != draft
    # echo's canned wrapper quotes back what it was given, trailing space and
    # all stripped the same way a real model's own output would be.
    assert draft.strip() in result


def test_length_and_pov_targets_reach_the_model_when_present():
    """Pulled straight out of the already-assembled parts (§ _context), not
    re-read from settings — so this can never quote a different target than
    the one the draft was actually written against."""
    parts = [
        {"id": "craft:pov", "text": "Third person for the room."},
        {"id": "craft:length", "text": "1 to 2 paragraphs, roughly 100 to 200 words."},
    ]
    draft = "She frowns and crosses her arms. " * 20
    result = sync(reply_polish.run(echo_provider(), POST_PROCESS, draft, "Mira", parts, 5.0))
    assert "Third person for the room." in result
    assert "1 to 2 paragraphs" in result


def test_no_length_or_pov_section_means_no_target_is_invented():
    draft = "She frowns and crosses her arms. " * 20
    result = sync(reply_polish.run(echo_provider(), POST_PROCESS, draft, "Mira", [], 5.0))
    assert "Point of view" not in result
    assert "Length target" not in result


# --------------------------------------------------------------------- repo


def test_add_message_stores_draft_text_only_when_given_one(db, character, chat):
    polished = repo.add_message(db, chat["id"], "assistant", "Fixed.", draft_text="fxied.")
    assert polished["has_draft_text"] is True

    untouched = repo.add_message(db, chat["id"], "assistant", "Just this.")
    assert untouched["has_draft_text"] is False


def test_restore_draft_text_puts_it_back_and_clears_itself(db, character, chat):
    message = repo.add_message(db, chat["id"], "assistant", "Fixed.", draft_text="fxied.")
    restored = repo.restore_draft_text(db, message["variant_id"])
    assert restored == "fxied."

    again = repo.get_message(db, message["id"])
    assert again["text"] == "fxied."
    assert again["has_draft_text"] is False

    # Nothing left to restore a second time.
    assert repo.restore_draft_text(db, message["variant_id"]) is None


def test_restore_draft_text_on_an_untouched_variant_is_a_no_op(db, character, chat):
    message = repo.add_message(db, chat["id"], "assistant", "Just this.")
    assert repo.restore_draft_text(db, message["variant_id"]) is None


def test_full_text_and_draft_text_restore_independently(db, character, chat):
    """A reply post_process rewrote, that the length backstop then also cut,
    carries both — and each undoes only its own step."""
    message = repo.add_message(
        db, chat["id"], "assistant", "Short.",
        full_text="Short.\n\nCut paragraph.", draft_text="The model's own first draft.",
    )
    assert message["has_full_text"] is True
    assert message["has_draft_text"] is True

    assert repo.restore_draft_text(db, message["variant_id"]) == "The model's own first draft."
    still_there = repo.get_message(db, message["id"])
    assert still_there["has_full_text"] is True
    assert still_there["text"] == "The model's own first draft."


# --------------------------------------------------------------------- API


def test_restore_draft_route_puts_the_draft_back(client, db, character, chat):
    message = repo.add_message(db, chat["id"], "assistant", "Fixed.", draft_text="fxied.")
    response = client.post(f"/api/messages/{message['id']}/restore-draft")
    assert response.status_code == 200
    assert response.json()["text"] == "fxied."


def test_restore_draft_route_404s_for_an_unknown_message(client):
    response = client.post("/api/messages/nope/restore-draft")
    assert response.status_code == 404


def test_restore_draft_route_400s_when_nothing_was_touched(client, db, character, chat):
    message = repo.add_message(db, chat["id"], "assistant", "Just this.")
    response = client.post(f"/api/messages/{message['id']}/restore-draft")
    assert response.status_code == 400


# ------------------------------------------------------- through a real turn


def test_post_process_holds_deltas_back_by_default(sched, chat):
    """Nothing streams live while the tier is on — the reveal is one delta
    carrying the whole (possibly unchanged) reply, once post_process has had
    its turn, not the word-at-a-time stream a raw generation produces."""
    events = sync(turn(sched, chat["id"], "Cold out."))
    deltas = events_of(events, "delta")
    assert len(deltas) == 1

    message = events_of(events, "reply")[0]["message"]
    assert deltas[0]["text"] == message["text"]


def test_turning_the_tier_off_streams_live_as_before(db, chat):
    """§ the "Without post process" flow: switching the tier off is switching
    post_process off, and the reply goes back to arriving live."""
    from app.passes.scheduler import PassScheduler

    off = PassScheduler(db, replace(SETTINGS, tiers_off=["foreground"]))
    events = sync(turn(off, chat["id"], "Cold out."))
    assert len(events_of(events, "delta")) > 1


def test_a_real_turn_can_be_edited_and_the_draft_restored(sched, chat, monkeypatch):
    """The scheduler wiring itself, both call sites — echo's own canned reply
    is too short relative to the full prompt post_process is given (its own
    length/POV target text included) to pass the accept-an-edit bounds
    reliably on its own, so post_process's own call is pinned to a fixed,
    deterministic answer instead. What that call is *given* is not
    reconstructed here — this is only checking that its answer reaches the
    stored message and that draft_text is what it replaced."""
    from app.providers import echo

    edited = "A polished version of the reply, a little longer than the draft it replaces."
    original_compose = echo.EchoProvider._compose

    def fake_compose(self, request):
        if request.pass_id == "post_process":
            return edited
        return original_compose(self, request)

    monkeypatch.setattr(echo.EchoProvider, "_compose", fake_compose)

    events = sync(turn(sched, chat["id"], "Cold out."))
    deltas = events_of(events, "delta")
    assert len(deltas) == 1
    assert deltas[0]["text"] == edited

    message = events_of(events, "reply")[0]["message"]
    assert message["text"] == edited
    assert message["has_draft_text"] is True

    stored = repo.get_message(sched.db, message["id"])
    assert stored["text"] == edited
    restored = repo.restore_draft_text(sched.db, stored["variant_id"])
    assert restored != edited
    assert restored  # the model's own original draft, whatever echo wrote for "basic"


def test_a_swipe_is_polished_the_same_way(sched, chat, monkeypatch):
    """The second call site (§ _run_swipe) gets the identical treatment."""
    from app.providers import echo

    edited = "A polished swipe, a little longer than the draft it replaces here."
    original_compose = echo.EchoProvider._compose

    def fake_compose(self, request):
        if request.pass_id == "post_process":
            return edited
        return original_compose(self, request)

    first = sync(turn(sched, chat["id"], "Cold out."))
    message_id = events_of(first, "reply")[0]["message"]["id"]

    monkeypatch.setattr(echo.EchoProvider, "_compose", fake_compose)

    async def do_swipe():
        return [e async for e in sched.run_swipe(message_id)]

    events = sync(do_swipe())
    deltas = events_of(events, "delta")
    assert len(deltas) == 1
    assert deltas[0]["text"] == edited

    variant = events_of(events, "variant")[0]["variant"]
    assert variant["text"] == edited
    assert variant["has_draft_text"] is True


def test_stopping_while_post_process_is_running_still_keeps_the_draft(sched, chat, monkeypatch):
    """A cancellation reaching mid-polish must not lose the turn — the draft
    post_process was given is stored exactly as generated, the same
    guarantee stopping mid-stream already gives (§ test_api.py)."""
    import asyncio

    from app.providers import echo

    async def hangs_forever(*args, **kwargs):
        await asyncio.sleep(999)
        raise AssertionError("never reached")

    monkeypatch.setattr(reply_polish, "run", hangs_forever)

    async def drive():
        seen = []
        task = asyncio.ensure_future(_collect(sched, chat["id"], seen))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if seen:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return seen

    async def _collect(sched, chat_id, seen):
        async for event in sched.run_turn(chat_id, "Cold out."):
            seen.append(event)

    sync(drive())

    replies = [m for m in repo.list_messages(sched.db, chat["id"]) if m["role"] == "assistant"]
    assert replies, "the draft was thrown away"
    assert replies[-1]["text"].strip()

    runs = [dict(r) for r in sched.db.query(
        "SELECT pass_id, status FROM pass_runs WHERE chat_id=?", (chat["id"],)
    )]
    assert any(r["pass_id"] == "basic" and r["status"] == "stopped" for r in runs), runs
    assert any(r["pass_id"] == "post_process" and r["status"] == "stopped" for r in runs), runs
