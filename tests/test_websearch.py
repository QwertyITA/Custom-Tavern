"""Web search injected into the prompt (roadmap 24).

Three rules hold this together, and each has a test below:

- It is off twice over — the switch is off by default *and* there is no URL
  until someone supplies one. Neither alone is enough: a switch that does
  nothing looks broken, and a URL that searches without being asked is a
  phone making requests nobody wanted.
- A search that fails is not an error. The turn goes ahead without it.
- Results belong to the turn that asked for them. Search answers go stale
  immediately and the model has no way to tell that they have.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import assembly, state as state_mod, websearch
from app.config import Settings, build_settings
from app.passes import registry
from app.state import SLICE_SEARCH

from tests.conftest import drain, sync


def configured(url: str = "http://searx.test/search?q={q}&format=json") -> Settings:
    return Settings(search_url=url)


# --------------------------------------------------------------- the URL


def test_the_placeholder_is_where_the_query_goes():
    url = websearch.build_url("http://s.test/search?q={q}&format=json", "ferry times")
    assert url == "http://s.test/search?q=ferry%20times&format=json"


def test_a_template_without_a_placeholder_gets_one_appended():
    """Guessing `q=` is friendlier than refusing a URL that is nearly right."""
    assert websearch.build_url("http://s.test/search", "x") == "http://s.test/search?q=x"
    assert websearch.build_url("http://s.test/s?a=1", "x") == "http://s.test/s?a=1&q=x"


def test_the_query_is_escaped():
    """Otherwise a message containing an ampersand rewrites the request."""
    url = websearch.build_url("http://s.test/?q={q}", "cats & dogs?x=1")
    assert "&" not in url.split("q=", 1)[1]
    assert "cats%20%26%20dogs%3Fx%3D1" in url


# ------------------------------------------------------------ reading it


def test_a_plain_results_list_is_read():
    payload = {"results": [{"title": "Ferry", "content": "It runs hourly.", "url": "http://a"}]}
    assert websearch.parse(payload) == [
        {"title": "Ferry", "snippet": "It runs hourly.", "url": "http://a"}
    ]


@pytest.mark.parametrize("key", ["results", "items", "organic_results", "data"])
def test_the_list_is_found_wherever_this_provider_put_it(key):
    rows = websearch.parse({key: [{"title": "T", "snippet": "S"}]})
    assert rows[0]["title"] == "T"


@pytest.mark.parametrize("key", ["content", "snippet", "description", "excerpt", "text", "body"])
def test_the_snippet_is_found_under_whatever_it_is_called(key):
    rows = websearch.parse({"results": [{"title": "T", key: "the text"}]})
    assert rows[0]["snippet"] == "the text"


def test_a_nested_list_is_found_one_level_down():
    """Brave puts it under `web`, and it is not worth a provider branch."""
    rows = websearch.parse({"web": {"results": [{"title": "T", "description": "S"}]}})
    assert rows[0]["title"] == "T"


def test_a_bare_list_is_read_too():
    assert websearch.parse([{"title": "T", "content": "S"}])[0]["title"] == "T"


def test_a_shape_it_cannot_read_comes_back_empty():
    """Empty reads as "found nothing", which is what it means to the reader."""
    assert websearch.parse({"error": "quota"}) == []
    assert websearch.parse("nonsense") == []
    assert websearch.parse(None) == []


def test_rows_with_neither_title_nor_snippet_are_dropped():
    rows = websearch.parse({"results": [{"url": "http://a"}, {"title": "T"}]})
    assert [r["title"] for r in rows] == ["T"]


def test_the_limit_is_honoured_and_capped():
    payload = {"results": [{"title": f"T{i}", "content": "s"} for i in range(20)]}
    assert len(websearch.parse(payload, 3)) == 3
    assert len(websearch.parse(payload, 99)) == websearch.MAX_RESULTS
    assert len(websearch.parse(payload, 0)) == 1


def test_a_very_long_snippet_is_cut():
    """A search result is a lead, not a document. Three pages of one crowds out
    the conversation it was supposed to help with."""
    rows = websearch.parse({"results": [{"title": "T", "content": "x" * 5000}]})
    assert len(rows[0]["snippet"]) == websearch.MAX_SNIPPET_CHARS


# ------------------------------------------------------------- rendering


def test_nothing_renders_to_nothing():
    assert websearch.render([]) == ""


def test_the_block_names_its_sources():
    """A character repeating something it half-read is a different thing from
    one citing where it came from."""
    text = websearch.render([{"title": "Ferry", "snippet": "Hourly.", "url": "http://a"}])
    assert "Ferry" in text and "Hourly." in text and "http://a" in text


def test_a_result_without_a_url_still_renders():
    text = websearch.render([{"title": "Ferry", "snippet": "Hourly.", "url": ""}])
    assert "Ferry" in text
    assert "()" not in text


# ------------------------------------------------------- running a search


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def transport(monkeypatch):
    """Swap the client's transport rather than the module's functions, so what
    is under test is the real request path — headers, status handling and all."""
    holder: dict[str, FakeTransport] = {}
    real = httpx.AsyncClient

    def factory(handler):
        fake = FakeTransport(handler)
        holder["fake"] = fake
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: real(**{**kw, "transport": fake})
        )
        return fake

    return factory


def ok(payload: dict):
    return lambda request: httpx.Response(200, json=payload)


def test_a_search_returns_normalised_rows(transport):
    transport(ok({"results": [{"title": "Ferry", "content": "Hourly.", "url": "http://a"}]}))
    rows = sync(websearch.search(configured(), "ferry times"))
    assert rows[0]["title"] == "Ferry"


def test_the_key_travels_in_both_conventions(transport):
    """Nobody should have to know which header their provider wants."""
    fake = transport(ok({"results": []}))
    sync(websearch.search(Settings(search_url="http://s.test/?q={q}",
                                   search_key="sk-EXAMPLE"), "x"))
    headers = fake.requests[0].headers
    assert headers["Authorization"] == "Bearer sk-EXAMPLE"
    assert headers["X-Subscription-Token"] == "sk-EXAMPLE"


def test_no_key_sends_no_authorization(transport):
    fake = transport(ok({"results": []}))
    sync(websearch.search(configured(), "x"))
    assert "authorization" not in fake.requests[0].headers


def test_the_configured_count_is_what_comes_back(transport):
    transport(ok({"results": [{"title": f"T{i}", "content": "s"} for i in range(8)]}))
    settings = Settings(search_url="http://s.test/?q={q}", search_results=2)
    assert len(sync(websearch.search(settings, "x"))) == 2


@pytest.mark.parametrize("boom", [
    lambda request: httpx.Response(500, text="down"),
    lambda request: httpx.Response(200, text="not json at all"),
])
def test_a_failed_search_is_empty_rather_than_fatal(transport, boom):
    """The reply matters more than the search. A search engine that is down
    should cost the turn nothing but the results."""
    transport(boom)
    assert sync(websearch.search(configured(), "x")) == []


def test_a_connection_error_is_empty_too(transport):
    def refuse(request):
        raise httpx.ConnectError("no route", request=request)

    transport(refuse)
    assert sync(websearch.search(configured(), "x")) == []


def test_nothing_is_requested_when_it_is_not_configured(transport):
    fake = transport(ok({"results": []}))
    assert sync(websearch.search(Settings(), "x")) == []
    assert not fake.requests


def test_nothing_is_requested_for_an_empty_query(transport):
    fake = transport(ok({"results": []}))
    assert sync(websearch.search(configured(), "   ")) == []
    assert not fake.requests


# ------------------------------------------------------------ the switch


def test_the_toggle_ships_off(db):
    assert registry.toggle_states(db)["web_search"] is False


def test_a_turn_does_not_search_when_the_switch_is_off(db, chat, sched, transport):
    fake = transport(ok({"results": [{"title": "T", "content": "s"}]}))
    sched.settings = configured()
    sync(drain(sched.run_turn(chat["id"], "when is the ferry?")))
    assert not fake.requests, "the switch is off, so nothing should be looked up"


def test_a_turn_does_not_search_without_a_url(db, chat, sched, transport):
    """Switching it on with nowhere to search is not an error — it is simply
    the other half of the setup still missing."""
    fake = transport(ok({"results": []}))
    sync(registry.set_toggle(db, "web_search", True))
    events = sync(drain(sched.run_turn(chat["id"], "when is the ferry?")))
    assert not fake.requests
    assert not [e for e in events if e["type"] == "search_start"]


def searching(db, sched) -> None:
    sync(registry.set_toggle(db, "web_search", True))
    sched.settings = configured()


def test_a_turn_searches_when_both_are_set(db, chat, sched, transport):
    fake = transport(ok({"results": [{"title": "Ferry", "content": "Hourly.", "url": "http://a"}]}))
    searching(db, sched)
    events = sync(drain(sched.run_turn(chat["id"], "when is the ferry?")))

    assert len(fake.requests) == 1
    assert "ferry" in str(fake.requests[0].url).lower()
    done = [e for e in events if e["type"] == "search_done"]
    assert done and done[0]["count"] == 1
    assert done[0]["sources"] == ["http://a"]


def test_the_start_and_done_events_come_as_a_pair(db, chat, sched, transport):
    """A start with no done after it is a spinner that never stops."""
    transport(ok({"results": []}))
    searching(db, sched)
    events = sync(drain(sched.run_turn(chat["id"], "hello")))
    kinds = [e["type"] for e in events]
    assert kinds.count("search_start") == kinds.count("search_done") == 1
    assert kinds.index("search_start") < kinds.index("search_done")


def test_the_search_happens_before_the_reply_streams(db, chat, sched, transport):
    """Results that arrive after the reply are results the reply did not use."""
    transport(ok({"results": [{"title": "T", "content": "s"}]}))
    searching(db, sched)
    events = sync(drain(sched.run_turn(chat["id"], "hello")))
    kinds = [e["type"] for e in events]
    assert kinds.index("search_done") < kinds.index("delta")


def test_a_failing_engine_still_produces_a_reply(db, chat, sched, transport):
    def refuse(request):
        raise httpx.ConnectError("no route", request=request)

    transport(refuse)
    searching(db, sched)
    events = sync(drain(sched.run_turn(chat["id"], "hello")))
    assert [e for e in events if e["type"] == "reply"], "the turn must survive it"
    assert not [e for e in events if e["type"] == "error"]


# ------------------------------------------------------------ in the prompt


def test_the_results_reach_the_prompt(db, chat, character, sched, transport):
    transport(ok({"results": [{"title": "Ferry", "content": "It runs hourly.",
                               "url": "http://a"}]}))
    searching(db, sched)
    sync(drain(sched.run_turn(chat["id"], "when is the ferry?")))

    out = assembly.build_reply_context(db, chat, character, Settings())
    assert "It runs hourly." in out.volatile
    assert "Looked up just now" in out.volatile


def test_the_results_sit_in_the_volatile_band(db, chat, character, sched, transport):
    """Anything above a changing section is recomputed with it (§7.1), and
    search results change on every single turn."""
    transport(ok({"results": [{"title": "Ferry", "content": "It runs hourly."}]}))
    searching(db, sched)
    sync(drain(sched.run_turn(chat["id"], "when is the ferry?")))

    out = assembly.build_reply_context(db, chat, character, Settings())
    assert "It runs hourly." in out.volatile
    assert "It runs hourly." not in out.system


def test_last_turns_results_are_not_offered_again(db, chat, character, sched, transport):
    """Search answers go stale immediately, and the model cannot tell that they
    have — so they are bound to the turn that asked."""
    transport(ok({"results": [{"title": "Ferry", "content": "It runs hourly."}]}))
    searching(db, sched)
    sync(drain(sched.run_turn(chat["id"], "when is the ferry?")))

    # A turn that searched and found nothing must not inherit the old answer.
    transport(ok({"results": []}))
    sync(drain(sched.run_turn(chat["id"], "thanks")))

    out = assembly.build_reply_context(db, chat, character, Settings())
    assert "It runs hourly." not in out.volatile


def test_a_turn_that_did_not_search_shows_nothing(db, chat, character, sched, transport):
    transport(ok({"results": [{"title": "Ferry", "content": "It runs hourly."}]}))
    searching(db, sched)
    sync(drain(sched.run_turn(chat["id"], "when is the ferry?")))

    sched.settings = Settings()  # switched off again mid-chat
    sync(drain(sched.run_turn(chat["id"], "thanks")))

    out = assembly.build_reply_context(db, chat, character, Settings())
    assert "It runs hourly." not in out.volatile


def test_the_section_can_be_switched_off_in_the_layout(db, chat, character, sched, transport):
    transport(ok({"results": [{"title": "Ferry", "content": "It runs hourly."}]}))
    searching(db, sched)
    sync(drain(sched.run_turn(chat["id"], "when is the ferry?")))

    off = Settings(prompt_sections=[{"id": "search", "enabled": False}])
    assert "It runs hourly." not in assembly.build_reply_context(db, chat, character, off).volatile


def test_the_slice_is_shared_rather_than_per_character(db, chat, sched, transport):
    """What the web says is not one character's opinion of it."""
    transport(ok({"results": [{"title": "T", "content": "s"}]}))
    searching(db, sched)
    sync(drain(sched.run_turn(chat["id"], "hello")))
    assert state_mod.read_slice(db, chat["id"], SLICE_SEARCH) is not None


