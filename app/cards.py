"""Character card import/export (§11, §17).

Reads TavernCard v2/v3 JSON and `.png` cards, and maps them onto our schema —
which adds `state_schema` and `nudges` on top of the standard fields.

PNG parsing is done by hand over the chunk structure rather than with Pillow:
Pillow is a compiled dependency and this has to install cleanly in Termux.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
import uuid
from pathlib import Path
from typing import Any

from .models import Character, LorebookEntry, VariableSchema
from .state import DEFAULT_STATE_SCHEMA

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CARD_KEYWORDS = ("ccv3", "chara")


class CardError(ValueError):
    """The file is not a character card we can read."""


# --------------------------------------------------------------------- PNG


def _iter_png_chunks(data: bytes):
    if not data.startswith(PNG_SIGNATURE):
        raise CardError("not a PNG file")
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        chunk_type = data[offset + 4 : offset + 8].decode("ascii", "replace")
        body = data[offset + 8 : offset + 8 + length]
        yield chunk_type, body
        offset += 12 + length  # length + type + data + crc
        if chunk_type == "IEND":
            break


def read_png_text_chunks(data: bytes) -> dict[str, str]:
    """Extract tEXt/iTXt entries. Card payloads live in one of these."""
    chunks: dict[str, str] = {}
    for chunk_type, body in _iter_png_chunks(data):
        if chunk_type == "tEXt":
            keyword, _, value = body.partition(b"\x00")
            chunks[keyword.decode("latin-1")] = value.decode("latin-1")
        elif chunk_type == "iTXt":
            keyword, _, rest = body.partition(b"\x00")
            # compression flag, compression method, then two null-terminated
            # language tag / translated keyword fields.
            if len(rest) < 2:
                continue
            compressed = rest[0]
            payload = rest[2:]
            for _ in range(2):
                _, _, payload = payload.partition(b"\x00")
            if compressed:
                import zlib

                try:
                    payload = zlib.decompress(payload)
                except zlib.error:
                    continue
            chunks[keyword.decode("latin-1")] = payload.decode("utf-8", "replace")
    return chunks


def card_json_from_png(data: bytes) -> dict[str, Any]:
    chunks = read_png_text_chunks(data)
    for keyword in CARD_KEYWORDS:
        raw = chunks.get(keyword)
        if not raw:
            continue
        try:
            decoded = base64.b64decode(raw, validate=False).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            decoded = raw
        try:
            return json.loads(decoded)
        except json.JSONDecodeError:
            continue
    raise CardError("no character data found in PNG (looked for ccv3 and chara)")


# ------------------------------------------------------------------ mapping


def _lorebook_from_book(book: dict[str, Any] | None) -> list[LorebookEntry]:
    entries: list[LorebookEntry] = []
    for raw in (book or {}).get("entries", []) or []:
        if isinstance(raw, str):
            continue
        keys = raw.get("keys") or raw.get("key") or []
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        entries.append(
            LorebookEntry(
                keys=[str(k) for k in keys],
                content=str(raw.get("content", "")),
                insertion_depth=int(raw.get("insertion_order", raw.get("insertion_depth", 0)) or 0),
                constant=bool(raw.get("constant", False)),
                token_budget=int(raw.get("token_budget", 200) or 200),
                enabled=bool(raw.get("enabled", True)),
                case_sensitive=bool(raw.get("case_sensitive", False)),
            )
        )
    return entries


def _state_schema_from(extensions: dict[str, Any]) -> dict[str, VariableSchema]:
    """Our own extension field, falling back to the canonical variable set."""
    raw = extensions.get("personal_tavern", {}).get("state_schema") or extensions.get(
        "state_schema"
    )
    source = raw or DEFAULT_STATE_SCHEMA
    out: dict[str, VariableSchema] = {}
    for name, spec in source.items():
        try:
            out[name] = VariableSchema.model_validate(spec)
        except ValueError:
            continue
    return out


def from_card_json(raw: dict[str, Any], *, character_id: str | None = None) -> Character:
    """Map a v1/v2/v3 card (or one of our own exports) onto Character."""
    if not isinstance(raw, dict):
        raise CardError("card is not a JSON object")

    # Our own export format round-trips directly.
    if "persona" in raw and "spec" not in raw:
        data = dict(raw)
        data.setdefault("id", character_id or uuid.uuid4().hex)
        try:
            return Character.model_validate(data)
        except ValueError as exc:
            raise CardError(f"invalid character export: {exc}") from exc

    body = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    name = str(body.get("name") or raw.get("name") or "Unnamed").strip()
    extensions = body.get("extensions") if isinstance(body.get("extensions"), dict) else {}

    persona_parts = [
        str(body.get("description") or "").strip(),
        str(body.get("personality") or "").strip(),
    ]
    persona = "\n\n".join(p for p in persona_parts if p)

    pfp_set = {}
    ours = extensions.get("personal_tavern", {}) if isinstance(extensions, dict) else {}
    if isinstance(ours.get("pfp_set"), dict):
        pfp_set = {str(k): str(v) for k, v in ours["pfp_set"].items()}

    return Character(
        id=character_id or uuid.uuid4().hex,
        name=name,
        version=int(ours.get("version", 1) or 1),
        persona=persona,
        first_mes=str(body.get("first_mes") or ""),
        example_dialogue=str(body.get("mes_example") or ""),
        scenario=str(body.get("scenario") or ""),
        system_prompt=str(body.get("system_prompt") or ""),
        pfp_set=pfp_set,
        backgrounds=list(ours.get("backgrounds") or []),
        state_schema=_state_schema_from(extensions if isinstance(extensions, dict) else {}),
        nudges=list(ours.get("nudges") or []),
        lorebook=_lorebook_from_book(body.get("character_book")),
        default_toggles=list(ours.get("default_toggles") or []),
        colours=dict(ours.get("colours") or {}),
    )


def from_bytes(data: bytes, filename: str = "") -> Character:
    if data.startswith(PNG_SIGNATURE) or filename.lower().endswith(".png"):
        return from_card_json(card_json_from_png(data))
    try:
        return from_card_json(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CardError(f"unreadable card file: {exc}") from exc


def to_card_json(character: Character) -> dict[str, Any]:
    """Export as a v2 card, with our additions under `extensions`.

    Staying inside the v2 envelope means the export is still importable by other
    frontends; the parts they do not understand ride in extensions.
    """
    payload = json.loads(character.model_dump_json())
    return {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": character.name,
            "description": character.persona,
            "personality": "",
            "scenario": character.scenario,
            "first_mes": character.first_mes,
            "mes_example": character.example_dialogue,
            "system_prompt": character.system_prompt,
            "creator_notes": "Exported by Personal Tavern",
            "character_book": {
                "entries": [
                    {
                        "keys": entry.keys,
                        "content": entry.content,
                        "insertion_order": entry.insertion_depth,
                        "constant": entry.constant,
                        "enabled": entry.enabled,
                        "case_sensitive": entry.case_sensitive,
                        "token_budget": entry.token_budget,
                    }
                    for entry in character.lorebook
                ]
            },
            "extensions": {
                "personal_tavern": {
                    "version": character.version,
                    "state_schema": payload["state_schema"],
                    "nudges": character.nudges,
                    "pfp_set": character.pfp_set,
                    "backgrounds": character.backgrounds,
                    "default_toggles": character.default_toggles,
                    "colours": character.colours,
                }
            },
        },
    }


def load_directory(path: Path) -> list[Character]:
    """Load every card file in data/characters/."""
    out: list[Character] = []
    if not path.exists():
        return out
    for file in sorted(path.iterdir()):
        if file.suffix.lower() not in (".json", ".png"):
            continue
        try:
            out.append(from_bytes(file.read_bytes(), file.name))
        except CardError as exc:
            print(f"[cards] skipping {file.name}: {exc}")
    return out
