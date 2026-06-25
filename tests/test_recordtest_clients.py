import asyncio
import configparser
import json
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest

from src import recordtest
from src.lcu_client import LCUConnectionInfo


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

    def get(self, url, ssl=False, **kwargs):
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


def test_app_config_generates_obs_password_when_missing():
    config = recordtest.AppConfig.from_dict({"obs": {"password": ""}})

    assert len(config.obs.password) >= 12


def test_app_config_reads_legacy_game_capture_settings_as_window_capture():
    config = recordtest.AppConfig.from_dict(
        {
            "obs": {
                "game_capture_name": "legacy_capture",
                "game_capture_window": "Legacy Window:LegacyClass:Legacy.exe",
            }
        }
    )

    assert config.obs.window_capture_name == "legacy_capture"
    assert config.obs.window_capture_window == "Legacy Window:LegacyClass:Legacy.exe"
    assert config.obs.window_capture_method == recordtest.DEFAULT_OBS_WINDOW_CAPTURE_METHOD


def test_app_config_upgrades_default_game_capture_values_to_window_capture_defaults():
    config = recordtest.AppConfig.from_dict(
        {
            "obs": {
                "window_capture_name": recordtest.DEFAULT_OBS_GAME_CAPTURE_NAME,
                "window_capture_window": recordtest.DEFAULT_OBS_LEGACY_GAME_CAPTURE_WINDOW,
            }
        }
    )

    assert config.obs.window_capture_name == recordtest.DEFAULT_OBS_WINDOW_CAPTURE_NAME
    assert config.obs.window_capture_window == recordtest.DEFAULT_OBS_WINDOW_CAPTURE_WINDOW


def test_app_config_uses_lossless_resolution_defaults_and_quality_recording():
    config = recordtest.AppConfig.from_dict({})

    assert config.obs.base_width == 1920
    assert config.obs.base_height == 1080
    assert config.obs.output_width == 1920
    assert config.obs.output_height == 1080
    assert config.obs.scale_type == "lanczos"
    assert config.obs.recording_quality == "Small"
    assert config.obs.recording_encoder == "auto"


def test_obs_video_and_quality_settings_are_sent_to_websocket():
    class RawClient:
        def __init__(self):
            self.calls = []

        def send(self, request_type, payload, raw=True):
            self.calls.append((request_type, payload, raw))
            return SimpleNamespace()

    client = RawClient()

    recordtest.apply_obs_video_settings(
        client,
        240000,
        fps_denominator=1001,
        base_width=1920,
        base_height=1080,
        output_width=1920,
        output_height=1080,
    )
    recordtest.apply_obs_recording_quality_settings(
        client,
        scale_type="lanczos",
        recording_quality="Small",
        recording_encoder="x264",
    )

    assert client.calls[0] == (
        "SetVideoSettings",
        {
            "fpsNumerator": 240000,
            "fpsDenominator": 1001,
            "baseWidth": 1920,
            "baseHeight": 1080,
            "outputWidth": 1920,
            "outputHeight": 1080,
        },
        True,
    )
    assert (
        "SetProfileParameter",
        {
            "parameterCategory": "Output",
            "parameterName": "Mode",
            "parameterValue": "Simple",
        },
        True,
    ) in client.calls
    assert (
        "SetProfileParameter",
        {
            "parameterCategory": "SimpleOutput",
            "parameterName": "RecFormat2",
            "parameterValue": "mkv",
        },
        True,
    ) in client.calls
    assert (
        "SetProfileParameter",
        {
            "parameterCategory": "AdvOut",
            "parameterName": "RecEncoder",
            "parameterValue": "obs_x264",
        },
        True,
    ) in client.calls
    assert client.calls[-1] == (
        "SetProfileParameter",
        {
            "parameterCategory": "SimpleOutput",
            "parameterName": "RecEncoder",
            "parameterValue": "x264",
        },
        True,
    )


def test_prepare_recording_start_does_not_reset_video_settings(tmp_path):
    class RawClient:
        def __init__(self):
            self.calls = []

        def send(self, request_type, payload, raw=True):
            self.calls.append((request_type, payload, raw))
            if request_type == "GetSpecialInputs":
                return {}
            return {}

        def set_record_directory(self, record_path):
            self.calls.append(("set_record_directory", record_path, None))

    config = recordtest.AppConfig.from_dict(
        {
            "obs": {
                "recording_encoder": "x264",
            },
            "paths": {
                "recordings_dir": str(tmp_path),
                "json_dir": str(tmp_path / "json"),
            }
        }
    )
    client = RawClient()
    obs_client = recordtest.ObsWebSocketClient(config=config)
    obs_client.client = client

    obs_client.prepare_recording_start()

    request_names = [call[0] for call in client.calls]
    assert "SetVideoSettings" not in request_names
    assert "set_record_directory" in request_names
    assert request_names.count("SetProfileParameter") >= 12
    assert (
        "SetProfileParameter",
        {
            "parameterCategory": "Output",
            "parameterName": "Mode",
            "parameterValue": "Simple",
        },
        True,
    ) in client.calls
    assert (
        "SetProfileParameter",
        {
            "parameterCategory": "SimpleOutput",
            "parameterName": "FilePath",
            "parameterValue": str(tmp_path),
        },
        True,
    ) in client.calls
    assert client.calls[-1][1] == {
        "parameterCategory": "SimpleOutput",
        "parameterName": "RecEncoder",
        "parameterValue": "x264",
    }


def test_recording_encoder_auto_selection_prefers_h264_hardware_encoders():
    kinds = [
        "obs_nvenc_hevc_tex",
        "ffmpeg_aom_av1",
        "obs_x264",
        "obs_nvenc_h264_tex",
    ]

    selected = recordtest.select_obs_recording_encoder(kinds)

    assert selected.profile_value == "nvenc"
    assert selected.encoder_kind == "obs_nvenc_h264_tex"
    assert selected.hardware is True


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        (["obs_qsv11_v2", "obs_x264"], "qsv"),
        (["h264_texture_amf", "obs_x264"], "amd"),
        (["obs_x264"], "x264"),
        (["obs_nvenc_hevc_tex", "ffmpeg_aom_av1"], "x264"),
    ],
)
def test_recording_encoder_auto_selection_uses_safe_fallback_order(kinds, expected):
    assert recordtest.select_obs_recording_encoder(kinds).profile_value == expected


def test_recording_quality_defaults_to_auto_gpu_when_hardware_encoder_exists(tmp_path):
    class RawClient:
        def __init__(self):
            self.calls = []

        def send(self, request_type, payload, raw=True):
            self.calls.append((request_type, payload, raw))
            return {}

    logs_dir = tmp_path / "config" / "obs-studio" / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "latest.txt").write_text(
        "Available Encoders:\n"
        "  - obs_x264 (x264)\n"
        "  - obs_nvenc_h264_tex (NVIDIA NVENC H.264)\n",
        encoding="utf-8",
    )
    client = RawClient()

    selected = recordtest.apply_obs_recording_quality_settings(client, obs_dir=tmp_path)

    assert selected.profile_value == "nvenc"
    assert selected.hardware is True
    assert client.calls[-1][1] == {
        "parameterCategory": "SimpleOutput",
        "parameterName": "RecEncoder",
        "parameterValue": "nvenc",
    }


