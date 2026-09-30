from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src import obs_websocket_client, recordtest

GAME_NAME = "lol_game_audio"
GAME_KIND = "wasapi_process_output_capture"


class GameAudioClient:
    """Stateful OBS boundary fake; never connects to an OBS process."""

    def __init__(self, config, *, existing=False, linked=True):
        self.scene_name = config.obs.scene_name
        self.inputs = {
            config.audio.mic.input_name: {
                "kind": "wasapi_input_capture",
                "settings": {"device_id": "test-microphone"},
                "mute": True,
                "volume_db": -8.0,
                "monitor": "OBS_MONITORING_TYPE_MONITOR_ONLY",
                "tracks": {"1": False, "2": True},
            },
        }
        self.scene_items = {self.scene_name: []}
        self.current_scene = "another-scene"
        self.calls = []
        self.record_requests = []
        self.fail_at = None
        self.supported_kinds = [GAME_KIND, "wasapi_input_capture", "window_capture"]
        self.next_id = 10
        if existing:
            self.inputs[GAME_NAME] = self._audio_state({"window": "old-target", "extra": "preserve"})
            if linked:
                self._add_scene_item(self.scene_name, GAME_NAME, False)

    @staticmethod
    def _audio_state(settings):
        return {
            "kind": GAME_KIND,
            "settings": dict(settings),
            "mute": True,
            "volume_db": -30.0,
            "monitor": "OBS_MONITORING_TYPE_MONITOR_ONLY",
            "tracks": {"1": False, "2": True, "3": False},
        }

    def _call(self, operation, *args):
        self.calls.append((operation, *deepcopy(args)))
        if self.fail_at == operation:
            raise RuntimeError(f"failed {operation}")

    def _add_scene_item(self, scene, name, enabled):
        item = {"sourceName": name, "sceneItemId": self.next_id, "sceneItemEnabled": enabled}
        self.next_id += 1
        self.scene_items.setdefault(scene, []).append(item)
        return item["sceneItemId"]

    def get_input_list(self):
        self._call("get_input_list")
        return SimpleNamespace(inputs=[
            {"inputName": name, "inputKind": state["kind"]}
            for name, state in self.inputs.items()
        ])

    def get_input_kind_list(self, unversioned):
        self._call("get_input_kind_list", unversioned)
        assert unversioned is True
        return SimpleNamespace(input_kinds=list(self.supported_kinds))

    def create_input(self, scene, name, kind, settings, enabled):
        self._call("create_input", scene, name, kind, settings, enabled)
        assert name not in self.inputs, "must not recreate an existing source"
        self.inputs[name] = self._audio_state(settings)
        self.inputs[name]["kind"] = kind
        item_id = self._add_scene_item(scene, name, enabled)
        return SimpleNamespace(scene_item_id=item_id)

    def set_input_settings(self, name, settings, overlay=True):
        self._call("set_input_settings", name, settings, overlay)
        if overlay:
            self.inputs[name]["settings"].update(settings)
        else:
            self.inputs[name]["settings"] = dict(settings)

    def get_scene_list(self):
        self._call("get_scene_list")
        return SimpleNamespace(scenes=[{"sceneName": name} for name in self.scene_items])

    def get_scene_item_list(self, scene):
        self._call("get_scene_item_list", scene)
        return SimpleNamespace(scene_items=deepcopy(self.scene_items.get(scene, [])))

    def create_scene_item(self, scene, name, enabled):
        self._call("create_scene_item", scene, name, enabled)
        item_id = self._add_scene_item(scene, name, enabled)
        return SimpleNamespace(scene_item_id=item_id)

    def set_scene_item_enabled(self, scene, item_id, enabled):
        self._call("set_scene_item_enabled", scene, item_id, enabled)
        item = next(item for item in self.scene_items[scene] if item["sceneItemId"] == item_id)
        item["sceneItemEnabled"] = enabled

    def set_input_mute(self, name, muted):
        self._call("set_input_mute", name, muted)
        self.inputs[name]["mute"] = muted

    def set_input_volume(self, name, vol_mul=None, vol_db=None):
        self._call("set_input_volume", name, vol_mul, vol_db)
        assert vol_mul is None
        self.inputs[name]["volume_db"] = vol_db

    def set_input_audio_monitor_type(self, name, mon_type):
        self._call("set_input_audio_monitor_type", name, mon_type)
        self.inputs[name]["monitor"] = mon_type

    def set_input_audio_tracks(self, name, track):
        self._call("set_input_audio_tracks", name, track)
        self.inputs[name]["tracks"].update(track)

    def set_current_program_scene(self, name):
        self._call("set_current_program_scene", name)
        self.current_scene = name

    def send(self, request_type, payload, raw=True):
        self._call("send", request_type, payload, raw)
        self.record_requests.append((request_type, deepcopy(self.inputs[GAME_NAME]), self.current_scene))
        return {"requestStatus": {"result": True}}


