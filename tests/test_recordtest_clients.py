import asyncio
import configparser
import inspect
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest

from src import obs_bootstrap, recordtest
from src.lcu_client import LCUConnectionInfo
from src.obs_process import OBSProcessTerminationError


def _managed_settings_stop_evidence(base_dir: Path):
    process = obs_bootstrap.OBSProcessInfo(
        pid=4312,
        executable_path=base_dir / "bin" / "64bit" / "obs64.exe",
        creation_time=10.0,
    )
    return obs_bootstrap._create_obs_settings_stop_evidence(
        base_dir,
        before=obs_bootstrap.OBSProcessQuerySnapshot(
            processes=(process,),
            queried_at=100.0,
        ),
        after=obs_bootstrap.OBSProcessQuerySnapshot(
            processes=(),
            queried_at=101.0,
        ),
        killed_pids=(process.pid,),
    )


def _create_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr or completed.stdout or "mklink /J failed")
        return
    os.symlink(target, link, target_is_directory=True)


def _hold_preflight_migration_during_copy(source: str, destination: str, entered, release) -> None:
    original_write_all = obs_bootstrap._write_all

    def hold_after_copy_write(descriptor: int, payload: bytes) -> None:
        original_write_all(descriptor, payload)
        if payload == b"preflight-live-copy":
            entered.set()
            if not release.wait(15):
                raise TimeoutError("test did not release migration")

    obs_bootstrap._write_all = hold_after_copy_write
    obs_bootstrap.migrate_legacy_obs_installation(destination, [source])


def _preflight_config(managed_dir: Path, tmp_path: Path) -> dict:
    return {
        "obs": {"dir": str(managed_dir), "port": 4455, "password": "secret"},
        "paths": {
            "bin_dir": str(tmp_path / "bin"),
            "recordings_dir": str(tmp_path / "recordings"),
            "json_dir": str(tmp_path / "recordings" / "json"),
            "champion_icons_dir": str(tmp_path / "assets" / "champions" / "icons"),
        },
    }


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


def test_recording_profile_rejects_profiles_root_reparse_without_external_write(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    config_dir = obs_dir / "config" / "obs-studio"
    config_dir.mkdir(parents=True)
    user_ini = config_dir / "user.ini"
    user_ini.write_text("[Basic]\nProfileDir=safe\n", encoding="utf-8")
    user_before = user_ini.read_bytes()

    external_profiles = tmp_path / "external-profiles"
    external_profile = external_profiles / "safe"
    external_profile.mkdir(parents=True)
    external_ini = external_profile / "basic.ini"
    external_ini.write_text("[General]\nName=external\n", encoding="utf-8")
    external_before = external_ini.read_bytes()
    sentinel = external_profiles / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    (config_dir / "basic").mkdir()
    _create_directory_link(config_dir / "basic" / "profiles", external_profiles)

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="reparse point"):
        recordtest.ensure_obs_recording_profile_ini(obs_dir, record_dir=tmp_path / "recordings")

    assert external_ini.read_bytes() == external_before
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert user_ini.read_bytes() == user_before


def test_recording_profile_rejects_later_profile_reparse_before_any_write(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    profiles_root = obs_dir / "config" / "obs-studio" / "basic" / "profiles"
    safe_ini = profiles_root / "aaa-safe" / "basic.ini"
    safe_ini.parent.mkdir(parents=True)
    safe_ini.write_text("[Output]\nMode=Advanced\n", encoding="utf-8")
    safe_before = safe_ini.read_bytes()
    user_ini = obs_dir / "config" / "obs-studio" / "user.ini"
    user_ini.write_text("[Basic]\nProfileDir=aaa-safe\n", encoding="utf-8")
    user_before = user_ini.read_bytes()

    external_profile = tmp_path / "external-profile"
    external_profile.mkdir()
    external_ini = external_profile / "basic.ini"
    external_ini.write_text("[General]\nName=external\n", encoding="utf-8")
    external_before = external_ini.read_bytes()
    _create_directory_link(profiles_root / "zzz-unsafe", external_profile)

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="reparse point"):
        recordtest.ensure_obs_recording_profile_ini(obs_dir, record_dir=tmp_path / "recordings")

    assert safe_ini.read_bytes() == safe_before
    assert user_ini.read_bytes() == user_before
    assert external_ini.read_bytes() == external_before
    assert not (profiles_root / recordtest.MANAGED_OBS_PROFILE_DIR_NAME).exists()


def test_recording_profile_rejects_later_basic_ini_hardlink_before_any_write(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    profiles_root = obs_dir / "config" / "obs-studio" / "basic" / "profiles"
    safe_ini = profiles_root / "aaa-safe" / "basic.ini"
    safe_ini.parent.mkdir(parents=True)
    safe_ini.write_text("[Output]\nMode=Advanced\n", encoding="utf-8")
    safe_before = safe_ini.read_bytes()
    user_ini = obs_dir / "config" / "obs-studio" / "user.ini"
    user_ini.write_text("[Basic]\nProfileDir=aaa-safe\n", encoding="utf-8")
    user_before = user_ini.read_bytes()

    external_ini = tmp_path / "external-basic.ini"
    external_ini.write_text("[General]\nName=external\n", encoding="utf-8")
    external_before = external_ini.read_bytes()
    linked_ini = profiles_root / "zzz-linked" / "basic.ini"
    linked_ini.parent.mkdir()
    os.link(external_ini, linked_ini)

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="hardlink"):
        recordtest.ensure_obs_recording_profile_ini(obs_dir, record_dir=tmp_path / "recordings")

    assert safe_ini.read_bytes() == safe_before
    assert user_ini.read_bytes() == user_before
    assert external_ini.read_bytes() == external_before
    assert linked_ini.read_bytes() == external_before


def test_recording_profile_rejects_user_ini_hardlink_before_profile_write(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    profile_ini = obs_dir / "config" / "obs-studio" / "basic" / "profiles" / "safe" / "basic.ini"
    profile_ini.parent.mkdir(parents=True)
    profile_ini.write_text("[Output]\nMode=Advanced\n", encoding="utf-8")
    profile_before = profile_ini.read_bytes()
    external_user = tmp_path / "external-user.ini"
    external_user.write_text("[Basic]\nProfileDir=safe\n", encoding="utf-8")
    external_before = external_user.read_bytes()
    user_ini = obs_dir / "config" / "obs-studio" / "user.ini"
    os.link(external_user, user_ini)

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="hardlink"):
        recordtest.ensure_obs_recording_profile_ini(obs_dir, record_dir=tmp_path / "recordings")

    assert profile_ini.read_bytes() == profile_before
    assert external_user.read_bytes() == external_before
    assert user_ini.read_bytes() == external_before


@pytest.mark.parametrize(
    "profile_name",
    [
        ".",
        "..",
        "../outside",
        "..\\outside",
        "C:\\outside",
        "name:stream",
        "bad<name",
        "bad>name",
        'bad"name',
        "bad|name",
        "bad?name",
        "bad*name",
        "CON",
        "NUL.txt",
        "trailing.",
        "trailing ",
    ],
)
def test_recording_profile_rejects_unsafe_profile_dir_component(profile_name, tmp_path):
    obs_dir = tmp_path / "obs-portable"
    profiles_root = obs_dir / "config" / "obs-studio" / "basic" / "profiles"
    safe_ini = profiles_root / "safe" / "basic.ini"
    safe_ini.parent.mkdir(parents=True)
    safe_ini.write_text("[Output]\nMode=Advanced\n", encoding="utf-8")
    safe_before = safe_ini.read_bytes()
    outside_ini = profiles_root.parent / "basic.ini"
    outside_ini.write_text("outside", encoding="utf-8")
    outside_before = outside_ini.read_bytes()
    user_ini = obs_dir / "config" / "obs-studio" / "user.ini"
    user_ini.write_text(f"[Basic]\nProfileDir={profile_name}\n", encoding="utf-8")
    user_before = user_ini.read_bytes()

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="単一component"):
        recordtest.ensure_obs_recording_profile_ini(obs_dir, record_dir=tmp_path / "recordings")

    assert safe_ini.read_bytes() == safe_before
    assert outside_ini.read_bytes() == outside_before
    assert user_ini.read_bytes() == user_before
    assert not (profiles_root / recordtest.MANAGED_OBS_PROFILE_DIR_NAME).exists()


def test_recording_profile_rejects_casefold_colliding_discovered_names(monkeypatch, tmp_path):
    obs_dir = tmp_path / "obs-portable"
    profiles_root = obs_dir / "config" / "obs-studio" / "basic" / "profiles"
    profile_dir = profiles_root / "Replay"
    profile_dir.mkdir(parents=True)
    (profile_dir / "basic.ini").write_text("[General]\nName=Replay\n", encoding="utf-8")
    monkeypatch.setattr(
        recordtest,
        "list_safe_obs_config_directory",
        lambda _path: (
            obs_bootstrap.OBSConfigDirectoryEntry("Replay", "directory"),
            obs_bootstrap.OBSConfigDirectoryEntry("replay", "directory"),
        ),
    )

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="case-insensitive"):
        recordtest.preflight_obs_recording_profile_ini(obs_dir)


def test_recording_profile_rejects_special_basic_ini_before_any_write(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    profiles_root = obs_dir / "config" / "obs-studio" / "basic" / "profiles"
    safe_ini = profiles_root / "aaa-safe" / "basic.ini"
    safe_ini.parent.mkdir(parents=True)
    safe_ini.write_text("[Output]\nMode=Advanced\n", encoding="utf-8")
    safe_before = safe_ini.read_bytes()
    special_ini = profiles_root / "zzz-special" / "basic.ini"
    special_ini.mkdir(parents=True)

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="通常ファイル"):
        recordtest.ensure_obs_recording_profile_ini(obs_dir, record_dir=tmp_path / "recordings")

    assert safe_ini.read_bytes() == safe_before


def test_recording_profile_propagates_acl_error_without_reset_or_write(monkeypatch, tmp_path):
    obs_dir = tmp_path / "obs-portable"
    profile_ini = obs_dir / "config" / "obs-studio" / "basic" / "profiles" / "safe" / "basic.ini"
    profile_ini.parent.mkdir(parents=True)
    profile_ini.write_text("[Output]\nMode=Advanced\n", encoding="utf-8")
    before = profile_ini.read_bytes()
    real_preflight = recordtest.preflight_obs_config_file

    def deny_profile_read(path, **kwargs):
        if Path(path) == profile_ini:
            raise PermissionError("simulated profile ACL denial")
        return real_preflight(path, **kwargs)

    monkeypatch.setattr(recordtest, "preflight_obs_config_file", deny_profile_read)

    with pytest.raises(PermissionError, match="ACL denial"):
        recordtest.ensure_obs_recording_profile_ini(obs_dir, record_dir=tmp_path / "recordings")

    assert profile_ini.read_bytes() == before
    assert not obs_bootstrap.get_obs_settings_transaction_marker(obs_dir).exists()


