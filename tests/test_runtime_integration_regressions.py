from __future__ import annotations

import json
import socket
import webbrowser
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest
import urllib3
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QDialogButtonBox, QMessageBox, QPushButton, QVBoxLayout, QWidget

from scripts import setup_env
from src import app as app_module, player as player_module, recorder_config, recordtest, self_check
from src.app import MainWindow, SettingsPage, SetupWizardDialog
from src.controllers import ConfigController
from src.player import ClipExportWorker, PlayerWidget

_OBS_OFFICIAL_PAGE = "https://github.com/obsproject/obs-studio/releases"
_FFMPEG_OFFICIAL_PAGE = "https://ffmpeg.org/download.html"
_OFFICIAL_PAGE_ALLOWLIST = frozenset({_OBS_OFFICIAL_PAGE, _FFMPEG_OFFICIAL_PAGE})


def _manual_setup_message_box_type(qtbot, *, choose_official_page: bool):
    class ManualSetupMessageBox(QMessageBox):
        def exec(self) -> int:
            if choose_official_page:
                target = next(
                    button
                    for button in self.buttons()
                    if self.buttonRole(button) == QMessageBox.ButtonRole.ActionRole
                )
            else:
                target = self.button(QMessageBox.StandardButton.Close)
            assert target is not None
            QTimer.singleShot(0, lambda: qtbot.mouseClick(target, Qt.MouseButton.LeftButton))
            return super().exec()

    return ManualSetupMessageBox


class _HomePageStub(QWidget):
    def __init__(self, on_play, on_settings, on_analytics) -> None:
        super().__init__()
        self.status_updates = []
        layout = QVBoxLayout(self)
        self.play_btn = QPushButton("リプレイを再生")
        self.settings_btn = QPushButton("設定")
        self.analytics_btn = QPushButton("データ分析")
        self.play_btn.clicked.connect(on_play)
        self.settings_btn.clicked.connect(on_settings)
        self.analytics_btn.clicked.connect(on_analytics)
        layout.addWidget(self.play_btn)
        layout.addWidget(self.settings_btn)
        layout.addWidget(self.analytics_btn)

    def set_recorder_status(self, badge_text: str, color_hex: str = "#cfcfcf") -> None:
        self.status_updates.append((badge_text, color_hex))


class _PlayerPageStub(QWidget):
    def __init__(self, on_back) -> None:
        super().__init__()
        self.back_btn = QPushButton("戻る", self)
        self.back_btn.clicked.connect(on_back)

    def on_leave(self, timeout_ms: int = 3000) -> bool:
        return True

    def open_selector(self) -> bool:
        return False


class _AnalyticsPageStub(QWidget):
    def __init__(self, on_back) -> None:
        super().__init__()
        self.back_btn = QPushButton("戻る", self)
        self.back_btn.clicked.connect(on_back)

    def on_page_shown(self) -> None:
        pass

    def stop_workers(self, timeout_ms: int = 1500) -> bool:
        return True


class _SettingsPageStub(QWidget):
    setup_completed = pyqtSignal()

    def __init__(self, on_back) -> None:
        super().__init__()
        self.load_count = 0
        self.shown_count = 0
        layout = QVBoxLayout(self)
        self.back_btn = QPushButton("戻る")
        self.back_btn.clicked.connect(on_back)
        layout.addWidget(self.back_btn)

    def load_settings(self) -> None:
        self.load_count += 1

    def on_page_shown(self) -> None:
        self.shown_count += 1

    def stop_workers(self, timeout_ms: int = 1500) -> bool:
        return True


