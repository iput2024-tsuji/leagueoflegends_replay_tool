from __future__ import annotations

import configparser
import errno
import hashlib
import io
import json
import logging
import os
import stat
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

try:
    from .obs_process import OBSProcessManager
except ImportError:
    from obs_process import OBSProcessManager


LOGGER = logging.getLogger("lol_replay.obs_bootstrap")
PORTABLE_OBS_MARKER_NAME = "obs_portable_mode.txt"
LEGACY_PORTABLE_OBS_MARKER_NAME = "portable_mode.txt"
TRAY_SETTINGS = {
    "SysTrayEnabled": "false",
    "SysTrayWhenStarted": "false",
    "SysTrayMinimizeToTray": "false",
}
TRAY_SETTINGS_SECTION = "BasicWindow"
STARTUP_SETTINGS = {
    # OBS shows the Auto-Configuration Wizard when FirstRun is false and
    # LastVersion has not been written yet. Portable builds can hit that path
    # on a fresh bootstrap unless we explicitly mark the profile initialized.
    "FirstRun": "true",
}
STARTUP_SETTINGS_SECTION = "General"
OBS_COPY_SKIP_NAMES = frozenset(
    {
        ".lol_replay_obs_lease.json",
        ".lol_replay_obs_copy_in_progress",
        ".lol_replay_obs_copy_lock",
        "temp_appdata",
    }
)
OBS_COPY_IN_PROGRESS_MARKER_NAME = ".lol_replay_obs_copy_in_progress"
OBS_COPY_LOCK_NAME = ".lol_replay_obs_copy_lock"
OBS_COPY_JOURNAL_SCHEMA_VERSION = 3
OBS_MIGRATION_PHASE_COPYING = "copying"
OBS_MIGRATION_PHASE_FINALIZE_PENDING = "finalize_pending"
OBS_COPY_JOURNAL_MAX_BYTES = 64 * 1024
OBS_BOOTSTRAP_CONFIG_MAX_BYTES = 16 * 1024 * 1024
OBS_COPY_SKIP_NAME_KEYS = frozenset(name.casefold() for name in OBS_COPY_SKIP_NAMES)
WINDOWS_LOCK_CONTENTION_ERRORS = frozenset({32, 33})
_ACTIVE_OBS_MIGRATION_CAPABILITY: ContextVar[tuple[Path, str] | None] = ContextVar(
    "active_obs_migration_capability",
    default=None,
)
_ACTIVE_OBS_BOOTSTRAP_MUTATION: ContextVar[Any | None] = ContextVar(
    "active_obs_bootstrap_mutation",
    default=None,
)


class OBSMigrationError(RuntimeError):
    """Base error for the portable OBS migration transaction."""


class OBSMigrationInProgressError(OBSMigrationError):
    """Raised when another process owns the migration lock."""


class OBSMigrationRecoveryRequiredError(OBSMigrationError):
    """Raised when a stale journal cannot be resumed safely."""


@dataclass(frozen=True)
class OBSMigrationJournal:
    source_dir: Path
    source_fingerprint: str
    owner_token: str
    phase: str = OBS_MIGRATION_PHASE_COPYING
    owner_pid: int | None = None
    started_at: float | None = None
    schema_version: int | None = None


@dataclass(frozen=True)
class OBSMigrationInventoryEntry:
    relative_parts: tuple[str, ...]
    kind: str
    size: int | None = None
    sha256: str | None = None

    @property
    def relative_path(self) -> str:
        return "/".join(self.relative_parts)


class OBSPathSafetyError(RuntimeError):
    """Raised when an OBS path could escape the managed lexical boundary."""


class _UnsafeOBSMigrationPathError(OBSPathSafetyError):
    pass


class _OBSMigrationLockBusyError(RuntimeError):
    pass


def _is_windows_lock_contention_error(error: OSError) -> bool:
    return getattr(error, "winerror", None) in WINDOWS_LOCK_CONTENTION_ERRORS


def _validate_migration_owner_token(value: object) -> str:
    if not isinstance(value, str) or len(value) != 32 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("owner_token must be exactly 32 lowercase hexadecimal characters")
    return value


@dataclass(frozen=True)
class BootstrapReport:
    obs_dir: Path
    obs_exe: Path
    obs_exe_exists: bool
    portable_marker_exists: bool
    legacy_marker_exists: bool
    config_dir_exists: bool
    global_ini_exists: bool
    user_ini_exists: bool
    global_ini_parse_error: str | None = None
    user_ini_parse_error: str | None = None
    missing_tray_settings: tuple[str, ...] = field(default_factory=tuple)
    missing_startup_settings: tuple[str, ...] = field(default_factory=tuple)
    missing_user_tray_settings: tuple[str, ...] = field(default_factory=tuple)
    missing_user_startup_settings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return (
            self.obs_exe_exists
            and self.portable_marker_exists
            and self.config_dir_exists
            and self.global_ini_exists
            and self.user_ini_exists
            and self.global_ini_parse_error is None
            and self.user_ini_parse_error is None
            and not self.missing_tray_settings
            and not self.missing_startup_settings
            and not self.missing_user_tray_settings
            and not self.missing_user_startup_settings
        )

    @property
    def needs_repair(self) -> bool:
        return not self.ready


@dataclass(frozen=True)
class OBSConfigFileSnapshot:
    """Lexical OBS config target captured before a guarded update."""

    path: Path
    payload: bytes | None
    identity: tuple[int, int, int] | None
    label: str

    @property
    def exists(self) -> bool:
        return self.payload is not None


@dataclass(frozen=True)
class OBSConfigDirectoryEntry:
    """A non-reparse child found below a validated OBS config directory."""

    name: str
    kind: str


def get_obs_executable_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "bin" / "64bit" / "obs64.exe"


def get_obs_config_dir(base_dir: str | Path) -> Path:
    return Path(base_dir) / "config" / "obs-studio"


def get_obs_global_ini_path(base_dir: str | Path) -> Path:
    return get_obs_config_dir(base_dir) / "global.ini"


def get_obs_user_ini_path(base_dir: str | Path) -> Path:
    return get_obs_config_dir(base_dir) / "user.ini"


def get_obs_websocket_config_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "config" / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json"


def get_portable_marker_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / PORTABLE_OBS_MARKER_NAME


def get_legacy_marker_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / LEGACY_PORTABLE_OBS_MARKER_NAME


def new_obs_ini_parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    return parser


def read_obs_ini_parser(path: Path) -> tuple[configparser.ConfigParser, bool]:
    """BOMなしUTF-8として読み、混入BOMは除去対象として検出する。"""
    parser = new_obs_ini_parser()
    raw, _identity = _read_safe_file_bytes(
        path,
        max_bytes=OBS_BOOTSTRAP_CONFIG_MAX_BYTES,
        label="OBS設定file",
    )
    text = raw.decode("utf-8")
    had_bom = text.startswith("\ufeff")
    if had_bom:
        text = text.lstrip("\ufeff")
    parser.read_string(text)
    return parser, had_bom


def missing_ini_settings(
    parser: configparser.ConfigParser,
    section: str,
    settings: dict[str, str],
) -> list[str]:
    if not parser.has_section(section):
        return [f"{section}.{key}" for key in settings]

    missing = []
    for key, value in settings.items():
        current = parser.get(section, key, fallback=None)
        if current is None or str(current).strip().lower() != value:
            missing.append(f"{section}.{key}")
    return missing


def apply_ini_settings(
    parser: configparser.ConfigParser,
    section: str,
    settings: dict[str, str],
) -> bool:
    changed = False
    if not parser.has_section(section):
        parser.add_section(section)
        changed = True

    for key, value in settings.items():
        current = parser.get(section, key, fallback=None)
        if current is None or str(current).strip().lower() != value:
            parser.set(section, key, value)
            changed = True
    return changed


def get_obs_copy_in_progress_marker(base_dir: str | Path) -> Path:
    return Path(base_dir) / OBS_COPY_IN_PROGRESS_MARKER_NAME


def get_obs_copy_lock_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / OBS_COPY_LOCK_NAME


def _path_lexists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def lexical_absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _absolute_path(path: str | Path) -> Path:
    return lexical_absolute_path(path)


