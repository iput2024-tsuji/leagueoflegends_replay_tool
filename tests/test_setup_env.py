import asyncio
import inspect
import os
import subprocess
from pathlib import Path

import pytest

from scripts import setup_env
from src import obs_bootstrap


def _configure_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "install"
    data = tmp_path / "data"
    obs_dir = data / "obs-portable"
    obs_exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    monkeypatch.setattr(setup_env, "ROOT_DIR", root)
    monkeypatch.setattr(setup_env, "DATA_DIR", data)
    monkeypatch.setattr(setup_env, "BIN_DIR", data / "bin")
    monkeypatch.setattr(setup_env, "OBS_PORTABLE_DIR", obs_dir)
    monkeypatch.setattr(setup_env, "OBS_EXE", obs_exe)
    monkeypatch.setattr(setup_env, "LEGACY_ROOT_OBS_PORTABLE_DIR", root / "obs-portable")
    monkeypatch.setattr(setup_env, "LEGACY_OBS_PORTABLE_DIR", root / "bin" / "OBS-Studio")
    monkeypatch.setattr(
        setup_env,
        "LEGACY_DATA_BIN_OBS_PORTABLE_DIR",
        data / "bin" / "OBS-Studio",
    )
    return obs_dir, obs_exe


def _write_fake_obs(obs_exe: Path) -> None:
    obs_exe.parent.mkdir(parents=True, exist_ok=True)
    obs_exe.write_bytes(b"fake obs")


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


def test_setup_module_has_no_network_or_archive_install_path():
    source = inspect.getsource(setup_env)

    assert "urllib" not in source
    assert "urlopen" not in source
    assert "zipfile" not in source
    assert "unpack_archive" not in source
    assert not hasattr(setup_env, "download_file")
    assert not hasattr(setup_env, "ensure_ffmpeg")


def test_manual_setup_message_identifies_upstream_and_exact_destination(monkeypatch, tmp_path):
    _obs_dir, obs_exe = _configure_paths(monkeypatch, tmp_path)

    message = setup_env.obs_manual_setup_message()

    assert setup_env.OBS_ARCHIVE_NAME in message
    assert str(obs_exe) in message
    assert setup_env.OBS_DOWNLOAD_PAGE in message
    assert "自動取得されません" in message


def test_missing_obs_requires_manual_setup_without_creating_a_runtime(monkeypatch, tmp_path):
    obs_dir, _obs_exe = _configure_paths(monkeypatch, tmp_path)

    with pytest.raises(setup_env.ManualSetupRequiredError, match="自動取得されません"):
        asyncio.run(setup_env.ensure_obs_portable())

    assert not obs_dir.exists()


def test_user_provided_obs_is_bootstrapped_and_reported_ready(monkeypatch, tmp_path):
    obs_dir, obs_exe = _configure_paths(monkeypatch, tmp_path)
    _write_fake_obs(obs_exe)
    progress: list[tuple[int, str]] = []

    result = asyncio.run(setup_env.ensure_obs_portable(lambda percent, text: progress.append((percent, text))))

    assert result == obs_dir
    assert setup_env.is_environment_ready() is True
    assert (obs_dir / "obs_portable_mode.txt").is_file()
    assert (obs_dir / "config" / "obs-studio" / "global.ini").is_file()
    assert (obs_dir / "config" / "obs-studio" / "user.ini").is_file()
    assert progress[-1] == (100, f"OBS is ready: {obs_dir}")


def test_existing_obs_root_reparse_is_rejected_without_external_write(monkeypatch, tmp_path):
    obs_dir, _obs_exe = _configure_paths(monkeypatch, tmp_path)
    external = tmp_path / "external-obs"
    external_executable = external / "bin" / "64bit" / "obs64.exe"
    _write_fake_obs(external_executable)
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    obs_dir.parent.mkdir(parents=True)
    _create_directory_link(obs_dir, external)

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="reparse point"):
        asyncio.run(setup_env.ensure_obs_portable())

    assert setup_env.is_environment_ready() is False
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert external_executable.read_bytes() == b"fake obs"
    assert not (external / obs_bootstrap.PORTABLE_OBS_MARKER_NAME).exists()
    assert not (external / "config" / "obs-studio" / "global.ini").exists()


