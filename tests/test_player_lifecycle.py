from types import MethodType, SimpleNamespace
from unittest.mock import Mock

from src import player as player_module
from src.app import MainWindow, PlayerPage
from src.player import ClipExportWorker, PlayerWidget, build_ban_pick_view_model


class FakeRunningWorker:
    def __init__(self, *, wait_result: bool) -> None:
        self.wait_result = wait_result
        self.cancel_called = 0
        self.wait_calls = []

    def isRunning(self) -> bool:
        return True

    def cancel(self) -> None:
        self.cancel_called += 1

    def wait(self, timeout_ms: int) -> bool:
        self.wait_calls.append(timeout_ms)
        return self.wait_result


class FakeMpvPlayer:
    def __init__(self) -> None:
        self.calls = []

    def unobserve_property(self, name, handler) -> None:
        self.calls.append(("unobserve", name, handler))

    def command(self, name) -> None:
        self.calls.append(("command", name))

    def terminate(self) -> None:
        self.calls.append(("terminate",))


class FakeButton:
    def __init__(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def show(self) -> None:
        self.visible = True


class FakeWindow:
    def __init__(self) -> None:
        self.calls = []

    def setStyleSheet(self, value: str) -> None:
        self.calls.append(("stylesheet", value))

    def showFullScreen(self) -> None:
        self.calls.append(("fullscreen",))

    def showNormal(self) -> None:
        self.calls.append(("normal",))

    def setWindowFlag(self, *args) -> None:
        raise AssertionError("fullscreen transitions must not recreate the native window")


class FakeProgress:
    def __init__(self) -> None:
        self.value = None
        self.text = None

    def setValue(self, value: int) -> None:
        self.value = value

    def setFormat(self, text: str) -> None:
        self.text = text


class FakeLabel:
    def __init__(self) -> None:
        self.text = None

    def setText(self, text: str) -> None:
        self.text = text


def test_player_widget_does_not_initialize_mpv_until_replay_is_loaded(qtbot, monkeypatch):
    calls = []
    monkeypatch.setattr(PlayerWidget, "init_mpv", lambda self: calls.append("init") or True)

    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)

    assert calls == []


def test_build_ban_pick_view_model_formats_teams_and_action_order():
    model = build_ban_pick_view_model(
        {
            "actions": [
                {
                    "order": 2,
                    "type": "pick",
                    "team": "ally",
                    "champion_id": 103,
                    "champion_name": "Ahri",
                    "assigned_position": "middle",
                },
                {
                    "order": 1,
                    "type": "ban",
                    "team": "enemy",
                    "champion_id": 122,
                    "champion_name": "Darius",
                },
            ],
            "teams": {
                "ally": [
                    {
                        "cell_id": 0,
                        "champion_name": "Ahri",
                        "assigned_position": "middle",
                    }
                ],
                "enemy": [
                    {
                        "cell_id": 5,
                        "champion_name": "Aatrox",
                        "assigned_position": "top",
                    }
                ],
            },
        }
    )

    assert model["has_data"] is True
    assert model["ally_lines"] == ["MID  Ahri"]
    assert model["enemy_lines"] == ["TOP  Aatrox"]
    assert [action["text"] for action in model["actions"]] == [
        "01. 敵 BAN: Darius",
        "02. 味方 PICK: Ahri · MID",
    ]


def test_build_ban_pick_view_model_uses_pick_actions_when_team_snapshot_is_missing():
    model = build_ban_pick_view_model(
        {
            "actions": [
                {
                    "order": 1,
                    "type": "pick",
                    "team": "ally",
                    "champion_id": 64,
                    "assigned_position": "jungle",
                }
            ]
        }
    )

    assert model["ally_lines"] == ["JUNGLE  Champion #64"]
    assert model["enemy_lines"] == []


def test_player_widget_displays_ban_pick_tab(qtbot):
    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)
    widget.ban_pick = {
        "actions": [
            {
                "order": 1,
                "type": "ban",
                "team": "enemy",
                "champion_name": "Darius",
            }
        ],
        "teams": {
            "ally": [{"champion_name": "Ahri", "assigned_position": "middle"}],
            "enemy": [{"champion_name": "Aatrox", "assigned_position": "top"}],
        },
    }

    widget.populate_ban_pick()

    assert widget.side_tabs.tabText(1) == "Ban/Pick"
    assert widget.ban_pick_ally_label.text() == "MID  Ahri"
    assert widget.ban_pick_enemy_label.text() == "TOP  Aatrox"
    assert widget.ban_pick_list.count() == 1
    assert widget.ban_pick_list.item(0).text() == "01. 敵 BAN: Darius"


def test_cancel_sync_worker_retains_reference_after_timeout():
    worker = FakeRunningWorker(wait_result=False)
    widget = SimpleNamespace(worker=worker, _sync_generation=0)

    stopped = PlayerWidget.cancel_sync_worker(widget, timeout_ms=25)

    assert stopped is False
    assert widget.worker is worker
    assert worker.cancel_called == 1
    assert worker.wait_calls == [25]


def test_start_sync_worker_does_not_replace_running_worker_after_timeout():
    worker = FakeRunningWorker(wait_result=False)
    widget = SimpleNamespace(worker=worker, _sync_generation=0, current_video_path="replay.mp4")
    widget.cancel_sync_worker = MethodType(PlayerWidget.cancel_sync_worker, widget)

    started = PlayerWidget.start_sync_worker(widget)

    assert started is False
    assert widget.worker is worker


