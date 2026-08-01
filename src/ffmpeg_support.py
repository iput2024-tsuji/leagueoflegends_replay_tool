from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

FFMPEG_TESTED_VERSION = "8.1.1"
FFMPEG_DOWNLOAD_PAGE = "https://ffmpeg.org/download.html"


def _dedupe_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
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


def _absolute_path_directories(path_value: str | None) -> tuple[Path, ...]:
    if not path_value:
        return ()
    directories: list[Path] = []
    for raw_entry in path_value.split(os.pathsep):
        entry = raw_entry.strip().strip('"')
        if not entry:
            continue
        expanded = Path(os.path.expandvars(os.path.expanduser(entry)))
        if not expanded.is_absolute():
            continue
        directories.append(expanded)
    return _dedupe_paths(directories)


def ffmpeg_candidates(
    *,
    explicit_path: str | Path | None = None,
    bin_dir: str | Path | None = None,
    app_root: str | Path | None = None,
    path_value: str | None = None,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if explicit_path and str(explicit_path).strip():
        configured = Path(os.path.expandvars(os.path.expanduser(str(explicit_path))))
        if configured.is_absolute():
            candidates.append(configured)
    if bin_dir:
        candidates.append(Path(bin_dir) / "ffmpeg.exe")
    if app_root:
        root = Path(app_root)
        candidates.extend((root / "bin" / "ffmpeg.exe", root / "ffmpeg.exe"))
    candidates.extend(directory / "ffmpeg.exe" for directory in _absolute_path_directories(path_value))
    return _dedupe_paths(candidates)


def resolve_ffmpeg_executable(
    *,
    explicit_path: str | Path | None = None,
    bin_dir: str | Path | None = None,
    app_root: str | Path | None = None,
    path_value: str | None = None,
) -> Path | None:
    effective_path = os.environ.get("PATH", "") if path_value is None else path_value
    for candidate in ffmpeg_candidates(
        explicit_path=explicit_path,
        bin_dir=bin_dir,
        app_root=app_root,
        path_value=effective_path,
    ):
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def manual_setup_message(bin_dir: str | Path) -> str:
    destination = Path(bin_dir) / "ffmpeg.exe"
    return (
        "FFmpegは自動取得・同梱されません。\n"
        f"設定画面でffmpeg.exeを選択するか、{destination} に配置するか、\n"
        "絶対パスのディレクトリをシステムPATHへ追加してください。\n"
        f"検証実績: FFmpeg {FFMPEG_TESTED_VERSION} x64\n"
        f"公式入手案内: {FFMPEG_DOWNLOAD_PAGE}"
    )
