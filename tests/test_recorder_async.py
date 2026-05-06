import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, Mock

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