def test_recording_profile_uses_shared_mutation_lock(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    profile_ini = obs_dir / "config" / "obs-studio" / "basic" / "profiles" / "safe" / "basic.ini"
    profile_ini.parent.mkdir(parents=True)
    profile_ini.write_text("[Output]\nMode=Advanced\n", encoding="utf-8")
    before = profile_ini.read_bytes()
    lock = obs_bootstrap._OBSInterProcessLock(obs_bootstrap.get_obs_copy_lock_path(obs_dir))
    assert lock.acquire() is True
    try:
        with pytest.raises(recordtest.OBSMigrationInProgressError):
            recordtest.ensure_obs_recording_profile_ini(obs_dir, record_dir=tmp_path / "recordings")
    finally:
        lock.release()

    assert profile_ini.read_bytes() == before


def test_recording_profile_rejects_unmanaged_obs_before_kill_or_commit(
    monkeypatch,
    tmp_path,
):
    obs_dir = tmp_path / "obs-portable"

    class UnmanagedProcessManager:
        kill_calls = 0

        def __init__(self, obs_dir_arg, **_kwargs):
            self.obs_dir = Path(obs_dir_arg).absolute()

        def query_obs_processes_strict(self):
            return obs_bootstrap.OBSProcessQuerySnapshot(
                processes=(
                    obs_bootstrap.OBSProcessInfo(
                        pid=99,
                        executable_path=(tmp_path / "regular-obs" / "obs64.exe").absolute(),
                        creation_time=10.0,
                    ),
                ),
                queried_at=100.0,
            )

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            type(self).kill_calls += 1
            raise AssertionError("unmanaged OBS must be rejected before managed kill")

        def has_managed_process(self):
            raise AssertionError("managed state must not be queried after unmanaged reject")

    monkeypatch.setattr(recordtest, "OBSProcessManager", UnmanagedProcessManager)
    profile_ini = (
        obs_dir
        / "config"
        / "obs-studio"
        / "basic"
        / "profiles"
        / recordtest.MANAGED_OBS_PROFILE_DIR_NAME
        / "basic.ini"
    )

    with pytest.raises(recordtest.RecorderError, match="管理対象外"):
        recordtest.ensure_obs_recording_profile_ini(
            obs_dir,
            record_dir=tmp_path / "recordings",
        )

    assert UnmanagedProcessManager.kill_calls == 0
    assert not profile_ini.exists()
    assert not obs_bootstrap.get_obs_settings_transaction_marker(obs_dir).exists()


def test_recording_profile_rejects_managed_obs_that_survives_kill(
    monkeypatch,
    tmp_path,
):
    obs_dir = tmp_path / "obs-portable"

    class StubbornProcessManager:
        kill_calls = 0

        def __init__(self, obs_dir_arg, **_kwargs):
            self.obs_dir = Path(obs_dir_arg).absolute()
            self.query_calls = 0
            self.process = obs_bootstrap.OBSProcessInfo(
                pid=4312,
                executable_path=self.obs_dir / "bin" / "64bit" / "obs64.exe",
                creation_time=10.0,
            )

        def query_obs_processes_strict(self):
            self.query_calls += 1
            return obs_bootstrap.OBSProcessQuerySnapshot(
                processes=(self.process,),
                queried_at=100.0 + self.query_calls,
            )

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            type(self).kill_calls += 1
            return [self.process.pid]

        def terminate_expected_obs_processes_strict(self, expected):
            self.kill_stale_managed_processes()
            return obs_bootstrap.OBSStrictTerminationResult(
                expected,
                self.query_obs_processes_strict(),
            )

        def has_managed_process(self):
            return True

    monkeypatch.setattr(recordtest, "OBSProcessManager", StubbornProcessManager)
    profile_ini = (
        obs_dir
        / "config"
        / "obs-studio"
        / "basic"
        / "profiles"
        / recordtest.MANAGED_OBS_PROFILE_DIR_NAME
        / "basic.ini"
    )

    with pytest.raises(recordtest.RecorderError, match="停止できません"):
        recordtest.ensure_obs_recording_profile_ini(
            obs_dir,
            record_dir=tmp_path / "recordings",
        )

    assert StubbornProcessManager.kill_calls == 1
    assert not profile_ini.exists()
    assert not obs_bootstrap.get_obs_settings_transaction_marker(obs_dir).exists()


def test_recording_profile_replans_real_obs_flush_and_preserves_unknown_key(
    monkeypatch,
    tmp_path,
):
    obs_dir = (tmp_path / "obs-portable").absolute()
    profile_ini = (
        obs_dir
        / "config"
        / "obs-studio"
        / "basic"
        / "profiles"
        / recordtest.MANAGED_OBS_PROFILE_DIR_NAME
        / "basic.ini"
    )

    class FlushingProcessManager:
        active = False
        flush_enabled = False
        kill_calls = 0
        query_calls = 0

        def __init__(self, obs_dir_arg, **_kwargs):
            self.obs_dir = Path(obs_dir_arg).absolute()
            self.process = obs_bootstrap.OBSProcessInfo(
                pid=4312,
                executable_path=self.obs_dir / "bin" / "64bit" / "obs64.exe",
                creation_time=10.0,
            )

        def query_obs_processes_strict(self):
            type(self).query_calls += 1
            return obs_bootstrap.OBSProcessQuerySnapshot(
                processes=(self.process,) if type(self).active else (),
                queried_at=100.0 + type(self).query_calls,
            )

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            type(self).kill_calls += 1
            if type(self).flush_enabled:
                profile_ini.write_bytes(
                    b"[General]\nName=external-update\n"
                    b"ExternalAfterStop=keep\n"
                )
            killed = [self.process.pid] if type(self).active else []
            type(self).active = False
            return killed

        def terminate_expected_obs_processes_strict(self, expected):
            killed = set(self.kill_stale_managed_processes())
            return obs_bootstrap.OBSStrictTerminationResult(
                tuple(item for item in expected if item.pid in killed),
                self.query_obs_processes_strict(),
            )

    monkeypatch.setattr(recordtest, "OBSProcessManager", FlushingProcessManager)
    record_dir = tmp_path / "recordings"
    recordtest.ensure_obs_recording_profile_ini(obs_dir, record_dir=record_dir)
    FlushingProcessManager.active = True
    FlushingProcessManager.flush_enabled = True

    changed_paths = recordtest.ensure_obs_recording_profile_ini(
        obs_dir,
        record_dir=record_dir,
    )

    rendered = profile_ini.read_text(encoding="utf-8")
    assert changed_paths == (profile_ini,)
    assert "Name=LoLReplayTool" in rendered
    assert "ExternalAfterStop=keep" in rendered
    assert FlushingProcessManager.kill_calls == 2
    assert FlushingProcessManager.query_calls == 5

    FlushingProcessManager.flush_enabled = False
    assert recordtest.ensure_obs_recording_profile_ini(
        obs_dir,
        record_dir=record_dir,
    ) == ()
    assert FlushingProcessManager.kill_calls == 3
    assert FlushingProcessManager.query_calls == 7
    assert not obs_bootstrap.get_obs_settings_transaction_marker(obs_dir).exists()


def test_startup_settings_compose_user_ini_from_one_original_snapshot(
    monkeypatch,
    tmp_path,
):
    obs_dir = (tmp_path / "obs-portable").absolute()
    user_ini = obs_bootstrap.get_obs_user_ini_path(obs_dir)
    user_ini.parent.mkdir(parents=True)
    user_ini.write_text(
        "[General]\nCustomGeneral=keep\n\n[Basic]\nCustomBasic=keep\n",
        encoding="utf-8",
    )
    real_preflight = obs_bootstrap.preflight_obs_config_file
    user_reads = 0

    def count_user_snapshot(path, **kwargs):
        nonlocal user_reads
        if Path(path).absolute() == user_ini.absolute():
            user_reads += 1
        return real_preflight(path, **kwargs)

    monkeypatch.setattr(
        obs_bootstrap,
        "preflight_obs_config_file",
        count_user_snapshot,
    )
    bootstrapper = obs_bootstrap.OBSBootstrapper(obs_dir)
    plan = recordtest._prepare_obs_startup_settings_plan(
        bootstrapper,
        port=4455,
        password="secret",
        record_dir=tmp_path / "recordings",
        scale_type=recordtest.DEFAULT_OBS_SCALE_TYPE,
        recording_quality=recordtest.DEFAULT_OBS_RECORDING_QUALITY,
        recording_encoder="x264",
        selected_encoder=None,
    )

    user_write = next(
        write
        for write in plan.transaction.writes
        if write.snapshot.path == user_ini.absolute()
    )
    rendered = user_write.payload.decode("utf-8")
    assert user_reads == 1
    assert "CustomGeneral=keep" in rendered
    assert "CustomBasic=keep" in rendered
    assert "FirstRun=true" in rendered
    assert "Profile=LoLReplayTool" in rendered
    assert "ProfileDir=LoLReplayTool" in rendered


def test_startup_settings_reject_user_ini_change_between_bootstrap_and_profile_plan(
    monkeypatch,
    tmp_path,
):
    obs_dir = (tmp_path / "obs-portable").absolute()
    user_ini = obs_bootstrap.get_obs_user_ini_path(obs_dir)
    user_ini.parent.mkdir(parents=True)
    user_ini.write_bytes(b"[General]\nCustom=original\n")
    external = b"[General]\nCustom=external-update\n"
    real_profile_preflight = recordtest.preflight_obs_recording_profile_ini
    stop_calls = 0

    def change_after_bootstrap_snapshot(base_dir, *, user_file=None):
        user_ini.write_bytes(external)
        return real_profile_preflight(base_dir, user_file=user_file)

    def stop_managed() -> None:
        nonlocal stop_calls
        stop_calls += 1

    monkeypatch.setattr(
        recordtest,
        "preflight_obs_recording_profile_ini",
        change_after_bootstrap_snapshot,
    )

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="内容が変化"):
        recordtest._execute_obs_startup_settings_transaction(
            obs_bootstrap.OBSBootstrapper(obs_dir),
            port=4455,
            password="secret",
            record_dir=tmp_path / "recordings",
            scale_type=recordtest.DEFAULT_OBS_SCALE_TYPE,
            recording_quality=recordtest.DEFAULT_OBS_RECORDING_QUALITY,
            recording_encoder="x264",
            selected_encoder=None,
            before_commit=stop_managed,
        )

    assert stop_calls == 0
    assert user_ini.read_bytes() == external
    assert not obs_bootstrap.get_obs_settings_transaction_marker(obs_dir).exists()


def test_startup_settings_replans_profile_flush_and_returns_fresh_paths(tmp_path):
    obs_dir = (tmp_path / "obs-portable").absolute()

    class RetryQueryManager:
        def __init__(self):
            self.query_calls = 0

        def query_obs_processes_strict(self):
            self.query_calls += 1
            return obs_bootstrap.OBSProcessQuerySnapshot(
                processes=(),
                queried_at=102.0 + self.query_calls,
            )

    retry_manager = RetryQueryManager()
    bootstrapper = obs_bootstrap.OBSBootstrapper(
        obs_dir,
        process_manager=retry_manager,
    )
    transaction_kwargs = {
        "port": 4455,
        "password": "secret",
        "record_dir": tmp_path / "recordings",
        "scale_type": recordtest.DEFAULT_OBS_SCALE_TYPE,
        "recording_quality": recordtest.DEFAULT_OBS_RECORDING_QUALITY,
        "recording_encoder": "x264",
        "selected_encoder": None,
    }
    recordtest._execute_obs_startup_settings_transaction(
        bootstrapper,
        **transaction_kwargs,
        before_commit=None,
    )
    profile_ini = (
        obs_dir
        / "config"
        / "obs-studio"
        / "basic"
        / "profiles"
        / recordtest.MANAGED_OBS_PROFILE_DIR_NAME
        / "basic.ini"
    )
    global_ini = obs_bootstrap.get_obs_global_ini_path(obs_dir)

    def simulate_obs_profile_flush():
        global_ini.write_bytes(
            b"[General]\nFirstRun=false\nExternalGlobalAfterStop=keep\n\n"
            b"[BasicWindow]\nSysTrayEnabled=false\n"
            b"SysTrayWhenStarted=false\nSysTrayMinimizeToTray=false\n"
        )
        profile_ini.write_bytes(
            b"[General]\nName=external-update\nExternalAfterStop=keep\n"
        )
        return _managed_settings_stop_evidence(obs_dir)

    result, changed_paths = recordtest._execute_obs_startup_settings_transaction(
        bootstrapper,
        **transaction_kwargs,
        before_commit=simulate_obs_profile_flush,
        run_before_commit_on_noop=True,
    )

    rendered = profile_ini.read_text(encoding="utf-8")
    global_rendered = global_ini.read_text(encoding="utf-8")
    assert result["global_ini_changed"] is True
    assert changed_paths == (profile_ini,)
    assert "Name=LoLReplayTool" in rendered
    assert "ExternalAfterStop=keep" in rendered
    assert "FirstRun=true" in global_rendered
    assert "ExternalGlobalAfterStop=keep" in global_rendered
    assert retry_manager.query_calls == 1
    assert not obs_bootstrap.get_obs_settings_transaction_marker(obs_dir).exists()


def test_startup_settings_reject_new_basic_ini_in_existing_profile_after_stop(
    tmp_path,
):
    obs_dir = (tmp_path / "obs-portable").absolute()
    bootstrapper = obs_bootstrap.OBSBootstrapper(obs_dir)
    transaction_kwargs = {
        "port": 4455,
        "password": "secret",
        "record_dir": tmp_path / "recordings",
        "scale_type": recordtest.DEFAULT_OBS_SCALE_TYPE,
        "recording_quality": recordtest.DEFAULT_OBS_RECORDING_QUALITY,
        "recording_encoder": "x264",
        "selected_encoder": None,
    }
    recordtest._execute_obs_startup_settings_transaction(
        bootstrapper,
        **transaction_kwargs,
        before_commit=None,
    )
    late_profile = (
        obs_dir
        / "config"
        / "obs-studio"
        / "basic"
        / "profiles"
        / "existing-empty"
        / "basic.ini"
    )
    late_profile.parent.mkdir()
    managed_profile = (
        late_profile.parent.parent
        / recordtest.MANAGED_OBS_PROFILE_DIR_NAME
        / "basic.ini"
    )
    managed_before = managed_profile.read_bytes()

    def simulate_obs_creating_profile_file():
        late_profile.write_bytes(b"[General]\nName=created-after-plan\n")
        return _managed_settings_stop_evidence(obs_dir)

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="存在状態"):
        recordtest._execute_obs_startup_settings_transaction(
            bootstrapper,
            **transaction_kwargs,
            before_commit=simulate_obs_creating_profile_file,
            run_before_commit_on_noop=True,
        )

    assert late_profile.read_bytes() == b"[General]\nName=created-after-plan\n"
    assert managed_profile.read_bytes() == managed_before
    assert not obs_bootstrap.get_obs_settings_transaction_marker(obs_dir).exists()


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


def test_preflight_rejects_config_reparse_before_profile_or_external_write(monkeypatch, tmp_path):
    managed_dir = (tmp_path / "obs-portable").absolute()
    obs_executable = managed_dir / "bin" / "64bit" / "obs64.exe"
    obs_executable.parent.mkdir(parents=True)
    obs_executable.write_bytes(b"fake obs")
    external = tmp_path / "external-config"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    _create_directory_link(managed_dir / "config", external)
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", tmp_path / "missing-legacy")
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "missing-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "missing-data")

    report = recordtest.run_preflight_checks(
        {
            "obs": {"dir": str(managed_dir), "port": 4455, "password": "secret"},
            "paths": {
                "bin_dir": str(tmp_path / "bin"),
                "recordings_dir": str(tmp_path / "recordings"),
                "json_dir": str(tmp_path / "recordings" / "json"),
                "champion_icons_dir": str(tmp_path / "assets" / "champions" / "icons"),
            },
        },
        auto_fix=True,
        ensure_dirs=True,
    )

    assert any("reparse point" in warning for warning in report["warnings"])
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (external / "obs-studio").exists()
    assert not (managed_dir / obs_bootstrap.PORTABLE_OBS_MARKER_NAME).exists()


def test_read_only_preflight_reports_pending_settings_transaction(monkeypatch, tmp_path):
    managed_dir = (tmp_path / "obs-portable").absolute()
    obs_executable = managed_dir / "bin" / "64bit" / "obs64.exe"
    obs_executable.parent.mkdir(parents=True)
    obs_executable.write_bytes(b"fake obs")
    obs_bootstrap.OBSBootstrapper(managed_dir).apply()
    settings_marker = obs_bootstrap.get_obs_settings_transaction_marker(managed_dir)
    settings_marker.write_bytes(b"inactive-stale-settings")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", tmp_path / "missing-legacy")
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "missing-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "missing-data")

    report = recordtest.run_preflight_checks(
        _preflight_config(managed_dir, tmp_path),
        auto_fix=False,
        ensure_dirs=False,
    )

    assert any(
        "OBS設定transaction" in warning and "再開待ち" in warning
        for warning in report["warnings"]
    )
    assert settings_marker.read_bytes() == b"inactive-stale-settings"