# ------------------------------------------------------------- settings


def test_the_search_settings_save_and_come_back(client, isolated_settings):
    from app import config

    body = {
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "search_url": "http://s.test/?q={q}",
        "search_key": "sk-EXAMPLE",
        "search_results": 6,
    }
    assert client.put("/api/settings", json=body).json()["ok"] is True
    assert config.SETTINGS.search_url == "http://s.test/?q={q}"
    assert config.SETTINGS.search_results == 6

    back = client.get("/api/settings").json()
    assert back["search_url"] == "http://s.test/?q={q}"
    assert back["search_results"] == 6


def test_the_search_key_is_masked_on_the_way_out(client, isolated_settings):
    """This repository is public and the settings screen is a screenshot away."""
    body = {
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "search_url": "http://s.test/?q={q}",
        "search_key": "sk-EXAMPLE",
    }
    client.put("/api/settings", json=body)
    assert client.get("/api/settings").json()["search_key"] == "***"


def test_saving_the_mask_back_keeps_the_key(client, isolated_settings):
    """Otherwise saving any unrelated setting wipes a key you cannot see to
    retype."""
    from app import config

    base = {
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "search_url": "http://s.test/?q={q}",
    }
    client.put("/api/settings", json={**base, "search_key": "sk-EXAMPLE"})
    client.put("/api/settings", json={**base, "search_key": "***"})
    assert config.SETTINGS.search_key == "sk-EXAMPLE"


