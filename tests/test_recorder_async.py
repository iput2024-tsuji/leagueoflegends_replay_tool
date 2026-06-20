import asyncio
import inspect
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
        self.prepare_recording_start = Mock()
        self.set_recording_encoder = Mock(
            return_value=recordtest.OBSRecordingEncoderSelection("x264", "obs_x264", "x264", False)
        )
        self.start_recording = Mock()
        self.toggle_recording = Mock()
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
            "end_temporary_failure_grace_sec": 180.0,
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


def test_wait_for_game_start_accepts_zero_live_client_game_time():
    tmp_path = runtime_dir("zero_game_time")
    config = config_for(tmp_path)
    riot_client = Mock()
    riot_client.get_all_game_data_result = AsyncMock(
        return_value=recordtest.RiotPollResult(
            recordtest.RiotPollStatus.IN_GAME,
            payload={"gameData": {"gameTime": 0.0}, "allPlayers": []},
        )
    )
    riot_client.get_active_player_name = AsyncMock(return_value="Tester#JP1")
    riot_client.get_champ_select_session_result = AsyncMock(
        return_value=recordtest.RiotPollResult(recordtest.RiotPollStatus.NOT_IN_GAME)
    )
    riot_client.get_match_metadata = AsyncMock(return_value={})

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=riot_client,
        auto_setup=False,
    )

    assert run(recorder.wait_for_game_start_async()) is True
    assert recorder.game_start_detection_source == "live_client"
    assert recorder.session_started is True


def test_wait_for_game_start_falls_back_to_lcu_in_progress_phase():
    tmp_path = runtime_dir("lcu_gameflow_fallback")
    config = config_for(tmp_path)
    riot_client = Mock()
    riot_client.get_all_game_data_result = AsyncMock(
        return_value=recordtest.RiotPollResult(
            recordtest.RiotPollStatus.TEMPORARY_FAILURE,
            error="Live Client API unavailable",
        )
    )
    riot_client.get_active_player_name = AsyncMock(return_value=None)
    riot_client.get_champ_select_session_result = AsyncMock(
        return_value=recordtest.RiotPollResult(recordtest.RiotPollStatus.NOT_IN_GAME)
    )
    riot_client.get_match_metadata = AsyncMock(
        return_value={
            "gameflow_phase": "InProgress",
            "queue_id": 420,
            "display_name": "ランク ソロ/デュオ",
        }
    )

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=riot_client,
        auto_setup=False,
    )
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    assert run(recorder.wait_for_game_start_async()) is True
    assert recorder.game_start_detection_source == "lcu"
    assert recorder.match_metadata["gameflow_phase"] == "InProgress"
    assert recorder.session_started is True


def test_wait_for_game_start_uses_dedicated_lcu_game_start_phase():
    tmp_path = runtime_dir("lcu_gameflow_phase")
    config = config_for(tmp_path)
    riot_client = Mock()
    riot_client.get_gameflow_phase_result = AsyncMock(
        return_value=recordtest.RiotPollResult(
            recordtest.RiotPollStatus.IN_GAME,
            payload={"phase": "game_start"},
        )
    )
    riot_client.get_all_game_data_result = AsyncMock(
        return_value=recordtest.RiotPollResult(
            recordtest.RiotPollStatus.TEMPORARY_FAILURE,
            error="Live Client API unavailable",
        )
    )
    riot_client.get_active_player_name = AsyncMock(return_value=None)
    riot_client.get_champ_select_session_result = AsyncMock(
        return_value=recordtest.RiotPollResult(recordtest.RiotPollStatus.NOT_IN_GAME)
    )
    riot_client.get_match_metadata = AsyncMock(return_value={})

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=riot_client,
        auto_setup=False,
    )
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    assert run(recorder.wait_for_game_start_async()) is True
    assert recorder.game_start_detection_source == "lcu"
    assert recorder.match_metadata["gameflow_phase"] == "game_start"
    assert recorder.session_started is True
    riot_client.get_all_game_data_result.assert_awaited()
    riot_client.get_match_metadata.assert_awaited_once()


