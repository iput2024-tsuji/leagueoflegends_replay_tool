from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QObject, Qt, pyqtSignal

from src import app
from src.app import SettingsPage
from src.recording_supervisor import RecordingSupervisor


class FakeSettingsPage:
    def __init__(self) -> None:
        self._audio_auto_refreshed_once = False
        self.calls = []

    def refresh_audio_devices(self, *, show_message=True, show_error=True, auto_launch=True):
        self.calls.append(
            {
                "show_message": show_message,
                "show_error": show_error,
                "auto_launch": auto_launch,
            }
        )
        return True

    def apply_audio_settings_to_obs(self, *, show_success=True, show_error=True, auto_launch=True):
        self.calls.append(
            {
                "show_success": show_success,
                "show_error": show_error,
                "auto_launch": auto_launch,
            }
        )
        return True


def test_settings_page_initial_audio_refresh_auto_launches_managed_obs():
    page = FakeSettingsPage()
    SettingsPage.on_page_shown(page)

    assert page._audio_auto_refreshed_once is True
    assert page.calls == [{"show_message": False, "show_error": False, "auto_launch": True}]


def test_settings_page_audio_auto_apply_auto_launches_managed_obs():
    page = FakeSettingsPage()

    SettingsPage._apply_audio_settings_auto(page)

    assert page.calls == [{"show_success": False, "show_error": False, "auto_launch": True}]


def test_successful_quick_setup_notifies_main_window(monkeypatch):
    emitted = []
    page = SimpleNamespace(
        load_settings=lambda: None,
        refresh_audio_devices=lambda **_kwargs: None,
        setup_completed=SimpleNamespace(emit=lambda: emitted.append(True)),
    )
    monkeypatch.setattr(app.QMessageBox, "information", lambda *_args, **_kwargs: None)

    SettingsPage._on_quick_setup_finished(
        page,
        {"errors": [], "warnings": []},
        {
            "scene_name": "LoL Replay",
            "window_capture_name": "League of Legends",
            "source_name": "Replay Sync",
            "source_color": 0xFF0000,
            "obs_launched": False,
        },
    )

    assert emitted == [True]


def test_settings_page_supports_fractional_high_fps(qtbot, monkeypatch):
    config = {
        "obs": {
            "password": "secret",
            "fps_numerator": 240000,
            "fps_denominator": 1001,
        },
        "paths": {},
        "storage": {},
        "polling": {},
        "audio": {},
        "app": {},
        "notifications": {
            "enabled": True,
            "recording_started": True,
            "recording_completed": False,
            "recording_failed": True,
            "minimized_to_tray": False,
        },
    }
    monkeypatch.setattr(app, "load_config", lambda: config)

    page = SettingsPage(lambda: None)
    qtbot.addWidget(page)

    assert page.obs_fps_numerator.maximum() > 120
    assert page.obs_fps_numerator.value() == 240000
    assert page.obs_fps_denominator.value() == 1001

    page.obs_fps_numerator.setValue(300000)
    page.obs_fps_denominator.setValue(1001)
    page._write_settings_ui_to_config(config)

    assert config["obs"]["fps_numerator"] == 300000
    assert config["obs"]["fps_denominator"] == 1001


def test_settings_page_saves_recording_encoder_preference(qtbot, monkeypatch):
    config = {
        "obs": {
            "password": "secret",
            "recording_encoder": "x264",
        },
        "paths": {},
        "storage": {},
        "polling": {},
        "audio": {},
        "app": {},
        "notifications": {},
    }
    monkeypatch.setattr(app, "load_config", lambda: config)

    page = SettingsPage(lambda: None)
    qtbot.addWidget(page)

    assert page.recording_encoder_combo.currentData() == "x264"

    assert page._select_combo_by_data(page.recording_encoder_combo, "auto") is True
    page._write_settings_ui_to_config(config)

    assert config["obs"]["recording_encoder"] == "auto"


