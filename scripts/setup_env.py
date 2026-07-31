"""Initial environment bootstrap for local binary dependencies.

Large binaries stay out of Git. This script downloads pinned Windows builds,
verifies SHA256 checksums, and places the runtime files where the app expects.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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
FFMPEG_LICENSE_DIR = DATA_DIR / "licenses" / f"FFmpeg-{FFMPEG_VERSION}"
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.1-essentials_build.zip"
FFMPEG_ZIP_MIRROR_URL = (
    "https://github.com/GyanD/codexffmpeg/releases/download/8.1.1/ffmpeg-8.1.1-essentials_build.zip"
)
FFMPEG_ZIP_SHA256 = "6f58ce889f59c311410f7d2b18895b33c03456463486f3b1ebc93d97a0f54541"
FFMPEG_INSTALL_MANIFEST = "_installed.json"
FFMPEG_TRANSACTION_SCHEMA_VERSION = 1

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


_WINDOWS_RESERVED_NAMES = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_WINDOWS_INVALID_PATH_CHARS = frozenset('<>:"|?*')
_WINDOWS_REPARSE_POINT_ATTRIBUTE = 0x400


def _is_path_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISLNK(metadata.st_mode)
        or (
            getattr(metadata, "st_file_attributes", 0)
            & _WINDOWS_REPARSE_POINT_ATTRIBUTE
        )
    )


def _validated_zip_members(archive: zipfile.ZipFile) -> dict[zipfile.ZipInfo, PurePosixPath]:
    """Return normalized members after applying Windows-safe extraction rules."""

    result: dict[zipfile.ZipInfo, PurePosixPath] = {}
    seen: dict[tuple[str, ...], zipfile.ZipInfo] = {}
    file_paths: set[tuple[str, ...]] = set()

    for info in archive.infolist():
        member_name = info.filename.replace("\\", "/")
        raw_parts = member_name.split("/")
        if not member_name or "\x00" in member_name:
            raise RuntimeError(f"Unsafe ZIP member path: {info.filename}")
        if any(not part for part in raw_parts[:-1]):
            raise RuntimeError(f"Unsafe ZIP member path: {info.filename}")

        member_path = PurePosixPath(member_name)
        parts = member_path.parts
        if (
            member_path.is_absolute()
            or not parts
            or any(part in {".", ".."} for part in parts)
            or any(part.endswith((" ", ".")) for part in parts)
            or any(any(char in _WINDOWS_INVALID_PATH_CHARS for char in part) for part in parts)
            or any(part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES for part in parts)
        ):
            raise RuntimeError(f"Unsafe ZIP member path: {info.filename}")

        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode) or (
            info.create_system == 0
            and info.external_attr & _WINDOWS_REPARSE_POINT_ATTRIBUTE
        ):
            raise RuntimeError(
                f"ZIP symbolic links or reparse points are not allowed: "
                f"{info.filename}"
            )

        canonical = tuple(part.casefold() for part in parts)
        previous = seen.get(canonical)
        if previous is not None:
            raise RuntimeError(
                "Case-insensitive ZIP member collision: "
                f"{previous.filename!r} and {info.filename!r}"
            )
        seen[canonical] = info
        if not info.is_dir():
            file_paths.add(canonical)
        result[info] = member_path

    for canonical, info in seen.items():
        for depth in range(1, len(canonical)):
            if canonical[:depth] in file_paths:
                raise RuntimeError(f"ZIP file/directory collision: {info.filename}")
    return result


def _is_license_material(path: PurePosixPath) -> bool:
    name = path.name.casefold()
    return any(
        name == prefix
        or name.startswith(f"{prefix}.")
        or name.startswith(f"{prefix}-")
        or name.startswith(f"{prefix}_")
        for prefix in ("license", "copying", "notice")
    )


def _is_build_information(path: PurePosixPath) -> bool:
    name = path.name.casefold()
    return any(
        name == prefix
        or name.startswith(f"{prefix}.")
        or name.startswith(f"{prefix}-")
        or name.startswith(f"{prefix}_")
        for prefix in ("readme", "build", "buildinfo")
    )


def _material_manifest_entries(directory: Path, paths: list[PurePosixPath]) -> list[dict[str, str]]:
    return [
        {
            "path": path.as_posix(),
            "sha256": _sha256(directory.joinpath(*path.parts)),
        }
        for path in sorted(paths, key=lambda item: item.as_posix().casefold())
    ]


def _remove_install_path(path: Path) -> None:
    if _is_path_link_or_reparse(path):
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _ffmpeg_transaction_journal_path(dest: Path, license_dir: Path) -> Path:
    return license_dir.parent / f".{license_dir.name}.{dest.name}.transaction.json"


def _write_json_atomically(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _recover_interrupted_ffmpeg_transaction(
    dest: Path,
    license_dir: Path,
    *,
    validate: Callable[[], None] | None = None,
) -> bool:
    """Finish or roll back an interrupted executable/materials replacement."""

    journal_path = _ffmpeg_transaction_journal_path(dest, license_dir)
    if not journal_path.exists() and not _is_path_link_or_reparse(journal_path):
        return False
    if _is_path_link_or_reparse(journal_path) or not journal_path.is_file():
        raise RuntimeError("FFmpeg transaction journal is not a regular file.")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("FFmpeg transaction journal is invalid.") from error
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != FFMPEG_TRANSACTION_SCHEMA_VERSION
        or journal.get("executable") != dest.name
        or journal.get("license_directory") != license_dir.name
        or not isinstance(journal.get("had_executable"), bool)
        or not isinstance(journal.get("had_licenses"), bool)
    ):
        raise RuntimeError("FFmpeg transaction journal does not match its destination.")

    token = journal.get("token")
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise RuntimeError("FFmpeg transaction journal token is invalid.")
    executable_backup = dest.with_name(f".{dest.name}.{token}.bak")
    license_backup = license_dir.with_name(f".{license_dir.name}.{token}.bak")
    if journal.get("executable_backup") != executable_backup.name or journal.get(
        "license_backup"
    ) != license_backup.name:
        raise RuntimeError("FFmpeg transaction journal backup paths are invalid.")
    if _is_path_link_or_reparse(executable_backup) or _is_path_link_or_reparse(
        license_backup
    ):
        raise RuntimeError("FFmpeg transaction backups must not be links or reparse points.")

    if validate is not None:
        try:
            validate()
        except Exception:
            pass
        else:
            _remove_install_path(executable_backup)
            _remove_install_path(license_backup)
            journal_path.unlink()
            return True

    rollback_errors = []
    try:
        if executable_backup.exists() or _is_path_link_or_reparse(executable_backup):
            _remove_install_path(dest)
            os.replace(executable_backup, dest)
        elif journal.get("had_executable") is False:
            _remove_install_path(dest)
    except Exception as error:
        rollback_errors.append(f"executable: {error}")
    try:
        if license_backup.exists() or _is_path_link_or_reparse(license_backup):
            _remove_install_path(license_dir)
            os.replace(license_backup, license_dir)
        elif journal.get("had_licenses") is False:
            _remove_install_path(license_dir)
    except Exception as error:
        rollback_errors.append(f"license materials: {error}")
    if rollback_errors:
        raise RuntimeError(
            "Interrupted FFmpeg transaction recovery failed: "
            + "; ".join(rollback_errors)
        )
    journal_path.unlink()
    return True


def _transactional_replace_ffmpeg(
    staged_executable: Path,
    staged_license_dir: Path,
    dest: Path,
    license_dir: Path,
    validate: Callable[[], None] | None = None,
) -> None:
    """Replace the executable and legal materials as one recoverable operation."""

    token = uuid.uuid4().hex
    executable_backup = dest.with_name(f".{dest.name}.{token}.bak")
    license_backup = license_dir.with_name(f".{license_dir.name}.{token}.bak")
    journal_path = _ffmpeg_transaction_journal_path(dest, license_dir)
    backed_up_executable = False
    backed_up_licenses = False
    installed_executable = False
    installed_licenses = False

    had_executable = dest.exists() or _is_path_link_or_reparse(dest)
    had_licenses = license_dir.exists() or _is_path_link_or_reparse(license_dir)
    if journal_path.exists() or _is_path_link_or_reparse(journal_path):
        raise RuntimeError(
            "An unrecovered FFmpeg installation transaction already exists."
        )
    _write_json_atomically(
        journal_path,
        {
            "schema_version": FFMPEG_TRANSACTION_SCHEMA_VERSION,
            "token": token,
            "executable": dest.name,
            "license_directory": license_dir.name,
            "executable_backup": executable_backup.name,
            "license_backup": license_backup.name,
            "had_executable": had_executable,
            "had_licenses": had_licenses,
        },
    )

    try:
        if had_executable:
            os.replace(dest, executable_backup)
            backed_up_executable = True
        if had_licenses:
            os.replace(license_dir, license_backup)
            backed_up_licenses = True

        os.replace(staged_executable, dest)
        installed_executable = True
        os.replace(staged_license_dir, license_dir)
        installed_licenses = True
        if validate is not None:
            validate()
    except Exception as install_error:
        rollback_errors = []
        try:
            if installed_licenses:
                _remove_install_path(license_dir)
            if backed_up_licenses:
                os.replace(license_backup, license_dir)
        except Exception as rollback_error:
            rollback_errors.append(f"license materials: {rollback_error}")
        try:
            if installed_executable:
                _remove_install_path(dest)
            if backed_up_executable:
                os.replace(executable_backup, dest)
        except Exception as rollback_error:
            rollback_errors.append(f"executable: {rollback_error}")

        details = ""
        if rollback_errors:
            details = "\nRollback also failed: " + "; ".join(rollback_errors)
        else:
            journal_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"FFmpeg installation failed; rollback was attempted.{details}"
        ) from install_error
    else:
        if backed_up_executable:
            _remove_install_path(executable_backup)
        if backed_up_licenses:
            _remove_install_path(license_backup)
        journal_path.unlink()


def _safe_manifest_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise RuntimeError("FFmpeg installation manifest contains an invalid material path.")
    path = PurePosixPath(value)
    if (
        "\\" in value
        or value != path.as_posix()
        or path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part.endswith((" ", ".")) for part in path.parts)
        or any(any(char in _WINDOWS_INVALID_PATH_CHARS for char in part) for part in path.parts)
        or any(part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_NAMES for part in path.parts)
    ):
        raise RuntimeError("FFmpeg installation manifest contains an unsafe material path.")
    return path


def _validate_manifest_materials(
    entries: object,
    license_dir: Path,
    predicate: Callable[[PurePosixPath], bool],
) -> None:
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("FFmpeg installation manifest is missing required materials.")
    seen = set()
    root = license_dir.resolve()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("FFmpeg installation manifest contains an invalid material entry.")
        path = _safe_manifest_path(entry.get("path"))
        canonical = tuple(part.casefold() for part in path.parts)
        if canonical in seen:
            raise RuntimeError("FFmpeg installation manifest contains duplicate material paths.")
        seen.add(canonical)
        if not predicate(path):
            raise RuntimeError(f"FFmpeg installation manifest misclassifies material: {path}")

        target = license_dir.joinpath(*path.parts)
        try:
            target.resolve().relative_to(root)
        except ValueError as error:
            raise RuntimeError(f"FFmpeg material is outside its installation directory: {path}") from error
        expected_sha256 = entry.get("sha256")
        if (
            not target.is_file()
            or _is_path_link_or_reparse(target)
            or not isinstance(expected_sha256, str)
            or _sha256(target).casefold() != expected_sha256.casefold()
        ):
            raise RuntimeError(f"FFmpeg material is missing or modified: {path}")


def _validate_ffmpeg_installation(
    dest: Path,
    license_dir: Path,
    *,
    version: str = FFMPEG_VERSION,
    archive_sha256: str = FFMPEG_ZIP_SHA256,
) -> None:
    if not dest.is_file() or _is_path_link_or_reparse(dest):
        raise RuntimeError("FFmpeg executable is missing.")
    manifest_path = license_dir / FFMPEG_INSTALL_MANIFEST
    if _is_path_link_or_reparse(license_dir) or _is_path_link_or_reparse(
        manifest_path
    ):
        raise RuntimeError(
            "FFmpeg installation materials must not be links or reparse points."
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as error:
        raise RuntimeError("FFmpeg installation manifest is missing or invalid.") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise RuntimeError("Unsupported FFmpeg installation manifest.")
    if manifest.get("version") != version:
        raise RuntimeError("FFmpeg installation version does not match the pinned version.")
    if str(manifest.get("archive_sha256", "")).casefold() != archive_sha256.casefold():
        raise RuntimeError("FFmpeg source archive hash does not match the pinned archive.")

    executable = manifest.get("executable")
    if not isinstance(executable, dict) or executable.get("path") != dest.name:
        raise RuntimeError("FFmpeg installation manifest contains an invalid executable path.")
    if str(executable.get("sha256", "")).casefold() != _sha256(dest).casefold():
        raise RuntimeError("FFmpeg executable hash does not match its installation manifest.")

    _validate_manifest_materials(manifest.get("license_files"), license_dir, _is_license_material)
    _validate_manifest_materials(manifest.get("documentation_files"), license_dir, _is_build_information)


def _is_ffmpeg_installation_ready(
    dest: Path = FFMPEG_EXE,
    license_dir: Path = FFMPEG_LICENSE_DIR,
    *,
    version: str = FFMPEG_VERSION,
    archive_sha256: str = FFMPEG_ZIP_SHA256,
) -> bool:
    try:
        journal_path = _ffmpeg_transaction_journal_path(dest, license_dir)
        if journal_path.exists() or _is_path_link_or_reparse(journal_path):
            return False
        _validate_ffmpeg_installation(
            dest,
            license_dir,
            version=version,
            archive_sha256=archive_sha256,
        )
    except Exception:
        return False
    return True


def _extract_ffmpeg(
    zip_path: Path,
    dest: Path,
    license_dir: Path | None = None,
    *,
    version: str = FFMPEG_VERSION,
    expected_archive_sha256: str | None = None,
) -> Path:
    resolved_license_dir = license_dir or dest.parent.parent / "licenses" / f"FFmpeg-{version}"
    archive_sha256 = _sha256(zip_path)
    if (
        expected_archive_sha256 is not None
        and archive_sha256.casefold() != expected_archive_sha256.casefold()
    ):
        raise RuntimeError("FFmpeg source archive hash changed before extraction.")
    _recover_interrupted_ffmpeg_transaction(
        dest,
        resolved_license_dir,
        validate=lambda: _validate_ffmpeg_installation(
            dest,
            resolved_license_dir,
            version=version,
            archive_sha256=archive_sha256,
        ),
    )

    with zipfile.ZipFile(zip_path) as archive:
        members = _validated_zip_members(archive)
        executable_members = [
            (info, path)
            for info, path in members.items()
            if not info.is_dir()
            and len(path.parts) >= 2
            and path.name.casefold() == "ffmpeg.exe"
            and path.parent.name.casefold() == "bin"
        ]
        if len(executable_members) != 1:
            raise RuntimeError("Exactly one bin/ffmpeg.exe must exist inside the downloaded ZIP.")

        executable_info, executable_path = executable_members[0]
        component_root = executable_path.parent.parent
        license_members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        documentation_members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        for info, path in members.items():
            if info.is_dir():
                continue
            try:
                relative_path = path.relative_to(component_root)
            except ValueError:
                continue
            if _is_license_material(relative_path):
                license_members.append((info, relative_path))
            elif _is_build_information(relative_path):
                documentation_members.append((info, relative_path))

        if not license_members:
            raise RuntimeError("A real FFmpeg license file (LICENSE/COPYING/NOTICE) was not found.")
        if not documentation_members:
            raise RuntimeError("FFmpeg README or build information was not found.")

        dest.parent.mkdir(parents=True, exist_ok=True)
        resolved_license_dir.parent.mkdir(parents=True, exist_ok=True)
        with (
            temporary_workspace("lol-replay-ffmpeg-exe-", parent=dest.parent) as executable_workspace,
            temporary_workspace(
                "lol-replay-ffmpeg-materials-",
                parent=resolved_license_dir.parent,
            ) as materials_workspace,
        ):
            staged_executable = executable_workspace / dest.name
            staged_license_dir = materials_workspace / "materials"
            staged_license_dir.mkdir()

            with archive.open(executable_info) as source, open(staged_executable, "wb") as target:
                shutil.copyfileobj(source, target)

            for info, relative_path in [*license_members, *documentation_members]:
                target = staged_license_dir.joinpath(*relative_path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(target, "wb") as output:
                    shutil.copyfileobj(source, output)

            license_paths = [path for _, path in license_members]
            documentation_paths = [path for _, path in documentation_members]
            manifest = {
                "schema_version": 1,
                "component": "FFmpeg",
                "version": version,
                "archive_sha256": archive_sha256,
                "executable": {
                    "path": dest.name,
                    "sha256": _sha256(staged_executable),
                },
                "license_files": _material_manifest_entries(staged_license_dir, license_paths),
                "documentation_files": _material_manifest_entries(
                    staged_license_dir,
                    documentation_paths,
                ),
            }
            (staged_license_dir / FFMPEG_INSTALL_MANIFEST).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            _validate_ffmpeg_installation(
                staged_executable,
                staged_license_dir,
                version=version,
                archive_sha256=archive_sha256,
            )
            _transactional_replace_ffmpeg(
                staged_executable,
                staged_license_dir,
                dest,
                resolved_license_dir,
                validate=lambda: _validate_ffmpeg_installation(
                    dest,
                    resolved_license_dir,
                    version=version,
                    archive_sha256=archive_sha256,
                ),
            )

    _validate_ffmpeg_installation(
        dest,
        resolved_license_dir,
        version=version,
        archive_sha256=archive_sha256,
    )
    return dest


def _safe_extractall(archive: zipfile.ZipFile, dest_dir: Path) -> None:
    _validated_zip_members(archive)
    archive.extractall(dest_dir.resolve())


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
            _safe_extractall(archive, extract_dir)
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
    if _is_ffmpeg_installation_ready(
        FFMPEG_EXE,
        FFMPEG_LICENSE_DIR,
        version=FFMPEG_PACKAGE.version,
        archive_sha256=FFMPEG_PACKAGE.sha256,
    ):
        report(progress_cb, FFMPEG_PACKAGE.progress_end, f"FFmpeg is ready: {FFMPEG_EXE}")
        return FFMPEG_EXE

    with setup_lock(cancel_cb=cancel_cb):
        _recover_interrupted_ffmpeg_transaction(
            FFMPEG_EXE,
            FFMPEG_LICENSE_DIR,
            validate=lambda: _validate_ffmpeg_installation(
                FFMPEG_EXE,
                FFMPEG_LICENSE_DIR,
                version=FFMPEG_PACKAGE.version,
                archive_sha256=FFMPEG_PACKAGE.sha256,
            ),
        )
        if _is_ffmpeg_installation_ready(
            FFMPEG_EXE,
            FFMPEG_LICENSE_DIR,
            version=FFMPEG_PACKAGE.version,
            archive_sha256=FFMPEG_PACKAGE.sha256,
        ):
            report(progress_cb, FFMPEG_PACKAGE.progress_end, f"FFmpeg is ready: {FFMPEG_EXE}")
            return FFMPEG_EXE
        cleanup_stale_temporary_workspaces(max_age_sec=0)
        with temporary_workspace("lol-replay-ffmpeg-") as tmp:
            zip_path = tmp / FFMPEG_PACKAGE.archive_name
            report(progress_cb, FFMPEG_PACKAGE.progress_start, "FFmpegを準備しています...")
            await download_file(FFMPEG_PACKAGE, zip_path, progress_cb, cancel_cb)
            report(progress_cb, FFMPEG_PACKAGE.progress_end, "FFmpegのSHA256を検証しています...")
            await verify_sha256(zip_path, FFMPEG_PACKAGE.sha256, FFMPEG_PACKAGE.name)
            await asyncio.to_thread(
                _extract_ffmpeg,
                zip_path,
                FFMPEG_EXE,
                FFMPEG_LICENSE_DIR,
                version=FFMPEG_PACKAGE.version,
                expected_archive_sha256=FFMPEG_PACKAGE.sha256,
            )

        _validate_ffmpeg_installation(
            FFMPEG_EXE,
            FFMPEG_LICENSE_DIR,
            version=FFMPEG_PACKAGE.version,
            archive_sha256=FFMPEG_PACKAGE.sha256,
        )

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
