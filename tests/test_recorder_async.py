import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from src import recordtest


class FakeOBSClient:
    def __init__(self):
        self.obs_process = None
        self._raw_client = object()
        self.connect = Mock()
        self.disconnect = Mock()
        self.setup_record_output = Mock()
        self.setup_sync_elements = Mock()
        self.apply_record_output_settings = Mock(return_value=True)
        self.apply_audio_profile = Mock(return_value=True)
        self.get_audio_device_catalog = Mock(return_value={})
        self.get_sync_source_id = Mock(return_value=1)
        self.set_sync_marker_enabled = Mock()
        self.start_recording = Mock()
        self.stop_recording = Mock(return_value="game.mp4")
        self.is_recording_active = Mock(return_value=True)
        self.shutdown = Mock()

    @property
    def raw_client(self):
        return self._raw_client


def run(coro):
    return asyncio.run(coro)


def runtime_dir(name):
    path = Path("tests") / "_tmp" / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_for(tmp_path, **overrides):
    data = {
        "paths": {
            "recordings_dir": str(tmp_path),
            "json_dir": str(tmp_path / "json"),
        },
        "polling": {
            "end_error_limit": 3,
            "end_missing_grace_sec": 60.0,
            "end_poll_sec": 0.1,
            "event_poll_sec": 0.1,
        },
    }
    data.update(overrides)
    return recordtest.AppConfig.from_dict(data)


def test_wait_for_game_start_retries_after_timeout_without_real_sleep():
    tmp_path = runtime_dir("retry")
    config = config_for(tmp_path)
    riot_client = Mock()
    riot_client.get_all_game_data = AsyncMock(
        side_effect=[
            None,
            {"gameData": {"gameTime": 12.0}, "allPlayers": []},
        ]
    )
    riot_client.get_active_player_name = AsyncMock(return_value="Tester#JP1")
    riot_client.get_event_data = AsyncMock(return_value={"Events": []})

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=riot_client,
        auto_setup=False,
    )
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    started = run(recorder.wait_for_game_start_async())

    assert started is True
    assert riot_client.get_all_game_data.await_count == 2
    recorder.wait_with_stop_async.assert_awaited_once()


def test_recorder_constructor_has_no_obs_side_effects_until_open():
    tmp_path = runtime_dir("constructor_no_side_effects")
    config = config_for(tmp_path)
    obs_client = FakeOBSClient()

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=obs_client,
        riot_api_client=Mock(),
        auto_setup=True,
    )

    obs_client.connect.assert_not_called()
    obs_client.setup_record_output.assert_not_called()
    obs_client.setup_sync_elements.assert_not_called()

    recorder.open()
    recorder.open()

    obs_client.connect.assert_called_once()
    obs_client.setup_record_output.assert_called_once()
    obs_client.setup_sync_elements.assert_called_once()


def test_game_end_event_flow_stops_recording_without_real_sleep():
    tmp_path = runtime_dir("game_end")
    config = config_for(tmp_path)
    obs_client = FakeOBSClient()
    riot_client = Mock()
    riot_client.get_all_game_data = AsyncMock(
        return_value={
            "gameData": {"gameTime": 1200.0},
            "allPlayers": [
                {"summonerName": "Tester#JP1", "championName": "Malphite", "team": "ORDER"},
                {"summonerName": "Enemy#JP1", "championName": "Darius", "team": "CHAOS"},
            ],
        }
    )
    riot_client.get_active_player_name = AsyncMock(return_value="Tester#JP1")
    riot_client.get_event_data = AsyncMock(
        return_value={
            "Events": [
                {
                    "EventID": 50,
                    "EventName": "BuildingKill",
                    "EventTime": 850.0,
                    "KillerName": "Tester#JP1",
                },
                {
                    "EventID": 99,
                    "EventName": "GameEnd",
                    "EventTime": 1200.0,
                    "Result": "Win",
                    "WinningTeam": "ORDER",
                },
            ]
        }
    )

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=obs_client,
        riot_api_client=riot_client,
        auto_setup=False,
    )
    recorder.my_name = "Tester#JP1"
    recorder.my_name_short = "Tester"
    recorder.recording_started = True
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    ended = run(recorder.record_until_end_async())
    recorder.stop_recording()

    assert ended is True
    assert recorder.game_result == "Win"
    assert recorder.winning_team == "ORDER"
    assert recorder.enemy_champions == ["Darius"]
    assert any(event.get("EventName") == "BuildingKill" for event in recorder.saved_events)
    building_event = next(event for event in recorder.all_events if event.get("EventName") == "BuildingKill")
    assert building_event["KillerTeam"] == "ORDER"
    assert building_event["team_relation"] == "own"
    obs_client.stop_recording.assert_called_once()
    recorder.wait_with_stop_async.assert_not_awaited()


def test_start_recording_failure_raises_and_does_not_create_session_data():
    tmp_path = runtime_dir("recording_start_failure")
    config = config_for(tmp_path)
    obs_client = FakeOBSClient()
    obs_client.start_recording.side_effect = RuntimeError("OBS busy")

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=obs_client,
        riot_api_client=Mock(),
        auto_setup=False,
    )
    recorder.session_started = True

    with pytest.raises(recordtest.RecorderError, match="OBS録画開始に失敗"):
        run(recorder.start_recording_async())

    assert recorder.recording_started is False
    assert recorder.record_path is None
    assert recorder.has_session_data() is False


