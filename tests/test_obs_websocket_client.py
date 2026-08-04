import inspect
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import obs_websocket_client, recordtest


def app_config():
    return recordtest.AppConfig.from_dict({})


def test_recordtest_reexports_the_split_client_with_same_identity() -> None:
    assert recordtest.ObsWebSocketClient is obs_websocket_client.ObsWebSocketClient
    assert recordtest.OBSClient is obs_websocket_client.OBSClient
    assert recordtest.OBSRecordingEncoderSelection is obs_websocket_client.OBSRecordingEncoderSelection
    assert recordtest._obs_raw is obs_websocket_client._obs_raw
    assert issubclass(recordtest.ObsWebSocketClient, recordtest.OBSClient)


def test_split_client_keeps_app_config_annotations() -> None:
    assert inspect.signature(obs_websocket_client.ObsWebSocketClient).parameters["config"].annotation == (
        "AppConfig | None"
    )
    assert inspect.signature(obs_websocket_client.OBSClient.apply_audio_profile).parameters[
        "cfg"
    ].annotation == "AppConfig"
    assert inspect.signature(obs_websocket_client.OBSClient.get_audio_device_catalog).parameters[
        "cfg"
    ].annotation == "AppConfig | None"
    assert inspect.signature(obs_websocket_client.ObsWebSocketClient.apply_audio_profile).parameters[
        "cfg"
    ].annotation == "AppConfig"


@pytest.mark.parametrize(
    ("working_directory", "module_import", "facade_import"),
    [
        (".", "from src import obs_websocket_client as module", "import src.recordtest as facade"),
        ("src", "import obs_websocket_client as module", "import recordtest as facade"),
    ],
)
def test_split_client_resolves_facade_in_real_import_modes(
    tmp_path,
    working_directory,
    module_import,
    facade_import,
) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["LOL_REPLAY_TOOL_DATA_DIR"] = str(tmp_path / "data")
    probe = "\n".join(
        (
            "from types import SimpleNamespace",
            module_import,
            "client = module.ObsWebSocketClient(config=SimpleNamespace())",
            facade_import,
            "assert facade.ObsWebSocketClient is module.ObsWebSocketClient",
            "assert module._recordtest_module() is facade",
            "assert isinstance(client, module.OBSClient)",
        )
    )

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repository_root / working_directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_connect_obs_client_falls_back_after_timeout(monkeypatch) -> None:
    calls = []
    raw_client = object()

    def construct_client(**kwargs):
        calls.append(kwargs)
        if kwargs["host"] == "localhost":
            raise TimeoutError("timed out")
        return raw_client

    monkeypatch.setattr(recordtest.obs, "ReqClient", construct_client)

    connected, used_host = obs_websocket_client.connect_obs_client(
        "localhost",
        4455,
        "secret",
        timeout=1.25,
    )

    assert connected is raw_client
    assert used_host == "127.0.0.1"
    assert [call["host"] for call in calls] == ["localhost", "127.0.0.1"]
    assert all(call["timeout"] == 1.25 for call in calls)


