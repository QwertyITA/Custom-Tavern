"""Settings: file-backed, env-overridable, no external config library.

Defaults are chosen so a fresh clone runs end to end with the built-in `echo`
provider — no network, no Ollama, no keys. Point a tier at a real backend in
`data/settings.json` when you have one.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TAVERN_DATA_DIR", REPO_ROOT / "data"))
STATIC_DIR = REPO_ROOT / "static"


@dataclass
class BackendConfig:
    """One inference endpoint. `kind` selects the provider implementation."""

    name: str
    kind: str  # ollama | openai | horde | llamacpp | echo
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    template: str = "auto"  # auto | chatml | llama3 | mistral | plain | messages
    timeout: float = 120.0
    # Horde-only knobs; ignored elsewhere.
    models: list[str] = field(default_factory=list)


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787

    # Tier → backend name (§3). Latency tolerance, not importance.
    tiers: dict[str, str] = field(
        default_factory=lambda: {
            "blocking": "echo",
            "foreground": "echo",
            "background": "echo",
        }
    )
    backends: list[BackendConfig] = field(
        default_factory=lambda: [BackendConfig(name="echo", kind="echo", model="echo-1")]
    )

    # Context management (§7).
    token_budget: int = 4096
    verbatim_window: int = 24  # messages kept in full text
    summary_budget: int = 700  # tokens before the summary is re-summarised
    lorebook_scan_depth: int = 6
    lorebook_total_budget: int = 600
    memory_max_injected: int = 6

    # Pass engine (§5).
    background_retries: int = 2
    blocking_await_ms: int = 1500  # how long the next turn waits on a late blocking pass
    pass_timeout: float = 120.0

    # Rendering / hygiene.
    strip_user_turn_leakage: bool = True

    @property
    def data_dir(self) -> Path:
        return DATA_DIR

    def backend(self, name: str) -> BackendConfig:
        for b in self.backends:
            if b.name == name:
                return b
        raise KeyError(f"unknown backend {name!r}")

    def backend_for_tier(self, tier: str) -> BackendConfig:
        name = self.tiers.get(tier) or self.tiers.get("blocking") or "echo"
        try:
            return self.backend(name)
        except KeyError:
            return BackendConfig(name="echo", kind="echo", model="echo-1")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for b in d["backends"]:
            if b.get("api_key"):
                b["api_key"] = "***"
        return d


def _coerce(raw: dict[str, Any]) -> Settings:
    backends = [BackendConfig(**b) for b in raw.pop("backends", [])] or None
    known = {f for f in Settings.__dataclass_fields__}
    kwargs = {k: v for k, v in raw.items() if k in known}
    if backends:
        kwargs["backends"] = backends
    return Settings(**kwargs)


def load_settings(path: Path | None = None) -> Settings:
    path = path or DATA_DIR / "settings.json"
    settings = Settings()
    if path.exists():
        try:
            settings = _coerce(json.loads(path.read_text()))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:  # keep booting
            print(f"[config] ignoring bad {path}: {exc}")

    if env_port := os.environ.get("TAVERN_PORT"):
        settings.port = int(env_port)
    if env_host := os.environ.get("TAVERN_HOST"):
        settings.host = env_host

    # Env overrides for the common single-backend case, so a phone can be
    # reconfigured without editing JSON.
    if url := os.environ.get("TAVERN_OLLAMA_URL"):
        model = os.environ.get("TAVERN_OLLAMA_MODEL", "llama3.1:8b")
        settings.backends = [
            b for b in settings.backends if b.name != "ollama"
        ] + [BackendConfig(name="ollama", kind="ollama", base_url=url, model=model)]
        settings.tiers["blocking"] = "ollama"
    return settings


SETTINGS = load_settings()


def reload_settings() -> Settings:
    global SETTINGS
    SETTINGS = load_settings()
    return SETTINGS