def test_preflight_rejects_nested_profile_reparse_before_bootstrap_write(monkeypatch, tmp_path):
    managed_dir = (tmp_path / "obs-portable").absolute()
    obs_executable = managed_dir / "bin" / "64bit" / "obs64.exe"
    obs_executable.parent.mkdir(parents=True)
    obs_executable.write_bytes(b"fake obs")
    config_dir = managed_dir / "config" / "obs-studio"
    config_dir.mkdir(parents=True)
    global_ini = config_dir / "global.ini"
    global_ini.write_text("[General]\nFirstRun=false\n", encoding="utf-8")
    global_before = global_ini.read_bytes()
    user_ini = config_dir / "user.ini"
    user_ini.write_text("[Basic]\nProfileDir=safe\n", encoding="utf-8")
    user_before = user_ini.read_bytes()
    profiles_root = config_dir / "basic" / "profiles"
    safe_ini = profiles_root / "aaa-safe" / "basic.ini"
    safe_ini.parent.mkdir(parents=True)
    safe_ini.write_text("[Output]\nMode=Advanced\n", encoding="utf-8")
    safe_before = safe_ini.read_bytes()
    external_profile = tmp_path / "external-profile"
    external_profile.mkdir()
    sentinel = external_profile / "basic.ini"
    sentinel.write_text("external", encoding="utf-8")
    sentinel_before = sentinel.read_bytes()
    _create_directory_link(profiles_root / "zzz-unsafe", external_profile)

    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", tmp_path / "missing-legacy")
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "missing-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "missing-data")

    report = recordtest.run_preflight_checks(
        _preflight_config(managed_dir, tmp_path),
        auto_fix=True,
        ensure_dirs=True,
    )

    assert any("reparse point" in warning for warning in report["warnings"])
    assert global_ini.read_bytes() == global_before
    assert user_ini.read_bytes() == user_before
    assert safe_ini.read_bytes() == safe_before
    assert sentinel.read_bytes() == sentinel_before
    assert not (managed_dir / obs_bootstrap.PORTABLE_OBS_MARKER_NAME).exists()


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


def test_riot_api_fetches_post_game_result_from_lcu_end_of_game_stats():
    connection = LCUConnectionInfo(port=54321, password="secret")
    provider = SimpleNamespace(
        get_connection_info=lambda: connection,
        invalidate=lambda: None,
    )
    eog_url = f"{connection.base_url}{recordtest.LCU_END_OF_GAME_STATS_PATH}"
    routes = {
        eog_url: {
            "localPlayer": {"summonerName": "Tester#JP1", "teamId": 200},
            "teams": [
                {"teamId": 100, "win": "Fail"},
                {"teamId": 200, "win": "Win"},
            ],
        },
    }
    client = recordtest.LiveClientRiotAPIClient(
        session_factory=FakeSessionFactory(routes),
        lcu_connection_provider=provider,
    )

    result = run(client.get_post_game_result(player_name="Tester#JP1", player_team=None))

    assert result.status == recordtest.RiotPollStatus.IN_GAME
    assert result.payload == {
        "game_result": "Win",
        "winning_team": "CHAOS",
        "player_team": "CHAOS",
        "source": "lcu_end_of_game",
    }


def test_riot_api_falls_back_to_gameflow_session_for_post_game_result():
    connection = LCUConnectionInfo(port=54321, password="secret")
    provider = SimpleNamespace(
        get_connection_info=lambda: connection,
        invalidate=lambda: None,
    )
    gameflow_url = f"{connection.base_url}{recordtest.LCU_GAMEFLOW_SESSION_PATH}"
    routes = {
        gameflow_url: {
            "phase": "EndOfGame",
            "gameData": {
                "teams": [
                    {"teamId": 100, "isWinningTeam": True},
                    {"teamId": 200, "isWinningTeam": False},
                ]
            },
        },
    }
    client = recordtest.LiveClientRiotAPIClient(
        session_factory=FakeSessionFactory(routes),
        lcu_connection_provider=provider,
    )

    result = run(client.get_post_game_result(player_name="Tester#JP1", player_team="CHAOS"))

    assert result.status == recordtest.RiotPollStatus.IN_GAME
    assert result.payload["game_result"] == "Loss"
    assert result.payload["winning_team"] == "ORDER"
    assert result.payload["player_team"] == "CHAOS"
    assert result.payload["source"] == "lcu_gameflow_session"


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


def test_obs_connection_disconnects_falsey_client_once(monkeypatch):
    disconnect_calls = []

    class FalseyClient:
        def __bool__(self):
            return False

        def get_version(self):
            return SimpleNamespace(obs_version="31.0.0")

        def disconnect(self):
            disconnect_calls.append("disconnect")

    client = FalseyClient()
    monkeypatch.setattr(recordtest.obs, "ReqClient", lambda **_kwargs: client)

    ok, detail = recordtest.test_obs_connection("example.test", 4455, "secret")

    assert ok is True
    assert "31.0.0" in detail
    assert disconnect_calls == ["disconnect"]


def test_obs_connection_keeps_primary_control_flow_when_disconnect_fails(monkeypatch):
    primary_error = KeyboardInterrupt("connection check interrupted")
    primary_cause = RuntimeError("connection cause")
    primary_context = ValueError("connection context")
    primary_error.__cause__ = primary_cause
    primary_error.__context__ = primary_context
    primary_error.__suppress_context__ = True
    disconnect_calls = []

    class Client:
        def get_version(self):
            raise primary_error

        def disconnect(self):
            disconnect_calls.append("disconnect")
            raise OSError("disconnect failed")

    monkeypatch.setattr(recordtest.obs, "ReqClient", lambda **_kwargs: Client())

    with pytest.raises(KeyboardInterrupt) as captured:
        recordtest.test_obs_connection("example.test", 4455, "secret")

    assert captured.value is primary_error
    assert primary_error.__cause__ is primary_cause
    assert primary_error.__context__ is primary_context
    assert primary_error.__suppress_context__ is True
    assert disconnect_calls == ["disconnect"]
    assert any(
        "disconnect failed" in note
        for note in getattr(primary_error, "__notes__", [])
    )


def test_obs_connection_cleanup_control_flow_supersedes_normal_failure(monkeypatch):
    primary_error = OSError("connection failed")
    cleanup_error = SystemExit("disconnect interrupted")
    cleanup_cause = RuntimeError("disconnect cause")
    cleanup_context = ValueError("disconnect context")
    cleanup_error.__cause__ = cleanup_cause
    cleanup_error.__context__ = cleanup_context
    cleanup_error.__suppress_context__ = True
    disconnect_calls = []

    class Client:
        def get_version(self):
            raise primary_error

        def disconnect(self):
            disconnect_calls.append("disconnect")
            raise cleanup_error

    monkeypatch.setattr(recordtest.obs, "ReqClient", lambda **_kwargs: Client())

    with pytest.raises(SystemExit) as captured:
        recordtest.test_obs_connection("example.test", 4455, "secret")

    assert captured.value is cleanup_error
    assert cleanup_error.__cause__ is cleanup_cause
    assert cleanup_error.__context__ is cleanup_context
    assert cleanup_error.__suppress_context__ is True
    assert disconnect_calls == ["disconnect"]
    assert any(
        "connection failed" in note
        for note in getattr(cleanup_error, "__notes__", [])
    )


def test_obs_connection_keeps_first_control_flow_across_disconnect(monkeypatch):
    primary_error = asyncio.CancelledError("connection cancelled")
    cleanup_error = SystemExit("disconnect interrupted")
    disconnect_calls = []

    class Client:
        def get_version(self):
            raise primary_error

        def disconnect(self):
            disconnect_calls.append("disconnect")
            raise cleanup_error

    monkeypatch.setattr(recordtest.obs, "ReqClient", lambda **_kwargs: Client())

    with pytest.raises(asyncio.CancelledError) as captured:
        recordtest.test_obs_connection("example.test", 4455, "secret")

    assert captured.value is primary_error
    assert disconnect_calls == ["disconnect"]
    assert any(
        "disconnect interrupted" in note
        for note in getattr(primary_error, "__notes__", [])
    )


def test_obs_connection_keeps_typed_failure_and_reports_disconnect_failure(
    monkeypatch,
):
    primary_error = OSError("connection failed")
    disconnect_calls = []
    log_calls = []

    class Client:
        def get_version(self):
            raise primary_error

        def disconnect(self):
            disconnect_calls.append("disconnect")
            raise TimeoutError("disconnect failed")

    monkeypatch.setattr(recordtest.obs, "ReqClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        recordtest.LOGGER,
        "error",
        lambda *args, **_kwargs: log_calls.append(args),
    )

    ok, detail = recordtest.test_obs_connection("example.test", 4455, "secret")

    assert ok is False
    assert "connection failed" in detail
    assert disconnect_calls == ["disconnect"]
    assert any(
        "disconnect failed" in " ".join(str(value) for value in args)
        for args in log_calls
    )
    assert any(
        "disconnect failed" in note
        for note in getattr(primary_error, "__notes__", [])
    )


def test_obs_connection_reports_normal_disconnect_failure_after_success(
    monkeypatch,
):
    disconnect_calls = []
    log_calls = []

    class Client:
        def get_version(self):
            return SimpleNamespace(obs_version="31.0.0")

        def disconnect(self):
            disconnect_calls.append("disconnect")
            raise OSError("disconnect failed after success")

    monkeypatch.setattr(recordtest.obs, "ReqClient", lambda **_kwargs: Client())
    monkeypatch.setattr(
        recordtest.LOGGER,
        "error",
        lambda *args, **_kwargs: log_calls.append(args),
    )

    ok, detail = recordtest.test_obs_connection("example.test", 4455, "secret")

    assert ok is True
    assert "31.0.0" in detail
    assert disconnect_calls == ["disconnect"]
    assert any(
        "disconnect failed after success" in " ".join(str(value) for value in args)
        for args in log_calls
    )


def test_obs_disconnect_clears_raw_client_even_when_socket_errors():
    class BrokenRawClient:
        def disconnect(self):
            raise TimeoutError("socket already closed")

    client = recordtest.ObsWebSocketClient(config=app_config())
    client.client = BrokenRawClient()

    with pytest.raises(TimeoutError):
        client.disconnect()

    assert client.raw_client is None


def test_obs_disconnect_clears_and_disconnects_falsey_raw_client():
    disconnect_calls = []

    class FalseyRawClient:
        def __bool__(self):
            return False

        def disconnect(self):
            disconnect_calls.append("disconnect")

    client = recordtest.ObsWebSocketClient(config=app_config())
    client.client = FalseyRawClient()

    client.disconnect()

    assert disconnect_calls == ["disconnect"]
    assert client.raw_client is None


def test_obs_shutdown_disconnects_after_typed_process_termination_failure(
    monkeypatch,
):
    termination_error = OBSProcessTerminationError("owned handle remained live")
    disconnect_calls = []

    class FailingProcessManager:
        def __init__(self, *args, **kwargs):
            pass

        def terminate_process(self, process):
            raise termination_error

    class RawClient:
        def disconnect(self):
            disconnect_calls.append("disconnect")

    monkeypatch.setattr(recordtest, "OBSProcessManager", FailingProcessManager)
    client = recordtest.ObsWebSocketClient(
        config=app_config(),
        obs_process=SimpleNamespace(pid=101),
    )
    client.client = RawClient()

    with pytest.raises(OBSProcessTerminationError) as captured:
        client.shutdown()

    assert captured.value is termination_error
    assert disconnect_calls == ["disconnect"]
    assert client.raw_client is None


def test_obs_shutdown_keeps_termination_primary_and_notes_disconnect_failure(
    monkeypatch,
):
    termination_error = OBSProcessTerminationError("owned handle remained live")
    disconnect_calls = []

    class FailingProcessManager:
        def __init__(self, *args, **kwargs):
            pass

        def terminate_process(self, process):
            raise termination_error

    class BrokenRawClient:
        def disconnect(self):
            disconnect_calls.append("disconnect")
            raise OSError("websocket disconnect failed")

    monkeypatch.setattr(recordtest, "OBSProcessManager", FailingProcessManager)
    client = recordtest.ObsWebSocketClient(
        config=app_config(),
        obs_process=SimpleNamespace(pid=101),
    )
    client.client = BrokenRawClient()

    with pytest.raises(OBSProcessTerminationError) as captured:
        client.shutdown()

    assert captured.value is termination_error
    assert disconnect_calls == ["disconnect"]
    assert any(
        "websocket disconnect failed" in note
        for note in captured.value.__notes__
    )
    assert client.raw_client is None


def test_obs_shutdown_disconnect_control_flow_supersedes_normal_termination_failure(
    monkeypatch,
):
    termination_error = OBSProcessTerminationError("owned handle remained live")
    disconnect_error = SystemExit("websocket disconnect interrupted")
    disconnect_cause = RuntimeError("disconnect cause")
    disconnect_error.__cause__ = disconnect_cause
    disconnect_error.__suppress_context__ = True
    disconnect_calls = []

    class FailingProcessManager:
        def __init__(self, *args, **kwargs):
            pass

        def terminate_process(self, process):
            raise termination_error

    class BrokenRawClient:
        def disconnect(self):
            disconnect_calls.append("disconnect")
            raise disconnect_error

    monkeypatch.setattr(recordtest, "OBSProcessManager", FailingProcessManager)
    client = recordtest.ObsWebSocketClient(
        config=app_config(),
        obs_process=SimpleNamespace(pid=101),
    )
    client.client = BrokenRawClient()

    with pytest.raises(SystemExit) as captured:
        client.shutdown()

    assert captured.value is disconnect_error
    assert disconnect_error.__cause__ is disconnect_cause
    assert disconnect_error.__suppress_context__ is True
    assert disconnect_calls == ["disconnect"]
    assert any(
        "owned handle remained live" in note
        for note in getattr(disconnect_error, "__notes__", [])
    )
    assert client.raw_client is None


def test_obs_shutdown_keeps_first_control_flow_across_disconnect_cleanup(
    monkeypatch,
):
    termination_error = KeyboardInterrupt("process termination interrupted")
    disconnect_error = SystemExit("websocket disconnect interrupted")

    class FailingProcessManager:
        def __init__(self, *args, **kwargs):
            pass

        def terminate_process(self, process):
            raise termination_error

    class BrokenRawClient:
        def disconnect(self):
            raise disconnect_error

    monkeypatch.setattr(recordtest, "OBSProcessManager", FailingProcessManager)
    client = recordtest.ObsWebSocketClient(
        config=app_config(),
        obs_process=SimpleNamespace(pid=101),
    )
    client.client = BrokenRawClient()

    with pytest.raises(KeyboardInterrupt) as captured:
        client.shutdown()

    assert captured.value is termination_error
    assert any(
        "websocket disconnect interrupted" in note
        for note in getattr(termination_error, "__notes__", [])
    )


