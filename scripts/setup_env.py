"""Initial environment bootstrap for local binary dependencies.

Large binaries stay out of Git. This script downloads pinned Windows builds,
verifies SHA256 checksums, and places the runtime files where the app expects.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ProgressCallback = Callable[[int, str], None]

ROOT_DIR = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT_DIR / "bin"
OBS_PORTABLE_DIR = ROOT_DIR / "obs-portable"
FFMPEG_EXE = BIN_DIR / "ffmpeg.exe"
OBS_EXE = OBS_PORTABLE_DIR / "bin" / "64bit" / "obs64.exe"

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
    return FFMPEG_EXE.exists() and OBS_EXE.exists()


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


def _extract_obs(zip_path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(dest_dir)
    obs_exe = dest_dir / "bin" / "64bit" / "obs64.exe"
    if not obs_exe.exists():
        raise RuntimeError(f"obs64.exe was not found after extraction: {obs_exe}")
    bootstrap_obs_portable_config(dest_dir)
    return dest_dir


def bootstrap_obs_portable_config(obs_dir: Path = OBS_PORTABLE_DIR) -> None:
    obs_dir.mkdir(parents=True, exist_ok=True)
    for marker_name in ("obs_portable_mode.txt", "portable_mode.txt"):
        marker = obs_dir / marker_name
        if not marker.exists():
            marker.write_text("", encoding="utf-8")

    obs_config_dir = obs_dir / "config" / "obs-studio"
    obs_config_dir.mkdir(parents=True, exist_ok=True)
    global_ini = obs_config_dir / "global.ini"
    text = global_ini.read_text(encoding="utf-8-sig", errors="replace") if global_ini.exists() else ""
    if "[BasicWindow]" not in text:
        text = (text.rstrip() + "\n\n[BasicWindow]\n").lstrip()

    desired = {
        "SysTrayEnabled": "false",
        "SysTrayWhenStarted": "false",
        "SysTrayMinimizeToTray": "false",
        "HideTrayIcon": "true",
    }
    lines = []
    in_basic = False
    inserted = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_basic and not inserted:
                lines.extend(f"{key}={value}" for key, value in desired.items())
                inserted = True
            in_basic = stripped.lower() == "[basicwindow]"
            lines.append(line)
            continue
        if in_basic and ("systray" in stripped.lower() or "hidetray" in stripped.lower()):
            continue
        lines.append(line)
    if in_basic and not inserted:
        lines.extend(f"{key}={value}" for key, value in desired.items())
    global_ini.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


async def ensure_ffmpeg(progress_cb: ProgressCallback | None = None) -> Path:
    if FFMPEG_EXE.exists():
        report(progress_cb, FFMPEG_PACKAGE.progress_end, f"FFmpeg exists: {FFMPEG_EXE}")
        return FFMPEG_EXE

    with tempfile.TemporaryDirectory(prefix="lol-replay-ffmpeg-") as tmp:
        zip_path = Path(tmp) / FFMPEG_PACKAGE.archive_name
        report(progress_cb, FFMPEG_PACKAGE.progress_start, "FFmpegを準備しています...")
        await download_file(FFMPEG_PACKAGE, zip_path, progress_cb)
        report(progress_cb, FFMPEG_PACKAGE.progress_end, "FFmpegのSHA256を検証しています...")
        await verify_sha256(zip_path, FFMPEG_PACKAGE.sha256, FFMPEG_PACKAGE.name)
        await asyncio.to_thread(_extract_ffmpeg, zip_path, FFMPEG_EXE)

    report(progress_cb, FFMPEG_PACKAGE.progress_end, f"FFmpeg installed: {FFMPEG_EXE}")
    return FFMPEG_EXE


async def ensure_obs_portable(progress_cb: ProgressCallback | None = None) -> Path:
    if OBS_EXE.exists():
        bootstrap_obs_portable_config(OBS_PORTABLE_DIR)
        report(progress_cb, OBS_PACKAGE.progress_end, f"OBS exists: {OBS_EXE}")
        return OBS_PORTABLE_DIR

    with tempfile.TemporaryDirectory(prefix="lol-replay-obs-") as tmp:
        zip_path = Path(tmp) / OBS_PACKAGE.archive_name
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
