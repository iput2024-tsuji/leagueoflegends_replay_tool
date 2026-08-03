import asyncio
import configparser
import json
import multiprocessing
import os
import shutil
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest

from src import obs_bootstrap, recordtest
from src.lcu_client import LCUConnectionInfo


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

        def __init__(self, *_args, **_kwargs):
            pass

        def unmanaged_processes(self):
            return [SimpleNamespace(pid=99, executable_path=tmp_path / "obs64.exe")]

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

        def __init__(self, *_args, **_kwargs):
            pass

        def unmanaged_processes(self):
            return []

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            type(self).kill_calls += 1
            return []

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


def test_startup_settings_revalidate_unchanged_profile_after_stop(tmp_path):
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
        before_commit=lambda: None,
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
    global_before = global_ini.read_bytes()

    def simulate_obs_profile_flush() -> None:
        profile_ini.write_bytes(b"[General]\nName=external-update\n")

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="内容が変化"):
        recordtest._execute_obs_startup_settings_transaction(
            bootstrapper,
            **transaction_kwargs,
            before_commit=simulate_obs_profile_flush,
            run_before_commit_on_noop=True,
        )

    assert profile_ini.read_bytes() == b"[General]\nName=external-update\n"
    assert global_ini.read_bytes() == global_before
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
        before_commit=lambda: None,
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

    def simulate_obs_creating_profile_file() -> None:
        late_profile.write_bytes(b"[General]\nName=created-after-plan\n")

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="作成されました"):
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
        kill_count = 0
        encoder_log_calls = 0
        terminated_pids = []

        def __init__(self, obs_dir_arg, logger=None):
            self.obs_dir = Path(obs_dir_arg)
            self.obs_exe = self.obs_dir / "bin" / "64bit" / "obs64.exe"

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            type(self).kill_count += 1
            return []

        def has_managed_process(self):
            return False

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

    monkeypatch.setattr(recordtest, "OBSProcessManager", FakeProcessManager)
    monkeypatch.setattr(recordtest, "is_tcp_port_open", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest.time, "sleep", lambda *args, **kwargs: None)
    return FakeProcessManager


@pytest.mark.parametrize(
    ("stale_phase", "expected_payload", "expected_kills"),
    [
        ("preparing", b"original", 1),
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


def test_launch_obs_gpu_repair_preflights_before_terminating_process(monkeypatch, tmp_path):
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
    assert manager.terminated_pids == []


def test_launch_obs_gpu_terminate_error_still_kills_all_managed_processes(
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

    def fail_known_process_termination(self, process, timeout_sec=3.0):
        type(self).terminated_pids.append(process.pid)
        raise RuntimeError("known process terminate failed")

    monkeypatch.setattr(
        manager,
        "terminate_process",
        fail_known_process_termination,
    )

    with pytest.raises(recordtest.RecorderError, match="再起動transaction"):
        recordtest.launch_obs(_fake_launch_config(tmp_path))

    assert manager.start_count == 1
    assert manager.terminated_pids == [101]
    assert manager.kill_count == 2
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

        def __init__(self, *_args, **_kwargs):
            pass

        def unmanaged_processes(self):
            return []

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            type(self).kill_calls += 1
            return []

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

    with pytest.raises(recordtest.RecorderError, match="停止できません"):
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