def test_obs_shutdown_retry_does_not_revalidate_terminated_process(monkeypatch):
    process = SimpleNamespace(pid=101)
    termination_calls = []
    disconnect_calls = []

    class ProcessManager:
        def __init__(self, *args, **kwargs):
            pass

        def terminate_process(self, candidate):
            termination_calls.append(candidate)

    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    client = recordtest.ObsWebSocketClient(
        config=app_config(),
        obs_process=process,
    )

    def flaky_disconnect():
        disconnect_calls.append("disconnect")
        if len(disconnect_calls) == 1:
            raise OSError("websocket disconnect failed")

    monkeypatch.setattr(client, "disconnect", flaky_disconnect)

    with pytest.raises(OSError, match="websocket disconnect failed"):
        client.shutdown()

    assert client.obs_process is None
    client.shutdown()

    assert termination_calls == [process]
    assert disconnect_calls == ["disconnect", "disconnect"]


def test_nonportable_launch_keeps_detection_primary_and_notes_cleanup_failure(
    monkeypatch,
):
    process = SimpleNamespace(pid=101)
    identity = obs_bootstrap.OBSProcessInfo(
        pid=101,
        executable_path=Path("C:/managed/obs/bin/64bit/obs64.exe"),
        creation_time=1000.0,
        creation_time_filetime=116_444_746_000_000_000,
    )

    class ProcessManager:
        def isolated_env(self):
            return {}

        def start_obs(self, **kwargs):
            return process

        def hide_main_windows(self, *args, **kwargs):
            return 0

        def latest_log_portable_mode(self, since=None):
            return False

        def terminate_process(self, candidate):
            assert candidate is process
            raise OBSProcessTerminationError("owned handle remained live")

    manager = ProcessManager()
    monkeypatch.setattr(
        recordtest,
        "_capture_started_obs_process_identity",
        lambda *args, **kwargs: identity,
    )
    monkeypatch.setattr(recordtest.time, "sleep", lambda *args: None)

    with pytest.raises(recordtest.RecorderError, match="ポータブルモードではなく") as captured:
        recordtest._start_hidden_obs_and_verify_portable(
            manager,
            obs_dir_abs="C:/managed/obs",
            obs_exe="C:/managed/obs/bin/64bit/obs64.exe",
        )

    assert any("手動で終了" in note for note in captured.value.__notes__)
    assert any("owned handle remained live" in note for note in captured.value.__notes__)


def test_launch_log_query_failure_stops_same_handle_once_and_notes_cleanup_failure(
    monkeypatch,
):
    process = SimpleNamespace(pid=101)
    identity = obs_bootstrap.OBSProcessInfo(
        pid=101,
        executable_path=Path("C:/managed/obs/bin/64bit/obs64.exe"),
        creation_time=1000.0,
        creation_time_filetime=116_444_746_000_000_000,
    )
    log_error = PermissionError("latest OBS log stat failed")
    cleanup_error = OBSProcessTerminationError("owned handle remained live")
    terminate_calls = []

    class ProcessManager:
        def isolated_env(self):
            return {}

        def start_obs(self, **kwargs):
            return process

        def hide_main_windows(self, *args, **kwargs):
            return 0

        def latest_log_portable_mode(self, since=None):
            raise log_error

        def terminate_process(self, candidate):
            terminate_calls.append(candidate)
            raise cleanup_error

    manager = ProcessManager()
    monkeypatch.setattr(
        recordtest,
        "_capture_started_obs_process_identity",
        lambda *args, **kwargs: identity,
    )
    monkeypatch.setattr(recordtest.time, "sleep", lambda *args: None)

    with pytest.raises(recordtest.RecorderError, match="起動検証") as captured:
        recordtest._start_hidden_obs_and_verify_portable(
            manager,
            obs_dir_abs="C:/managed/obs",
            obs_exe="C:/managed/obs/bin/64bit/obs64.exe",
        )

    assert captured.value.__cause__ is log_error
    assert terminate_calls == [process]
    assert any("手動で終了" in note for note in captured.value.__notes__)
    assert any("owned handle remained live" in note for note in captured.value.__notes__)


@pytest.mark.parametrize(
    "error_type",
    [KeyboardInterrupt, SystemExit, asyncio.CancelledError],
)
def test_started_identity_control_flow_is_pure_validation_and_preserves_object(
    error_type,
):
    process = SimpleNamespace(pid=101)
    primary_error = error_type("identity capture interrupted")
    terminate_calls = []

    class Manager:
        def query_obs_processes_strict(self):
            raise primary_error

        def terminate_process(self, candidate):
            terminate_calls.append(candidate)

    with pytest.raises(error_type) as captured:
        recordtest._capture_started_obs_process_identity(Manager(), process)

    assert captured.value is primary_error
    assert terminate_calls == []


def test_started_identity_return_boundary_interruption_cleans_handle_once(
    monkeypatch,
):
    process = SimpleNamespace(pid=101)
    identity = obs_bootstrap.OBSProcessInfo(
        pid=101,
        executable_path=Path("C:/managed/obs/bin/64bit/obs64.exe"),
        creation_time=1000.0,
        creation_time_filetime=116_444_746_000_000_000,
    )
    primary_error = KeyboardInterrupt("identity handoff interrupted")
    terminate_calls = []

    class Manager:
        def isolated_env(self):
            return {}

        def start_obs(self, **kwargs):
            return process

        def hide_main_windows(self, *args, **kwargs):
            pytest.fail("interruption must happen before portable verification")

        def terminate_process(self, candidate):
            terminate_calls.append(candidate)

    monkeypatch.setattr(
        recordtest,
        "_capture_started_obs_process_identity",
        lambda *args, **kwargs: identity,
    )
    source_lines, first_line = inspect.getsourcelines(
        recordtest._start_hidden_obs_and_verify_portable
    )
    target_line = first_line + next(
        index
        for index, line in enumerate(source_lines)
        if "hidden_windows =" in line
    )
    previous_trace = sys.gettrace()

    def interrupt_after_identity_assignment(frame, event, arg):
        if (
            frame.f_code
            is recordtest._start_hidden_obs_and_verify_portable.__code__
            and event == "line"
            and frame.f_lineno == target_line
        ):
            sys.settrace(previous_trace)
            raise primary_error
        return interrupt_after_identity_assignment

    sys.settrace(interrupt_after_identity_assignment)
    try:
        with pytest.raises(KeyboardInterrupt) as captured:
            recordtest._start_hidden_obs_and_verify_portable(
                Manager(),
                obs_dir_abs="C:/managed/obs",
                obs_exe="C:/managed/obs/bin/64bit/obs64.exe",
            )
    finally:
        sys.settrace(previous_trace)

    assert captured.value is primary_error
    assert terminate_calls == [process]


def test_portable_verification_control_flow_keeps_primary_and_notes_cleanup(
    monkeypatch,
):
    process = SimpleNamespace(pid=101)
    primary_error = SystemExit("window verification interrupted")
    cleanup_error = OBSProcessTerminationError("owned handle remained live")
    terminate_calls = []
    identity = obs_bootstrap.OBSProcessInfo(
        pid=101,
        executable_path=Path("C:/managed/obs/bin/64bit/obs64.exe"),
        creation_time=1000.0,
        creation_time_filetime=116_444_746_000_000_000,
    )

    class Manager:
        def isolated_env(self):
            return {}

        def start_obs(self, **kwargs):
            return process

        def hide_main_windows(self, *args, **kwargs):
            raise primary_error

        def terminate_process(self, candidate):
            terminate_calls.append(candidate)
            raise cleanup_error

    monkeypatch.setattr(
        recordtest,
        "_capture_started_obs_process_identity",
        lambda *args, **kwargs: identity,
    )

    with pytest.raises(SystemExit) as captured:
        recordtest._start_hidden_obs_and_verify_portable(
            Manager(),
            obs_dir_abs="C:/managed/obs",
            obs_exe="C:/managed/obs/bin/64bit/obs64.exe",
        )

    assert captured.value is primary_error
    assert terminate_calls == [process]
    assert any("owned handle remained live" in note for note in primary_error.__notes__)


def test_portable_verification_cleanup_control_flow_supersedes_normal_failure(
    monkeypatch,
):
    process = SimpleNamespace(pid=101)
    identity = obs_bootstrap.OBSProcessInfo(
        pid=101,
        executable_path=Path("C:/managed/obs/bin/64bit/obs64.exe"),
        creation_time=1000.0,
        creation_time_filetime=116_444_746_000_000_000,
    )
    verification_error = OSError("portable log query failed")
    cleanup_error = SystemExit("portable cleanup interrupted")
    cleanup_cause = RuntimeError("cleanup cause")
    cleanup_error.__cause__ = cleanup_cause
    cleanup_error.__suppress_context__ = True
    terminate_calls = []

    class Manager:
        def isolated_env(self):
            return {}

        def start_obs(self, **kwargs):
            return process

        def hide_main_windows(self, *args, **kwargs):
            return 0

        def latest_log_portable_mode(self, since=None):
            raise verification_error

        def terminate_process(self, candidate):
            terminate_calls.append(candidate)
            raise cleanup_error

    monkeypatch.setattr(
        recordtest,
        "_capture_started_obs_process_identity",
        lambda *args, **kwargs: identity,
    )
    monkeypatch.setattr(recordtest.time, "sleep", lambda *args: None)

    with pytest.raises(SystemExit) as captured:
        recordtest._start_hidden_obs_and_verify_portable(
            Manager(),
            obs_dir_abs="C:/managed/obs",
            obs_exe="C:/managed/obs/bin/64bit/obs64.exe",
        )

    assert captured.value is cleanup_error
    assert cleanup_error.__cause__ is cleanup_cause
    assert cleanup_error.__suppress_context__ is True
    assert terminate_calls == [process]
    assert any(
        "portable log query failed" in note
        for note in getattr(cleanup_error, "__notes__", [])
    )


def test_started_handle_cleanup_propagates_manager_error_even_if_poll_says_exited():
    process = SimpleNamespace(pid=101, poll=lambda: 0)
    termination_error = OBSProcessTerminationError("poll failed before exit proof")

    class Manager:
        def terminate_process(self, candidate):
            assert candidate is process
            raise termination_error

    with pytest.raises(recordtest.RecorderError) as captured:
        recordtest._ensure_started_obs_handle_stopped(Manager(), process)

    assert captured.value.__cause__ is termination_error


def test_setup_obs_sync_elements_propagates_success_path_cleanup_failure(
    monkeypatch,
    tmp_path,
):
    launched_process = SimpleNamespace(pid=101)
    cleanup_error = OBSProcessTerminationError("owned handle remained live")
    shutdown_calls = []

    class ProcessManager:
        def __init__(self, *args, **kwargs):
            pass

    class Recorder:
        _open_cleanup_attempted = False

        def __init__(self, *args, **kwargs):
            pass

        def open(self):
            pass

        def apply_record_output_settings(self):
            pass

        def apply_audio_profile(self, cfg):
            pass

        def shutdown_obs(self):
            shutdown_calls.append("shutdown")
            raise cleanup_error

    monkeypatch.setattr(recordtest, "ensure_recording_dirs", lambda config: None)
    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: launched_process)
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", Recorder)

    with pytest.raises(OBSProcessTerminationError) as captured:
        recordtest._setup_obs_sync_elements_locked(
            {
                "obs": {"obs_dir": str(tmp_path / "obs-portable")},
                "paths": {
                    "recordings_dir": str(tmp_path / "recordings"),
                    "json_dir": str(tmp_path / "json"),
                },
            }
        )

    assert captured.value is cleanup_error
    assert shutdown_calls == ["shutdown"]


def test_setup_obs_sync_elements_reports_falsey_launched_process(monkeypatch, tmp_path):
    shutdown_calls = []

    class FalseyProcess:
        pid = 101

        def __bool__(self):
            return False

    launched_process = FalseyProcess()

    class ProcessManager:
        def __init__(self, *args, **kwargs):
            pass

    class Recorder:
        _open_cleanup_attempted = False

        def __init__(self, *args, **kwargs):
            pass

        def open(self):
            pass

        def apply_record_output_settings(self):
            pass

        def apply_audio_profile(self, cfg):
            pass

        def shutdown_obs(self):
            shutdown_calls.append("shutdown")

    monkeypatch.setattr(recordtest, "ensure_recording_dirs", lambda config: None)
    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: launched_process)
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", Recorder)

    result = recordtest._setup_obs_sync_elements_locked(
        {
            "obs": {"obs_dir": str(tmp_path / "obs-portable")},
            "paths": {
                "recordings_dir": str(tmp_path / "recordings"),
                "json_dir": str(tmp_path / "json"),
            },
        }
    )

    assert result["obs_launched"] is True
    assert shutdown_calls == ["shutdown"]


def test_setup_obs_sync_elements_keeps_body_primary_and_notes_cleanup_failure(
    monkeypatch,
    tmp_path,
):
    launched_process = SimpleNamespace(pid=101)
    primary_error = recordtest.RecorderError("setup body failed")
    cleanup_error = OBSProcessTerminationError("owned handle remained live")
    shutdown_calls = []

    class ProcessManager:
        def __init__(self, *args, **kwargs):
            pass

    class Recorder:
        def __init__(self, *args, **kwargs):
            pass

        def open(self):
            raise primary_error

        def shutdown_obs(self):
            shutdown_calls.append("shutdown")
            raise cleanup_error

    monkeypatch.setattr(recordtest, "ensure_recording_dirs", lambda config: None)
    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: launched_process)
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", Recorder)

    with pytest.raises(recordtest.RecorderError) as captured:
        recordtest._setup_obs_sync_elements_locked(
            {
                "obs": {"obs_dir": str(tmp_path / "obs-portable")},
                "paths": {
                    "recordings_dir": str(tmp_path / "recordings"),
                    "json_dir": str(tmp_path / "json"),
                },
            }
        )

    assert captured.value is primary_error
    assert shutdown_calls == ["shutdown"]
    assert any("owned handle remained live" in note for note in primary_error.__notes__)


def test_setup_obs_sync_cleanup_control_flow_supersedes_normal_body_failure(
    monkeypatch,
    tmp_path,
):
    launched_process = SimpleNamespace(pid=101)
    primary_error = recordtest.RecorderError("setup body failed")
    cleanup_error = asyncio.CancelledError("setup cleanup cancelled")

    class ProcessManager:
        def __init__(self, *args, **kwargs):
            pass

    class Recorder:
        _open_cleanup_attempted = False

        def __init__(self, *args, **kwargs):
            pass

        def open(self):
            raise primary_error

        def shutdown_obs(self):
            raise cleanup_error

    monkeypatch.setattr(recordtest, "ensure_recording_dirs", lambda config: None)
    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: launched_process)
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", Recorder)

    with pytest.raises(asyncio.CancelledError) as captured:
        recordtest._setup_obs_sync_elements_locked(
            {
                "obs": {"obs_dir": str(tmp_path / "obs-portable")},
                "paths": {
                    "recordings_dir": str(tmp_path / "recordings"),
                    "json_dir": str(tmp_path / "json"),
                },
            }
        )

    assert captured.value is cleanup_error
    assert any(
        "setup body failed" in note
        for note in getattr(cleanup_error, "__notes__", [])
    )


