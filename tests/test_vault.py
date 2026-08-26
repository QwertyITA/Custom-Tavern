"""Character vault (§ app/vault.py, app/main.py's /api/vault/*).

Threat model, per the feature's own spec: hide certain cards from someone
browsing the app on the phone without the PIN. Not encryption — nothing here
tests that the data is unreadable at rest, because it never claims to be.
"""

from __future__ import annotations

import pytest

from app import vault


@pytest.fixture(autouse=True)
def _reset_vault_throttle():
    vault.reset()
    yield
    vault.reset()


def test_a_fresh_vault_is_not_configured(client):
    settings = client.get("/api/settings").json()
    assert settings["vault_configured"] is False
    assert settings["vault_unlocked"] is False
    # And never leaks a hash or salt to the browser under any key.
    assert "vault_pin_hash" not in settings
    assert "vault_pin_salt" not in settings


def test_setup_needs_six_digits(client):
    for bad in ("12345", "1234567", "12345a", "", "abcdef"):
        r = client.post("/api/vault/setup", json={"pin": bad})
        assert r.status_code == 400, bad


def test_setup_configures_and_auto_unlocks(client):
    r = client.post("/api/vault/setup", json={"pin": "204719"})
    assert r.status_code == 200
    body = r.json()
    assert body["vault_configured"] is True
    assert body["vault_unlocked"] is True

    settings = client.get("/api/settings").json()
    assert settings["vault_configured"] is True
    assert settings["vault_unlocked"] is True


def test_setup_twice_is_refused(client):
    client.post("/api/vault/setup", json={"pin": "111111"})
    r = client.post("/api/vault/setup", json={"pin": "222222"})
    assert r.status_code == 400


def test_lock_then_unlock_with_the_right_pin(client):
    client.post("/api/vault/setup", json={"pin": "555555"})
    client.post("/api/vault/lock")
    assert client.get("/api/settings").json()["vault_unlocked"] is False

    r = client.post("/api/vault/unlock", json={"pin": "555555"})
    assert r.status_code == 200
    assert client.get("/api/settings").json()["vault_unlocked"] is True


def test_lock_needs_no_pin(client):
    client.post("/api/vault/setup", json={"pin": "555555"})
    r = client.post("/api/vault/lock")
    assert r.status_code == 200
    assert r.json()["vault_unlocked"] is False


def test_unlock_with_the_wrong_pin_is_refused_and_stays_locked(client):
    client.post("/api/vault/setup", json={"pin": "555555"})
    client.post("/api/vault/lock")

    r = client.post("/api/vault/unlock", json={"pin": "000000"})
    assert r.status_code == 401
    assert client.get("/api/settings").json()["vault_unlocked"] is False


def test_too_many_wrong_pins_locks_out_further_attempts(client):
    client.post("/api/vault/setup", json={"pin": "555555"})
    client.post("/api/vault/lock")

    for _ in range(5):
        r = client.post("/api/vault/unlock", json={"pin": "000000"})
        assert r.status_code == 401
    # The 6th attempt is throttled rather than checked at all — even the
    # correct PIN is refused until the cooldown passes.
    r = client.post("/api/vault/unlock", json={"pin": "555555"})
    assert r.status_code == 429


def test_change_pin_needs_the_current_one(client):
    client.post("/api/vault/setup", json={"pin": "111111"})
    r = client.post("/api/vault/change", json={"current_pin": "000000", "new_pin": "222222"})
    assert r.status_code == 401

    r = client.post("/api/vault/change", json={"current_pin": "111111", "new_pin": "222222"})
    assert r.status_code == 200

    client.post("/api/vault/lock")
    # The old PIN no longer works, the new one does.
    assert client.post("/api/vault/unlock", json={"pin": "111111"}).status_code == 401
    assert client.post("/api/vault/unlock", json={"pin": "222222"}).status_code == 200


def test_remove_clears_the_pin_but_keeps_which_cards_were_vaulted(client, character):
    client.post("/api/vault/setup", json={"pin": "111111"})
    client.post(f"/api/characters/{character.id}/vault", json={"vaulted": True})
    client.post("/api/vault/lock")
    assert character.id not in [c["id"] for c in client.get("/api/characters").json()]

    r = client.post("/api/vault/remove", json={"current_pin": "111111"})
    assert r.status_code == 200
    settings = client.get("/api/settings").json()
    assert settings["vault_configured"] is False
    assert settings["vault_unlocked"] is False

    # No PIN at all means nothing is gated any more, vaulted or not.
    ids = [c["id"] for c in client.get("/api/characters").json()]
    assert character.id in ids

    # Setting a new PIN resumes hiding the same card without re-picking it.
    client.post("/api/vault/setup", json={"pin": "999999"})
    client.post("/api/vault/lock")
    ids = [c["id"] for c in client.get("/api/characters").json()]
    assert character.id not in ids