@pytest.fixture
def config(tmp_path):
    return recordtest.AppConfig.from_dict({
        "paths": {"recordings_dir": str(tmp_path), "json_dir": str(tmp_path / "json")},
    })


def make_client(config, **kwargs):
    client = obs_websocket_client.ObsWebSocketClient(config=config)
    client.client = GameAudioClient(config, **kwargs)
    return client, client.client


def assert_audio_ready(config, state):
    assert state["kind"] == GAME_KIND
    assert state["settings"]["window"] == config.obs.window_capture_window
    assert state["settings"]["priority"] == 2
    assert state["mute"] is False
    assert state["volume_db"] == 0.0
    assert state["monitor"] == "OBS_MONITORING_TYPE_NONE"
    assert state["tracks"] == {"1": True, "2": True, "3": False}


@pytest.mark.parametrize("existing,linked", [(False, False), (True, True), (True, False)])
def test_game_audio_is_created_or_restored_without_changing_microphone(config, existing, linked):
    client, raw = make_client(config, existing=existing, linked=linked)
    microphone = deepcopy(raw.inputs[config.audio.mic.input_name])

    client._ensure_game_audio_capture()

    assert_audio_ready(config, raw.inputs[GAME_NAME])
    assert raw.inputs[config.audio.mic.input_name] == microphone
    assert raw.current_scene == config.obs.scene_name
    assert raw.scene_items[config.obs.scene_name] == [{
        "sourceName": GAME_NAME, "sceneItemId": 10, "sceneItemEnabled": True,
    }]
    operations = [call[0] for call in raw.calls]
    assert operations.count("create_input") == (0 if existing else 1)
    assert operations.count("create_scene_item") == (1 if existing and not linked else 0)
    if existing:
        assert raw.inputs[GAME_NAME]["settings"]["extra"] == "preserve"


def test_repeated_game_audio_setup_reuses_the_same_source_and_scene_item(config):
    client, raw = make_client(config)
    client._ensure_game_audio_capture()
    state = deepcopy((raw.inputs, raw.scene_items))

    client._ensure_game_audio_capture()

    assert (raw.inputs, raw.scene_items) == state
    assert sum(call[0] == "create_input" for call in raw.calls) == 1
    assert not any(call[0] == "create_scene_item" for call in raw.calls)


@pytest.mark.parametrize("reserved_name", ["window_capture_name", "source_name", "mic"])
@pytest.mark.parametrize("setup_method", ["_ensure_game_audio_capture", "setup_sync_elements"])
def test_configured_source_name_collision_is_rejected_without_modification(config, reserved_name, setup_method):
    if reserved_name == "mic":
        config = replace(config, audio=replace(config.audio, mic=replace(config.audio.mic, input_name=GAME_NAME)))
    else:
        config = replace(config, obs=replace(config.obs, **{reserved_name: GAME_NAME}))
    client, raw = make_client(config, existing=True)
    before = deepcopy((raw.inputs, raw.scene_items, raw.current_scene))

    with pytest.raises(recordtest.RecorderError, match="重複"):
        getattr(client, setup_method)()

    assert (raw.inputs, raw.scene_items, raw.current_scene) == before
    assert raw.calls == []
    assert raw.record_requests == []


def test_other_input_kind_at_game_audio_name_is_preserved(config):
    client, raw = make_client(config, existing=True)
    raw.inputs[GAME_NAME]["kind"] = "wasapi_output_capture"
    before = deepcopy((raw.inputs, raw.scene_items, raw.current_scene))

    with pytest.raises(recordtest.RecorderError):
        client._ensure_game_audio_capture()

    assert (raw.inputs, raw.scene_items, raw.current_scene) == before


def test_unsupported_game_audio_kind_does_not_create_or_modify_inputs(config):
    client, raw = make_client(config)
    raw.supported_kinds.remove(GAME_KIND)
    before = deepcopy((raw.inputs, raw.scene_items, raw.current_scene))

    with pytest.raises(recordtest.RecorderError):
        client._ensure_game_audio_capture()

    assert (raw.inputs, raw.scene_items, raw.current_scene) == before


