"""Guards on the credential protections themselves.

This repository is public, so the ignore rules and the masking below are load
bearing. A regression here is not a broken test — it is a leaked key.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from app import config
from app.config import BackendConfig, Settings

REPO = Path(__file__).resolve().parent.parent

# Paths that must never become trackable.
SECRET_PATHS = [
    "data/settings.json",
    ".env",
    ".env.local",
    "private.pem",
    "server.key",
    "id_rsa",
    "id_ed25519",
    ".git-credentials",
    "secrets.json",
]


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )


def repo_is_git() -> bool:
    return (REPO / ".git").exists() and git("rev-parse", "--git-dir").returncode == 0


needs_git = pytest.mark.skipif(not repo_is_git(), reason="not a git checkout")


@needs_git
@pytest.mark.parametrize("path", SECRET_PATHS)
def test_secret_paths_are_gitignored(path):
    assert git("check-ignore", "-q", path).returncode == 0, (
        f"{path} is NOT gitignored — it could be committed to a public repo"
    )


@needs_git
def test_the_example_settings_file_stays_trackable():
    """The template must remain tracked, or the ignore rule is too broad."""
    assert git("check-ignore", "-q", "data/settings.example.json").returncode != 0


@needs_git
def test_no_secret_path_is_currently_tracked():
    tracked = set(git("ls-files").stdout.splitlines())
    for path in SECRET_PATHS:
        assert path not in tracked, f"{path} is tracked in git"


def test_example_settings_contains_no_real_key():
    """0000000000 is AI Horde's published anonymous key, not a secret."""
    body = json.loads((REPO / "data/settings.example.json").read_text())
    for backend in body["backends"]:
        key = backend.get("api_key", "")
        assert key == "" or set(key) == {"0"}, f"{backend['name']} has a real-looking key"


def test_settings_masks_api_keys_in_its_dict():
    """/api/settings serves this straight to the client."""
    settings = Settings(
        backends=[BackendConfig(name="x", kind="openai", api_key="sk-realkey1234567890")]
    )
    assert settings.to_dict()["backends"][0]["api_key"] == "***"


def test_settings_with_no_key_stays_empty_rather_than_masked():
    settings = Settings(backends=[BackendConfig(name="x", kind="echo")])
    assert settings.to_dict()["backends"][0]["api_key"] == ""


# ------------------------------------------------------------------- hook


HOOK = REPO / ".githooks/pre-commit"


def test_the_pre_commit_hook_exists_and_is_executable():
    assert HOOK.exists(), "the credential guard is missing"
    import os

    assert os.access(HOOK, os.X_OK), "the credential guard is not executable"


@needs_git
@pytest.mark.parametrize(
    "content,should_block",
    [
        ('TOKEN = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"', True),
        ('key = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"', True),
        ('AWS = "AKIAIOSFODNN7EXAMPLE"', True),
        ('url = "https://user:hunter2pass@example.com/repo"', True),
        ('{"api_key": "9f8e7d6c5b4a3210ffee"}', True),
        ("-----BEGIN OPENSSH PRIVATE KEY-----", True),
        ('{"api_key": "0000000000"}', False),   # Horde anonymous key
        ('{"api_key": ""}', False),
        ('{"api_key": "your-key-here"}', False),
        ("Set your api_key in data/settings.json before use.", False),
        ('headers["Authorization"] = f"Bearer {self.config.api_key}"', False),
    ],
)
def test_hook_verdicts(tmp_path, content, should_block):
    """Run the real hook against a scratch repo, so this tests the shipped file."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)

    probe = tmp_path / "probe.txt"
    probe.write_text(content + "\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "probe.txt"], check=True)

    result = subprocess.run(
        ["bash", str(HOOK)], cwd=tmp_path, capture_output=True, text=True
    )
    blocked = result.returncode != 0
    assert blocked is should_block, (
        f"{'expected block' if should_block else 'unexpected block'}: {content}\n{result.stdout}"
    )


# --------------------------------------------------- settings storage (§13)


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    """Isolate the settings file so a test can never write the real one."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "settings_path", lambda: path)
    return path


def horde(**overrides):
    base = {"name": "horde", "kind": "horde", "api_key": "REAL-HORDE-KEY-1234", "model": "m"}
    return {**base, **overrides}