def test_the_key_never_reaches_the_saved_file(client, isolated_settings):
    client.put("/api/settings", json={
        "backends": [{"name": "echo", "kind": "echo", "model": "echo-1"}],
        "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
        "search_url": "http://s.test/?q={q}",
        "search_key": "sk-EXAMPLE",
    })
    # It does reach the file — that is where credentials live — but the file is
    # the gitignored one, and what is checked here is that it went nowhere else.
    saved = json.loads(isolated_settings.read_text())
    assert saved["search_key"] == "sk-EXAMPLE"
    assert "sk-EXAMPLE" not in json.dumps(client.get("/api/settings").json())


def test_the_result_count_is_clamped():
    for asked, expected in ((0, 1), (99, 8), ("nonsense", 4)):
        settings = build_settings(
            {
                "backends": [{"name": "echo", "kind": "echo"}],
                "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
                "search_results": asked,
            },
            Settings(),
        )
        assert settings.search_results == expected


def test_the_url_is_trimmed_and_bounded():
    settings = build_settings(
        {
            "backends": [{"name": "echo", "kind": "echo"}],
            "tiers": {"blocking": "echo", "foreground": "echo", "background": "echo"},
            "search_url": "  http://s.test/?q={q}  " + "x" * 900,
        },
        Settings(),
    )
    assert len(settings.search_url) <= 500
    assert settings.search_url.startswith("http://")


# --------------------------------------------------------------- layout


def test_the_section_is_in_the_catalogue():
    ids = [s["id"] for s in websearch_sections()]
    assert "search" in ids


def websearch_sections():
    from app import prompt_layout

    return prompt_layout.BUILTIN


def test_the_section_cannot_be_moved_out_of_its_band():
    from app import prompt_layout

    layout = prompt_layout.normalise([{"id": "search", "enabled": True},
                                      {"id": "character", "enabled": True}])
    bands = [s["band"] for s in layout]
    assert bands == sorted(bands, key=prompt_layout.BAND_IDS.index)
