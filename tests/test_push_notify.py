"""Web Push (§ app/push_notify.py): the action-stripping, and — the part
that actually matters — whether what gets sent is genuinely RFC 8291/8292
compliant rather than merely self-consistent with the `webpush` library's
own encryption.

That distinction is the whole reason for `_decrypt_aes128gcm` below: it is
written fresh from the RFC's own key-derivation and framing, not by running
`webpush`'s encrypt function backwards, so a bug that made encryption wrong
in a way that still round-tripped against itself would still be caught here.
It is also the only verification available in this environment at all — a
real subscription's endpoint is the browser vendor's actual push service
(`fcm.googleapis.com` for Chrome/Brave), and this sandbox's network policy
has no route there, so an actual on-device delivery can only be confirmed by
the person running this on their phone. What can be proven here is proven:
correct encryption, a correctly signed VAPID token, the right HTTP shape,
dead-subscription pruning, and the *actions*-stripped body.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode

import httpx
import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app import config, push_notify


def _b64u(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode()


def _subscriber_keys() -> tuple[ec.EllipticCurvePrivateKey, bytes, dict]:
    """A fake browser: a real P-256 key pair plus a real 16-byte auth
    secret, encoded exactly the way `PushSubscription.toJSON()` would."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_bytes = private_key.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    auth_secret = b"\x11" * 16
    subscription = {
        "endpoint": "https://fcm.googleapis.com/fcm/send/fake-endpoint-abc123",
        "keys": {"p256dh": _b64u(public_bytes), "auth": _b64u(auth_secret)},
    }
    return private_key, auth_secret, subscription


def _decrypt_aes128gcm(payload: bytes, ua_private_key, auth_secret: bytes) -> bytes:
    """Independent decryption (§ this file's own docstring) — RFC 8291's key
    derivation and RFC 8188's aes128gcm record framing, derived from the
    spec rather than from `webpush`'s own encrypt implementation."""
    salt = payload[:16]
    idlen = payload[20]
    as_public_bytes = payload[21 : 21 + idlen]
    ciphertext = payload[21 + idlen :]

    as_public_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public_bytes)
    ua_public_bytes = ua_private_key.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    shared_secret = ua_private_key.exchange(ec.ECDH(), as_public_key)

    context = b"WebPush: info\x00" + ua_public_bytes + as_public_bytes
    prk_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth_secret, info=context).derive(
        shared_secret
    )

    cek = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00"
    ).derive(prk_key)
    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00"
    ).derive(prk_key)

    padded = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert padded[-1:] == b"\x02", "last-record padding delimiter (RFC 8188 §2)"
    return padded[:-1]


# ------------------------------------------------------------ action stripping


def test_action_text_is_stripped():
    assert push_notify._strip_actions('*leans back* "Hello there" *smiles*') == '"Hello there"'


def test_an_action_only_reply_strips_to_nothing():
    assert push_notify._strip_actions("*just a gesture, nothing said*") == ""


def test_whitespace_left_behind_by_stripping_is_collapsed():
    text = '*turns away*\n\n"Fine."\n\n*leaves*'
    assert push_notify._strip_actions(text) == '"Fine."'


# --------------------------------------------------------- the real pipeline


def test_a_push_is_correctly_encrypted_and_signed(monkeypatch):
    """The end-to-end proof: encrypt with the real VAPID/webpush machinery,
    decrypt independently, and check the payload, the VAPID JWT's signature
    and claims, and the HTTP shape all match what a real push service would
    require."""
    ua_private, auth_secret, subscription = _subscriber_keys()

    settings = config.Settings(reply_notifications=True, push_subscriptions=[subscription])
    config.ensure_vapid_keys(settings)

    captured = {}

    def fake_post(url, *, content, headers, timeout):
        captured["url"] = url
        captured["content"] = content
        captured["headers"] = headers
        return httpx.Response(201, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)

    push_notify.send_all(settings, "Mira", '*leans back* "Good to see you." *smiles*')

    assert captured["url"] == subscription["endpoint"]
    assert captured["headers"]["content-encoding"] == "aes128gcm"

    plaintext = _decrypt_aes128gcm(captured["content"], ua_private, auth_secret)
    import json

    body = json.loads(plaintext)
    assert body == {"title": "Mira", "body": '"Good to see you."'}

    # The VAPID Authorization header: "vapid t=<jwt>, k=<application key>".
    auth_header = captured["headers"]["authorization"]
    assert auth_header.startswith("vapid t=")
    token = auth_header.split("t=", 1)[1].split(",", 1)[0]
    claims = jwt.decode(
        token,
        key=_vapid_public_key_for_jwt(settings),
        algorithms=["ES256"],
        audience="https://fcm.googleapis.com",
    )
    assert claims["sub"] == f"mailto:{push_notify._VAPID_SUBSCRIBER}"
    assert claims["aud"] == "https://fcm.googleapis.com"


def _vapid_public_key_for_jwt(settings: config.Settings):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    return load_pem_public_key(settings.vapid_public_key_pem.encode())


def test_nothing_is_sent_when_the_setting_is_off(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1))
    _, _, subscription = _subscriber_keys()
    settings = config.Settings(reply_notifications=False, push_subscriptions=[subscription])
    push_notify.send_all(settings, "Mira", "Hello.")
    assert not calls


