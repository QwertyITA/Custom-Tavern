"""Shared fixtures. Every test runs against a throwaway database and the
built-in `echo` backend, so the suite is hermetic and needs no network."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cards, repo  # noqa: E402
from app.config import SETTINGS  # noqa: E402
from app.db import Database, set_db  # noqa: E402
from app.models import Character, LorebookEntry  # noqa: E402
from app.passes import registry  # noqa: E402
from app.passes.scheduler import PassScheduler  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def pristine_settings(monkeypatch):
    """Every test starts from stock settings, not the developer's own file.

    `config.SETTINGS` is loaded from `data/settings.json` at import, so without
    this a suite that calls itself hermetic quietly depends on whatever the
    person running it last saved in the GUI — which is how a prompt-layout
    change made in a browser ends up failing an unrelated assembly test on one
    machine and passing on every other.
    """
    from app import config, main

    original = config.SETTINGS
    # Both places settings live. `config.SETTINGS` is what the routes read and
    # `scheduler.settings` is what every turn runs against, and resetting only
    # the first leaves a scheduler still holding whatever the previous test
    # saved — which is how a translation configured in one test showed up in
    # another that had never asked for one.
    def _use(settings) -> None:
        config.apply_settings(settings)
        if main.SCHEDULER is not None:
            main.SCHEDULER.settings = settings

    _use(config.Settings())
    monkeypatch.setattr(config, "settings_path", lambda: Path("/nonexistent/settings.json"))
    yield config.SETTINGS
    _use(original)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    set_db(database)
    registry.seed(database)
    yield database
    set_db(None)
    database.close()


@pytest.fixture
def character(db) -> Character:
    card = Character(
        id="testchar",
        name="Mira",
        persona="Dry, observant, warms slowly.",
        first_mes='*She looks up.* "Sit wherever."',
        nudges=[
            {"pattern": r"\bthanks\b", "variable": "willingness", "delta": 1},
            {"pattern": r"\bliar\b", "variable": "trust", "delta": -2},
        ],
        lorebook=[
            LorebookEntry(keys=["tavern"], content="The tavern is the Long Wait.", constant=True),
            LorebookEntry(keys=["Harrow"], content="Harrow is the harbourmaster."),
        ],
        pfp_set={"neutral": "n.png", "happy": "h.png"},
        backgrounds=[{"id": "tavern_interior", "img": "t.jpg"}],
    )
    repo.save_character(db, card)
    return card


@pytest.fixture
def chat(db, character) -> dict:
    return repo.create_chat(db, character.id, "test chat")


@pytest.fixture
def sched(db) -> PassScheduler:
    return PassScheduler(db, SETTINGS)


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point the save path at a temp file — never the developer's real one."""
    from app import config

    original = config.SETTINGS
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "settings_path", lambda: path)
    yield path
    config.apply_settings(original)


def sync(coro):
    """Run a coroutine from a sync test — keeps pytest-asyncio off the dep list,
    which matters when the dev machine is a phone."""
    return asyncio.run(coro)


async def drain(agen) -> list[dict]:
    """Collect every event an async generator yields."""
    return [event async for event in agen]


async def turn(sched: PassScheduler, chat_id: str, text: str) -> list[dict]:
    """One full turn, then wait for its background passes to settle."""
    events = await drain(sched.run_turn(chat_id, text))
    await sched.await_pending(chat_id, timeout=20)
    return events


def events_of(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e.get("type") == kind]