@pytest.mark.parametrize("operation,existing,linked", [
    ("get_input_list", False, False),
    ("get_input_kind_list", False, False),
    ("create_input", False, False),
    ("set_input_settings", True, True),
    ("get_scene_item_list", True, True),
    ("create_scene_item", True, False),
    ("set_scene_item_enabled", True, True),
    ("set_input_mute", True, True),
    ("set_input_volume", True, True),
    ("set_input_audio_monitor_type", True, True),
    ("set_input_audio_tracks", True, True),
    ("set_current_program_scene", True, True),
])
@pytest.mark.parametrize("record_method", ["start_recording", "toggle_recording"])
def test_audio_api_failure_prevents_raw_record_request(config, operation, existing, linked, record_method):
    client, raw = make_client(config, existing=existing, linked=linked)
    microphone = deepcopy(raw.inputs[config.audio.mic.input_name])
    raw.fail_at = operation

    with pytest.raises(recordtest.RecorderError, match=f"failed {operation}"):
        getattr(client, record_method)()

    assert raw.record_requests == []
    assert not any(call[0] == "send" for call in raw.calls)
    assert raw.inputs[config.audio.mic.input_name] == microphone


@pytest.mark.parametrize("record_method,request_name", [
    ("start_recording", "StartRecord"), ("toggle_recording", "ToggleRecord"),
])
def test_raw_record_request_observes_restored_audio_and_selected_scene(config, record_method, request_name):
    client, raw = make_client(config, existing=True)

    getattr(client, record_method)()

    assert len(raw.record_requests) == 1
    request, audio_at_request, scene_at_request = raw.record_requests[0]
    assert request == request_name
    assert_audio_ready(config, audio_at_request)
    assert scene_at_request == config.obs.scene_name
    assert raw.calls[-1] == ("send", request_name, {}, True)


@pytest.mark.parametrize("operation,field", [
    ("get_input_list", "inputs"),
    ("get_scene_item_list", "scene_items"),
])
@pytest.mark.parametrize("invalid", [None, {}])
def test_malformed_inventory_does_not_allow_recording(config, monkeypatch, operation, field, invalid):
    client, raw = make_client(config, existing=True)
    monkeypatch.setattr(raw, operation, lambda *args: SimpleNamespace(**{field: invalid}))

    with pytest.raises(recordtest.RecorderError):
        client.start_recording()

    assert raw.record_requests == []


@pytest.mark.parametrize("existing", [False, True])
def test_disabled_microphone_profile_never_uses_disabled_as_wasapi_device(config, existing):
    config = replace(config, audio=replace(config.audio, mic=replace(
        config.audio.mic, device_id="disabled", mute=False,
    )))
    raw = GameAudioClient(config, existing=True)
    name = config.audio.mic.input_name
    if existing:
        raw._add_scene_item(config.obs.scene_name, name, True)
        raw._add_scene_item("other-scene", name, True)
    else:
        del raw.inputs[name]
    game_before = deepcopy(raw.inputs[GAME_NAME])

    recordtest.apply_audio_profile_from_config(raw, config)
    recordtest.apply_audio_profile_from_config(raw, config)

    mic = raw.inputs[name]
    assert mic["settings"]["device_id"] == ("test-microphone" if existing else "default")
    assert mic["mute"] is False
    mic_items = [item for item in raw.scene_items[config.obs.scene_name] if item["sourceName"] == name]
    assert len(mic_items) == 1
    assert mic_items[0]["sceneItemEnabled"] is False
    assert raw.inputs[GAME_NAME] == game_before
    assert all(call[1] == name for call in raw.calls if call[0].startswith("set_input_"))
    assert not [call for call in raw.calls if call[0] == "set_input_settings"]
    if existing:
        assert raw.scene_items["other-scene"][0]["sceneItemEnabled"] is True