def test_lcu_game_start_waits_for_live_client_before_starting():
    tmp_path = runtime_dir("lcu_waits_for_live_client")
    config = config_for(tmp_path)
    riot_client = Mock()
    riot_client.get_gameflow_phase_result = AsyncMock(
        return_value=recordtest.RiotPollResult(
            recordtest.RiotPollStatus.IN_GAME,
            payload={"phase": "InProgress"},
        )
    )
    riot_client.get_all_game_data_result = AsyncMock(
        side_effect=[
            recordtest.RiotPollResult(
                recordtest.RiotPollStatus.TEMPORARY_FAILURE,
                error="Live Client API unavailable",
            ),
            recordtest.RiotPollResult(
                recordtest.RiotPollStatus.IN_GAME,
                payload={"gameData": {"gameTime": 3.5}, "allPlayers": []},
            ),
        ]
    )
    riot_client.get_active_player_name = AsyncMock(return_value="Tester#JP1")
    riot_client.get_champ_select_session_result = AsyncMock(
        return_value=recordtest.RiotPollResult(recordtest.RiotPollStatus.NOT_IN_GAME)
    )
    riot_client.get_match_metadata = AsyncMock(return_value={})

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=riot_client,
        auto_setup=False,
    )
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    assert run(recorder.wait_for_game_start_async()) is True
    assert recorder.game_start_detection_source == "live_client"
    assert recorder.sync_game_time == 0.0
    assert riot_client.get_all_game_data_result.await_count == 2
    recorder.wait_with_stop_async.assert_awaited_once_with(recordtest.DEFAULT_LCU_START_LIVE_CLIENT_POLL_SEC)


def test_wait_for_game_start_captures_ban_pick_order_and_champions():
    tmp_path = runtime_dir("ban_pick")
    config = config_for(tmp_path)
    riot_client = Mock()
    riot_client.get_champ_select_session_result = AsyncMock(
        return_value=recordtest.RiotPollResult(
            recordtest.RiotPollStatus.IN_GAME,
            payload={
                "gameId": 123,
                "localPlayerCellId": 0,
                "timer": {"phase": "FINALIZATION"},
                "myTeam": [{"cellId": 0, "championId": 103, "assignedPosition": "middle"}],
                "theirTeam": [{"cellId": 5, "championId": 266, "assignedPosition": "top"}],
                "actions": [
                    [
                        {
                            "id": 1,
                            "actorCellId": 5,
                            "championId": 122,
                            "completed": True,
                            "isAllyAction": False,
                            "pickTurn": 1,
                            "type": "ban",
                        }
                    ],
                    [
                        {
                            "id": 2,
                            "actorCellId": 0,
                            "championId": 103,
                            "completed": True,
                            "isAllyAction": True,
                            "pickTurn": 1,
                            "type": "pick",
                        }
                    ],
                ],
            },
        )
    )
    riot_client.get_champion_catalog = AsyncMock(
        return_value={103: "Ahri", 122: "Darius", 266: "Aatrox"}
    )
    riot_client.get_match_metadata = AsyncMock(
        return_value={
            "queue_id": 420,
            "queue_type": "RANKED_SOLO_5x5",
            "display_name": "ランク ソロ/デュオ",
            "source": "lcu",
        }
    )
    riot_client.get_all_game_data = AsyncMock(
        return_value={"gameData": {"gameTime": 12.0}, "allPlayers": []}
    )
    riot_client.get_active_player_name = AsyncMock(return_value="Tester#JP1")

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=riot_client,
        auto_setup=False,
    )

    assert run(recorder.wait_for_game_start_async()) is True

    ban_pick = recorder.build_session_payload()["ban_pick"]
    match = recorder.build_session_payload()["match"]
    assert [action["type"] for action in ban_pick["actions"]] == ["ban", "pick"]
    assert [action["champion_name"] for action in ban_pick["actions"]] == ["Darius", "Ahri"]
    assert ban_pick["teams"]["enemy"][0]["champion_name"] == "Aatrox"
    assert match["queue_id"] == 420
    assert match["display_name"] == "ランク ソロ/デュオ"