def a_settings(**overrides):
    return Settings(
        backends=[BackendConfig(**horde())],
        tiers={"blocking": "horde", "foreground": "horde", "background": "horde"},
        **overrides,
    )


def test_saved_file_is_owner_only(settings_file):
    config.save_settings(a_settings(), settings_file)
    assert stat.S_IMODE(settings_file.stat().st_mode) == 0o600


def test_saved_file_holds_the_real_key_not_the_mask(settings_file):
    config.save_settings(a_settings(), settings_file)
    stored = json.loads(settings_file.read_text())
    assert stored["backends"][0]["api_key"] == "REAL-HORDE-KEY-1234"


def test_save_is_atomic_and_leaves_no_temp_file(settings_file):
    config.save_settings(a_settings(), settings_file)
    assert [p.name for p in settings_file.parent.iterdir()] == ["settings.json"]


def test_masked_key_round_trips_without_being_destroyed():
    """The browser never sees the key, so an untouched field returns MASK."""
    current = a_settings()
    payload = json.loads(json.dumps(current.to_dict()))
    assert payload["backends"][0]["api_key"] == config.MASK

    payload["backends"][0]["model"] = "a-different-model"
    rebuilt = config.build_settings(payload, current)
    assert rebuilt.backends[0].api_key == "REAL-HORDE-KEY-1234"
    assert rebuilt.backends[0].model == "a-different-model"


def test_a_real_edit_replaces_the_key():
    current = a_settings()
    payload = current.to_dict()
    payload["backends"][0]["api_key"] = "A-NEW-KEY-5678"
    assert config.build_settings(payload, current).backends[0].api_key == "A-NEW-KEY-5678"


def test_masked_key_for_an_unknown_backend_becomes_empty():
    """A new backend cannot inherit some other backend's secret."""
    payload = {
        "backends": [{"name": "brand-new", "kind": "openai", "api_key": config.MASK}],
        "tiers": {"blocking": "brand-new", "foreground": "brand-new", "background": "brand-new"},
    }
    assert config.build_settings(payload, a_settings()).backends[0].api_key == ""


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"backends": []}, "at least one backend"),
        ({"backends": [{"name": "x", "kind": "nonsense"}]}, "unknown backend kind"),
        ({"backends": [{"name": "", "kind": "echo"}]}, "needs a name"),
        ({"backends": [{"name": "x", "kind": "echo"}, {"name": "x", "kind": "echo"}]}, "unique"),
        ({"backends": [{"name": "x", "kind": "echo"}], "tiers": {"blocking": "ghost"}}, "unknown backend"),
        ({"backends": [{"name": "x", "kind": "echo", "template": "klingon"}]}, "unknown template"),
        ({"backends": [{"name": "x", "kind": "echo"}], "port": 99999}, "port must be"),
        ({"backends": [{"name": "x", "kind": "echo"}], "token_budget": -5}, "cannot be negative"),
    ],
)
def test_invalid_settings_are_rejected(payload, message):
    payload.setdefault("tiers", {"blocking": "x", "foreground": "x", "background": "x"})
    with pytest.raises(config.SettingsError, match=message):
        config.build_settings(payload, a_settings())


def test_error_text_never_carries_the_key():
    """A base_url can embed a token, and httpx puts the URL in the message."""
    from app.main import _safe_error

    exc = RuntimeError("connect failed: https://host/v1/example-token-abc123/models")
    assert "example-token-abc123" not in _safe_error(exc, "example-token-abc123")
    assert config.MASK in _safe_error(exc, "example-token-abc123")


def test_error_text_is_unchanged_when_there_is_no_key():
    from app.main import _safe_error

    assert _safe_error(RuntimeError("plain failure"), "") == "plain failure"


# ------------------------------------------------------ appearance (§12)


def test_theme_keeps_only_known_tokens():
    theme = config.validate_theme({"--accent": "#ff0000", "--not-a-token": "#000000"})
    assert theme == {"--accent": "#ff0000"}


def test_theme_omits_values_equal_to_the_default():
    """An empty map means "all defaults", so the palette can change later."""
    default = config.THEME_VARS["--accent"]["default"]
    assert config.validate_theme({"--accent": default}) == {}


