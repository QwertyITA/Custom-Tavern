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

from .models import AuthorsNote, Character, CharacterReactions, LorebookEntry, PfpEffect, VariableSchema
from .postprocess import strip_unrenderable
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


def _authors_note_from(ours: dict[str, Any]) -> AuthorsNote:
    """Ours, and only ours — no other frontend writes this field."""
    try:
        return AuthorsNote.model_validate(ours.get("authors_note") or {})
    except ValueError:
        return AuthorsNote()


def _model_or_default(cls: type, raw: Any) -> Any:
    """Ours, and only ours, same as `_authors_note_from` — a card missing the
    field, or carrying junk in it, gets the model's own defaults rather than
    failing the whole import over one extension field."""
    if isinstance(raw, dict):
        try:
            return cls.model_validate(raw)
        except ValueError:
            pass
    return cls()


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

    def prose(*keys: str) -> str:
        """A card field as prose this app can actually draw.

        Cards from the browse sites carry HTML — an `<img>` in the greeting is
        common, `<b>` and `<font>` less so. Model output is rendered with
        textContent (§8), so every one of them arrives on screen as its own
        source, and an image tag with a long URL costs sixty tokens of prompt
        on every turn to do it. Taken out on the way in, where it happens once,
        rather than at each of the places the text is used.
        """
        return strip_unrenderable(
            "\n\n".join(p for p in (str(body.get(k) or "").strip() for k in keys) if p)
        )

    persona = prose("description", "personality")

    pfp_set = {}
    ours = extensions.get("personal_tavern", {}) if isinstance(extensions, dict) else {}
    if isinstance(ours.get("pfp_set"), dict):
        pfp_set = {str(k): str(v) for k, v in ours["pfp_set"].items()}

    return Character(
        id=character_id or uuid.uuid4().hex,
        name=name,
        version=int(ours.get("version", 1) or 1),
        persona=persona,
        first_mes=prose("first_mes"),
        alternate_greetings=[
            strip_unrenderable(str(g))
            for g in (body.get("alternate_greetings") or [])
            if str(g).strip()
        ],
        example_dialogue=prose("mes_example"),
        scenario=prose("scenario"),
        system_prompt=prose("system_prompt"),
        post_history_instructions=prose("post_history_instructions"),
        pfp_set=pfp_set,
        pfp_shape="square" if ours.get("pfp_shape") == "square" else "portrait",
        pfp_effect=_model_or_default(PfpEffect, ours.get("pfp_effect")),
        reactions=_model_or_default(CharacterReactions, ours.get("reactions")),
        backgrounds=list(ours.get("backgrounds") or []),
        state_schema=_state_schema_from(extensions if isinstance(extensions, dict) else {}),
        nudges=list(ours.get("nudges") or []),
        lorebook=_lorebook_from_book(body.get("character_book")),
        default_toggles=list(ours.get("default_toggles") or []),
        colours=dict(ours.get("colours") or {}),
        authors_note=_authors_note_from(ours),
        stop_strings=[str(x) for x in (ours.get("stop_strings") or []) if str(x).strip()],
    )


def from_bytes(data: bytes, filename: str = "") -> Character:
    if data.startswith(PNG_SIGNATURE) or filename.lower().endswith(".png"):
        return from_card_json(card_json_from_png(data))
    try:
        return from_card_json(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CardError(f"unreadable card file: {exc}") from exc


def to_card_json(character: Character) -> dict[str, Any]:
    """Export as a TavernCard v2, readable by SillyTavern and anything else.

    The v2 payload under `data` is the real card. The same fields are also
    mirrored at the top level, which is the v1 shape: that is what SillyTavern
    itself writes, and it is what lets an older or stricter importer read the
    card at all instead of seeing an object it has no rule for. Everything
    ours that the format has no place for rides in `extensions`, which
    importers are required to preserve and ignore.
    """
    payload = json.loads(character.model_dump_json())
    book = {
        "name": f"{character.name} lorebook",
        "entries": [
            {
                "id": index,
                "keys": entry.keys,
                "secondary_keys": [],
                "comment": "",
                "content": entry.content,
                "constant": entry.constant,
                "selective": False,
                "insertion_order": entry.insertion_depth,
                "enabled": entry.enabled,
                "position": "before_char",
                "case_sensitive": entry.case_sensitive,
                "token_budget": entry.token_budget,
                "extensions": {},
            }
            for index, entry in enumerate(character.lorebook)
        ],
    }

    data = {
        "name": character.name,
        "description": character.persona,
        "personality": "",
        "scenario": character.scenario,
        "first_mes": character.first_mes,
        "mes_example": character.example_dialogue,
        "creator_notes": "Exported by Personal Tavern",
        "system_prompt": character.system_prompt,
        "post_history_instructions": character.post_history_instructions,
        "alternate_greetings": list(character.alternate_greetings),
        "tags": [],
        "creator": "",
        "character_version": str(character.version),
        "character_book": book,
        "extensions": {
            "personal_tavern": {
                "version": character.version,
                "state_schema": payload["state_schema"],
                "nudges": character.nudges,
                "pfp_set": character.pfp_set,
                "pfp_shape": character.pfp_shape,
                "pfp_effect": payload["pfp_effect"],
                "reactions": payload["reactions"],
                "backgrounds": character.backgrounds,
                "default_toggles": character.default_toggles,
                "colours": character.colours,
                "authors_note": payload["authors_note"],
                "stop_strings": character.stop_strings,
            }
        },
    }

    return {
        # v1 fields at the top level, for importers that never learned v2.
        "name": data["name"],
        "description": data["description"],
        "personality": data["personality"],
        "scenario": data["scenario"],
        "first_mes": data["first_mes"],
        "mes_example": data["mes_example"],
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": data,
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
