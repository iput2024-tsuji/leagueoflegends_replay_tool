from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

try:
    from .session_log import load_session_payload
except ImportError:
    from session_log import load_session_payload

LOGGER = logging.getLogger("lol_replay.storage")
VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".flv", ".mov", ".avi"})


class StoragePaths(Protocol):
    recordings_dir: Path
    json_dir: Path


class StorageLimit(Protocol):
    max_size_bytes: int | None


class StorageConfig(Protocol):
    paths: StoragePaths
    storage: StorageLimit


def parse_max_storage_bytes(storage_cfg: dict[str, Any], default_max_gb: float = 50) -> int | None:
    max_bytes = storage_cfg.get("max_size_bytes")
    if isinstance(max_bytes, (int, float)) and max_bytes > 0:
        return int(max_bytes)
    max_gb = storage_cfg.get("max_size_gb", default_max_gb)
    if isinstance(max_gb, (int, float)) and max_gb > 0:
        return int(float(max_gb) * 1024 * 1024 * 1024)
    max_mb = storage_cfg.get("max_size_mb")
    if isinstance(max_mb, (int, float)) and max_mb > 0:
        return int(float(max_mb) * 1024 * 1024)
    return None


def is_within(child: str | Path, parent: str | Path) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _safe_exists(path: str | Path) -> bool:
    try:
        return Path(path).exists()
    except OSError:
        return False


def _safe_is_file(path: str | Path) -> bool:
    try:
        return Path(path).is_file()
    except OSError:
        return False


def _safe_resolve(path: str | Path) -> Path | None:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return None


def _safe_mtime(path: str | Path) -> float | None:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def _safe_glob(path: str | Path, pattern: str) -> tuple[Path, ...]:
    try:
        return tuple(Path(path).glob(pattern))
    except OSError:
        return ()


def get_dir_size(path: str | Path) -> int:
    total = 0
    try:
        for item in Path(path).rglob("*"):
            if _safe_is_file(item):
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def total_storage_size(config: StorageConfig) -> int:
    roots = [Path(config.paths.recordings_dir)]
    json_path = Path(config.paths.json_dir)
    if not is_within(json_path, roots[0]):
        roots.append(json_path)
    return sum(get_dir_size(root) for root in roots if _safe_exists(root))


def parse_saved_at(value: Any) -> float | None:
    if not value:
        return None
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError, OverflowError):
        return None


def load_json_metadata(path: str | Path, config: StorageConfig) -> tuple[float | None, Path | None]:
    source = Path(path)
    try:
        data = load_session_payload(source)
        saved_at = parse_saved_at(data.get("saved_at"))
        video_path = data.get("obs_record_path")
        if not video_path:
            return saved_at, None
        raw_path = Path(str(video_path))
        candidates = (
            [raw_path]
            if raw_path.is_absolute()
            else [source.parent / raw_path, Path(config.paths.recordings_dir) / raw_path.name]
        )
        for candidate in candidates:
            if _safe_exists(candidate):
                return saved_at, candidate
        return (saved_at, candidates[-1]) if candidates else (saved_at, raw_path)
    except (OSError, TypeError, ValueError):
        return None, None


def is_app_owned_video_path(path: str | Path | None, config: StorageConfig) -> bool:
    if not path:
        return False
    video_path = _safe_resolve(path)
    recordings_dir = _safe_resolve(config.paths.recordings_dir)
    if video_path is None or recordings_dir is None or not is_within(video_path, recordings_dir):
        return False
    return video_path.suffix.lower() in VIDEO_EXTENSIONS


def find_app_owned_clip_paths(video_path: str | Path | None, config: StorageConfig) -> tuple[Path, ...]:
    if not video_path:
        return ()
    source = _safe_resolve(video_path)
    recordings_dir = _safe_resolve(config.paths.recordings_dir)
    clips_dir = _safe_resolve(Path(config.paths.recordings_dir) / "clips")
    if source is None or recordings_dir is None or clips_dir is None:
        return ()
    if (
        not is_within(source, recordings_dir)
        or source.suffix.lower() not in VIDEO_EXTENSIONS
        or not _safe_exists(clips_dir)
    ):
        return ()
    matches = []
    for candidate in _safe_glob(clips_dir, f"{source.stem}_clip_*"):
        resolved = _safe_resolve(candidate)
        if (
            resolved
            and _safe_is_file(resolved)
            and is_within(resolved, clips_dir)
            and resolved.suffix.lower() in VIDEO_EXTENSIONS
        ):
            matches.append(resolved)
    return tuple(sorted(matches))


def enforce_storage_limit(
    config: StorageConfig,
    keep_paths: list[str | Path] | None = None,
    *,
    delete_file: Callable[[Path], None] | None = None,
) -> None:
    if not config.storage.max_size_bytes:
        return
    delete = delete_file or (lambda path: path.unlink(missing_ok=True))
    retained = {resolved for path in keep_paths or [] if path and (resolved := _safe_resolve(path))}
    total = total_storage_size(config)
    if total <= config.storage.max_size_bytes:
        return
    if _safe_exists(config.paths.json_dir):
        entries = []
        for json_path in _safe_glob(config.paths.json_dir, "*.json"):
            saved_at, video_path = load_json_metadata(json_path, config)
            entries.append((saved_at or _safe_mtime(json_path) or 0.0, json_path, video_path))
        for _, json_path, video_path in sorted(entries, key=lambda item: item[0]):
            if _safe_resolve(json_path) in retained:
                continue
            try:
                clips = find_app_owned_clip_paths(video_path, config)
                if (
                    video_path
                    and _safe_exists(video_path)
                    and _safe_resolve(video_path) not in retained
                    and is_app_owned_video_path(video_path, config)
                ):
                    delete(Path(video_path))
                for clip in clips:
                    if _safe_resolve(clip) not in retained:
                        delete(clip)
            except OSError:
                pass
            try:
                delete(json_path)
            except OSError:
                pass
            total = total_storage_size(config)
            if total <= config.storage.max_size_bytes:
                return
    if total > config.storage.max_size_bytes:
        LOGGER.warning(
            "Storage limit is still exceeded after deleting app-owned sessions. Untracked files under recordings_dir were left untouched: %s",
            config.paths.recordings_dir,
        )
