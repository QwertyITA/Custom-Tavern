"""Settings: file-backed, env-overridable, no external config library.

Defaults are chosen so a fresh clone runs end to end with the built-in `echo`
provider — no network, no Ollama, no keys. Point a tier at a real backend in
`data/settings.json` when you have one.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TAVERN_DATA_DIR", REPO_ROOT / "data"))
STATIC_DIR = REPO_ROOT / "static"

# What the client sees in place of a stored key. Sending it back unchanged means
# "keep what you have" — the browser is never given the real value, so it cannot
# leak one back through a form post, a screenshot or a cached response.
MASK = "***"

VALID_KINDS = ("echo", "ollama", "llamacpp", "openai", "horde")
VALID_TEMPLATES = ("auto", "messages", "chatml", "llama3", "mistral", "plain")
TIERS = ("blocking", "foreground", "background")

# Sensible starting values per backend kind. The settings screen fills these in
# when you pick a kind, so a new backend is one field away from working instead
# of a blank form you have to know the answers for. Single source of truth: the
# GUI reads these from /api/settings rather than hardcoding its own copy.
KIND_DEFAULTS: dict[str, dict[str, Any]] = {
    "echo": {
        "model": "echo-1", "base_url": "", "template": "auto", "timeout": 120,
        "note": "Built-in fake model. No network, no key — use it to test the UI.",
    },
    "ollama": {
        "model": "llama3.1:8b", "base_url": "http://127.0.0.1:11434",
        "template": "auto", "timeout": 120,
        "note": "Your PC over Tailscale. Use its tailnet IP, not localhost, "
                "unless Ollama runs on the phone.",
    },
    "llamacpp": {
        "model": "qwen2.5-3b-instruct", "base_url": "http://127.0.0.1:8080",
        "template": "chatml", "timeout": 300,
        "note": "llama.cpp server on the phone. Foreground only — it throttles "
                "hard when backgrounded.",
    },
    "openai": {
        "model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1",
        "template": "auto", "timeout": 120,
        "note": "Any OpenAI-compatible /v1 endpoint: hosted APIs, LM Studio, "
                "vLLM, text-generation-webui.",
    },
    "horde": {
        "model": "", "base_url": "https://aihorde.net/api/v2",
        "api_key": "0000000000", "template": "chatml", "timeout": 300,
        "note": "Free and network-only, so it is the safest background tier. "
                "Slow and queue-bound: never use it for the reply. "
                "0000000000 is the anonymous key — a real key gets priority.",
    },
}


def kind_defaults() -> dict[str, dict[str, Any]]:
    """Defaults minus the prose, plus the prose under its own key."""
    return {kind: dict(values) for kind, values in KIND_DEFAULTS.items()}


# Every appearance value the settings screen can edit (§12: "custom colours for
# every element"). Declared here rather than in the frontend so the editor is
# generated from one list — adding a token here makes it editable, with no
# matching change in the GUI, and nothing is customisable only by editing JSON.
THEME_TOKENS: list[dict[str, str]] = [
    {"var": "--bg", "label": "Background", "group": "Surfaces", "type": "color", "default": "#fdf7f9"},
    {"var": "--panel", "label": "Bars", "group": "Surfaces", "type": "color", "default": "#ffffff"},
    {"var": "--panel-2", "label": "Inputs", "group": "Surfaces", "type": "color", "default": "#f7eef2"},
    {"var": "--line", "label": "Borders", "group": "Surfaces", "type": "color", "default": "#eedde4"},

    {"var": "--text", "label": "Text", "group": "Text", "type": "color", "default": "#332c30"},
    {"var": "--muted", "label": "Muted text", "group": "Text", "type": "color", "default": "#8b7d84"},
    {"var": "--accent", "label": "Accent", "group": "Text", "type": "color", "default": "#c2617f"},

    {"var": "--c-default", "label": "Narration", "group": "Message markup", "type": "color", "default": "#3c3438"},
    {"var": "--c-dialogue", "label": "Dialogue", "group": "Message markup", "type": "color", "default": "#a34a6d"},
    {"var": "--c-action", "label": "Action", "group": "Message markup", "type": "color", "default": "#6f5aa8"},
    {"var": "--c-strong", "label": "Emphasis", "group": "Message markup", "type": "color", "default": "#a9722c"},

    {"var": "--ai-bubble", "label": "Character bubble", "group": "Bubbles", "type": "color", "default": "#ffffff"},
    {"var": "--user-bubble", "label": "Your bubble", "group": "Bubbles", "type": "color", "default": "#fbeef3"},
    {"var": "--ok", "label": "Success", "group": "Bubbles", "type": "color", "default": "#3f7d5a"},
    {"var": "--error", "label": "Error", "group": "Bubbles", "type": "color", "default": "#c0405e"},

    {"var": "--radius", "label": "Corner rounding", "group": "Layout", "type": "px", "default": "10px"},
    {"var": "--font-size", "label": "Text size", "group": "Layout", "type": "px", "default": "15px"},
    {"var": "--bubble-max", "label": "Bubble width", "group": "Layout", "type": "pct", "default": "86%"},
]

THEME_VARS = {token["var"]: token for token in THEME_TOKENS}

# Values are written straight into style.setProperty, so they are restricted to
# shapes that cannot carry anything else — no url(), no expression, no escape
# out of the declaration.
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_PX_RE = re.compile(r"^\d{1,3}px$")
_PCT_RE = re.compile(r"^\d{1,3}%$")


def validate_theme(raw: Any) -> dict[str, str]:
    """Keep only known tokens whose values match their declared shape."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for name, value in raw.items():
        token = THEME_VARS.get(str(name))
        if token is None:
            continue
        text = str(value).strip()
        pattern = {"color": _COLOR_RE, "px": _PX_RE, "pct": _PCT_RE}[token["type"]]
        if not pattern.match(text):
            raise SettingsError(f"{token['label']}: {text!r} is not a valid {token['type']} value")
        if text != token["default"]:  # only store what differs from the default
            out[str(name)] = text
    return out