def test_recorder_open_keeps_primary_and_marks_cleanup_attempt(monkeypatch, tmp_path):
    primary_error = recordtest.RecorderError("connect failed")
    cleanup_error = OBSProcessTerminationError("owned handle remained live")
    shutdown_calls = []

    class Client:
        obs_process = SimpleNamespace(pid=101)

        def connect(self):
            raise primary_error

        def shutdown(self):
            shutdown_calls.append("shutdown")
            raise cleanup_error

    recorder = recordtest.LoLAutoRecorder(
        config=recordtest.AppConfig.from_dict(
            {
                "obs": {"obs_dir": str(tmp_path / "obs-portable")},
                "paths": {
                    "recordings_dir": str(tmp_path / "recordings"),
                    "json_dir": str(tmp_path / "json"),
                },
            }
        ),
        obs_client=Client(),
        auto_setup=False,
    )

    with pytest.raises(recordtest.RecorderError) as captured:
        recorder.open()

    assert captured.value is primary_error
    assert shutdown_calls == ["shutdown"]
    assert recorder._open_cleanup_attempted is True
    assert any("owned handle remained live" in note for note in primary_error.__notes__)


def test_recorder_open_cleans_the_same_falsey_obs_client_once(tmp_path):
    primary_error = recordtest.RecorderError("connect failed")
    calls = []

    class FalseyClient:
        obs_process = SimpleNamespace(pid=101)

        def __bool__(self):
            return False

        def connect(self):
            calls.append("connect")
            raise primary_error

        def shutdown(self):
            calls.append("shutdown")

    client = FalseyClient()
    recorder = recordtest.LoLAutoRecorder(
        config=recordtest.AppConfig.from_dict(
            {
                "obs": {"obs_dir": str(tmp_path / "obs-portable")},
                "paths": {
                    "recordings_dir": str(tmp_path / "recordings"),
                    "json_dir": str(tmp_path / "json"),
                },
            }
        ),
        obs_client=client,
        auto_setup=False,
    )

    with pytest.raises(recordtest.RecorderError) as captured:
        recorder.open()

    assert captured.value is primary_error
    assert recorder.obs_client is client
    assert calls == ["connect", "shutdown"]
    assert recorder._open_cleanup_attempted is True


def test_recorder_open_cleanup_control_flow_supersedes_normal_startup_failure(
    tmp_path,
):
    primary_error = recordtest.RecorderError("connect failed")
    cleanup_error = SystemExit("recorder cleanup interrupted")
    cleanup_cause = RuntimeError("recorder cleanup cause")
    cleanup_error.__cause__ = cleanup_cause
    cleanup_error.__suppress_context__ = True

    class Client:
        obs_process = SimpleNamespace(pid=101)

        def connect(self):
            raise primary_error

        def shutdown(self):
            raise cleanup_error

    recorder = recordtest.LoLAutoRecorder(
        config=recordtest.AppConfig.from_dict(
            {
                "obs": {"obs_dir": str(tmp_path / "obs-portable")},
                "paths": {
                    "recordings_dir": str(tmp_path / "recordings"),
                    "json_dir": str(tmp_path / "json"),
                },
            }
        ),
        obs_client=Client(),
        auto_setup=False,
    )

    with pytest.raises(SystemExit) as captured:
        recorder.open()

    assert captured.value is cleanup_error
    assert cleanup_error.__cause__ is cleanup_cause
    assert cleanup_error.__suppress_context__ is True
    assert recorder._open_cleanup_attempted is True
    assert any(
        "connect failed" in note
        for note in getattr(cleanup_error, "__notes__", [])
    )


@pytest.mark.parametrize(
    "error_type",
    [KeyboardInterrupt, SystemExit, asyncio.CancelledError],
)
def test_recorder_open_preserves_control_flow_and_runs_cleanup_once(
    tmp_path,
    error_type,
):
    primary_error = error_type("recorder startup interrupted")
    cleanup_error = OBSProcessTerminationError("owned handle remained live")
    shutdown_calls = []

    class Client:
        obs_process = SimpleNamespace(pid=101)

        def connect(self):
            raise primary_error

        def shutdown(self):
            shutdown_calls.append("shutdown")
            raise cleanup_error

    recorder = recordtest.LoLAutoRecorder(
        config=recordtest.AppConfig.from_dict(
            {
                "obs": {"obs_dir": str(tmp_path / "obs-portable")},
                "paths": {
                    "recordings_dir": str(tmp_path / "recordings"),
                    "json_dir": str(tmp_path / "json"),
                },
            }
        ),
        obs_client=Client(),
        auto_setup=False,
    )

    with pytest.raises(error_type) as captured:
        recorder.open()

    assert captured.value is primary_error
    assert recorder._open_cleanup_attempted is True
    assert shutdown_calls == ["shutdown"]
    assert any("owned handle remained live" in note for note in primary_error.__notes__)


def test_setup_borrowed_open_interruption_does_not_disconnect_twice(
    monkeypatch,
    tmp_path,
):
    primary_error = KeyboardInterrupt("borrowed recorder open interrupted")
    disconnect_calls = []

    class ProcessManager:
        def __init__(self, *args, **kwargs):
            pass

    class Recorder:
        def __init__(self, *args, **kwargs):
            self._open_cleanup_attempted = False

        def open(self):
            self._open_cleanup_attempted = True
            disconnect_calls.append("disconnect")
            raise primary_error

        def disconnect_obs(self):
            disconnect_calls.append("disconnect")

    monkeypatch.setattr(recordtest, "ensure_recording_dirs", lambda config: None)
    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", Recorder)

    with pytest.raises(KeyboardInterrupt) as captured:
        recordtest._setup_obs_sync_elements_locked(
            {
                "obs": {"obs_dir": str(tmp_path / "obs-portable")},
                "paths": {
                    "recordings_dir": str(tmp_path / "recordings"),
                    "json_dir": str(tmp_path / "json"),
                },
            },
            auto_launch=False,
        )

    assert captured.value is primary_error
    assert disconnect_calls == ["disconnect"]


def test_recorder_disconnect_cleans_state_when_client_disconnect_fails(tmp_path):
    disconnect_error = RuntimeError("websocket disconnect failed")

    class Client:
        obs_process = None

        def disconnect(self):
            raise disconnect_error

    recorder = recordtest.LoLAutoRecorder(
        config=recordtest.AppConfig.from_dict(
            {
                "obs": {"obs_dir": str(tmp_path / "obs-portable")},
                "paths": {
                    "recordings_dir": str(tmp_path / "recordings"),
                    "json_dir": str(tmp_path / "json"),
                },
            }
        ),
        obs_client=Client(),
        status_cb=lambda _message: None,
        auto_setup=False,
    )
    recorder.opened = True
    assert recorder._status_handler is not None

    with pytest.raises(RuntimeError) as captured:
        recorder.disconnect_obs()

    assert captured.value is disconnect_error
    assert recorder.opened is False
    assert recorder._status_handler is None


def test_cli_recorder_keeps_body_primary_when_shutdown_fails(monkeypatch):
    primary_error = OSError("recording wait failed")
    cleanup_error = OBSProcessTerminationError("owned OBS cleanup failed")
    calls = []

    class App:
        def open(self):
            calls.append("open")

        def apply_audio_profile(self, config):
            calls.append("apply_audio_profile")

        def reset_session(self):
            calls.append("reset_session")

        async def wait_for_game_start_async(self):
            calls.append("wait_for_game_start_async")
            raise primary_error

        def stop_recording(self):
            calls.append("stop_recording")

        def shutdown_obs(self):
            calls.append("shutdown_obs")
            raise cleanup_error

    app = App()
    config = SimpleNamespace()
    monkeypatch.setattr(recordtest, "load_settings", lambda: {})
    monkeypatch.setattr(
        recordtest,
        "run_preflight_checks",
        lambda *args, **kwargs: {
            "config": {},
            "changed": False,
            "warnings": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        recordtest.AppConfig,
        "from_dict",
        classmethod(lambda cls, data: config),
    )
    monkeypatch.setattr(recordtest, "setup_environment", lambda candidate: None)
    monkeypatch.setattr(recordtest, "launch_obs", lambda candidate: SimpleNamespace(pid=101))
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda **kwargs: app)

    with pytest.raises(OSError) as captured:
        asyncio.run(recordtest.run_cli_recorder())

    assert captured.value is primary_error
    assert calls == [
        "open",
        "apply_audio_profile",
        "reset_session",
        "wait_for_game_start_async",
        "stop_recording",
        "shutdown_obs",
    ]
    assert any("owned OBS cleanup failed" in note for note in primary_error.__notes__)
    assert any("手動で終了" in note for note in primary_error.__notes__)


def test_cli_cleanup_control_flow_supersedes_normal_body_failure():
    primary_error = OSError("recording wait failed")
    cleanup_error = SystemExit("shutdown interrupted")
    cleanup_cause = RuntimeError("shutdown cause")
    cleanup_error.__cause__ = cleanup_cause
    cleanup_error.__suppress_context__ = True

    class App:
        def stop_recording(self):
            pass

        def shutdown_obs(self):
            raise cleanup_error

    with pytest.raises(SystemExit) as captured:
        recordtest._cleanup_cli_recorder(App(), primary_error)

    assert captured.value is cleanup_error
    assert cleanup_error.__cause__ is cleanup_cause
    assert cleanup_error.__suppress_context__ is True
    assert any(
        "recording wait failed" in note
        for note in getattr(cleanup_error, "__notes__", [])
    )
    assert any(
        "手動で終了" in note
        for note in getattr(cleanup_error, "__notes__", [])
    )


def test_cli_cleanup_keeps_first_control_flow_and_attempts_later_cleanup():
    primary_error = OSError("recording wait failed")
    first_cleanup_error = SystemExit("stop recording interrupted")
    later_cleanup_error = KeyboardInterrupt("shutdown interrupted")
    calls = []

    class App:
        def stop_recording(self):
            calls.append("stop")
            raise first_cleanup_error

        def shutdown_obs(self):
            calls.append("shutdown")
            raise later_cleanup_error

    with pytest.raises(SystemExit) as captured:
        recordtest._cleanup_cli_recorder(App(), primary_error)

    assert captured.value is first_cleanup_error
    assert calls == ["stop", "shutdown"]
    notes = getattr(first_cleanup_error, "__notes__", [])
    assert any("recording wait failed" in note for note in notes)
    assert any("shutdown interrupted" in note for note in notes)