@pytest.fixture
def runtime_roots(monkeypatch, tmp_path):
    app_root = tmp_path / "install"
    data_root = tmp_path / "data"
    obs_root = data_root / "obs-portable"
    app_root.mkdir()

    monkeypatch.setattr(recorder_config, "get_user_data_root", lambda: data_root)
    monkeypatch.setattr(player_module, "ROOT_DIR", app_root)
    monkeypatch.setattr(player_module, "DATA_DIR", data_root)
    monkeypatch.setattr(app_module, "ROOT_DIR", app_root)
    monkeypatch.setattr(app_module, "DATA_DIR", data_root)
    monkeypatch.setattr(self_check, "get_app_root", lambda: app_root)
    monkeypatch.setattr(self_check, "get_user_data_root", lambda: data_root)

    monkeypatch.setattr(recordtest, "ROOT_DIR", app_root)
    monkeypatch.setattr(recordtest, "DATA_DIR", data_root)
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_root)
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", app_root / "obs-portable")
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", app_root / "bin" / "OBS-Studio")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", data_root / "bin" / "OBS-Studio")

    monkeypatch.setattr(setup_env, "ROOT_DIR", app_root)
    monkeypatch.setattr(setup_env, "DATA_DIR", data_root)
    monkeypatch.setattr(setup_env, "BIN_DIR", data_root / "bin")
    monkeypatch.setattr(setup_env, "OBS_PORTABLE_DIR", obs_root)
    monkeypatch.setattr(setup_env, "OBS_EXE", obs_root / "bin" / "64bit" / "obs64.exe")
    monkeypatch.setattr(setup_env, "LEGACY_ROOT_OBS_PORTABLE_DIR", app_root / "obs-portable")
    monkeypatch.setattr(setup_env, "LEGACY_OBS_PORTABLE_DIR", app_root / "bin" / "OBS-Studio")
    monkeypatch.setattr(setup_env, "LEGACY_DATA_BIN_OBS_PORTABLE_DIR", data_root / "bin" / "OBS-Studio")

    return SimpleNamespace(app_root=app_root, data_root=data_root, obs_root=obs_root)


def _runtime_config(*, setup_completed: bool = True) -> dict:
    # The autouse fixture in tests/conftest.py returns an isolated copy.
    config = recordtest.load_settings()
    config["obs"]["dir"] = "obs-portable"
    config["obs"]["password"] = "integration-secret"
    config["paths"].update(
        {
            "bin_dir": "bin",
            "ffmpeg_executable": "tools/ffmpeg.exe",
            "recordings_dir": "recordings",
            "json_dir": "recordings/json",
            "champion_icons_dir": "assets/champions/icons",
            "champion_aliases_path": "config/champion_aliases.json",
        }
    )
    config["app"]["setup_completed"] = setup_completed
    return config


