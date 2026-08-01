"""Validate and configure user-provided OBS Studio.

This module intentionally performs no network access and does not unpack or
install third-party binaries. Users obtain OBS Studio from the upstream
project and place a dedicated portable copy where the application expects it.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

from src.app_paths import get_app_root, get_user_data_root
from src.obs_bootstrap import OBSBootstrapper, copy_obs_tree_contents, is_obs_copy_in_progress
from src.obs_process import OBSProcessManager

ProgressCallback = Callable[[int, str], None]

ROOT_DIR = get_app_root()
DATA_DIR = get_user_data_root()
BIN_DIR = DATA_DIR / "bin"
OBS_PORTABLE_DIR = DATA_DIR / "obs-portable"
LEGACY_ROOT_OBS_PORTABLE_DIR = ROOT_DIR / "obs-portable"
LEGACY_OBS_PORTABLE_DIR = ROOT_DIR / "bin" / "OBS-Studio"
LEGACY_DATA_BIN_OBS_PORTABLE_DIR = DATA_DIR / "bin" / "OBS-Studio"
OBS_EXE = OBS_PORTABLE_DIR / "bin" / "64bit" / "obs64.exe"

OBS_TESTED_VERSION = "32.1.2"
OBS_DOWNLOAD_PAGE = "https://github.com/obsproject/obs-studio/releases"
OBS_ARCHIVE_NAME = f"OBS-Studio-{OBS_TESTED_VERSION}-Windows-x64.zip"


class ManualSetupRequiredError(RuntimeError):
    """Raised when a user-provided runtime has not been placed yet."""


def report(progress_cb: ProgressCallback | None, percent: int, message: str) -> None:
    percent = max(0, min(100, int(percent)))
    if progress_cb:
        progress_cb(percent, message)
    print(f"[{percent:3d}%] {message}")


def obs_manual_setup_message() -> str:
    return (
        "OBS Studioは自動取得されません。\n"
        f"公式Releaseから {OBS_ARCHIVE_NAME} を取得し、展開後に\n"
        f"{OBS_EXE}\n"
        "が存在する状態にしてください。\n"
        f"公式ページ: {OBS_DOWNLOAD_PAGE}"
    )


def is_environment_ready() -> bool:
    if not OBS_EXE.is_file() or is_obs_copy_in_progress(OBS_PORTABLE_DIR):
        return False
    try:
        return OBSBootstrapper(OBS_PORTABLE_DIR).check().ready
    except Exception:
        return False


def _dedupe_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            key = str(path.resolve()).casefold()
        except Exception:
            key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def legacy_obs_portable_dirs() -> tuple[Path, ...]:
    try:
        current_obs_dir = str(OBS_PORTABLE_DIR.resolve()).casefold()
    except Exception:
        current_obs_dir = str(OBS_PORTABLE_DIR).casefold()
    return tuple(
        path
        for path in _dedupe_paths(
            (
                LEGACY_ROOT_OBS_PORTABLE_DIR,
                LEGACY_OBS_PORTABLE_DIR,
                LEGACY_DATA_BIN_OBS_PORTABLE_DIR,
            )
        )
        if str(path.resolve()).casefold() != current_obs_dir
    )


def bootstrap_obs_portable_config(obs_dir: Path = OBS_PORTABLE_DIR) -> None:
    OBSBootstrapper(obs_dir).apply()


def migrate_legacy_obs_portable(progress_cb: ProgressCallback | None = None) -> bool:
    """Copy an existing legacy portable OBS without deleting the source."""

    if OBS_EXE.is_file() and not is_obs_copy_in_progress(OBS_PORTABLE_DIR):
        return False

    for legacy_dir in legacy_obs_portable_dirs():
        legacy_exe = legacy_dir / "bin" / "64bit" / "obs64.exe"
        if not legacy_exe.is_file():
            continue
        report(progress_cb, 10, f"旧OBS配置をローカル移行しています: {legacy_dir}")
        OBSProcessManager(legacy_dir).kill_stale_managed_processes()
        copy_obs_tree_contents(legacy_dir, OBS_PORTABLE_DIR)
        bootstrap_obs_portable_config(OBS_PORTABLE_DIR)
        report(progress_cb, 90, f"OBS migrated: {OBS_PORTABLE_DIR}")
        return True

    return False


async def ensure_obs_portable(
    progress_cb: ProgressCallback | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> Path:
    """Validate a user-provided portable OBS and apply managed settings."""

    if cancel_cb and cancel_cb():
        raise RuntimeError("セットアップをキャンセルしました。")

    if not OBS_EXE.is_file() or is_obs_copy_in_progress(OBS_PORTABLE_DIR):
        await asyncio.to_thread(migrate_legacy_obs_portable, progress_cb)
    if not OBS_EXE.is_file() or is_obs_copy_in_progress(OBS_PORTABLE_DIR):
        raise ManualSetupRequiredError(obs_manual_setup_message())

    if cancel_cb and cancel_cb():
        raise RuntimeError("セットアップをキャンセルしました。")
    report(progress_cb, 50, "手動配置されたOBS Studioを検査しています...")
    await asyncio.to_thread(bootstrap_obs_portable_config, OBS_PORTABLE_DIR)
    report(progress_cb, 100, f"OBS is ready: {OBS_PORTABLE_DIR}")
    return OBS_PORTABLE_DIR


async def ensure_environment(progress_cb: ProgressCallback | None = None) -> None:
    await ensure_obs_portable(progress_cb)
    report(progress_cb, 100, "外部ツールの確認が完了しました。")


def run_setup(progress_cb: ProgressCallback | None = None) -> None:
    asyncio.run(ensure_environment(progress_cb))


async def main() -> int:
    try:
        await ensure_environment()
        return 0
    except Exception as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
