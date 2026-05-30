import asyncio
import os
import shutil
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts import setup_env


def runtime_dir(name):
    path = Path("tests") / "_tmp" / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_extract_obs_flattens_top_level_zip_directory(monkeypatch):
    tmp_path = runtime_dir("setup_env_obs_extract")
    zip_path = tmp_path / "obs.zip"
    dest = tmp_path / "obs-portable"

    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("OBS-Studio-Portable/bin/64bit/obs64.exe", "fake")
        archive.writestr("OBS-Studio-Portable/data/obs-plugins/plugin.txt", "plugin")

    setup_env._extract_obs(zip_path, dest)

    assert (dest / "bin" / "64bit" / "obs64.exe").exists()
    assert (dest / "data" / "obs-plugins" / "plugin.txt").exists()
    assert not (dest / "OBS-Studio-Portable" / "bin" / "64bit" / "obs64.exe").exists()


def test_bootstrap_obs_portable_config_writes_marker_and_tray_settings(monkeypatch):
    obs_dir = runtime_dir("setup_env_obs_bootstrap") / "obs-portable"

    setup_env.bootstrap_obs_portable_config(obs_dir)

    global_ini = obs_dir / "config" / "obs-studio" / "global.ini"
    user_ini = obs_dir / "config" / "obs-studio" / "user.ini"
    text = global_ini.read_text(encoding="utf-8")
    user_text = user_ini.read_text(encoding="utf-8")

    assert (obs_dir / "obs_portable_mode.txt").exists()
    assert "[General]" in text
    assert "FirstRun=true" in text
    assert "[BasicWindow]" in text
    assert "SysTrayEnabled=false" in text
    assert "SysTrayWhenStarted=false" in text
    assert "SysTrayMinimizeToTray=false" in text
    assert "HideTrayIcon" not in text
    assert "FirstRun=true" in user_text
    assert "SysTrayEnabled=false" in user_text
    assert "SysTrayWhenStarted=false" in user_text
    assert "SysTrayMinimizeToTray=false" in user_text


def test_cleanup_legacy_archives_removes_setup_zips():
    root = runtime_dir("setup_env_cleanup_archives")
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    obs_zip = bin_dir / "OBS-Studio-32.1.2-Windows-x64.zip"
    ffmpeg_zip = bin_dir / "ffmpeg-8.1.1-essentials_build.zip"
    keep_file = bin_dir / "mpv-1.dll"
    obs_zip.write_bytes(b"obs")
    ffmpeg_zip.write_bytes(b"ffmpeg")
    keep_file.write_bytes(b"mpv")

    removed = setup_env.cleanup_legacy_archives(bin_dir)

    assert {path.name for path in removed} == {ffmpeg_zip.name, obs_zip.name}
    assert not obs_zip.exists()
    assert not ffmpeg_zip.exists()
    assert keep_file.exists()


def test_cleanup_obs_debug_symbols_removes_pdb_files():
    root = runtime_dir("setup_env_cleanup_pdb")
    obs_dir = root / "obs-portable"
    pdb = obs_dir / "bin" / "64bit" / "obs64.pdb"
    dll = obs_dir / "bin" / "64bit" / "obs.dll"
    pdb.parent.mkdir(parents=True, exist_ok=True)
    pdb.write_bytes(b"debug")
    dll.write_bytes(b"dll")

    removed = setup_env.cleanup_obs_debug_symbols(obs_dir)

    assert removed == [pdb]
    assert not pdb.exists()
    assert dll.exists()


def test_environment_ready_requires_bootstrapped_obs_global_ini(monkeypatch):
    root = runtime_dir("setup_env_ready_requires_bootstrap")
    ffmpeg = root / "bin" / "ffmpeg.exe"
    obs_dir = root / "obs-portable"
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    global_ini = obs_dir / "config" / "obs-studio" / "global.ini"
    ffmpeg.parent.mkdir(parents=True, exist_ok=True)
    obs_exe.parent.mkdir(parents=True, exist_ok=True)
    global_ini.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.write_text("fake", encoding="utf-8")
    obs_exe.write_text("fake", encoding="utf-8")
    global_ini.write_text("[BasicWindow]\nSysTrayEnabled=true\n", encoding="utf-8")

    monkeypatch.setattr(setup_env, "FFMPEG_EXE", ffmpeg)
    monkeypatch.setattr(setup_env, "OBS_EXE", obs_exe)
    monkeypatch.setattr(setup_env, "OBS_PORTABLE_DIR", obs_dir)

    assert setup_env.is_environment_ready() is False

    setup_env.bootstrap_obs_portable_config(obs_dir)

    assert setup_env.is_environment_ready() is True


