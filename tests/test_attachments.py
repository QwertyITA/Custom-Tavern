"""Files attached to a message (§19).

The two kinds behave differently on purpose: a text file is read once and its
text travels with the message, while an image is stored and reaches the model
only where the backend can see one. What is protected here is mostly that
second half — a picture silently ignored, or silently sent to something that
cannot look at it, both read as the feature being broken.
"""

from __future__ import annotations

import json

import pytest

from app import attachments
from app.config import BackendConfig
from app.db import get_db
from app.models import Sampling
from app.providers import GenRequest
from app.providers.ollama import LlamaCppProvider, OllamaProvider
from app.providers.openai_compat import OpenAICompatProvider

# The smallest possible real PNG: 1x1, transparent.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


def stage(client, data: bytes, filename: str):
    return client.post(
        "/api/attachments", content=data, params={"filename": filename}
    )


def a_chat(client) -> str:
    character_id = client.get("/api/characters").json()[0]["id"]
    return client.post("/api/chats", json={"character_id": character_id}).json()["id"]


def send(client, chat_id: str, text: str, ids=()) -> None:
    body = {"text": text, "attachments": list(ids)}
    with client.stream("POST", f"/api/chats/{chat_id}/send", json=body) as response:
        assert response.status_code == 200, response.read()
        for _ in response.iter_lines():
            pass


# ------------------------------------------------------------------ staging


def test_a_text_file_is_read_at_upload(client):
    body = stage(client, b"the ferry timetable", "notes.txt").json()
    assert body["kind"] == "text"
    assert body["text"] == "the ferry timetable"
    assert body["message_id"] is None, "staged, not yet on a message"


def test_an_image_is_stored_and_served(client):
    body = stage(client, PNG, "photo.png").json()
    assert body["kind"] == "image" and body["size"] == len(PNG)
    assert body["text"] == "", "an image has no text to carry"

    served = client.get(f"/api/attachments/{body['id']}/file")
    assert served.status_code == 200
    assert served.content == PNG
    assert served.headers["content-type"].startswith("image/png")


def test_the_stored_filename_never_leaves_the_server(client):
    """It is an implementation detail; the id is what URLs are built from."""
    assert "stored_as" not in stage(client, PNG, "photo.png").json()


def test_a_text_file_is_not_kept_on_disk(client):
    """The text *is* the file. A second copy on a phone to re-read later would
    be storage spent on nothing."""
    body = stage(client, b"hello", "notes.txt").json()
    assert client.get(f"/api/attachments/{body['id']}/file").status_code == 404


@pytest.mark.parametrize("name", ["thing.exe", "archive.zip", "noextension", "a.docx"])
def test_a_kind_of_file_this_cannot_read_is_refused_with_a_list(client, name):
    response = stage(client, b"data", name)
    assert response.status_code == 400
    assert "images" in response.json()["detail"] and "text" in response.json()["detail"]


def test_an_empty_file_is_refused(client):
    assert stage(client, b"", "notes.txt").status_code == 400


def test_a_text_file_that_is_not_utf8_is_refused(client):
    response = stage(client, b"\xff\xfe\x00binary", "notes.txt")
    assert response.status_code == 400
    assert "UTF-8" in response.json()["detail"]


def test_an_oversized_image_is_refused_with_the_limit(client):
    huge = PNG + b"\x00" * attachments.MAX_IMAGE_BYTES
    response = stage(client, huge, "big.png")
    assert response.status_code == 400
    assert "limit" in response.json()["detail"]


def test_a_long_text_file_is_truncated_rather_than_refused(client):
    """A dropped-in document can be enormous, and spending the whole context on
    it silently is worse than using part of it."""
    body = stage(client, b"x" * (attachments.MAX_TEXT_CHARS * 2), "long.txt").json()
    assert len(body["text"]) == attachments.MAX_TEXT_CHARS


def test_the_shown_name_is_never_a_path(client):
    body = stage(client, b"hi", "../../etc/passwd.txt").json()
    assert "/" not in body["name"] and ".." not in body["name"]


def test_a_staged_attachment_can_be_dropped(client):
    body = stage(client, PNG, "photo.png").json()
    assert client.delete(f"/api/attachments/{body['id']}").status_code == 200
    assert client.get(f"/api/attachments/{body['id']}/file").status_code == 404


def test_deleting_something_that_is_not_there_is_a_404(client):
    assert client.delete("/api/attachments/nope").status_code == 404


def test_stale_staged_attachments_are_swept(client, monkeypatch):
    """Someone picks a file, changes their mind and closes the app."""
    old = stage(client, PNG, "forgotten.png").json()
    monkeypatch.setattr(attachments, "STAGED_TTL_SECONDS", -1)
    stage(client, b"anything", "new.txt")  # any upload runs the sweep
    assert attachments.get(get_db(), old["id"]) is None


