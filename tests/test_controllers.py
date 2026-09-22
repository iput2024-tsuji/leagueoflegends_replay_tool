from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import recordtest
from src.controllers import (
    AudioSettingsController,
    ConfigController,
    _close_runtime_preserving_primary,
)
from src.obs_runtime import RecorderRuntime


class FakeRuntime:
    def __init__(self) -> None:
        self.recorder = SimpleNamespace(apply_record_output_settings=Mock(), apply_audio_profile=Mock())
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
    runtime_manager.runtime.recorder.apply_audio_profile.assert_called_once_with(runtime_manager.calls[0][0])
    assert runtime_manager.runtime.closed is True


@pytest.mark.parametrize(
    ("method_name", "recorder_method"),
    [
        ("refresh_audio_devices", "get_audio_device_catalog"),
        ("apply_audio_settings", "apply_audio_profile"),
        ("apply_runtime_output_settings", "apply_record_output_settings"),
        ("apply_runtime_output_settings", "apply_audio_profile"),
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
    if method_name == "apply_runtime_output_settings" and recorder_method == "apply_audio_profile":
        recorder.apply_record_output_settings.assert_called_once_with()
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


def test_connection_test_cleanup_helper_preserves_control_flow_primary():
    primary_error = KeyboardInterrupt("connection test interrupted")
    cleanup_error = RuntimeError("connection runtime close failed")

    class Runtime:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            raise cleanup_error

    runtime = Runtime()

    with pytest.raises(KeyboardInterrupt) as captured:
        try:
            raise primary_error
        except BaseException as exc:
            _close_runtime_preserving_primary(runtime, exc)
            raise

    assert captured.value is primary_error
    assert runtime.close_calls == 1
    assert any("connection runtime close failed" in note for note in primary_error.__notes__)


def test_connection_cleanup_control_flow_supersedes_normal_primary():
    primary_error = OSError("connection test failed")
    cleanup_error = SystemExit("runtime close interrupted")
    cleanup_cause = RuntimeError("runtime close cause")
    cleanup_error.__cause__ = cleanup_cause
    cleanup_error.__suppress_context__ = True

    class Runtime:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            raise cleanup_error

    runtime = Runtime()

    with pytest.raises(SystemExit) as captured:
        try:
            raise primary_error
        except BaseException as exc:
            _close_runtime_preserving_primary(runtime, exc)
            raise

    assert captured.value is cleanup_error
    assert cleanup_error.__cause__ is cleanup_cause
    assert cleanup_error.__suppress_context__ is True
    assert runtime.close_calls == 1
    assert any(
        "connection test failed" in note
        for note in getattr(cleanup_error, "__notes__", [])
    )
    assert any(
        "手動で終了" in note
        for note in getattr(cleanup_error, "__notes__", [])
    )


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


@pytest.fixture
def live_audio_controller(monkeypatch):
    current = {
        "obs": {"scene_name": "recording-scene", "fps_numerator": 60},
        "paths": {"recordings_dir": "current-recordings"},
        "audio": {
            "mic": {
                "input_name": "existing-mic",
                "device_id": "device-a",
                "device_name": "Microphone A",
                "volume_db": 0.0,
                "mute": False,
            }
        },
    }
    repository = Mock()
    repository.load.side_effect = lambda **kwargs: deepcopy(current)
    repository.save.side_effect = lambda data: current.update(deepcopy(data))
    raw = Mock(spec=[
        "get_input_list", "get_scene_item_id", "get_scene_item_list", "set_scene_item_enabled", "send",
        "set_input_settings", "set_input_volume", "set_input_mute",
    ])
    raw.get_input_list.return_value = SimpleNamespace(inputs=[
        {"inputName": "existing-mic", "inputKind": "wasapi_input_capture"},
        {"inputName": "lol_game_audio", "inputKind": "wasapi_process_output_capture"},
    ])
    raw.get_scene_item_id.return_value = SimpleNamespace(scene_item_id=7)
    raw.get_scene_item_list.return_value = SimpleNamespace(scene_items=[
        {"sourceName": "existing-mic", "sceneItemId": 7, "sceneItemEnabled": True},
    ])
    raw.send.return_value = {"propertyItems": [
        {"itemValue": "device-b", "itemName": "Microphone B"},
    ]}
    recorder = SimpleNamespace(
        obs_client=SimpleNamespace(raw_client=raw),
        disconnect_obs=Mock(),
        shutdown_obs=Mock(),
        finalize_session=Mock(),
        get_audio_device_catalog=Mock(),
        apply_audio_profile=Mock(),
        apply_record_output_settings=Mock(),
    )
    runtime = RecorderRuntime(recorder=recorder)
    manager = SimpleNamespace(open_recorder=Mock(return_value=runtime))
    config = ConfigController(repository=repository, runtime_manager=manager)
    config.run_preflight = Mock(side_effect=AssertionError("live audio must skip preflight"))
    forbidden = [config.run_preflight]
    for name in ("ensure_recording_dirs", "launch_obs", "ensure_managed_audio_inputs"):
        guard = Mock(side_effect=AssertionError(f"live audio must not call {name}"))
        monkeypatch.setattr(recordtest, name, guard)
        forbidden.append(guard)
    controller = AudioSettingsController(config_controller=config, runtime_manager=manager)
    yield SimpleNamespace(
        controller=controller, current=current, repository=repository,
        raw=raw, recorder=recorder, manager=manager,
    )
    for guard in forbidden:
        guard.assert_not_called()
    recorder.shutdown_obs.assert_not_called()
    recorder.finalize_session.assert_not_called()
    recorder.get_audio_device_catalog.assert_not_called()
    recorder.apply_audio_profile.assert_not_called()
    recorder.apply_record_output_settings.assert_not_called()
    for call in manager.open_recorder.call_args_list:
        assert call.kwargs["auto_launch"] is False
        assert call.kwargs["auto_setup"] is False
        assert call.kwargs["configure_output"] is False


def test_live_audio_refresh_uses_existing_source_without_saving_stale_catalog_snapshot(live_audio_controller):
    state = live_audio_controller
    stale = {"obs": {"scene_name": "stale-scene"}, "audio": {"mic": {"input_name": "stale-mic"}}}
    original = deepcopy(stale)

    result = state.controller.refresh_audio_devices(stale, auto_launch=True, live_audio=True)

    assert result == {"catalog": {"mic": [{"id": "device-b", "name": "Microphone B"}]}, "obs_launched": False}
    assert stale == original
    state.raw.get_scene_item_id.assert_called_once_with("recording-scene", "existing-mic")
    state.raw.send.assert_called_once_with(
        "GetInputPropertiesListPropertyItems",
        {"inputName": "existing-mic", "propertyName": "device_id"},
        raw=True,
    )
    state.raw.set_input_settings.assert_not_called()
    state.raw.set_input_volume.assert_not_called()
    state.raw.set_input_mute.assert_not_called()
    state.repository.load.assert_called_once_with(create_if_missing=False)
    state.repository.save.assert_not_called()
    state.recorder.disconnect_obs.assert_called_once_with()


@pytest.mark.parametrize(("device", "volume", "mute"), [
    ("default", -3.0, False), ("disabled", -12.0, True), ("disabled", 0.0, False), ("device-b", 6.0, True),
])
def test_live_audio_apply_only_updates_existing_mic_and_preserves_latest_general_settings(
    live_audio_controller, device, volume, mute,
):
    state = live_audio_controller
    stale = {
        "obs": {"scene_name": "stale-scene", "fps_numerator": 10},
        "paths": {"recordings_dir": "stale-recordings"},
        "audio": {"mic": {
            "input_name": "untrusted-new-mic", "device_id": device,
            "device_name": "Selected microphone", "volume_db": volume, "mute": mute,
        }},
    }

    def concurrent_general_save(*args):
        state.current["obs"]["fps_numerator"] = 120
        state.current["paths"]["recordings_dir"] = "new-recordings"
        state.current["notifications"] = {"enabled": False}
        state.current["audio"]["unrelated_option"] = "latest"

    state.raw.set_input_mute.side_effect = concurrent_general_save
    result = state.controller.apply_audio_settings(stale, auto_launch=True, live_audio=True)

    assert result == {"obs_launched": False}
    if device == "disabled":
        state.raw.set_input_settings.assert_not_called()
    else:
        state.raw.set_input_settings.assert_called_once_with("existing-mic", {"device_id": device}, overlay=True)
    state.raw.set_scene_item_enabled.assert_called_once_with("recording-scene", 7, device != "disabled")
    state.raw.set_input_volume.assert_called_once_with("existing-mic", vol_db=volume)
    state.raw.set_input_mute.assert_called_once_with("existing-mic", mute)
    state.raw.send.assert_not_called()
    assert state.repository.load.call_count == 2
    assert all(call.kwargs == {"create_if_missing": False} for call in state.repository.load.call_args_list)
    saved = state.repository.save.call_args.args[0]
    assert saved["obs"] == {"scene_name": "recording-scene", "fps_numerator": 120}
    assert saved["paths"]["recordings_dir"] == "new-recordings"
    assert saved["notifications"] == {"enabled": False}
    assert saved["audio"]["unrelated_option"] == "latest"
    assert saved["audio"]["mic"] == {
        "input_name": "existing-mic", "device_id": device,
        "device_name": "Selected microphone", "volume_db": volume, "mute": mute,
    }
    state.repository.save.assert_called_once()
    state.recorder.disconnect_obs.assert_called_once_with()


@pytest.mark.parametrize("method", ["refresh_audio_devices", "apply_audio_settings"])
@pytest.mark.parametrize("failure", ["missing_input", "wrong_kind", "missing_scene_item", "scene_query_error"])
def test_live_audio_invalid_existing_source_never_mutates_or_persists(live_audio_controller, method, failure):
    state = live_audio_controller
    if failure == "missing_input":
        state.raw.get_input_list.return_value.inputs = []
    elif failure == "wrong_kind":
        state.raw.get_input_list.return_value.inputs[0]["inputKind"] = "wasapi_process_output_capture"
    elif failure == "missing_scene_item":
        state.raw.get_scene_item_id.return_value = SimpleNamespace(scene_item_id=None)
    else:
        state.raw.get_scene_item_id.side_effect = recordtest.RecorderError("scene lookup failed")

    with pytest.raises(recordtest.RecorderError):
        getattr(state.controller, method)({"audio": {"mic": {"mute": True}}}, live_audio=True)

    state.raw.send.assert_not_called()
    state.raw.set_input_settings.assert_not_called()
    state.raw.set_input_volume.assert_not_called()
    state.raw.set_input_mute.assert_not_called()
    state.repository.save.assert_not_called()
    state.recorder.disconnect_obs.assert_called_once_with()


@pytest.mark.parametrize("failed_call", ["set_input_settings", "set_input_volume", "set_input_mute", "set_scene_item_enabled"])
def test_live_audio_apply_failure_preserves_primary_and_disconnects_borrowed_runtime(
    live_audio_controller, failed_call,
):
    state = live_audio_controller
    primary = recordtest.RecorderError("microphone operation failed")
    getattr(state.raw, failed_call).side_effect = primary
    state.recorder.disconnect_obs.side_effect = RuntimeError("borrowed disconnect failed")

    with pytest.raises(recordtest.RecorderError) as caught:
        state.controller.apply_audio_settings({"audio": {"mic": {"mute": True}}}, live_audio=True)

    assert caught.value is primary
    assert any("borrowed disconnect failed" in note for note in primary.__notes__)
    state.repository.save.assert_not_called()
    state.recorder.disconnect_obs.assert_called_once_with()


def test_live_audio_catalog_failure_disconnects_without_saving(live_audio_controller):
    state = live_audio_controller
    primary = RuntimeError("catalog request failed")
    state.raw.send.side_effect = primary

    with pytest.raises(recordtest.RecorderError) as caught:
        state.controller.refresh_audio_devices({}, live_audio=True)

    assert caught.value.__cause__ is primary
    state.repository.save.assert_not_called()
    state.recorder.disconnect_obs.assert_called_once_with()


@pytest.mark.parametrize("method", ["refresh_audio_devices", "apply_audio_settings"])
def test_live_audio_owned_identity_rejection_never_reaches_obs_or_persistence(live_audio_controller, method):
    state = live_audio_controller
    primary = recordtest.RecorderError("managed OBS identity mismatch")
    state.manager.open_recorder.side_effect = primary

    with pytest.raises(recordtest.RecorderError) as caught:
        getattr(state.controller, method)({}, auto_launch=True, live_audio=True)

    assert caught.value is primary
    assert state.raw.mock_calls == []
    state.repository.save.assert_not_called()
    state.recorder.disconnect_obs.assert_not_called()


def test_live_microphone_disable_failure_does_not_save_or_change_device(live_audio_controller):
    state = live_audio_controller
    original = deepcopy(state.current)
    state.raw.set_scene_item_enabled.side_effect = recordtest.RecorderError("scene disable failed")
    with pytest.raises(recordtest.RecorderError, match="scene disable failed"):
        state.controller.apply_audio_settings(
            {"audio": {"mic": {"device_id": "disabled", "mute": False}}}, live_audio=True,
        )
    assert state.current == original
    state.raw.set_input_settings.assert_not_called()
    state.raw.set_input_mute.assert_not_called()
    state.repository.save.assert_not_called()
    state.recorder.disconnect_obs.assert_called_once_with()