def test_environment_ready_requires_obs_first_run_initialized(monkeypatch):
    root = runtime_dir("setup_env_ready_requires_first_run")
    ffmpeg = root / "bin" / "ffmpeg.exe"
    obs_dir = root / "obs-portable"
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    global_ini = obs_dir / "config" / "obs-studio" / "global.ini"
    ffmpeg.parent.mkdir(parents=True, exist_ok=True)
    obs_exe.parent.mkdir(parents=True, exist_ok=True)
    global_ini.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.write_text("fake", encoding="utf-8")
    obs_exe.write_text("fake", encoding="utf-8")
    global_ini.write_text(
        "[General]\n"
        "FirstRun=false\n\n"
        "[BasicWindow]\n"
        "SysTrayEnabled=false\n"
        "SysTrayWhenStarted=false\n"
        "SysTrayMinimizeToTray=false\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(setup_env, "FFMPEG_EXE", ffmpeg)
    monkeypatch.setattr(setup_env, "OBS_EXE", obs_exe)
    monkeypatch.setattr(setup_env, "OBS_PORTABLE_DIR", obs_dir)

    assert setup_env.is_environment_ready() is False

    setup_env.bootstrap_obs_portable_config(obs_dir)

    assert setup_env.is_environment_ready() is True


def test_environment_ready_requires_bootstrapped_obs_user_ini(monkeypatch):
    root = runtime_dir("setup_env_ready_requires_user_ini")
    ffmpeg = root / "bin" / "ffmpeg.exe"
    obs_dir = root / "obs-portable"
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    global_ini = obs_dir / "config" / "obs-studio" / "global.ini"
    ffmpeg.parent.mkdir(parents=True, exist_ok=True)
    obs_exe.parent.mkdir(parents=True, exist_ok=True)
    global_ini.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.write_text("fake", encoding="utf-8")
    obs_exe.write_text("fake", encoding="utf-8")
    global_ini.write_text(
        "[General]\n"
        "FirstRun=true\n\n"
        "[BasicWindow]\n"
        "SysTrayEnabled=false\n"
        "SysTrayWhenStarted=false\n"
        "SysTrayMinimizeToTray=false\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(setup_env, "FFMPEG_EXE", ffmpeg)
    monkeypatch.setattr(setup_env, "OBS_EXE", obs_exe)
    monkeypatch.setattr(setup_env, "OBS_PORTABLE_DIR", obs_dir)

    assert setup_env.is_environment_ready() is False

    setup_env.bootstrap_obs_portable_config(obs_dir)

    assert setup_env.is_environment_ready() is True


def test_bootstrap_obs_portable_config_regenerates_corrupt_global_ini():
    obs_dir = runtime_dir("setup_env_obs_bootstrap_corrupt") / "obs-portable"
    global_ini = obs_dir / "config" / "obs-studio" / "global.ini"
    global_ini.parent.mkdir(parents=True, exist_ok=True)
    global_ini.write_text("[General\nbroken", encoding="utf-8")

    setup_env.bootstrap_obs_portable_config(obs_dir)

    text = global_ini.read_text(encoding="utf-8")
    assert "FirstRun=true" in text
    assert "SysTrayEnabled=false" in text
    assert "SysTrayWhenStarted=false" in text
    assert "SysTrayMinimizeToTray=false" in text
    user_text = (obs_dir / "config" / "obs-studio" / "user.ini").read_text(encoding="utf-8")
    assert "FirstRun=true" in user_text
    assert "SysTrayEnabled=false" in user_text


def test_ensure_obs_portable_migrates_legacy_obs_studio(monkeypatch):
    root = runtime_dir("setup_env_legacy_obs_migration")
    legacy_dir = root / "bin" / "OBS-Studio"
    obs_dir = root / "obs-portable"
    legacy_exe = legacy_dir / "bin" / "64bit" / "obs64.exe"
    legacy_ini = legacy_dir / "config" / "obs-studio" / "global.ini"
    legacy_exe.parent.mkdir(parents=True, exist_ok=True)
    legacy_ini.parent.mkdir(parents=True, exist_ok=True)
    legacy_exe.write_text("fake", encoding="utf-8")
    legacy_ini.write_text(
        "[General]\n"
        "FirstRun=false\n\n"
        "[BasicWindow]\n"
        "SysTrayEnabled=true\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(setup_env, "OBS_PORTABLE_DIR", obs_dir)
    monkeypatch.setattr(setup_env, "LEGACY_OBS_PORTABLE_DIR", legacy_dir)
    monkeypatch.setattr(setup_env, "OBS_EXE", obs_dir / "bin" / "64bit" / "obs64.exe")
    monkeypatch.setattr(setup_env, "LEGACY_OBS_EXE", legacy_exe)

    assert setup_env.migrate_legacy_obs_portable() is True

    migrated_ini = obs_dir / "config" / "obs-studio" / "global.ini"
    migrated_user_ini = obs_dir / "config" / "obs-studio" / "user.ini"
    assert (obs_dir / "bin" / "64bit" / "obs64.exe").exists()
    text = migrated_ini.read_text(encoding="utf-8")
    assert "FirstRun=true" in text
    assert "SysTrayEnabled=false" in text
    user_text = migrated_user_ini.read_text(encoding="utf-8")
    assert "FirstRun=true" in user_text
    assert "SysTrayEnabled=false" in user_text


def test_environment_ready_does_not_require_ffmpeg(monkeypatch):
    root = runtime_dir("setup_env_ready_without_ffmpeg")
    ffmpeg = root / "bin" / "ffmpeg.exe"
    obs_dir = root / "obs-portable"
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    obs_exe.parent.mkdir(parents=True, exist_ok=True)
    obs_exe.write_text("fake", encoding="utf-8")

    monkeypatch.setattr(setup_env, "FFMPEG_EXE", ffmpeg)
    monkeypatch.setattr(setup_env, "OBS_EXE", obs_exe)
    monkeypatch.setattr(setup_env, "OBS_PORTABLE_DIR", obs_dir)

    setup_env.bootstrap_obs_portable_config(obs_dir)

    assert not ffmpeg.exists()
    assert setup_env.is_environment_ready() is True


def test_cleanup_stale_temporary_workspaces_removes_old_directories_only():
    root = runtime_dir("setup_env_cleanup_tmp")
    tmp_dir = root / "downloads" / "_tmp"
    old_workspace = tmp_dir / "old"
    current_workspace = tmp_dir / "current"
    old_workspace.mkdir(parents=True)
    current_workspace.mkdir(parents=True)
    (old_workspace / "archive.zip").write_bytes(b"old")
    (current_workspace / "archive.zip").write_bytes(b"current")
    old_time = time.time() - 7200
    os.utime(old_workspace, (old_time, old_time))

    removed = setup_env.cleanup_stale_temporary_workspaces(max_age_sec=3600, base_dir=tmp_dir)

    assert removed == [old_workspace]
    assert not old_workspace.exists()
    assert current_workspace.exists()


def test_setup_lock_rejects_concurrent_setup():
    root = runtime_dir("setup_env_lock")
    lock_path = root / ".setup.lock"

    with setup_env.setup_lock(lock_path=lock_path, timeout_sec=0):
        with pytest.raises(setup_env.SetupLockTimeoutError):
            with setup_env.setup_lock(lock_path=lock_path, timeout_sec=0):
                pass

    assert not lock_path.exists()


def test_download_retries_with_fallback_url(monkeypatch):
    root = runtime_dir("setup_env_download_fallback")
    dest = root / "package.zip"
    package = setup_env.BinaryPackage(
        name="Package",
        version="1",
        url="https://primary.invalid/package.zip",
        sha256="unused",
        archive_name="package.zip",
        progress_start=0,
        progress_end=100,
        fallback_urls=("https://mirror.invalid/package.zip",),
    )
    calls = []

    def fake_download_once(package_arg, url, dest_arg, progress_cb=None, cancel_cb=None):
        calls.append(url)
        if url == package.url:
            raise TimeoutError("primary timed out")
        dest_arg.write_bytes(b"mirror")

    monkeypatch.setattr(setup_env, "_download_once", fake_download_once)

    setup_env._download(package, dest)

    assert calls == [package.url, package.fallback_urls[0]]
    assert dest.read_bytes() == b"mirror"


def test_ensure_ffmpeg_rechecks_after_setup_lock(monkeypatch):
    root = runtime_dir("setup_env_ffmpeg_recheck")
    ffmpeg = root / "bin" / "ffmpeg.exe"

    @contextmanager
    def fake_setup_lock(**kwargs):
        ffmpeg.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg.write_text("installed by another process", encoding="utf-8")
        yield root / ".setup.lock"

    async def fail_download(*args, **kwargs):
        raise AssertionError("download must not run after another process installed FFmpeg")

    monkeypatch.setattr(setup_env, "FFMPEG_EXE", ffmpeg)
    monkeypatch.setattr(setup_env, "setup_lock", fake_setup_lock)
    monkeypatch.setattr(setup_env, "download_file", fail_download)

    result = asyncio.run(setup_env.ensure_ffmpeg())

    assert result == ffmpeg


def test_ensure_environment_skips_optional_ffmpeg(monkeypatch):
    calls = []

    async def fake_ensure_obs(progress_cb=None, cancel_cb=None):
        calls.append("obs")

    async def fail_ensure_ffmpeg(*args, **kwargs):
        raise AssertionError("FFmpeg must be installed lazily during clip export")

    monkeypatch.setattr(setup_env, "ensure_obs_portable", fake_ensure_obs)
    monkeypatch.setattr(setup_env, "ensure_ffmpeg", fail_ensure_ffmpeg)
    monkeypatch.setattr(setup_env, "cleanup_setup_archives", lambda: [])

    asyncio.run(setup_env.ensure_environment())

    assert calls == ["obs"]
