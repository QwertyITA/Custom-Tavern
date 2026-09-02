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
import zlib
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


# ----------------------------------------------------------- writing chunks

# Not a standard keyword (ccv3/chara are) — this app's own bonus, read only
# by the code just below and written only by to_card_png. A plain PNG
# viewer, and any other card reader, just never looks for it and sees an
# ordinary portrait; only this app's own import goes looking further.
SPRITE_KEYWORD = "tavern-sprites"


def _text_chunk(keyword: str, text: str) -> bytes:
    """One tEXt chunk, length-prefixed and CRC'd — the write side of
    read_png_text_chunks' tEXt branch above. `text` must already be pure
    ASCII (base64 output always is), since tEXt is latin-1 only."""
    body = keyword.encode("latin-1") + b"\x00" + text.encode("latin-1")
    chunk_type = b"tEXt"
    payload = chunk_type + body
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    return struct.pack(">I", len(body)) + payload + struct.pack(">I", crc)


def _insert_before_iend(png: bytes, chunk: bytes) -> bytes:
    """Splice one raw chunk in just ahead of IEND — the one position an
    ancillary chunk (§ the PNG spec) is always valid to add, regardless of
    whatever else the file already carries."""
    idx = png.rfind(b"IEND")
    if idx < 4:
        raise CardError("not a well-formed PNG (no IEND chunk)")
    insert_at = idx - 4  # IEND's own 4-byte length prefix
    return png[:insert_at] + chunk + png[insert_at:]


def embed_sprites(png: bytes, sprites: dict[str, dict[str, str]]) -> bytes:
    """Bundles extra expression portraits into one exported PNG.

    Base64 JSON in a tEXt chunk, the same trick the card payload itself
    already rides in under the `chara` keyword (§ card_json_from_png) — so
    a normal image viewer just shows the neutral portrait on the canvas,
    and only this app's own import path (§ extract_sprites) goes looking
    for anything past it. `sprites` is `{name: {"img_b64": ...,
    "description": ...}}`; the image bytes are base64 a second time here
    (once for this JSON, same as any binary-in-JSON has to be) — simpler
    than a binary-safe format for what is, in practice, a handful of small
    cropped portraits, not a media library.
    """
    if not png.startswith(PNG_SIGNATURE):
        raise CardError("not a PNG file")
    payload = base64.b64encode(json.dumps(sprites).encode("utf-8")).decode("ascii")
    return _insert_before_iend(png, _text_chunk(SPRITE_KEYWORD, payload))


def extract_sprites(png: bytes) -> dict[str, dict[str, str]]:
    """The other half of embed_sprites. Empty (never an error) for a PNG
    with nothing of ours in it — every card that predates this feature, and
    every card that never had extra expressions to carry."""
    raw = read_png_text_chunks(png).get(SPRITE_KEYWORD)
    if not raw:
        return {}
    try:
        decoded = base64.b64decode(raw, validate=False).decode("utf-8")
        data = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def to_card_png(character: Character, base_png: bytes, sprite_files: dict[str, bytes]) -> bytes:
    """The character as one PNG: the neutral portrait as the visible image,
    the card JSON in a `chara` chunk exactly like any V2/V3 card already
    carries (round-trips through the plain PNG import unchanged), and every
    other pfp_set entry's own image bytes bundled alongside it (§
    embed_sprites) — described from character.expression_meta where one
    exists. `sprite_files` is keyed by the same pfp_set name, holding each
    slot's raw image bytes; entries this app cannot resolve to a real file
    (§ export_character, main.py) are simply not offered here, not an error.
    """
    if not base_png.startswith(PNG_SIGNATURE):
        raise CardError("the neutral portrait is not a PNG")
    chara_b64 = base64.b64encode(
        json.dumps(to_card_json(character)).encode("utf-8")
    ).decode("ascii")
    out = _insert_before_iend(base_png, _text_chunk("chara", chara_b64))
    if not sprite_files:
        return out
    meta = character.expression_meta or {}
    sprites = {
        key: {
            "img_b64": base64.b64encode(data).decode("ascii"),
            "description": (meta.get(key) or {}).get("description", ""),
        }
        for key, data in sprite_files.items()
    }
    return embed_sprites(out, sprites)


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


# The card fields already read into a Character field of their own — kept
# out of card_passthrough so a value edited in this app always wins over
# whatever the original import carried, rather than the two disagreeing.
_KNOWN_DATA_FIELDS = {
    "name", "description", "personality", "scenario", "first_mes", "mes_example",
    "system_prompt", "post_history_instructions", "alternate_greetings",
    "character_book", "extensions", "spec", "spec_version", "data",
}


def _passthrough_from(body: dict[str, Any]) -> dict[str, Any]:
    """Whatever `body` carries that nothing on Character reads — tags,
    creator, creator_notes, a v3 card's source/nickname/creation_date/
    group_only_greetings, and so on (§ Character.card_passthrough,
    ISSUES-TRIAGE.md #12). Only top-level fields; a lorebook entry's own
    extras stay out of scope for the same reason `character_book` itself is
    excluded — that one field already has a real, modelled shape.
    """
    return {k: v for k, v in body.items() if k not in _KNOWN_DATA_FIELDS}


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
        card_spec=str(raw.get("spec") or "chara_card_v2"),
        card_spec_version=str(raw.get("spec_version") or "2.0"),
        card_passthrough=_passthrough_from(body),
    )


def from_bytes(data: bytes, filename: str = "") -> Character:
    if data.startswith(PNG_SIGNATURE) or filename.lower().endswith(".png"):
        return from_card_json(card_json_from_png(data))
    try:
        return from_card_json(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CardError(f"unreadable card file: {exc}") from exc


def to_card_json(character: Character) -> dict[str, Any]:
    """Export as a TavernCard, readable by SillyTavern and anything else.

    The payload under `data` is the real card. The same fields are also
    mirrored at the top level, which is the v1 shape: that is what SillyTavern
    itself writes, and it is what lets an older or stricter importer read the
    card at all instead of seeing an object it has no rule for. Everything
    ours that the format has no place for rides in `extensions`, which
    importers are required to preserve and ignore.

    `spec`/`spec_version` mirror whatever the source card declared
    (`character.card_spec`/`card_spec_version`) rather than always writing
    v2 — a card imported as v3 round-trips as v3. And `character.
    card_passthrough` — whatever the import carried that nothing on this
    model reads, tags/creator/creator_notes/a v3 card's source or
    creation_date and the rest — is merged in underneath this app's own
    fields, so editing a character here doesn't silently blank the parts of
    its card this app was never going to touch anyway
    (§ ISSUES-TRIAGE.md #12).
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

    # Our own fields last: card_passthrough is whatever this app never read
    # off the original import, and none of it may override a value this app
    # actually owns and might have edited since.
    data = {
        **character.card_passthrough,
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
    data.setdefault("tags", [])
    data.setdefault("creator", "")
    data.setdefault("character_version", str(character.version))

    return {
        # v1 fields at the top level, for importers that never learned v2.
        "name": data["name"],
        "description": data["description"],
        "personality": data["personality"],
        "scenario": data["scenario"],
        "first_mes": data["first_mes"],
        "mes_example": data["mes_example"],
        "spec": character.card_spec,
        "spec_version": character.card_spec_version,
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
