"""Full-data backup (ISSUES-TRIAGE.md #1, app/backup.py)."""

from __future__ import annotations

import io
import zipfile

import pytest

from app import backup, repo


@pytest.fixture
def isolated_backgrounds(tmp_path, monkeypatch):
    from app import config

    path = tmp_path / "backgrounds"
    monkeypatch.setattr(config, "USER_BACKGROUND_DIR", path)
    return path


@pytest.fixture
def isolated_attachments(tmp_path, monkeypatch):
    from app import attachments

    path = tmp_path / "attachments"
    monkeypatch.setattr(attachments, "ATTACHMENT_DIR", path)
    return path


def test_backup_contains_a_readable_db_snapshot(db, character):
    payload = backup.build(db)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert "tavern.db" in zf.namelist()
        # A valid SQLite file starts with this exact 16-byte magic string.
        assert zf.read("tavern.db")[:16] == b"SQLite format 3\x00"


def test_backup_picks_up_a_write_still_only_in_the_wal_file(db, character, tmp_path):
    """The database runs in WAL mode (db.py) — a plain file copy can miss a
    commit that never made it out of `-wal`. The backup has to go through
    the same writer thread every other write does, not read the file raw."""
    import sqlite3

    chat = repo.create_chat(db, character.id, "wal chat")
    payload = backup.build(db)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        snapshot_bytes = zf.read("tavern.db")

    snapshot_path = tmp_path / "snapshot.db"
    snapshot_path.write_bytes(snapshot_bytes)
    conn = sqlite3.connect(snapshot_path)
    try:
        row = conn.execute("SELECT id FROM chats WHERE id = ?", (chat["id"],)).fetchone()
    finally:
        conn.close()
    assert row is not None


def test_backup_includes_settings_json_when_present(db, isolated_settings):
    from app import config

    config.save_settings(config.Settings(), isolated_settings)
    payload = backup.build(db)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert "settings.json" in zf.namelist()


def test_backup_omits_settings_json_when_there_is_none(db, isolated_settings):
    # isolated_settings points at a path that is never written in this test.
    payload = backup.build(db)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert "settings.json" not in zf.namelist()


def test_backup_includes_asset_directories(
    db, isolated_avatars, isolated_avatar_idle, isolated_backgrounds, isolated_attachments
):
    isolated_avatars.mkdir(parents=True)
    (isolated_avatars / "mira.png").write_bytes(b"fake-png")
    isolated_backgrounds.mkdir(parents=True)
    (isolated_backgrounds / "tavern.jpg").write_bytes(b"fake-jpg")

    payload = backup.build(db)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = zf.namelist()
        assert "avatars/mira.png" in names
        assert "backgrounds/tavern.jpg" in names


def test_backup_skips_asset_directories_that_dont_exist(
    db, isolated_avatars, isolated_avatar_idle, isolated_backgrounds, isolated_attachments
):
    # None of the isolated dirs are created — build() must not raise.
    payload = backup.build(db)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.testzip() is None  # a valid, uncorrupted archive


def test_filename_is_a_timestamped_zip():
    name = backup.filename()
    assert name.startswith("tavern-backup-")
    assert name.endswith(".zip")


# ------------------------------------------------------------------- route


def test_download_backup_route_serves_a_zip(client):
    response = client.get("/api/backup")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert "tavern.db" in zf.namelist()


def test_download_backup_route_download_flag_sets_content_disposition(client):
    plain = client.get("/api/backup")
    assert "content-disposition" not in plain.headers

    attachment = client.get("/api/backup?download=true")
    disposition = attachment.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=")
    assert disposition.endswith('.zip"')