def test_nothing_is_sent_with_no_subscriptions(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1))
    settings = config.Settings(reply_notifications=True, push_subscriptions=[])
    push_notify.send_all(settings, "Mira", "Hello.")
    assert not calls


def test_an_action_only_reply_sends_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: calls.append(1))
    _, _, subscription = _subscriber_keys()
    settings = config.Settings(reply_notifications=True, push_subscriptions=[subscription])
    push_notify.send_all(settings, "Mira", "*only a gesture*")
    assert not calls


def test_a_dead_subscription_is_pruned(monkeypatch):
    """410 Gone (or 404) means the push service itself says this browser is
    never coming back — kept around it would just fail, silently, forever."""
    _, _, subscription = _subscriber_keys()
    settings = config.Settings(reply_notifications=True, push_subscriptions=[subscription])
    config.ensure_vapid_keys(settings)

    monkeypatch.setattr(
        httpx, "post", lambda url, **k: httpx.Response(410, request=httpx.Request("POST", url))
    )
    saved = []
    monkeypatch.setattr(config, "save_settings", lambda s, path=None: saved.append(s))

    push_notify.send_all(settings, "Mira", "Hello.")
    assert settings.push_subscriptions == []
    assert saved and saved[0].push_subscriptions == []


def test_a_surviving_subscription_is_kept_after_a_success(monkeypatch):
    _, _, subscription = _subscriber_keys()
    settings = config.Settings(reply_notifications=True, push_subscriptions=[subscription])
    config.ensure_vapid_keys(settings)
    monkeypatch.setattr(
        httpx, "post", lambda url, **k: httpx.Response(201, request=httpx.Request("POST", url))
    )
    monkeypatch.setattr(config, "save_settings", lambda *a, **k: None)
    push_notify.send_all(settings, "Mira", "Hello.")
    assert settings.push_subscriptions == [subscription]


# --------------------------------------------------------------- the routes


def test_vapid_keys_are_ready_before_a_subscription_can_even_be_attempted(client, isolated_settings):
    """§ ensure_vapid_keys's own docstring: generating the key pair only on
    first subscribe is a chicken and egg that never hatches — the browser
    needs the *public* half to attempt `pushManager.subscribe` at all, which
    happens before this route is ever called. So the key exists from the
    moment settings are loaded, not from the moment something subscribes."""
    settings = client.get("/api/settings").json()
    assert settings["vapid_public_key"]
    assert "vapid_private_key" not in settings
    assert "vapid_public_key_pem" not in settings
    assert "push_subscriptions" not in settings  # never served back


def test_subscribing_stores_it(client, isolated_settings):
    r = client.post(
        "/api/push/subscribe",
        json={"endpoint": "https://fcm.googleapis.com/fcm/send/x", "keys": {"p256dh": "p", "auth": "a"}},
    )
    assert r.status_code == 200
    assert config.SETTINGS.push_subscriptions == [
        {"endpoint": "https://fcm.googleapis.com/fcm/send/x", "keys": {"p256dh": "p", "auth": "a"}}
    ]


def test_subscribing_twice_with_the_same_endpoint_replaces_not_duplicates(client, isolated_settings):
    body = {"endpoint": "https://fcm.googleapis.com/fcm/send/x", "keys": {"p256dh": "p", "auth": "a"}}
    client.post("/api/push/subscribe", json=body)
    body["keys"]["auth"] = "a2"
    client.post("/api/push/subscribe", json=body)
    assert len(config.SETTINGS.push_subscriptions) == 1
    assert config.SETTINGS.push_subscriptions[0]["keys"]["auth"] == "a2"


def test_an_incomplete_subscription_is_rejected(client, isolated_settings):
    assert client.post("/api/push/subscribe", json={"endpoint": ""}).status_code == 400


def test_unsubscribing_removes_only_that_endpoint(client, isolated_settings):
    client.post("/api/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/a", "keys": {"p256dh": "p", "auth": "a"},
    })
    client.post("/api/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/b", "keys": {"p256dh": "p", "auth": "a"},
    })
    client.post("/api/push/unsubscribe", json={"endpoint": "https://fcm.googleapis.com/fcm/send/a"})
    remaining = [s["endpoint"] for s in config.SETTINGS.push_subscriptions]
    assert remaining == ["https://fcm.googleapis.com/fcm/send/b"]


def test_a_settings_save_does_not_wipe_vapid_keys_or_subscriptions(client, isolated_settings):
    """§ build_settings — every field not explicitly carried forward resets
    to its dataclass default, which used to mean the private key and every
    subscription vanished the moment anyone touched an unrelated setting."""
    client.post("/api/push/subscribe", json={
        "endpoint": "https://fcm.googleapis.com/fcm/send/x", "keys": {"p256dh": "p", "auth": "a"},
    })
    before_key = config.SETTINGS.vapid_public_key
    before_subs = list(config.SETTINGS.push_subscriptions)

    client.put("/api/settings", json={"token_budget": 12000})

    assert config.SETTINGS.vapid_public_key == before_key
    assert config.SETTINGS.push_subscriptions == before_subs
