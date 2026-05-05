import asyncio
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path
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


def test_obs_bootstrapper_creates_portable_marker_and_tray_disabled_global_ini(monkeypatch):
    obs_dir = Path("tests") / "_tmp" / "obs_bootstrapper" / "obs-portable"
    shutil.rmtree(obs_dir.parent, ignore_errors=True)
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir.resolve())

    result = recordtest.OBSBootstrapper(obs_dir).bootstrap()
    global_ini = result["global_ini_path"]

    assert (obs_dir / "obs_portable_mode.txt").exists()
    assert (obs_dir / "portable_mode.txt").exists()
    assert global_ini.exists()

    text = global_ini.read_text(encoding="utf-8")
    assert "SysTrayEnabled=false" in text
    assert "SysTrayWhenStarted=false" in text
    assert "SysTrayMinimizeToTray=false" in text
    assert "HideTrayIcon=true" in text