def _file_identity(file_stat: os.stat_result) -> tuple[int, int, int]:
    return (int(file_stat.st_dev), int(file_stat.st_ino), stat.S_IFMT(file_stat.st_mode))


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = int(getattr(file_stat, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(file_stat.st_mode) or bool(attributes & reparse_flag)


def _validate_existing_entry(
    path: Path,
    *,
    expected_kind: str,
    reject_hardlinks: bool = True,
) -> os.stat_result:
    try:
        file_stat = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise _UnsafeOBSMigrationPathError(f"pathを検査できません: {path} ({exc})") from exc
    if _is_reparse_point(file_stat):
        raise _UnsafeOBSMigrationPathError(f"reparse pointは利用できません: {path}")
    if expected_kind == "file" and not stat.S_ISREG(file_stat.st_mode):
        raise _UnsafeOBSMigrationPathError(f"通常ファイルではありません: {path}")
    if expected_kind == "directory" and not stat.S_ISDIR(file_stat.st_mode):
        raise _UnsafeOBSMigrationPathError(f"通常ディレクトリではありません: {path}")
    if reject_hardlinks and expected_kind == "file" and int(file_stat.st_nlink) != 1:
        raise _UnsafeOBSMigrationPathError(f"hardlinkされたファイルは利用できません: {path}")
    return file_stat


def _validate_open_identity(path: Path, descriptor: int, before: os.stat_result) -> os.stat_result:
    opened = os.fstat(descriptor)
    if _file_identity(opened) != _file_identity(before):
        raise _UnsafeOBSMigrationPathError(f"open中にpath identityが変化しました: {path}")
    after = _validate_existing_entry(
        path,
        expected_kind="file" if stat.S_ISREG(before.st_mode) else "directory",
    )
    if _file_identity(after) != _file_identity(opened):
        raise _UnsafeOBSMigrationPathError(f"open後にpath identityが変化しました: {path}")
    return opened


def _validate_existing_path_chain(path: str | Path, *, expected_kind: str) -> bool:
    absolute = _absolute_path(path)
    anchor = Path(absolute.anchor)
    current = anchor
    relative_parts = absolute.parts[1:]
    for index, part in enumerate(relative_parts):
        current /= part
        if not _path_lexists(current):
            return False
        is_leaf = index == len(relative_parts) - 1
        _validate_existing_entry(
            current,
            expected_kind=expected_kind if is_leaf else "directory",
            reject_hardlinks=is_leaf and expected_kind == "file",
        )
    return True


def validate_obs_installation_path(base_dir: str | Path) -> bool:
    base_path = _absolute_path(base_dir)
    if not _validate_existing_path_chain(base_path, expected_kind="directory"):
        return False
    return _validate_existing_path_chain(get_obs_executable_path(base_path), expected_kind="file")


def _ensure_safe_directory_chain(path: Path) -> None:
    absolute = _absolute_path(path)
    anchor = Path(absolute.anchor)
    current = anchor
    for part in absolute.parts[1:]:
        current /= part
        if _path_lexists(current):
            _validate_existing_entry(current, expected_kind="directory", reject_hardlinks=False)
            continue
        try:
            os.mkdir(current)
        except FileExistsError:
            pass
        except OSError as exc:
            raise _UnsafeOBSMigrationPathError(f"directoryを作成できません: {current} ({exc})") from exc
        _validate_existing_entry(current, expected_kind="directory", reject_hardlinks=False)


def _validate_safe_directory_chain(path: Path) -> None:
    absolute = _absolute_path(path)
    anchor = Path(absolute.anchor)
    current = anchor
    for part in absolute.parts[1:]:
        current /= part
        _validate_existing_entry(current, expected_kind="directory", reject_hardlinks=False)


def preflight_obs_config_directory(path: str | Path) -> bool:
    """Validate every existing lexical component without creating directories."""

    return _validate_existing_path_chain(_absolute_path(path), expected_kind="directory")


def ensure_safe_obs_config_directory(path: str | Path) -> Path:
    """Create a config directory while rejecting reparse components."""

    absolute = _absolute_path(path)
    _ensure_safe_directory_chain(absolute)
    return absolute


def list_safe_obs_config_directory(path: str | Path) -> tuple[OBSConfigDirectoryEntry, ...]:
    """List a validated directory without following reparse or special children."""

    absolute = _absolute_path(path)
    if not preflight_obs_config_directory(absolute):
        return ()

    before = _validate_existing_entry(
        absolute,
        expected_kind="directory",
        reject_hardlinks=False,
    )
    entries: list[OBSConfigDirectoryEntry] = []
    try:
        with os.scandir(absolute) as iterator:
            for entry in iterator:
                child = absolute / entry.name
                try:
                    child_stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise _UnsafeOBSMigrationPathError(
                        f"directory entryを検査できません: {child} ({exc})"
                    ) from exc
                if _is_reparse_point(child_stat):
                    raise _UnsafeOBSMigrationPathError(f"reparse pointは利用できません: {child}")
                if stat.S_ISDIR(child_stat.st_mode):
                    kind = "directory"
                    expected_kind = "directory"
                elif stat.S_ISREG(child_stat.st_mode):
                    kind = "file"
                    expected_kind = "file"
                else:
                    raise _UnsafeOBSMigrationPathError(f"特殊entryは利用できません: {child}")
                _validate_existing_entry(
                    child,
                    expected_kind=expected_kind,
                    reject_hardlinks=False,
                )
                entries.append(OBSConfigDirectoryEntry(name=entry.name, kind=kind))
    except _UnsafeOBSMigrationPathError:
        raise
    except OSError as exc:
        raise _UnsafeOBSMigrationPathError(
            f"directoryを安全に列挙できません: {absolute} ({exc})"
        ) from exc

    after = _validate_existing_entry(
        absolute,
        expected_kind="directory",
        reject_hardlinks=False,
    )
    if _file_identity(after) != _file_identity(before):
        raise _UnsafeOBSMigrationPathError(
            f"directory列挙中にidentityが変化しました: {absolute}"
        )
    return tuple(sorted(entries, key=lambda item: (item.name.casefold(), item.name)))


def _open_flags(*, write: bool = False, create_exclusive: bool = False) -> int:
    flags = os.O_WRONLY if write else os.O_RDONLY
    flags |= int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOINHERIT", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    if create_exclusive:
        flags |= os.O_CREAT | os.O_EXCL
    return flags


class _OBSInterProcessLock:
    """Small cross-platform advisory lock held for the whole migration."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: Any | None = None

    def acquire(self, *, create_parent: bool = True) -> bool:
        if create_parent:
            _ensure_safe_directory_chain(self.path.parent)
        elif not _path_lexists(self.path):
            return True

        descriptor: int | None = None
        lock_file: Any | None = None

        def open_existing_lock() -> tuple[int, os.stat_result]:
            existing = _validate_existing_entry(self.path, expected_kind="file")
            if int(existing.st_size) != 1:
                raise _UnsafeOBSMigrationPathError(f"lock fileのsizeが不正です: {self.path}")
            try:
                descriptor = os.open(self.path, os.O_RDWR | _open_flags())
            except OSError as exc:
                if _is_windows_lock_contention_error(exc):
                    raise _OBSMigrationLockBusyError from exc
                raise
            return descriptor, existing

        try:
            if _path_lexists(self.path):
                descriptor, before = open_existing_lock()
            else:
                try:
                    descriptor = os.open(self.path, os.O_RDWR | _open_flags(create_exclusive=True), 0o600)
                    if os.write(descriptor, b"\0") != 1:
                        raise _UnsafeOBSMigrationPathError(f"lock fileを初期化できません: {self.path}")
                    before = os.fstat(descriptor)
                except FileExistsError:
                    descriptor, before = open_existing_lock()

            opened = _validate_open_identity(self.path, descriptor, before)
            os.lseek(descriptor, 0, os.SEEK_SET)
            lock_file = os.fdopen(descriptor, "r+b", buffering=0)
            descriptor = None
        except _OBSMigrationLockBusyError:
            if descriptor is not None:
                os.close(descriptor)
            return False
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            elif lock_file is not None:
                lock_file.close()
            raise

        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_file.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        try:
            lock_file.seek(0)
            if lock_file.read(1) != b"\0" or int(opened.st_size) != 1:
                raise _UnsafeOBSMigrationPathError(f"lock fileの内容が不正です: {self.path}")
            _validate_open_identity(self.path, lock_file.fileno(), opened)
        except Exception:
            lock_file.close()
            raise

        self._file = lock_file
        return True

    def release(self) -> None:
        lock_file = self._file
        self._file = None
        if lock_file is None:
            return
        try:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()


def is_obs_copy_in_progress(base_dir: str | Path) -> bool:
    base_path = _absolute_path(base_dir)
    marker = get_obs_copy_in_progress_marker(base_path)
    if _path_lexists(marker):
        return True

    lock_path = get_obs_copy_lock_path(base_path)
    if not _path_lexists(lock_path):
        return False
    lock = _OBSInterProcessLock(lock_path)
    try:
        if not lock.acquire(create_parent=False):
            return True
    except (OSError, _UnsafeOBSMigrationPathError):
        return True
    lock.release()
    return False


def _normalized_obs_paths(paths: Iterable[str | Path], *, excluded: str | Path) -> tuple[Path, ...]:
    excluded_key = str(_absolute_path(excluded)).casefold()
    normalized: list[Path] = []
    seen: set[str] = set()
    for value in paths:
        path = _absolute_path(value)
        key = str(path).casefold()
        if key == excluded_key or key in seen:
            continue
        seen.add(key)
        normalized.append(path)
    return tuple(normalized)


def _is_valid_obs_migration_source(path: Path) -> bool:
    if not validate_obs_installation_path(path):
        return False
    source_marker = get_obs_copy_in_progress_marker(path)
    if _path_lexists(source_marker):
        _validate_existing_entry(source_marker, expected_kind="file")
        raise _UnsafeOBSMigrationPathError(f"移行元にコピー中markerがあります: {source_marker}")
    return True


def _migration_recovery_error(destination: Path, reason: str) -> OBSMigrationRecoveryRequiredError:
    return OBSMigrationRecoveryRequiredError(
        "OBSのコピー移行記録から安全に再開できません。"
        f"{reason}\n"
        "移行元を元の場所へ戻して再検査するか、"
        f"{destination} 全体を別の場所へ退避して空にしてから、"
        "公式ReleaseのWindows x64 ZIPを専用obs-portableへ再展開してください。"
    )


def _migration_finalize_error(destination: Path, error: Exception) -> OBSMigrationRecoveryRequiredError:
    return OBSMigrationRecoveryRequiredError(
        "OBS本体のコピーは完了しましたが、起動前設定の最終化に失敗しました。"
        "コピー中markerを維持し、次回実行時に最終化だけを再試行します。\n"
        f"対象: {destination}\n"
        f"原因: {type(error).__name__}: {error}"
    )


def _read_obs_migration_journal(marker: Path) -> OBSMigrationJournal:
    try:
        raw, _identity = _read_safe_regular_file(marker)
        text = raw.decode("utf-8").strip()
    except Exception as exc:
        raise _migration_recovery_error(marker.parent, f"コピー中markerを読めません: {exc}") from exc
    if not text:
        raise _migration_recovery_error(marker.parent, "コピー中markerが空です。")

    if not text.startswith("{"):
        raise _migration_recovery_error(
            marker.parent,
            "旧形式のコピー中markerには移行元fingerprintがないため自動再開できません。",
        )

    try:
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise TypeError("journal root must be an object")
        source_text = payload.get("source")
        if not isinstance(source_text, str) or not source_text.strip():
            raise ValueError("source is missing")
        source_fingerprint = payload.get("source_fingerprint")
        if (
            not isinstance(source_fingerprint, str)
            or len(source_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in source_fingerprint)
        ):
            raise ValueError("source_fingerprint is missing or invalid")
        schema_version = int(payload.get("schema_version"))
        if schema_version != OBS_COPY_JOURNAL_SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {schema_version}")
        phase = str(payload.get("phase", ""))
        if phase not in {OBS_MIGRATION_PHASE_COPYING, OBS_MIGRATION_PHASE_FINALIZE_PENDING}:
            raise ValueError(f"unsupported migration phase: {phase}")
        owner_pid = int(payload["owner_pid"]) if payload.get("owner_pid") is not None else None
        owner_token = _validate_migration_owner_token(payload.get("owner_token"))
        started_at = float(payload["started_at"]) if payload.get("started_at") is not None else None
    except Exception as exc:
        raise _migration_recovery_error(marker.parent, f"コピー中markerが壊れています: {exc}") from exc
    return OBSMigrationJournal(
        source_dir=Path(source_text),
        source_fingerprint=source_fingerprint,
        phase=phase,
        owner_pid=owner_pid,
        owner_token=owner_token,
        started_at=started_at,
        schema_version=schema_version,
    )


def _validated_journal_source(
    journal: OBSMigrationJournal,
    allowed_sources: tuple[Path, ...],
    destination: Path,
) -> Path:
    try:
        source = _absolute_path(journal.source_dir)
    except Exception as exc:
        raise _migration_recovery_error(destination, f"markerの移行元を解決できません: {exc}") from exc
    allowed_by_key = {str(path).casefold(): path for path in allowed_sources}
    source_key = str(source).casefold()
    if source_key not in allowed_by_key:
        raise _migration_recovery_error(destination, f"markerの移行元は許可済みlegacy候補ではありません: {source}")
    source = allowed_by_key[source_key]
    if not _is_valid_obs_migration_source(source):
        raise _migration_recovery_error(
            destination,
            f"markerの移行元に有効なポータブルOBSがありません: {get_obs_executable_path(source)}",
        )
    return source


def _read_safe_file_bytes(
    path: Path,
    *,
    max_bytes: int,
    label: str,
) -> tuple[bytes, tuple[int, int, int]]:
    before = _validate_existing_entry(path, expected_kind="file")
    if int(before.st_size) > max_bytes:
        raise _UnsafeOBSMigrationPathError(f"{label}が{max_bytes} bytesを超えています: {path}")
    descriptor = os.open(path, _open_flags())
    try:
        _validate_open_identity(path, descriptor, before)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _UnsafeOBSMigrationPathError(f"{label}が読み取り中に{max_bytes} bytesを超えました: {path}")
            chunks.append(chunk)
        after = _validate_open_identity(path, descriptor, before)
        if int(after.st_size) != int(before.st_size) or total != int(after.st_size):
            raise _UnsafeOBSMigrationPathError(f"{label}のsizeが読み取り中に変化しました: {path}")
        return b"".join(chunks), _file_identity(before)
    finally:
        os.close(descriptor)


def preflight_obs_config_file(
    path: str | Path,
    *,
    label: str,
    max_bytes: int = OBS_BOOTSTRAP_CONFIG_MAX_BYTES,
) -> OBSConfigFileSnapshot:
    """Capture a config file without following links or creating missing parents."""

    absolute = _absolute_path(path)
    preflight_obs_config_directory(absolute.parent)
    if not _path_lexists(absolute):
        return OBSConfigFileSnapshot(
            path=absolute,
            payload=None,
            identity=None,
            label=label,
        )
    payload, identity = _read_safe_file_bytes(
        absolute,
        max_bytes=max_bytes,
        label=label,
    )
    return OBSConfigFileSnapshot(
        path=absolute,
        payload=payload,
        identity=identity,
        label=label,
    )


def revalidate_obs_config_file(
    snapshot: OBSConfigFileSnapshot,
    *,
    max_bytes: int = OBS_BOOTSTRAP_CONFIG_MAX_BYTES,
) -> None:
    """Require a target to retain its preflight existence, identity, and bytes."""

    path = _absolute_path(snapshot.path)
    if path != snapshot.path:
        raise OBSPathSafetyError(f"config snapshot pathがlexical absoluteではありません: {snapshot.path}")
    preflight_obs_config_directory(path.parent)
    if snapshot.payload is None:
        if _path_lexists(path):
            _validate_existing_entry(path, expected_kind="file")
            raise OBSPathSafetyError(f"preflight後にconfig fileが作成されました: {path}")
        if snapshot.identity is not None:
            raise OBSPathSafetyError(f"missing config snapshotにidentityがあります: {path}")
        return

    if snapshot.identity is None:
        raise OBSPathSafetyError(f"existing config snapshotにidentityがありません: {path}")
    if not _path_lexists(path):
        raise OBSPathSafetyError(f"preflight後にconfig fileが消失しました: {path}")
    payload, identity = _read_safe_file_bytes(
        path,
        max_bytes=max_bytes,
        label=snapshot.label,
    )
    if identity != snapshot.identity:
        raise OBSPathSafetyError(f"preflight後にconfig file identityが変化しました: {path}")
    if payload != snapshot.payload:
        raise OBSPathSafetyError(f"preflight後にconfig file内容が変化しました: {path}")


def _read_safe_regular_file(path: Path) -> tuple[bytes, tuple[int, int, int]]:
    return _read_safe_file_bytes(
        path,
        max_bytes=OBS_COPY_JOURNAL_MAX_BYTES,
        label="marker",
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("file write made no progress")
        view = view[written:]


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _transaction_copy_temporary_path(
    destination: Path,
    owner_token: str,
) -> Path:
    owner_token = _validate_migration_owner_token(owner_token)
    return destination.with_name(f".{destination.name}.{owner_token}.copy.tmp")


def _transaction_write_temporary_path(
    path: Path,
    owner_token: str,
) -> Path:
    owner_token = _validate_migration_owner_token(owner_token)
    return path.with_name(f".{path.name}.{owner_token}.write.tmp")


def _safe_write_temporary_path(path: Path) -> Path:
    capability = _ACTIVE_OBS_MIGRATION_CAPABILITY.get()
    if capability is not None:
        destination, owner_token = capability
        scope = _ACTIVE_OBS_BOOTSTRAP_MUTATION.get()
        if (
            scope is None
            or scope.base_dir != destination
            or scope.migration_owner_token != owner_token
        ):
            raise OBSPathSafetyError("OBS移行の最終化scope外ではtransaction一時fileを作成できません。")
        absolute_path = _absolute_path(path)
        try:
            absolute_path.relative_to(destination)
        except ValueError as exc:
            raise OBSPathSafetyError(
                f"OBS移行destination外へtransaction一時fileを書き込めません: {absolute_path}"
            ) from exc
        return _transaction_write_temporary_path(absolute_path, owner_token)
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.write.tmp")


def _write_safe_file_bytes(
    path: Path,
    payload: bytes,
    *,
    expected_snapshot: OBSConfigFileSnapshot | None = None,
) -> None:
    path = _absolute_path(path)
    if expected_snapshot is not None:
        if expected_snapshot.path != path:
            raise OBSPathSafetyError(
                f"config snapshotと書き込み先が一致しません: {expected_snapshot.path} != {path}"
            )
        revalidate_obs_config_file(expected_snapshot)
    temporary = _safe_write_temporary_path(path)
    _ensure_safe_directory_chain(path.parent)
    destination_before = _validate_existing_entry(path, expected_kind="file") if _path_lexists(path) else None
    descriptor: int | None = None
    temporary_identity: tuple[int, int, int] | None = None
    try:
        descriptor = os.open(temporary, _open_flags(write=True, create_exclusive=True), 0o600)
        temporary_stat = os.fstat(descriptor)
        temporary_identity = _file_identity(temporary_stat)
        _validate_open_identity(temporary, descriptor, temporary_stat)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        written = _validate_open_identity(temporary, descriptor, temporary_stat)
        if int(written.st_size) != len(payload):
            raise _UnsafeOBSMigrationPathError(f"一時fileのsizeが一致しません: {temporary}")
        os.close(descriptor)
        descriptor = None

        if expected_snapshot is not None:
            revalidate_obs_config_file(expected_snapshot)
        if destination_before is None:
            if _path_lexists(path):
                _validate_existing_entry(path, expected_kind="file")
                raise _UnsafeOBSMigrationPathError(f"file作成時に競合しました: {path}")
        else:
            destination_current = _validate_existing_entry(path, expected_kind="file")
            if _file_identity(destination_current) != _file_identity(destination_before):
                raise _UnsafeOBSMigrationPathError(f"書き込み前にfile identityが変化しました: {path}")

        temporary_current = _validate_existing_entry(temporary, expected_kind="file")
        if _file_identity(temporary_current) != temporary_identity:
            raise _UnsafeOBSMigrationPathError(f"一時fileのidentityが変化しました: {temporary}")
        os.replace(temporary, path)
        final_stat = _validate_existing_entry(path, expected_kind="file")
        if _file_identity(final_stat) != temporary_identity:
            raise _UnsafeOBSMigrationPathError(f"file確定時にidentityが変化しました: {path}")
        _fsync_parent_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_identity is not None and _path_lexists(temporary):
            try:
                temporary_current = _validate_existing_entry(temporary, expected_kind="file")
                if _file_identity(temporary_current) == temporary_identity:
                    temporary.unlink()
            except _UnsafeOBSMigrationPathError:
                pass


def write_preflighted_obs_config_file(
    snapshot: OBSConfigFileSnapshot,
    payload: bytes,
) -> Path:
    """Atomically replace a config file only if its preflight snapshot still matches."""

    _write_safe_file_bytes(
        snapshot.path,
        payload,
        expected_snapshot=snapshot,
    )
    return snapshot.path


def _write_obs_migration_journal(
    marker: Path,
    source: Path,
    source_fingerprint: str,
    owner_token: str,
    *,
    phase: str = OBS_MIGRATION_PHASE_COPYING,
) -> None:
    owner_token = _validate_migration_owner_token(owner_token)
    if phase not in {OBS_MIGRATION_PHASE_COPYING, OBS_MIGRATION_PHASE_FINALIZE_PENDING}:
        raise ValueError(f"unsupported migration phase: {phase}")
    payload = {
        "schema_version": OBS_COPY_JOURNAL_SCHEMA_VERSION,
        "source": str(source),
        "source_fingerprint": source_fingerprint,
        "phase": phase,
        "owner_pid": os.getpid(),
        "owner_token": owner_token,
        "started_at": time.time(),
    }
    encoded = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    if len(encoded) > OBS_COPY_JOURNAL_MAX_BYTES:
        raise _UnsafeOBSMigrationPathError(
            f"生成するmarkerが{OBS_COPY_JOURNAL_MAX_BYTES} bytesを超えています: {marker}"
        )
    temporary = marker.with_name(f"{marker.name}.{owner_token}.tmp")
    descriptor: int | None = None
    temporary_identity: tuple[int, int, int] | None = None
    try:
        descriptor = os.open(temporary, _open_flags(write=True, create_exclusive=True), 0o600)
        temporary_stat = os.fstat(descriptor)
        temporary_identity = _file_identity(temporary_stat)
        _validate_open_identity(temporary, descriptor, temporary_stat)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
        _validate_open_identity(temporary, descriptor, temporary_stat)
        os.close(descriptor)
        descriptor = None
        if _path_lexists(marker):
            _validate_existing_entry(marker, expected_kind="file")
        os.replace(temporary, marker)
        marker_stat = _validate_existing_entry(marker, expected_kind="file")
        if _file_identity(marker_stat) != temporary_identity:
            raise _UnsafeOBSMigrationPathError(f"marker確定時にidentityが変化しました: {marker}")
        _fsync_parent_directory(marker.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_identity is not None and _path_lexists(temporary):
            try:
                current = _validate_existing_entry(temporary, expected_kind="file")
                if _file_identity(current) == temporary_identity:
                    temporary.unlink()
            except _UnsafeOBSMigrationPathError:
                pass


def _remove_obs_migration_journal_if_owned(marker: Path, owner_token: str) -> None:
    raw, identity = _read_safe_regular_file(marker)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise _migration_recovery_error(marker.parent, f"OBSのコピー中markerを再検証できません: {exc}") from exc
    journal = _read_obs_migration_journal(marker)
    if journal.owner_token != owner_token:
        raise _migration_recovery_error(marker.parent, "markerの所有者が変化したため解除しません。")
    if payload.get("owner_token") != owner_token:
        raise _migration_recovery_error(marker.parent, "markerの所有者が変化したため解除しません。")
    current = _validate_existing_entry(marker, expected_kind="file")
    if _file_identity(current) != identity:
        raise _migration_recovery_error(marker.parent, "markerのidentityが変化したため解除しません。")
    marker.unlink()


@dataclass(frozen=True)
class _OBSBootstrapMutationScope:
    base_dir: Path
    lock: _OBSInterProcessLock | None
    migration_owner_token: str | None = None


def _validate_migration_finalize_capability(base_dir: Path, owner_token: str) -> None:
    owner_token = _validate_migration_owner_token(owner_token)
    marker = get_obs_copy_in_progress_marker(base_dir)
    if not _path_lexists(marker):
        raise _migration_recovery_error(base_dir, "最終化権限に対応するコピー中markerがありません。")
    journal = _read_obs_migration_journal(marker)
    if journal.phase != OBS_MIGRATION_PHASE_FINALIZE_PENDING:
        raise _migration_recovery_error(base_dir, "コピー中markerが最終化待ちphaseではありません。")
    if journal.owner_token != owner_token:
        raise _migration_recovery_error(base_dir, "コピー中markerのowner tokenが最終化権限と一致しません。")


@contextmanager
def _obs_bootstrap_mutation_guard(base_dir: Path):
    base_path = _absolute_path(base_dir)
    active_scope = _ACTIVE_OBS_BOOTSTRAP_MUTATION.get()
    if active_scope is not None:
        if active_scope.base_dir != base_path:
            raise OBSPathSafetyError(
                "OBS設定更新中に別の管理destinationへnested writeしようとしました: "
                f"{active_scope.base_dir} -> {base_path}"
            )
        yield
        return

    migration_capability = _ACTIVE_OBS_MIGRATION_CAPABILITY.get()
    if migration_capability is not None:
        destination, owner_token = migration_capability
        if destination != base_path:
            raise OBSPathSafetyError(
                "OBS移行の最終化権限を別の管理destinationへ利用できません: "
                f"{destination} -> {base_path}"
            )
        _validate_migration_finalize_capability(base_path, owner_token)
        scope = _OBSBootstrapMutationScope(base_path, None, owner_token)
        scope_token = _ACTIVE_OBS_BOOTSTRAP_MUTATION.set(scope)
        try:
            yield
        finally:
            _ACTIVE_OBS_BOOTSTRAP_MUTATION.reset(scope_token)
        return

    lock = _OBSInterProcessLock(get_obs_copy_lock_path(base_path))
    try:
        acquired = lock.acquire()
    except _UnsafeOBSMigrationPathError:
        raise
    except OSError as exc:
        raise _migration_recovery_error(base_path, f"OBS設定更新lockを安全に確保できません: {exc}") from exc
    if not acquired:
        raise OBSMigrationInProgressError(
            "別のプロセスがOBSのコピー移行または起動前設定更新を実行中です。完了後に再検査してください。"
        )

    scope_token = None
    try:
        marker = get_obs_copy_in_progress_marker(base_path)
        if _path_lexists(marker):
            journal = _read_obs_migration_journal(marker)
            raise _migration_recovery_error(
                base_path,
                "コピー中markerが残っているため、通常の起動前設定更新は行いません。"
                f" phase={journal.phase}",
            )
        scope = _OBSBootstrapMutationScope(base_path, lock)
        scope_token = _ACTIVE_OBS_BOOTSTRAP_MUTATION.set(scope)
        yield
    finally:
        if scope_token is not None:
            _ACTIVE_OBS_BOOTSTRAP_MUTATION.reset(scope_token)
        lock.release()


def _guard_obs_bootstrap_mutation(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def guarded(bootstrapper, *args, **kwargs):
        with _obs_bootstrap_mutation_guard(bootstrapper.base_dir):
            return method(bootstrapper, *args, **kwargs)

    return guarded


@contextmanager
def obs_config_mutation_guard(base_dir: str | Path):
    """Share the migration/bootstrap lock with additional OBS config writers."""

    with _obs_bootstrap_mutation_guard(_absolute_path(base_dir)):
        yield


def _inventory_fingerprint(entries: tuple[OBSMigrationInventoryEntry, ...]) -> str:
    payload = [
        {
            "path": entry.relative_path,
            "kind": entry.kind,
            "size": entry.size,
            "sha256": entry.sha256,
        }
        for entry in entries
    ]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hash_safe_file(path: Path) -> tuple[int, str]:
    before = _validate_existing_entry(path, expected_kind="file")
    descriptor = os.open(path, _open_flags())
    digest = hashlib.sha256()
    size = 0
    try:
        _validate_open_identity(path, descriptor, before)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        _validate_open_identity(path, descriptor, before)
    finally:
        os.close(descriptor)
    if size != int(before.st_size):
        raise _UnsafeOBSMigrationPathError(f"読み取り中にsizeが変化しました: {path}")
    return size, digest.hexdigest()


def _validate_inventory_parts(parts: tuple[str, ...], path: Path) -> None:
    if not parts:
        raise _UnsafeOBSMigrationPathError(f"空のrelative pathは利用できません: {path}")
    for part in parts:
        if not part or part in {".", ".."} or "/" in part or "\\" in part:
            raise _UnsafeOBSMigrationPathError(f"安全でないpath componentがあります: {path}")


def _inventory_path(root: Path, entry: OBSMigrationInventoryEntry) -> Path:
    _validate_inventory_parts(entry.relative_parts, root / entry.relative_path)
    return root.joinpath(*entry.relative_parts)


def _is_internal_inventory_path(relative_parts: tuple[str, ...]) -> bool:
    return relative_parts[0].casefold() in OBS_COPY_SKIP_NAME_KEYS


def _is_orphaned_journal_temporary(relative_parts: tuple[str, ...]) -> bool:
    if len(relative_parts) != 1:
        return False
    name = relative_parts[0].casefold()
    prefix = f"{OBS_COPY_IN_PROGRESS_MARKER_NAME.casefold()}."
    return name.startswith(prefix) and name.endswith(".tmp")


def _transaction_data_temporary_details(name: str) -> tuple[str, str, str] | None:
    for kind, suffix in (("copy", ".copy.tmp"), ("write", ".write.tmp")):
        if not name.startswith(".") or not name.endswith(suffix):
            continue
        target_and_token = name[1 : -len(suffix)]
        target_name, separator, owner_token = target_and_token.rpartition(".")
        if not separator or not target_name:
            raise _UnsafeOBSMigrationPathError(f"不正なtransaction一時file名です: {name}")
        try:
            owner_token = _validate_migration_owner_token(owner_token)
        except ValueError as exc:
            raise _UnsafeOBSMigrationPathError(f"不正なtransaction owner tokenがあります: {name}") from exc
        return kind, target_name, owner_token
    return None


def _validate_owned_transaction_temporary(path: Path) -> tuple[int, int, int]:
    _validate_safe_directory_chain(path.parent)
    before = _validate_existing_entry(path, expected_kind="file")
    descriptor = os.open(path, _open_flags())
    try:
        opened = _validate_open_identity(path, descriptor, before)
    finally:
        os.close(descriptor)
    return _file_identity(opened)


def _remove_owned_transaction_temporaries(paths: Iterable[Path]) -> None:
    validated: list[tuple[Path, tuple[int, int, int]]] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path).casefold()
        if key in seen or not _path_lexists(path):
            continue
        seen.add(key)
        validated.append((path, _validate_owned_transaction_temporary(path)))

    for path, identity in validated:
        _validate_safe_directory_chain(path.parent)
        current = _validate_existing_entry(path, expected_kind="file")
        if _file_identity(current) != identity:
            raise _UnsafeOBSMigrationPathError(f"transaction一時fileのidentityが変化しました: {path}")
        path.unlink()
        _fsync_parent_directory(path.parent)


def _recover_owned_transaction_temporaries(
    destination: Path,
    source_entries: tuple[OBSMigrationInventoryEntry, ...],
    owner_token: str,
) -> None:
    owner_token = _validate_migration_owner_token(owner_token)
    marker = get_obs_copy_in_progress_marker(destination)
    candidates = [marker.with_name(f"{marker.name}.{owner_token}.tmp")]
    candidates.extend(
        _transaction_copy_temporary_path(_inventory_path(destination, entry), owner_token)
        for entry in source_entries
        if entry.kind == "file"
    )
    candidates.extend(
        _transaction_write_temporary_path(path, owner_token)
        for path in (
            get_portable_marker_path(destination),
            get_legacy_marker_path(destination),
            get_obs_global_ini_path(destination),
            get_obs_user_ini_path(destination),
            get_obs_websocket_config_path(destination),
        )
    )
    _remove_owned_transaction_temporaries(candidates)


def _build_obs_tree_inventory(root: Path) -> tuple[OBSMigrationInventoryEntry, ...]:
    _validate_safe_directory_chain(root)
    inventory: list[OBSMigrationInventoryEntry] = []
    seen: set[tuple[str, ...]] = set()

    def visit(directory: Path, relative_parent: tuple[str, ...] = (), *, include: bool = True) -> None:
        before = _validate_existing_entry(directory, expected_kind="directory", reject_hardlinks=False)
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise _UnsafeOBSMigrationPathError(f"directoryを走査できません: {directory} ({exc})") from exc
        for child in children:
            child_path = directory / child.name
            relative_parts = (*relative_parent, child.name)
            _validate_inventory_parts(relative_parts, child_path)
            if _is_orphaned_journal_temporary(relative_parts):
                raise _UnsafeOBSMigrationPathError(f"孤立したjournal一時fileがあります: {child_path}")
            if _transaction_data_temporary_details(child.name) is not None:
                raise _UnsafeOBSMigrationPathError(f"孤立したtransaction一時fileがあります: {child_path}")
            relative_path = "/".join(relative_parts)
            normalized_key = tuple(part.casefold() for part in relative_parts)
            if normalized_key in seen:
                raise _UnsafeOBSMigrationPathError(f"大文字小文字が衝突するentryがあります: {relative_path}")
            seen.add(normalized_key)
            child_stat = _validate_existing_entry(
                child_path,
                expected_kind="directory" if child.is_dir(follow_symlinks=False) else "file",
                reject_hardlinks=True,
            )
            child_include = include and not _is_internal_inventory_path(relative_parts)
            if stat.S_ISDIR(child_stat.st_mode):
                if child_include:
                    inventory.append(OBSMigrationInventoryEntry(relative_parts, "directory"))
                visit(child_path, relative_parts, include=child_include)
            elif stat.S_ISREG(child_stat.st_mode):
                if child_include:
                    size, sha256 = _hash_safe_file(child_path)
                    inventory.append(OBSMigrationInventoryEntry(relative_parts, "file", size, sha256))
            else:
                raise _UnsafeOBSMigrationPathError(f"特殊entryは利用できません: {child_path}")
        after = _validate_existing_entry(directory, expected_kind="directory", reject_hardlinks=False)
        if _file_identity(after) != _file_identity(before):
            raise _UnsafeOBSMigrationPathError(f"走査中にdirectory identityが変化しました: {directory}")

    visit(root)
    return tuple(sorted(inventory, key=lambda entry: entry.relative_path.casefold()))


def _validate_destination_subset(
    destination_entries: tuple[OBSMigrationInventoryEntry, ...],
    source_entries: tuple[OBSMigrationInventoryEntry, ...],
    destination: Path,
) -> None:
    source_by_path = {entry.relative_path.casefold(): entry for entry in source_entries}
    for entry in destination_entries:
        expected = source_by_path.get(entry.relative_path.casefold())
        if expected is None:
            raise _migration_recovery_error(
                destination,
                f"コピー先に移行元へ存在しないentryがあります: {entry.relative_path}",
            )
        if expected.kind != entry.kind:
            raise _migration_recovery_error(
                destination,
                f"コピー先entryの種類が移行元と一致しません: {entry.relative_path}",
            )


def _is_finalize_managed_entry(entry: OBSMigrationInventoryEntry) -> bool:
    parts = tuple(part.casefold() for part in entry.relative_parts)
    if parts in {
        (PORTABLE_OBS_MARKER_NAME.casefold(),),
        (LEGACY_PORTABLE_OBS_MARKER_NAME.casefold(),),
    }:
        return True
    return parts == ("config",) or parts[:2] == ("config", "obs-studio")


def _validate_finalize_pending_destination(
    destination_entries: tuple[OBSMigrationInventoryEntry, ...],
    source_entries: tuple[OBSMigrationInventoryEntry, ...],
    destination: Path,
) -> None:
    source_unmanaged = {
        tuple(part.casefold() for part in entry.relative_parts): entry
        for entry in source_entries
        if not _is_finalize_managed_entry(entry)
    }
    destination_unmanaged = {
        tuple(part.casefold() for part in entry.relative_parts): entry
        for entry in destination_entries
        if not _is_finalize_managed_entry(entry)
    }
    if destination_unmanaged != source_unmanaged:
        raise _migration_recovery_error(
            destination,
            "最終化待ちのbin/plugin等が移行元inventoryと一致しません。",
        )


def _ensure_safe_destination_directory(path: Path) -> None:
    if _path_lexists(path):
        _validate_existing_entry(path, expected_kind="directory", reject_hardlinks=False)
        return
    parent = path.parent
    _validate_existing_entry(parent, expected_kind="directory", reject_hardlinks=False)
    try:
        os.mkdir(path)
    except FileExistsError as exc:
        raise _UnsafeOBSMigrationPathError(f"directory作成時に競合しました: {path}") from exc
    except OSError as exc:
        raise _UnsafeOBSMigrationPathError(f"directoryを作成できません: {path} ({exc})") from exc
    _validate_existing_entry(path, expected_kind="directory", reject_hardlinks=False)


def _copy_inventory_file(
    source: Path,
    destination: Path,
    expected: OBSMigrationInventoryEntry,
    owner_token: str,
) -> None:
    temporary = _transaction_copy_temporary_path(destination, owner_token)
    source_before = _validate_existing_entry(source, expected_kind="file")
    source_descriptor = os.open(source, _open_flags())
    temporary_descriptor: int | None = None
    temporary_identity: tuple[int, int, int] | None = None
    try:
        _validate_open_identity(source, source_descriptor, source_before)
        destination_before = (
            _validate_existing_entry(destination, expected_kind="file") if _path_lexists(destination) else None
        )
        temporary_descriptor = os.open(
            temporary,
            _open_flags(write=True, create_exclusive=True),
            0o600,
        )
        temporary_stat = os.fstat(temporary_descriptor)
        temporary_identity = _file_identity(temporary_stat)
        _validate_open_identity(temporary, temporary_descriptor, temporary_stat)

        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            _write_all(temporary_descriptor, chunk)
        os.fsync(temporary_descriptor)
        _validate_open_identity(source, source_descriptor, source_before)
        if size != expected.size or digest.hexdigest() != expected.sha256:
            raise _UnsafeOBSMigrationPathError(f"コピー中に移行元fileが変化しました: {source}")
        written = _validate_open_identity(temporary, temporary_descriptor, temporary_stat)
        if int(written.st_size) != size:
            raise _UnsafeOBSMigrationPathError(f"一時コピーのsizeが一致しません: {temporary}")
        os.close(temporary_descriptor)
        temporary_descriptor = None

        if destination_before is None:
            if _path_lexists(destination):
                _validate_existing_entry(destination, expected_kind="file")
                raise _UnsafeOBSMigrationPathError(f"コピー先fileの作成時に競合しました: {destination}")
        else:
            destination_current = _validate_existing_entry(destination, expected_kind="file")
            if _file_identity(destination_current) != _file_identity(destination_before):
                raise _UnsafeOBSMigrationPathError(f"コピー中にコピー先fileが変化しました: {destination}")

        temporary_current = _validate_existing_entry(temporary, expected_kind="file")
        if _file_identity(temporary_current) != temporary_identity:
            raise _UnsafeOBSMigrationPathError(f"一時コピーのidentityが変化しました: {temporary}")
        os.replace(temporary, destination)
        destination_after = _validate_existing_entry(destination, expected_kind="file")
        if _file_identity(destination_after) != temporary_identity:
            raise _UnsafeOBSMigrationPathError(f"コピー先確定時にidentityが変化しました: {destination}")
    finally:
        os.close(source_descriptor)
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        if temporary_identity is not None and _path_lexists(temporary):
            try:
                temporary_current = _validate_existing_entry(temporary, expected_kind="file")
                if _file_identity(temporary_current) == temporary_identity:
                    temporary.unlink()
            except _UnsafeOBSMigrationPathError:
                pass


def _copy_obs_inventory(
    source: Path,
    destination: Path,
    source_entries: tuple[OBSMigrationInventoryEntry, ...],
    owner_token: str,
) -> None:
    for entry in source_entries:
        if entry.kind == "directory":
            _ensure_safe_destination_directory(_inventory_path(destination, entry))
    for entry in source_entries:
        if entry.kind != "file":
            continue
        target = _inventory_path(destination, entry)
        _validate_existing_entry(target.parent, expected_kind="directory", reject_hardlinks=False)
        _copy_inventory_file(_inventory_path(source, entry), target, entry, owner_token)


def migrate_legacy_obs_installation(
    destination_dir: str | Path,
    legacy_candidates: Iterable[str | Path],
    *,
    prepare_source: Callable[[Path], None] | None = None,
    finalize_destination: Callable[[Path], None] | None = None,
) -> Path | None:
    """Copy one allow-listed legacy OBS tree under an inter-process lease.

    The source is never deleted. A journal remains after interruption and is
    only removed by the process whose token still owns it. The OS lock, rather
    than a PID from the journal, is authoritative for live/stale detection so
    PID reuse cannot keep a stale migration locked forever.
    """

    destination = _absolute_path(destination_dir)
    allowed_sources = _normalized_obs_paths(legacy_candidates, excluded=destination)
    marker = get_obs_copy_in_progress_marker(destination)

    try:
        marker_exists = _path_lexists(marker)
        if not marker_exists and _is_valid_obs_migration_source(destination):
            if not is_obs_copy_in_progress(destination):
                return None

        first_source = next((path for path in allowed_sources if _is_valid_obs_migration_source(path)), None)
        if not marker_exists and first_source is None:
            return None

        lock = _OBSInterProcessLock(get_obs_copy_lock_path(destination))
        if not lock.acquire():
            raise OBSMigrationInProgressError(
                "別のプロセスがOBSのコピー移行を実行中です。完了後に再検査してください。"
            )
    except OBSMigrationError:
        raise
    except (OSError, _UnsafeOBSMigrationPathError) as exc:
        raise _migration_recovery_error(destination, f"migration lockを安全に確保できません: {exc}") from exc

    try:
        try:
            stale_journal: OBSMigrationJournal | None = None
            if _path_lexists(marker):
                stale_journal = _read_obs_migration_journal(marker)
                source = _validated_journal_source(stale_journal, allowed_sources, destination)
            elif _is_valid_obs_migration_source(destination):
                return None
            else:
                source = next((path for path in allowed_sources if _is_valid_obs_migration_source(path)), None)
                if source is None:
                    return None

            owner_token = stale_journal.owner_token if stale_journal is not None else uuid.uuid4().hex
            phase = stale_journal.phase if stale_journal is not None else OBS_MIGRATION_PHASE_COPYING
            if phase == OBS_MIGRATION_PHASE_COPYING and prepare_source is not None:
                prepare_source(source)
            source_entries = _build_obs_tree_inventory(source)
            source_fingerprint = _inventory_fingerprint(source_entries)
            if stale_journal is not None and source_fingerprint != stale_journal.source_fingerprint:
                raise _migration_recovery_error(
                    destination,
                    "marker作成後に移行元の内容が変化したため、部分配置へoverlayできません。",
                )
            if stale_journal is not None:
                _recover_owned_transaction_temporaries(destination, source_entries, owner_token)

            _write_obs_migration_journal(
                marker,
                source,
                source_fingerprint,
                owner_token,
                phase=phase,
            )
            if phase == OBS_MIGRATION_PHASE_COPYING:
                destination_entries = _build_obs_tree_inventory(destination)
                _validate_destination_subset(destination_entries, source_entries, destination)

                _copy_obs_inventory(source, destination, source_entries, owner_token)
                if _build_obs_tree_inventory(source) != source_entries:
                    raise _migration_recovery_error(destination, "コピー中に移行元の内容が変化しました。")
                copied_entries = _build_obs_tree_inventory(destination)
                if copied_entries != source_entries:
                    raise _migration_recovery_error(
                        destination,
                        "コピー後inventoryが移行元と双方向一致しません。余剰または不完全なentryがあります。",
                    )
                if finalize_destination is None:
                    _remove_obs_migration_journal_if_owned(marker, owner_token)
                    return source
                _write_obs_migration_journal(
                    marker,
                    source,
                    source_fingerprint,
                    owner_token,
                    phase=OBS_MIGRATION_PHASE_FINALIZE_PENDING,
                )
            else:
                if finalize_destination is None:
                    raise _migration_recovery_error(
                        destination,
                        "最終化待ちmarkerがありますが、最終化処理が指定されていません。",
                    )
                destination_entries = _build_obs_tree_inventory(destination)
                _validate_finalize_pending_destination(destination_entries, source_entries, destination)
        except OBSMigrationError:
            raise
        except (OSError, _UnsafeOBSMigrationPathError) as exc:
            raise _migration_recovery_error(destination, f"安全なコピー移行を完了できません: {exc}") from exc

        if finalize_destination is not None:
            context_token = _ACTIVE_OBS_MIGRATION_CAPABILITY.set((destination, owner_token))
            try:
                finalize_destination(destination)
            except Exception as exc:
                raise _migration_finalize_error(destination, exc) from exc
            finally:
                _ACTIVE_OBS_MIGRATION_CAPABILITY.reset(context_token)
            try:
                _recover_owned_transaction_temporaries(destination, source_entries, owner_token)
                _remove_obs_migration_journal_if_owned(marker, owner_token)
            except OBSMigrationError:
                raise
            except (OSError, _UnsafeOBSMigrationPathError) as exc:
                raise _migration_recovery_error(
                    destination,
                    f"最終化済みmarkerを安全に解除できません: {exc}",
                ) from exc
        return source
    finally:
        lock.release()


class OBSBootstrapper:
    """ポータブルOBSの検査と修復を分離して扱う。"""

    def __init__(
        self,
        base_dir: str | Path,
        process_manager: OBSProcessManager | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.base_dir = _absolute_path(base_dir)
        self.process_manager = process_manager or OBSProcessManager(self.base_dir)
        self.logger = logger or LOGGER

    @property
    def obs_exe(self) -> Path:
        return get_obs_executable_path(self.base_dir)

    def validate_layout(self, *, include_websocket: bool = True) -> bool:
        directories = (
            self.base_dir,
            self.base_dir / "config",
            get_obs_config_dir(self.base_dir),
        )
        for directory in directories:
            _validate_existing_path_chain(directory, expected_kind="directory")

        files = [
            self.obs_exe,
            get_portable_marker_path(self.base_dir),
            get_legacy_marker_path(self.base_dir),
            get_obs_global_ini_path(self.base_dir),
            get_obs_user_ini_path(self.base_dir),
            self.base_dir / ".lol_replay_obs_lease.json",
        ]
        if include_websocket:
            websocket_path = get_obs_websocket_config_path(self.base_dir)
            _validate_existing_path_chain(websocket_path.parent, expected_kind="directory")
            files.append(websocket_path)
        for path in files:
            _validate_existing_path_chain(path, expected_kind="file")
        return _validate_existing_path_chain(self.obs_exe, expected_kind="file")

    def _prepare_write_layout(self, *, include_websocket: bool = True) -> None:
        _ensure_safe_directory_chain(self.base_dir)
        self.validate_layout(include_websocket=include_websocket)

    def _preflight_existing_config_read(self, path: Path, *, label: str) -> None:
        if _path_lexists(path):
            _read_safe_file_bytes(
                path,
                max_bytes=OBS_BOOTSTRAP_CONFIG_MAX_BYTES,
                label=label,
            )

    def _preflight_existing_config_reads(self, *, include_websocket: bool) -> None:
        self._preflight_existing_config_read(
            get_obs_global_ini_path(self.base_dir),
            label="global.ini",
        )
        self._preflight_existing_config_read(
            get_obs_user_ini_path(self.base_dir),
            label="user.ini",
        )
        if include_websocket:
            self._preflight_existing_config_read(
                get_obs_websocket_config_path(self.base_dir),
                label="obs-websocket設定file",
            )

    def preflight_apply(self, port: int | None = None) -> None:
        """Validate every full-bootstrap target without creating or changing it."""

        include_websocket = port is not None
        self.validate_layout(include_websocket=include_websocket)
        self._preflight_existing_config_reads(include_websocket=include_websocket)

    def check(self) -> BootstrapReport:
        obs_exe_exists = self.validate_layout()
        global_ini = get_obs_global_ini_path(self.base_dir)
        user_ini = get_obs_user_ini_path(self.base_dir)
        global_ini_exists = _validate_existing_path_chain(global_ini, expected_kind="file")
        user_ini_exists = _validate_existing_path_chain(user_ini, expected_kind="file")
        global_parse_error = None
        user_parse_error = None
        missing_tray = []
        missing_startup = []
        missing_user_tray = []
        missing_user_startup = []
        if global_ini_exists:
            try:
                parser, had_bom = read_obs_ini_parser(global_ini)
                if had_bom:
                    missing_tray.append("encoding.BOM")
                missing_startup.extend(missing_ini_settings(parser, STARTUP_SETTINGS_SECTION, STARTUP_SETTINGS))
                missing_tray.extend(missing_ini_settings(parser, TRAY_SETTINGS_SECTION, TRAY_SETTINGS))
                for section in parser.sections():
                    for key in parser.options(section):
                        lower_key = key.lower()
                        allowed = section == TRAY_SETTINGS_SECTION and key in TRAY_SETTINGS
                        if ("systray" in lower_key or "hidetray" in lower_key) and not allowed:
                            missing_tray.append(f"{section}.{key}")
            except (OBSPathSafetyError, OSError):
                raise
            except (UnicodeError, configparser.Error) as e:
                global_parse_error = f"{type(e).__name__}: {e}"
        if user_ini_exists:
            try:
                parser, had_bom = read_obs_ini_parser(user_ini)
                if had_bom:
                    missing_user_tray.append("encoding.BOM")
                missing_user_startup.extend(missing_ini_settings(parser, STARTUP_SETTINGS_SECTION, STARTUP_SETTINGS))
                missing_user_tray.extend(missing_ini_settings(parser, TRAY_SETTINGS_SECTION, TRAY_SETTINGS))
                for section in parser.sections():
                    for key in parser.options(section):
                        lower_key = key.lower()
                        allowed = section == TRAY_SETTINGS_SECTION and key in TRAY_SETTINGS
                        if ("systray" in lower_key or "hidetray" in lower_key) and not allowed:
                            missing_user_tray.append(f"{section}.{key}")
            except (OBSPathSafetyError, OSError):
                raise
            except (UnicodeError, configparser.Error) as e:
                user_parse_error = f"{type(e).__name__}: {e}"

        return BootstrapReport(
            obs_dir=self.base_dir,
            obs_exe=self.obs_exe,
            obs_exe_exists=obs_exe_exists,
            portable_marker_exists=_validate_existing_path_chain(
                get_portable_marker_path(self.base_dir), expected_kind="file"
            ),
            legacy_marker_exists=_validate_existing_path_chain(
                get_legacy_marker_path(self.base_dir), expected_kind="file"
            ),
            config_dir_exists=_validate_existing_path_chain(
                get_obs_config_dir(self.base_dir), expected_kind="directory"
            ),
            global_ini_exists=global_ini_exists,
            user_ini_exists=user_ini_exists,
            global_ini_parse_error=global_parse_error,
            user_ini_parse_error=user_parse_error,
            missing_tray_settings=tuple(missing_tray),
            missing_startup_settings=tuple(missing_startup),
            missing_user_tray_settings=tuple(missing_user_tray),
            missing_user_startup_settings=tuple(missing_user_startup),
        )

    @_guard_obs_bootstrap_mutation
    def apply(
        self,
        port: int | None = None,
        password: str = "",
        *,
        stop_managed_processes: bool = True,
    ) -> dict[str, Any]:
        self._prepare_write_layout(include_websocket=port is not None)
        self._preflight_existing_config_reads(include_websocket=port is not None)
        if stop_managed_processes:
            self.process_manager.kill_stale_managed_processes()
        marker = self.ensure_portable_mode_marker()
        config_dir = self.ensure_config_dir()
        changed_ini, global_ini_path = self.ensure_global_ini(stop_managed_processes=False)
        changed_user_ini, user_ini_path = self.ensure_user_ini(stop_managed_processes=False)
        websocket_result = None
        if port is not None:
            websocket_result = self.ensure_websocket_config(port, password)
        return {
            "marker": marker,
            "config_dir": config_dir,
            "global_ini_changed": changed_ini,
            "global_ini_path": global_ini_path,
            "user_ini_changed": changed_user_ini,
            "user_ini_path": user_ini_path,
            "websocket": websocket_result,
        }

    def bootstrap(self, port: int | None = None, password: str = "") -> dict[str, Any]:
        """Backward-compatible alias for the full repair/setup flow."""
        return self.apply(port=port, password=password)

    @_guard_obs_bootstrap_mutation
    def ensure_portable_mode_marker(self) -> Path:
        self._prepare_write_layout(include_websocket=False)
        primary_marker = get_portable_marker_path(self.base_dir)
        if not _path_lexists(primary_marker):
            _write_safe_file_bytes(primary_marker, b"")

        legacy_marker = get_legacy_marker_path(self.base_dir)
        if not _path_lexists(legacy_marker):
            _write_safe_file_bytes(legacy_marker, b"")
        return primary_marker

    @_guard_obs_bootstrap_mutation
    def ensure_config_dir(self) -> Path:
        self._prepare_write_layout(include_websocket=False)
        config_dir = get_obs_config_dir(self.base_dir)
        _ensure_safe_directory_chain(config_dir)
        return config_dir

    @_guard_obs_bootstrap_mutation
    def ensure_global_ini(self, *, stop_managed_processes: bool = True) -> tuple[bool, Path]:
        # OBS reads global.ini only at startup and may rewrite it on exit.
        # Stop every process from this managed portable tree before patching.
        self._prepare_write_layout(include_websocket=False)
        self._preflight_existing_config_read(
            get_obs_global_ini_path(self.base_dir),
            label="global.ini",
        )
        if stop_managed_processes:
            self.process_manager.kill_stale_managed_processes()
        return self._ensure_obs_ini(get_obs_global_ini_path(self.base_dir), label="global.ini")

    @_guard_obs_bootstrap_mutation
    def ensure_user_ini(self, *, stop_managed_processes: bool = True) -> tuple[bool, Path]:
        # OBS 32.x reads UI startup and tray flags from user.ini.
        self._prepare_write_layout(include_websocket=False)
        self._preflight_existing_config_read(
            get_obs_user_ini_path(self.base_dir),
            label="user.ini",
        )
        if stop_managed_processes:
            self.process_manager.kill_stale_managed_processes()
        return self._ensure_obs_ini(get_obs_user_ini_path(self.base_dir), label="user.ini")

    def _ensure_obs_ini(self, ini_path: Path, label: str) -> tuple[bool, Path]:
        _ensure_safe_directory_chain(ini_path.parent)
        if _path_lexists(ini_path):
            _validate_existing_entry(ini_path, expected_kind="file")
        parser = new_obs_ini_parser()

        parse_failed = False
        normalized_encoding = False
        if _path_lexists(ini_path):
            try:
                parser, normalized_encoding = read_obs_ini_parser(ini_path)
            except (OBSPathSafetyError, OSError):
                raise
            except (UnicodeError, configparser.Error) as e:
                self.logger.warning("Corrupt OBS %s will be regenerated: %s (%s)", label, ini_path, e)
                parse_failed = True

        changed = parse_failed or normalized_encoding
        for section in parser.sections():
            for key in list(parser.options(section)):
                lower_key = key.lower()
                allowed = section == TRAY_SETTINGS_SECTION and key in TRAY_SETTINGS
                if ("systray" in lower_key or "hidetray" in lower_key) and not allowed:
                    parser.remove_option(section, key)
                    changed = True

        changed = apply_ini_settings(parser, STARTUP_SETTINGS_SECTION, STARTUP_SETTINGS) or changed
        changed = apply_ini_settings(parser, TRAY_SETTINGS_SECTION, TRAY_SETTINGS) or changed

        if changed or not _path_lexists(ini_path):
            buffer = io.StringIO()
            parser.write(buffer, space_around_delimiters=False)
            _write_safe_file_bytes(ini_path, buffer.getvalue().encode("utf-8"))
        return changed, ini_path

    @_guard_obs_bootstrap_mutation
    def ensure_websocket_config(self, port: int, password: str) -> tuple[bool, Path]:
        self._prepare_write_layout(include_websocket=True)
        config_path = get_obs_websocket_config_path(self.base_dir)
        _ensure_safe_directory_chain(config_path.parent)
        if _path_lexists(config_path):
            _validate_existing_entry(config_path, expected_kind="file")

        data = {}
        if _path_lexists(config_path):
            try:
                raw, _identity = _read_safe_file_bytes(
                    config_path,
                    max_bytes=OBS_BOOTSTRAP_CONFIG_MAX_BYTES,
                    label="obs-websocket設定file",
                )
                loaded = json.loads(raw.decode("utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OBSPathSafetyError, OSError):
                raise
            except (UnicodeError, json.JSONDecodeError) as e:
                self.logger.warning("OBS websocket config was unreadable and will be reset: %s", e, exc_info=True)
                data = {}

        changed = False

        def set_if_diff(key: str, value: Any) -> None:
            nonlocal changed
            if data.get(key) != value:
                data[key] = value
                changed = True

        port_value = max(1, min(65535, int(port)))
        password_text = str(password or "")
        if not password_text:
            raise ValueError("obs-websocket password must not be empty.")

        set_if_diff("server_enabled", True)
        set_if_diff("server_port", port_value)
        set_if_diff("auth_required", True)
        set_if_diff("server_password", password_text)

        if changed:
            payload = json.dumps(data, indent=4, ensure_ascii=False).encode("utf-8")
            _write_safe_file_bytes(config_path, payload)
        return changed, config_path
