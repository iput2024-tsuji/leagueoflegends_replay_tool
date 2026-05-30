"""Initial environment bootstrap for local binary dependencies.

Large binaries stay out of Git. This script downloads pinned Windows builds,
verifies SHA256 checksums, and places the runtime files where the app expects.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from src.app_paths import get_app_root, get_user_data_root
from src.obs_bootstrap import OBSBootstrapper
from src.obs_process import OBSProcessManager

ProgressCallback = Callable[[int, str], None]

ROOT_DIR = get_app_root()
DATA_DIR = get_user_data_root()
BIN_DIR = DATA_DIR / "bin"
OBS_PORTABLE_DIR = DATA_DIR / "obs-portable"
LEGACY_ROOT_OBS_PORTABLE_DIR = ROOT_DIR / "obs-portable"
LEGACY_OBS_PORTABLE_DIR = ROOT_DIR / "bin" / "OBS-Studio"
LEGACY_DATA_BIN_OBS_PORTABLE_DIR = DATA_DIR / "bin" / "OBS-Studio"
FFMPEG_EXE = BIN_DIR / "ffmpeg.exe"
OBS_EXE = OBS_PORTABLE_DIR / "bin" / "64bit" / "obs64.exe"
LEGACY_OBS_EXE = LEGACY_OBS_PORTABLE_DIR / "bin" / "64bit" / "obs64.exe"

FFMPEG_VERSION = "8.1.1"
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.1-essentials_build.zip"
FFMPEG_ZIP_MIRROR_URL = (
    "https://github.com/GyanD/codexffmpeg/releases/download/8.1.1/ffmpeg-8.1.1-essentials_build.zip"
)
FFMPEG_ZIP_SHA256 = "6f58ce889f59c311410f7d2b18895b33c03456463486f3b1ebc93d97a0f54541"

OBS_VERSION = "32.1.2"
OBS_ZIP_URL = "https://github.com/obsproject/obs-studio/releases/download/32.1.2/OBS-Studio-32.1.2-Windows-x64.zip"
OBS_ZIP_SHA256 = "8d97e4563bd8d22d03e63042aa7dccede1d555c9bd35ce8a9e5019b0d0201bf6"

DOWNLOAD_SOCKET_TIMEOUT_SEC = 30
DOWNLOAD_TOTAL_TIMEOUT_SEC = 180
SETUP_LOCK_WAIT_TIMEOUT_SEC = 240
SETUP_LOCK_POLL_INTERVAL_SEC = 0.25
STALE_LOCK_GRACE_SEC = 60
STALE_WORKSPACE_MAX_AGE_SEC = 60 * 60
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class BinaryPackage:
    name: str
    version: str
    url: str
    sha256: str
    archive_name: str
    progress_start: int
    progress_end: int
    fallback_urls: tuple[str, ...] = ()


FFMPEG_PACKAGE = BinaryPackage(
    name="FFmpeg",
    version=FFMPEG_VERSION,
    url=FFMPEG_ZIP_URL,
    sha256=FFMPEG_ZIP_SHA256,
    archive_name=f"ffmpeg-{FFMPEG_VERSION}-essentials_build.zip",
    progress_start=0,
    progress_end=95,
    fallback_urls=(FFMPEG_ZIP_MIRROR_URL,),
)

OBS_PACKAGE = BinaryPackage(
    name="OBS Studio",
    version=OBS_VERSION,
    url=OBS_ZIP_URL,
    sha256=OBS_ZIP_SHA256,
    archive_name=f"OBS-Studio-{OBS_VERSION}-Windows-x64.zip",
    progress_start=0,
    progress_end=95,
)


def report(progress_cb: ProgressCallback | None, percent: int, message: str) -> None:
    percent = max(0, min(100, int(percent)))
    if progress_cb:
        progress_cb(percent, message)
    print(f"[{percent:3d}%] {message}")


def is_environment_ready() -> bool:
    if not OBS_EXE.exists():
        return False
    try:
        return OBSBootstrapper(OBS_PORTABLE_DIR).check().ready
    except Exception:
        return False


def _dedupe_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    result = []
    seen = set()
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


@contextmanager
def temporary_workspace(prefix: str, parent: Path | None = None) -> Iterator[Path]:
    base_dir = parent or (DATA_DIR / "downloads" / "_tmp")
    base_dir.mkdir(parents=True, exist_ok=True)
    workspace = base_dir / f"{prefix}{uuid.uuid4().hex}"
    workspace.mkdir(parents=True, exist_ok=False)
    try:
        yield workspace
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


class SetupCancelledError(RuntimeError):
    """Raised when the caller cancels a background setup operation."""


class SetupLockTimeoutError(RuntimeError):
    """Raised when another setup process keeps the setup lease too long."""


def _raise_if_cancelled(cancel_cb: Callable[[], bool] | None) -> None:
    if cancel_cb and cancel_cb():
        raise SetupCancelledError("セットアップをキャンセルしました。")


def _is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock_payload(lock_path: Path) -> dict[str, object]:
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _is_stale_setup_lock(lock_path: Path) -> bool:
    try:
        age_sec = max(0.0, time.time() - lock_path.stat().st_mtime)
    except FileNotFoundError:
        return False
    payload = _read_lock_payload(lock_path)
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid:
        return not _is_process_running(pid)
    return age_sec >= STALE_LOCK_GRACE_SEC


@contextmanager
def setup_lock(
    *,
    timeout_sec: float = SETUP_LOCK_WAIT_TIMEOUT_SEC,
    poll_interval_sec: float = SETUP_LOCK_POLL_INTERVAL_SEC,
    lock_path: Path | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> Iterator[Path]:
    path = lock_path or (DATA_DIR / "downloads" / ".setup.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    token = uuid.uuid4().hex

    while True:
        _raise_if_cancelled(cancel_cb)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _is_stale_setup_lock(path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise SetupLockTimeoutError(
                    "別のLoLReplayToolがセットアップ中です。完了後に再試行してください。"
                ) from None
            time.sleep(max(0.01, float(poll_interval_sec)))
            continue

        with os.fdopen(fd, "w", encoding="utf-8") as lock_file:
            json.dump({"pid": os.getpid(), "token": token, "created_at": time.time()}, lock_file)
        break

    try:
        yield path
    finally:
        payload = _read_lock_payload(path)
        if payload.get("token") == token:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def cleanup_stale_temporary_workspaces(
    *, max_age_sec: float = STALE_WORKSPACE_MAX_AGE_SEC, base_dir: Path | None = None
) -> list[Path]:
    tmp_dir = base_dir or (DATA_DIR / "downloads" / "_tmp")
    if not tmp_dir.exists():
        return []
    removed = []
    now = time.time()
    try:
        workspaces = list(tmp_dir.iterdir())
    except OSError:
        return []
    for path in workspaces:
        if not path.is_dir():
            continue
        try:
            age_sec = max(0.0, now - path.stat().st_mtime)
        except OSError:
            continue
        if age_sec < max(0.0, float(max_age_sec)):
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed.append(path)
    return removed


def _download_once(
    package: BinaryPackage,
    url: str,
    dest: Path,
    progress_cb: ProgressCallback | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "LoLReplayTool-setup/1.0"})
    started_at = time.monotonic()
    with urllib.request.urlopen(request, timeout=DOWNLOAD_SOCKET_TIMEOUT_SEC) as response:
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        with open(dest, "wb") as f:
            while True:
                _raise_if_cancelled(cancel_cb)
                if time.monotonic() - started_at > DOWNLOAD_TOTAL_TIMEOUT_SEC:
                    raise TimeoutError(f"{package.name} download exceeded {DOWNLOAD_TOTAL_TIMEOUT_SEC} seconds")
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    span = package.progress_end - package.progress_start
                    percent = package.progress_start + int((downloaded / total) * span)
                    downloaded_mb = downloaded / (1024 * 1024)
                    total_mb = total / (1024 * 1024)
                    report(
                        progress_cb,
                        percent,
                        f"{package.name} {package.version} をダウンロード中... {downloaded_mb:.1f}/{total_mb:.1f} MB",
                    )
        if downloaded == 0:
            raise RuntimeError(f"{package.name} download returned an empty file")
        if total and downloaded != total:
            raise RuntimeError(f"{package.name} download was incomplete: {downloaded}/{total} bytes")


def _download(
    package: BinaryPackage,
    dest: Path,
    progress_cb: ProgressCallback | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> None:
    urls = (package.url, *package.fallback_urls)
    failures = []
    for attempt, url in enumerate(urls, start=1):
        _raise_if_cancelled(cancel_cb)
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        if attempt > 1:
            report(progress_cb, package.progress_start, f"{package.name} の取得元を切り替えて再試行します...")
        try:
            _download_once(package, url, dest, progress_cb, cancel_cb)
            return
        except SetupCancelledError:
            raise
        except Exception as e:
            failures.append(f"{url}: {type(e).__name__}: {e}")

    raise RuntimeError(f"{package.name} download failed.\n" + "\n".join(failures))


async def download_file(
    package: BinaryPackage,
    dest: Path,
    progress_cb: ProgressCallback | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> None:
    await asyncio.to_thread(_download, package, dest, progress_cb, cancel_cb)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def verify_sha256(path: Path, expected_sha256: str, label: str) -> None:
    actual = await asyncio.to_thread(_sha256, path)
    if actual.lower() != expected_sha256.lower():
        raise RuntimeError(f"{label} checksum mismatch.\nexpected: {expected_sha256}\nactual:   {actual}")


def _extract_ffmpeg(zip_path: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        members = [name for name in archive.namelist() if name.replace("\\", "/").endswith("/bin/ffmpeg.exe")]
        if not members:
            raise RuntimeError("ffmpeg.exe was not found inside the downloaded ZIP.")
        with archive.open(members[0]) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    return dest


def _copy_tree_contents(src_dir: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        if item.name in {".lol_replay_obs_lease.json", "temp_appdata"}:
            continue
        target = dest_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)


def cleanup_obs_debug_symbols(obs_dir: Path) -> list[Path]:
    removed = []
    if not obs_dir.exists():
        return removed
    for path in obs_dir.rglob("*.pdb"):
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed.append(path)
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return removed


def _find_obs_root(extract_dir: Path) -> Path:
    matches = list(extract_dir.rglob("bin/64bit/obs64.exe"))
    if not matches:
        raise RuntimeError("obs64.exe was not found inside the downloaded ZIP.")
    # obs64.exe -> 64bit -> bin -> OBS root
    return matches[0].parents[2]


def kill_stale_obs_processes() -> None:
    OBSProcessManager(OBS_PORTABLE_DIR).kill_stale_managed_processes()


def _extract_obs(zip_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    # Extract next to the destination so security policies on %TEMP% do not
    # block executable-looking files during test/build setup.
    with temporary_workspace("lol-replay-obs-extract-", parent=dest_dir.parent) as extract_dir:
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_dir)
        obs_root = _find_obs_root(extract_dir)
        _copy_tree_contents(obs_root, dest_dir)
        cleanup_obs_debug_symbols(dest_dir)

    obs_exe = dest_dir / "bin" / "64bit" / "obs64.exe"
    if not obs_exe.exists():
        raise RuntimeError(f"obs64.exe was not found after extraction: {obs_exe}")
    bootstrap_obs_portable_config(dest_dir)
    return dest_dir


def bootstrap_obs_portable_config(obs_dir: Path = OBS_PORTABLE_DIR) -> None:
    OBSBootstrapper(obs_dir).apply()


def cleanup_legacy_archives(bin_dir: Path = BIN_DIR) -> list[Path]:
    removed = []
    for pattern in ("OBS-Studio-*.zip", "ffmpeg-*.zip", "ffmpeg-*.7z"):
        for path in bin_dir.glob(pattern):
            if not path.is_file():
                continue
            try:
                path.unlink()
                removed.append(path)
            except FileNotFoundError:
                continue
    return removed


def cleanup_setup_archives() -> list[Path]:
    removed = []
    for bin_dir in _dedupe_paths((BIN_DIR, ROOT_DIR / "bin")):
        removed.extend(cleanup_legacy_archives(bin_dir))
    return removed


def migrate_legacy_obs_portable(progress_cb: ProgressCallback | None = None) -> bool:
    if OBS_EXE.exists():
        return False

    for legacy_dir in legacy_obs_portable_dirs():
        legacy_exe = legacy_dir / "bin" / "64bit" / "obs64.exe"
        if not legacy_exe.exists():
            continue
        report(progress_cb, OBS_PACKAGE.progress_start, f"旧OBS配置を移行しています: {legacy_dir}")
        OBSProcessManager(legacy_dir).kill_stale_managed_processes()
        _copy_tree_contents(legacy_dir, OBS_PORTABLE_DIR)
        removed_debug_files = cleanup_obs_debug_symbols(OBS_PORTABLE_DIR)
        if removed_debug_files:
            report(progress_cb, OBS_PACKAGE.progress_end, f"OBSデバッグファイルを削除しました: {len(removed_debug_files)}件")
        bootstrap_obs_portable_config(OBS_PORTABLE_DIR)
        report(progress_cb, OBS_PACKAGE.progress_end, f"OBS migrated: {OBS_PORTABLE_DIR}")
        return True

    return False


async def ensure_ffmpeg(
    progress_cb: ProgressCallback | None = None, cancel_cb: Callable[[], bool] | None = None
) -> Path:
    if FFMPEG_EXE.exists():
        report(progress_cb, FFMPEG_PACKAGE.progress_end, f"FFmpeg exists: {FFMPEG_EXE}")
        return FFMPEG_EXE

    with setup_lock(cancel_cb=cancel_cb):
        if FFMPEG_EXE.exists():
            report(progress_cb, FFMPEG_PACKAGE.progress_end, f"FFmpeg exists: {FFMPEG_EXE}")
            return FFMPEG_EXE
        cleanup_stale_temporary_workspaces(max_age_sec=0)
        with temporary_workspace("lol-replay-ffmpeg-") as tmp:
            zip_path = tmp / FFMPEG_PACKAGE.archive_name
            report(progress_cb, FFMPEG_PACKAGE.progress_start, "FFmpegを準備しています...")
            await download_file(FFMPEG_PACKAGE, zip_path, progress_cb, cancel_cb)
            report(progress_cb, FFMPEG_PACKAGE.progress_end, "FFmpegのSHA256を検証しています...")
            await verify_sha256(zip_path, FFMPEG_PACKAGE.sha256, FFMPEG_PACKAGE.name)
            await asyncio.to_thread(_extract_ffmpeg, zip_path, FFMPEG_EXE)

    report(progress_cb, FFMPEG_PACKAGE.progress_end, f"FFmpeg installed: {FFMPEG_EXE}")
    return FFMPEG_EXE


async def ensure_obs_portable(
    progress_cb: ProgressCallback | None = None, cancel_cb: Callable[[], bool] | None = None
) -> Path:
    if OBS_EXE.exists():
        removed_debug_files = await asyncio.to_thread(cleanup_obs_debug_symbols, OBS_PORTABLE_DIR)
        bootstrap_obs_portable_config(OBS_PORTABLE_DIR)
        if removed_debug_files:
            report(progress_cb, OBS_PACKAGE.progress_end, f"OBSデバッグファイルを削除しました: {len(removed_debug_files)}件")
        report(progress_cb, OBS_PACKAGE.progress_end, f"OBS exists: {OBS_EXE}")
        return OBS_PORTABLE_DIR

    with setup_lock(cancel_cb=cancel_cb):
        if OBS_EXE.exists():
            removed_debug_files = await asyncio.to_thread(cleanup_obs_debug_symbols, OBS_PORTABLE_DIR)
            bootstrap_obs_portable_config(OBS_PORTABLE_DIR)
            if removed_debug_files:
                report(progress_cb, OBS_PACKAGE.progress_end, f"OBSデバッグファイルを削除しました: {len(removed_debug_files)}件")
            report(progress_cb, OBS_PACKAGE.progress_end, f"OBS exists: {OBS_EXE}")
            return OBS_PORTABLE_DIR
        cleanup_stale_temporary_workspaces(max_age_sec=0)
        if await asyncio.to_thread(migrate_legacy_obs_portable, progress_cb):
            return OBS_PORTABLE_DIR
        with temporary_workspace("lol-replay-obs-") as tmp:
            zip_path = tmp / OBS_PACKAGE.archive_name
            report(progress_cb, OBS_PACKAGE.progress_start, "OBS Studio Portableを準備しています...")
            await download_file(OBS_PACKAGE, zip_path, progress_cb, cancel_cb)
            report(progress_cb, OBS_PACKAGE.progress_end, "OBS StudioのSHA256を検証しています...")
            await verify_sha256(zip_path, OBS_PACKAGE.sha256, OBS_PACKAGE.name)
            await asyncio.to_thread(_extract_obs, zip_path, OBS_PORTABLE_DIR)

    report(progress_cb, OBS_PACKAGE.progress_end, f"OBS installed: {OBS_PORTABLE_DIR}")
    return OBS_PORTABLE_DIR


async def ensure_environment(progress_cb: ProgressCallback | None = None) -> None:
    await ensure_obs_portable(progress_cb)
    removed_archives = await asyncio.to_thread(cleanup_setup_archives)
    if removed_archives:
        report(progress_cb, 99, f"不要なセットアップZIPを削除しました: {len(removed_archives)}件")
    report(progress_cb, 100, "環境構築が完了しました。")


def run_setup(progress_cb: ProgressCallback | None = None) -> None:
    asyncio.run(ensure_environment(progress_cb))


async def main() -> int:
    try:
        await ensure_environment()
        return 0
    except Exception as e:
        print(f"Setup failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