def test_recording_quality_auto_detects_encoder_from_obs_log_when_requested(tmp_path):
    class RawClient:
        def __init__(self):
            self.calls = []

        def send(self, request_type, payload, raw=True):
            self.calls.append((request_type, payload, raw))
            return {}

    logs_dir = tmp_path / "config" / "obs-studio" / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "latest.txt").write_text(
        "Available Encoders:\n"
        "  - obs_x264 (x264)\n"
        "  - obs_nvenc_hevc_tex (NVIDIA NVENC HEVC)\n"
        "  - obs_nvenc_h264_tex (NVIDIA NVENC H.264)\n",
        encoding="utf-8",
    )
    client = RawClient()

    selected = recordtest.apply_obs_recording_quality_settings(
        client,
        recording_encoder="auto",
        obs_dir=tmp_path,
    )

    assert selected.profile_value == "nvenc"
    assert client.calls[-1][1] == {
        "parameterCategory": "SimpleOutput",
        "parameterName": "RecEncoder",
        "parameterValue": "nvenc",
    }


def test_recording_quality_falls_back_to_x264_without_obs_log(tmp_path):
    class RawClient:
        def send(self, request_type, payload, raw=True):
            return {}

    selected = recordtest.apply_obs_recording_quality_settings(RawClient(), obs_dir=tmp_path)

    assert selected.profile_value == "x264"
    assert selected.hardware is False


