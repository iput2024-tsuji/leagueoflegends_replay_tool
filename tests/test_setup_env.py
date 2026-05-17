import shutil
import zipfile
from pathlib import Path

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
