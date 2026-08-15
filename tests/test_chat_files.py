"""Chat management: rename, search, export and import (§10).

The export is the one place where "a chat is worth more than the app" has to be
true in practice — the messages are what someone spent their evenings on, and
they have to be able to get them out and put them back.
"""

from __future__ import annotations

import json

import pytest

from app import chat_files, repo
from app.db import get_db


def send(client, chat_id: str, text: str) -> None:
    with client.stream("POST", f"/api/chats/{chat_id}/send", json={"text": text}) as response:
        for _ in response.iter_lines():
            pass


def a_chat(client, title: str = "") -> tuple[str, str]:
    character_id = client.get("/api/characters").json()[0]["id"]
    body = {"character_id": character_id}
    if title:
        body["title"] = title
    return character_id, client.post("/api/chats", json=body).json()["id"]


# ------------------------------------------------------------------ renaming


def test_a_chat_can_be_renamed(client):
    _, chat_id = a_chat(client)
    body = client.patch(f"/api/chats/{chat_id}", json={"title": "The lighthouse"}).json()
    assert body["chat"]["title"] == "The lighthouse"
    assert client.get(f"/api/chats/{chat_id}").json()["chat"]["title"] == "The lighthouse"


def test_renaming_does_not_reorder_the_list(client):
    """The list is ordered by when the story last moved. Renaming a chat is not
    the story moving, and having it jump to the top would be a small lie about
    where you left off."""
    _, older = a_chat(client)
    send(client, older, "first")
    _, newer = a_chat(client)
    send(client, newer, "second")

    before = [c["id"] for c in client.get("/api/chats").json()]
    client.patch(f"/api/chats/{older}", json={"title": "Renamed"})
    assert [c["id"] for c in client.get("/api/chats").json()] == before


def test_a_blank_name_is_allowed(client):
    """An untitled chat is a real thing — it is what every new one is."""
    _, chat_id = a_chat(client, "Something")
    client.patch(f"/api/chats/{chat_id}", json={"title": "   "})
    assert client.get(f"/api/chats/{chat_id}").json()["chat"]["title"] == ""


def test_renaming_a_chat_that_is_not_there_is_a_404(client):
    assert client.patch("/api/chats/nope", json={"title": "x"}).status_code == 404


# ------------------------------------------------------------------- search


def test_search_finds_a_chat_by_its_title(client):
    _, chat_id = a_chat(client, "The lighthouse job")
    hits = client.get("/api/chats/search", params={"q": "lighthouse"}).json()
    assert [h["id"] for h in hits] == [chat_id]


def test_search_finds_a_chat_by_something_said_in_it(client):
    """A list of titles is not an answer to a search for a phrase, which is
    most of why this exists."""
    _, chat_id = a_chat(client, "Untitled")
    send(client, chat_id, "Ask about the harbourmaster.")
    hits = client.get("/api/chats/search", params={"q": "harbourmaster"}).json()
    assert chat_id in [h["id"] for h in hits]
    assert "harbourmaster" in next(h for h in hits if h["id"] == chat_id)["hit"].lower()


def test_search_carries_the_character_name(client):
    _, chat_id = a_chat(client, "Findable")
    hit = client.get("/api/chats/search", params={"q": "Findable"}).json()[0]
    assert hit["character_name"]


def test_search_ignores_case(client):
    a_chat(client, "The Lighthouse")
    assert client.get("/api/chats/search", params={"q": "lighthouse"}).json()


def test_an_empty_search_returns_nothing_rather_than_everything(client):
    a_chat(client, "Something")
    for query in ("", "   "):
        assert client.get("/api/chats/search", params={"q": query}).json() == []


def test_search_does_not_match_a_chat_that_never_said_it(client):
    _, chat_id = a_chat(client, "Quiet")
    send(client, chat_id, "nothing relevant here")
    assert client.get("/api/chats/search", params={"q": "zeppelin"}).json() == []


def test_search_survives_wildcards_in_the_query(client):
    """`%` and `_` are LIKE's own wildcards; typing one must not match
    everything."""
    a_chat(client, "Plain title")
    assert client.get("/api/chats/search", params={"q": "%"}).json() == []


def test_search_is_not_shadowed_by_the_chat_id_route(client):
    """Routes match in declaration order, so /api/chats/search sitting below
    /api/chats/{chat_id} would be read as a chat id and 404."""
    assert client.get("/api/chats/search", params={"q": "x"}).status_code == 200


# ------------------------------------------------------------------- export


