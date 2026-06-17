from types import SimpleNamespace
from unittest.mock import Mock

from src import recordtest
from src.controllers import AudioSettingsController, ConfigController


class FakeRuntime:
    def __init__(self) -> None:
        self.recorder = SimpleNamespace(apply_record_output_settings=Mock())
        self.owns_process = True
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeRuntimeManager:
    def __init__(self) -> None:
        self.runtime = FakeRuntime()
        self.calls = []

    def open_recorder(self, config, **kwargs):
        self.calls.append((config, kwargs))
        return self.runtime


class FakeConfigController:
    def __init__(self) -> None:
        self.saved = []

    def run_preflight(self, data, auto_fix=True, force_obs_detect=True):
        return {"config": data, "changed": False, "errors": []}

    def save_config(self, data):
        self.saved.append(data)


def test_runtime_output_settings_launches_managed_obs_when_stopped(monkeypatch, tmp_path):
    runtime_manager = FakeRuntimeManager()
    controller = AudioSettingsController(
        config_controller=FakeConfigController(),
        runtime_manager=runtime_manager,
    )
    data = {
        "paths": {
            "recordings_dir": str(tmp_path / "recordings"),
            "json_dir": str(tmp_path / "recordings" / "json"),
        }
    }
    monkeypatch.setattr(recordtest, "ensure_recording_dirs", lambda config: None)

    assert controller.apply_runtime_output_settings(data) is True

    assert runtime_manager.calls[0][1]["auto_launch"] is True
    runtime_manager.runtime.recorder.apply_record_output_settings.assert_called_once()
    assert runtime_manager.runtime.closed is True


def test_connection_test_launches_and_closes_managed_obs(monkeypatch):
    runtime_manager = FakeRuntimeManager()
    controller = ConfigController(
        repository=SimpleNamespace(),
        runtime_manager=runtime_manager,
    )
    report = {"config": {}, "changed": False, "errors": []}
    monkeypatch.setattr(controller, "run_preflight", lambda *args, **kwargs: report)

    returned_report, ok, detail = controller.test_obs_connection({})

    assert returned_report is report
    assert ok is True
    assert "接続成功" in detail
    assert runtime_manager.calls[0][1]["auto_launch"] is True
    assert runtime_manager.runtime.closed is True


def test_apply_auto_defaults_preserves_setup_completed_without_forced_detection(monkeypatch):
    controller = ConfigController(repository=SimpleNamespace(), runtime_manager=FakeRuntimeManager())
    monkeypatch.setattr(recordtest, "detect_obs_dir", lambda: None)

    config, _changed, _notes = controller.apply_auto_defaults(
        {"app": {"setup_completed": True}},
        force_obs_detect=False,
    )

    assert config["app"]["setup_completed"] is True


def test_apply_auto_defaults_updates_setup_completed_with_forced_detection(monkeypatch):
    controller = ConfigController(repository=SimpleNamespace(), runtime_manager=FakeRuntimeManager())
    monkeypatch.setattr(recordtest, "detect_obs_dir", lambda: None)

    config, changed, notes = controller.apply_auto_defaults(
        {"app": {"setup_completed": True}},
        force_obs_detect=True,
    )

    assert config["app"]["setup_completed"] is False
    assert changed is True
    assert "OBSフォルダの検出結果に合わせて初期設定状態を更新しました。" in notes
