from __future__ import annotations

from pathlib import PurePosixPath

FORBIDDEN_RUNTIME_DIRECTORIES = frozenset({"obs-portable", "obs-studio"})
FORBIDDEN_RUNTIME_EXECUTABLES = frozenset(
    {"obs64.exe", "ffmpeg.exe", "ffprobe.exe", "ffplay.exe"}
)


def is_user_provided_runtime_path(relative: str) -> bool:
    path = PurePosixPath(relative.replace("\\", "/"))
    directory_names = {part.casefold() for part in path.parts[:-1]}
    name = path.name.casefold()
    is_obs_package = name.startswith("obs-studio-") and name.endswith(
        (".exe", ".msi", ".zip", ".7z")
    )
    is_ffmpeg_archive = name.startswith("ffmpeg-") and name.endswith((".zip", ".7z"))
    return bool(
        directory_names & FORBIDDEN_RUNTIME_DIRECTORIES
        or name in FORBIDDEN_RUNTIME_EXECUTABLES
        or is_obs_package
        or is_ffmpeg_archive
    )