def test_export_carries_the_messages(client):
    _, chat_id = a_chat(client, "Exportable")
    send(client, chat_id, "Is the ferry running?")

    payload = client.get(f"/api/chats/{chat_id}/export").json()
    assert payload["format"] == chat_files.FORMAT
    assert payload["chat"]["title"] == "Exportable"
    texts = [v["text"] for m in payload["messages"] for v in m["variants"]]
    assert any("ferry" in t for t in texts)


def test_export_carries_the_state_and_the_summary(client):
    _, chat_id = a_chat(client)
    send(client, chat_id, "hello")
    payload = client.get(f"/api/chats/{chat_id}/export").json()
    assert "state" in payload and "summary" in payload
    assert payload["state"], "a turn has run, so there is state"


def test_export_does_not_carry_the_character(client):
    """It has its own export, it is usually shared between chats, and copying
    it into every one would let an import silently fork someone's character."""
    payload = client.get(f"/api/chats/{a_chat(client)[1]}/export").json()
    assert set(payload["character"]) == {"id", "name"}
    assert "persona" not in payload["character"]


def test_export_offers_a_filename_only_when_asked_to_download(client):
    _, chat_id = a_chat(client, "The lighthouse")
    plain = client.get(f"/api/chats/{chat_id}/export")
    assert "content-disposition" not in plain.headers

    saved = client.get(f"/api/chats/{chat_id}/export", params={"download": True})
    assert ".json" in saved.headers["content-disposition"]
    assert "lighthouse" in saved.headers["content-disposition"].lower()


@pytest.mark.parametrize("name", ["", "a/b", "Wren: the ferry?", "  ", "x" * 200])
def test_a_filename_is_always_usable(name):
    made = chat_files.filename_for({"title": name}, "Wren")
    assert made.endswith(".json") and len(made) <= 65
    assert not set(made) & set('/\\:*?"<>|')


def test_exporting_a_chat_that_is_not_there_is_a_404(client):
    assert client.get("/api/chats/nope/export").status_code == 404


# ------------------------------------------------------------------- import


def round_trip(client, chat_id: str, **params) -> dict:
    payload = client.get(f"/api/chats/{chat_id}/export").json()
    response = client.post("/api/chats/import", content=json.dumps(payload), params=params)
    assert response.status_code == 200, response.text
    return response.json()["chat"]


def test_a_chat_survives_a_round_trip(client):
    _, chat_id = a_chat(client, "Round trip")
    send(client, chat_id, "Is the ferry running?")
    send(client, chat_id, "And the weather?")

    before = client.get(f"/api/chats/{chat_id}/messages").json()
    restored = round_trip(client, chat_id)
    after = client.get(f"/api/chats/{restored['id']}/messages").json()

    assert restored["title"] == "Round trip"
    assert [m["text"] for m in after] == [m["text"] for m in before]
    assert [m["role"] for m in after] == [m["role"] for m in before]
    assert [m["turn"] for m in after] == [m["turn"] for m in before]


def test_an_import_is_a_copy_rather_than_an_overwrite(client):
    _, chat_id = a_chat(client, "Original")
    send(client, chat_id, "hello")
    restored = round_trip(client, chat_id)

    assert restored["id"] != chat_id
    assert client.get(f"/api/chats/{chat_id}").status_code == 200, "the first one is still here"


def test_swipe_variants_survive_and_the_right_one_stays_active(client):
    _, chat_id = a_chat(client)
    send(client, chat_id, "Tell me about the wreck.")
    message = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
               if m["role"] == "assistant"][-1]
    with client.stream("POST", f"/api/messages/{message['id']}/swipe", json={}) as response:
        for _ in response.iter_lines():
            pass

    before = client.get(f"/api/chats/{chat_id}/messages").json()
    original = next(m for m in before if m["id"] == message["id"])
    assert original["variant_count"] > 1

    restored = round_trip(client, chat_id)
    after = client.get(f"/api/chats/{restored['id']}/messages").json()
    copy = [m for m in after if m["role"] == "assistant"][-1]
    assert copy["variant_count"] == original["variant_count"]
    assert copy["text"] == original["text"], "the variant that was on screen is still active"


def test_hidden_messages_stay_hidden(client):
    _, chat_id = a_chat(client)
    send(client, chat_id, "an aside")
    mine = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
            if m["role"] == "user"][-1]
    client.post(f"/api/messages/{mine['id']}/hidden", json={"hidden": True})

    restored = round_trip(client, chat_id)
    after = client.get(f"/api/chats/{restored['id']}/messages").json()
    assert any(m["hidden"] for m in after)


