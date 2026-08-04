from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QListWidgetItem

from src import player as player_module
from src.player import (
    PlayerWidget,
    calculate_event_seek_position,
    calculate_sync_offset,
)
from src.session_log import load_session_payload, save_session_payload


@pytest.mark.parametrize(
    ("event_time", "sync_offset", "duration", "expected"),
    [
        (10, 0, None, 5.0),
        (4.5, 0, None, 0.0),
        (-30, 2, None, 0.0),
        (30, 2.5, None, 27.5),
        (120, 0, 90, 90.0),
        (120, 0, 0, 115.0),
        (120, 0, float("nan"), 115.0),
        (120, 0, float("inf"), 115.0),
    ],
)
def test_calculate_event_seek_position_applies_preroll_and_bounds(
    event_time,
    sync_offset,
    duration,
    expected,
):
    assert calculate_event_seek_position(event_time, sync_offset, duration) == expected


@pytest.mark.parametrize("event_time", [None, "10", True, float("nan"), float("inf"), float("-inf"), object()])
def test_calculate_event_seek_position_rejects_invalid_event_times(event_time):
    assert calculate_event_seek_position(event_time, 0, 100) is None


@pytest.mark.parametrize("sync_offset", [None, "2", True, float("nan"), float("inf"), object()])
def test_calculate_event_seek_position_rejects_invalid_sync_offsets(sync_offset):
    assert calculate_event_seek_position(30, sync_offset, 100) is None


def test_calculate_event_seek_position_rejects_finite_addition_overflow():
    largest_float = float.fromhex("0x1.fffffffffffffp+1023")

    assert calculate_event_seek_position(largest_float, largest_float, 100) is None


def test_saved_and_reloaded_sync_time_produces_the_same_seek_position(tmp_path):
    session_path = tmp_path / "session.json"
    payload = {
        "schema_version": 1,
        "sync_game_time": 12.5,
        "events_all": [{"EventName": "DragonKill", "EventTime": 40.0}],
    }
    original_offset = calculate_sync_offset(18.0, payload["sync_game_time"])
    save_session_payload(session_path, payload)

    reloaded = load_session_payload(session_path)
    reloaded_offset = calculate_sync_offset(18.0, reloaded["sync_game_time"])

    assert original_offset == reloaded_offset == 5.5
    assert calculate_event_seek_position(40.0, original_offset, 120.0) == calculate_event_seek_position(
        reloaded["events_all"][0]["EventTime"],
        reloaded_offset,
        120.0,
    )


def test_calculate_sync_offset_rejects_non_finite_or_non_numeric_values():
    assert calculate_sync_offset(18.0, 12.5) == 5.5
    assert calculate_sync_offset(float("inf"), 12.5) is None
    assert calculate_sync_offset(18.0, float("nan")) is None
    assert calculate_sync_offset(18.0, "12.5") is None
    assert calculate_sync_offset(True, 12.5) is None


def test_event_list_omits_invalid_event_times(qtbot):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)
    widget.my_name = "Tester"
    widget.events_all = [
        {"EventName": "DragonKill", "EventTime": 30},
        {"EventName": "DragonKill"},
        {"EventName": "DragonKill", "EventTime": "40"},
        {"EventName": "DragonKill", "EventTime": True},
        {"EventName": "DragonKill", "EventTime": float("nan")},
        {"EventName": "DragonKill", "EventTime": float("inf")},
        {"EventName": "DragonKill", "EventTime": float("-inf")},
    ]

    widget.populate_event_list()

    assert widget.event_list.count() == 2
    assert widget.event_list.item(0).data(Qt.ItemDataRole.UserRole) == 0.0
    assert widget.event_list.item(1).data(Qt.ItemDataRole.UserRole) == 30.0


def test_event_click_seeks_with_absolute_exact_and_duration_clamp(qtbot):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)
    seek = Mock()
    widget.player = SimpleNamespace(seek=seek, pause=True)
    widget.offset = 4.0
    widget.duration = 60.0
    item = QListWidgetItem()
    item.setData(Qt.ItemDataRole.UserRole, 80.0)

    widget.on_event_clicked(item)

    seek.assert_called_once_with(60.0, reference="absolute", precision="exact")
    assert widget.player.pause is False
    assert widget.play_btn.text() == "Pause"
    widget.player = None


@pytest.mark.parametrize("item_data", [None, "30", True, float("nan"), float("inf"), object()])
def test_event_click_ignores_invalid_item_data(qtbot, item_data):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)
    seek = Mock()
    widget.player = SimpleNamespace(seek=seek, pause=True)
    widget.offset = 0.0
    widget.duration = 100.0
    item = QListWidgetItem()
    item.setData(Qt.ItemDataRole.UserRole, item_data)

    widget.on_event_clicked(item)

    seek.assert_not_called()
    assert widget.player.pause is True
    widget.player = None


def test_event_click_safely_ignores_missing_player_item_or_offset(qtbot):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)
    item = QListWidgetItem()
    item.setData(Qt.ItemDataRole.UserRole, 30.0)

    widget.on_event_clicked(item)

    seek = Mock()
    widget.player = SimpleNamespace(seek=seek, pause=True)
    widget.on_event_clicked(None)
    widget.offset = None
    widget.on_event_clicked(item)

    seek.assert_not_called()
    widget.player = None


