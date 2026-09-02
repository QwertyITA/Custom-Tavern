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


def test_sprites_round_trip_through_a_png():
    """§ cards.embed_sprites / extract_sprites — the write side of the same
    tEXt-chunk trick the card payload itself already rides in (§
    card_json_from_png above), just under its own keyword."""
    png = make_png({})
    sprites = {
        "happy": {"img_b64": base64.b64encode(b"HAPPYBYTES").decode(), "description": "a grin"}
    }
    out = cards.embed_sprites(png, sprites)
    assert out.startswith(cards.PNG_SIGNATURE)
    assert cards.extract_sprites(out) == sprites
    # A PNG with nothing of ours embedded reads back empty, not an error —
    # every card that predates this feature looks like this.
    assert cards.extract_sprites(png) == {}


def test_to_card_png_bundles_the_card_and_its_extra_portraits():
    base = make_png({})
    character = Character(
        id="x", name="Mira",
        pfp_set={"neutral": "/avatars/n.png", "happy": "/avatars/h.png"},
        expression_meta={"happy": {"description": "a grin"}},
    )
    out = cards.to_card_png(character, base, {"happy": b"HAPPYBYTES"})

    back = cards.card_json_from_png(out)
    assert back.get("name") == "Mira"
    sprites = cards.extract_sprites(out)
    assert base64.b64decode(sprites["happy"]["img_b64"]) == b"HAPPYBYTES"
    assert sprites["happy"]["description"] == "a grin"


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


# ------------------------------------------------------ v3 field passthrough


def test_a_v3_cards_spec_round_trips_as_v3():
    """ISSUES-TRIAGE.md #12: a card imported as v3 used to always be
    re-exported as v2 — this app's own spec, not the source's."""
    v3_card = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {"name": "Nyx", "description": "A wanderer.", "first_mes": "Hey."},
    }
    character = cards.from_card_json(v3_card)
    assert character.card_spec == "chara_card_v3"
    assert character.card_spec_version == "3.0"

    exported = cards.to_card_json(character)
    assert exported["spec"] == "chara_card_v3"
    assert exported["spec_version"] == "3.0"


def test_fields_this_app_never_reads_survive_a_round_trip():
    """tags, creator, creator_notes and v3's own source/nickname/
    creation_date/group_only_greetings all used to be silently dropped on
    export — nothing in this app ever asked to change any of them."""
    v3_card = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "Nyx", "description": "A wanderer.", "first_mes": "Hey.",
            "tags": ["oc", "fantasy"],
            "creator": "somebody",
            "character_version": "1.2",
            "nickname": "Nyxie",
            "source": ["https://example.invalid/card"],
            "creation_date": 1700000000,
            "group_only_greetings": ["Hello, everyone."],
        },
    }
    character = cards.from_card_json(v3_card)
    exported = cards.to_card_json(character)
    data = exported["data"]
    assert data["tags"] == ["oc", "fantasy"]
    assert data["creator"] == "somebody"
    assert data["character_version"] == "1.2"
    assert data["nickname"] == "Nyxie"
    assert data["source"] == ["https://example.invalid/card"]
    assert data["creation_date"] == 1700000000
    assert data["group_only_greetings"] == ["Hello, everyone."]


def test_an_edit_in_this_app_still_wins_over_passthrough():
    """Passthrough never gets a chance to disagree with a field this app
    actually owns — name isn't swept into it in the first place."""
    v2_card = {
        "spec": "chara_card_v2", "spec_version": "2.0",
        "data": {"name": "Original Name", "description": "Z", "first_mes": "Hi.",
                  "tags": ["kept"]},
    }
    character = cards.from_card_json(v2_card)
    character.name = "Edited Name"
    exported = cards.to_card_json(character)
    assert exported["data"]["name"] == "Edited Name"
    assert exported["data"]["tags"] == ["kept"]  # untouched field still preserved


def test_a_character_written_in_this_app_gets_v2_defaults_with_no_passthrough():
    exported = cards.to_card_json(Character(id="x", name="Y", persona="Z"))
    data = exported["data"]
    assert exported["spec"] == "chara_card_v2"
    assert data["tags"] == []
    assert data["creator"] == ""
    assert data["character_version"] == "1"


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


# ------------------------------------------------ the card is its own picture


def png_card(payload: dict) -> bytes:
    """A minimal PNG carrying a card in a tEXt chunk, the way a real one does."""
    import base64
    import json
    import struct
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    text = b"chara\x00" + base64.b64encode(json.dumps(payload).encode())
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"tEXt", text)
        + chunk(b"IEND", b"")
    )


