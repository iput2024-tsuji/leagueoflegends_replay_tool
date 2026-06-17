import json
import os

import pytest

from src.session_log import (
    SESSION_LOG_SCHEMA_VERSION,
    SessionLogRepository,
    SessionLogV1,
    load_session_payload,
    load_session_payload_result,
    migrate_session_payload,
)


def test_session_log_payload_round_trips_with_schema_version(tmp_path):
    path = tmp_path / "session.json"
    log = SessionLogV1(
        summoner_name="Tester#JP1",
        champion_name="Malphite",
        enemy_champions=["Darius", "Lux"],
        player_team="ORDER",
        game_result="Win",
        winning_team="ORDER",
        saved_at="2026-01-01 00:00:00",
        sync_game_time=12.5,
        obs_record_path="game.mp4",
        recordings_dir="recordings",
        json_path=str(path),
        match={
            "queue_id": 420,
            "queue_type": "RANKED_SOLO_5x5",
            "display_name": "ランク ソロ/デュオ",
        },
        ban_pick={
            "actions": [
                {
                    "order": 1,
                    "type": "ban",
                    "champion_id": 122,
                    "champion_name": "Darius",
                }
            ]
        },
        events=[{"EventID": 1, "EventName": "GameStart"}],
        events_all=[{"EventID": 1, "EventName": "GameStart"}],
    )
    path.write_text(json.dumps(log.to_payload(), ensure_ascii=False), encoding="utf-8")

    payload = load_session_payload(path)

    assert payload["schema_version"] == SESSION_LOG_SCHEMA_VERSION
    assert payload["session_status"] == "completed"
    assert payload["session_phase"] is None
    assert payload["failure_reason"] is None
    assert payload["summoner_name"] == "Tester#JP1"
    assert payload["enemy_champions"] == ["Darius", "Lux"]
    assert payload["match"]["queue_id"] == 420
    assert payload["ban_pick"]["actions"][0]["champion_name"] == "Darius"
    assert payload["counts"] == {"filtered": 1, "all": 1}


def test_session_log_loader_accepts_legacy_payload_without_schema_version(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps(
            {
                "summoner_name": "Tester#JP1",
                "enemy_champions": "Darius,Lux",
                "sync_game_time": "12.5",
                "events": [{"EventID": 1}],
            }
        ),
        encoding="utf-8",
    )

    payload = load_session_payload(path)

    assert payload["schema_version"] == SESSION_LOG_SCHEMA_VERSION
    assert payload["enemy_champions"] == ["Darius", "Lux"]
    assert payload["sync_game_time"] == 12.5
    assert payload["match"] == {}
    assert payload["ban_pick"] == {}
    assert payload["counts"] == {"filtered": 1, "all": 0}


def test_session_log_loader_rejects_unknown_schema_version(tmp_path):
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported session log schema_version"):
        load_session_payload(path)


def test_session_log_migration_pipeline_applies_incremental_migrations():
    def migrate_v1(payload):
        payload["session_status"] = "completed"
        payload["match"] = {"queue_id": 420}
        return payload

    payload = migrate_session_payload(
        {"schema_version": 1, "summoner_name": "Tester#JP1"},
        target_version=2,
        migrations={1: migrate_v1},
    )

    assert payload["schema_version"] == 2
    assert payload["session_status"] == "completed"
    assert payload["match"] == {"queue_id": 420}
    assert payload["summoner_name"] == "Tester#JP1"


def test_session_log_migration_pipeline_requires_explicit_migration():
    with pytest.raises(ValueError, match="missing session log migration: v1 -> v2"):
        migrate_session_payload({"schema_version": 1}, target_version=2, migrations={})


def test_session_log_result_reports_load_errors(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{broken", encoding="utf-8")

    result = load_session_payload_result(path)

    assert result.valid is False
    assert result.payload is None
    assert result.errors
    assert str(path) == str(result.path)


def test_session_log_repository_saves_atomically(tmp_path):
    path = tmp_path / "session.json"
    payload = SessionLogV1(
        session_status="failed_partial",
        failure_reason="OBS disconnected",
        events_all=[{"EventID": 1}],
    ).to_payload()

    SessionLogRepository().save_payload(path, payload)

    assert not path.with_name("session.json.tmp").exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["session_status"] == "failed_partial"
    assert loaded["failure_reason"] == "OBS disconnected"


def test_session_log_repository_leaves_previous_file_when_replace_fails(monkeypatch, tmp_path):
    path = tmp_path / "session.json"
    path.write_text('{"old": true}', encoding="utf-8")

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        SessionLogRepository().save_payload(path, {"new": True})

    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