def test_the_state_comes_back(client):
    _, chat_id = a_chat(client)
    send(client, chat_id, "hello there")
    before = client.get(f"/api/chats/{chat_id}/state").json()["slices"]

    restored = round_trip(client, chat_id)
    after = client.get(f"/api/chats/{restored['id']}/state").json()["slices"]
    # A subset check, not equality: background passes land whenever they land,
    # so the source chat may have grown another slice between the two reads.
    assert set(before) <= set(after)
    for name in before:
        assert after[name]["value"] == before[name]["value"]


def test_an_import_binds_to_a_character_by_name_when_the_id_has_gone(client):
    _, chat_id = a_chat(client, "Rehomed")
    send(client, chat_id, "hello")
    payload = client.get(f"/api/chats/{chat_id}/export").json()
    real_name = payload["character"]["name"]
    payload["character"]["id"] = "an-id-that-is-not-here"

    body = client.post("/api/chats/import", content=json.dumps(payload)).json()
    db = get_db()
    owner = repo.get_character(db, body["chat"]["character_id"])
    assert owner.name == real_name


def test_an_import_with_nobody_to_bind_to_says_so(client):
    _, chat_id = a_chat(client)
    payload = client.get(f"/api/chats/{chat_id}/export").json()
    payload["character"] = {"id": "gone", "name": "Someone Who Left"}

    response = client.post("/api/chats/import", content=json.dumps(payload))
    assert response.status_code == 400
    assert "Someone Who Left" in response.json()["detail"]
    assert "import the character card first" in response.json()["detail"]


def test_an_explicit_character_wins(client):
    _, chat_id = a_chat(client)
    payload = client.get(f"/api/chats/{chat_id}/export").json()
    payload["character"] = {"id": "gone", "name": "Nobody"}
    real = client.get("/api/characters").json()[0]["id"]

    body = client.post("/api/chats/import", content=json.dumps(payload),
                       params={"character_id": real}).json()
    assert body["chat"]["character_id"] == real


def test_an_explicit_character_that_does_not_exist_is_refused(client):
    _, chat_id = a_chat(client)
    payload = client.get(f"/api/chats/{chat_id}/export").json()
    response = client.post("/api/chats/import", content=json.dumps(payload),
                           params={"character_id": "not-a-character"})
    assert response.status_code == 400


@pytest.mark.parametrize("junk", ["null", "[]", '"text"', '{"format": "something-else"}'])
def test_a_file_that_is_not_a_chat_export_is_refused(client, junk):
    response = client.post("/api/chats/import", content=junk)
    assert response.status_code == 400
    assert "chat export" in response.json()["detail"]


def test_a_newer_export_format_is_refused_rather_than_half_read(client):
    payload = {"format": chat_files.FORMAT, "version": chat_files.VERSION + 1,
               "character": {}, "messages": []}
    response = client.post("/api/chats/import", content=json.dumps(payload))
    assert response.status_code == 400
    assert "newer version" in response.json()["detail"]


def test_an_empty_upload_is_refused(client):
    assert client.post("/api/chats/import", content=b"").status_code == 400


def test_unreadable_json_is_refused_with_a_reason(client):
    response = client.post("/api/chats/import", content=b"{not json")
    assert response.status_code == 400
    assert "unreadable" in response.json()["detail"]


def test_a_message_with_no_variants_is_skipped_rather_than_left_blank(client):
    """It has no text, so it would render as an empty bubble."""
    _, chat_id = a_chat(client)
    send(client, chat_id, "hello")
    payload = client.get(f"/api/chats/{chat_id}/export").json()
    payload["messages"].append({"turn": 99, "role": "user", "variants": []})

    body = client.post("/api/chats/import", content=json.dumps(payload)).json()
    after = client.get(f"/api/chats/{body['chat']['id']}/messages").json()
    assert all(m["text"] for m in after)


def test_an_imported_chat_can_be_continued(client):
    """The real test of an import: the chat has to still work."""
    _, chat_id = a_chat(client)
    send(client, chat_id, "Is the ferry running?")
    restored = round_trip(client, chat_id)

    before = len(client.get(f"/api/chats/{restored['id']}/messages").json())
    send(client, restored["id"], "And tomorrow?")
    after = client.get(f"/api/chats/{restored['id']}/messages").json()
    assert len(after) > before
    assert after[-1]["turn"] > after[0]["turn"]