def test_wait_for_game_start_requires_previous_live_client_session_to_clear():
    tmp_path = runtime_dir("previous_game_clear")
    config = config_for(tmp_path)
    riot_client = Mock()
    riot_client.get_all_game_data_result = AsyncMock(
        side_effect=[
            recordtest.RiotPollResult(
                recordtest.RiotPollStatus.IN_GAME,
                payload={"gameData": {"gameTime": 1500.0}},
            ),
            recordtest.RiotPollResult(recordtest.RiotPollStatus.NOT_IN_GAME),
            recordtest.RiotPollResult(
                recordtest.RiotPollStatus.IN_GAME,
                payload={"gameData": {"gameTime": 1.0}, "allPlayers": []},
            ),
        ]
    )
    riot_client.get_all_game_data = AsyncMock(return_value=None)
    riot_client.get_active_player_name = AsyncMock(return_value="Tester#JP1")
    riot_client.get_champ_select_session_result = AsyncMock(
        return_value=recordtest.RiotPollResult(recordtest.RiotPollStatus.NOT_IN_GAME)
    )
    riot_client.get_match_metadata = AsyncMock(return_value={})

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=riot_client,
        auto_setup=False,
    )
    recorder._require_game_clear = True
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    assert run(recorder.wait_for_game_start_async()) is True
    assert riot_client.get_all_game_data_result.await_count == 3
    assert recorder._require_game_clear is False


def test_previous_game_clear_accepts_consecutive_live_client_connection_failures():
    tmp_path = runtime_dir("previous_game_process_closed")
    config = config_for(
        tmp_path,
        polling={
            "end_error_limit": 3,
            "end_missing_grace_sec": 60.0,
            "end_poll_sec": 0.1,
            "event_poll_sec": 0.1,
        },
    )
    riot_client = Mock()
    riot_client.get_all_game_data_result = AsyncMock(
        return_value=recordtest.RiotPollResult(recordtest.RiotPollStatus.TEMPORARY_FAILURE)
    )

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=riot_client,
        auto_setup=False,
    )
    recorder._require_game_clear = True
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    assert run(recorder.wait_for_previous_game_clear_async()) is True
    assert riot_client.get_all_game_data_result.await_count == 3
    assert recorder._require_game_clear is False


def test_start_recording_waits_until_obs_reports_active():
    tmp_path = runtime_dir("recording_start_confirmation")
    config = config_for(tmp_path)
    obs_client = FakeOBSClient()
    obs_client.is_recording_active.side_effect = [False, True]
    riot_client = Mock()
    riot_client.get_event_data = AsyncMock(
        return_value={"Events": [{"EventName": "GameStart", "EventTime": 0.0}]}
    )
    riot_client.get_all_game_data = AsyncMock(
        return_value={"gameData": {"gameTime": 1.0}, "allPlayers": []}
    )

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=obs_client,
        riot_api_client=riot_client,
        auto_setup=False,
    )
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    run(recorder.start_recording_async())

    assert recorder.recording_started is True
    obs_client.prepare_recording_start.assert_called_once()
    assert obs_client.is_recording_active.call_count == 2


def test_game_start_event_wait_uses_short_default_timeout():
    default = inspect.signature(recordtest.LoLAutoRecorder.wait_until_game_start_event_async).parameters[
        "timeout_sec"
    ].default

    assert default == recordtest.DEFAULT_GAME_START_EVENT_WAIT_SEC
    assert recordtest.DEFAULT_GAME_START_EVENT_WAIT_SEC <= 3.0


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

    outcome = run(recorder.record_until_end_async())
    recorder.stop_recording()

    assert outcome == recordtest.RecordingOutcome.COMPLETED
    assert recorder.game_result == "Win"
    assert recorder.winning_team == "ORDER"
    assert recorder.enemy_champions == ["Darius"]
    assert any(event.get("EventName") == "BuildingKill" for event in recorder.saved_events)
    building_event = next(event for event in recorder.all_events if event.get("EventName") == "BuildingKill")
    assert building_event["KillerTeam"] == "ORDER"
    assert building_event["team_relation"] == "own"
    obs_client.stop_recording.assert_called_once()
    recorder.wait_with_stop_async.assert_not_awaited()