def test_the_sweep_leaves_sent_attachments_alone(client, monkeypatch):
    chat_id = a_chat(client)
    kept = stage(client, PNG, "sent.png").json()
    send(client, chat_id, "look at this", [kept["id"]])

    monkeypatch.setattr(attachments, "STAGED_TTL_SECONDS", -1)
    stage(client, b"anything", "new.txt")
    assert attachments.get(get_db(), kept["id"]) is not None


# ------------------------------------------------------------------ sending


def test_sending_binds_the_attachment_to_the_message(client):
    chat_id = a_chat(client)
    item = stage(client, PNG, "photo.png").json()
    send(client, chat_id, "look at this", [item["id"]])

    mine = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
            if m["role"] == "user"][-1]
    assert [a["id"] for a in mine["attachments"]] == [item["id"]]


def test_a_message_can_be_sent_with_no_words_at_all(client):
    """"Look at this" with a picture and no text is a real message."""
    chat_id = a_chat(client)
    item = stage(client, PNG, "photo.png").json()
    send(client, chat_id, "", [item["id"]])
    assert any(m["attachments"] for m in client.get(f"/api/chats/{chat_id}/messages").json())


def test_a_message_with_neither_words_nor_files_is_refused(client):
    chat_id = a_chat(client)
    response = client.post(f"/api/chats/{chat_id}/send", json={"text": "  "})
    assert response.status_code == 400


def test_an_attachment_cannot_be_claimed_twice(client):
    """A replayed request must not move someone's picture onto a later turn."""
    chat_id = a_chat(client)
    item = stage(client, PNG, "photo.png").json()
    send(client, chat_id, "first", [item["id"]])
    send(client, chat_id, "second", [item["id"]])

    mine = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
            if m["role"] == "user"]
    assert [len(m["attachments"]) for m in mine] == [1, 0]


def test_messages_with_no_attachments_carry_an_empty_list(client):
    chat_id = a_chat(client)
    send(client, chat_id, "nothing attached")
    assert all(m["attachments"] == []
               for m in client.get(f"/api/chats/{chat_id}/messages").json())


# ------------------------------------------------------------- in the prompt


def last_prompt(client, chat_id: str) -> str:
    reply = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
             if m["role"] == "assistant"][-1]
    body = client.get(f"/api/messages/{reply['id']}/prompt").json()
    return "\n".join(p["text"] for p in body.get("parts", []))


def test_a_text_file_reaches_the_prompt(client):
    chat_id = a_chat(client)
    item = stage(client, b"The ferry leaves at six.", "timetable.txt").json()
    send(client, chat_id, "when is it", [item["id"]])

    assembled = client.get(f"/api/chats/{chat_id}/messages").json()
    # The conversation part is not copied into the record, so check the built
    # prompt directly instead.
    from app import assembly, repo
    from app.config import Settings

    db = get_db()
    chat = repo.get_chat(db, chat_id)
    character = repo.get_character(db, chat["character_id"])
    out = assembly.build_reply_context(db, chat, character, Settings())
    joined = "\n".join(m["content"] for m in out.messages)
    assert "The ferry leaves at six." in joined
    assert "timetable.txt" in joined
    assert assembled  # the turn really ran


def test_an_image_is_named_even_where_it_cannot_be_seen(client):
    """A reply that ignores a picture the person clearly meant something by is
    worse than one that says it cannot see it."""
    text = attachments.prompt_suffix(
        [{"kind": "image", "name": "wreck.png", "text": ""}], can_see_images=False
    )
    assert "wreck.png" in text
    assert "cannot see images" in text


def test_an_image_is_named_where_it_can_be_seen_too(client):
    text = attachments.prompt_suffix(
        [{"kind": "image", "name": "wreck.png", "text": ""}], can_see_images=True
    )
    assert "wreck.png" in text
    assert "cannot see" not in text


def test_images_only_reach_a_backend_that_can_see_them(client):
    from app import assembly, repo
    from app.config import Settings

    chat_id = a_chat(client)
    item = stage(client, PNG, "photo.png").json()
    send(client, chat_id, "look", [item["id"]])

    db = get_db()
    chat = repo.get_chat(db, chat_id)
    character = repo.get_character(db, chat["character_id"])

    blind = assembly.build_reply_context(db, chat, character, Settings(), sees_images=False)
    assert blind.images == []

    seeing = assembly.build_reply_context(db, chat, character, Settings(), sees_images=True)
    assert len(seeing.images) == 1


