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
from src.obs_bootstrap import (
    OBSBootstrapper,
    _strictly_stop_managed_obs_processes,
    has_pending_obs_copy_transaction,
    has_pending_obs_settings_transaction,
    lexical_absolute_path,
    migrate_legacy_obs_installation,
    obs_config_mutation_guard,
    validate_obs_installation_path,
)
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
    try:
        if not validate_obs_installation_path(OBS_PORTABLE_DIR):
            return False
        if has_pending_obs_settings_transaction(OBS_PORTABLE_DIR):
            return False
        if has_pending_obs_copy_transaction(OBS_PORTABLE_DIR):
            return False
        return OBSBootstrapper(OBS_PORTABLE_DIR).check().ready
    except Exception:
        return False


def _dedupe_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        path = lexical_absolute_path(path)
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def legacy_obs_portable_dirs() -> tuple[Path, ...]:
    current_obs_dir = str(lexical_absolute_path(OBS_PORTABLE_DIR)).casefold()
    return tuple(
        path
        for path in _dedupe_paths(
            (
                LEGACY_ROOT_OBS_PORTABLE_DIR,
                LEGACY_OBS_PORTABLE_DIR,
                LEGACY_DATA_BIN_OBS_PORTABLE_DIR,
            )
        )
        if str(lexical_absolute_path(path)).casefold() != current_obs_dir
    )


def bootstrap_obs_portable_config(obs_dir: Path = OBS_PORTABLE_DIR) -> None:
    OBSBootstrapper(obs_dir).apply()


def _stop_obs_tree_for_settings_recovery(process_manager: OBSProcessManager) -> None:
    try:
        _strictly_stop_managed_obs_processes(
            process_manager.obs_dir,
            process_manager,
        )
    except Exception as exc:
        raise ManualSetupRequiredError(
            "管理対象OBSをstrict identityで安全に停止できません。"
            f"全OBSを手動終了してから再試行してください: {exc}"
        ) from exc


def migrate_legacy_obs_portable(progress_cb: ProgressCallback | None = None) -> bool:
    """Copy an existing legacy portable OBS without deleting the source."""

    if has_pending_obs_settings_transaction(OBS_PORTABLE_DIR):
        destination_manager = OBSProcessManager(OBS_PORTABLE_DIR)
        with obs_config_mutation_guard(
            OBS_PORTABLE_DIR,
            before_settings_recovery=lambda: _stop_obs_tree_for_settings_recovery(
                destination_manager
            ),
        ):
            pass

    for legacy_dir in legacy_obs_portable_dirs():
        if (
            validate_obs_installation_path(legacy_dir)
            and has_pending_obs_settings_transaction(legacy_dir)
        ):
            legacy_manager = OBSProcessManager(legacy_dir)
            with obs_config_mutation_guard(
                legacy_dir,
                before_settings_recovery=lambda manager=legacy_manager: (
                    _stop_obs_tree_for_settings_recovery(manager)
                ),
            ):
                pass

    def prepare_source(legacy_dir: Path) -> None:
        report(progress_cb, 10, f"旧OBS配置をローカル移行しています: {legacy_dir}")
        _stop_obs_tree_for_settings_recovery(OBSProcessManager(legacy_dir))

    migrated_from = migrate_legacy_obs_installation(
        OBS_PORTABLE_DIR,
        legacy_obs_portable_dirs(),
        prepare_source=prepare_source,
        finalize_destination=bootstrap_obs_portable_config,
    )
    if migrated_from is not None:
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

    obs_is_valid = validate_obs_installation_path(OBS_PORTABLE_DIR)
    if not obs_is_valid or has_pending_obs_copy_transaction(OBS_PORTABLE_DIR):
        await asyncio.to_thread(migrate_legacy_obs_portable, progress_cb)
    if (
        not validate_obs_installation_path(OBS_PORTABLE_DIR)
        or has_pending_obs_copy_transaction(OBS_PORTABLE_DIR)
    ):
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
