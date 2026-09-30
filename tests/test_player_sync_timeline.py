from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import Qt

from src import player as player_module
from src.player import PlayerWidget
from src.session_log import load_session_payload, save_session_payload


@pytest.fixture
def replay_widget(qtbot, monkeypatch):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)
    widget.player = SimpleNamespace(play=Mock(), seek=Mock(), pause=True, time_pos=0.0)
    monkeypatch.setattr(widget, "cancel_sync_worker", lambda **_kwargs: True)
    monkeypatch.setattr(widget, "init_mpv", lambda: True)
    monkeypatch.setattr(widget, "update_video_fps", lambda: None)
    worker = SimpleNamespace(progress=Mock(), finished=Mock(), start=Mock())
    worker_factory = Mock(return_value=worker)
    monkeypatch.setattr(player_module, "SyncWorker", worker_factory)
    yield widget, worker_factory
    widget.worker = None
    widget.player = None


@pytest.fixture
def replay_logs(tmp_path):
    video = tmp_path / "recording.mkv"
    video.touch()
    payload = {
        "schema_version": 1,
        "obs_record_path": video.name,
        "sync_game_time": 12.5,
        "summoner_name": "Tester",
        "events_all": [
            {"EventName": "ChampionKill", "EventTime": event_time, "KillerName": "Tester", "VictimName": "Opponent"}
            for event_time in (29.5, 50.0, 75.78820037841797)
        ],
    }
    legacy_path = tmp_path / "legacy.json"
    save_session_payload(legacy_path, payload)
    payload["sync_intervals"] = [
        {"game_start": 29.0, "game_end": 30.0, "video_start": 24.0, "video_end": 25.0},
        {"game_start": 75.0, "game_end": 76.0, "video_start": 184.0, "video_end": 185.0},
    ]
    timed_path = tmp_path / "timed.json"
    save_session_payload(timed_path, payload)
    return timed_path, legacy_path


def test_saved_timeline_loads_and_seeks_before_and_after_pause(replay_widget, replay_logs):
    widget, worker_factory = replay_widget
    timed_path, _legacy_path = replay_logs

    assert widget.load_data(timed_path) is True
    assert widget.sync_intervals == load_session_payload(timed_path)["sync_intervals"]
    widget.player.play.assert_called_once_with(str(timed_path.parent / "recording.mkv"))
    worker_factory.assert_not_called()
    assert widget.event_list.isEnabled() is True
    assert widget.event_list.count() == 4

    widget.on_event_clicked(widget.event_list.item(1))
    widget.player.seek.assert_called_once_with(19.5, reference="absolute", precision="exact")
    widget.player.seek.reset_mock()
    widget.on_event_clicked(widget.event_list.item(3))
    widget.player.seek.assert_called_once_with(
        pytest.approx(179.78820037841797), reference="absolute", precision="exact",
    )


def test_loading_legacy_clears_timeline_and_manual_corrections_then_uses_marker(replay_widget, replay_logs):
    widget, worker_factory = replay_widget
    timed_path, legacy_path = replay_logs
    assert widget.load_data(timed_path) is True

    widget.event_list.setCurrentItem(widget.event_list.item(3))
    widget.player.time_pos = 186.78820037841797
    widget.sync_to_current_position()
    assert widget.offset == pytest.approx(2.0)
    widget.event_list.setCurrentItem(widget.event_list.item(2))
    widget.player.time_pos = 160.0
    widget.sync_to_current_position()
    assert widget.manual_event_times == {50.0: pytest.approx(158.0)}
    widget.on_event_clicked(widget.event_list.item(2))
    assert widget.player.seek.call_args.args[0] == pytest.approx(155.0)

    assert "sync_intervals" not in load_session_payload(legacy_path)
    assert widget.load_data(legacy_path) is True
    assert widget.sync_intervals is None
    assert widget.manual_event_times == {}
    assert widget.offset is None
    assert widget.event_list.isEnabled() is False
    assert "Game Start" in widget.event_list.item(0).text()
    worker_factory.assert_called_once_with(widget.current_video_path, max_seconds=180)
    worker = worker_factory.return_value
    worker.start.assert_called_once_with()

    widget.on_sync_finished(worker, widget._sync_generation, 18.0)
    assert widget.offset == 5.5
    assert widget.event_list.isEnabled() is True
    old_event = widget.event_list.item(2)
    assert old_event.data(Qt.ItemDataRole.UserRole) == 50.0
    widget.player.seek.reset_mock()
    widget.on_event_clicked(old_event)
    widget.player.seek.assert_called_once_with(50.5, reference="absolute", precision="exact")
