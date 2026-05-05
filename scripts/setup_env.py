"""Initial environment bootstrap for local binary dependencies.

This script intentionally keeps large binaries out of Git. It downloads the
Windows FFmpeg essentials ZIP and installs only ffmpeg.exe into ./bin.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT_DIR / "bin"
FFMPEG_EXE = BIN_DIR / "ffmpeg.exe"

FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_SHA256_URL = FFMPEG_ZIP_URL + ".sha256"


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "LoLReplayTool-setup/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
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
                    percent = int(downloaded / total * 100)
                    print(f"\rDownloading {dest.name}: {percent:3d}%", end="", flush=True)
    if total:
        print()


async def download_file(url: str, dest: Path) -> None:
    await asyncio.to_thread(_download, url, dest)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_sha256_file(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None
    return text.split()[0].lower()


async def verify_sha256(zip_path: Path, sha_path: Path) -> None:
    try:
        await download_file(FFMPEG_SHA256_URL, sha_path)
    except Exception as e:
        print(f"Warning: SHA256 file could not be downloaded. Skipping verification. ({e})")
        return

    expected = _parse_sha256_file(sha_path)
    if not expected:
        print("Warning: SHA256 file was empty. Skipping verification.")
        return

    actual = await asyncio.to_thread(_sha256, zip_path)
    if actual.lower() != expected:
        raise RuntimeError(
            "FFmpeg ZIP checksum mismatch.\n"
            f"expected: {expected}\n"
            f"actual:   {actual}"
        )


def _extract_ffmpeg(zip_path: Path, dest: Path) -> Path:
    with zipfile.ZipFile(zip_path) as archive:
        members = [name for name in archive.namelist() if name.replace("\\", "/").endswith("/bin/ffmpeg.exe")]
        if not members:
            raise RuntimeError("ffmpeg.exe was not found inside the downloaded ZIP.")
        member = members[0]
        with archive.open(member) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    return dest


async def ensure_ffmpeg() -> Path:
    if FFMPEG_EXE.exists():
        print(f"FFmpeg already exists: {FFMPEG_EXE}")
        return FFMPEG_EXE

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lol-replay-ffmpeg-") as tmp:
        tmp_dir = Path(tmp)
        zip_path = tmp_dir / "ffmpeg-release-essentials.zip"
        sha_path = tmp_dir / "ffmpeg-release-essentials.zip.sha256"

        print("FFmpeg was not found. Downloading Windows essentials build...")
        print(f"Source: {FFMPEG_ZIP_URL}")
        await download_file(FFMPEG_ZIP_URL, zip_path)
        await verify_sha256(zip_path, sha_path)
        await asyncio.to_thread(_extract_ffmpeg, zip_path, FFMPEG_EXE)

    print(f"FFmpeg installed: {FFMPEG_EXE}")
    return FFMPEG_EXE


async def main() -> int:
    try:
        await ensure_ffmpeg()
        return 0
    except Exception as e:
        print(f"Setup failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