def test_importing_a_png_card_keeps_the_picture(client, isolated_settings, isolated_avatars):
    """The JSON lives in a tEXt chunk of a PNG that *is* the portrait. Reading
    the chunk and dropping the file left every imported card faceless."""
    from app import repo
    from app.db import get_db

    data = png_card({"spec": "chara_card_v2", "data": {"name": "Wren", "description": "A ferryman."}})
    response = client.post("/api/characters/import?filename=wren.png", content=data)
    assert response.status_code == 200

    character = repo.get_character(get_db(), response.json()["id"])
    assert character.pfp_set.get("neutral", "").startswith("/avatars/")
    saved = isolated_avatars / character.pfp_set["neutral"].rsplit("/", 1)[-1]
    assert saved.read_bytes() == data


def test_exporting_as_png_bundles_extra_portraits_and_reimport_restores_them(
    client, isolated_settings, isolated_avatars
):
    """§ export_character_png, main.py — the whole point: a plain JSON
    export never carried the image bytes behind pfp_set, only local paths,
    so every expression but whichever happened to be a card's own visible
    picture was lost the moment the export left this device."""
    from app import repo
    from app.db import get_db

    character_id = client.get("/api/characters").json()[0]["id"]
    neutral_png = make_png({})
    happy_png = make_png({})
    isolated_avatars.mkdir(parents=True, exist_ok=True)
    (isolated_avatars / "n.png").write_bytes(neutral_png)
    (isolated_avatars / "h.png").write_bytes(happy_png)

    response = client.put(f"/api/characters/{character_id}", json={
        "pfp_set": {"neutral": "/avatars/n.png", "happy": "/avatars/h.png"},
        "expression_meta": {"happy": {"description": "a grin"}},
    })
    assert response.status_code == 200

    exported = client.get(f"/api/characters/{character_id}/export.png")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("image/png")
    png_bytes = exported.content
    sprites = cards.extract_sprites(png_bytes)
    assert base64.b64decode(sprites["happy"]["img_b64"]) == happy_png
    assert sprites["happy"]["description"] == "a grin"

    # Reimporting it (a stand-in for "on another device") restores the
    # extra portrait as a real file again, not a dangling path.
    reimport = client.post("/api/characters/import?filename=roundtrip.png", content=png_bytes)
    assert reimport.status_code == 200
    reimported = repo.get_character(get_db(), reimport.json()["id"])
    assert reimported.pfp_set.get("happy", "").startswith("/avatars/")
    assert reimported.expression_meta.get("happy", {}).get("description") == "a grin"
    saved = isolated_avatars / reimported.pfp_set["happy"].rsplit("/", 1)[-1]
    assert saved.read_bytes() == happy_png


def test_exporting_as_png_needs_a_resolvable_neutral_portrait(client):
    """A card whose neutral picture is bundled static art (never this app's
    own upload) has no raw bytes this can read generically — refused rather
    than silently exporting a blank or wrong image."""
    character_id = client.get("/api/characters").json()[0]["id"]
    response = client.put(f"/api/characters/{character_id}", json={
        "pfp_set": {"neutral": "mira/neutral.png"},
    })
    assert response.status_code == 200
    exported = client.get(f"/api/characters/{character_id}/export.png")
    assert exported.status_code == 400


def test_a_json_card_imports_without_a_picture(client, isolated_settings):
    import json

    from app import repo
    from app.db import get_db

    card = {"spec": "chara_card_v2", "data": {"name": "Wren", "description": "A ferryman."}}
    response = client.post(
        "/api/characters/import?filename=wren.json", content=json.dumps(card).encode()
    )
    character = repo.get_character(get_db(), response.json()["id"])
    assert character.pfp_set == {}


def test_an_oversized_card_is_refused_with_the_limit(client, isolated_settings):
    """Picking the wrong file — a video instead of a card — should not have to
    land in memory before it is rejected (§KNOWN-ISSUES.md)."""
    from app import config

    huge = b"\x00" * (config.MAX_CARD_IMPORT_BYTES + 1)
    response = client.post("/api/characters/import?filename=huge.png", content=huge)
    assert response.status_code == 400
    assert "MB" in response.json()["detail"]


def test_the_portrait_shape_round_trips_through_a_card():
    from app.cards import from_card_json, to_card_json
    from app.models import Character

    character = Character(id="c", name="Wren", pfp_shape="square")
    assert from_card_json(to_card_json(character)).pfp_shape == "square"
    assert Character(id="c", name="Wren").pfp_shape == "portrait"
