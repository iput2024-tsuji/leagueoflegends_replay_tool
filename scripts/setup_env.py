"""Initial environment bootstrap for local binary dependencies.

Large binaries stay out of Git. This script downloads pinned Windows builds,
verifies SHA256 checksums, and places the runtime files where the app expects.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
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
FFMPEG_ZIP_SHA256 = "6f58ce889f59c311410f7d2b18895b33c03456463486f3b1ebc93d97a0f54541"

OBS_VERSION = "32.1.2"
OBS_ZIP_URL = "https://github.com/obsproject/obs-studio/releases/download/32.1.2/OBS-Studio-32.1.2-Windows-x64.zip"
OBS_ZIP_SHA256 = "8d97e4563bd8d22d03e63042aa7dccede1d555c9bd35ce8a9e5019b0d0201bf6"


@dataclass(frozen=True)
class BinaryPackage:
    name: str
    version: str
    url: str
    sha256: str
    archive_name: str
    progress_start: int
    progress_end: int


FFMPEG_PACKAGE = BinaryPackage(
    name="FFmpeg",
    version=FFMPEG_VERSION,
    url=FFMPEG_ZIP_URL,
    sha256=FFMPEG_ZIP_SHA256,
    archive_name=f"ffmpeg-{FFMPEG_VERSION}-essentials_build.zip",
    progress_start=0,
    progress_end=45,
)

OBS_PACKAGE = BinaryPackage(
    name="OBS Studio",
    version=OBS_VERSION,
    url=OBS_ZIP_URL,
    sha256=OBS_ZIP_SHA256,
    archive_name=f"OBS-Studio-{OBS_VERSION}-Windows-x64.zip",
    progress_start=45,
    progress_end=95,
)


def report(progress_cb: ProgressCallback | None, percent: int, message: str) -> None:
    percent = max(0, min(100, int(percent)))
    if progress_cb:
        progress_cb(percent, message)
    print(f"[{percent:3d}%] {message}")


def is_environment_ready() -> bool:
    if not (FFMPEG_EXE.exists() and OBS_EXE.exists()):
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


def _download(package: BinaryPackage, dest: Path, progress_cb: ProgressCallback | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(package.url, headers={"User-Agent": "LoLReplayTool-setup/1.0"})
    with urllib.request.urlopen(request, timeout=180) as response:
        total = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        with open(dest, "wb") as f:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    span = package.progress_end - package.progress_start
                    percent = package.progress_start + int((downloaded / total) * span)
                    report(progress_cb, percent, f"{package.name} {package.version} をダウンロード中...")


async def download_file(package: BinaryPackage, dest: Path, progress_cb: ProgressCallback | None = None) -> None:
    await asyncio.to_thread(_download, package, dest, progress_cb)


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


async def ensure_ffmpeg(progress_cb: ProgressCallback | None = None) -> Path:
    if FFMPEG_EXE.exists():
        report(progress_cb, FFMPEG_PACKAGE.progress_end, f"FFmpeg exists: {FFMPEG_EXE}")
        return FFMPEG_EXE

    with temporary_workspace("lol-replay-ffmpeg-") as tmp:
        zip_path = tmp / FFMPEG_PACKAGE.archive_name
        report(progress_cb, FFMPEG_PACKAGE.progress_start, "FFmpegを準備しています...")
        await download_file(FFMPEG_PACKAGE, zip_path, progress_cb)
        report(progress_cb, FFMPEG_PACKAGE.progress_end, "FFmpegのSHA256を検証しています...")
        await verify_sha256(zip_path, FFMPEG_PACKAGE.sha256, FFMPEG_PACKAGE.name)
        await asyncio.to_thread(_extract_ffmpeg, zip_path, FFMPEG_EXE)

    report(progress_cb, FFMPEG_PACKAGE.progress_end, f"FFmpeg installed: {FFMPEG_EXE}")
    return FFMPEG_EXE


async def ensure_obs_portable(progress_cb: ProgressCallback | None = None) -> Path:
    if OBS_EXE.exists():
        removed_debug_files = await asyncio.to_thread(cleanup_obs_debug_symbols, OBS_PORTABLE_DIR)
        bootstrap_obs_portable_config(OBS_PORTABLE_DIR)
        if removed_debug_files:
            report(progress_cb, OBS_PACKAGE.progress_end, f"OBSデバッグファイルを削除しました: {len(removed_debug_files)}件")
        report(progress_cb, OBS_PACKAGE.progress_end, f"OBS exists: {OBS_EXE}")
        return OBS_PORTABLE_DIR

    if await asyncio.to_thread(migrate_legacy_obs_portable, progress_cb):
        return OBS_PORTABLE_DIR

    with temporary_workspace("lol-replay-obs-") as tmp:
        zip_path = tmp / OBS_PACKAGE.archive_name
        report(progress_cb, OBS_PACKAGE.progress_start, "OBS Studio Portableを準備しています...")
        await download_file(OBS_PACKAGE, zip_path, progress_cb)
        report(progress_cb, OBS_PACKAGE.progress_end, "OBS StudioのSHA256を検証しています...")
        await verify_sha256(zip_path, OBS_PACKAGE.sha256, OBS_PACKAGE.name)
        await asyncio.to_thread(_extract_obs, zip_path, OBS_PORTABLE_DIR)

    report(progress_cb, OBS_PACKAGE.progress_end, f"OBS installed: {OBS_PORTABLE_DIR}")
    return OBS_PORTABLE_DIR


async def ensure_environment(progress_cb: ProgressCallback | None = None) -> None:
    await ensure_ffmpeg(progress_cb)
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