def test_process_events_saves_player_assists():
    tmp_path = runtime_dir("player_assists")
    recorder = recordtest.LoLAutoRecorder(
        config=config_for(tmp_path),
        obs_client=FakeOBSClient(),
        riot_api_client=Mock(),
        auto_setup=False,
    )
    recorder.my_name = "Tester#JP1"
    recorder.my_name_short = "Tester"

    recorder.process_events(
        [
            {
                "EventID": 1,
                "EventName": "ChampionKill",
                "EventTime": 123.0,
                "KillerName": "Ally",
                "VictimName": "Enemy",
                "Assisters": ["Tester", "Support"],
            }
        ]
    )

    assert len(recorder.saved_events) == 1
    assert recorder.saved_events[0]["Assisters"] == ["Tester", "Support"]


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
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    with pytest.raises(recordtest.RecorderError, match="OBS録画開始に失敗"):
        run(recorder.start_recording_async())

    obs_client.prepare_recording_start.assert_called_once()
    obs_client.start_recording.assert_called_once()
    obs_client.setup_record_output.assert_not_called()
    assert recorder.recording_started is False
    assert recorder.record_path is None
    assert recorder.has_session_data() is False


def test_start_recording_recovers_with_toggle_and_x264_when_start_record_never_becomes_active():
    tmp_path = runtime_dir("recording_start_inactive")
    config = config_for(tmp_path)
    obs_client = FakeOBSClient()
    riot_client = Mock()
    riot_client.get_event_data = AsyncMock(
        return_value={"Events": [{"EventName": "GameStart", "EventTime": 0.0}]}
    )
    riot_client.get_all_game_data = AsyncMock(
        return_value={"gameData": {"gameTime": 2.0}, "allPlayers": []}
    )

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=obs_client,
        riot_api_client=riot_client,
        auto_setup=False,
    )
    recorder.wait_with_stop_async = AsyncMock(return_value=True)
    recorder.wait_for_recording_active_async = AsyncMock(side_effect=[False, True])

    run(recorder.start_recording_async())

    assert recorder.recording_started is True
    obs_client.start_recording.assert_called_once()
    obs_client.toggle_recording.assert_called_once()
    obs_client.set_recording_encoder.assert_called_once_with("x264")
    assert obs_client.prepare_recording_start.call_count == 2
    assert recorder.wait_for_recording_active_async.await_count == 2
    obs_client.setup_record_output.assert_not_called()


def test_start_recording_reports_recovery_failure_when_recording_stays_inactive():
    tmp_path = runtime_dir("recording_start_recovery_failure")
    config = config_for(tmp_path)
    obs_client = FakeOBSClient()

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=obs_client,
        riot_api_client=Mock(),
        auto_setup=False,
    )
    recorder.wait_with_stop_async = AsyncMock(return_value=True)
    recorder.wait_for_recording_active_async = AsyncMock(side_effect=[False, False])

    with pytest.raises(recordtest.RecorderError, match="復旧試行"):
        run(recorder.start_recording_async())

    obs_client.start_recording.assert_called_once()
    obs_client.toggle_recording.assert_called_once()
    obs_client.set_recording_encoder.assert_called_once_with("x264")


def test_start_recording_validates_sync_source_before_obs_recording():
    tmp_path = runtime_dir("recording_sync_source_missing")
    config = config_for(tmp_path)
    obs_client = FakeOBSClient()
    obs_client.get_sync_source_id.return_value = None

    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=obs_client,
        riot_api_client=Mock(),
        auto_setup=False,
    )
    recorder.session_started = True

    with pytest.raises(recordtest.RecorderError, match="同期用ソース"):
        run(recorder.start_recording_async())

    obs_client.start_recording.assert_not_called()
    assert recorder.recording_started is False
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

    outcome = run(recorder.record_until_end_async())

    assert outcome == recordtest.RecordingOutcome.COMPLETED
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

    outcome = run(recorder.record_until_end_async())

    assert outcome == recordtest.RecordingOutcome.COMPLETED
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

    outcome = run(recorder.record_until_end_async())

    assert outcome == recordtest.RecordingOutcome.COMPLETED
    assert riot_client.get_all_game_data_result.await_count == 3
    riot_client.get_event_data.assert_not_awaited()