def test_record_until_end_does_not_stop_on_missing_api_count_before_grace():
    tmp_path = runtime_dir("missing_api_grace")
    config = config_for(tmp_path)
    obs_client = FakeOBSClient()
    riot_client = Mock()
    riot_client.get_all_game_data = AsyncMock(
        side_effect=[
            None,
            None,
            None,
            {"gameData": {"gameTime": 1500.0}, "allPlayers": []},
        ]
    )
    riot_client.get_active_player_name = AsyncMock(return_value="Tester#JP1")
    riot_client.get_event_data = AsyncMock(
        return_value={"Events": [{"EventID": 10, "EventName": "GameEnd", "EventTime": 1500.0}]}
    )

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=obs_client,
        riot_api_client=riot_client,
        auto_setup=False,
    )
    recorder.recording_started = True
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    ended = run(recorder.record_until_end_async())

    assert ended is True
    assert riot_client.get_all_game_data.await_count == 4


def test_record_until_end_ignores_temporary_failures_past_error_limit():
    tmp_path = runtime_dir("temporary_failures_do_not_end")
    config = config_for(
        tmp_path,
        polling={
            "end_error_limit": 3,
            "end_missing_grace_sec": 0.0,
            "end_poll_sec": 0.1,
            "event_poll_sec": 0.1,
        },
    )
    obs_client = FakeOBSClient()
    riot_client = Mock()
    riot_client.get_all_game_data_result = AsyncMock(
        side_effect=[
            recordtest.RiotPollResult(recordtest.RiotPollStatus.TEMPORARY_FAILURE, error="timeout 1"),
            recordtest.RiotPollResult(recordtest.RiotPollStatus.TEMPORARY_FAILURE, error="timeout 2"),
            recordtest.RiotPollResult(recordtest.RiotPollStatus.TEMPORARY_FAILURE, error="timeout 3"),
            recordtest.RiotPollResult(
                recordtest.RiotPollStatus.IN_GAME,
                payload={"gameData": {"gameTime": 1500.0}, "allPlayers": []},
            ),
        ]
    )
    riot_client.get_all_game_data = AsyncMock(return_value=None)
    riot_client.get_active_player_name = AsyncMock(return_value="Tester#JP1")
    riot_client.get_event_data = AsyncMock(
        return_value={"Events": [{"EventID": 10, "EventName": "GameEnd", "EventTime": 1500.0}]}
    )

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=obs_client,
        riot_api_client=riot_client,
        auto_setup=False,
    )
    recorder.recording_started = True
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    ended = run(recorder.record_until_end_async())

    assert ended is True
    assert riot_client.get_all_game_data_result.await_count == 4
    riot_client.get_event_data.assert_awaited_once()


def test_record_until_end_stops_after_confirmed_not_in_game():
    tmp_path = runtime_dir("not_in_game_confirmed")
    config = config_for(
        tmp_path,
        polling={
            "end_error_limit": 3,
            "end_missing_grace_sec": 0.0,
            "end_poll_sec": 0.1,
            "event_poll_sec": 0.1,
        },
    )
    obs_client = FakeOBSClient()
    riot_client = Mock()
    riot_client.get_all_game_data_result = AsyncMock(
        side_effect=[
            recordtest.RiotPollResult(recordtest.RiotPollStatus.NOT_IN_GAME),
            recordtest.RiotPollResult(recordtest.RiotPollStatus.NOT_IN_GAME),
            recordtest.RiotPollResult(recordtest.RiotPollStatus.NOT_IN_GAME),
        ]
    )
    riot_client.get_all_game_data = AsyncMock(return_value=None)
    riot_client.get_active_player_name = AsyncMock(return_value="Tester#JP1")
    riot_client.get_event_data = AsyncMock(return_value={"Events": []})

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=obs_client,
        riot_api_client=riot_client,
        auto_setup=False,
    )
    recorder.recording_started = True
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    ended = run(recorder.record_until_end_async())

    assert ended is True
    assert riot_client.get_all_game_data_result.await_count == 3
    riot_client.get_event_data.assert_not_awaited()


def test_save_json_is_idempotent(monkeypatch):
    tmp_path = runtime_dir("save_json_idempotent")
    config = config_for(tmp_path)
    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=Mock(),
        auto_setup=False,
    )
    recorder.output_file = tmp_path / "json" / "session.json"
    recorder.sync_game_time = 1.0
    recorder.all_events = [{"EventID": 1, "EventName": "GameStart", "EventTime": 0.0}]
    saved_payloads = []
    monkeypatch.setattr(recordtest, "save_payload", lambda path, payload: saved_payloads.append((path, payload)))
    monkeypatch.setattr(recordtest, "enforce_storage_limit", lambda *args, **kwargs: None)

    recorder.save_json()
    recorder.save_json()

    assert len(saved_payloads) == 1
    assert saved_payloads[0][1]["schema_version"] == 1
    assert recorder.has_session_data() is False
