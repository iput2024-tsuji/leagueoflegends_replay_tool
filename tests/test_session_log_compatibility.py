import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from src.analytics import GameDataAnalyzer
from src.player import ReplaySelectDialog
from src.session_log import (
    SESSION_LOG_SCHEMA_VERSION,
    load_session_payload,
    load_session_payload_result,
    save_session_payload,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "session_logs"
SUPPORTED_FIXTURES = (
    "legacy-no-schema.json",
    "current-complete.json",
    "current-optional-missing.json",
    "partial-save.json",
)
REQUIRED_NORMALIZED_KEYS = {
    "schema_version",
    "session_status",
    "session_phase",
    "failure_reason",
    "summoner_name",
    "champion_name",
    "enemy_champions",
    "player_team",
    "game_result",
    "winning_team",
    "saved_at",
    "sync_game_time",
    "obs_record_path",
    "paths",
    "match",
    "ban_pick",
    "events",
    "events_all",
    "counts",
}


@pytest.mark.parametrize("fixture_name", SUPPORTED_FIXTURES)
def test_session_log_fixtures_normalize_and_resave(fixture_name, tmp_path):
    fixture_path = FIXTURE_DIR / fixture_name

    normalized = load_session_payload(fixture_path)
    saved_path = tmp_path / fixture_name
    save_session_payload(saved_path, normalized)
    saved = json.loads(saved_path.read_text(encoding="utf-8"))

    assert normalized["schema_version"] == SESSION_LOG_SCHEMA_VERSION
    assert REQUIRED_NORMALIZED_KEYS <= normalized.keys()
    assert REQUIRED_NORMALIZED_KEYS <= saved.keys()
    assert saved["counts"] == {
        "filtered": len(saved["events"]),
        "all": len(saved["events_all"]),
    }
    assert load_session_payload(saved_path) == normalized


def test_legacy_fixture_normalizes_missing_fields_and_relative_path():
    payload = load_session_payload(FIXTURE_DIR / "legacy-no-schema.json")

    assert payload["session_status"] == "completed"
    assert payload["enemy_champions"] == ["Darius", "Lux"]
    assert payload["sync_game_time"] == 12.5
    assert payload["obs_record_path"] == "videos/legacy.mp4"
    assert payload["match"] == {}
    assert payload["ban_pick"] == {}
    assert payload["events_all"] == []


def test_current_and_partial_fixtures_preserve_absolute_paths_and_statuses():
    current = load_session_payload(FIXTURE_DIR / "current-complete.json")
    partial = load_session_payload(FIXTURE_DIR / "partial-save.json")

    assert current["obs_record_path"] == "C:/fixture-media/current.mp4"
    assert current["match"]["game_id"] == "fixture-current-001"
    assert current["ban_pick"]["actions"][0]["champion_name"] == "Darius"
    assert partial["obs_record_path"] == "D:/fixture-media/partial.mkv"
    assert partial["session_status"] == "failed_partial"
    assert partial["failure_reason"] == "fixture recorder interruption"


def test_optional_missing_fixture_uses_safe_defaults():
    payload = load_session_payload(FIXTURE_DIR / "current-optional-missing.json")

    assert payload["session_status"] == "aborted"
    assert payload["enemy_champions"] == []
    assert payload["match"] == {}
    assert payload["ban_pick"] == {}
    assert payload["events"] == []
    assert payload["events_all"] == []
    assert payload["counts"] == {"filtered": 0, "all": 0}


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    (
        (
            "legacy-no-schema.json",
            {
                "champion_name": "Malphite",
                "result": "Win",
                "summoner": "LegacyPlayer#TEST",
                "match_name": "Unknown",
            },
        ),
        (
            "current-complete.json",
            {
                "champion_name": "Ahri",
                "result": "Loss",
                "summoner": "CurrentPlayer#TEST",
                "match_name": "ランク ソロ/デュオ",
            },
        ),
        (
            "current-optional-missing.json",
            {
                "champion_name": "Lux",
                "result": "Unknown",
                "summoner": "OptionalMissing#TEST",
                "match_name": "Unknown",
            },
        ),
        (
            "partial-save.json",
            {
                "champion_name": "Nami",
                "result": "Unknown",
                "summoner": "PartialPlayer#TEST",
                "match_name": "Unknown",
            },
        ),
    ),
)
def test_player_list_metadata_reads_compatible_fixtures(fixture_name, expected):
    context = SimpleNamespace(recordings_dir=FIXTURE_DIR / "fixture-recordings")

    meta = ReplaySelectDialog.load_meta(context, FIXTURE_DIR / fixture_name)

    assert {key: meta[key] for key in expected} == expected
    assert meta["saved_at"]
    assert meta["video_exists"] is False


def test_analytics_dataframe_reads_all_supported_fixtures_and_reports_future_version():
    analyzer = GameDataAnalyzer(json_dir=FIXTURE_DIR)

    dataframe = analyzer.load_dataframe()

    assert set(dataframe["match_id"]) == {
        "legacy-no-schema",
        "game:fixture-current-001",
        "current-optional-missing",
        "partial-save",
    }
    assert set(dataframe["champion_name"]) == {"Malphite", "Ahri", "Lux", "Nami"}

    legacy = dataframe[dataframe["match_id"] == "legacy-no-schema"].iloc[0]
    assert legacy["event_name"] == "GameStart"
    assert legacy["enemy_champions"] == ["Darius", "Lux"]
    assert bool(legacy["is_win"]) is True

    current_events = dataframe[dataframe["match_id"] == "game:fixture-current-001"]
    assert set(current_events["event_name"]) == {"GameStart", "DragonKill"}
    assert current_events["is_win"].eq(False).all()

    missing = dataframe[dataframe["match_id"] == "current-optional-missing"].iloc[0]
    assert pd.isna(missing["event_name"])
    assert missing["enemy_champions"] == []

    assert len(analyzer.load_errors) == 1
    assert analyzer.load_errors[0]["path"].endswith("future-version.json")
    assert "unsupported session log schema_version" in analyzer.load_errors[0]["error"]


def test_future_version_fixture_is_rejected_explicitly():
    path = FIXTURE_DIR / "future-version.json"

    with pytest.raises(ValueError, match="unsupported session log schema_version: 999"):
        load_session_payload(path)

    result = load_session_payload_result(path)
    assert result.valid is False
    assert result.payload is None
    assert result.errors == ("ValueError: unsupported session log schema_version: 999",)