def test_recording_profile_ini_repairs_existing_advanced_output(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    profile_dir = obs_dir / "config" / "obs-studio" / "basic" / "profiles" / "bad_profile"
    profile_dir.mkdir(parents=True)
    basic_ini = profile_dir / "basic.ini"
    basic_ini.write_text(
        "[General]\n"
        "Name=bad_profile\n\n"
        "[Output]\n"
        "Mode=Advanced\n\n"
        "[SimpleOutput]\n"
        "FilePath=F:\\old\n"
        "RecFormat2=hybrid_mp4\n"
        "UseAdvanced=true\n"
        "RecEncoder=nvenc\n\n"
        "[AdvOut]\n"
        "RecType=FFmpeg\n"
        "RecFilePath=F:\\old\n"
        "RecFormat2=hybrid_mp4\n"
        "RecEncoder=obs_nvenc_h264_tex\n",
        encoding="utf-8",
    )
    user_ini = obs_dir / "config" / "obs-studio" / "user.ini"
    user_ini.write_text("[Basic]\nProfile=bad_profile\nProfileDir=bad_profile\n", encoding="utf-8")
    record_dir = tmp_path / "recordings"

    changed = recordtest.ensure_obs_recording_profile_ini(obs_dir, record_dir=record_dir)

    assert basic_ini.resolve() in changed
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(basic_ini, encoding="utf-8")
    assert parser.get("Output", "Mode") == "Simple"
    assert parser.get("SimpleOutput", "FilePath") == str(record_dir)
    assert parser.get("SimpleOutput", "RecFormat2") == "mkv"
    assert parser.get("SimpleOutput", "UseAdvanced") == "false"
    assert parser.get("SimpleOutput", "RecEncoder") == "x264"
    assert parser.get("AdvOut", "RecType") == "Standard"
    assert parser.get("AdvOut", "RecFilePath") == str(record_dir)
    assert parser.get("AdvOut", "RecFormat2") == "mkv"
    assert parser.get("AdvOut", "RecEncoder") == "obs_x264"
    assert "Profile=LoLReplayTool" in user_ini.read_text(encoding="utf-8")
    assert "ProfileDir=LoLReplayTool" in user_ini.read_text(encoding="utf-8")


def test_recording_profile_ini_forces_managed_profile_over_obs_generated_name(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    profiles_root = obs_dir / "config" / "obs-studio" / "basic" / "profiles"
    legacy_profile = profiles_root / "LoL_Replay_Tool" / "basic.ini"
    legacy_profile.parent.mkdir(parents=True)
    legacy_profile.write_text(
        "[General]\n"
        "Name=LoL Replay Tool\n\n"
        "[Output]\n"
        "Mode=Advanced\n\n"
        "[SimpleOutput]\n"
        "FilePath=F:\\old\n"
        "RecFormat2=hybrid_mp4\n"
        "UseAdvanced=true\n"
        "RecEncoder=nvenc\n",
        encoding="utf-8",
    )
    user_ini = obs_dir / "config" / "obs-studio" / "user.ini"
    user_ini.parent.mkdir(parents=True, exist_ok=True)
    user_ini.write_text(
        "[Basic]\n"
        "Profile=LoL Replay Tool\n"
        "ProfileDir=LoL_Replay_Tool\n"
        "SceneCollection=無題\n",
        encoding="utf-8",
    )
    record_dir = tmp_path / "recordings"

    changed = recordtest.ensure_obs_recording_profile_ini(obs_dir, record_dir=record_dir)

    managed_profile = profiles_root / recordtest.MANAGED_OBS_PROFILE_DIR_NAME / "basic.ini"
    assert managed_profile.resolve() in changed
    assert legacy_profile.resolve() in changed
    assert user_ini.resolve() in changed
    assert managed_profile.exists()
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(user_ini, encoding="utf-8")
    assert parser.get("Basic", "Profile") == "LoLReplayTool"
    assert parser.get("Basic", "ProfileDir") == "LoLReplayTool"
    parser.read(managed_profile, encoding="utf-8")
    assert parser.get("General", "Name") == "LoLReplayTool"
    assert parser.get("Output", "Mode") == "Simple"
    assert parser.get("SimpleOutput", "RecEncoder") == "x264"


def test_recording_profile_ini_creates_managed_profile_when_missing(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    record_dir = tmp_path / "recordings"

    changed = recordtest.ensure_obs_recording_profile_ini(obs_dir, record_dir=record_dir)

    profile_ini = (
        obs_dir
        / "config"
        / "obs-studio"
        / "basic"
        / "profiles"
        / recordtest.MANAGED_OBS_PROFILE_DIR_NAME
        / "basic.ini"
    )
    user_ini = obs_dir / "config" / "obs-studio" / "user.ini"
    assert profile_ini.resolve() in changed
    assert user_ini.resolve() in changed
    assert profile_ini.exists()
    assert user_ini.exists()
    assert "Profile=LoLReplayTool" in user_ini.read_text(encoding="utf-8")
    assert "ProfileDir=LoLReplayTool" in user_ini.read_text(encoding="utf-8")
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(profile_ini, encoding="utf-8")
    assert parser.get("General", "Name") == "LoLReplayTool"


def test_record_status_details_include_obs_profile_and_output_diagnostics():
    class RawClient:
        def get_record_status(self):
            return SimpleNamespace(
                output_active=False,
                output_paused=False,
                output_timecode="00:00:00.000",
                output_duration=0,
                output_bytes=0,
            )

        def send(self, request_type, payload, raw=True):
            if request_type == "GetProfileParameter":
                key = (payload["parameterCategory"], payload["parameterName"])
                values = {
                    ("Output", "Mode"): "Simple",
                    ("SimpleOutput", "RecEncoder"): "x264",
                    ("AdvOut", "RecEncoder"): "obs_x264",
                }
                return {"parameterValue": values.get(key, "")}
            if request_type == "GetProfileList":
                return {"currentProfileName": "LoLReplayTool", "profiles": ["LoLReplayTool"]}
            if request_type == "GetSceneCollectionList":
                return {"currentSceneCollectionName": "LoLReplayTool", "sceneCollections": ["LoLReplayTool"]}
            if request_type == "GetOutputList":
                return {
                    "outputs": [
                        {
                            "outputName": "simple_file_output",
                            "outputKind": "mp4_output",
                            "outputActive": False,
                        }
                    ]
                }
            if request_type == "GetOutputStatus":
                assert payload == {"outputName": "simple_file_output"}
                return {"outputActive": False, "outputBytes": 0, "outputDuration": 0}
            if request_type == "GetOutputSettings":
                assert payload == {"outputName": "simple_file_output"}
                return {"outputSettings": {"path": "C:/recordings/game.mkv", "muxer_settings": ""}}
            raise AssertionError(request_type)

    obs_client = recordtest.ObsWebSocketClient(config=app_config())
    obs_client.client = RawClient()

    details = obs_client.get_record_status_details()

    assert details["OBS.current_profile"] == "LoLReplayTool"
    assert details["OBS.current_scene_collection"] == "LoLReplayTool"
    assert details["OBS.outputs"] == "simple_file_output(mp4_output, active=False)"
    assert details["simple_file_output.active"] is False
    assert details["simple_file_output.path"] == "C:/recordings/game.mkv"


def test_start_recording_raises_when_raw_obs_response_reports_failure():
    class RawClient:
        def send(self, request_type, payload, raw=True):
            assert request_type == "StartRecord"
            return {
                "requestStatus": {
                    "result": False,
                    "code": 500,
                    "comment": "Output start failed",
                }
            }

    obs_client = recordtest.ObsWebSocketClient(config=app_config())
    obs_client.client = RawClient()

    with pytest.raises(recordtest.RecorderError, match="Output start failed"):
        obs_client.start_recording()


def test_app_config_preserves_fractional_high_fps():
    config = recordtest.AppConfig.from_dict(
        {
            "obs": {
                "fps_numerator": 240000,
                "fps_denominator": 1001,
            }
        }
    )

    assert config.obs.fps_numerator == 240000
    assert config.obs.fps_denominator == 1001
    assert config.obs.fps == pytest.approx(239.7602397602)


def test_app_config_exposes_champion_aliases_path(tmp_path):
    config = recordtest.AppConfig.from_dict(
        {
            "paths": {
                "champion_aliases_path": str(tmp_path / "config" / "champion_aliases.json"),
            }
        }
    )

    assert config.paths.champion_aliases_path == (tmp_path / "config" / "champion_aliases.json").resolve()


def test_app_config_defaults_champion_aliases_path_to_user_data():
    config = recordtest.AppConfig.from_dict({})

    assert config.paths.champion_aliases_path == (
        recordtest.DATA_DIR / recordtest.DEFAULT_CHAMPION_ALIASES_PATH
    ).resolve()


def test_preflight_generates_and_persists_obs_password(monkeypatch, tmp_path):
    managed_dir = (tmp_path / "obs-portable").resolve()
    obs_exe = managed_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", (tmp_path / "legacy-root-obs").resolve())
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", (tmp_path / "legacy-bin-obs").resolve())
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", (tmp_path / "legacy-data-bin-obs").resolve())
    cfg = {
        "obs": {"password": "", "dir": str(managed_dir)},
        "paths": {
            "bin_dir": str(tmp_path / "bin"),
            "recordings_dir": str(tmp_path / "recordings"),
            "json_dir": str(tmp_path / "recordings" / "json"),
            "champion_icons_dir": str(tmp_path / "assets" / "champions" / "icons"),
        },
    }

    report = recordtest.run_preflight_checks(cfg, auto_fix=True, ensure_dirs=False)

    assert report["errors"] == []
    assert len(report["config"]["obs"]["password"]) >= 12
    assert any("WebSocketパスワード" in note for note in report["notes"])


def test_preflight_migrates_legacy_game_capture_keys_to_window_capture(monkeypatch, tmp_path):
    managed_dir = (tmp_path / "obs-portable").resolve()
    obs_exe = managed_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", (tmp_path / "legacy-root-obs").resolve())
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", (tmp_path / "legacy-bin-obs").resolve())
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", (tmp_path / "legacy-data-bin-obs").resolve())
    cfg = {
        "obs": {
            "password": "already-secret",
            "dir": str(managed_dir),
            "game_capture_name": "legacy_capture",
            "game_capture_window": "Legacy Window:LegacyClass:Legacy.exe",
        },
        "paths": {
            "bin_dir": str(tmp_path / "bin"),
            "recordings_dir": str(tmp_path / "recordings"),
            "json_dir": str(tmp_path / "recordings" / "json"),
            "champion_icons_dir": str(tmp_path / "assets" / "champions" / "icons"),
        },
    }

    report = recordtest.run_preflight_checks(cfg, auto_fix=True, ensure_dirs=False)

    obs = report["config"]["obs"]
    assert report["errors"] == []
    assert obs["window_capture_name"] == "legacy_capture"
    assert obs["window_capture_window"] == "Legacy Window:LegacyClass:Legacy.exe"
    assert obs["window_capture_method"] == recordtest.DEFAULT_OBS_WINDOW_CAPTURE_METHOD
    assert "game_capture_name" not in obs
    assert "game_capture_window" not in obs


def test_preflight_repairs_bad_default_window_capture_migration(monkeypatch, tmp_path):
    managed_dir = (tmp_path / "obs-portable").resolve()
    obs_exe = managed_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", (tmp_path / "legacy-root-obs").resolve())
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", (tmp_path / "legacy-bin-obs").resolve())
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", (tmp_path / "legacy-data-bin-obs").resolve())
    cfg = {
        "obs": {
            "password": "already-secret",
            "dir": str(managed_dir),
            "window_capture_name": recordtest.DEFAULT_OBS_GAME_CAPTURE_NAME,
            "window_capture_window": recordtest.DEFAULT_OBS_LEGACY_GAME_CAPTURE_WINDOW,
            "window_capture_method": recordtest.DEFAULT_OBS_WINDOW_CAPTURE_METHOD,
        },
        "paths": {
            "bin_dir": str(tmp_path / "bin"),
            "recordings_dir": str(tmp_path / "recordings"),
            "json_dir": str(tmp_path / "recordings" / "json"),
            "champion_icons_dir": str(tmp_path / "assets" / "champions" / "icons"),
        },
    }

    report = recordtest.run_preflight_checks(cfg, auto_fix=True, ensure_dirs=False)

    obs = report["config"]["obs"]
    assert report["errors"] == []
    assert obs["window_capture_name"] == recordtest.DEFAULT_OBS_WINDOW_CAPTURE_NAME
    assert obs["window_capture_window"] == recordtest.DEFAULT_OBS_WINDOW_CAPTURE_WINDOW


def test_preflight_rejects_missing_obs_password_without_auto_fix():
    cfg = {"obs": {"password": ""}}

    report = recordtest.run_preflight_checks(cfg, auto_fix=False, ensure_dirs=False)

    assert any("WebSocketパスワード" in error for error in report["errors"])


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


def test_riot_api_fetches_champ_select_and_champion_catalog_from_lcu():
    connection = LCUConnectionInfo(port=54321, password="secret")
    provider = SimpleNamespace(
        get_connection_info=lambda: connection,
        invalidate=lambda: None,
    )
    champ_select_url = f"{connection.base_url}{recordtest.LCU_CHAMP_SELECT_PATH}"
    champion_summary_url = f"{connection.base_url}{recordtest.LCU_CHAMPION_SUMMARY_PATH}"
    gameflow_phase_url = f"{connection.base_url}{recordtest.LCU_GAMEFLOW_PHASE_PATH}"
    gameflow_url = f"{connection.base_url}{recordtest.LCU_GAMEFLOW_SESSION_PATH}"
    queue_catalog_url = f"{connection.base_url}{recordtest.LCU_GAME_QUEUES_PATH}"
    routes = {
        champ_select_url: {"actions": [], "gameId": 123},
        champion_summary_url: [
            {"id": 103, "name": "Ahri"},
            {"id": 266, "name": "Aatrox"},
        ],
        gameflow_phase_url: "GameStart",
        gameflow_url: {
            "phase": "InProgress",
            "gameData": {
                "gameId": 456,
                "gameMode": "CLASSIC",
                "queue": {"id": 420, "type": "RANKED_SOLO_5x5"},
                "map": {"id": 11, "name": "Summoner's Rift"},
            }
        },
        queue_catalog_url: [
            {"id": 420, "name": "ランク ソロ/デュオ", "type": "RANKED_SOLO_5x5"}
        ],
    }
    client = recordtest.LiveClientRiotAPIClient(
        session_factory=FakeSessionFactory(routes),
        lcu_connection_provider=provider,
    )

    result = run(client.get_champ_select_session_result())
    catalog = run(client.get_champion_catalog())
    phase = run(client.get_gameflow_phase_result())
    match = run(client.get_match_metadata())

    assert result.status == recordtest.RiotPollStatus.IN_GAME
    assert result.payload["gameId"] == 123
    assert catalog == {103: "Ahri", 266: "Aatrox"}
    assert phase.status == recordtest.RiotPollStatus.IN_GAME
    assert phase.payload == {"phase": "GameStart"}
    assert match["queue_id"] == 420
    assert match["display_name"] == "ランク ソロ/デュオ"
    assert match["gameflow_phase"] == "InProgress"
    assert match["game_id"] == "456"


def test_riot_api_returns_none_when_lcu_server_is_down():
    client = recordtest.LiveClientRiotAPIClient(
        session_factory=FakeSessionFactory(error=aiohttp.ClientConnectionError("down"))
    )

    assert run(client.get_active_player_name()) is None
    assert run(client.get_event_data()) is None
    assert run(client.get_all_game_data()) is None


def test_riot_api_poll_result_distinguishes_live_client_states():
    client = recordtest.LiveClientRiotAPIClient(
        session_factory=FakeSessionFactory(
            {
                recordtest.ALL_GAME_URL: {
                    "gameData": {"gameTime": 10.0},
                    "allPlayers": [],
                }
            }
        )
    )

    result = run(client.get_all_game_data_result())

    assert result.status == recordtest.RiotPollStatus.IN_GAME
    assert result.payload["gameData"]["gameTime"] == 10.0


def test_riot_api_poll_result_treats_404_as_not_in_game():
    error = aiohttp.ClientResponseError(
        SimpleNamespace(real_url=recordtest.ALL_GAME_URL),
        (),
        status=404,
    )
    client = recordtest.LiveClientRiotAPIClient(session_factory=FakeSessionFactory(error=error))

    result = run(client.get_all_game_data_result())

    assert result.status == recordtest.RiotPollStatus.NOT_IN_GAME


def test_riot_api_poll_result_treats_connection_error_as_temporary_failure():
    client = recordtest.LiveClientRiotAPIClient(
        session_factory=FakeSessionFactory(error=aiohttp.ClientConnectionError("down"))
    )

    result = run(client.get_all_game_data_result())

    assert result.status == recordtest.RiotPollStatus.TEMPORARY_FAILURE


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


def test_disable_obs_global_audio_devices_disables_profile_and_live_inputs():
    class FakeObsRawClient:
        def __init__(self):
            self.requests = []
            self.mute_calls = []
            self.settings_calls = []

        def send(self, request_type, payload, raw=True):
            self.requests.append((request_type, dict(payload)))
            if request_type == "GetSpecialInputs":
                return {
                    "desktop1": "Desktop Audio",
                    "desktop2": None,
                    "mic1": "Mic/Aux",
                    "mic2": None,
                }
            return {}

        def set_input_mute(self, input_name, muted):
            self.mute_calls.append((input_name, muted))

        def set_input_settings(self, input_name, settings, overlay=True):
            self.settings_calls.append((input_name, dict(settings), overlay))

    client = FakeObsRawClient()

    recordtest.disable_obs_global_audio_devices(client)

    profile_requests = [
        payload for request_type, payload in client.requests if request_type == "SetProfileParameter"
    ]
    assert {payload["parameterName"] for payload in profile_requests} == set(
        recordtest.OBS_GLOBAL_AUDIO_DEVICE_PARAMETERS
    )
    assert all(payload["parameterCategory"] == "Audio" for payload in profile_requests)
    assert all(payload["parameterValue"] == "disabled" for payload in profile_requests)
    assert set(client.mute_calls) == {("Desktop Audio", True), ("Mic/Aux", True)}
    assert {
        (input_name, settings["device_id"], overlay)
        for input_name, settings, overlay in client.settings_calls
    } == {
        ("Desktop Audio", "disabled", True),
        ("Mic/Aux", "disabled", True),
    }


def test_setup_sync_elements_replaces_game_capture_with_window_capture_and_removes_empty_initial_scene():
    class FakeObsRawClient:
        def __init__(self):
            self.scenes = [{"sceneName": "Scene"}]
            self.inputs = [{"inputName": recordtest.DEFAULT_OBS_GAME_CAPTURE_NAME, "inputKind": "game_capture"}]
            self.scene_items_by_scene = {
                recordtest.DEFAULT_OBS_SCENE_NAME: [
                    {
                        "sourceName": recordtest.DEFAULT_OBS_GAME_CAPTURE_NAME,
                        "sceneItemId": 1,
                        "sceneItemIndex": 0,
                    }
                ],
            }
            self.created_inputs = []
            self.removed_inputs = []
            self.removed_scenes = []
            self.index_calls = []
            self.transform_calls = []
            self.current_scene = None
            self.next_item_id = 2

        def get_scene_list(self):
            return SimpleNamespace(scenes=self.scenes)

        def create_scene(self, scene_name):
            self.scenes.append({"sceneName": scene_name})
            self.scene_items_by_scene.setdefault(scene_name, [])

        def set_current_program_scene(self, scene_name):
            self.current_scene = scene_name

        def remove_scene(self, scene_name):
            self.removed_scenes.append(scene_name)
            self.scenes = [scene for scene in self.scenes if scene.get("sceneName") != scene_name]
            self.scene_items_by_scene.pop(scene_name, None)

        def get_input_list(self):
            return SimpleNamespace(inputs=self.inputs)

        def remove_input(self, input_name):
            self.removed_inputs.append(input_name)
            self.inputs = [item for item in self.inputs if item.get("inputName") != input_name]
            for items in self.scene_items_by_scene.values():
                items[:] = [item for item in items if item.get("sourceName") != input_name]

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
            scene_items = self.scene_items_by_scene.setdefault(scene_name, [])
            scene_items.append(
                {"sourceName": input_name, "sceneItemId": self.next_item_id, "sceneItemIndex": len(scene_items)}
            )
            self.next_item_id += 1

        def create_scene_item(self, scene_name, source_name, enabled=None):
            scene_items = self.scene_items_by_scene.setdefault(scene_name, [])
            scene_items.append(
                {"sourceName": source_name, "sceneItemId": self.next_item_id, "sceneItemIndex": len(scene_items)}
            )
            self.next_item_id += 1

        def get_scene_item_list(self, scene_name):
            return SimpleNamespace(scene_items=self.scene_items_by_scene.setdefault(scene_name, []))

        def set_scene_item_index(self, scene_name, item_id, item_index):
            self.index_calls.append((scene_name, item_id, item_index))
            for item in self.scene_items_by_scene.setdefault(scene_name, []):
                if item["sceneItemId"] == item_id:
                    item["sceneItemIndex"] = item_index

        def set_input_settings(self, input_name, settings, overlay=True):
            return None

        def set_scene_item_transform(self, scene_name, item_id, transform):
            self.transform_calls.append((scene_name, item_id, dict(transform)))

        def set_scene_item_enabled(self, scene_name, item_id, enabled):
            return None

    raw_client = FakeObsRawClient()
    client = recordtest.ObsWebSocketClient(
        config=recordtest.AppConfig.from_dict(
            {
                "obs": {
                    "game_capture_name": recordtest.DEFAULT_OBS_GAME_CAPTURE_NAME,
                    "game_capture_window": recordtest.DEFAULT_OBS_GAME_CAPTURE_WINDOW,
                }
            }
        )
    )
    client.client = raw_client

    client.setup_sync_elements()

    window_capture = raw_client.created_inputs[0]
    sync_marker = raw_client.created_inputs[1]
    assert raw_client.current_scene == recordtest.DEFAULT_OBS_SCENE_NAME
    assert raw_client.removed_scenes == ["Scene"]
    assert raw_client.removed_inputs == [recordtest.DEFAULT_OBS_GAME_CAPTURE_NAME]
    assert window_capture["kind"] == "window_capture"
    assert window_capture["name"] == recordtest.DEFAULT_OBS_WINDOW_CAPTURE_NAME
    assert window_capture["settings"]["method"] == recordtest.DEFAULT_OBS_WINDOW_CAPTURE_METHOD
    assert window_capture["settings"]["window"] == recordtest.DEFAULT_OBS_WINDOW_CAPTURE_WINDOW
    assert window_capture["settings"]["client_area"] is True
    assert window_capture["settings"]["capture_audio"] is False
    assert sync_marker["name"] == recordtest.DEFAULT_OBS_SOURCE_NAME

    scene_items = raw_client.scene_items_by_scene[recordtest.DEFAULT_OBS_SCENE_NAME]
    window_item_id = scene_items[0]["sceneItemId"]
    sync_item_id = scene_items[1]["sceneItemId"]
    assert (recordtest.DEFAULT_OBS_SCENE_NAME, window_item_id, 0) in raw_client.index_calls
    assert (recordtest.DEFAULT_OBS_SCENE_NAME, sync_item_id, 1) in raw_client.index_calls
    assert (
        recordtest.DEFAULT_OBS_SCENE_NAME,
        window_item_id,
        {
            "positionX": 0.0,
            "positionY": 0.0,
            "alignment": 5,
            "boundsType": "OBS_BOUNDS_SCALE_INNER",
            "boundsAlignment": 0,
            "boundsWidth": 1920.0,
            "boundsHeight": 1080.0,
        },
    ) in raw_client.transform_calls


def test_existing_window_capture_disables_embedded_audio_capture():
    class FakeObsRawClient:
        def __init__(self):
            self.inputs = [
                {
                    "inputName": recordtest.DEFAULT_OBS_WINDOW_CAPTURE_NAME,
                    "inputKind": "window_capture",
                }
            ]
            self.scene_items_by_scene = {
                recordtest.DEFAULT_OBS_SCENE_NAME: [
                    {
                        "sourceName": recordtest.DEFAULT_OBS_WINDOW_CAPTURE_NAME,
                        "sceneItemId": 5,
                        "sceneItemIndex": 0,
                    }
                ]
            }
            self.settings_calls = []

        def get_input_list(self):
            return SimpleNamespace(inputs=self.inputs)

        def set_input_settings(self, input_name, settings, overlay=True):
            self.settings_calls.append((input_name, dict(settings), overlay))

        def get_scene_item_list(self, scene_name):
            return SimpleNamespace(scene_items=self.scene_items_by_scene.setdefault(scene_name, []))

    raw_client = FakeObsRawClient()
    client = recordtest.ObsWebSocketClient(config=app_config())
    client.client = raw_client

    scene_item_id = client._ensure_window_capture_exists()

    assert scene_item_id == 5
    assert raw_client.settings_calls
    input_name, settings, overlay = raw_client.settings_calls[-1]
    assert input_name == recordtest.DEFAULT_OBS_WINDOW_CAPTURE_NAME
    assert overlay is True
    assert settings["capture_audio"] is False


def test_setup_sync_elements_keeps_initial_scene_when_window_capture_creation_fails():
    class FailingObsRawClient:
        def __init__(self):
            self.scenes = [{"sceneName": "Scene"}]
            self.inputs = []
            self.scene_items_by_scene = {}
            self.removed_scenes = []

        def get_scene_list(self):
            return SimpleNamespace(scenes=self.scenes)

        def create_scene(self, scene_name):
            self.scenes.append({"sceneName": scene_name})
            self.scene_items_by_scene.setdefault(scene_name, [])

        def set_current_program_scene(self, scene_name):
            return None

        def remove_scene(self, scene_name):
            self.removed_scenes.append(scene_name)

        def get_input_list(self):
            return SimpleNamespace(inputs=self.inputs)

        def create_input(self, scene_name, input_name, input_kind, input_settings, scene_item_enabled):
            if input_kind == "window_capture":
                raise RuntimeError("window capture unavailable")
            raise AssertionError("sync source must not be created after capture setup fails")

        def get_scene_item_list(self, scene_name):
            return SimpleNamespace(scene_items=self.scene_items_by_scene.setdefault(scene_name, []))

    raw_client = FailingObsRawClient()
    client = recordtest.ObsWebSocketClient(config=app_config())
    client.client = raw_client

    with pytest.raises(recordtest.RecorderError, match="ウィンドウキャプチャ"):
        client.setup_sync_elements()

    assert raw_client.removed_scenes == []


def test_obs_bootstrapper_creates_portable_marker_and_tray_disabled_global_ini(monkeypatch):
    obs_dir = Path("tests") / "_tmp" / "obs_bootstrapper" / "obs-portable"
    shutil.rmtree(obs_dir.parent, ignore_errors=True)
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir.resolve())
    result = recordtest.OBSBootstrapper(obs_dir).bootstrap()
    global_ini = result["global_ini_path"]
    user_ini = result["user_ini_path"]
    config_dir = result["config_dir"]

    assert (obs_dir / "obs_portable_mode.txt").exists()
    assert (obs_dir / "portable_mode.txt").exists()
    assert config_dir == (obs_dir / "config" / "obs-studio").resolve()
    assert config_dir.exists()
    assert global_ini.exists()
    assert user_ini.exists()

    text = global_ini.read_text(encoding="utf-8")
    assert "[General]" in text
    assert "FirstRun=true" in text
    assert "[BasicWindow]" in text
    assert "SysTrayEnabled=false" in text
    assert "SysTrayWhenStarted=false" in text
    assert "SysTrayMinimizeToTray=false" in text
    assert "HideTrayIcon" not in text

    user_text = user_ini.read_text(encoding="utf-8")
    assert "[General]" in user_text
    assert "FirstRun=true" in user_text
    assert "[BasicWindow]" in user_text
    assert "SysTrayEnabled=false" in user_text
    assert "SysTrayWhenStarted=false" in user_text
    assert "SysTrayMinimizeToTray=false" in user_text


def test_global_ini_removes_bom_and_nonstandard_tray_keys(monkeypatch):
    obs_dir = Path("tests") / "_tmp" / "obs_bootstrapper_bom" / "obs-portable"
    shutil.rmtree(obs_dir.parent, ignore_errors=True)
    ini_path = obs_dir / "config" / "obs-studio" / "global.ini"
    ini_path.parent.mkdir(parents=True, exist_ok=True)
    ini_path.write_text(
        "\ufeff[General]\nFirstRun=false\nSysTrayEnabled=true\n\n"
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
    assert "[General]" in text
    assert "FirstRun=true" in text
    assert "[BasicWindow]" in text
    assert "SysTrayEnabled=false" in text
    assert "SysTrayWhenStarted=false" in text
    assert "SysTrayMinimizeToTray=false" in text
    assert "HideTrayIcon" not in text


def test_global_ini_parse_error_deletes_and_regenerates_before_patch(monkeypatch):
    obs_dir = Path("tests") / "_tmp" / "obs_bootstrapper_corrupt" / "obs-portable"
    shutil.rmtree(obs_dir.parent, ignore_errors=True)
    ini_path = obs_dir / "config" / "obs-studio" / "global.ini"
    ini_path.parent.mkdir(parents=True, exist_ok=True)
    ini_path.write_text("[BasicWindow\nbroken", encoding="utf-8")

    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir.resolve())

    changed, result_path = recordtest.ensure_portable_obs_global_ini(obs_dir)

    assert changed is True
    assert result_path == ini_path.resolve()
    text = ini_path.read_text(encoding="utf-8")
    assert "FirstRun=true" in text
    assert "SysTrayEnabled=false" in text
    assert "SysTrayWhenStarted=false" in text
    assert "SysTrayMinimizeToTray=false" in text


def test_user_ini_is_primary_source_for_first_run_and_tray(monkeypatch):
    obs_dir = Path("tests") / "_tmp" / "obs_bootstrapper_user_ini" / "obs-portable"
    shutil.rmtree(obs_dir.parent, ignore_errors=True)
    user_ini = obs_dir / "config" / "obs-studio" / "user.ini"
    user_ini.parent.mkdir(parents=True, exist_ok=True)
    user_ini.write_text(
        "[General]\n"
        "FirstRun=false\n\n"
        "[BasicWindow]\n"
        "SysTrayEnabled=true\n"
        "SysTrayWhenStarted=true\n"
        "SysTrayMinimizeToTray=true\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir.resolve())

    changed, result_path = recordtest.ensure_portable_obs_user_ini(obs_dir)

    assert changed is True
    assert result_path == user_ini.resolve()
    text = user_ini.read_text(encoding="utf-8")
    assert "FirstRun=true" in text
    assert "SysTrayEnabled=false" in text
    assert "SysTrayWhenStarted=false" in text
    assert "SysTrayMinimizeToTray=false" in text


def _read_obs_profile_ini(path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


def _fake_launch_config(root, recording_encoder="auto"):
    return recordtest.AppConfig.from_dict(
        {
            "obs": {
                "password": "secret",
                "recording_encoder": recording_encoder,
            },
            "paths": {
                "recordings_dir": str(root / "recordings"),
                "json_dir": str(root / "recordings" / "json"),
            },
        }
    )


def _install_fake_obs_launch(monkeypatch, *, before_encoder_kinds=(), after_encoder_kinds=()):
    class FakeProcessManager:
        start_count = 0
        encoder_log_calls = 0
        terminated_pids = []

        def __init__(self, obs_dir_arg, logger=None):
            self.obs_dir = Path(obs_dir_arg)
            self.obs_exe = self.obs_dir / "bin" / "64bit" / "obs64.exe"

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            return []

        def unmanaged_processes(self):
            return []

        def isolated_env(self):
            return {}

        def start_obs(self, *args, **kwargs):
            type(self).start_count += 1
            return SimpleNamespace(pid=100 + type(self).start_count, poll=lambda: None)

        def hide_main_windows(self, *args, **kwargs):
            return 0

        def latest_log_portable_mode(self, since=None):
            return True

        def latest_log_encoder_kinds(self, since=None):
            type(self).encoder_log_calls += 1
            if type(self).start_count <= 0:
                return list(before_encoder_kinds)
            return list(after_encoder_kinds)

        def terminate_process(self, process, timeout_sec=3.0):
            type(self).terminated_pids.append(process.pid)

    class FakeBootstrapper:
        def __init__(self, base_dir):
            self.base_dir = Path(base_dir)

        def apply(self, *args, **kwargs):
            return {
                "global_ini_changed": False,
                "global_ini_path": self.base_dir / "config" / "obs-studio" / "global.ini",
                "user_ini_changed": False,
                "user_ini_path": self.base_dir / "config" / "obs-studio" / "user.ini",
                "websocket": (
                    False,
                    self.base_dir
                    / "config"
                    / "obs-studio"
                    / "plugin_config"
                    / "obs-websocket"
                    / "config.json",
                ),
            }

    monkeypatch.setattr(recordtest, "OBSProcessManager", FakeProcessManager)
    monkeypatch.setattr(recordtest, "OBSBootstrapper", FakeBootstrapper)
    monkeypatch.setattr(recordtest, "is_tcp_port_open", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest.time, "sleep", lambda *args, **kwargs: None)
    return FakeProcessManager


def test_launch_obs_uses_startup_log_gpu_encoder_without_restart(monkeypatch, tmp_path):
    obs_dir = (tmp_path / "obs-portable").resolve()
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir)
    manager = _install_fake_obs_launch(
        monkeypatch,
        before_encoder_kinds=["obs_nvenc_h264_tex", "obs_x264"],
        after_encoder_kinds=["obs_nvenc_h264_tex", "obs_x264"],
    )

    process = recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert process.pid == 101
    assert manager.start_count == 1
    assert manager.terminated_pids == []
    profile_ini = (
        obs_dir
        / "config"
        / "obs-studio"
        / "basic"
        / "profiles"
        / recordtest.MANAGED_OBS_PROFILE_DIR_NAME
        / "basic.ini"
    )
    parser = _read_obs_profile_ini(profile_ini)
    assert parser.get("SimpleOutput", "RecEncoder") == "nvenc"
    assert parser.get("AdvOut", "RecEncoder") == "obs_nvenc_h264_tex"


def test_launch_obs_restarts_once_when_gpu_encoder_is_discovered_after_start(monkeypatch, tmp_path):
    obs_dir = (tmp_path / "obs-portable").resolve()
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir)
    manager = _install_fake_obs_launch(
        monkeypatch,
        before_encoder_kinds=[],
        after_encoder_kinds=["obs_nvenc_h264_tex", "obs_x264"],
    )

    process = recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert process.pid == 102
    assert manager.start_count == 2
    assert manager.terminated_pids == [101]
    profile_ini = (
        obs_dir
        / "config"
        / "obs-studio"
        / "basic"
        / "profiles"
        / recordtest.MANAGED_OBS_PROFILE_DIR_NAME
        / "basic.ini"
    )
    parser = _read_obs_profile_ini(profile_ini)
    assert parser.get("SimpleOutput", "RecEncoder") == "nvenc"
    assert parser.get("AdvOut", "RecEncoder") == "obs_nvenc_h264_tex"


def test_launch_obs_does_not_restart_for_hevc_only_encoder_detection(monkeypatch, tmp_path):
    obs_dir = (tmp_path / "obs-portable").resolve()
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir)
    manager = _install_fake_obs_launch(
        monkeypatch,
        before_encoder_kinds=[],
        after_encoder_kinds=["obs_nvenc_hevc_tex"],
    )

    process = recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert process.pid == 101
    assert manager.start_count == 1
    assert manager.terminated_pids == []
    profile_ini = (
        obs_dir
        / "config"
        / "obs-studio"
        / "basic"
        / "profiles"
        / recordtest.MANAGED_OBS_PROFILE_DIR_NAME
        / "basic.ini"
    )
    parser = _read_obs_profile_ini(profile_ini)
    assert parser.get("SimpleOutput", "RecEncoder") == "x264"
    assert parser.get("AdvOut", "RecEncoder") == "obs_x264"


def test_launch_obs_does_not_probe_gpu_when_x264_is_configured(monkeypatch, tmp_path):
    obs_dir = (tmp_path / "obs-portable").resolve()
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir)
    manager = _install_fake_obs_launch(
        monkeypatch,
        before_encoder_kinds=["obs_nvenc_h264_tex", "obs_x264"],
        after_encoder_kinds=["obs_nvenc_h264_tex", "obs_x264"],
    )

    process = recordtest.launch_obs(_fake_launch_config(tmp_path, recording_encoder="x264"))

    assert process.pid == 101
    assert manager.start_count == 1
    assert manager.encoder_log_calls == 0
    assert manager.terminated_pids == []
    profile_ini = (
        obs_dir
        / "config"
        / "obs-studio"
        / "basic"
        / "profiles"
        / recordtest.MANAGED_OBS_PROFILE_DIR_NAME
        / "basic.ini"
    )
    parser = _read_obs_profile_ini(profile_ini)
    assert parser.get("SimpleOutput", "RecEncoder") == "x264"
    assert parser.get("AdvOut", "RecEncoder") == "obs_x264"


