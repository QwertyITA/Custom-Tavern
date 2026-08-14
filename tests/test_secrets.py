"""Guards on the credential protections themselves.

This repository is public, so the ignore rules and the masking below are load
bearing. A regression here is not a broken test — it is a leaked key.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

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