def test_cli_constructor_failure_cleans_raw_process_once_and_keeps_primary(monkeypatch):
    primary_error = OSError("recorder constructor failed")
    cleanup_error = OBSProcessTerminationError("owned OBS cleanup failed")
    process = SimpleNamespace(pid=101)
    cleanup_calls = []
    config = SimpleNamespace(obs=SimpleNamespace(obs_dir="C:/managed/obs"))

    class ProcessManager:
        def __init__(self, obs_dir, logger=None):
            assert obs_dir == config.obs.obs_dir

        def terminate_process(self, candidate):
            cleanup_calls.append(candidate)
            raise cleanup_error

    monkeypatch.setattr(recordtest, "load_settings", lambda: {})
    monkeypatch.setattr(
        recordtest,
        "run_preflight_checks",
        lambda *args, **kwargs: {
            "config": {},
            "changed": False,
            "warnings": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        recordtest.AppConfig,
        "from_dict",
        classmethod(lambda cls, data: config),
    )
    monkeypatch.setattr(recordtest, "setup_environment", lambda candidate: None)
    monkeypatch.setattr(recordtest, "launch_obs", lambda candidate: process)
    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(
        recordtest,
        "LoLAutoRecorder",
        lambda **kwargs: (_ for _ in ()).throw(primary_error),
    )

    with pytest.raises(OSError) as captured:
        asyncio.run(recordtest.run_cli_recorder())

    assert captured.value is primary_error
    assert cleanup_calls == [process]
    assert any("owned OBS cleanup failed" in note for note in primary_error.__notes__)
    assert any("手動で終了" in note for note in primary_error.__notes__)


def test_cli_constructor_control_flow_cleans_raw_process_once(monkeypatch):
    primary_error = asyncio.CancelledError("recorder constructor cancelled")
    process = SimpleNamespace(pid=101)
    cleanup_calls = []
    config = SimpleNamespace(obs=SimpleNamespace(obs_dir="C:/managed/obs"))

    class ProcessManager:
        def __init__(self, obs_dir, logger=None):
            assert obs_dir == config.obs.obs_dir

        def terminate_process(self, candidate):
            cleanup_calls.append(candidate)

    monkeypatch.setattr(recordtest, "load_settings", lambda: {})
    monkeypatch.setattr(
        recordtest,
        "run_preflight_checks",
        lambda *args, **kwargs: {
            "config": {},
            "changed": False,
            "warnings": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        recordtest.AppConfig,
        "from_dict",
        classmethod(lambda cls, data: config),
    )
    monkeypatch.setattr(recordtest, "setup_environment", lambda candidate: None)
    monkeypatch.setattr(recordtest, "launch_obs", lambda candidate: process)
    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(
        recordtest,
        "LoLAutoRecorder",
        lambda **kwargs: (_ for _ in ()).throw(primary_error),
    )

    with pytest.raises(asyncio.CancelledError) as captured:
        asyncio.run(recordtest.run_cli_recorder())

    assert captured.value is primary_error
    assert cleanup_calls == [process]


def test_cli_constructor_cleanup_control_flow_supersedes_normal_failure(
    monkeypatch,
):
    primary_error = OSError("recorder constructor failed")
    cleanup_error = SystemExit("raw process cleanup interrupted")
    process = SimpleNamespace(pid=101)
    cleanup_calls = []
    config = SimpleNamespace(obs=SimpleNamespace(obs_dir="C:/managed/obs"))

    class ProcessManager:
        def __init__(self, obs_dir, logger=None):
            assert obs_dir == config.obs.obs_dir

        def terminate_process(self, candidate):
            cleanup_calls.append(candidate)
            raise cleanup_error

    monkeypatch.setattr(recordtest, "load_settings", lambda: {})
    monkeypatch.setattr(
        recordtest,
        "run_preflight_checks",
        lambda *args, **kwargs: {
            "config": {},
            "changed": False,
            "warnings": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        recordtest.AppConfig,
        "from_dict",
        classmethod(lambda cls, data: config),
    )
    monkeypatch.setattr(recordtest, "setup_environment", lambda candidate: None)
    monkeypatch.setattr(recordtest, "launch_obs", lambda candidate: process)
    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(
        recordtest,
        "LoLAutoRecorder",
        lambda **kwargs: (_ for _ in ()).throw(primary_error),
    )

    with pytest.raises(SystemExit) as captured:
        asyncio.run(recordtest.run_cli_recorder())

    assert captured.value is cleanup_error
    assert cleanup_calls == [process]
    assert any(
        "recorder constructor failed" in note
        for note in getattr(cleanup_error, "__notes__", [])
    )


def test_cli_open_cleanup_is_not_repeated_by_outer_finally(monkeypatch):
    primary_error = OSError("recorder open failed")
    calls = []
    config = SimpleNamespace()

    class App:
        _open_cleanup_attempted = False

        def open(self):
            calls.append("open")
            self._open_cleanup_attempted = True
            calls.append("shutdown_obs")
            raise primary_error

        def stop_recording(self):
            calls.append("stop_recording")

        def shutdown_obs(self):
            calls.append("shutdown_obs")

    app = App()
    monkeypatch.setattr(recordtest, "load_settings", lambda: {})
    monkeypatch.setattr(
        recordtest,
        "run_preflight_checks",
        lambda *args, **kwargs: {
            "config": {},
            "changed": False,
            "warnings": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        recordtest.AppConfig,
        "from_dict",
        classmethod(lambda cls, data: config),
    )
    monkeypatch.setattr(recordtest, "setup_environment", lambda candidate: None)
    monkeypatch.setattr(recordtest, "launch_obs", lambda candidate: SimpleNamespace(pid=101))
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda **kwargs: app)

    with pytest.raises(OSError) as captured:
        asyncio.run(recordtest.run_cli_recorder())

    assert captured.value is primary_error
    assert calls == ["open", "shutdown_obs"]


def test_cli_startup_keyboard_interrupt_is_reraised_after_single_open_cleanup(
    monkeypatch,
):
    primary_error = KeyboardInterrupt("startup interrupted")
    calls = []
    config = SimpleNamespace()

    class App:
        _open_cleanup_attempted = False

        def open(self):
            calls.append("open")
            self._open_cleanup_attempted = True
            calls.append("shutdown_obs")
            raise primary_error

        def shutdown_obs(self):
            calls.append("shutdown_obs")

    app = App()
    monkeypatch.setattr(recordtest, "load_settings", lambda: {})
    monkeypatch.setattr(
        recordtest,
        "run_preflight_checks",
        lambda *args, **kwargs: {
            "config": {},
            "changed": False,
            "warnings": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        recordtest.AppConfig,
        "from_dict",
        classmethod(lambda cls, data: config),
    )
    monkeypatch.setattr(recordtest, "setup_environment", lambda candidate: None)
    monkeypatch.setattr(recordtest, "launch_obs", lambda candidate: SimpleNamespace(pid=101))
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda **kwargs: app)

    with pytest.raises(KeyboardInterrupt) as captured:
        asyncio.run(recordtest.run_cli_recorder())

    assert captured.value is primary_error
    assert calls == ["open", "shutdown_obs"]


def test_cli_keyboard_interrupt_after_handoff_is_graceful(monkeypatch):
    calls = []
    config = SimpleNamespace()

    class App:
        _open_cleanup_attempted = False

        def open(self):
            calls.append("open")

        def apply_audio_profile(self, candidate):
            calls.append("apply_audio_profile")

        def reset_session(self):
            calls.append("reset_session")

        async def wait_for_game_start_async(self):
            calls.append("wait_for_game_start_async")
            raise KeyboardInterrupt("user pressed Ctrl+C")

        def stop_recording(self):
            calls.append("stop_recording")

        def shutdown_obs(self):
            calls.append("shutdown_obs")

    app = App()
    monkeypatch.setattr(recordtest, "load_settings", lambda: {})
    monkeypatch.setattr(
        recordtest,
        "run_preflight_checks",
        lambda *args, **kwargs: {
            "config": {},
            "changed": False,
            "warnings": [],
            "errors": [],
        },
    )
    monkeypatch.setattr(
        recordtest.AppConfig,
        "from_dict",
        classmethod(lambda cls, data: config),
    )
    monkeypatch.setattr(recordtest, "setup_environment", lambda candidate: None)
    monkeypatch.setattr(
        recordtest,
        "launch_obs",
        lambda candidate: SimpleNamespace(pid=101),
    )
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda **kwargs: app)

    asyncio.run(recordtest.run_cli_recorder())

    assert calls == [
        "open",
        "apply_audio_profile",
        "reset_session",
        "wait_for_game_start_async",
        "stop_recording",
        "shutdown_obs",
    ]


def test_cli_typed_preflight_error_still_exits_with_status_one(monkeypatch):
    monkeypatch.setattr(recordtest, "load_settings", lambda: {})
    monkeypatch.setattr(
        recordtest,
        "run_preflight_checks",
        lambda *args, **kwargs: {
            "config": {},
            "changed": False,
            "warnings": [],
            "errors": ["typed preflight failure"],
        },
    )

    with pytest.raises(SystemExit) as captured:
        asyncio.run(recordtest.run_cli_recorder())

    assert captured.value.code == 1


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
    class FakePopen:
        def __init__(self, pid, identity):
            self.pid = pid
            self.identity = identity
            self.returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    class FakeProcessManager:
        start_count = 0
        kill_count = 0
        encoder_log_calls = 0
        terminated_pids = []
        strict_signal_pids = []
        active_processes = {}
        process_handles = {}
        query_clock = 100.0

        def __init__(self, obs_dir_arg, logger=None):
            self.obs_dir = Path(obs_dir_arg)
            self.obs_exe = self.obs_dir / "bin" / "64bit" / "obs64.exe"

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            type(self).kill_count += 1
            killed = sorted(type(self).active_processes)
            type(self).active_processes.clear()
            return killed

        def terminate_expected_obs_processes_strict(self, expected):
            type(self).kill_count += 1
            current = tuple(type(self).active_processes.values())
            if {item.pid: item for item in current} != {
                item.pid: item for item in expected
            }:
                raise obs_bootstrap.OBSProcessQueryError(
                    "strict fake identity mismatch"
                )
            type(self).strict_signal_pids.extend(item.pid for item in expected)
            for item in expected:
                type(self).active_processes.pop(item.pid, None)
                process = type(self).process_handles.get(item.pid)
                if process is not None:
                    process.returncode = 0
            return obs_bootstrap.OBSStrictTerminationResult(
                expected,
                self.query_obs_processes_strict(),
            )

        def query_obs_processes_strict(self):
            type(self).query_clock += 1.0
            return obs_bootstrap.OBSProcessQuerySnapshot(
                processes=tuple(type(self).active_processes.values()),
                queried_at=type(self).query_clock,
            )

        def query_popen_process_identity(self, process):
            return process.identity

        def has_managed_process(self):
            return False

        def unmanaged_processes(self):
            return []

        def isolated_env(self):
            return {}

        def start_obs(self, *args, **kwargs):
            type(self).start_count += 1
            pid = 100 + type(self).start_count
            identity = obs_bootstrap.OBSProcessInfo(
                pid=pid,
                executable_path=self.obs_exe.absolute(),
                creation_time=1000.0 + pid,
                creation_time_filetime=(
                    116_444_736_000_000_000 + (1000 + pid) * 10_000_000
                ),
            )
            type(self).active_processes[pid] = identity
            process = FakePopen(pid, identity)
            type(self).process_handles[pid] = process
            return process

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
            process.returncode = 0
            if type(self).active_processes.get(process.pid) == process.identity:
                type(self).active_processes.pop(process.pid, None)

    monkeypatch.setattr(recordtest, "OBSProcessManager", FakeProcessManager)
    monkeypatch.setattr(recordtest, "is_tcp_port_open", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest.time, "sleep", lambda *args, **kwargs: None)
    return FakeProcessManager


@pytest.mark.parametrize(
    ("stale_phase", "expected_payload", "expected_kills"),
    [
        ("preparing", b"original", 1),
        ("committing", b"original", 2),
        ("committed", b"desired", 2),
    ],
)
def test_launch_obs_recovers_pending_settings_without_copy_classification(
    monkeypatch,
    tmp_path,
    stale_phase,
    expected_payload,
    expected_kills,
):
    obs_dir = (tmp_path / "obs-portable").absolute()
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    custom_ini = obs_dir / "config" / "obs-studio" / "custom.ini"
    custom_ini.parent.mkdir(parents=True)
    custom_ini.write_bytes(b"original")
    snapshot = obs_bootstrap.preflight_obs_config_file(custom_ini, label="custom.ini")
    plan = obs_bootstrap.OBSConfigTransactionPlan(
        base_dir=obs_dir,
        directories=(custom_ini.parent,),
        writes=(obs_bootstrap.OBSConfigPlannedWrite(snapshot, b"desired"),),
    )

    if stale_phase == "preparing":
        real_write = obs_bootstrap._write_settings_temporary
        write_calls = 0

        def crash_in_preparing(path, payload):
            nonlocal write_calls
            write_calls += 1
            result = real_write(path, payload)
            if write_calls == 1:
                raise SystemExit("stale preparing")
            return result

        monkeypatch.setattr(
            obs_bootstrap,
            "_write_settings_temporary",
            crash_in_preparing,
        )
        with pytest.raises(SystemExit, match="stale preparing"):
            obs_bootstrap.execute_obs_config_transaction(plan)
        monkeypatch.setattr(obs_bootstrap, "_write_settings_temporary", real_write)
    elif stale_phase == "committing":
        real_journal = obs_bootstrap._write_settings_journal

        def crash_after_committing(base, owner, phase, writes):
            result = real_journal(base, owner, phase, writes)
            if phase == "committing":
                raise SystemExit("stale committing")
            return result

        monkeypatch.setattr(
            obs_bootstrap,
            "_write_settings_journal",
            crash_after_committing,
        )
        with pytest.raises(SystemExit, match="stale committing"):
            obs_bootstrap.execute_obs_config_transaction(plan)
        monkeypatch.setattr(obs_bootstrap, "_write_settings_journal", real_journal)
    else:
        real_journal = obs_bootstrap._write_settings_journal

        def crash_after_committed(base, owner, phase, writes):
            result = real_journal(base, owner, phase, writes)
            if phase == "committed":
                raise SystemExit("stale committed")
            return result

        monkeypatch.setattr(
            obs_bootstrap,
            "_write_settings_journal",
            crash_after_committed,
        )
        with pytest.raises(SystemExit, match="stale committed"):
            obs_bootstrap.execute_obs_config_transaction(plan)
        monkeypatch.setattr(obs_bootstrap, "_write_settings_journal", real_journal)

    assert obs_bootstrap.has_pending_obs_settings_transaction(obs_dir) is True
    assert obs_bootstrap.has_pending_obs_copy_transaction(obs_dir) is False
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", tmp_path / "missing-legacy")
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "missing-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "missing-data")
    manager = _install_fake_obs_launch(monkeypatch)

    process = recordtest.launch_obs(_fake_launch_config(tmp_path, recording_encoder="x264"))

    assert process.pid == 101
    assert custom_ini.read_bytes() == expected_payload
    assert manager.kill_count == expected_kills
    assert obs_bootstrap.has_pending_obs_settings_transaction(obs_dir) is False
    assert obs_bootstrap.has_pending_obs_copy_transaction(obs_dir) is False


def test_launch_obs_rejects_profile_reparse_before_stopping_process(monkeypatch, tmp_path):
    obs_dir = (tmp_path / "obs-portable").absolute()
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    profiles_root = obs_dir / "config" / "obs-studio" / "basic" / "profiles"
    profiles_root.mkdir(parents=True)
    external_profile = tmp_path / "external-profile"
    external_profile.mkdir()
    sentinel = external_profile / "basic.ini"
    sentinel.write_text("external", encoding="utf-8")
    sentinel_before = sentinel.read_bytes()
    _create_directory_link(profiles_root / "unsafe", external_profile)

    class TrackingProcessManager:
        kill_calls = 0
        start_calls = 0

        def __init__(self, obs_dir_arg, logger=None):
            self.obs_dir = Path(obs_dir_arg)
            self.obs_exe = self.obs_dir / "bin" / "64bit" / "obs64.exe"

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            type(self).kill_calls += 1
            return []

        def unmanaged_processes(self):
            return []

        def start_obs(self, *args, **kwargs):
            type(self).start_calls += 1
            raise AssertionError("unsafe profile must not reach OBS startup")

    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", tmp_path / "missing-legacy")
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "missing-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "missing-data")
    monkeypatch.setattr(recordtest, "OBSProcessManager", TrackingProcessManager)

    with pytest.raises(recordtest.RecorderError, match="reparse point|安全性検査"):
        recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert TrackingProcessManager.kill_calls == 0
    assert TrackingProcessManager.start_calls == 0
    assert sentinel.read_bytes() == sentinel_before


def test_launch_obs_gpu_plan_failure_cleans_started_process(monkeypatch, tmp_path):
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
    real_preflight = recordtest.preflight_obs_recording_profile_ini
    preflight_calls = 0

    def fail_gpu_restart_preflight(base_dir, *, user_file=None):
        nonlocal preflight_calls
        preflight_calls += 1
        if preflight_calls == 2:
            raise obs_bootstrap.OBSPathSafetyError("GPU restart unsafe profile")
        return real_preflight(base_dir, user_file=user_file)

    monkeypatch.setattr(recordtest, "preflight_obs_recording_profile_ini", fail_gpu_restart_preflight)

    with pytest.raises(recordtest.RecorderError, match="再起動transaction"):
        recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert preflight_calls == 2
    assert manager.start_count == 1
    assert manager.terminated_pids == [101]
    assert manager.active_processes == {}


def test_launch_obs_encoder_probe_cancellation_cleans_started_handle_once(
    monkeypatch,
    tmp_path,
):
    obs_dir = (tmp_path / "obs-portable").resolve()
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir)
    manager = _install_fake_obs_launch(monkeypatch)
    primary_error = asyncio.CancelledError("encoder probe cancelled")
    monkeypatch.setattr(
        recordtest,
        "_wait_for_obs_startup_encoder_selection",
        lambda *args, **kwargs: (_ for _ in ()).throw(primary_error),
    )

    with pytest.raises(asyncio.CancelledError) as captured:
        recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert captured.value is primary_error
    assert manager.start_count == 1
    assert manager.terminated_pids == [101]
    assert manager.strict_signal_pids == []
    assert manager.active_processes == {}


def test_launch_obs_cleanup_control_flow_supersedes_normal_encoder_failure(
    monkeypatch,
    tmp_path,
):
    obs_dir = (tmp_path / "obs-portable").resolve()
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir)
    manager = _install_fake_obs_launch(monkeypatch)
    primary_error = OSError("encoder probe failed")
    cleanup_error = SystemExit("encoder cleanup interrupted")
    terminate_calls = []

    monkeypatch.setattr(
        recordtest,
        "_wait_for_obs_startup_encoder_selection",
        lambda *args, **kwargs: (_ for _ in ()).throw(primary_error),
    )

    def interrupt_termination(self, process, timeout_sec=3.0):
        terminate_calls.append(process.pid)
        raise cleanup_error

    monkeypatch.setattr(manager, "terminate_process", interrupt_termination)

    with pytest.raises(SystemExit) as captured:
        recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert captured.value is cleanup_error
    assert terminate_calls == [101]
    assert any(
        "encoder probe failed" in note
        for note in getattr(cleanup_error, "__notes__", [])
    )