def test_launch_obs_refuses_external_websocket_port(monkeypatch):
    obs_dir = Path("tests") / "_tmp" / "obs_launch_port_conflict" / "obs-portable"
    shutil.rmtree(obs_dir.parent, ignore_errors=True)
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True, exist_ok=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir.resolve())

    class FakeProcessManager:
        def __init__(self, obs_dir_arg, logger=None):
            self.obs_dir = Path(obs_dir_arg)
            self.obs_exe = self.obs_dir / "bin" / "64bit" / "obs64.exe"

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            return []

        def unmanaged_processes(self):
            return []

        def isolated_env(self):
            return {}

        def start_obs(self, *args, **kwargs):
            raise AssertionError("managed OBS must not start when the websocket port is occupied")

    monkeypatch.setattr(recordtest, "OBSProcessManager", FakeProcessManager)
    monkeypatch.setattr(recordtest, "is_tcp_port_open", lambda *args, **kwargs: True)

    with pytest.raises(recordtest.RecorderError, match="OBS WebSocketポート"):
        recordtest.launch_obs(recordtest.AppConfig.from_dict({}))


def test_preflight_migrates_legacy_obs_studio_to_managed_portable(monkeypatch):
    root = Path("tests") / "_tmp" / "preflight_legacy_obs_migration"
    shutil.rmtree(root, ignore_errors=True)
    managed_dir = (root / "obs-portable").resolve()
    legacy_dir = (root / "bin" / "OBS-Studio").resolve()
    legacy_exe = legacy_dir / "bin" / "64bit" / "obs64.exe"
    legacy_ini = legacy_dir / "config" / "obs-studio" / "global.ini"
    legacy_exe.parent.mkdir(parents=True, exist_ok=True)
    legacy_ini.parent.mkdir(parents=True, exist_ok=True)
    legacy_exe.write_text("fake", encoding="utf-8")
    legacy_ini.write_text(
        "[General]\nFirstRun=false\n\n[BasicWindow]\nSysTrayEnabled=true\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(recordtest, "ROOT_DIR", root.resolve())
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", legacy_dir)

    report = recordtest.run_preflight_checks(
        {
            "obs": {"dir": str(legacy_dir), "port": 4455, "password": ""},
            "paths": {
                "bin_dir": str(root / "bin"),
                "recordings_dir": str(root / "recordings"),
                "json_dir": str(root / "recordings" / "json"),
                "champion_icons_dir": str(root / "assets" / "champions" / "icons"),
            },
        },
        auto_fix=True,
        ensure_dirs=True,
    )

    assert report["errors"] == []
    assert report["config"]["obs"]["dir"] == recordtest.DEFAULT_OBS_DIR
    migrated_ini = managed_dir / "config" / "obs-studio" / "global.ini"
    migrated_user_ini = managed_dir / "config" / "obs-studio" / "user.ini"
    assert (managed_dir / "bin" / "64bit" / "obs64.exe").exists()
    text = migrated_ini.read_text(encoding="utf-8")
    assert "FirstRun=true" in text
    assert "SysTrayEnabled=false" in text
    user_text = migrated_user_ini.read_text(encoding="utf-8")
    assert "FirstRun=true" in user_text
    assert "SysTrayEnabled=false" in user_text
    assert "SysTrayWhenStarted=false" in user_text
    legacy_text = legacy_ini.read_text(encoding="utf-8")
    assert "FirstRun=true" in legacy_text
    assert "SysTrayEnabled=false" in legacy_text
    legacy_user_text = (legacy_dir / "config" / "obs-studio" / "user.ini").read_text(encoding="utf-8")
    assert "FirstRun=true" in legacy_user_text
    assert "SysTrayEnabled=false" in legacy_user_text


def test_preflight_defers_bootstrap_repair_while_managed_obs_is_running(monkeypatch):
    root = Path("tests") / "_tmp" / "preflight_defer_running_obs_repair"
    shutil.rmtree(root, ignore_errors=True)
    managed_dir = (root / "obs-portable").resolve()
    obs_exe = managed_dir / "bin" / "64bit" / "obs64.exe"
    global_ini = managed_dir / "config" / "obs-studio" / "global.ini"
    obs_exe.parent.mkdir(parents=True, exist_ok=True)
    global_ini.parent.mkdir(parents=True, exist_ok=True)
    obs_exe.write_text("fake", encoding="utf-8")
    global_ini.write_text("[General]\nFirstRun=false\n", encoding="utf-8")

    class FakeProcessManager:
        def __init__(self, obs_dir_arg, logger=None):
            self.obs_dir = Path(obs_dir_arg)

        def has_managed_process(self):
            return True

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            raise AssertionError("preflight must not stop a running OBS to repair ini files")

    monkeypatch.setattr(recordtest, "ROOT_DIR", root.resolve())
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", (root / "bin" / "OBS-Studio").resolve())
    monkeypatch.setattr(recordtest, "OBSProcessManager", FakeProcessManager)

    report = recordtest.run_preflight_checks(
        {
            "obs": {"dir": str(managed_dir), "port": 4455, "password": ""},
            "paths": {
                "bin_dir": str(root / "bin"),
                "recordings_dir": str(root / "recordings"),
                "json_dir": str(root / "recordings" / "json"),
                "champion_icons_dir": str(root / "assets" / "champions" / "icons"),
            },
        },
        auto_fix=True,
        ensure_dirs=True,
    )

    assert report["errors"] == []
    assert any("起動中" in warning and "延期" in warning for warning in report["warnings"])
    assert "FirstRun=false" in global_ini.read_text(encoding="utf-8")


def test_launch_obs_refuses_unmanaged_obs_process(monkeypatch):
    obs_dir = Path("tests") / "_tmp" / "obs_launch_unmanaged_process" / "obs-portable"
    shutil.rmtree(obs_dir.parent, ignore_errors=True)
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True, exist_ok=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir.resolve())
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", (obs_dir.parent / "bin" / "OBS-Studio").resolve())

    class FakeProcessManager:
        def __init__(self, obs_dir_arg, logger=None):
            self.obs_dir = Path(obs_dir_arg)
            self.obs_exe = self.obs_dir / "bin" / "64bit" / "obs64.exe"

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            return []

        def unmanaged_processes(self):
            return [
                SimpleNamespace(
                    pid=200,
                    executable_path=Path("C:/Program Files/obs-studio/bin/64bit/obs64.exe"),
                )
            ]

        def isolated_env(self):
            return {}

        def start_obs(self, *args, **kwargs):
            raise AssertionError("managed OBS must not start while unmanaged OBS is running")

    monkeypatch.setattr(recordtest, "OBSProcessManager", FakeProcessManager)
    monkeypatch.setattr(recordtest, "is_tcp_port_open", lambda *args, **kwargs: False)

    with pytest.raises(recordtest.RecorderError, match="管理対象外のOBS"):
        recordtest.launch_obs(recordtest.AppConfig.from_dict({}))