@pytest.mark.parametrize(
    "value",
    ["red", "url(javascript:alert(1))", "#12", "#gggggg", "10px; background:url(x)", ""],
)
def test_theme_rejects_anything_that_is_not_a_plain_colour(value):
    """Values go straight into style.setProperty — nothing may escape it."""
    with pytest.raises(config.SettingsError):
        config.validate_theme({"--accent": value})


@pytest.mark.parametrize("value", ["100vw", "-5px", "10em", "999999px", "10"])
def test_theme_rejects_out_of_shape_sizes(value):
    with pytest.raises(config.SettingsError):
        config.validate_theme({"--radius": value})


def test_every_theme_token_is_declared_completely():
    for token in config.THEME_TOKENS:
        assert token["var"].startswith("--")
        assert token["label"] and token["group"]
        assert token["type"] in ("color", "px", "pct")
        # A token whose own default is invalid could never be reset to it.
        assert config.validate_theme({token["var"]: token["default"]}) == {}


def test_css_root_matches_the_declared_theme_defaults():
    """The stylesheet and THEME_TOKENS are two halves of one palette.

    The editor shows token["default"] when nothing is overridden, and
    validate_theme drops a value equal to it. If the CSS said something else,
    the editor would display a colour the page is not actually using.
    """
    css = (REPO / "static/styles.css").read_text()
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    declared = dict(
        line.strip().rstrip(";").split(":", 1)
        for line in root.splitlines()
        if line.strip().startswith("--") and ":" in line
    )
    declared = {k.strip(): v.strip() for k, v in declared.items()}

    for token in config.THEME_TOKENS:
        assert token["var"] in declared, f"{token['var']} missing from styles.css :root"
        assert declared[token["var"]] == token["default"], (
            f"{token['var']}: styles.css has {declared[token['var']]}, "
            f"THEME_TOKENS says {token['default']}"
        )


def test_stylesheet_declares_a_colour_scheme():
    """Opts the page out of Chromium's forced dark repainting.

    Without this, Brave and Chrome repaint light surfaces dark at paint time
    while leaving text colours alone — a black page with the light theme's
    pink text on it.
    """
    css = (REPO / "static/styles.css").read_text()
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    assert "color-scheme:" in root

    app = (REPO / "static/app.js").read_text()
    # And it must follow the chosen palette, not stay pinned to light.
    assert "colorScheme" in app and "updateColorScheme" in app


# ------------------------------------------------------- backdrop (§12)


def test_the_bundled_backdrop_exists_and_is_well_formed():
    """Shipped as vector: a public repo should not carry a stock photo with a
    licence question attached, and an SVG stays sharp at any phone size."""
    import xml.etree.ElementTree as ET

    path = config.BACKGROUND_DIR / "tavern.svg"
    assert path.exists(), "the default backdrop is missing"
    root = ET.parse(path).getroot()
    assert root.tag.endswith("svg")
    assert root.get("viewBox")
    assert path.stat().st_size < 200_000, "a backdrop this large belongs in a raster format"


def test_the_default_backdrop_is_one_that_ships():
    assert config.Settings().background in config.available_backgrounds()


@pytest.mark.parametrize("value", ["none", ""])
def test_backdrop_can_be_turned_off(value):
    assert config.validate_background(value) == config.NO_BACKGROUND


@pytest.mark.parametrize(
    "value",
    ["../../etc/passwd", "/etc/passwd", "http://example.com/x.png",
     "not-a-file.png", "tavern.svg/../../secret"],
)
def test_backdrop_must_name_a_bundled_file(value):
    """The value becomes a URL path, so free text would point anywhere."""
    with pytest.raises(config.SettingsError):
        config.validate_background(value)


@pytest.mark.parametrize("value", [-1, 101, 999, "abc"])
def test_backdrop_fade_is_bounded(value):
    payload = {
        "backends": [{"name": "e", "kind": "echo"}],
        "tiers": {"blocking": "e", "foreground": "e", "background": "e"},
        "background_dim": value,
    }
    with pytest.raises(config.SettingsError):
        config.build_settings(payload, Settings())
