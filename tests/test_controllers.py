from types import SimpleNamespace
from unittest.mock import Mock

import pytest

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


@pytest.mark.parametrize(
    ("method_name", "recorder_method"),
    [
        ("refresh_audio_devices", "get_audio_device_catalog"),
        ("apply_audio_settings", "apply_audio_profile"),
        ("apply_runtime_output_settings", "apply_record_output_settings"),
    ],
)
def test_audio_settings_actions_keep_body_primary_when_runtime_close_fails(
    monkeypatch,
    method_name,
    recorder_method,
):
    primary_error = recordtest.RecorderError("settings body failed")
    cleanup_error = RuntimeError("owned OBS cleanup failed")
    recorder = SimpleNamespace(
        get_audio_device_catalog=Mock(),
        apply_audio_profile=Mock(),
        apply_record_output_settings=Mock(),
    )
    getattr(recorder, recorder_method).side_effect = primary_error

    class Runtime:
        owns_process = True

        def __init__(self):
            self.recorder = recorder
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            raise cleanup_error

    runtime = Runtime()
    runtime_manager = SimpleNamespace(open_recorder=lambda *args, **kwargs: runtime)
    controller = AudioSettingsController(
        config_controller=FakeConfigController(),
        runtime_manager=runtime_manager,
    )
    monkeypatch.setattr(
        controller,
        "_prepare_config",
        lambda *args, **kwargs: ({"config": {}}, SimpleNamespace()),
    )

    with pytest.raises(recordtest.RecorderError) as captured:
        getattr(controller, method_name)({})

    assert captured.value is primary_error
    assert runtime.close_calls == 1
    assert any("owned OBS cleanup failed" in note for note in primary_error.__notes__)
    assert any("手動で終了" in note for note in primary_error.__notes__)


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