def test_legacy_obs_is_copied_without_deleting_user_files(monkeypatch, tmp_path):
    obs_dir, _obs_exe = _configure_paths(monkeypatch, tmp_path)
    legacy_dir = setup_env.LEGACY_OBS_PORTABLE_DIR
    legacy_exe = legacy_dir / "bin" / "64bit" / "obs64.exe"
    _write_fake_obs(legacy_exe)
    user_file = legacy_dir / "config" / "obs-studio" / "basic" / "profiles" / "user.txt"
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text("keep me", encoding="utf-8")
    (legacy_dir / ".lol_replay_obs_lease.json").write_text("{}", encoding="utf-8")
    (legacy_dir / "temp_appdata").mkdir()

    assert setup_env.migrate_legacy_obs_portable() is True

    assert legacy_exe.is_file()
    assert user_file.read_text(encoding="utf-8") == "keep me"
    assert (obs_dir / "bin" / "64bit" / "obs64.exe").is_file()
    assert (obs_dir / user_file.relative_to(legacy_dir)).read_text(encoding="utf-8") == "keep me"
    assert not (obs_dir / ".lol_replay_obs_lease.json").exists()
    assert not (obs_dir / "temp_appdata").exists()
    assert setup_env.is_environment_ready() is True


def test_existing_destination_prevents_legacy_copy(monkeypatch, tmp_path):
    obs_dir, obs_exe = _configure_paths(monkeypatch, tmp_path)
    _write_fake_obs(obs_exe)
    legacy_exe = setup_env.LEGACY_OBS_PORTABLE_DIR / "bin" / "64bit" / "obs64.exe"
    _write_fake_obs(legacy_exe)

    assert setup_env.migrate_legacy_obs_portable() is False
    assert obs_exe.read_bytes() == b"fake obs"
    assert obs_dir.is_dir()


def test_cancelled_setup_does_not_copy_legacy_obs(monkeypatch, tmp_path):
    obs_dir, _obs_exe = _configure_paths(monkeypatch, tmp_path)
    legacy_exe = setup_env.LEGACY_OBS_PORTABLE_DIR / "bin" / "64bit" / "obs64.exe"
    _write_fake_obs(legacy_exe)

    with pytest.raises(RuntimeError, match="キャンセル"):
        asyncio.run(setup_env.ensure_obs_portable(cancel_cb=lambda: True))

    assert not obs_dir.exists()
    assert legacy_exe.is_file()


def test_partial_legacy_copy_is_retried_without_deleting_source(monkeypatch, tmp_path):
    obs_dir, obs_exe = _configure_paths(monkeypatch, tmp_path)
    legacy_dir = setup_env.LEGACY_OBS_PORTABLE_DIR
    legacy_exe = legacy_dir / "bin" / "64bit" / "obs64.exe"
    _write_fake_obs(legacy_exe)
    real_copy_file = obs_bootstrap._copy_inventory_file

    def fail_after_executable(*args, **kwargs):
        real_copy_file(*args, **kwargs)
        raise OSError("simulated interrupted copy")

    monkeypatch.setattr(obs_bootstrap, "_copy_inventory_file", fail_after_executable)
    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="interrupted"):
        setup_env.migrate_legacy_obs_portable()

    assert obs_exe.is_file()
    assert legacy_exe.is_file()
    assert obs_bootstrap.is_obs_copy_in_progress(obs_dir) is True
    assert setup_env.is_environment_ready() is False

    monkeypatch.setattr(obs_bootstrap, "_copy_inventory_file", real_copy_file)
    assert setup_env.migrate_legacy_obs_portable() is True

    assert obs_bootstrap.is_obs_copy_in_progress(obs_dir) is False
    assert legacy_exe.is_file()
    assert setup_env.is_environment_ready() is True


def test_ensure_environment_only_validates_obs(monkeypatch):
    calls: list[str] = []

    async def fake_ensure_obs(progress_cb=None, cancel_cb=None):
        calls.append("obs")
        return Path("obs-portable")

    monkeypatch.setattr(setup_env, "ensure_obs_portable", fake_ensure_obs)

    asyncio.run(setup_env.ensure_environment())

    assert calls == ["obs"]
