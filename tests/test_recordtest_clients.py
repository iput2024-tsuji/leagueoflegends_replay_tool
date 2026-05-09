import asyncio
import json
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest

from src import recordtest


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    async def json(self, content_type=None):
        return self.payload

    async def text(self):
        return str(self.payload)


class FakeRequestContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, routes=None, error=None, timeout=None):
        self.routes = routes or {}
        self.error = error
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, ssl=False):
        if self.error:
            raise self.error
        return FakeRequestContext(FakeResponse(self.routes.get(url)))


class FakeSessionFactory:
    def __init__(self, routes=None, error=None):
        self.routes = routes or {}
        self.error = error

    def __call__(self, timeout=None):
        return FakeSession(routes=self.routes, error=self.error, timeout=timeout)


def run(coro):
    return asyncio.run(coro)


def app_config():
    return recordtest.AppConfig.from_dict({})


def test_app_config_is_immutable():
    config = app_config()

    with pytest.raises(FrozenInstanceError):
        config.obs.port = 1234


def test_riot_api_fetches_and_parses_live_client_payloads():
    routes = {
        recordtest.ACTIVE_PLAYER_URL: "Summoner#JP1",
        recordtest.EVENT_URL: {"Events": [{"EventName": "GameStart", "EventTime": 1.5}]},
        recordtest.ALL_GAME_URL: {"gameData": {"gameTime": 42.0}, "allPlayers": []},
    }
    client = recordtest.LiveClientRiotAPIClient(session_factory=FakeSessionFactory(routes))

    assert run(client.get_active_player_name()) == "Summoner#JP1"
    assert run(client.get_event_data())["Events"][0]["EventName"] == "GameStart"
    assert run(client.get_all_game_data())["gameData"]["gameTime"] == 42.0


def test_riot_api_returns_none_when_lcu_server_is_down():
    client = recordtest.LiveClientRiotAPIClient(
        session_factory=FakeSessionFactory(error=aiohttp.ClientConnectionError("down"))
    )

    assert run(client.get_active_player_name()) is None
    assert run(client.get_event_data()) is None
    assert run(client.get_all_game_data()) is None


def test_obs_websocket_timeout_is_wrapped_as_recorder_error():
    client = recordtest.ObsWebSocketClient(config=app_config(), max_retries=2, retry_delay=0)

    with patch("src.recordtest.connect_obs_client", side_effect=TimeoutError("timed out")):
        with pytest.raises(recordtest.RecorderError) as exc:
            client.connect()

    assert "OBS WebSocket" in str(exc.value)
    assert "timed out" in str(exc.value)


def test_obs_disconnect_clears_raw_client_even_when_socket_errors():
    class BrokenRawClient:
        def disconnect(self):
            raise TimeoutError("socket already closed")

    client = recordtest.ObsWebSocketClient(config=app_config())
    client.client = BrokenRawClient()

    with pytest.raises(TimeoutError):
        client.disconnect()

    assert client.raw_client is None


def test_setup_sync_elements_creates_game_capture_below_sync_marker():
    class FakeObsRawClient:
        def __init__(self):
            self.scenes = [{"sceneName": recordtest.DEFAULT_OBS_SCENE_NAME}]
            self.inputs = []
            self.scene_items = []
            self.created_inputs = []
            self.index_calls = []
            self.next_item_id = 1

        def get_scene_list(self):
            return SimpleNamespace(scenes=self.scenes)

        def get_input_list(self):
            return SimpleNamespace(inputs=self.inputs)

        def create_input(self, scene_name, input_name, input_kind, input_settings, scene_item_enabled):
            self.inputs.append({"inputName": input_name, "inputKind": input_kind})
            self.created_inputs.append(
                {
                    "scene": scene_name,
                    "name": input_name,
                    "kind": input_kind,
                    "settings": dict(input_settings),
                    "enabled": scene_item_enabled,
                }
            )
            self.scene_items.append(
                {"sourceName": input_name, "sceneItemId": self.next_item_id, "sceneItemIndex": len(self.scene_items)}
            )
            self.next_item_id += 1

        def create_scene_item(self, scene_name, source_name, enabled=None):
            self.scene_items.append(
                {"sourceName": source_name, "sceneItemId": self.next_item_id, "sceneItemIndex": len(self.scene_items)}
            )
            self.next_item_id += 1

        def get_scene_item_list(self, scene_name):
            return SimpleNamespace(scene_items=self.scene_items)

        def set_scene_item_index(self, scene_name, item_id, item_index):
            self.index_calls.append((scene_name, item_id, item_index))
            for item in self.scene_items:
                if item["sceneItemId"] == item_id:
                    item["sceneItemIndex"] = item_index

        def set_input_settings(self, input_name, settings, overlay=True):
            return None

        def set_scene_item_transform(self, scene_name, item_id, transform):
            return None

        def set_scene_item_enabled(self, scene_name, item_id, enabled):
            return None

    raw_client = FakeObsRawClient()
    client = recordtest.ObsWebSocketClient(config=app_config())
    client.client = raw_client

    client.setup_sync_elements()

    game_capture = raw_client.created_inputs[0]
    sync_marker = raw_client.created_inputs[1]
    assert game_capture["kind"] == "game_capture"
    assert game_capture["settings"]["capture_mode"] == "window"
    assert game_capture["settings"]["window"] == recordtest.DEFAULT_OBS_GAME_CAPTURE_WINDOW
    assert sync_marker["name"] == recordtest.DEFAULT_OBS_SOURCE_NAME

    game_item_id = raw_client.scene_items[0]["sceneItemId"]
    sync_item_id = raw_client.scene_items[1]["sceneItemId"]
    assert (recordtest.DEFAULT_OBS_SCENE_NAME, game_item_id, 0) in raw_client.index_calls
    assert (recordtest.DEFAULT_OBS_SCENE_NAME, sync_item_id, 1) in raw_client.index_calls


