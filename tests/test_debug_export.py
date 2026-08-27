"""Freeze/crash export (app/debug_export.py)."""

from __future__ import annotations

import time

import pytest

from app import debug_export


def insert_run(db, *, chat_id="c1", pass_id="basic", status="running",
                started_at=None, error=None, run_id=None):
    run_id = run_id or f"run-{pass_id}-{status}-{started_at}"
    db.write_sync(lambda conn: conn.execute(
        "INSERT INTO pass_runs(id, chat_id, turn, pass_id, tier, status, started_at, error) "
        "VALUES(?,?,0,?,?,?,?,?)",
        (run_id, chat_id, pass_id, "blocking", status, started_at, error),
    ))
    return run_id


# ------------------------------------------------------------- _log_tail


def test_log_tail_reads_the_real_file(db, monkeypatch, tmp_path):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    (tmp_path / "tavern.log").write_text("line one\nline two\nline three\n")
    assert "line two" in debug_export._log_tail()


def test_log_tail_only_keeps_the_end(db, monkeypatch, tmp_path):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    lines = [f"line {i}" for i in range(debug_export.LOG_TAIL_LINES + 50)]
    (tmp_path / "tavern.log").write_text("\n".join(lines))
    tail = debug_export._log_tail()
    assert "line 0\n" not in tail
    assert f"line {len(lines) - 1}" in tail


def test_log_tail_missing_file_says_so(db, monkeypatch, tmp_path):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert "no log file" in debug_export._log_tail()


# ------------------------------------------------------------ _stuck_runs


def test_flags_a_run_stuck_well_past_the_threshold(db):
    old = time.time() - debug_export.STUCK_AFTER_SECONDS - 30
    insert_run(db, status="running", started_at=old)
    stuck = debug_export._stuck_runs(db)
    assert len(stuck) == 1
    assert stuck[0]["running_for_seconds"] >= debug_export.STUCK_AFTER_SECONDS


def test_does_not_flag_a_run_still_within_the_threshold(db):
    insert_run(db, status="running", started_at=time.time() - 5)
    assert debug_export._stuck_runs(db) == []


def test_does_not_flag_a_finished_run(db):
    insert_run(db, status="done", started_at=time.time() - 999)
    assert debug_export._stuck_runs(db) == []


# -------------------------------------------------------- _recent_failures


def test_lists_a_failed_run_with_its_error(db):
    insert_run(db, status="failed", started_at=time.time(), error="backend timed out")
    failures = debug_export._recent_failures(db)
    assert len(failures) == 1
    assert failures[0]["error"] == "backend timed out"


def test_does_not_list_a_non_failed_run(db):
    insert_run(db, status="done", started_at=time.time())
    assert debug_export._recent_failures(db) == []


# ------------------------------------------------------- _settings_snapshot


def test_settings_snapshot_masks_a_real_key(db):
    from app import config

    settings = config.Settings(
        backends=[config.BackendConfig(name="x", kind="openai_compat", model="m", api_key="sk-real-secret")]
    )
    config.apply_settings(settings)
    snap = debug_export._settings_snapshot()
    assert snap["backends"][0]["api_key"] == config.MASK
    assert "sk-real-secret" not in str(snap)


def test_settings_snapshot_excludes_the_writing_library(db):
    from app import config

    settings = config.Settings(
        prompt_sections=[{"id": "craft:pov", "enabled": True, "text": "a very long instruction " * 50}]
    )
    config.apply_settings(settings)
    snap = debug_export._settings_snapshot()
    assert "prompt_sections" not in snap


# -------------------------------------------------------------------- build


def test_build_includes_every_section(db, sched):
    text = debug_export.build(db, sched)
    for heading in (
        "process health", "settings (masked)", "still 'running'",
        "failed pass runs", "server log",
    ):
        assert heading in text


def test_build_surfaces_a_stuck_run_by_id(db, sched):
    insert_run(db, chat_id="stuck-chat", status="running",
               started_at=time.time() - debug_export.STUCK_AFTER_SECONDS - 10)
    text = debug_export.build(db, sched)
    assert "stuck-chat" in text


def test_filename_is_a_timestamped_txt():
    name = debug_export.filename()
    assert name.startswith("tavern-debug-")
    assert name.endswith(".txt")


# ------------------------------------------------------------------- route


def test_download_debug_export_route_serves_text(client):
    response = client.get("/api/debug/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "process health" in response.text


def test_download_debug_export_download_flag_sets_content_disposition(client):
    plain = client.get("/api/debug/export")
    assert "content-disposition" not in plain.headers

    attachment = client.get("/api/debug/export?download=true")
    disposition = attachment.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=")
    assert disposition.endswith('.txt"')