def test_record_until_end_stops_after_persistent_temporary_failures():
    tmp_path = runtime_dir("temporary_failures_timeout")
    config = config_for(
        tmp_path,
        polling={
            "end_error_limit": 3,
            "end_missing_grace_sec": 60.0,
            "end_temporary_failure_grace_sec": 0.0,
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

    outcome = run(recorder.record_until_end_async())

    assert outcome == recordtest.RecordingOutcome.COMPLETED
    assert riot_client.get_all_game_data_result.await_count == 3
    riot_client.get_event_data.assert_not_awaited()


def test_record_until_end_returns_cancelled_when_stop_requested():
    tmp_path = runtime_dir("record_until_end_cancelled")
    config = config_for(tmp_path)
    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=Mock(),
        auto_setup=False,
    )
    recorder.request_stop()

    outcome = run(recorder.record_until_end_async())

    assert outcome == recordtest.RecordingOutcome.CANCELLED


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
    assert saved_payloads[0][1]["session_status"] == "completed"
    assert recorder.has_session_data() is False


def test_finalize_failed_partial_session_marks_json_status(monkeypatch):
    tmp_path = runtime_dir("failed_partial_status")
    config = config_for(tmp_path)
    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=Mock(),
        auto_setup=False,
    )
    recorder.output_file = tmp_path / "json" / "session.json"
    recorder.recording_started = True
    recorder.all_events = [{"EventID": 1, "EventName": "GameStart", "EventTime": 0.0}]
    saved_payloads = []
    monkeypatch.setattr(recordtest, "save_payload", lambda path, payload: saved_payloads.append((path, payload)))
    monkeypatch.setattr(recordtest, "enforce_storage_limit", lambda *args, **kwargs: None)

    recorder.finalize_session(
        outcome=recordtest.RecordingOutcome.FAILED_PARTIAL,
        failure_reason=RuntimeError("OBS disconnected"),
    )

    assert len(saved_payloads) == 1
    assert saved_payloads[0][1]["session_status"] == "failed_partial"
    assert saved_payloads[0][1]["session_phase"] == "failed"
    assert saved_payloads[0][1]["failure_reason"] == "OBS disconnected"


def test_finalize_aborted_session_marks_json_status(monkeypatch):
    tmp_path = runtime_dir("aborted_status")
    config = config_for(tmp_path)
    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=Mock(),
        auto_setup=False,
    )
    recorder.output_file = tmp_path / "json" / "session.json"
    recorder.recording_started = True
    saved_payloads = []
    monkeypatch.setattr(recordtest, "save_payload", lambda path, payload: saved_payloads.append((path, payload)))
    monkeypatch.setattr(recordtest, "enforce_storage_limit", lambda *args, **kwargs: None)

    result = recorder.finalize_session(
        outcome=recordtest.RecordingOutcome.ABORTED,
        failure_reason="user stopped recording",
    )

    assert result.success is True
    assert saved_payloads[0][1]["session_status"] == "aborted"
    assert saved_payloads[0][1]["session_phase"] == "aborted"
    assert saved_payloads[0][1]["failure_reason"] == "user stopped recording"


def test_finalize_writes_pending_session_when_atomic_save_fails(monkeypatch):
    tmp_path = runtime_dir("pending_after_save_failure")
    config = config_for(tmp_path)
    recorder = recordtest.LoLAutoRecorder(
        config=config,
        obs_client=FakeOBSClient(),
        riot_api_client=Mock(),
        auto_setup=False,
    )
    recorder.output_file = tmp_path / "json" / "session.json"
    recorder.recording_started = True
    recorder.all_events = [{"EventID": 1, "EventName": "GameStart", "EventTime": 0.0}]
    monkeypatch.setattr(recordtest, "save_payload", Mock(side_effect=OSError("disk full")))
    monkeypatch.setattr(recordtest, "enforce_storage_limit", lambda *args, **kwargs: None)

    result = recorder.finalize_session(outcome=recordtest.RecordingOutcome.COMPLETED)

    assert result.success is False
    assert "disk full" in result.error
    assert result.pending_path
    assert Path(result.pending_path).exists()
    assert "finalize_error" in Path(result.pending_path).read_text(encoding="utf-8")