def test_obs_bootstrapper_creates_portable_marker_and_tray_disabled_global_ini(monkeypatch):
    obs_dir = Path("tests") / "_tmp" / "obs_bootstrapper" / "obs-portable"
    shutil.rmtree(obs_dir.parent, ignore_errors=True)
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir.resolve())
    result = recordtest.OBSBootstrapper(obs_dir).bootstrap()
    global_ini = result["global_ini_path"]
    config_dir = result["config_dir"]

    assert (obs_dir / "obs_portable_mode.txt").exists()
    assert (obs_dir / "portable_mode.txt").exists()
    assert config_dir == (obs_dir / "config" / "obs-studio").resolve()
    assert config_dir.exists()
    assert global_ini.exists()

    text = global_ini.read_text(encoding="utf-8")
    assert "[BasicWindow]" in text
    assert "SysTrayEnabled=false" in text
    assert "SysTrayWhenStarted" not in text
    assert "SysTrayMinimizeToTray" not in text
    assert "HideTrayIcon" not in text


def test_global_ini_removes_bom_and_nonstandard_tray_keys(monkeypatch):
    obs_dir = Path("tests") / "_tmp" / "obs_bootstrapper_bom" / "obs-portable"
    shutil.rmtree(obs_dir.parent, ignore_errors=True)
    ini_path = obs_dir / "config" / "obs-studio" / "global.ini"
    ini_path.parent.mkdir(parents=True, exist_ok=True)
    ini_path.write_text(
        "\ufeff[General]\nSysTrayEnabled=true\n\n"
        "[BasicWindow]\n"
        "SysTrayEnabled=true\n"
        "SysTrayWhenStarted=false\n"
        "SysTrayMinimizeToTray=false\n"
        "HideTrayIcon=true\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir.resolve())

    changed, _result_path = recordtest.ensure_portable_obs_global_ini(obs_dir)

    assert changed is True
    raw = ini_path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert "[BasicWindow]" in text
    assert "SysTrayEnabled=false" in text
    assert "SysTrayWhenStarted" not in text
    assert "SysTrayMinimizeToTray" not in text
    assert "HideTrayIcon" not in text


def test_global_ini_parse_error_deletes_and_regenerates_before_patch(monkeypatch):
    obs_dir = Path("tests") / "_tmp" / "obs_bootstrapper_corrupt" / "obs-portable"
    shutil.rmtree(obs_dir.parent, ignore_errors=True)
    ini_path = obs_dir / "config" / "obs-studio" / "global.ini"
    ini_path.parent.mkdir(parents=True, exist_ok=True)
    ini_path.write_text("[BasicWindow\nbroken", encoding="utf-8")

    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir.resolve())

    def regenerate(_self, target_ini, timeout_sec=8.0):
        assert not target_ini.exists()
        target_ini.write_text("[General]\nExisting=true\n\n[Other]\nKeep=true\n", encoding="utf-8")

    monkeypatch.setattr(recordtest.OBSBootstrapper, "regenerate_global_ini_with_obs", regenerate)

    changed, result_path = recordtest.ensure_portable_obs_global_ini(obs_dir)

    assert changed is True
    assert result_path == ini_path.resolve()
    text = ini_path.read_text(encoding="utf-8")
    assert "Existing=true" in text
    assert "[Other]" in text
    assert "Keep=true" in text
    assert "SysTrayEnabled=false" in text


def test_storage_limit_only_deletes_json_referenced_app_video():
    root = Path("tests") / "_tmp" / "storage_limit_scope"
    shutil.rmtree(root, ignore_errors=True)
    recordings_dir = root / "recordings"
    json_dir = recordings_dir / "json"
    json_dir.mkdir(parents=True, exist_ok=True)

    owned_video = recordings_dir / "owned.mp4"
    unrelated_video = recordings_dir / "unrelated.mp4"
    owned_video.write_bytes(b"owned video")
    unrelated_video.write_bytes(b"unrelated video that must remain")
    json_path = json_dir / "lol_20260101_000000.json"
    json_path.write_text(
        json.dumps(
            {
                "saved_at": "2026-01-01 00:00:00",
                "obs_record_path": owned_video.name,
            }
        ),
        encoding="utf-8",
    )

    config = recordtest.AppConfig.from_dict(
        {
            "paths": {
                "recordings_dir": str(recordings_dir),
                "json_dir": str(json_dir),
            },
            "storage": {"max_size_bytes": 1},
        }
    )

    recordtest.enforce_storage_limit(config)

    assert not owned_video.exists()
    assert not json_path.exists()
    assert unrelated_video.exists()