def test_connect_obs_client_reports_authentication_failure(monkeypatch) -> None:
    calls = []

    def reject_client(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("authentication failed")

    monkeypatch.setattr(recordtest.obs, "ReqClient", reject_client)

    with pytest.raises(recordtest.RecorderError, match="認証に失敗"):
        obs_websocket_client.connect_obs_client("localhost", 4455, "wrong")

    assert [call["host"] for call in calls] == ["localhost"]


def test_client_connect_retries_through_recordtest_monkeypatch(monkeypatch) -> None:
    attempts = []
    sleeps = []
    raw_client = SimpleNamespace(get_version=lambda: SimpleNamespace(obs_version="31.0.0"))

    def connect_client(*args):
        attempts.append(args)
        if len(attempts) < 3:
            raise TimeoutError("timed out")
        return raw_client, "127.0.0.1"

    monkeypatch.setattr(recordtest, "connect_obs_client", connect_client)
    monkeypatch.setattr(obs_websocket_client.time, "sleep", sleeps.append)
    client = obs_websocket_client.ObsWebSocketClient(
        config=app_config(),
        max_retries=3,
        retry_delay=0.25,
    )

    client.connect()

    assert client.raw_client is raw_client
    assert len(attempts) == 3
    assert sleeps == [0.25, 0.25]


class SceneClient:
    def __init__(self) -> None:
        self.scenes = [{"sceneName": "Scene"}]
        self.inputs = [
            {
                "inputName": recordtest.DEFAULT_OBS_GAME_CAPTURE_NAME,
                "inputKind": "game_capture",
            }
        ]
        self.scene_items = {recordtest.DEFAULT_OBS_SCENE_NAME: [], "Scene": []}
        self.created_inputs = []
        self.removed_inputs = []
        self.removed_scenes = []
        self.enabled_calls = []
        self.next_id = 10

    def get_scene_list(self):
        return SimpleNamespace(scenes=self.scenes)

    def create_scene(self, scene_name):
        self.scenes.append({"sceneName": scene_name})
        self.scene_items.setdefault(scene_name, [])

    def set_current_program_scene(self, scene_name):
        self.current_scene = scene_name

    def remove_scene(self, scene_name):
        self.removed_scenes.append(scene_name)
        self.scenes = [item for item in self.scenes if item.get("sceneName") != scene_name]

    def get_input_list(self):
        return SimpleNamespace(inputs=self.inputs)

    def create_input(self, scene_name, input_name, input_kind, settings, enabled):
        self.inputs.append({"inputName": input_name, "inputKind": input_kind})
        self.created_inputs.append((scene_name, input_name, input_kind, dict(settings), enabled))
        self.scene_items.setdefault(scene_name, []).append(
            {"sourceName": input_name, "sceneItemId": self.next_id}
        )
        self.next_id += 1

    def remove_input(self, input_name):
        self.removed_inputs.append(input_name)
        self.inputs = [item for item in self.inputs if item.get("inputName") != input_name]

    def get_scene_item_list(self, scene_name):
        return SimpleNamespace(scene_items=self.scene_items.setdefault(scene_name, []))

    def create_scene_item(self, scene_name, source_name, enabled):
        self.scene_items.setdefault(scene_name, []).append(
            {"sourceName": source_name, "sceneItemId": self.next_id}
        )
        self.next_id += 1

    def set_input_settings(self, input_name, settings, overlay=True):
        return None

    def set_scene_item_transform(self, scene_name, item_id, transform):
        return None

    def set_scene_item_index(self, scene_name, item_id, index):
        return None

    def set_scene_item_enabled(self, scene_name, item_id, enabled):
        self.enabled_calls.append((scene_name, item_id, enabled))


def test_setup_sync_elements_handles_scene_and_input_crud() -> None:
    raw_client = SceneClient()
    client = obs_websocket_client.ObsWebSocketClient(config=app_config())
    client.client = raw_client

    client.setup_sync_elements()

    created_kinds = [item[2] for item in raw_client.created_inputs]
    assert created_kinds == ["window_capture", "color_source_v3"]
    assert raw_client.current_scene == recordtest.DEFAULT_OBS_SCENE_NAME
    assert raw_client.removed_inputs == [recordtest.DEFAULT_OBS_GAME_CAPTURE_NAME]
    assert raw_client.removed_scenes == ["Scene"]
    assert raw_client.enabled_calls[-1][2] is False


def test_audio_operations_keep_recordtest_monkeypatch_boundary(monkeypatch) -> None:
    raw_client = object()
    calls = []
    config = app_config()

    monkeypatch.setattr(
        recordtest,
        "apply_audio_profile_from_config",
        lambda *args, **kwargs: calls.append(("apply", args, kwargs)) or True,
    )
    monkeypatch.setattr(
        recordtest,
        "get_audio_device_catalog",
        lambda *args, **kwargs: calls.append(("catalog", args, kwargs)) or {"mic": []},
    )
    client = obs_websocket_client.ObsWebSocketClient(config=config)
    client.client = raw_client

    assert client.apply_audio_profile(config) is True
    assert client.get_audio_device_catalog(config) == {"mic": []}
    assert [call[0] for call in calls] == ["apply", "catalog"]
    assert all(call[1][0] is raw_client for call in calls)


class RecordingClient:
    def __init__(self) -> None:
        self.requests = []
        self.marker_calls = []

    def send(self, request_type, payload, raw=True):
        self.requests.append((request_type, dict(payload), raw))
        if request_type == "GetProfileParameter":
            return {"parameterValue": ""}
        return {}

    def stop_record(self):
        return SimpleNamespace(output_path="C:/recordings/game.mkv")

    def get_record_status(self):
        return SimpleNamespace(
            output_active=True,
            output_paused=False,
            output_timecode="00:00:01.000",
            output_duration=1000,
            output_bytes=2048,
        )

    def get_scene_item_list(self, scene_name):
        return SimpleNamespace(
            scene_items=[
                {
                    "sourceName": recordtest.DEFAULT_OBS_SOURCE_NAME,
                    "sceneItemId": 77,
                }
            ]
        )

    def set_scene_item_enabled(self, scene_name, item_id, enabled):
        self.marker_calls.append((scene_name, item_id, enabled))


def test_recording_status_and_sync_marker_requests() -> None:
    raw_client = RecordingClient()
    client = obs_websocket_client.ObsWebSocketClient(config=app_config())
    client.client = raw_client

    client.start_recording()
    assert client.stop_recording() == "C:/recordings/game.mkv"
    assert client.is_recording_active() is True
    details = client.get_record_status_details()
    client.set_sync_marker_enabled(True)

    assert raw_client.requests[0] == ("StartRecord", {}, True)
    assert details["output_timecode"] == "00:00:01.000"
    assert details["output_bytes"] == 2048
    assert raw_client.marker_calls == [(recordtest.DEFAULT_OBS_SCENE_NAME, 77, True)]


def test_raw_request_falls_back_for_legacy_send_signature() -> None:
    calls = []

    class LegacyRawClient:
        def send(self, request_type, payload):
            calls.append((request_type, payload))
            return {"ok": True}

    response = obs_websocket_client._obs_raw(LegacyRawClient(), "GetVersion")

    assert response == {"ok": True}
    assert calls == [("GetVersion", {})]
