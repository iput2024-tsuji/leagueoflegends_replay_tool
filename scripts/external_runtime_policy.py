from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

FORBIDDEN_RUNTIME_DIRECTORIES = frozenset({"obs-portable", "obs-studio"})
FORBIDDEN_RUNTIME_EXECUTABLES = frozenset(
    {"obs64.exe", "ffmpeg.exe", "ffprobe.exe", "ffplay.exe"}
)


class ExternalRuntimePolicyError(RuntimeError):
    """The tracked-source inventory could not be checked safely."""


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
        or name in FORBIDDEN_RUNTIME_DIRECTORIES
        or name in FORBIDDEN_RUNTIME_EXECUTABLES
        or is_obs_package
        or is_ffmpeg_archive
    )


def find_user_provided_runtime_paths(paths: Iterable[str]) -> list[str]:
    """Return tracked paths that violate the user-provided runtime boundary."""
    return sorted(
        {path for path in paths if is_user_provided_runtime_path(path)},
        key=lambda path: (path.casefold(), path),
    )


def _parse_git_tracked_paths(raw: bytes) -> list[str]:
    if not raw:
        return []
    if not raw.endswith(b"\0"):
        raise ExternalRuntimePolicyError(
            "Git tracked-file inventory is not NUL-terminated."
        )
    try:
        decoded = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExternalRuntimePolicyError(
            "Git tracked-file inventory is not valid UTF-8."
        ) from exc
    entries = decoded.split("\0")[:-1]
    if any(not entry for entry in entries):
        raise ExternalRuntimePolicyError(
            "Git tracked-file inventory contains an empty path."
        )
    return entries


def _run_git(repository_root: Path, *arguments: str) -> bytes:
    try:
        process = subprocess.run(
            ["git", "-C", str(repository_root), *arguments],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExternalRuntimePolicyError(
            f"Cannot run Git command ({' '.join(arguments)}): {exc}"
        ) from exc
    if process.returncode != 0:
        stderr = process.stderr.decode("utf-8", errors="replace").strip()
        detail = f": {stderr}" if stderr else ""
        raise ExternalRuntimePolicyError(
            f"Git command failed ({' '.join(arguments)}) with exit code "
            f"{process.returncode}{detail}"
        )
    return process.stdout


def _canonical_git_top_level(repository_root: Path) -> Path:
    raw = _run_git(repository_root, "rev-parse", "--show-toplevel")
    try:
        decoded = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ExternalRuntimePolicyError(
            "Git repository top-level path is not valid UTF-8."
        ) from exc
    lines = decoded.splitlines()
    if len(lines) != 1 or not lines[0]:
        raise ExternalRuntimePolicyError(
            "Git repository top-level output is missing or ambiguous."
        )
    try:
        return Path(lines[0]).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExternalRuntimePolicyError(
            f"Git repository top level cannot be resolved: {lines[0]}: {exc}"
        ) from exc


def git_tracked_paths(repository_root: Path) -> list[str]:
    """Read the repository index without inspecting untracked generated files."""
    try:
        root = repository_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExternalRuntimePolicyError(
            f"Repository root cannot be resolved: {repository_root}: {exc}"
        ) from exc
    if not root.is_dir():
        raise ExternalRuntimePolicyError(f"Repository root is not a directory: {root}")

    top_level = _canonical_git_top_level(root)
    if top_level != root:
        raise ExternalRuntimePolicyError(
            "Repository root must be the canonical Git top level: "
            f"requested {root}, detected {top_level}"
        )

    raw = _run_git(
        root,
        "ls-files",
        "--cached",
        "--full-name",
        "-z",
        "--",
    )
    return _parse_git_tracked_paths(raw)


def check_tracked_source(repository_root: Path) -> list[str]:
    """Return policy violations from the repository's tracked source tree."""
    return find_user_provided_runtime_paths(git_tracked_paths(repository_root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reject tracked OBS Studio and standalone FFmpeg files that users "
            "must obtain separately."
        )
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path.cwd(),
        help="Git repository to inspect (default: current directory)",
    )
    args = parser.parse_args(argv)
    try:
        forbidden = check_tracked_source(args.repository_root)
    except ExternalRuntimePolicyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if forbidden:
        print(
            "ERROR: User-provided OBS/standalone FFmpeg files must not be "
            "tracked in this repository:",
            file=sys.stderr,
        )
        for path in forbidden:
            print(f"  - {path!r}", file=sys.stderr)
        return 1
    print("Tracked source runtime policy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
