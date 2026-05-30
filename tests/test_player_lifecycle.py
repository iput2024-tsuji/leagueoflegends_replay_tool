from types import MethodType, SimpleNamespace

from src.app import PlayerPage
from src.player import PlayerWidget


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


def test_player_widget_does_not_initialize_mpv_until_replay_is_loaded(qtbot, monkeypatch):
    calls = []
    monkeypatch.setattr(PlayerWidget, "init_mpv", lambda self: calls.append("init") or True)

    widget = PlayerWidget(auto_open=False)
    qtbot.addWidget(widget)

    assert calls == []


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
    widget = SimpleNamespace(player=player, _player_shutting_down=False)
    widget.on_mpv_time_update = lambda *args: None
    widget.on_mpv_duration_update = lambda *args: None
    widget.cancel_background_tasks = lambda timeout_ms: True
    widget._unobserve_mpv_properties = MethodType(PlayerWidget._unobserve_mpv_properties, widget)
    widget._terminate_mpv_player = MethodType(PlayerWidget._terminate_mpv_player, widget)

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