def test_stale_sync_worker_completion_releases_retained_reference():
    worker = object()
    widget = SimpleNamespace(worker=worker, _sync_generation=2)

    PlayerWidget.on_sync_finished(widget, worker, generation=1, found_time=-1.0)

    assert widget.worker is None


def test_shutdown_player_unobserves_properties_before_terminating_mpv():
    player = FakeMpvPlayer()
    calls = []
    widget = SimpleNamespace(player=player, _player_shutting_down=False)
    widget.on_mpv_time_update = lambda *args: None
    widget.on_mpv_duration_update = lambda *args: None
    widget.cancel_background_tasks = lambda timeout_ms: calls.append(("background", timeout_ms)) or True
    widget._unobserve_mpv_properties = MethodType(PlayerWidget._unobserve_mpv_properties, widget)
    terminate = MethodType(PlayerWidget._terminate_mpv_player, widget)
    widget._terminate_mpv_player = lambda mpv_player: (calls.append(("player",)), terminate(mpv_player))[1]

    stopped = PlayerWidget.shutdown_player(widget)

    assert stopped is True
    assert widget.player is None
    assert widget._player_shutting_down is True
    assert player.calls == [
        ("unobserve", "time-pos", widget.on_mpv_time_update),
        ("unobserve", "duration", widget.on_mpv_duration_update),
        ("command", "stop"),
        ("terminate",),
    ]
    assert calls == [("player",), ("background", 3000)]


def test_fullscreen_transition_does_not_change_window_flags():
    window = FakeWindow()
    page = SimpleNamespace(
        back_btn=FakeButton(),
        open_btn=FakeButton(),
        window=lambda: window,
    )

    PlayerPage.handle_fullscreen(page, True)
    PlayerPage.handle_fullscreen(page, False)

    assert window.calls == [
        ("stylesheet", "background-color: black;"),
        ("fullscreen",),
        ("stylesheet", ""),
        ("normal",),
    ]
    assert page.back_btn.visible is True
    assert page.open_btn.visible is True


def test_player_page_leave_exits_fullscreen_and_shuts_down_player():
    calls = []
    player_widget = SimpleNamespace(
        is_fullscreen_mode=True,
        toggle_fullscreen=lambda: calls.append(("fullscreen",)),
        shutdown_player=lambda timeout_ms: calls.append(("shutdown", timeout_ms)) or False,
    )
    page = SimpleNamespace(player_widget=player_widget)

    stopped = PlayerPage.on_leave(page, timeout_ms=123)

    assert stopped is False
    assert calls == [("fullscreen",), ("shutdown", 123)]


def test_close_to_tray_stops_player_before_hiding_window():
    calls = []
    event = SimpleNamespace(ignore=lambda: calls.append(("ignore",)))
    window = SimpleNamespace(
        _is_quitting=False,
        _tray_icon=None,
        _tray_notice_shown=False,
        should_minimize_to_tray=lambda: True,
        _stop_player=lambda timeout_ms: calls.append(("stop_player", timeout_ms)) or True,
        hide=lambda: calls.append(("hide",)),
    )

    MainWindow.closeEvent(window, event)

    assert calls == [("stop_player", 1000), ("ignore",), ("hide",)]


def test_clip_export_preserves_source_frame_timing_and_uses_quality_encoder_settings():
    worker = ClipExportWorker(
        "ffmpeg.exe",
        "input.mp4",
        "output.mp4",
        start_sec=10.0,
        end_sec=20.0,
    )
    encoder_args = dict(ClipExportWorker.ENCODER_PROFILES)["h264_nvenc"]

    command = worker._build_ffmpeg_command(encoder_args)

    assert "-vf" not in command
    assert "-r" not in command
    assert "-fps_mode" not in command
    assert command[command.index("-preset") + 1] == "p5"
    assert command[command.index("-cq") + 1] == "19"


def test_export_clip_starts_lazy_ffmpeg_setup_when_binary_is_missing(monkeypatch):
    widget = SimpleNamespace(
        ffmpeg_setup_worker=None,
        clip_worker=None,
        current_video_path="replay.mp4",
        clip_start=1.0,
        clip_end=2.0,
        start_ffmpeg_setup=Mock(),
    )
    monkeypatch.setattr(player_module, "find_ffmpeg_executable", lambda: None)

    PlayerWidget.export_clip(widget)

    widget.start_ffmpeg_setup.assert_called_once_with()


def test_ffmpeg_setup_completion_resumes_clip_export():
    progress = FakeProgress()
    label = FakeLabel()
    widget = SimpleNamespace(
        _pending_clip_export=True,
        clip_progress=progress,
        info_label=label,
        start_clip_export=Mock(),
    )

    PlayerWidget.on_ffmpeg_setup_installed(widget, "bin/ffmpeg.exe")

    assert widget._pending_clip_export is False
    assert progress.value == 100
    assert progress.text == "FFmpegの準備完了"
    assert label.text == "FFmpegの準備が完了しました。"
    widget.start_clip_export.assert_called_once_with("bin/ffmpeg.exe")


def test_cancel_background_tasks_requests_ffmpeg_setup_stop():
    ffmpeg_worker = FakeRunningWorker(wait_result=True)
    widget = SimpleNamespace(
        _pending_clip_export=True,
        ffmpeg_setup_worker=ffmpeg_worker,
        clip_worker=None,
        cancel_sync_worker=lambda timeout_ms: True,
    )

    stopped = PlayerWidget.cancel_background_tasks(widget, timeout_ms=50)

    assert stopped is True
    assert widget._pending_clip_export is False
    assert widget.ffmpeg_setup_worker is None
    assert ffmpeg_worker.cancel_called == 1
    assert ffmpeg_worker.wait_calls == [50]
