"""Character card import/export (§11, §17)."""

from __future__ import annotations

import base64
import json
import struct
import zlib

import pytest

from app import cards
from app.models import Band, Character, LorebookEntry, VariableSchema


def make_png(chunks: dict[str, str]) -> bytes:
    """A minimal valid PNG carrying tEXt chunks, built the way card tools do."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    for keyword, value in chunks.items():
        out += chunk(b"tEXt", keyword.encode("latin-1") + b"\x00" + value.encode("latin-1"))
    out += chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00", 9))
    out += chunk(b"IEND", b"")
    return out


V2_CARD = {
    "spec": "chara_card_v2",
    "spec_version": "2.0",
    "data": {
        "name": "Mira",
        "description": "Keeps the back bar.",
        "personality": "Dry and observant.",
        "scenario": "A tavern on the coast road.",
        "first_mes": '*She looks up.* "Sit wherever."',
        "mes_example": "{{user}}: Hi\n{{char}}: Hm.",
        "character_book": {
            "entries": [
                {"keys": ["tavern"], "content": "The Long Wait.", "constant": True},
                {"keys": "Harrow, harbourmaster", "content": "Owed money.", "insertion_order": 3},
            ]
        },
    },
}


def test_v2_json_maps_onto_our_schema():
    character = cards.from_card_json(V2_CARD)
    assert character.name == "Mira"
    # description and personality both belong in the persona.
    assert "Keeps the back bar." in character.persona
    assert "Dry and observant." in character.persona
    assert character.scenario.startswith("A tavern")
    assert character.first_mes.startswith("*She looks up.*")


def test_character_book_becomes_a_lorebook():
    character = cards.from_card_json(V2_CARD)
    assert len(character.lorebook) == 2
    constant = next(e for e in character.lorebook if e.constant)
    assert constant.content == "The Long Wait."
    # A comma-joined key string is a real shape in the wild.
    harrow = next(e for e in character.lorebook if not e.constant)
    assert harrow.keys == ["Harrow", "harbourmaster"]
    assert harrow.insertion_depth == 3


def test_cards_without_a_state_schema_get_the_canonical_set():
    character = cards.from_card_json(V2_CARD)
    assert set(character.state_schema) == {"willingness", "trust", "mood", "energy"}


def test_our_extensions_survive_import():
    card = json.loads(json.dumps(V2_CARD))
    card["data"]["extensions"] = {
        "personal_tavern": {
            "pfp_set": {"neutral": "n.png"},
            "nudges": [{"pattern": "x", "variable": "trust", "delta": 1}],
            "colours": {"--c-dialogue": "#fff"},
            "state_schema": {"resolve": {"min": 0, "max": 5, "baseline": 2, "bands": []}},
        }
    }
    character = cards.from_card_json(card)
    assert character.pfp_set == {"neutral": "n.png"}
    assert character.nudges[0]["variable"] == "trust"
    assert character.colours == {"--c-dialogue": "#fff"}
    assert set(character.state_schema) == {"resolve"}


def test_png_card_is_read_from_the_chara_chunk():
    payload = base64.b64encode(json.dumps(V2_CARD).encode()).decode()
    character = cards.from_bytes(make_png({"chara": payload}), "mira.png")
    assert character.name == "Mira"


def test_v3_chunk_wins_over_v2():
    v3 = json.loads(json.dumps(V2_CARD))
    v3["data"]["name"] = "Mira v3"
    png = make_png(
        {
            "ccv3": base64.b64encode(json.dumps(v3).encode()).decode(),
            "chara": base64.b64encode(json.dumps(V2_CARD).encode()).decode(),
        }
    )
    assert cards.from_bytes(png, "mira.png").name == "Mira v3"


def test_unencoded_json_in_the_chunk_still_parses():
    character = cards.from_bytes(make_png({"chara": json.dumps(V2_CARD)}), "mira.png")
    assert character.name == "Mira"


def test_png_without_card_data_raises():
    with pytest.raises(cards.CardError, match="no character data"):
        cards.from_bytes(make_png({"Comment": "just a picture"}), "plain.png")


def test_non_card_file_raises():
    with pytest.raises(cards.CardError):
        cards.from_bytes(b"this is not a card", "notes.txt")


def test_export_round_trips_through_import():
    original = Character(
        id="mira",
        name="Mira",
        persona="Dry and observant.",
        scenario="A tavern.",
        first_mes="Hello.",
        nudges=[{"pattern": r"\bthanks\b", "variable": "trust", "delta": 1}],
        pfp_set={"neutral": "n.png"},
        backgrounds=[{"id": "bar", "img": "bar.jpg"}],
        lorebook=[LorebookEntry(keys=["tavern"], content="The Long Wait.", constant=True)],
        state_schema={
            "resolve": VariableSchema(
                min=0, max=5, baseline=2, decay=0.1, label="Resolve",
                bands=[Band(range=(0, 5), label="steady", guidance="holds the line")],
            )
        },
    )
    exported = cards.to_card_json(original)
    assert exported["spec"] == "chara_card_v2"  # still importable elsewhere

    reimported = cards.from_card_json(exported, character_id="mira")
    assert reimported.name == original.name
    assert reimported.persona == original.persona
    assert reimported.nudges == original.nudges
    assert reimported.pfp_set == original.pfp_set
    assert reimported.backgrounds == original.backgrounds
    assert [e.content for e in reimported.lorebook] == ["The Long Wait."]
    assert set(reimported.state_schema) == {"resolve"}
    assert reimported.state_schema["resolve"].bands[0].guidance == "holds the line"


def test_a_card_with_no_schema_falls_back_to_the_canonical_set():
    exported = cards.to_card_json(Character(id="x", name="Y", persona="Z"))
    reimported = cards.from_card_json(exported)
    assert set(reimported.state_schema) == {"willingness", "trust", "mood", "energy"}


def test_our_own_export_format_imports_directly():
    original = Character(id="x", name="Y", persona="Z")
    assert cards.from_card_json(json.loads(original.model_dump_json())).name == "Y"


def test_bundled_example_card_loads(tmp_path):
    from app.config import DATA_DIR

    loaded = cards.load_directory(DATA_DIR / "characters")
    assert any(c.name == "Mira" for c in loaded)
    mira = next(c for c in loaded if c.name == "Mira")
    assert mira.nudges and mira.lorebook and mira.pfp_set


# ------------------------------------------- tags this app cannot draw (§8)


def test_html_in_a_card_is_taken_out_on_import():
    """Cards from the browse sites carry HTML — an <img> in the greeting is
    common. Model output is drawn with textContent, so every tag arrives on
    screen as its own source and costs prompt tokens on every turn to do it."""
    from app.cards import from_card_json

    card = {
        "spec": "chara_card_v2",
        "data": {
            "name": "Wren",
            "description": "A ferryman.<br>Quiet.",
            "first_mes": '*She waits.*<img src="https://example.test/a-very-long-name.webp">',
            "mes_example": "{{char}}: <b>Fine.</b>",
            "alternate_greetings": ['<img src="x.png"> *She is late.*'],
        },
    }
    character = from_card_json(card)
    assert "<img" not in character.first_mes
    assert character.first_mes.strip() == "*She waits.*"
    assert "<br>" not in character.persona
    assert "<b>" not in character.example_dialogue
    assert "<img" not in character.alternate_greetings[0]


def test_prose_that_merely_contains_an_angle_bracket_is_left_alone():
    from app.cards import from_card_json

    card = {"spec": "chara_card_v2", "data": {"name": "Wren", "description": "3 < 5 always"}}
    assert from_card_json(card).persona == "3 < 5 always"