def test_microphone_disable_then_device_restore_keeps_game_and_mute_independent(config):
    raw = GameAudioClient(config, existing=True)
    name = config.audio.mic.input_name
    item_id = raw._add_scene_item(config.obs.scene_name, name, True)
    game_before = deepcopy(raw.inputs[GAME_NAME])
    for device in ("disabled", "another-device", "default"):
        raw.calls.clear()
        recordtest.apply_audio_input_settings(
            raw, name, device_id=device, volume_db=-3, mute=False, scene_name=config.obs.scene_name,
        )
        mutations = [call for call in raw.calls if call[0].startswith("set_")]
        if device == "disabled":
            assert mutations[0] == ("set_scene_item_enabled", config.obs.scene_name, item_id, False)
            assert raw.inputs[name]["settings"]["device_id"] == "test-microphone"
        else:
            assert mutations[-1] == ("set_scene_item_enabled", config.obs.scene_name, item_id, True)
            assert raw.inputs[name]["settings"]["device_id"] == device
        assert raw.inputs[name]["mute"] is False
        assert raw.inputs[GAME_NAME] == game_before


@pytest.mark.parametrize("failure", ["wrong_kind", "missing_item", "duplicate_item", "invalid_item", "input_query", "scene_query"])
def test_microphone_selection_rejects_unknown_target_before_mutation(config, failure):
    raw = GameAudioClient(config, existing=True)
    name = config.audio.mic.input_name
    raw._add_scene_item(config.obs.scene_name, name, True)
    if failure == "wrong_kind":
        raw.inputs[name]["kind"] = "wasapi_process_output_capture"
    elif failure == "missing_item":
        raw.scene_items[config.obs.scene_name] = []
    elif failure == "duplicate_item":
        raw._add_scene_item(config.obs.scene_name, name, True)
    elif failure == "invalid_item":
        raw.scene_items[config.obs.scene_name][-1]["sceneItemId"] = True
    elif failure == "input_query":
        raw.fail_at = "get_input_list"
    else:
        raw.fail_at = "get_scene_item_list"
    original_inputs = deepcopy(raw.inputs)
    original_scenes = deepcopy(raw.scene_items)
    with pytest.raises((recordtest.RecorderError, RuntimeError)):
        recordtest.apply_audio_input_settings(
            raw, name, device_id="disabled", volume_db=0, mute=False, scene_name=config.obs.scene_name,
        )
    assert raw.inputs == original_inputs
    assert raw.scene_items == original_scenes
    assert all(call[0].startswith("get_") for call in raw.calls)


@pytest.mark.parametrize("failure", ["wrong_kind", "input_query"])
def test_disabled_microphone_setup_does_not_replace_unverified_input(config, failure):
    raw = GameAudioClient(config, existing=True)
    name = config.audio.mic.input_name
    config = replace(config, audio=replace(config.audio, mic=replace(config.audio.mic, device_id="disabled")))
    if failure == "wrong_kind":
        raw.inputs[name]["kind"] = "wasapi_process_output_capture"
    else:
        raw.fail_at = "get_input_list"
    with pytest.raises((recordtest.RecorderError, RuntimeError)):
        recordtest.apply_audio_profile_from_config(raw, config)
    assert all(call[0].startswith("get_") for call in raw.calls)


def test_audio_input_settings_without_scene_preserves_normal_api_and_rejects_disabled(config):
    raw = GameAudioClient(config)
    name = config.audio.mic.input_name
    recordtest.apply_audio_input_settings(raw, name, "another-device", -2, True)
    assert raw.inputs[name]["settings"]["device_id"] == "another-device"
    assert raw.inputs[name]["mute"] is True
    assert [call[0] for call in raw.calls] == ["set_input_settings", "set_input_volume", "set_input_mute"]
    raw.calls.clear()
    with pytest.raises(recordtest.RecorderError):
        recordtest.apply_audio_input_settings(raw, name, "disabled", 0, False)
    assert raw.calls == []


@pytest.mark.parametrize("failed_call", ["set_input_settings", "set_input_volume", "set_input_mute"])
def test_microphone_restore_failure_keeps_scene_item_disabled(config, failed_call):
    raw = GameAudioClient(config, existing=True)
    name = config.audio.mic.input_name
    item_id = raw._add_scene_item(config.obs.scene_name, name, False)
    raw.fail_at = failed_call
    with pytest.raises(RuntimeError, match=f"failed {failed_call}"):
        recordtest.apply_audio_input_settings(
            raw, name, device_id="another-device", volume_db=-3, mute=False, scene_name=config.obs.scene_name,
        )
    mic_item = next(item for item in raw.scene_items[config.obs.scene_name] if item["sceneItemId"] == item_id)
    assert mic_item["sceneItemEnabled"] is False
    assert not [call for call in raw.calls if call[0] == "set_scene_item_enabled"]