def test_settings_page_saves_independent_notification_preferences(qtbot, monkeypatch):
    config = {
        "obs": {"password": "secret"},
        "paths": {},
        "storage": {},
        "polling": {},
        "audio": {},
        "app": {},
        "notifications": {},
    }
    monkeypatch.setattr(app, "load_config", lambda: config)

    page = SettingsPage(lambda: None)
    qtbot.addWidget(page)

    page.notifications_enabled_check.setChecked(True)
    page.notification_recording_started_check.setChecked(False)
    page.notification_recording_completed_check.setChecked(True)
    page.notification_recording_failed_check.setChecked(False)
    page.notification_minimized_to_tray_check.setChecked(True)
    page._write_settings_ui_to_config(config)

    assert config["notifications"] == {
        "enabled": True,
        "recording_started": False,
        "recording_completed": True,
        "recording_failed": False,
        "minimized_to_tray": True,
    }

    page.notifications_enabled_check.setChecked(False)
    assert page.notification_recording_started_check.isEnabled() is False
    assert page.notification_recording_completed_check.isEnabled() is False


class HeldAudioWorker(QObject):
    """外部接続せず、テストが明示的に完了させるワーカー。"""

    loaded = pyqtSignal(dict)
    failed = pyqtSignal(str)
    finished = pyqtSignal()
    instances = []

    def __init__(self, data, **kwargs):
        super().__init__()
        self.data = data
        self.kwargs = kwargs
        self.instances.append(self)

    def start(self):
        pass

    def isRunning(self):
        return True


@pytest.fixture
def live_audio_page(qtbot, monkeypatch):
    from copy import deepcopy

    config = {
        "obs": {"password": "test"}, "paths": {}, "storage": {}, "polling": {},
        "audio": {"mic": {"device_id": "original", "device_name": "Original", "mute": False}},
        "app": {}, "notifications": {},
    }
    monkeypatch.setattr(app, "load_config", lambda: deepcopy(config))
    page = SettingsPage(lambda: None)
    qtbot.addWidget(page)
    page.set_recording_audio_only(True)
    HeldAudioWorker.instances = []
    monkeypatch.setattr(app, "AudioDeviceRefreshWorker", HeldAudioWorker)
    monkeypatch.setattr(app, "AudioApplyWorker", HeldAudioWorker)
    monkeypatch.setattr(app, "run_preflight", Mock(side_effect=AssertionError("live preflight")))
    monkeypatch.setattr(app, "save_config", Mock(side_effect=AssertionError("UI snapshot saved")))
    monkeypatch.setattr(app.QMessageBox, "information", Mock())
    monkeypatch.setattr(app.QMessageBox, "warning", Mock())
    yield page
    page._audio_apply_timer.stop()
    page._audio_apply_pending = False
    page._audio_refresh_worker = None
    page._audio_apply_worker = None


def test_live_audio_save_flushes_latest_mute_without_output_or_full_config(live_audio_page):
    page = live_audio_page
    page.fields["obs.port"].setText("9999")
    page.audio_mic_mute.setChecked(True)
    page.apply_runtime_output_settings_to_obs = Mock(side_effect=AssertionError("output configured"))
    page.save_settings()

    worker = HeldAudioWorker.instances[-1]
    assert worker.kwargs == {"auto_launch": False, "live_audio": True}
    assert set(worker.data) == {"audio"}
    assert worker.data["audio"]["mic"]["mute"] is True
    assert not page._audio_apply_timer.isActive()
    assert not page.settings_buttons.isEnabled()
    worker.loaded.emit({"obs_launched": False})
    worker.finished.emit()
    assert page.settings_buttons.isEnabled()
    assert page.back_btn.isEnabled()
    assert page.tabs.currentIndex() == 1
    assert all(not page.tabs.isTabEnabled(index) for index in (0, 2, 3))
    page.apply_runtime_output_settings_to_obs.assert_not_called()


