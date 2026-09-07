"""The channel check (§ POST /api/channel/check, main.py): whether there is
anything on the other end of each configured tier.

The distinction this endpoint exists to hold is that three states are honest
where two are not — reachable, refusing, and *couldn't say* — and that the
cheap probe never claims what only the generation probe can establish.
"""

from __future__ import annotations

import pytest

from app import config, providers
from app.config import BackendConfig


def _tiers(monkeypatch, tiers: dict, backends: list[BackendConfig]) -> None:
    monkeypatch.setattr(config.SETTINGS, "tiers", tiers)
    monkeypatch.setattr(config.SETTINGS, "backends", backends)


def test_the_echo_backend_reads_as_open(client):
    rows = client.post("/api/channel/check").json()["backends"]
    assert [r["state"] for r in rows] == ["open"]
    assert rows[0]["name"] == "echo"


def test_three_tiers_on_one_backend_are_probed_once(client, monkeypatch):
    """One link, one row, one request. Probing the same key three times over
    would say the same thing three times and cost three times as much."""
    rows = client.post("/api/channel/check").json()["backends"]
    assert len(rows) == 1
    assert rows[0]["tiers"] == ["blocking", "foreground", "background"]


def test_two_backends_get_a_row_each(client, monkeypatch):
    _tiers(
        monkeypatch,
        {"blocking": "one", "foreground": "two", "background": "two"},
        [
            BackendConfig(name="one", kind="echo", model="echo-1"),
            BackendConfig(name="two", kind="echo", model="echo-1"),
        ],
    )
    rows = {r["name"]: r for r in client.post("/api/channel/check").json()["backends"]}
    assert set(rows) == {"one", "two"}
    assert rows["one"]["tiers"] == ["blocking"]
    assert rows["two"]["tiers"] == ["foreground", "background"]


def test_tiers_come_back_in_the_order_the_ui_lists_them(client, monkeypatch):
    _tiers(
        monkeypatch,
        {"background": "echo", "blocking": "echo", "foreground": "echo"},
        [BackendConfig(name="echo", kind="echo", model="echo-1")],
    )
    rows = client.post("/api/channel/check").json()["backends"]
    assert rows[0]["tiers"] == ["blocking", "foreground", "background"]


def test_a_tier_pointing_at_a_missing_backend_is_broken(client, monkeypatch):
    _tiers(
        monkeypatch,
        {"blocking": "gone", "foreground": "echo", "background": "echo"},
        [BackendConfig(name="echo", kind="echo", model="echo-1")],
    )
    rows = {r["name"]: r for r in client.post("/api/channel/check").json()["backends"]}
    assert rows["gone"]["state"] == "broken"
    assert "gone" in rows["gone"]["error"]
    assert rows["echo"]["state"] == "open"


def test_a_backend_that_cannot_enumerate_says_unknown_not_open(client, monkeypatch):
    """`list_models` answers [] rather than raising when a backend has no way
    to say (§ providers/base.py). An empty answer is not a reachable backend —
    it is no answer at all, and reporting it as open would be the exact lie
    this indicator exists to stop telling."""

    async def nothing(self):
        return []

    monkeypatch.setattr(providers.base.Provider, "list_models", nothing, raising=False)
    for provider in (providers.echo.EchoProvider,):
        monkeypatch.setattr(provider, "list_models", nothing, raising=False)

    rows = client.post("/api/channel/check").json()["backends"]
    assert rows[0]["state"] == "unknown"


def test_a_provider_that_raises_is_broken_with_the_reason(client, monkeypatch):
    async def refuse(self):
        raise providers.ProviderError("401 unauthorized")

    monkeypatch.setattr(providers.echo.EchoProvider, "list_models", refuse, raising=False)
    rows = client.post("/api/channel/check").json()["backends"]
    assert rows[0]["state"] == "broken"
    assert "401" in rows[0]["error"]


def test_a_key_never_reaches_the_error_text(client, monkeypatch):
    """Same rule the generation probe already follows (§ _safe_error): whatever
    a provider puts in its exception, the key does not come back out of it."""
    secret = "sk-DEADBEEFDEADBEEFDEADBEEF"
    _tiers(
        monkeypatch,
        {"blocking": "keyed", "foreground": "keyed", "background": "keyed"},
        [BackendConfig(name="keyed", kind="echo", model="echo-1", api_key=secret)],
    )

    async def leak(self):
        raise providers.ProviderError(f"rejected key {secret}")

    monkeypatch.setattr(providers.echo.EchoProvider, "list_models", leak, raising=False)
    body = client.post("/api/channel/check").text
    assert secret not in body


@pytest.mark.parametrize("field", ["name", "kind", "tiers", "state", "error"])
def test_every_row_carries_what_the_indicator_reads(client, field):
    rows = client.post("/api/channel/check").json()["backends"]
    assert field in rows[0]
