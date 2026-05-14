import json

import pytest

from src.session_log import SESSION_LOG_SCHEMA_VERSION, SessionLogV1, load_session_payload, load_session_payload_result


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
        events=[{"EventID": 1, "EventName": "GameStart"}],
        events_all=[{"EventID": 1, "EventName": "GameStart"}],
    )
    path.write_text(json.dumps(log.to_payload(), ensure_ascii=False), encoding="utf-8")

    payload = load_session_payload(path)

    assert payload["schema_version"] == SESSION_LOG_SCHEMA_VERSION
    assert payload["summoner_name"] == "Tester#JP1"
    assert payload["enemy_champions"] == ["Darius", "Lux"]
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
    assert payload["counts"] == {"filtered": 1, "all": 0}


def test_session_log_loader_rejects_unknown_schema_version(tmp_path):
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported session log schema_version"):
        load_session_payload(path)


def test_session_log_result_reports_load_errors(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{broken", encoding="utf-8")

    result = load_session_payload_result(path)

    assert result.valid is False
    assert result.payload is None
    assert result.errors
    assert str(path) == str(result.path)
