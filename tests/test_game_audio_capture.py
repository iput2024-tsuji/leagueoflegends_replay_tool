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