def test_launch_obs_gpu_stop_interruption_is_not_signaled_twice(
    monkeypatch,
    tmp_path,
):
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
    primary_error = KeyboardInterrupt("GPU stop interrupted")
    terminate_calls = []

    def interrupt_termination(self, process, timeout_sec=3.0):
        terminate_calls.append(process.pid)
        raise primary_error

    monkeypatch.setattr(manager, "terminate_process", interrupt_termination)

    with pytest.raises(KeyboardInterrupt) as captured:
        recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert captured.value is primary_error
    assert terminate_calls == [101]
    assert manager.strict_signal_pids == []
    assert manager.active_processes == {
        101: manager.process_handles[101].identity,
    }
    assert any("同じPopenへ再度signalせず" in note for note in primary_error.__notes__)


def test_launch_obs_fails_closed_when_gpu_transaction_skips_stop_callback(
    monkeypatch,
    tmp_path,
):
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
    original_transaction = recordtest._execute_obs_startup_settings_transaction
    transaction_calls = 0

    def skip_second_stop_callback(*args, **kwargs):
        nonlocal transaction_calls
        transaction_calls += 1
        if transaction_calls == 2:
            return ({}, [])
        return original_transaction(*args, **kwargs)

    monkeypatch.setattr(
        recordtest,
        "_execute_obs_startup_settings_transaction",
        skip_second_stop_callback,
    )

    with pytest.raises(recordtest.RecorderError, match="停止完了証跡なし"):
        recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert manager.start_count == 1
    assert manager.terminated_pids == [101]
    assert manager.active_processes == {}


def test_launch_obs_gpu_prequery_failure_cleans_started_process(
    monkeypatch,
    tmp_path,
):
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
    real_query = recordtest._query_managed_obs_processes_before_settings_stop

    def fail_gpu_prequery(base_dir, process_manager):
        if manager.start_count == 1:
            raise obs_bootstrap.OBSProcessQueryError("GPU pre-query failed")
        return real_query(base_dir, process_manager)

    monkeypatch.setattr(
        recordtest,
        "_query_managed_obs_processes_before_settings_stop",
        fail_gpu_prequery,
    )

    with pytest.raises(recordtest.RecorderError, match="再起動transaction"):
        recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert manager.start_count == 1
    assert manager.terminated_pids == [101]
    assert manager.strict_signal_pids == []
    assert manager.active_processes == {}


def test_launch_obs_gpu_strict_cleanup_failure_leaves_started_handle_stopped(
    monkeypatch,
    tmp_path,
):
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
    real_terminate = manager.terminate_expected_obs_processes_strict

    def fail_gpu_strict_cleanup(self, expected):
        if type(self).start_count == 1 and expected == ():
            raise obs_bootstrap.OBSProcessQueryError("GPU strict cleanup failed")
        return real_terminate(self, expected)

    monkeypatch.setattr(
        manager,
        "terminate_expected_obs_processes_strict",
        fail_gpu_strict_cleanup,
    )

    with pytest.raises(recordtest.RecorderError, match="再起動transaction"):
        recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert manager.start_count == 1
    assert manager.terminated_pids == [101]
    assert manager.strict_signal_pids == []
    assert manager.active_processes == {}


def test_launch_obs_gpu_cleanup_failure_preserves_primary_and_cleanup_chain(
    monkeypatch,
    tmp_path,
):
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
    real_query = recordtest._query_managed_obs_processes_before_settings_stop
    real_start = manager.start_obs
    started = []
    popen_kill_attempts = []

    def start_unstoppable_process(self, *args, **kwargs):
        process = real_start(self, *args, **kwargs)

        def fail_kill():
            popen_kill_attempts.append(process.pid)
            raise RuntimeError("Popen kill failed")

        process.kill = fail_kill
        process.wait = lambda timeout=None: (_ for _ in ()).throw(
            RuntimeError("Popen wait failed")
        )
        started.append(process)
        return process

    def fail_gpu_prequery(base_dir, process_manager):
        if manager.start_count == 1:
            raise obs_bootstrap.OBSProcessQueryError("GPU pre-query primary failure")
        return real_query(base_dir, process_manager)

    def fail_manager_cleanup(self, process, timeout_sec=3.0):
        type(self).terminated_pids.append(process.pid)
        raise RuntimeError("manager cleanup failed")

    monkeypatch.setattr(manager, "start_obs", start_unstoppable_process)
    monkeypatch.setattr(manager, "terminate_process", fail_manager_cleanup)
    monkeypatch.setattr(
        recordtest,
        "_query_managed_obs_processes_before_settings_stop",
        fail_gpu_prequery,
    )

    with pytest.raises(
        recordtest.RecorderError,
        match="再起動transaction",
    ) as raised:
        recordtest.launch_obs(_fake_launch_config(tmp_path))

    chained = []
    pending = [raised.value]
    while pending:
        current = pending.pop()
        if current is None or id(current) in {id(item) for item in chained}:
            continue
        chained.append(current)
        pending.extend(
            [
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            ]
        )
    rendered_chain = "\n".join(str(item) for item in chained)
    assert "GPU pre-query primary failure" in rendered_chain
    assert any(
        "GPU再起動対象OBSの安全な後始末にも失敗" in note
        for note in raised.value.__notes__
    )
    assert any("manager cleanup failed" in note for note in raised.value.__notes__)
    assert manager.start_count == 1
    assert manager.terminated_pids == [101]
    assert popen_kill_attempts == []
    assert started[0].poll() is None
    assert manager.active_processes == {101: started[0].identity}


def test_launch_obs_gpu_terminate_error_does_not_resignal_known_process(
    monkeypatch,
    tmp_path,
):
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
    popen_signal_attempts = []

    def fail_known_process_termination(self, process, timeout_sec=3.0):
        type(self).terminated_pids.append(process.pid)
        popen_signal_attempts.append(process.pid)
        raise RuntimeError("known process terminate failed")

    monkeypatch.setattr(
        manager,
        "terminate_process",
        fail_known_process_termination,
    )

    with pytest.raises(recordtest.RecorderError, match="再起動transaction") as captured:
        recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert manager.start_count == 1
    assert manager.terminated_pids == [101]
    assert popen_signal_attempts == [101]
    assert manager.strict_signal_pids == []
    assert manager.active_processes == {
        101: manager.process_handles[101].identity,
    }
    assert manager.kill_count == 2
    assert any("同じPopenへ再度signalせず" in note for note in captured.value.__notes__)
    assert not obs_bootstrap.get_obs_settings_transaction_marker(obs_dir).exists()


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
    assert manager.kill_count == 2
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


def test_launch_obs_gpu_stop_evidence_explains_known_and_remaining_processes(
    monkeypatch,
    tmp_path,
):
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
    real_latest = manager.latest_log_encoder_kinds
    real_factory = recordtest._create_obs_settings_stop_evidence
    known_evidence_calls = []

    def latest_with_remaining_process(self, since=None):
        result = real_latest(self, since=since)
        if type(self).start_count == 1 and 201 not in type(self).active_processes:
            type(self).active_processes[201] = obs_bootstrap.OBSProcessInfo(
                pid=201,
                executable_path=self.obs_exe.absolute(),
                creation_time=1201.0,
            )
        return result

    def capture_factory(*args, **kwargs):
        evidence = real_factory(*args, **kwargs)
        if kwargs.get("known_managed_process") is not None:
            known_evidence_calls.append((args, kwargs, evidence))
        return evidence

    monkeypatch.setattr(manager, "latest_log_encoder_kinds", latest_with_remaining_process)
    monkeypatch.setattr(recordtest, "_create_obs_settings_stop_evidence", capture_factory)

    process = recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert process.pid == 102
    assert len(known_evidence_calls) == 1
    _args, kwargs, evidence = known_evidence_calls[0]
    before = kwargs["before"]
    known = kwargs["known_managed_process"]
    assert {item.pid for item in before.processes} == {101, 201}
    assert known == next(item for item in before.processes if item.pid == 101)
    assert kwargs["killed_pids"] == (201,)
    assert evidence.known_managed_process == known
    assert evidence.managed_after == ()
    assert manager.terminated_pids == [101]
    assert manager.kill_count == 2


def test_launch_obs_gpu_stop_rejects_reused_known_pid_without_signaling_it(
    monkeypatch,
    tmp_path,
):
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
    real_terminate = manager.terminate_process
    replacement = obs_bootstrap.OBSProcessInfo(
        pid=101,
        executable_path=obs_exe,
        creation_time=9999.0,
        creation_time_filetime=116_444_835_990_000_000,
    )

    def terminate_then_reuse_pid(self, process, timeout_sec=3.0):
        real_terminate(self, process, timeout_sec=timeout_sec)
        type(self).active_processes[process.pid] = replacement

    monkeypatch.setattr(manager, "terminate_process", terminate_then_reuse_pid)

    with pytest.raises(recordtest.RecorderError, match="再起動transaction"):
        recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert manager.start_count == 1
    assert manager.terminated_pids == [101]
    assert manager.strict_signal_pids == []
    assert manager.active_processes == {101: replacement}


def test_launch_obs_cleans_started_handle_when_strict_identity_is_incomplete(
    monkeypatch,
    tmp_path,
):
    obs_dir = (tmp_path / "obs-portable").resolve()
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir)
    manager = _install_fake_obs_launch(monkeypatch)
    real_query = manager.query_obs_processes_strict

    def query_without_creation_time(self):
        snapshot = real_query(self)
        if type(self).start_count == 1 and snapshot.processes:
            return obs_bootstrap.OBSProcessQuerySnapshot(
                tuple(replace(item, creation_time=None) for item in snapshot.processes),
                snapshot.queried_at,
            )
        return snapshot

    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        query_without_creation_time,
    )

    with pytest.raises(recordtest.RecorderError, match="strict identity"):
        recordtest.launch_obs(_fake_launch_config(tmp_path, recording_encoder="x264"))

    assert manager.start_count == 1
    assert manager.terminated_pids == [101]
    assert manager.active_processes == {}


def test_launch_obs_rejects_started_popen_identity_replacement_without_signaling_it(
    monkeypatch,
    tmp_path,
):
    obs_dir = (tmp_path / "obs-portable").resolve()
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir)
    manager = _install_fake_obs_launch(monkeypatch)
    real_query = manager.query_obs_processes_strict
    replacement = obs_bootstrap.OBSProcessInfo(
        pid=101,
        executable_path=obs_exe,
        creation_time=9999.0,
        creation_time_filetime=116_444_835_990_000_000,
    )

    def query_replacement_after_start(self):
        if type(self).start_count == 1 and type(self).active_processes:
            type(self).active_processes[101] = replacement
        return real_query(self)

    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        query_replacement_after_start,
    )

    with pytest.raises(recordtest.RecorderError, match="strict identity"):
        recordtest.launch_obs(_fake_launch_config(tmp_path, recording_encoder="x264"))

    assert manager.start_count == 1
    assert manager.terminated_pids == [101]
    assert manager.strict_signal_pids == []
    assert manager.active_processes == {101: replacement}


def test_launch_obs_gpu_replan_does_not_terminate_or_kill_twice(
    monkeypatch,
    tmp_path,
):
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
    profile_ini = (
        obs_dir
        / "config"
        / "obs-studio"
        / "basic"
        / "profiles"
        / recordtest.MANAGED_OBS_PROFILE_DIR_NAME
        / "basic.ini"
    )
    real_terminate = manager.terminate_process

    def terminate_with_profile_flush(self, process, timeout_sec=3.0):
        real_terminate(self, process, timeout_sec=timeout_sec)
        profile_ini.write_bytes(
            b"[General]\nName=external-update\nExternalAfterStop=keep\n"
        )

    monkeypatch.setattr(manager, "terminate_process", terminate_with_profile_flush)

    process = recordtest.launch_obs(_fake_launch_config(tmp_path))

    rendered = profile_ini.read_text(encoding="utf-8")
    assert process.pid == 102
    assert manager.start_count == 2
    assert manager.terminated_pids == [101]
    assert manager.kill_count == 2
    assert "ExternalAfterStop=keep" in rendered
    assert "RecEncoder=nvenc" in rendered
    assert not obs_bootstrap.get_obs_settings_transaction_marker(obs_dir).exists()


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

        def query_obs_processes_strict(self):
            return obs_bootstrap.OBSProcessQuerySnapshot(
                processes=(),
                queried_at=100.0,
            )

        def terminate_expected_obs_processes_strict(self, expected):
            return obs_bootstrap.OBSStrictTerminationResult(
                expected,
                self.query_obs_processes_strict(),
            )

        def has_managed_process(self):
            return False

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
    assert not (managed_dir / ".lol_replay_obs_copy_in_progress").exists()
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