def test_live_refresh_preserves_new_mute_volume_and_missing_device_then_saves(live_audio_page):
    page = live_audio_page
    assert page.refresh_audio_devices(show_message=False)
    refresh = HeldAudioWorker.instances[-1]
    assert refresh.kwargs == {"auto_launch": False, "live_audio": True}
    assert page.audio_mic_mute.isEnabled()
    assert page.audio_mic_volume.isEnabled()
    assert not page.audio_mic_device.isEnabled()
    page.audio_mic_mute.setChecked(True)
    page.audio_mic_volume.setValue(-125)
    page._audio_apply_timer.stop()
    page._apply_audio_settings_auto()
    assert not page.settings_buttons.isEnabled()
    page.save_settings()  # busy中の呼び出しも新しいワーカーを開始しない。
    assert len(HeldAudioWorker.instances) == 1
    refresh.loaded.emit({
        "catalog": {"mic": [{"id": "other", "name": "Other"}]},
        "config": {"audio": {"mic": {"device_id": "old", "volume_db": 0, "mute": False}}},
    })
    assert page.audio_mic_device.currentData() == "original"
    assert page.audio_mic_mute.isChecked()
    assert page.audio_mic_volume.value() == -125
    refresh.finished.emit()
    assert len(HeldAudioWorker.instances) == 2
    apply = HeldAudioWorker.instances[-1]
    assert apply.data["audio"]["mic"]["device_id"] == "original"
    assert apply.data["audio"]["mic"]["mute"] is True
    assert apply.data["audio"]["mic"]["volume_db"] == -12.5
    apply.finished.emit()
    assert page.back_btn.isEnabled()
    page.save_settings()
    saved = HeldAudioWorker.instances[-1]
    assert saved.data["audio"]["mic"]["mute"] is True
    saved.finished.emit()


def test_live_apply_serializes_new_input_and_refresh_until_finished(live_audio_page):
    page = live_audio_page
    page.apply_audio_settings_to_obs(show_success=False)
    first = HeldAudioWorker.instances[-1]
    page.audio_mic_mute.setChecked(True)
    page._audio_apply_timer.stop()
    page._apply_audio_settings_auto()
    assert not page.refresh_audio_devices()
    assert len(HeldAudioWorker.instances) == 1
    first.finished.emit()
    assert len(HeldAudioWorker.instances) == 2
    second = HeldAudioWorker.instances[-1]
    assert second.data["audio"]["mic"]["mute"] is True
    assert second.kwargs["live_audio"] is True
    second.finished.emit()
    assert page.settings_buttons.isEnabled()


def test_mute_has_large_clickable_indicator_and_immediate_state_text(live_audio_page, qtbot):
    page = live_audio_page
    page.show()
    checkbox = page.audio_mic_mute
    qtbot.mouseClick(checkbox, Qt.MouseButton.LeftButton)
    assert checkbox.isChecked()
    assert "ON" in checkbox.text()
    assert checkbox.minimumHeight() >= 36
    assert "width: 24px" in checkbox.styleSheet()
    qtbot.mouseClick(checkbox, Qt.MouseButton.LeftButton)
    assert not checkbox.isChecked()
    assert "OFF" in checkbox.text()


@pytest.mark.parametrize("read_failed", [False, True])
def test_reset_during_debounce_restores_navigation(live_audio_page, monkeypatch, read_failed):
    page = live_audio_page
    load = Mock(return_value={"audio": {"mic": {"mute": False}}})
    monkeypatch.setattr(app.CONFIG_CONTROLLER.repository, "load", load)
    page.audio_mic_mute.setChecked(True)
    assert not page.back_btn.isEnabled()
    if read_failed:
        load.side_effect = OSError("read failed")
        with pytest.raises(OSError, match="read failed"):
            page.reset_settings()
    else:
        page.reset_settings()
        assert not page.audio_mic_mute.isChecked()
    assert not page._audio_apply_timer.isActive()
    assert page.back_btn.isEnabled()
    assert page.settings_buttons.isEnabled()
    assert not HeldAudioWorker.instances