def test_loading_new_replay_clears_previous_duration_before_event_seek(qtbot, monkeypatch, tmp_path):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)
    player = SimpleNamespace(play=Mock(), seek=Mock(), pause=False)
    widget.player = player
    widget.duration = 40.0
    video_path = tmp_path / "next.mp4"
    monkeypatch.setattr(
        player_module,
        "load_session_payload",
        lambda _path: {
            "sync_game_time": 0.0,
            "events": [],
            "events_all": [],
            "ban_pick": {},
            "summoner_name": "Tester",
        },
    )
    monkeypatch.setattr(player_module, "resolve_video_path", lambda *_args: video_path)
    monkeypatch.setattr(widget, "cancel_sync_worker", lambda timeout_ms=1000: True)
    monkeypatch.setattr(widget, "init_mpv", lambda: True)
    monkeypatch.setattr(widget, "update_video_fps", lambda: None)
    monkeypatch.setattr(widget, "start_sync_worker", lambda: True)

    assert widget.load_data(tmp_path / "next.json") is True
    assert widget.duration == 0.0

    widget.offset = 0.0
    item = QListWidgetItem()
    item.setData(Qt.ItemDataRole.UserRole, 100.0)
    widget.on_event_clicked(item)

    player.seek.assert_called_once_with(95.0, reference="absolute", precision="exact")
    widget.player = None


@pytest.mark.parametrize("duration", [None, 0, -1, float("nan"), float("inf"), float("-inf"), True, "60"])
def test_duration_updates_fail_closed_to_unknown(qtbot, duration):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)
    widget.duration = 90.0

    widget.on_mpv_duration_update("duration", duration)

    assert widget.duration == 0.0


def test_duration_update_stores_only_positive_finite_numeric_values(qtbot):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)

    widget.on_mpv_duration_update("duration", 90)

    assert widget.duration == 90.0


@pytest.mark.parametrize("time_pos", [float("nan"), float("inf"), float("-inf"), True, "10"])
def test_time_updates_ignore_non_finite_or_non_numeric_values(qtbot, time_pos):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)
    initial_text = widget.time_label.text()
    initial_slider = widget.slider.value()

    widget.on_mpv_time_update("time-pos", time_pos)
    widget.apply_time_update(time_pos)

    assert widget.time_label.text() == initial_text
    assert widget.slider.value() == initial_slider


def _prepare_active_sync_widget(widget, *, sync_game_time):
    worker = object()
    widget.worker = worker
    widget._sync_generation = 7
    widget.sync_game_time = sync_game_time
    widget.event_list.setEnabled(False)
    widget.player = SimpleNamespace(pause=True)
    return worker


@pytest.mark.parametrize(
    ("found_time", "sync_game_time", "expected_offset"),
    [
        (18.0, 0.0, 18.0),
        (18.0, 12.5, 5.5),
    ],
)
def test_active_sync_completion_applies_finite_offset_and_resumes_playback(
    qtbot,
    found_time,
    sync_game_time,
    expected_offset,
):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)
    worker = _prepare_active_sync_widget(widget, sync_game_time=sync_game_time)

    widget.on_sync_finished(worker, generation=7, found_time=found_time)

    assert widget.worker is None
    assert widget.offset == expected_offset
    assert widget.info_label.text() == f"✅ Synced\nOffset: {expected_offset:.2f}s"
    assert widget.offset_label.text() == f"Offset: {expected_offset:+.2f}s"
    assert widget.event_list.isEnabled() is True
    assert widget.player.pause is False
    assert widget.play_btn.text() == "Pause"
    widget.player = None


@pytest.mark.parametrize("found_time", [-1.0, float("nan"), float("inf"), float("-inf"), True])
def test_active_sync_completion_fails_closed_for_invalid_found_time(qtbot, found_time):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)
    worker = _prepare_active_sync_widget(widget, sync_game_time=12.5)

    widget.on_sync_finished(worker, generation=7, found_time=found_time)

    assert widget.worker is None
    assert widget.offset == 0
    assert widget.info_label.text() == "⚠️ No Marker Found\nOffset: 0s"
    assert widget.offset_label.text() == "Offset: +0.00s"
    assert widget.event_list.isEnabled() is True
    assert widget.player.pause is False
    assert widget.play_btn.text() == "Pause"
    widget.player = None


@pytest.mark.parametrize("sync_game_time", [None, "12.5", True, float("nan"), float("inf"), float("-inf")])
def test_active_sync_completion_fails_closed_for_invalid_sync_game_time(qtbot, sync_game_time):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)
    worker = _prepare_active_sync_widget(widget, sync_game_time=sync_game_time)

    widget.on_sync_finished(worker, generation=7, found_time=18.0)

    assert widget.worker is None
    assert widget.offset == 0
    assert widget.info_label.text() == "⚠️ No Marker Found\nOffset: 0s"
    assert widget.offset_label.text() == "Offset: +0.00s"
    assert widget.event_list.isEnabled() is True
    assert widget.player.pause is False
    assert widget.play_btn.text() == "Pause"
    widget.player = None