def test_storage_limit_only_deletes_json_referenced_app_video():
    root = Path("tests") / "_tmp" / "storage_limit_scope"
    shutil.rmtree(root, ignore_errors=True)
    recordings_dir = root / "recordings"
    json_dir = recordings_dir / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = recordings_dir / "clips"
    clips_dir.mkdir()

    owned_video = recordings_dir / "owned.mp4"
    unrelated_video = recordings_dir / "unrelated.mp4"
    owned_clip = clips_dir / "owned_clip_1000_2000.mp4"
    unrelated_clip = clips_dir / "unrelated_clip_1000_2000.mp4"
    owned_video.write_bytes(b"owned video")
    unrelated_video.write_bytes(b"unrelated video that must remain")
    owned_clip.write_bytes(b"owned clip")
    unrelated_clip.write_bytes(b"unrelated clip that must remain")
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
    assert not owned_clip.exists()
    assert not json_path.exists()
    assert unrelated_video.exists()
    assert unrelated_clip.exists()


def test_storage_limit_deletes_session_when_clip_listing_fails(monkeypatch, tmp_path):
    recordings_dir = tmp_path / "recordings"
    json_dir = recordings_dir / "json"
    clips_dir = recordings_dir / "clips"
    json_dir.mkdir(parents=True)
    clips_dir.mkdir()

    owned_video = recordings_dir / "owned.mp4"
    owned_clip = clips_dir / "owned_clip_1000_2000.mp4"
    json_path = json_dir / "lol_20260101_000000.json"
    owned_video.write_bytes(b"owned video")
    owned_clip.write_bytes(b"owned clip")
    json_path.write_text(
        json.dumps(
            {
                "saved_at": "2026-01-01 00:00:00",
                "obs_record_path": owned_video.name,
            }
        ),
        encoding="utf-8",
    )
    resolved_clips_dir = clips_dir.resolve()
    original_glob = Path.glob

    def fail_clip_glob(path: Path, pattern: str, *args, **kwargs):
        if path.resolve() == resolved_clips_dir:
            raise OSError("access denied")
        return original_glob(path, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", fail_clip_glob)
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
    assert owned_clip.exists()