def test_launch_obs_rejects_incomplete_managed_obs_copy(monkeypatch, tmp_path):
    obs_dir = (tmp_path / "obs-portable").resolve()
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True)
    obs_exe.write_text("partial", encoding="utf-8")
    (obs_dir / ".lol_replay_obs_copy_in_progress").write_text(
        "missing legacy source", encoding="utf-8"
    )
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", obs_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", tmp_path / "missing-legacy")
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "missing-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "missing-data")

    class UnexpectedProcessManager:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("incomplete OBS must not reach process startup")

    monkeypatch.setattr(recordtest, "OBSProcessManager", UnexpectedProcessManager)

    with pytest.raises(recordtest.RecorderError, match="旧形式.*fingerprint"):
        recordtest.launch_obs(_fake_launch_config(tmp_path))


def test_preflight_preserves_live_migration_message(monkeypatch, tmp_path):
    managed_dir = (tmp_path / "obs-portable").resolve()
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", tmp_path / "legacy")
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "legacy-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "legacy-data")

    def raise_live(*_args, **_kwargs):
        raise obs_bootstrap.OBSMigrationInProgressError("LIVE MIGRATION DETAIL")

    monkeypatch.setattr(recordtest, "migrate_legacy_obs_installation", raise_live)
    report = recordtest.run_preflight_checks(
        {
            "obs": {"dir": str(managed_dir), "port": 4455, "password": "secret"},
            "paths": {
                "bin_dir": str(tmp_path / "bin"),
                "recordings_dir": str(tmp_path / "recordings"),
                "json_dir": str(tmp_path / "recordings" / "json"),
                "champion_icons_dir": str(tmp_path / "assets" / "champions" / "icons"),
            },
        },
        auto_fix=True,
        ensure_dirs=True,
    )

    assert any("LIVE MIGRATION DETAIL" in warning for warning in report["warnings"])


def test_preflight_preserves_stale_migration_recovery_message(monkeypatch, tmp_path):
    managed_dir = (tmp_path / "obs-portable").resolve()
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", tmp_path / "legacy")
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "legacy-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "legacy-data")

    def raise_stale(*_args, **_kwargs):
        raise obs_bootstrap.OBSMigrationRecoveryRequiredError("STALE MIGRATION DETAIL")

    forbidden_calls = []

    def track_legacy_repair(*_args, **_kwargs):
        forbidden_calls.append("legacy repair")
        return None

    class TrackingBootstrapper:
        def __init__(self, *_args, **_kwargs):
            forbidden_calls.append("bootstrap")

    class TrackingProcessManager:
        def __init__(self, *_args, **_kwargs):
            forbidden_calls.append("process manager")

    def track_profile_repair(*_args, **_kwargs):
        forbidden_calls.append("profile repair")
        return ()

    monkeypatch.setattr(recordtest, "migrate_legacy_obs_installation", raise_stale)
    monkeypatch.setattr(recordtest, "repair_legacy_managed_obs_if_present", track_legacy_repair)
    monkeypatch.setattr(recordtest, "OBSBootstrapper", TrackingBootstrapper)
    monkeypatch.setattr(recordtest, "OBSProcessManager", TrackingProcessManager)
    monkeypatch.setattr(recordtest, "ensure_obs_recording_profile_ini", track_profile_repair)
    report = recordtest.run_preflight_checks(
        {
            "obs": {"dir": str(managed_dir), "port": 4455, "password": "secret"},
            "paths": {
                "bin_dir": str(tmp_path / "bin"),
                "recordings_dir": str(tmp_path / "recordings"),
                "json_dir": str(tmp_path / "recordings" / "json"),
                "champion_icons_dir": str(tmp_path / "assets" / "champions" / "icons"),
            },
        },
        auto_fix=True,
        ensure_dirs=True,
    )

    assert any("STALE MIGRATION DETAIL" in warning for warning in report["warnings"])
    assert forbidden_calls == []


def test_managed_migration_entry_recovers_destination_settings_first(
    monkeypatch,
    tmp_path,
):
    managed_dir = (tmp_path / "obs-portable").absolute()
    executable = managed_dir / "bin" / "64bit" / "obs64.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fake obs")
    target = managed_dir / "config" / "obs-studio" / "custom.ini"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")
    snapshot = obs_bootstrap.preflight_obs_config_file(target, label="custom.ini")
    plan = obs_bootstrap.OBSConfigTransactionPlan(
        base_dir=managed_dir,
        directories=(target.parent,),
        writes=(obs_bootstrap.OBSConfigPlannedWrite(snapshot, b"desired"),),
    )
    real_write = obs_bootstrap._write_settings_temporary
    write_calls = 0

    def crash_after_backup(path, payload):
        nonlocal write_calls
        write_calls += 1
        result = real_write(path, payload)
        if write_calls == 1:
            raise SystemExit("stale destination settings")
        return result

    monkeypatch.setattr(obs_bootstrap, "_write_settings_temporary", crash_after_backup)
    with pytest.raises(SystemExit, match="stale destination"):
        obs_bootstrap.execute_obs_config_transaction(plan)
    monkeypatch.setattr(obs_bootstrap, "_write_settings_temporary", real_write)
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", tmp_path / "missing-legacy")
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "missing-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "missing-data")

    class NoOBSProcesses:
        def __init__(self, *_args, **_kwargs):
            pass

        def unmanaged_processes(self):
            return []

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            return []

        def has_managed_process(self):
            return False

    monkeypatch.setattr(recordtest, "OBSProcessManager", NoOBSProcesses)

    assert recordtest.migrate_legacy_managed_obs_if_needed() is None

    assert obs_bootstrap.has_pending_obs_settings_transaction(managed_dir) is False
    assert target.read_bytes() == b"original"


def test_preflight_partial_copy_failure_keeps_source_fingerprint_resumable(monkeypatch, tmp_path):
    managed_dir = (tmp_path / "obs-portable").absolute()
    legacy_dir = (tmp_path / "legacy-obs").absolute()
    legacy_executable = legacy_dir / "bin" / "64bit" / "obs64.exe"
    legacy_global_ini = legacy_dir / "config" / "obs-studio" / "global.ini"
    legacy_executable.parent.mkdir(parents=True)
    legacy_global_ini.parent.mkdir(parents=True)
    legacy_executable.write_bytes(b"fake legacy obs")
    legacy_global_ini.write_text(
        "[General]\nFirstRun=false\n\n[BasicWindow]\nSysTrayEnabled=true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", legacy_dir)
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "missing-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "missing-data")
    source_inventory = obs_bootstrap._build_obs_tree_inventory(legacy_dir)
    source_fingerprint = obs_bootstrap._inventory_fingerprint(source_inventory)
    real_copy = obs_bootstrap._copy_inventory_file
    failed_once = False

    def copy_then_fail(source_path, destination_path, expected, owner_token, **kwargs):
        nonlocal failed_once
        real_copy(source_path, destination_path, expected, owner_token, **kwargs)
        if not failed_once:
            failed_once = True
            raise OSError("simulated partial copy failure")

    monkeypatch.setattr(obs_bootstrap, "_copy_inventory_file", copy_then_fail)
    report = recordtest.run_preflight_checks(
        _preflight_config(managed_dir, tmp_path),
        auto_fix=True,
        ensure_dirs=False,
    )

    assert any("partial copy failure" in warning for warning in report["warnings"])
    assert obs_bootstrap._build_obs_tree_inventory(legacy_dir) == source_inventory
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(managed_dir)
    journal = json.loads(marker.read_text(encoding="utf-8"))
    assert journal["source_fingerprint"] == source_fingerprint

    monkeypatch.setattr(obs_bootstrap, "_copy_inventory_file", real_copy)
    assert recordtest.migrate_legacy_managed_obs_if_needed(port=4455, password="secret") == legacy_dir

    assert obs_bootstrap._build_obs_tree_inventory(legacy_dir) == source_inventory
    assert (managed_dir / "bin" / "64bit" / "obs64.exe").read_bytes() == b"fake legacy obs"
    assert not marker.exists()


def test_recordtest_legacy_migration_rejects_source_that_survives_kill(
    monkeypatch,
    tmp_path,
):
    managed_dir = (tmp_path / "obs-portable").absolute()
    legacy_dir = (tmp_path / "legacy-obs").absolute()
    legacy_executable = legacy_dir / "bin" / "64bit" / "obs64.exe"
    legacy_executable.parent.mkdir(parents=True)
    legacy_executable.write_bytes(b"fake legacy obs")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", legacy_dir)
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "missing-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "missing-data")

    class StubbornProcessManager:
        kill_calls = 0

        def __init__(self, obs_dir_arg, **_kwargs):
            self.obs_dir = Path(obs_dir_arg).absolute()
            self.query_calls = 0
            self.process = obs_bootstrap.OBSProcessInfo(
                pid=4312,
                executable_path=self.obs_dir / "bin" / "64bit" / "obs64.exe",
                creation_time=10.0,
            )

        def query_obs_processes_strict(self):
            self.query_calls += 1
            return obs_bootstrap.OBSProcessQuerySnapshot(
                processes=(self.process,),
                queried_at=100.0 + self.query_calls,
            )

        def is_managed_process(self, process):
            return process.executable_path == self.process.executable_path

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            type(self).kill_calls += 1
            return []

        def terminate_expected_obs_processes_strict(self, expected):
            type(self).kill_calls += 1
            return obs_bootstrap.OBSStrictTerminationResult(
                expected,
                self.query_obs_processes_strict(),
            )

        def has_managed_process(self):
            return True

    copy_started = False

    def run_prepare_source(destination, sources, *, prepare_source, **_kwargs):
        nonlocal copy_started
        prepare_source(legacy_dir)
        copy_started = True
        return legacy_dir

    monkeypatch.setattr(recordtest, "OBSProcessManager", StubbornProcessManager)
    monkeypatch.setattr(
        recordtest,
        "migrate_legacy_obs_installation",
        run_prepare_source,
    )

    with pytest.raises(recordtest.RecorderError, match="strict identity"):
        recordtest.migrate_legacy_managed_obs_if_needed(
            port=4455,
            password="secret",
        )

    assert StubbornProcessManager.kill_calls == 1
    assert copy_started is False


def test_preflight_does_not_mutate_live_copy_and_normalizes_after_completion(monkeypatch, tmp_path):
    managed_dir = (tmp_path / "obs-portable").absolute()
    legacy_dir = (tmp_path / "legacy-obs").absolute()
    legacy_executable = legacy_dir / "bin" / "64bit" / "obs64.exe"
    legacy_executable.parent.mkdir(parents=True)
    legacy_executable.write_bytes(b"preflight-live-copy")
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", legacy_dir)
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "missing-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "missing-data")
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_preflight_migration_during_copy,
        args=(str(legacy_dir), str(managed_dir), entered, release),
    )
    process.start()
    child_exitcode = None
    try:
        assert entered.wait(15), "migration did not pause with its marker and copy temporary"
        marker = obs_bootstrap.get_obs_copy_in_progress_marker(managed_dir)
        marker_before = marker.read_bytes()
        owner_token = json.loads(marker_before)["owner_token"]
        copy_temporary = obs_bootstrap._transaction_copy_temporary_path(
            managed_dir / legacy_executable.relative_to(legacy_dir),
            owner_token,
        )
        temporary_before = copy_temporary.read_bytes()

        report = recordtest.run_preflight_checks(
            _preflight_config(managed_dir, tmp_path),
            auto_fix=True,
            ensure_dirs=False,
        )

        assert any("別のプロセス" in warning for warning in report["warnings"])
        assert marker.read_bytes() == marker_before
        assert copy_temporary.read_bytes() == temporary_before
        assert legacy_executable.read_bytes() == b"preflight-live-copy"
        assert not obs_bootstrap.get_portable_marker_path(managed_dir).exists()
        assert not obs_bootstrap.get_obs_global_ini_path(managed_dir).exists()
        assert not obs_bootstrap.get_obs_user_ini_path(managed_dir).exists()
        assert not obs_bootstrap.get_obs_websocket_config_path(managed_dir).exists()
    finally:
        release.set()
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)
        child_exitcode = process.exitcode
        process.close()

    assert child_exitcode == 0
    completed_report = recordtest.run_preflight_checks(
        _preflight_config(managed_dir, tmp_path),
        auto_fix=True,
        ensure_dirs=False,
    )

    assert completed_report["errors"] == []
    assert obs_bootstrap.get_portable_marker_path(managed_dir).is_file()
    assert obs_bootstrap.get_obs_global_ini_path(managed_dir).is_file()
    assert obs_bootstrap.get_obs_user_ini_path(managed_dir).is_file()
    assert obs_bootstrap.get_obs_websocket_config_path(managed_dir).is_file()


def test_preflight_does_not_dirty_empty_obs_destination_before_legacy_appears(monkeypatch, tmp_path):
    managed_dir = (tmp_path / "obs-portable").absolute()
    legacy_dir = (tmp_path / "legacy-obs").absolute()
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)
    monkeypatch.setattr(recordtest, "LEGACY_MANAGED_OBS_DIR", legacy_dir)
    monkeypatch.setattr(recordtest, "LEGACY_ROOT_OBS_DIR", tmp_path / "missing-root")
    monkeypatch.setattr(recordtest, "LEGACY_DATA_BIN_OBS_DIR", tmp_path / "missing-data")

    first_report = recordtest.run_preflight_checks(
        _preflight_config(managed_dir, tmp_path),
        auto_fix=True,
        ensure_dirs=False,
    )

    assert first_report["errors"]
    assert not managed_dir.exists()

    legacy_executable = legacy_dir / "bin" / "64bit" / "obs64.exe"
    legacy_executable.parent.mkdir(parents=True)
    legacy_executable.write_bytes(b"late legacy obs")
    second_report = recordtest.run_preflight_checks(
        _preflight_config(managed_dir, tmp_path),
        auto_fix=True,
        ensure_dirs=False,
    )

    assert second_report["errors"] == []
    assert (managed_dir / "bin" / "64bit" / "obs64.exe").read_bytes() == b"late legacy obs"
    assert obs_bootstrap.get_portable_marker_path(managed_dir).is_file()


def test_launch_obs_preserves_live_migration_message(monkeypatch, tmp_path):
    managed_dir = (tmp_path / "obs-portable").resolve()
    monkeypatch.setattr(recordtest, "MANAGED_PORTABLE_OBS_DIR", managed_dir)

    def raise_live(*_args, **_kwargs):
        raise obs_bootstrap.OBSMigrationInProgressError("LIVE MIGRATION DETAIL")

    monkeypatch.setattr(recordtest, "migrate_legacy_obs_installation", raise_live)
    with pytest.raises(recordtest.RecorderError, match="LIVE MIGRATION DETAIL"):
        recordtest.launch_obs(_fake_launch_config(tmp_path))


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