def test_reset_button_click_cancels_debounce_and_restores_saved_mute(live_audio_page, monkeypatch, qtbot):
    page = live_audio_page
    load = Mock(return_value={"audio": {"mic": {"mute": False}}})
    monkeypatch.setattr(app.CONFIG_CONTROLLER.repository, "load", load)
    page.show()
    page.audio_mic_mute.setChecked(True)
    assert page._audio_apply_timer.isActive()
    assert not page.back_btn.isEnabled()
    reset = page.settings_buttons.button(app.QDialogButtonBox.StandardButton.Reset)

    qtbot.mouseClick(reset, Qt.MouseButton.LeftButton)

    load.assert_called_once_with(create_if_missing=False)
    assert not page.audio_mic_mute.isChecked()
    assert not page._audio_apply_timer.isActive()
    assert page.back_btn.isEnabled()
    assert not HeldAudioWorker.instances


def test_audio_prepare_failure_restores_navigation(live_audio_page):
    page = live_audio_page
    page.audio_mic_mute.setChecked(True)
    page._collect_audio_data_from_ui = Mock(side_effect=ValueError("invalid mic"))
    page.save_settings()
    assert page.back_btn.isEnabled()
    assert page.settings_buttons.isEnabled()
    assert not HeldAudioWorker.instances


def test_general_save_preflight_failure_restores_navigation(live_audio_page, monkeypatch):
    page = live_audio_page
    page.set_recording_audio_only(False)
    page.audio_mic_mute.setChecked(True)
    monkeypatch.setattr(app, "run_preflight", Mock(return_value={"errors": ["unavailable"]}))
    monkeypatch.setattr(app.QMessageBox, "critical", Mock())
    page.save_settings()
    assert page.back_btn.isEnabled()
    assert page.settings_buttons.isEnabled()
    assert not page._audio_apply_timer.isActive()


def test_general_save_carries_debounced_mute_to_serial_output_worker(live_audio_page, monkeypatch):
    page = live_audio_page
    page.set_recording_audio_only(False)
    saved = []
    monkeypatch.setattr(app, "save_config", lambda config: saved.append(config))
    monkeypatch.setattr(app, "run_preflight", lambda config, **kwargs: {"errors": [], "config": config})
    monkeypatch.setattr(app, "RuntimeOutputApplyWorker", HeldAudioWorker)
    page.audio_mic_mute.setChecked(True)
    page.save_settings()
    worker = HeldAudioWorker.instances[-1]
    assert worker.data["audio"]["mic"]["mute"] is True
    assert saved[-1]["audio"]["mic"]["mute"] is True
    assert not page._audio_apply_timer.isActive()
    assert not page.back_btn.isEnabled()
    worker.finished.emit()
    assert page.back_btn.isEnabled()


def test_busy_save_does_not_cancel_latest_mic_debounce(live_audio_page):
    page = live_audio_page
    page.apply_audio_settings_to_obs(show_success=False)
    page.audio_mic_mute.setChecked(True)
    page.save_settings()
    assert page._audio_apply_timer.isActive()
    assert len(HeldAudioWorker.instances) == 1
    HeldAudioWorker.instances[-1].finished.emit()