def test_remove_needs_the_current_pin(client):
    client.post("/api/vault/setup", json={"pin": "111111"})
    r = client.post("/api/vault/remove", json={"current_pin": "000000"})
    assert r.status_code == 401
    assert client.get("/api/settings").json()["vault_configured"] is True


def test_roster_hides_a_vaulted_character_only_while_locked(client, character):
    r = client.get("/api/characters").json()
    assert character.id in [c["id"] for c in r]

    client.post("/api/vault/setup", json={"pin": "111111"})
    client.post(f"/api/characters/{character.id}/vault", json={"vaulted": True})
    # Still unlocked — vaulting alone doesn't hide it yet.
    assert character.id in [c["id"] for c in client.get("/api/characters").json()]

    client.post("/api/vault/lock")
    assert character.id not in [c["id"] for c in client.get("/api/characters").json()]

    client.post("/api/vault/unlock", json={"pin": "111111"})
    ids = [c["id"] for c in client.get("/api/characters").json()]
    assert character.id in ids
    row = next(c for c in client.get("/api/characters").json() if c["id"] == character.id)
    assert row["vaulted"] is True


def test_a_card_marked_vaulted_before_any_pin_exists_stays_visible(client, character):
    """No PIN means nothing is gated — a flag with nothing to gate it is
    inert, not a silent hide."""
    r = client.post(f"/api/characters/{character.id}/vault", json={"vaulted": True})
    assert r.status_code == 200
    assert character.id in [c["id"] for c in client.get("/api/characters").json()]


def test_get_character_404s_the_same_as_not_found_while_vaulted_and_locked(client, character):
    client.post("/api/vault/setup", json={"pin": "111111"})
    client.post(f"/api/characters/{character.id}/vault", json={"vaulted": True})
    client.post("/api/vault/lock")

    assert client.get(f"/api/characters/{character.id}").status_code == 404
    # And the toggle route itself is gated the same way — no un-vaulting a
    # card through the API while it's locked either.
    r = client.post(f"/api/characters/{character.id}/vault", json={"vaulted": False})
    assert r.status_code == 404


def test_create_chat_refuses_a_vaulted_and_locked_character(client, character):
    client.post("/api/vault/setup", json={"pin": "111111"})
    client.post(f"/api/characters/{character.id}/vault", json={"vaulted": True})
    client.post("/api/vault/lock")

    r = client.post("/api/chats", json={"character_id": character.id, "title": ""})
    assert r.status_code == 404


def test_chats_list_hides_chats_of_a_vaulted_and_locked_character(client, character, chat):
    r = client.get("/api/chats").json()
    assert chat["id"] in [c["id"] for c in r]

    client.post("/api/vault/setup", json={"pin": "111111"})
    client.post(f"/api/characters/{character.id}/vault", json={"vaulted": True})
    client.post("/api/vault/lock")

    r = client.get("/api/chats").json()
    assert chat["id"] not in [c["id"] for c in r]

    r = client.get(f"/api/chats?character_id={character.id}").json()
    assert r == []


def test_settings_save_never_wipes_the_vault_pin(client, character):
    """build_settings starts a brand-new Settings() and copies fields across
    one by one (§ config.build_settings) — a field it forgets to carry over
    resets to the dataclass default on the very next unrelated save."""
    client.post("/api/vault/setup", json={"pin": "111111"})
    client.post("/api/vault/lock")

    settings = client.get("/api/settings").json()
    settings["motion"] = 40  # an unrelated field, saved through the normal path
    r = client.put("/api/settings", json=settings)
    assert r.status_code == 200

    after = client.get("/api/settings").json()
    assert after["vault_configured"] is True
    assert after["vault_unlocked"] is False
    # And the PIN itself still works — not just the "configured" flag.
    assert client.post("/api/vault/unlock", json={"pin": "111111"}).status_code == 200


def test_vault_module_hashes_dont_round_trip_plaintext(client):
    """Not a promise of real secrecy (§ app/vault.py's docstring) — just that
    the PIN isn't sitting in settings.json as the four digits someone typed."""
    client.post("/api/vault/setup", json={"pin": "424242"})
    from app import config

    assert config.SETTINGS.vault_pin_hash != "424242"
    assert "424242" not in config.SETTINGS.vault_pin_hash
    assert config.SETTINGS.vault_pin_salt