def test_only_the_newest_turn_s_images_are_sent(client):
    """Re-sending every image in the window on every turn would be the single
    most expensive thing this app does."""
    from app import assembly, repo
    from app.config import Settings

    chat_id = a_chat(client)
    for word in ("first", "second"):
        item = stage(client, PNG, f"{word}.png").json()
        send(client, chat_id, word, [item["id"]])

    db = get_db()
    chat = repo.get_chat(db, chat_id)
    character = repo.get_character(db, chat["character_id"])
    out = assembly.build_reply_context(db, chat, character, Settings(), sees_images=True)
    assert len(out.images) == 1


# ---------------------------------------------------- which backends can see


def test_the_providers_declare_what_they_can_see():
    assert OllamaProvider.sees_images is True
    assert OpenAICompatProvider.sees_images is True
    # Its /completion endpoint takes a prompt string and nothing else.
    assert LlamaCppProvider.sees_images is False


def test_ollama_puts_images_on_the_newest_user_turn():
    provider = OllamaProvider(BackendConfig(name="o", kind="ollama", model="llava"))
    request = GenRequest(
        system="S",
        messages=[{"role": "user", "content": "old"},
                  {"role": "assistant", "content": "reply"},
                  {"role": "user", "content": "look"}],
        sampling=Sampling(),
        images=["BASE64DATA"],
    )
    _, payload = provider._payload(request, stream=False)
    carrying = [m for m in payload["messages"] if m.get("images")]
    assert len(carrying) == 1
    assert carrying[0]["content"] == "look"
    assert carrying[0]["images"] == ["BASE64DATA"]


def test_openai_turns_the_newest_turn_into_parts():
    provider = OpenAICompatProvider(BackendConfig(name="x", kind="openai", model="gpt-4o"))
    request = GenRequest(
        system="S",
        messages=[{"role": "user", "content": "look"}],
        sampling=Sampling(),
        images=["BASE64DATA"],
    )
    payload = provider._payload(request, stream=False)
    content = payload["messages"][-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["image_url"]["url"].endswith("BASE64DATA")


def test_no_images_means_the_payload_is_shaped_exactly_as_before():
    """The common turn must not pay for this feature."""
    provider = OpenAICompatProvider(BackendConfig(name="x", kind="openai", model="m"))
    request = GenRequest(
        system="S", messages=[{"role": "user", "content": "hi"}], sampling=Sampling()
    )
    payload = provider._payload(request, stream=False)
    assert payload["messages"][-1]["content"] == "hi"


# ----------------------------------------------------------------- clean-up


def test_deleting_a_message_removes_its_image_from_disk(client):
    chat_id = a_chat(client)
    item = stage(client, PNG, "photo.png").json()
    send(client, chat_id, "look", [item["id"]])
    mine = [m for m in client.get(f"/api/chats/{chat_id}/messages").json()
            if m["role"] == "user"][-1]

    path = attachments.path_for(get_db(), item["id"])
    assert path is not None and path.is_file()

    client.delete(f"/api/messages/{mine['id']}")
    assert not path.is_file(), "the cascade drops the row; the file needs sweeping"


def test_deleting_a_chat_removes_its_images_from_disk(client):
    chat_id = a_chat(client)
    item = stage(client, PNG, "photo.png").json()
    send(client, chat_id, "look", [item["id"]])
    path = attachments.path_for(get_db(), item["id"])

    client.delete(f"/api/chats/{chat_id}")
    assert not path.is_file()


def test_sweeping_orphans_leaves_live_files_alone(client):
    chat_id = a_chat(client)
    kept = stage(client, PNG, "kept.png").json()
    send(client, chat_id, "look", [kept["id"]])

    attachments.delete_orphans(get_db())
    assert attachments.path_for(get_db(), kept["id"]).is_file()


def test_the_chat_route_carries_attachments_too(client):
    """This is the route the app loads a chat through, so anything missing here
    is missing until a reload that happens to hit /messages instead — which is
    to say, never. Found by opening a chat with a picture in it and watching it
    come back empty."""
    chat_id = a_chat(client)
    item = stage(client, PNG, "photo.png").json()
    send(client, chat_id, "look", [item["id"]])

    composite = client.get(f"/api/chats/{chat_id}").json()["messages"]
    listed = client.get(f"/api/chats/{chat_id}/messages").json()
    assert [m.get("attachments") for m in composite] == [m.get("attachments") for m in listed]
    assert any(m["attachments"] for m in composite)


def test_a_small_file_does_not_round_to_nothing(client):
    """"0 KB" next to a filename reads as the upload having failed."""
    body = stage(client, PNG, "tiny.png").json()
    assert 0 < body["size"] < 1024