def _write_runtime(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ffmpeg runtime fixture")
    return path.resolve()


def _self_check_ffmpeg_path(report: dict) -> Path | None:
    check = next(item for item in report["checks"] if item["name"] == "ffmpeg")
    if check["status"] != "ok":
        return None
    prefix = "ffmpeg="
    assert check["message"].startswith(prefix)
    return Path(check["message"][len(prefix) :])


@pytest.mark.parametrize("selected_source", ["explicit", "bin", "app_root", "absolute_path"])
def test_player_and_self_check_choose_the_same_ffmpeg(
    monkeypatch,
    runtime_roots,
    tmp_path,
    selected_source,
):
    config = _runtime_config()
    candidates = {
        "explicit": runtime_roots.data_root / "tools" / "ffmpeg.exe",
        "bin": runtime_roots.data_root / "bin" / "ffmpeg.exe",
        "app_root": runtime_roots.app_root / "bin" / "ffmpeg.exe",
        "absolute_path": tmp_path / "system-path" / "ffmpeg.exe",
    }
    priority = tuple(candidates)
    selected_index = priority.index(selected_source)
    for source in priority[selected_index:]:
        _write_runtime(candidates[source])

    absolute_path_value = str(candidates["absolute_path"].parent.resolve())
    monkeypatch.setenv("PATH", absolute_path_value)
    monkeypatch.setattr(player_module, "load_app_config", lambda: config)

    config_path = tmp_path / "self-check" / "setting.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(self_check, "CONFIG_PATH", config_path)
    monkeypatch.setattr(self_check, "SAMPLE_CONFIG_PATH", tmp_path / "missing-sample.json")
    monkeypatch.setattr(self_check, "has_mpv_dll", lambda _bin_dir, _app_root: False)

    player_path = player_module.find_ffmpeg_executable()
    report = self_check.run_self_check()
    self_check_path = _self_check_ffmpeg_path(report)

    expected = candidates[selected_source].resolve()
    assert Path(player_path) == expected
    assert self_check_path == expected


def _forbid_external_interactions(monkeypatch):
    communication_calls = []
    opened_urls = []

    def forbidden(label):
        def fail(*_args, **_kwargs):
            communication_calls.append(label)
            raise AssertionError(f"unexpected external communication: {label}")

        return fail

    monkeypatch.setattr(socket, "create_connection", forbidden("socket.create_connection"))
    monkeypatch.setattr(socket.socket, "connect", forbidden("socket.connect"))
    monkeypatch.setattr(urllib3.PoolManager, "request", forbidden("urllib3.PoolManager.request"))
    monkeypatch.setattr(recordtest.obs, "ReqClient", forbidden("obs.ReqClient"))
    monkeypatch.setattr(webbrowser, "open", forbidden("webbrowser.open"))
    monkeypatch.setattr(webbrowser, "open_new", forbidden("webbrowser.open_new"))
    monkeypatch.setattr(webbrowser, "open_new_tab", forbidden("webbrowser.open_new_tab"))

    def record_url(url) -> bool:
        opened_urls.append(url.toString())
        return True

    desktop_services = SimpleNamespace(openUrl=record_url)
    monkeypatch.setattr(app_module, "QDesktopServices", desktop_services)
    monkeypatch.setattr(player_module, "QDesktopServices", desktop_services)
    return communication_calls, opened_urls


def test_missing_runtimes_do_not_communicate_or_open_a_browser_without_user_action(
    monkeypatch,
    qtbot,
    runtime_roots,
):
    communication_calls, opened_urls = _forbid_external_interactions(monkeypatch)
    config = _runtime_config(setup_completed=False)
    monkeypatch.setenv("PATH", str(runtime_roots.data_root / "missing-path"))

    controller = ConfigController()
    preflight_report = controller.run_preflight(config, auto_fix=True, force_obs_detect=True)
    assert preflight_report["errors"]

    monkeypatch.setattr(
        player_module,
        "QMessageBox",
        _manual_setup_message_box_type(qtbot, choose_official_page=False),
    )
    monkeypatch.setattr(player_module, "load_app_config", lambda: config)
    clip_widget = QWidget()
    qtbot.addWidget(clip_widget)
    clip_widget.clip_worker = None
    clip_widget.current_video_path = "missing-runtime-replay.mp4"
    clip_widget.clip_start = 1.0
    clip_widget.clip_end = 2.0
    clip_widget.start_clip_export = Mock()
    clip_widget.show_ffmpeg_setup_required = MethodType(PlayerWidget.show_ffmpeg_setup_required, clip_widget)

    PlayerWidget.export_clip(clip_widget)

    clip_widget.start_clip_export.assert_not_called()
    assert communication_calls == []
    assert opened_urls == []


def test_cancelled_startup_setup_then_settings_completion_restarts_monitor_on_home(
    monkeypatch,
    qtbot,
    runtime_roots,
):
    communication_calls, opened_urls = _forbid_external_interactions(monkeypatch)
    config = _runtime_config(setup_completed=False)
    monkeypatch.setenv("PATH", str(runtime_roots.data_root / "missing-path"))
    controller = ConfigController()
    cancelled_wizards = []

    class AutoCancelledSetupWizard(SetupWizardDialog):
        def __init__(self, parent, startup_mode):
            super().__init__(parent, startup_mode)
            cancelled_wizards.append(self)
            button_box = self.findChild(QDialogButtonBox)
            assert button_box is not None
            cancel_button = button_box.button(QDialogButtonBox.StandardButton.Cancel)
            assert cancel_button is not None
            QTimer.singleShot(0, lambda: qtbot.mouseClick(cancel_button, Qt.MouseButton.LeftButton))

    monkeypatch.setattr(app_module, "load_config", lambda: config)
    monkeypatch.setattr(app_module, "run_preflight", controller.run_preflight)
    monkeypatch.setattr(app_module, "save_config", lambda _data: None)
    monkeypatch.setattr(app_module, "SetupWizardDialog", AutoCancelledSetupWizard)
    monkeypatch.setattr(app_module.QMessageBox, "warning", Mock())
    monkeypatch.setattr(app_module, "HomePage", _HomePageStub)
    monkeypatch.setattr(app_module, "PlayerPage", _PlayerPageStub)
    monkeypatch.setattr(app_module, "AnalyticsPage", _AnalyticsPageStub)
    monkeypatch.setattr(app_module, "SettingsPage", _SettingsPageStub)
    monkeypatch.setattr(MainWindow, "init_tray_icon", lambda self: None)
    start_calls = []
    monkeypatch.setattr(MainWindow, "start_background_recorder", lambda self: start_calls.append(self))

    window = MainWindow()
    qtbot.addWidget(window)

    assert len(cancelled_wizards) == 1
    assert window.stack.currentWidget() is window.home_page
    assert window._recorder_autostart_enabled is False
    assert start_calls == []

    qtbot.mouseClick(window.home_page.settings_btn, Qt.MouseButton.LeftButton)
    assert window.stack.currentWidget() is window.settings_page
    assert window.settings_page.shown_count == 1

    window.settings_page.setup_completed.emit()
    assert window._recorder_autostart_enabled is True
    assert start_calls == []

    qtbot.mouseClick(window.settings_page.back_btn, Qt.MouseButton.LeftButton)
    assert window.stack.currentWidget() is window.home_page
    assert start_calls == [window]
    assert communication_calls == []
    assert opened_urls == []


def test_official_pages_open_only_from_explicit_button_clicks(monkeypatch, qtbot, runtime_roots):
    communication_calls, opened_urls = _forbid_external_interactions(monkeypatch)
    config = _runtime_config(setup_completed=False)
    monkeypatch.setattr(app_module, "load_config", lambda: config)
    monkeypatch.setattr(
        player_module,
        "get_ffmpeg_runtime_paths",
        lambda: SimpleNamespace(bin_dir=runtime_roots.data_root / "bin"),
    )

    setup_dialog = SetupWizardDialog(startup_mode=False)
    settings_page = SettingsPage(lambda: None)
    qtbot.addWidget(setup_dialog)
    qtbot.addWidget(settings_page)

    assert opened_urls == []
    qtbot.mouseClick(setup_dialog.obs_download_btn, Qt.MouseButton.LeftButton)
    assert opened_urls == [_OBS_OFFICIAL_PAGE]
    qtbot.mouseClick(settings_page.ffmpeg_download_btn, Qt.MouseButton.LeftButton)
    assert opened_urls == [_OBS_OFFICIAL_PAGE, _FFMPEG_OFFICIAL_PAGE]

    monkeypatch.setattr(
        player_module,
        "QMessageBox",
        _manual_setup_message_box_type(qtbot, choose_official_page=False),
    )
    player_parent = QWidget()
    qtbot.addWidget(player_parent)
    PlayerWidget.show_ffmpeg_setup_required(player_parent)
    assert opened_urls == [_OBS_OFFICIAL_PAGE, _FFMPEG_OFFICIAL_PAGE]

    monkeypatch.setattr(
        player_module,
        "QMessageBox",
        _manual_setup_message_box_type(qtbot, choose_official_page=True),
    )
    PlayerWidget.show_ffmpeg_setup_required(player_parent)

    assert opened_urls == [
        _OBS_OFFICIAL_PAGE,
        _FFMPEG_OFFICIAL_PAGE,
        _FFMPEG_OFFICIAL_PAGE,
    ]
    assert set(opened_urls) <= _OFFICIAL_PAGE_ALLOWLIST
    assert communication_calls == []


def test_clip_export_reports_when_resolved_ffmpeg_disappears_before_execution(
    monkeypatch,
    qtbot,
    runtime_roots,
    tmp_path,
):
    config = _runtime_config()
    selected = _write_runtime(runtime_roots.data_root / "tools" / "ffmpeg.exe")
    monkeypatch.setattr(player_module, "load_app_config", lambda: config)
    monkeypatch.setenv("PATH", str(runtime_roots.data_root / "missing-path"))
    resolved = player_module.find_ffmpeg_executable()
    assert resolved == str(selected)

    selected.unlink()
    worker = ClipExportWorker(
        resolved,
        tmp_path / "input.mp4",
        tmp_path / "clips" / "output.mp4",
        start_sec=1.0,
        end_sec=2.0,
    )
    failures = []
    completed = []
    worker.export_failed.connect(failures.append)
    worker.export_finished.connect(completed.append)

    worker.run()
    qtbot.wait(1)

    assert completed == []
    assert len(failures) == 1
    assert "FFmpegが見つかりません" in failures[0]
    assert not (tmp_path / "clips" / "output.mp4").exists()