def theme_tokens() -> list[dict[str, str]]:
    return [dict(token) for token in THEME_TOKENS]


BACKGROUND_DIR = STATIC_DIR / "backgrounds"
BACKGROUND_SUFFIXES = (".svg", ".jpg", ".jpeg", ".png", ".webp", ".avif")
NO_BACKGROUND = "none"


# Backdrops the user adds themselves. Deliberately not static/backgrounds:
# that folder is tracked and ships with the app, so writing uploads into it
# would put personal images in a public git working tree and make every
# `git pull` a possible conflict. data/ is gitignored.
USER_BACKGROUND_DIR = DATA_DIR / "backgrounds"
MAX_BACKGROUND_BYTES = 12 * 1024 * 1024

# Persona portraits. Same reasoning as backdrops — user images belong in the
# gitignored data directory, not in the tracked static tree.
AVATAR_DIR = DATA_DIR / "avatars"
MAX_AVATAR_BYTES = 4 * 1024 * 1024


def _listing(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return [
        f.name for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in BACKGROUND_SUFFIXES
    ]


def available_backgrounds() -> list[str]:
    """Every backdrop that can be chosen, bundled and uploaded together.

    Enumerated rather than hardcoded, so dropping a file in either folder makes
    it selectable with no code change.
    """
    return sorted(set(_listing(BACKGROUND_DIR)) | set(_listing(USER_BACKGROUND_DIR)))


def user_backgrounds() -> list[str]:
    """Only the uploaded ones — the bundled art is not the user's to delete."""
    return sorted(_listing(USER_BACKGROUND_DIR))


def background_path(name: str) -> Path | None:
    """Resolve a backdrop name to a file, uploads winning over bundled.

    The name reaches this from a URL, so it is matched against the directory
    listing rather than joined onto a path — a name is only ever a name.
    """
    if name not in available_backgrounds():
        return None
    uploaded = USER_BACKGROUND_DIR / name
    if uploaded.is_file():
        return uploaded
    bundled = BACKGROUND_DIR / name
    return bundled if bundled.is_file() else None


def validate_background(raw: Any) -> str:
    """Only a bundled filename or "none".

    The value becomes a URL path, so accepting free text would let a saved
    setting point anywhere; matching against the directory listing keeps it to
    files that actually exist.
    """
    value = str(raw or "").strip()
    if value in ("", NO_BACKGROUND):
        return NO_BACKGROUND
    if value in available_backgrounds():
        return value
    raise SettingsError(f"unknown background {value!r}")


def user_avatars() -> list[str]:
    return sorted(_listing(AVATAR_DIR))


def avatar_path(name: str) -> Path | None:
    """Resolve an avatar name to a file. Matched against the listing rather
    than joined onto a path, so a name is only ever a name."""
    if name not in user_avatars():
        return None
    path = AVATAR_DIR / name
    return path if path.is_file() else None


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

    # Appearance overrides: CSS variable -> value. Only keys in THEME_TOKENS,
    # only values matching that token's shape (§12, §18.4).
    theme: dict[str, str] = field(default_factory=dict)

    # Backdrop behind the chat. A bundled filename or "none"; background_dim is
    # how strongly the theme background is washed over it, so the image can be
    # present without the text fighting it.
    background: str = "tavern.svg"
    background_dim: int = 70

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
        """Safe to serve to the client: real keys become MASK, never plaintext."""
        d = asdict(self)
        for b in d["backends"]:
            if b.get("api_key"):
                b["api_key"] = MASK
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


class SettingsError(ValueError):
    """The submitted settings are not usable. Message is shown to the user."""


def merge_backend(raw: dict, existing: list[BackendConfig]) -> BackendConfig:
    """Validate one backend from a client payload, restoring a masked key.

    The client is never sent a real key, so an unedited field comes back as
    MASK. That means "leave it alone" — writing MASK through would destroy the
    key, and making the user retype it every time they change a model would
    train them to keep it somewhere less safe.
    """
    if not isinstance(raw, dict):
        raise SettingsError("each backend must be an object")
    name = str(raw.get("name", "")).strip()
    if not name:
        raise SettingsError("every backend needs a name")
    kind = str(raw.get("kind", "")).strip()
    if kind not in VALID_KINDS:
        raise SettingsError(f"{name}: unknown backend kind {kind!r}")
    template = str(raw.get("template", "auto")).strip() or "auto"
    if template not in VALID_TEMPLATES:
        raise SettingsError(f"{name}: unknown template {template!r}")

    key = str(raw.get("api_key", ""))
    if key == MASK:
        by_name = {b.name: b for b in existing}
        key = by_name[name].api_key if name in by_name else ""

    try:
        timeout = float(raw.get("timeout", 120.0))
    except (TypeError, ValueError):
        raise SettingsError(f"{name}: timeout must be a number") from None

    models = raw.get("models") or []
    if not isinstance(models, list):
        raise SettingsError(f"{name}: models must be a list")

    return BackendConfig(
        name=name,
        kind=kind,
        model=str(raw.get("model", "")),
        base_url=str(raw.get("base_url", "")).strip(),
        api_key=key,
        template=template,
        timeout=timeout,
        models=[str(m) for m in models],
    )


def _merge_secrets(incoming: list[dict], existing: list[BackendConfig]) -> list[BackendConfig]:
    out = [merge_backend(raw, existing) for raw in incoming]
    if len({b.name for b in out}) != len(out):
        raise SettingsError("backend names must be unique")
    if not out:
        raise SettingsError("at least one backend is required")
    return out


def build_settings(payload: dict[str, Any], current: Settings) -> Settings:
    """Validate a client payload into a Settings, without writing anything."""
    if not isinstance(payload, dict):
        raise SettingsError("settings must be an object")

    backends = _merge_secrets(payload.get("backends", []), current.backends)
    names = {b.name for b in backends}

    tiers = dict(current.tiers)
    for tier, backend_name in (payload.get("tiers") or {}).items():
        if tier not in TIERS:
            raise SettingsError(f"unknown tier {tier!r}")
        if backend_name not in names:
            raise SettingsError(f"tier {tier} points at unknown backend {backend_name!r}")
        tiers[tier] = backend_name
    for tier in TIERS:
        if tiers.get(tier) not in names:
            raise SettingsError(f"tier {tier} must be assigned to a backend")

    settings = Settings(backends=backends, tiers=tiers)

    numeric = {
        "port": int, "token_budget": int, "verbatim_window": int, "summary_budget": int,
        "lorebook_scan_depth": int, "lorebook_total_budget": int, "memory_max_injected": int,
        "background_retries": int, "blocking_await_ms": int, "pass_timeout": float,
    }
    for field_name, caster in numeric.items():
        if field_name in payload:
            try:
                value = caster(payload[field_name])
            except (TypeError, ValueError):
                raise SettingsError(f"{field_name} must be a number") from None
            if value < 0:
                raise SettingsError(f"{field_name} cannot be negative")
            setattr(settings, field_name, value)
        else:
            setattr(settings, field_name, getattr(current, field_name))

    if not 1 <= settings.port <= 65535:
        raise SettingsError("port must be between 1 and 65535")

    settings.host = str(payload.get("host", current.host)) or current.host
    settings.strip_user_turn_leakage = bool(
        payload.get("strip_user_turn_leakage", current.strip_user_turn_leakage)
    )
    settings.theme = (
        validate_theme(payload["theme"]) if "theme" in payload else dict(current.theme)
    )
    settings.background = (
        validate_background(payload["background"])
        if "background" in payload else current.background
    )
    try:
        dim = int(payload.get("background_dim", current.background_dim))
    except (TypeError, ValueError):
        raise SettingsError("background_dim must be a number") from None
    if not 0 <= dim <= 100:
        raise SettingsError("background_dim must be between 0 and 100")
    settings.background_dim = dim
    return settings


def settings_path() -> Path:
    return DATA_DIR / "settings.json"


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Write settings.json with real keys, atomically and owner-only.

    Atomic because a half-written settings file on a phone that ran out of
    battery mid-save would take the app down on next launch. Owner-only because
    this is the one file in the project that holds plaintext credentials.
    """
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = asdict(settings)  # deliberately not to_dict(): keys must be real
    body = json.dumps(payload, indent=2) + "\n"

    tmp = path.with_name(path.name + ".tmp")
    # Create with 0600 from the outset rather than chmod-ing afterwards, so the
    # key is never briefly readable by another app on the device.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


SETTINGS = load_settings()


def reload_settings() -> Settings:
    global SETTINGS
    SETTINGS = load_settings()
    return SETTINGS


def apply_settings(settings: Settings) -> Settings:
    """Swap in a new Settings for the running process."""
    global SETTINGS
    SETTINGS = settings
    return SETTINGS
