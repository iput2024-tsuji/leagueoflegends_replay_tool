from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

try:
    from .session_log import load_session_payload
except ImportError:
    from session_log import load_session_payload

VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".flv", ".mov", ".avi"})


class RecordingDeletionError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordingDeletionPlan:
    json_path: Path
    video_path: Path | None
    clip_paths: tuple[Path, ...] = ()
    metadata_error: str | None = None

    @property
    def paths(self) -> tuple[Path, ...]:
        paths = []
        if self.video_path is not None and self.video_path.exists():
            paths.append(self.video_path)
        paths.extend(path for path in self.clip_paths if path.exists())
        if self.json_path.exists():
            paths.append(self.json_path)
        return tuple(paths)


@dataclass(frozen=True)
class RecordingDeletionResult:
    deleted_paths: tuple[Path, ...]
    failed_path: Path | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.failed_path is None


def is_within(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except Exception:
        return False


class RecordingLibrary:
    def __init__(self, recordings_dir: str | Path, json_dir: str | Path) -> None:
        self.recordings_dir = Path(recordings_dir).resolve()
        self.json_dir = Path(json_dir).resolve()

    def plan_deletion(self, json_path: str | Path) -> RecordingDeletionPlan:
        source = Path(json_path).resolve()
        if not is_within(source, self.json_dir):
            raise RecordingDeletionError(
                f"設定された録画ログディレクトリ外のJSONは削除できません: {source}"
            )
        if source.suffix.lower() != ".json":
            raise RecordingDeletionError(f"録画ログではないファイルは削除できません: {source}")

        try:
            payload = load_session_payload(source)
        except Exception as e:
            return RecordingDeletionPlan(
                json_path=source,
                video_path=None,
                metadata_error=f"{type(e).__name__}: {e}",
            )

        video_path = self._resolve_owned_video(source, payload.get("obs_record_path"))
        clip_paths = self._find_owned_clips(video_path)
        return RecordingDeletionPlan(
            json_path=source,
            video_path=video_path,
            clip_paths=clip_paths,
        )

    def delete(
        self,
        plan: RecordingDeletionPlan,
        delete_file: Callable[[Path], bool],
    ) -> RecordingDeletionResult:
        deleted = []
        for path in plan.paths:
            try:
                if not delete_file(path):
                    raise OSError("削除処理が失敗しました")
                deleted.append(path)
            except Exception as e:
                return RecordingDeletionResult(
                    deleted_paths=tuple(deleted),
                    failed_path=path,
                    error=f"{type(e).__name__}: {e}",
                )
        return RecordingDeletionResult(deleted_paths=tuple(deleted))

    def _resolve_owned_video(self, json_path: Path, value: object) -> Path | None:
        if not value:
            return None

        raw = Path(str(value))
        candidates = []
        if raw.is_absolute():
            candidates.append(raw)
        else:
            candidates.extend(
                (
                    json_path.parent / raw,
                    self.recordings_dir / raw,
                    self.recordings_dir / raw.name,
                )
            )

        for candidate in candidates:
            resolved = candidate.resolve()
            if (
                resolved.exists()
                and resolved.is_file()
                and is_within(resolved, self.recordings_dir)
                and resolved.suffix.lower() in VIDEO_EXTENSIONS
            ):
                return resolved
        return None

    def _find_owned_clips(self, video_path: Path | None) -> tuple[Path, ...]:
        if video_path is None:
            return ()
        clips_dir = (self.recordings_dir / "clips").resolve()
        if not clips_dir.exists():
            return ()

        matches = []
        pattern = f"{video_path.stem}_clip_*"
        for candidate in clips_dir.glob(pattern):
            resolved = candidate.resolve()
            if (
                resolved.is_file()
                and is_within(resolved, clips_dir)
                and resolved.suffix.lower() in VIDEO_EXTENSIONS
            ):
                matches.append(resolved)
        return tuple(sorted(matches))
