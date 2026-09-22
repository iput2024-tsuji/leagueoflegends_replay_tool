import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src import recordtest
from src.session_log import SessionLogV1


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    clock = SimpleNamespace(monotonic=Mock(return_value=100.0), strftime=time.strftime)
    monkeypatch.setattr(recordtest, "time", clock)
    config = recordtest.AppConfig.from_dict({
        "paths": {"recordings_dir": str(tmp_path), "json_dir": str(tmp_path / "json")},
    })
    obs_client = Mock(obs_process=None)
    obs_client.get_recording_clock.return_value = None
    recorder = recordtest.LoLAutoRecorder(
        config=config, obs_client=obs_client, riot_api_client=Mock(), auto_setup=False,
    )
    return recorder


def live_result(game_time):
    return recordtest.RiotPollResult(
        recordtest.RiotPollStatus.IN_GAME, payload={"gameData": {"gameTime": game_time}},
    )


def observe(recorder, game_time, video_time, *, latency=0.1):
    recorder.obs_client.get_recording_clock.return_value = video_time
    recorder._observe_recording_sync(live_result(game_time), 100.0 - latency)


def test_pause_and_api_gap_save_only_observed_intervals(recorder):
    for game_time, video_time in [(10, 5), (11, 6), (11, 7), (11, 8), (12, 9)]:
        observe(recorder, game_time, video_time)
    recorder._observe_recording_sync(
        recordtest.RiotPollResult(recordtest.RiotPollStatus.TEMPORARY_FAILURE), 99.9,
    )
    for game_time, video_time in [(20, 100), (21, 101)]:
        observe(recorder, game_time, video_time)

    intervals = [
        {"game_start": 10.0, "game_end": 11.0, "video_start": 5.0, "video_end": 6.0},
        {"game_start": 11.0, "game_end": 12.0, "video_start": 8.0, "video_end": 9.0},
        {"game_start": 20.0, "game_end": 21.0, "video_start": 100.0, "video_end": 101.0},
    ]
    assert recorder.sync_intervals == intervals
    assert SessionLogV1.from_payload(recorder.build_session_payload()).to_payload()["sync_intervals"] == intervals
    assert recorder.session_outcome == recordtest.RecordingOutcome.COMPLETED
    assert all(call[0] == "get_recording_clock" for call in recorder.obs_client.mock_calls)


@pytest.mark.parametrize(
    ("game_time", "video_time", "latency"),
    [(12, 7, 0.6), (12, None, 0.1), (12, True, 0.1), (12, float("nan"), 0.1),
     (float("inf"), 7, 0.1), (True, 7, 0.1), (-1, 7, 0.1)],
)
def test_invalid_or_delayed_sample_breaks_continuity_without_losing_prior_intervals(
    recorder, game_time, video_time, latency,
):
    observe(recorder, 10, 5)
    observe(recorder, 11, 6)
    observe(recorder, game_time, video_time, latency=latency)
    observe(recorder, 13, 8)
    observe(recorder, 14, 9)

    assert [(item["game_start"], item["game_end"]) for item in recorder.sync_intervals] == [(10, 11), (13, 14)]
    assert recorder.failure_reason is None


def test_clock_failure_breaks_continuity_without_changing_recording_outcome(recorder):
    observe(recorder, 10, 5)
    recorder.obs_client.get_recording_clock.side_effect = TimeoutError("unavailable")
    recorder._observe_recording_sync(live_result(11), 99.9)
    recorder.obs_client.get_recording_clock.side_effect = None
    observe(recorder, 12, 7)
    observe(recorder, 13, 8)

    assert len(recorder.sync_intervals) == 1
    assert recorder.sync_intervals[0]["game_start"] == 12
    assert recorder.session_outcome == recordtest.RecordingOutcome.COMPLETED
    recorder.obs_client.start_recording.assert_not_called()
    recorder.obs_client.stop_recording.assert_not_called()


@pytest.mark.parametrize("rolled_back", [(9, 7), (12, 4)])
def test_clock_rollback_across_api_gap_disables_mapping_until_next_session(recorder, rolled_back):
    observe(recorder, 10, 5)
    observe(recorder, 11, 6)
    recorder._observe_recording_sync(
        recordtest.RiotPollResult(recordtest.RiotPollStatus.TEMPORARY_FAILURE), 99.9,
    )
    observe(recorder, *rolled_back)
    observe(recorder, 20, 20)
    observe(recorder, 21, 21)

    assert recorder.sync_intervals == []
    assert recorder.build_session_payload()["sync_intervals"] == []
    assert recorder._sync_sampling_disabled is True
    recorder.reset_session()
    observe(recorder, 1, 1)
    observe(recorder, 2, 2)
    assert len(recorder.sync_intervals) == 1


def test_recording_loop_observes_clock_before_slower_context_and_keeps_normal_end(recorder):
    calls = []
    results = iter([live_result(10), live_result(11)])

    async def poll():
        calls.append("live")
        return next(results)

    def clock():
        calls.append("clock")
        return 5.0 if calls.count("clock") == 1 else 6.0

    async def context(*_args):
        calls.append("context")
        return recordtest.RecordingEndDecision(False, recordtest.RecordingEndReason.STILL_ACTIVE)

    recorder.poll_all_game_data = poll
    recorder.obs_client.get_recording_clock.side_effect = clock
    recorder._observe_recording_context_end_async = context
    recorder.riot_api_client.get_active_player_name = AsyncMock(return_value="Tester")
    recorder.riot_api_client.get_event_data = AsyncMock(side_effect=[
        {"Events": []}, {"Events": [{"EventName": "GameEnd", "EventTime": 11.0}]},
    ])
    recorder.ensure_post_game_result_async = AsyncMock()
    recorder.wait_with_stop_async = AsyncMock(return_value=True)

    outcome = asyncio.run(recorder.record_until_end_async())

    assert outcome == recordtest.RecordingOutcome.COMPLETED
    assert calls == ["live", "clock", "context", "live", "clock", "context"]
    assert recorder.build_session_payload()["sync_intervals"] == [
        {"game_start": 10.0, "game_end": 11.0, "video_start": 5.0, "video_end": 6.0},
    ]