@pytest.mark.parametrize("method,worker_name", [
    ("run_preflight_fix", "PreflightWorker"), ("run_quick_setup", "QuickSetupWorker"),
])
def test_general_worker_blocks_navigation_and_mic_until_finished(live_audio_page, monkeypatch, method, worker_name):
    page = live_audio_page
    page.set_recording_audio_only(False)
    monkeypatch.setattr(app, worker_name, HeldAudioWorker)
    getattr(page, method)()
    worker = HeldAudioWorker.instances[-1]
    assert not page.back_btn.isEnabled()
    assert not page.settings_buttons.isEnabled()
    assert not page.tabs.isTabEnabled(1)
    assert not page.audio_mic_mute.isEnabled()
    assert not page.audio_mic_device.isEnabled()
    assert not page.refresh_audio_devices()
    page.save_settings()
    assert len(HeldAudioWorker.instances) == 1
    worker.finished.emit()
    assert page.back_btn.isEnabled()
    assert page.settings_buttons.isEnabled()
    assert page.tabs.isTabEnabled(1)
    assert page.audio_mic_mute.isEnabled()
    assert page.audio_mic_device.isEnabled()


@pytest.mark.parametrize("recording", [False, True])
def test_settings_entry_uses_atomic_reservation_and_keeps_recording(recording, monkeypatch):
    supervisor = RecordingSupervisor(config_controller=Mock(), recording_controller=Mock())
    if recording:
        assert supervisor._begin_recording()
    worker = app.RecorderWorker()
    worker.supervisor = supervisor
    worker.isRunning = Mock(return_value=True)
    worker.stop = Mock()
    settings = SimpleNamespace(set_recording_audio_only=Mock(), on_page_shown=Mock())
    window = SimpleNamespace(
        _stop_player=Mock(), bg_recorder_worker=worker, stop_background_recorder=Mock(return_value=True),
        _last_recorder_shutdown_failed=False, settings_page=settings, stack=Mock(),
    )
    app.MainWindow.show_settings(window)
    settings.set_recording_audio_only.assert_called_once_with(recording)
    window.stack.setCurrentWidget.assert_called_once_with(settings)
    if recording:
        worker.stop.assert_not_called()
        window.stop_background_recorder.assert_not_called()
        assert not supervisor._update_shutdown_reserved
    else:
        worker.stop.assert_called_once_with()
        window.stop_background_recorder.assert_called_once_with(wait_ms=5000)
        assert supervisor._update_shutdown_reserved
        assert not supervisor._begin_recording()


@pytest.mark.parametrize("wait_ok,cleanup_failed", [(False, False), (True, True)])
def test_settings_entry_blocks_general_edit_after_failed_idle_shutdown(wait_ok, cleanup_failed, monkeypatch):
    worker = SimpleNamespace(
        isRunning=lambda: True, request_update_shutdown=lambda: True,
        update_shutdown_failed=lambda: cleanup_failed,
    )
    settings = SimpleNamespace(set_recording_audio_only=Mock(), on_page_shown=Mock())
    window = SimpleNamespace(
        _stop_player=Mock(), bg_recorder_worker=worker,
        stop_background_recorder=Mock(return_value=wait_ok), _last_recorder_shutdown_failed=False,
        settings_page=settings, stack=Mock(),
    )
    monkeypatch.setattr(app.QMessageBox, "warning", Mock())
    app.MainWindow.show_settings(window)
    settings.set_recording_audio_only.assert_not_called()
    window.stack.setCurrentWidget.assert_not_called()
    assert window._last_recorder_shutdown_failed is cleanup_failed


def test_settings_entry_checks_finished_worker_before_queued_finished_signal(monkeypatch):
    worker = SimpleNamespace(isRunning=lambda: False, update_shutdown_failed=lambda: True)
    settings = SimpleNamespace(set_recording_audio_only=Mock(), on_page_shown=Mock())
    window = SimpleNamespace(
        _stop_player=Mock(), bg_recorder_worker=worker, _last_recorder_shutdown_failed=False,
        settings_page=settings, stack=Mock(),
    )
    monkeypatch.setattr(app.QMessageBox, "warning", Mock())
    app.MainWindow.show_settings(window)
    settings.set_recording_audio_only.assert_not_called()
    window.stack.setCurrentWidget.assert_not_called()
    assert window._last_recorder_shutdown_failed
