from __future__ import annotations

import configparser
import hashlib
import io
import json
import logging
import os
import stat
import time
import uuid
from collections.abc import Callable, Iterable
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

try:
    from .obs_process import OBSProcessManager
except ImportError:
    from obs_process import OBSProcessManager

try:
    from . import obs_transaction_fs as _transaction_fs
except ImportError:
    import obs_transaction_fs as _transaction_fs


WINDOWS_LOCK_CONTENTION_ERRORS = _transaction_fs.WINDOWS_LOCK_CONTENTION_ERRORS
OBS_TRANSACTION_TEMP_COPY = _transaction_fs.OBS_TRANSACTION_TEMP_COPY
OBS_TRANSACTION_TEMP_WRITE = _transaction_fs.OBS_TRANSACTION_TEMP_WRITE
OBS_TRANSACTION_TEMP_JOURNAL = _transaction_fs.OBS_TRANSACTION_TEMP_JOURNAL
WINDOWS_RESERVED_PATH_NAMES = _transaction_fs.WINDOWS_RESERVED_PATH_NAMES
_filesystem_name_key = _transaction_fs._filesystem_name_key
_filesystem_parts_key = _transaction_fs._filesystem_parts_key
_filesystem_path_key = _transaction_fs._filesystem_path_key
_OBSTransactionTemporaryDescriptor = _transaction_fs._OBSTransactionTemporaryDescriptor
_OBSFilesystemMetadata = _transaction_fs._OBSFilesystemMetadata
_OBSPhysicalDirectoryComponent = _transaction_fs._OBSPhysicalDirectoryComponent
_OBSPhysicalDirectoryChain = _transaction_fs._OBSPhysicalDirectoryChain
OBSPathSafetyError = _transaction_fs.OBSPathSafetyError
_UnsafeOBSMigrationPathError = _transaction_fs._UnsafeOBSMigrationPathError
_OBSMigrationLockBusyError = _transaction_fs._OBSMigrationLockBusyError
_is_windows_lock_contention_error = _transaction_fs._is_windows_lock_contention_error
_validate_migration_owner_token = _transaction_fs._validate_migration_owner_token
_path_lexists = _transaction_fs._path_lexists
lexical_absolute_path = _transaction_fs.lexical_absolute_path
_absolute_path = _transaction_fs._absolute_path
_file_identity = _transaction_fs._file_identity
_validate_single_path_component = _transaction_fs._validate_single_path_component
_windows_raise_ntstatus = _transaction_fs._windows_raise_ntstatus
_windows_directory_access = _transaction_fs._windows_directory_access
_windows_open_absolute_directory = _transaction_fs._windows_open_absolute_directory
_windows_open_relative_handle = _transaction_fs._windows_open_relative_handle
_windows_handle_details = _transaction_fs._windows_handle_details
_windows_close_handle = _transaction_fs._windows_close_handle
_windows_rename_open_file = _transaction_fs._windows_rename_open_file
_windows_mark_open_file_for_deletion = _transaction_fs._windows_mark_open_file_for_deletion
_is_reparse_point = _transaction_fs._is_reparse_point
_validate_existing_entry = _transaction_fs._validate_existing_entry
_OBSDirectoryLease = _transaction_fs._OBSDirectoryLease
_snapshot_open_entry_metadata = _transaction_fs._snapshot_open_entry_metadata
_physical_directory_chain = _transaction_fs._physical_directory_chain
_validate_distinct_physical_directory_trees = (
    _transaction_fs._validate_distinct_physical_directory_trees
)
_supports_posix_handle_relative_migration = (
    _transaction_fs._supports_posix_handle_relative_migration
)
_supports_handle_relative_migration = _transaction_fs._supports_handle_relative_migration
_OBSInterProcessLock = _transaction_fs._OBSInterProcessLock
_write_all = _transaction_fs._write_all
_fsync_parent_directory = _transaction_fs._fsync_parent_directory
_transaction_temporary_path = _transaction_fs._transaction_temporary_path
_transaction_copy_temporary_path = _transaction_fs._transaction_copy_temporary_path
_transaction_write_temporary_path = _transaction_fs._transaction_write_temporary_path
_transaction_journal_temporary_path = _transaction_fs._transaction_journal_temporary_path
_read_safe_relative_file_bytes = _transaction_fs._read_safe_relative_file_bytes
_directory_for_descendant_parent = _transaction_fs._directory_for_descendant_parent

if os.name == "nt":
    _WINDOWS_INVALID_HANDLE = _transaction_fs._WINDOWS_INVALID_HANDLE
    _WINDOWS_GENERIC_READ = _transaction_fs._WINDOWS_GENERIC_READ
    _WINDOWS_GENERIC_WRITE = _transaction_fs._WINDOWS_GENERIC_WRITE
    _WINDOWS_DELETE = _transaction_fs._WINDOWS_DELETE
    _WINDOWS_READ_CONTROL = _transaction_fs._WINDOWS_READ_CONTROL
    _WINDOWS_SYNCHRONIZE = _transaction_fs._WINDOWS_SYNCHRONIZE
    _WINDOWS_MAXIMUM_ALLOWED = _transaction_fs._WINDOWS_MAXIMUM_ALLOWED
    _WINDOWS_FILE_READ_DATA = _transaction_fs._WINDOWS_FILE_READ_DATA
    _WINDOWS_FILE_WRITE_DATA = _transaction_fs._WINDOWS_FILE_WRITE_DATA
    _WINDOWS_FILE_LIST_DIRECTORY = _transaction_fs._WINDOWS_FILE_LIST_DIRECTORY
    _WINDOWS_FILE_ADD_FILE = _transaction_fs._WINDOWS_FILE_ADD_FILE
    _WINDOWS_FILE_ADD_SUBDIRECTORY = _transaction_fs._WINDOWS_FILE_ADD_SUBDIRECTORY
    _WINDOWS_FILE_TRAVERSE = _transaction_fs._WINDOWS_FILE_TRAVERSE
    _WINDOWS_FILE_READ_ATTRIBUTES = _transaction_fs._WINDOWS_FILE_READ_ATTRIBUTES
    _WINDOWS_FILE_WRITE_ATTRIBUTES = _transaction_fs._WINDOWS_FILE_WRITE_ATTRIBUTES
    _WINDOWS_FILE_SHARE_READ_WRITE = _transaction_fs._WINDOWS_FILE_SHARE_READ_WRITE
    _WINDOWS_FILE_SHARE_ALL = _transaction_fs._WINDOWS_FILE_SHARE_ALL
    _WINDOWS_OPEN_EXISTING = _transaction_fs._WINDOWS_OPEN_EXISTING
    _WINDOWS_FILE_OPEN = _transaction_fs._WINDOWS_FILE_OPEN
    _WINDOWS_FILE_CREATE = _transaction_fs._WINDOWS_FILE_CREATE
    _WINDOWS_FILE_OPEN_IF = _transaction_fs._WINDOWS_FILE_OPEN_IF
    _WINDOWS_FILE_DIRECTORY_FILE = _transaction_fs._WINDOWS_FILE_DIRECTORY_FILE
    _WINDOWS_FILE_NON_DIRECTORY_FILE = _transaction_fs._WINDOWS_FILE_NON_DIRECTORY_FILE
    _WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT = (
        _transaction_fs._WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT
    )
    _WINDOWS_FILE_OPEN_REPARSE_POINT = _transaction_fs._WINDOWS_FILE_OPEN_REPARSE_POINT
    _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = (
        _transaction_fs._WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
    )
    _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = (
        _transaction_fs._WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
    )
    _WINDOWS_OBJ_CASE_INSENSITIVE = _transaction_fs._WINDOWS_OBJ_CASE_INSENSITIVE
    _WINDOWS_FILE_ATTRIBUTE_NORMAL = _transaction_fs._WINDOWS_FILE_ATTRIBUTE_NORMAL
    _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = (
        _transaction_fs._WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT
    )
    _WINDOWS_FILE_RENAME_INFO = _transaction_fs._WINDOWS_FILE_RENAME_INFO
    _WINDOWS_FILE_DISPOSITION_INFO = _transaction_fs._WINDOWS_FILE_DISPOSITION_INFO
    _WINDOWS_FILE_STREAM_INFO = _transaction_fs._WINDOWS_FILE_STREAM_INFO
    _WINDOWS_NT_FILE_RENAME_INFORMATION = (
        _transaction_fs._WINDOWS_NT_FILE_RENAME_INFORMATION
    )
    _WindowsUnicodeString = _transaction_fs._WindowsUnicodeString
    _WindowsObjectAttributes = _transaction_fs._WindowsObjectAttributes
    _WindowsIOStatusBlock = _transaction_fs._WindowsIOStatusBlock
    _WindowsByHandleFileInformation = _transaction_fs._WindowsByHandleFileInformation
    _WindowsFileDispositionInfo = _transaction_fs._WindowsFileDispositionInfo
    _WindowsFileRenameInfoHeader = _transaction_fs._WindowsFileRenameInfoHeader
    _WindowsFileStreamInfo = _transaction_fs._WindowsFileStreamInfo
    _WINDOWS_KERNEL32 = _transaction_fs._WINDOWS_KERNEL32
    _WINDOWS_NTDLL = _transaction_fs._WINDOWS_NTDLL
    _WINDOWS_ADVAPI32 = _transaction_fs._WINDOWS_ADVAPI32


def _parse_transaction_temporary(
    path: Path,
) -> _OBSTransactionTemporaryDescriptor | None:
    return _transaction_fs._parse_transaction_temporary(
        path,
        journal_target_name=OBS_COPY_IN_PROGRESS_MARKER_NAME,
    )


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
        ".lol_replay_obs_settings_transaction.json",
        "temp_appdata",
    }
)
OBS_COPY_IN_PROGRESS_MARKER_NAME = ".lol_replay_obs_copy_in_progress"
OBS_COPY_LOCK_NAME = ".lol_replay_obs_copy_lock"
OBS_SETTINGS_TRANSACTION_MARKER_NAME = ".lol_replay_obs_settings_transaction.json"
OBS_FINALIZE_INVENTORY_SKIP_NAMES = frozenset(
    {OBS_COPY_IN_PROGRESS_MARKER_NAME, OBS_COPY_LOCK_NAME}
)
OBS_FINALIZER_CALLBACK_INVENTORY_SKIP_NAMES = frozenset({OBS_COPY_LOCK_NAME})
OBS_COPY_JOURNAL_SCHEMA_VERSION = 4
OBS_COPY_JOURNAL_COMPATIBLE_SCHEMA_VERSIONS = frozenset({3, 4})
OBS_MIGRATION_PHASE_COPYING = "copying"
OBS_MIGRATION_PHASE_FINALIZE_PENDING = "finalize_pending"
OBS_COPY_JOURNAL_MAX_BYTES = 64 * 1024
OBS_BOOTSTRAP_CONFIG_MAX_BYTES = 16 * 1024 * 1024
OBS_SETTINGS_TRANSACTION_MAX_BYTES = 64 * 1024 * 1024
OBS_MIGRATION_FINALIZE_FILE_PARTS = frozenset(
    {
        (PORTABLE_OBS_MARKER_NAME,),
        (LEGACY_PORTABLE_OBS_MARKER_NAME,),
        ("config", "obs-studio", "global.ini"),
        ("config", "obs-studio", "user.ini"),
        (
            "config",
            "obs-studio",
            "plugin_config",
            "obs-websocket",
            "config.json",
        ),
    }
)
OBS_MIGRATION_FINALIZE_DIRECTORY_PARTS = frozenset(
    {
        ("config",),
        ("config", "obs-studio"),
        ("config", "obs-studio", "plugin_config"),
        ("config", "obs-studio", "plugin_config", "obs-websocket"),
    }
)


OBS_COPY_SKIP_NAME_KEYS = frozenset(
    _filesystem_name_key(name) for name in OBS_COPY_SKIP_NAMES
)
OBS_MIGRATION_FINALIZE_FILE_KEYS = frozenset(
    _filesystem_parts_key(parts) for parts in OBS_MIGRATION_FINALIZE_FILE_PARTS
)
OBS_MIGRATION_FINALIZE_DIRECTORY_KEYS = frozenset(
    _filesystem_parts_key(parts) for parts in OBS_MIGRATION_FINALIZE_DIRECTORY_PARTS
)
_ACTIVE_OBS_MIGRATION_CAPABILITY: ContextVar[tuple[Path, str] | None] = ContextVar(
    "active_obs_migration_capability",
    default=None,
)
_ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE: ContextVar[Any | None] = ContextVar(
    "active_obs_migration_directory_lease",
    default=None,
)
_ACTIVE_OBS_MIGRATION_VALIDATOR: ContextVar[Callable[[], None] | None] = ContextVar(
    "active_obs_migration_validator",
    default=None,
)
_ACTIVE_OBS_BOOTSTRAP_MUTATION: ContextVar[Any | None] = ContextVar(
    "active_obs_bootstrap_mutation",
    default=None,
)
_ACTIVE_OBS_SETTINGS_TRANSACTION_OWNER: ContextVar[str | None] = ContextVar(
    "active_obs_settings_transaction_owner",
    default=None,
)
_ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS: ContextVar[frozenset[str] | None] = ContextVar(
    "active_obs_settings_transaction_targets",
    default=None,
)


class OBSMigrationError(RuntimeError):
    """Base error for the portable OBS migration transaction."""


class OBSMigrationInProgressError(OBSMigrationError):
    """Raised when another process owns the migration lock."""


class OBSMigrationRecoveryRequiredError(OBSMigrationError):
    """Raised when a stale journal cannot be resumed safely."""


class OBSSettingsTransactionError(RuntimeError):
    """Raised when a guarded settings transaction is rolled back."""


class OBSSettingsRecoveryRequiredError(OBSSettingsTransactionError):
    """Raised when a stale settings transaction cannot be rolled back safely."""


class _OBSSettingsCommitDurabilityUncertainError(RuntimeError):
    """Keep recovery material when the committed journal may not be durable."""


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
    metadata: _OBSFilesystemMetadata | None = None

    @property
    def relative_path(self) -> str:
        return "/".join(self.relative_parts)

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
class OBSConfigPlannedWrite:
    """One immutable target and its desired bytes in a settings transaction."""

    snapshot: OBSConfigFileSnapshot
    payload: bytes

    @property
    def changed(self) -> bool:
        return self.snapshot.payload != self.payload


@dataclass(frozen=True)
class OBSConfigTransactionPlan:
    """All directories and files committed under one OBS mutation guard."""

    base_dir: Path
    directories: tuple[Path, ...]
    writes: tuple[OBSConfigPlannedWrite, ...]


@dataclass(frozen=True)
class OBSBootstrapApplyPlan:
    """Prepared portable-mode/bootstrap updates without filesystem mutation."""

    transaction: OBSConfigTransactionPlan
    marker: Path
    config_dir: Path
    global_ini_path: Path
    user_ini_path: Path
    websocket_path: Path | None


@dataclass(frozen=True)
class _OBSSettingsJournalEntry:
    target: Path
    label: str
    original_exists: bool
    original_size: int
    original_sha256: str
    desired_size: int
    desired_sha256: str


@dataclass(frozen=True)
class _OBSSettingsJournal:
    owner_token: str
    phase: str
    entries: tuple[_OBSSettingsJournalEntry, ...]


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


def _parse_obs_ini_payload(payload: bytes) -> tuple[configparser.ConfigParser, bool]:
    parser = new_obs_ini_parser()
    text = payload.decode("utf-8")
    had_bom = text.startswith("\ufeff")
    if had_bom:
        text = text.lstrip("\ufeff")
    parser.read_string(text)
    return parser, had_bom


def read_obs_ini_parser(path: Path) -> tuple[configparser.ConfigParser, bool]:
    """BOMなしUTF-8として読み、混入BOMは除去対象として検出する。"""
    raw, _identity = _read_safe_file_bytes(
        path,
        max_bytes=OBS_BOOTSTRAP_CONFIG_MAX_BYTES,
        label="OBS設定file",
    )
    return _parse_obs_ini_payload(raw)


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


def get_obs_settings_transaction_marker(base_dir: str | Path) -> Path:
    return Path(base_dir) / OBS_SETTINGS_TRANSACTION_MARKER_NAME






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


def _is_valid_obs_installation_lease(root_lease: _OBSDirectoryLease) -> bool:
    """Authoritatively validate the minimal OBS layout relative to a held root."""

    root_lease.validate_lexical_binding()
    try:
        binary_directory = root_lease.open_descendant_directory(("bin", "64bit"))
    except FileNotFoundError:
        return False
    try:
        try:
            descriptor = binary_directory.open_file(
                "obs64.exe",
                write=False,
                create_exclusive=False,
            )
        except FileNotFoundError:
            return False
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or int(opened.st_nlink) != 1:
                raise _UnsafeOBSMigrationPathError(
                    f"安全なOBS executableではありません: {binary_directory.path / 'obs64.exe'}"
                )
        finally:
            os.close(descriptor)
    finally:
        binary_directory.close()
    root_lease.validate_lexical_binding()
    return True


def _active_obs_mutation_root_lease() -> _OBSDirectoryLease | None:
    """Return the held physical root after proving its lexical ownership."""

    migration_directory = _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.get()
    if migration_directory is not None:
        validator = _ACTIVE_OBS_MIGRATION_VALIDATOR.get()
        if validator is not None:
            validator()
        migration_directory.validate_lexical_binding()
        return migration_directory
    scope = _ACTIVE_OBS_BOOTSTRAP_MUTATION.get()
    if scope is None or scope.lock is None:
        return None
    scope.lock.validate_ownership()
    root_lease = scope.lock.directory_lease
    if root_lease.path != scope.base_dir:
        raise OBSPathSafetyError(
            "OBS設定lockの物理rootとmutation scopeが一致しません: "
            f"{root_lease.path} != {scope.base_dir}"
        )
    return root_lease


def _validate_active_obs_mutation_boundary() -> None:
    root_lease = _active_obs_mutation_root_lease()
    if root_lease is not None:
        root_lease.validate_lexical_binding()


def _ensure_safe_directory_chain(path: Path) -> None:
    absolute = _absolute_path(path)
    migration_directory = _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.get()
    if migration_directory is not None:
        if absolute == migration_directory.path:
            migration_directory.validate_lexical_binding()
            return
        try:
            relative = absolute.relative_to(migration_directory.path)
        except ValueError as exc:
            raise OBSPathSafetyError(
                f"OBS移行finalizerがdestination外へdirectoryを作成できません: {absolute}"
            ) from exc
        relative_key = _filesystem_parts_key(relative.parts)
        if relative_key not in OBS_MIGRATION_FINALIZE_DIRECTORY_KEYS:
            raise OBSPathSafetyError(
                f"OBS移行finalizerが作成できるdirectoryではありません: {absolute}"
            )
        validator = _ACTIVE_OBS_MIGRATION_VALIDATOR.get()
        if validator is not None:
            validator()
        with migration_directory.open_descendant_directory(
            tuple(relative.parts),
            create=True,
        ):
            pass
        return
    settings_root = _active_obs_mutation_root_lease()
    if settings_root is not None:
        if absolute == settings_root.path:
            settings_root.validate_lexical_binding()
            return
        try:
            relative = absolute.relative_to(settings_root.path)
        except ValueError as exc:
            raise OBSPathSafetyError(
                f"OBS設定transactionが管理root外へdirectoryを作成できません: {absolute}"
            ) from exc
        with settings_root.open_descendant_directory(
            tuple(relative.parts),
            create=True,
        ):
            pass
        settings_root.validate_lexical_binding()
        return
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




def is_obs_copy_in_progress(base_dir: str | Path) -> bool:
    base_path = _absolute_path(base_dir)
    marker = get_obs_copy_in_progress_marker(base_path)
    settings_marker = get_obs_settings_transaction_marker(base_path)
    lock_path = get_obs_copy_lock_path(base_path)
    lock = _OBSInterProcessLock(lock_path)
    destination_probe: _OBSDirectoryLease | None = None
    try:
        try:
            destination_probe = _OBSDirectoryLease.open_absolute(base_path)
        except FileNotFoundError:
            return False

        def scan_without_lock() -> bool:
            if (
                destination_probe.relative_file_identity_or_none(marker.name)
                is not None
                or destination_probe.relative_file_identity_or_none(lock_path.name)
                is not None
            ):
                return True
            if (
                destination_probe.relative_file_identity_or_none(settings_marker.name)
                is not None
            ):
                return False
            root_temporaries = _root_transaction_temporaries(base_path)
            if (
                len(root_temporaries) == 1
                and root_temporaries[0].kind == OBS_TRANSACTION_TEMP_WRITE
                and _filesystem_name_key(root_temporaries[0].target_name)
                == _filesystem_name_key(settings_marker.name)
            ):
                return False
            has_temporary = _has_transaction_temporary_name_under_lease(
                destination_probe
            )
            return (
                destination_probe.relative_file_identity_or_none(marker.name)
                is not None
                or destination_probe.relative_file_identity_or_none(lock_path.name)
                is not None
                or (
                    has_temporary
                )
            )

        if destination_probe.relative_file_identity_or_none(marker.name) is not None:
            return True
        if destination_probe.relative_file_identity_or_none(lock_path.name) is None:
            return scan_without_lock()
        if not lock.acquire(
            create_parent=False,
            directory_lease=destination_probe,
            initialize_empty=False,
        ):
            return True
        try:
            try:
                locked_directory = lock.directory_lease
            except _UnsafeOBSMigrationPathError:
                return scan_without_lock()
            if locked_directory.relative_file_identity_or_none(marker.name) is not None:
                return True
            if (
                locked_directory.relative_file_identity_or_none(settings_marker.name)
                is not None
            ):
                return False
            root_temporaries = _root_transaction_temporaries(base_path)
            if (
                len(root_temporaries) == 1
                and root_temporaries[0].kind == OBS_TRANSACTION_TEMP_WRITE
                and _filesystem_name_key(root_temporaries[0].target_name)
                == _filesystem_name_key(settings_marker.name)
            ):
                return False
            return bool(_list_root_transaction_temporaries(locked_directory))
        except (OSError, _UnsafeOBSMigrationPathError):
            return True
    except (OSError, _UnsafeOBSMigrationPathError):
        return True
    finally:
        try:
            if destination_probe is not None:
                destination_probe.close()
        finally:
            lock.release()


def _normalized_obs_paths(paths: Iterable[str | Path], *, excluded: str | Path) -> tuple[Path, ...]:
    excluded_path = _absolute_path(excluded)
    excluded_key = _filesystem_path_key(excluded_path)
    excluded_parts = _filesystem_parts_key(excluded_path.parts)
    normalized: list[Path] = []
    seen: set[str] = set()
    for value in paths:
        path = _absolute_path(value)
        key = _filesystem_path_key(path)
        path_parts = _filesystem_parts_key(path.parts)
        overlaps_destination = (
            path_parts[: len(excluded_parts)] == excluded_parts
            or excluded_parts[: len(path_parts)] == path_parts
        )
        if overlaps_destination:
            raise _UnsafeOBSMigrationPathError(
                f"移行元とdestinationを同一またはancestor関係にできません: {path} / {excluded_path}"
            )
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


def _read_obs_migration_journal(
    marker: Path,
    *,
    directory_lease: _OBSDirectoryLease | None = None,
    expected_identity: tuple[int, int, int] | None = None,
) -> OBSMigrationJournal:
    try:
        if directory_lease is None:
            active_lease = _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.get()
            if active_lease is not None and active_lease.path == _absolute_path(marker.parent):
                directory_lease = active_lease
        if directory_lease is None:
            raw, identity = _read_safe_regular_file(marker)
        else:
            raw, identity = _read_safe_relative_file_bytes(
                directory_lease,
                marker.name,
                max_bytes=OBS_COPY_JOURNAL_MAX_BYTES,
                label="marker",
            )
        if expected_identity is not None and identity != expected_identity:
            raise _UnsafeOBSMigrationPathError(
                f"検証後にmarker identityが変化しました: {marker}"
            )
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
        if schema_version not in OBS_COPY_JOURNAL_COMPATIBLE_SCHEMA_VERSIONS:
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
    allowed_by_key = {_filesystem_path_key(path): path for path in allowed_sources}
    source_key = _filesystem_path_key(source)
    if source_key not in allowed_by_key:
        raise _migration_recovery_error(destination, f"markerの移行元は許可済みlegacy候補ではありません: {source}")
    return allowed_by_key[source_key]


def _active_obs_mutation_parent_for_file(
    path: Path,
) -> tuple[_OBSDirectoryLease, bool] | None:
    root_lease = _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.get()
    absolute = _absolute_path(path)
    if root_lease is not None:
        try:
            relative = absolute.relative_to(root_lease.path)
        except ValueError as exc:
            raise OBSPathSafetyError(
                f"OBS移行destination外のconfigを参照できません: {absolute}"
            ) from exc
        if _filesystem_parts_key(relative.parts) not in OBS_MIGRATION_FINALIZE_FILE_KEYS:
            raise OBSPathSafetyError(
                f"OBS移行finalizerが参照できるfileではありません: {absolute}"
            )
        validator = _ACTIVE_OBS_MIGRATION_VALIDATOR.get()
        if validator is not None:
            validator()
    else:
        root_lease = _active_obs_mutation_root_lease()
        if root_lease is None:
            return None
        try:
            absolute.relative_to(root_lease.path)
        except ValueError as exc:
            raise OBSPathSafetyError(
                f"OBS設定transaction root外のconfigを参照できません: {absolute}"
            ) from exc
    return _directory_for_descendant_parent(
        root_lease,
        absolute,
        mutable=False,
    )


def _safe_config_file_exists(path: Path) -> bool:
    try:
        active_parent = _active_obs_mutation_parent_for_file(path)
    except FileNotFoundError:
        return False
    if active_parent is None:
        return _path_lexists(path)
    parent, parent_owned = active_parent
    try:
        try:
            return parent.relative_file_identity_or_none(path.name) is not None
        except PermissionError as file_error:
            # Windows reports ACCESS_DENIED when a directory is opened with a
            # non-directory handle. Prove that case relative to the held parent
            # so callers receive the same path-safety error as lexical checks.
            try:
                child_directory = parent.open_child_directory(path.name)
            except _UnsafeOBSMigrationPathError:
                raise
            except OSError as directory_error:
                raise file_error from directory_error
            else:
                child_directory.close()
                raise _UnsafeOBSMigrationPathError(
                    f"通常ファイルではありません: {path}"
                ) from file_error
    finally:
        if parent_owned:
            parent.close()


def _read_safe_file_bytes(
    path: Path,
    *,
    max_bytes: int,
    label: str,
) -> tuple[bytes, tuple[int, int, int]]:
    active_parent = _active_obs_mutation_parent_for_file(path)
    if active_parent is not None:
        parent, parent_owned = active_parent
        try:
            return _read_safe_relative_file_bytes(
                parent,
                path.name,
                max_bytes=max_bytes,
                label=label,
            )
        finally:
            if parent_owned:
                parent.close()
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
    if _active_obs_mutation_root_lease() is None:
        preflight_obs_config_directory(absolute.parent)
    if not _safe_config_file_exists(absolute):
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
    if _active_obs_mutation_root_lease() is None:
        preflight_obs_config_directory(path.parent)
    if snapshot.payload is None:
        if _safe_config_file_exists(path):
            raise OBSPathSafetyError(f"preflight後にconfig fileが作成されました: {path}")
        if snapshot.identity is not None:
            raise OBSPathSafetyError(f"missing config snapshotにidentityがあります: {path}")
        return

    if snapshot.identity is None:
        raise OBSPathSafetyError(f"existing config snapshotにidentityがありません: {path}")
    if not _safe_config_file_exists(path):
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




def _require_obs_config_write_scope(path: str | Path) -> Path:
    """Require a guarded lexical target below the managed OBS root."""

    absolute = _absolute_path(path)
    scope = _ACTIVE_OBS_BOOTSTRAP_MUTATION.get()
    if scope is None:
        raise OBSPathSafetyError(
            "OBS設定fileはobs_config_mutation_guardの外では変更できません。"
        )
    _validate_active_obs_mutation_boundary()
    try:
        relative = absolute.relative_to(scope.base_dir)
    except ValueError as exc:
        raise OBSPathSafetyError(
            f"migration destination外または管理OBS root外へ設定fileを書き込めません: {absolute}"
        ) from exc
    if not relative.parts:
        raise OBSPathSafetyError(f"管理OBS root自体をfileとして変更できません: {absolute}")
    for part in relative.parts:
        _validate_single_path_component(part)
    return absolute


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
            relative = absolute_path.relative_to(destination)
        except ValueError as exc:
            raise OBSPathSafetyError(
                f"OBS移行destination外へtransaction一時fileを書き込めません: {absolute_path}"
            ) from exc
        relative_parts = _filesystem_parts_key(relative.parts)
        if relative_parts not in OBS_MIGRATION_FINALIZE_FILE_KEYS:
            raise OBSPathSafetyError(
                "OBS移行finalizerが変更できるfileではありません: "
                f"{absolute_path}"
            )
        return _transaction_write_temporary_path(absolute_path, owner_token)
    settings_owner = _ACTIVE_OBS_SETTINGS_TRANSACTION_OWNER.get()
    if settings_owner is not None:
        return _transaction_write_temporary_path(path, settings_owner)
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.write.tmp")


def _write_safe_file_bytes(
    path: Path,
    payload: bytes,
    *,
    expected_snapshot: OBSConfigFileSnapshot | None = None,
) -> None:
    path = _require_obs_config_write_scope(path)
    if expected_snapshot is not None:
        if expected_snapshot.path != path:
            raise OBSPathSafetyError(
                f"config snapshotと書き込み先が一致しません: {expected_snapshot.path} != {path}"
            )
        revalidate_obs_config_file(expected_snapshot)
    migration_directory_lease = _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.get()
    if migration_directory_lease is not None:
        _write_safe_file_bytes_with_migration_lease(
            path,
            payload,
            migration_directory_lease,
            expected_snapshot=expected_snapshot,
        )
        return
    settings_root_lease = _active_obs_mutation_root_lease()
    if settings_root_lease is not None:
        _write_safe_file_bytes_with_migration_lease(
            path,
            payload,
            settings_root_lease,
            expected_snapshot=expected_snapshot,
        )
        return
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

    path = _require_obs_config_write_scope(snapshot.path)
    planned_targets = _ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.get()
    if planned_targets is None or _filesystem_path_key(path) not in planned_targets:
        raise OBSPathSafetyError(
            f"OBS設定transactionで計画されていないfileは変更できません: {path}"
        )

    _write_safe_file_bytes(
        path,
        payload,
        expected_snapshot=snapshot,
    )
    return snapshot.path


def delete_preflighted_obs_config_file(snapshot: OBSConfigFileSnapshot) -> Path:
    """Delete one guarded managed-root file only while its snapshot matches."""

    path = _require_obs_config_write_scope(snapshot.path)
    planned_targets = _ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.get()
    if planned_targets is None or _filesystem_path_key(path) not in planned_targets:
        raise OBSPathSafetyError(
            f"OBS設定transactionで削除を計画されていないfileは変更できません: {path}"
        )
    if snapshot.payload is None or snapshot.identity is None:
        raise OBSPathSafetyError(f"削除対象fileがpreflight時に存在しません: {path}")
    revalidate_obs_config_file(snapshot)
    root_lease = _active_obs_mutation_root_lease()
    if root_lease is None:
        raise OBSPathSafetyError("削除対象に対応する管理OBS root leaseがありません。")
    parent, parent_owned = _directory_for_descendant_parent(root_lease, path)
    try:
        parent.unlink_file(path.name, expected_identity=snapshot.identity)
    finally:
        if parent_owned:
            parent.close()
    _validate_active_obs_mutation_boundary()
    return path


def _settings_payload_digest(payload: bytes) -> tuple[int, str]:
    return len(payload), hashlib.sha256(payload).hexdigest()


def _settings_recovery_error(base_dir: Path, reason: str) -> OBSSettingsRecoveryRequiredError:
    marker = get_obs_settings_transaction_marker(base_dir)
    return OBSSettingsRecoveryRequiredError(
        "OBS起動前設定transactionを安全に復旧できません。"
        f" 管理対象OBSを停止したまま再試行してください。marker={marker} reason={reason} "
        "再試行でも復旧できない場合はobs-portable全体を退避し、利用者が取得した"
        "公式OBS ZIPを専用obs-portableへ再展開してから設定を再構成してください。"
    )


def _settings_relative_parts(base_dir: Path, path: Path) -> tuple[str, ...]:
    absolute = _require_obs_config_write_scope(path)
    try:
        relative = absolute.relative_to(base_dir)
    except ValueError as exc:
        raise OBSPathSafetyError(
            f"設定transaction targetが管理OBS root外です: {absolute}"
        ) from exc
    if not relative.parts:
        raise OBSPathSafetyError(f"管理OBS root自体を設定targetにできません: {absolute}")
    for part in relative.parts:
        _validate_single_path_component(part)
    return tuple(relative.parts)


def _validate_settings_target_components(parts: tuple[str, ...]) -> None:
    for part in parts:
        try:
            temporary = _parse_transaction_temporary(Path(part))
        except _UnsafeOBSMigrationPathError as exc:
            raise OBSPathSafetyError(
                f"設定target componentが不正なtransaction一時file形式です: {part}"
            ) from exc
        if temporary is not None:
            raise OBSPathSafetyError(
                f"transaction一時file形式のcomponentを設定targetにできません: {part}"
            )


def _validated_settings_transaction_plan(
    plan: OBSConfigTransactionPlan,
) -> tuple[
    Path,
    tuple[Path, ...],
    tuple[OBSConfigPlannedWrite, ...],
    tuple[OBSConfigPlannedWrite, ...],
]:
    base_dir = _absolute_path(plan.base_dir)
    scope = _ACTIVE_OBS_BOOTSTRAP_MUTATION.get()
    if scope is None or scope.base_dir != base_dir:
        raise OBSPathSafetyError(
            "設定transaction planは対応する管理OBS mutation guard内でのみ実行できます。"
        )

    reserved_root_names = OBS_COPY_SKIP_NAME_KEYS
    directories: list[Path] = []
    seen_directories: set[str] = set()
    for value in plan.directories:
        directory = _absolute_path(value)
        if directory != base_dir:
            relative_parts = _settings_relative_parts(base_dir, directory)
            _validate_settings_target_components(relative_parts)
            if _filesystem_name_key(relative_parts[0]) in reserved_root_names:
                raise OBSPathSafetyError(
                    "transaction管理pathを設定directoryにできません: "
                    f"{directory}"
                )
        key = _filesystem_path_key(directory)
        if key not in seen_directories:
            seen_directories.add(key)
            directories.append(directory)

    planned_writes: list[OBSConfigPlannedWrite] = []
    changed_writes: list[OBSConfigPlannedWrite] = []
    seen_targets: set[str] = set()
    reserved_targets = {
        _filesystem_path_key(base_dir / name) for name in OBS_COPY_SKIP_NAMES
    }
    for write in plan.writes:
        if (
            not isinstance(write.snapshot.label, str)
            or not write.snapshot.label
            or write.snapshot.label.strip() != write.snapshot.label
        ):
            raise OBSPathSafetyError(
                f"設定transaction labelが不正です: {write.snapshot.label!r}"
            )
        if not isinstance(write.payload, bytes):
            raise OBSPathSafetyError(
                "設定transaction desired payloadはbytesである必要があります: "
                f"{write.snapshot.path}"
            )
        if len(write.payload) > OBS_BOOTSTRAP_CONFIG_MAX_BYTES:
            raise OBSPathSafetyError(
                "設定transaction desired payloadが上限を超えています: "
                f"{write.snapshot.path} "
                f"({len(write.payload)} > {OBS_BOOTSTRAP_CONFIG_MAX_BYTES})"
            )
        target = _absolute_path(write.snapshot.path)
        if target != write.snapshot.path:
            raise OBSPathSafetyError(
                f"設定transaction snapshotがlexical absoluteではありません: {write.snapshot.path}"
            )
        relative_parts = _settings_relative_parts(base_dir, target)
        _validate_settings_target_components(relative_parts)
        if _filesystem_name_key(relative_parts[0]) in reserved_root_names:
            raise OBSPathSafetyError(
                f"transaction管理namespaceを設定targetにできません: {target}"
            )
        key = _filesystem_path_key(target)
        if key in reserved_targets:
            raise OBSPathSafetyError(f"transaction管理fileを設定targetにできません: {target}")
        if key in seen_targets:
            raise OBSPathSafetyError(f"設定transaction targetが重複しています: {target}")
        seen_targets.add(key)
        planned_writes.append(write)
        if write.changed:
            changed_writes.append(write)
    return (
        base_dir,
        tuple(directories),
        tuple(planned_writes),
        tuple(changed_writes),
    )


def _settings_journal_payload(
    base_dir: Path,
    owner_token: str,
    phase: str,
    writes: tuple[OBSConfigPlannedWrite, ...],
) -> bytes:
    owner_token = _validate_migration_owner_token(owner_token)
    if phase not in {"preparing", "committing", "committed"}:
        raise ValueError(f"unsupported settings transaction phase: {phase}")
    entries: list[dict[str, object]] = []
    for write in writes:
        original = write.snapshot.payload
        original_size, original_sha256 = _settings_payload_digest(original or b"")
        desired_size, desired_sha256 = _settings_payload_digest(write.payload)
        entries.append(
            {
                "path": "/".join(_settings_relative_parts(base_dir, write.snapshot.path)),
                "label": write.snapshot.label,
                "original_exists": original is not None,
                "original_size": original_size,
                "original_sha256": original_sha256,
                "desired_size": desired_size,
                "desired_sha256": desired_sha256,
            }
        )
    payload = json.dumps(
        {
            "schema_version": 1,
            "owner_token": owner_token,
            "phase": phase,
            "entries": entries,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(payload) > OBS_SETTINGS_TRANSACTION_MAX_BYTES:
        raise OBSPathSafetyError(
            "OBS設定transaction journalが上限を超えています: "
            f"{len(payload)} > {OBS_SETTINGS_TRANSACTION_MAX_BYTES}"
        )
    return payload


def _parse_settings_journal(base_dir: Path, payload: bytes) -> _OBSSettingsJournal:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _settings_recovery_error(base_dir, f"journal JSONが壊れています: {exc}") from exc
    if (
        not isinstance(raw, dict)
        or type(raw.get("schema_version")) is not int
        or raw.get("schema_version") != 1
    ):
        raise _settings_recovery_error(base_dir, "未対応のjournal schemaです。")
    try:
        raw_owner_token = raw["owner_token"]
        if not isinstance(raw_owner_token, str):
            raise ValueError("owner token must be a string")
        owner_token = _validate_migration_owner_token(raw_owner_token)
    except (KeyError, ValueError) as exc:
        raise _settings_recovery_error(base_dir, "journal owner tokenが不正です。") from exc
    phase = raw.get("phase")
    if phase not in {"preparing", "committing", "committed", "prepared"}:
        raise _settings_recovery_error(base_dir, f"journal phaseが不正です: {phase!r}")
    raw_entries = raw.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise _settings_recovery_error(
            base_dir,
            "journal entriesが空でない配列ではありません。",
        )

    entries: list[_OBSSettingsJournalEntry] = []
    seen: set[str] = set()
    reserved_targets = {
        _filesystem_path_key(base_dir / name) for name in OBS_COPY_SKIP_NAMES
    }
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise _settings_recovery_error(base_dir, "journal entryがobjectではありません。")
        relative_text = raw_entry.get("path")
        if not isinstance(relative_text, str) or not relative_text:
            raise _settings_recovery_error(base_dir, "journal target pathが不正です。")
        parts = tuple(relative_text.split("/"))
        try:
            for part in parts:
                _validate_single_path_component(part)
            _validate_settings_target_components(parts)
        except _UnsafeOBSMigrationPathError as exc:
            raise _settings_recovery_error(
                base_dir,
                f"journal target componentが不正です: {relative_text}",
            ) from exc
        except OBSPathSafetyError as exc:
            raise _settings_recovery_error(base_dir, str(exc)) from exc
        target = _absolute_path(base_dir.joinpath(*parts))
        if _settings_relative_parts(base_dir, target) != parts:
            raise _settings_recovery_error(base_dir, f"journal targetが正規化されていません: {relative_text}")
        if _filesystem_name_key(parts[0]) in OBS_COPY_SKIP_NAME_KEYS:
            raise _settings_recovery_error(
                base_dir,
                f"journal targetがtransaction管理namespaceを参照しています: {target}",
            )
        key = _filesystem_path_key(target)
        if key in seen or key in reserved_targets:
            raise _settings_recovery_error(base_dir, f"journal targetが重複または予約済みです: {target}")
        seen.add(key)
        try:
            original_exists = raw_entry["original_exists"]
            original_size = raw_entry["original_size"]
            original_sha256 = raw_entry["original_sha256"]
            desired_size = raw_entry["desired_size"]
            desired_sha256 = raw_entry["desired_sha256"]
            label = raw_entry["label"]
        except KeyError as exc:
            raise _settings_recovery_error(base_dir, f"journal entry metadataが不正です: {target}") from exc
        def canonical_digest(value: object) -> bool:
            return (
                isinstance(value, str)
                and len(value) == 64
                and all(character in "0123456789abcdef" for character in value)
            )
        if (
            not isinstance(original_exists, bool)
            or isinstance(original_size, bool)
            or not isinstance(original_size, int)
            or isinstance(desired_size, bool)
            or not isinstance(desired_size, int)
            or original_size < 0
            or desired_size < 0
            or original_size > OBS_BOOTSTRAP_CONFIG_MAX_BYTES
            or desired_size > OBS_BOOTSTRAP_CONFIG_MAX_BYTES
            or not canonical_digest(original_sha256)
            or not canonical_digest(desired_sha256)
            or not isinstance(label, str)
            or not label
            or label.strip() != label
            or (
                not original_exists
                and (
                    original_size != 0
                    or original_sha256 != hashlib.sha256(b"").hexdigest()
                )
            )
        ):
            raise _settings_recovery_error(base_dir, f"journal entry metadataが範囲外です: {target}")
        entries.append(
            _OBSSettingsJournalEntry(
                target=target,
                label=label,
                original_exists=original_exists,
                original_size=original_size,
                original_sha256=original_sha256,
                desired_size=desired_size,
                desired_sha256=desired_sha256,
            )
        )
    return _OBSSettingsJournal(
        owner_token=owner_token,
        phase=str(phase),
        entries=tuple(entries),
    )


def _settings_entry_matches(payload: bytes, *, size: int, sha256: str) -> bool:
    actual_size, actual_sha256 = _settings_payload_digest(payload)
    return actual_size == size and actual_sha256 == sha256


def _read_settings_file_or_none(path: Path, *, label: str) -> OBSConfigFileSnapshot:
    return preflight_obs_config_file(
        path,
        label=label,
        max_bytes=OBS_BOOTSTRAP_CONFIG_MAX_BYTES,
    )


def _write_settings_temporary(path: Path, payload: bytes) -> None:
    path = _require_obs_config_write_scope(path)
    root_lease = _active_obs_mutation_root_lease()
    if root_lease is None:
        raise OBSPathSafetyError(
            "設定transaction一時fileに対応する管理OBS root leaseがありません。"
        )
    parent, parent_owned = _directory_for_descendant_parent(root_lease, path)
    try:
        if parent.relative_file_identity_or_none(path.name) is not None:
            raise OBSPathSafetyError(f"設定transaction一時fileが既に存在します: {path}")
        descriptor: int | None = None
        try:
            descriptor = parent.open_file(
                path.name,
                write=True,
                create_exclusive=True,
            )
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            if int(os.fstat(descriptor).st_size) != len(payload):
                raise OBSPathSafetyError(f"設定transaction一時fileのsizeが一致しません: {path}")
        finally:
            if descriptor is not None:
                os.close(descriptor)
        parent.flush_metadata()
    finally:
        if parent_owned:
            parent.close()
    _validate_active_obs_mutation_boundary()


def _settings_temporary_snapshot(
    path: Path,
    *,
    label: str,
    size: int,
    sha256: str,
) -> OBSConfigFileSnapshot | None:
    snapshot = _read_settings_file_or_none(path, label=label)
    if snapshot.payload is None:
        return None
    if not _settings_entry_matches(snapshot.payload, size=size, sha256=sha256):
        raise OBSPathSafetyError(f"所有中の設定transaction一時file内容が一致しません: {path}")
    return snapshot


def _replace_settings_temporary(
    temporary: Path,
    target_snapshot: OBSConfigFileSnapshot,
    *,
    expected_size: int,
    expected_sha256: str,
    expected_temporary_identity: tuple[int, int, int] | None = None,
) -> tuple[int, int, int]:
    target = _require_obs_config_write_scope(target_snapshot.path)
    if temporary.parent != target.parent:
        raise OBSPathSafetyError(f"設定transaction一時fileのparentが一致しません: {temporary}")
    revalidate_obs_config_file(target_snapshot)
    temporary_snapshot = _settings_temporary_snapshot(
        temporary,
        label=f"{target_snapshot.label} transaction temporary",
        size=expected_size,
        sha256=expected_sha256,
    )
    if temporary_snapshot is None or temporary_snapshot.identity is None:
        raise OBSPathSafetyError(f"設定transaction一時fileがありません: {temporary}")
    if (
        expected_temporary_identity is not None
        and temporary_snapshot.identity != expected_temporary_identity
    ):
        raise OBSPathSafetyError(
            f"設定transaction一時file identityがinventory後に変化しました: {temporary}"
        )
    root_lease = _active_obs_mutation_root_lease()
    if root_lease is None:
        raise OBSPathSafetyError(
            "設定transaction確定に対応する管理OBS root leaseがありません。"
        )
    parent, parent_owned = _directory_for_descendant_parent(root_lease, target)
    try:
        current_identity = parent.relative_file_identity_or_none(target.name)
        if current_identity != target_snapshot.identity:
            raise OBSPathSafetyError(f"設定確定直前にtarget identityが変化しました: {target}")
        descriptor = parent.open_file(
            temporary.name,
            write=True,
            create_exclusive=False,
            delete=True,
        )
        try:
            if _file_identity(os.fstat(descriptor)) != temporary_snapshot.identity:
                raise OBSPathSafetyError(f"設定transaction一時file identityが変化しました: {temporary}")
            parent.replace_open_file(descriptor, temporary.name, target.name)
        finally:
            os.close(descriptor)
    finally:
        if parent_owned:
            parent.close()
    _validate_active_obs_mutation_boundary()
    return temporary_snapshot.identity


def _validate_desired_settings_targets(
    writes: tuple[OBSConfigPlannedWrite, ...],
    expected_identities: dict[str, tuple[int, int, int]],
) -> None:
    """Reopen every planned target and prove desired bytes and identity."""

    for write in writes:
        current = _read_settings_file_or_none(
            write.snapshot.path,
            label=f"{write.snapshot.label} committed target",
        )
        expected_identity = expected_identities.get(
            _filesystem_path_key(write.snapshot.path)
        )
        desired_size, desired_sha256 = _settings_payload_digest(write.payload)
        if (
            current.payload is None
            or current.identity is None
            or expected_identity is None
            or current.identity != expected_identity
            or not _settings_entry_matches(
                current.payload,
                size=desired_size,
                sha256=desired_sha256,
            )
        ):
            raise OBSPathSafetyError(
                "committed phase前にtargetのdesired payload／identityを確認できません: "
                f"{write.snapshot.path}"
            )


def _settings_recovery_temporary_snapshot(
    base_dir: Path,
    path: Path,
    *,
    label: str,
    size: int,
    sha256: str,
    identities: dict[str, tuple[int, int, int]],
) -> OBSConfigFileSnapshot | None:
    snapshot = _settings_temporary_snapshot(
        path,
        label=label,
        size=size,
        sha256=sha256,
    )
    if snapshot is None:
        return None
    expected_identity = identities.get(_filesystem_path_key(path))
    if expected_identity is None or snapshot.identity != expected_identity:
        raise _settings_recovery_error(
            base_dir,
            f"recovery inventory後にtransaction一時file identityが変化しました: {path}",
        )
    return snapshot


def _delete_owned_settings_recovery_temporary(
    base_dir: Path,
    path: Path,
    *,
    label: str,
    identities: dict[str, tuple[int, int, int]],
) -> None:
    """Delete an owned temporary even when a hard crash left partial bytes."""

    snapshot = preflight_obs_config_file(
        path,
        label=label,
        max_bytes=OBS_BOOTSTRAP_CONFIG_MAX_BYTES,
    )
    if snapshot.payload is None:
        return
    expected_identity = identities.get(_filesystem_path_key(path))
    if expected_identity is None or snapshot.identity != expected_identity:
        raise _settings_recovery_error(
            base_dir,
            f"recovery inventory外またはidentity変更後の一時fileは削除しません: {path}",
        )
    delete_preflighted_obs_config_file(snapshot)


def _settings_temp_paths(target: Path, owner_token: str) -> tuple[Path, Path]:
    return (
        _transaction_copy_temporary_path(target, owner_token),
        _transaction_write_temporary_path(target, owner_token),
    )


def _write_settings_journal(
    base_dir: Path,
    owner_token: str,
    phase: str,
    writes: tuple[OBSConfigPlannedWrite, ...],
) -> None:
    marker = get_obs_settings_transaction_marker(base_dir)
    snapshot = preflight_obs_config_file(
        marker,
        label="OBS設定transaction journal",
        max_bytes=OBS_SETTINGS_TRANSACTION_MAX_BYTES,
    )
    if phase == "preparing" and snapshot.payload is not None:
        raise OBSPathSafetyError(f"既存のOBS設定transaction journalがあります: {marker}")
    if phase in {"committing", "committed"} and snapshot.payload is None:
        raise OBSPathSafetyError(f"更新対象のOBS設定transaction journalがありません: {marker}")
    write_preflighted_obs_config_file(
        snapshot,
        _settings_journal_payload(base_dir, owner_token, phase, writes),
    )


def _recover_settings_journal_locked(
    base_dir: Path,
    marker_snapshot: OBSConfigFileSnapshot,
    journal: _OBSSettingsJournal,
    temporary_identities: dict[str, tuple[int, int, int]],
) -> None:
    marker = get_obs_settings_transaction_marker(base_dir)
    target_keys = {
        _filesystem_path_key(marker),
        _filesystem_path_key(
            _transaction_write_temporary_path(marker, journal.owner_token)
        ),
        *(_filesystem_path_key(entry.target) for entry in journal.entries),
    }
    for entry in journal.entries:
        backup, desired = _settings_temp_paths(entry.target, journal.owner_token)
        target_keys.add(_filesystem_path_key(backup))
        target_keys.add(_filesystem_path_key(desired))
    owner_token = _ACTIVE_OBS_SETTINGS_TRANSACTION_OWNER.set(journal.owner_token)
    targets_token = _ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.set(frozenset(target_keys))
    try:
        for entry in reversed(journal.entries):
            backup, desired = _settings_temp_paths(entry.target, journal.owner_token)
            if journal.phase == "preparing":
                if entry.original_exists:
                    _delete_owned_settings_recovery_temporary(
                        base_dir,
                        backup,
                        label=f"{entry.label} preparing backup",
                        identities=temporary_identities,
                    )
                _delete_owned_settings_recovery_temporary(
                    base_dir,
                    desired,
                    label=f"{entry.label} preparing desired temporary",
                    identities=temporary_identities,
                )
                continue

            current = _read_settings_file_or_none(entry.target, label=entry.label)
            if journal.phase == "committed":
                if current.payload is None or not _settings_entry_matches(
                    current.payload,
                    size=entry.desired_size,
                    sha256=entry.desired_sha256,
                ):
                    raise _settings_recovery_error(
                        base_dir,
                        f"commit済みtargetが計画payloadと一致しません: {entry.target}",
                    )
            elif entry.original_exists:
                if current.payload is not None and _settings_entry_matches(
                    current.payload,
                    size=entry.original_size,
                    sha256=entry.original_sha256,
                ):
                    pass
                elif current.payload is not None and _settings_entry_matches(
                    current.payload,
                    size=entry.desired_size,
                    sha256=entry.desired_sha256,
                ):
                    backup_snapshot = _settings_recovery_temporary_snapshot(
                        base_dir,
                        backup,
                        label=f"{entry.label} rollback backup",
                        size=entry.original_size,
                        sha256=entry.original_sha256,
                        identities=temporary_identities,
                    )
                    if backup_snapshot is None:
                        raise _settings_recovery_error(
                            base_dir,
                            f"rollback backupがありません: {backup}",
                        )
                    _replace_settings_temporary(
                        backup,
                        current,
                        expected_size=entry.original_size,
                        expected_sha256=entry.original_sha256,
                        expected_temporary_identity=backup_snapshot.identity,
                    )
                    current = _read_settings_file_or_none(entry.target, label=entry.label)
                else:
                    raise _settings_recovery_error(
                        base_dir,
                        f"targetがoriginal／desiredのどちらとも一致しません: {entry.target}",
                    )
                if current.payload is None or not _settings_entry_matches(
                    current.payload,
                    size=entry.original_size,
                    sha256=entry.original_sha256,
                ):
                    raise _settings_recovery_error(base_dir, f"rollback結果を確認できません: {entry.target}")
            else:
                if current.payload is not None:
                    if not _settings_entry_matches(
                        current.payload,
                        size=entry.desired_size,
                        sha256=entry.desired_sha256,
                    ):
                        raise _settings_recovery_error(
                            base_dir,
                            f"新規targetがdesired payloadと一致しません: {entry.target}",
                        )
                    delete_preflighted_obs_config_file(current)
                current = _read_settings_file_or_none(entry.target, label=entry.label)
                if current.payload is not None:
                    raise _settings_recovery_error(base_dir, f"新規targetをrollbackできません: {entry.target}")

            if entry.original_exists:
                _delete_owned_settings_recovery_temporary(
                    base_dir,
                    backup,
                    label=f"{entry.label} rollback backup",
                    identities=temporary_identities,
                )
            _delete_owned_settings_recovery_temporary(
                base_dir,
                desired,
                label=f"{entry.label} desired temporary",
                identities=temporary_identities,
            )

        current_marker = preflight_obs_config_file(
            marker,
            label="OBS設定transaction journal",
            max_bytes=OBS_SETTINGS_TRANSACTION_MAX_BYTES,
        )
        if current_marker.payload != marker_snapshot.payload:
            # A phase update may have completed while its temporary cleanup was
            # interrupted. The authoritative marker must still match the phase
            # selected by the caller before target recovery begins.
            parsed = _parse_settings_journal(base_dir, current_marker.payload or b"")
            if parsed != journal:
                raise _settings_recovery_error(base_dir, "復旧中にjournal内容が変化しました。")
        delete_preflighted_obs_config_file(current_marker)
    finally:
        _ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.reset(targets_token)
        _ACTIVE_OBS_SETTINGS_TRANSACTION_OWNER.reset(owner_token)


def _root_transaction_temporaries(
    base_dir: Path,
    *,
    directory_lease: _OBSDirectoryLease | None = None,
) -> tuple[_OBSTransactionTemporaryDescriptor, ...]:
    """List journal temporaries relevant before a durable marker exists."""

    if directory_lease is not None:
        if directory_lease.path != _absolute_path(base_dir):
            raise OBSPathSafetyError(
                "transaction temporary inventoryのroot leaseが一致しません: "
                f"{directory_lease.path} != {base_dir}"
            )
        directory_lease.validate_lexical_binding()
        temporaries = tuple(
            descriptor
            for descriptor, _identity in _list_root_journal_temporaries(
                directory_lease
            )
        )
        directory_lease.validate_lexical_binding()
        return temporaries

    temporaries: list[_OBSTransactionTemporaryDescriptor] = []
    settings_marker = get_obs_settings_transaction_marker(base_dir)
    settings_prefix = _filesystem_name_key(f".{settings_marker.name}.")
    migration_marker = get_obs_copy_in_progress_marker(base_dir)
    migration_prefix = _filesystem_name_key(f"{migration_marker.name}.")
    for entry in list_safe_obs_config_directory(base_dir):
        path = base_dir / entry.name
        name_key = _filesystem_name_key(entry.name)
        if not (
            (
                name_key.startswith(settings_prefix)
                and name_key.endswith(".write.tmp")
            )
            or (
                name_key.startswith(migration_prefix)
                and name_key.endswith(".tmp")
            )
        ):
            continue
        parsed = _parse_transaction_temporary(path)
        if parsed is None:
            continue
        if entry.kind != "file":
            raise _settings_recovery_error(
                base_dir,
                f"transaction一時pathが通常fileではありません: {path}",
            )
        temporaries.append(parsed)
    return tuple(temporaries)


def has_pending_obs_settings_transaction(base_dir: str | Path) -> bool:
    """Report only recoverable settings state, without conflating copy migration."""

    base_path = _absolute_path(base_dir)
    if _path_lexists(get_obs_settings_transaction_marker(base_path)):
        return True
    if not _path_lexists(base_path):
        return False
    return any(
        descriptor.kind == OBS_TRANSACTION_TEMP_WRITE
        and _filesystem_name_key(descriptor.target_name)
        == _filesystem_name_key(OBS_SETTINGS_TRANSACTION_MARKER_NAME)
        for descriptor in _root_transaction_temporaries(base_path)
    )


def has_pending_obs_copy_transaction(base_dir: str | Path) -> bool:
    """Check copy marker/owner state without scanning unrelated OBS contents."""

    base_path = _absolute_path(base_dir)
    marker = get_obs_copy_in_progress_marker(base_path)
    lock_path = get_obs_copy_lock_path(base_path)
    try:
        with _OBSDirectoryLease.open_absolute(base_path) as probe:
            if probe.relative_file_identity_or_none(marker.name) is not None:
                return True
            root_temporaries = _root_transaction_temporaries(
                base_path,
                directory_lease=probe,
            )
            if any(
                descriptor.kind == OBS_TRANSACTION_TEMP_JOURNAL
                and _filesystem_name_key(descriptor.target_name)
                == _filesystem_name_key(marker.name)
                for descriptor in root_temporaries
            ):
                return True
            if probe.relative_file_identity_or_none(lock_path.name) is None:
                return False
            lock = _OBSInterProcessLock(lock_path)
            try:
                if not lock.acquire(
                    create_parent=False,
                    directory_lease=probe,
                    initialize_empty=False,
                ):
                    # The shared lock also protects settings transactions.
                    # Without a copy marker/pre-journal, lock contention alone
                    # is generic mutation activity rather than copy state.
                    return False
                locked_root = lock.directory_lease
                if locked_root.relative_file_identity_or_none(marker.name) is not None:
                    return True
                return any(
                    descriptor.kind == OBS_TRANSACTION_TEMP_JOURNAL
                    and _filesystem_name_key(descriptor.target_name)
                    == _filesystem_name_key(marker.name)
                    for descriptor in _root_transaction_temporaries(
                        base_path,
                        directory_lease=locked_root,
                    )
                )
            finally:
                lock.release()
    except FileNotFoundError:
        return False
    except (OSError, _UnsafeOBSMigrationPathError):
        return True


def _recover_orphaned_settings_journal_temporary(base_dir: Path) -> bool:
    """Remove the sole pre-journal temporary left before any target mutation."""

    root = _active_obs_mutation_root_lease()
    if root is None:
        raise OBSPathSafetyError(
            "orphan journal temporary復旧に対応する管理OBS root leaseがありません。"
        )
    root_temporaries = _root_transaction_temporaries(
        base_dir,
        directory_lease=root,
    )
    try:
        all_temporaries = _list_root_transaction_temporaries(
            root,
            strict_names=False,
        )
    except (OSError, _UnsafeOBSMigrationPathError) as exc:
        raise _settings_recovery_error(
            base_dir,
            f"orphan journal temporaryのinventoryを検査できません: {exc}",
        ) from exc
    if not root_temporaries:
        if all_temporaries:
            raise _settings_recovery_error(
                base_dir,
                "markerなしでdata transaction一時fileが残っています。",
            )
        return False
    marker = get_obs_settings_transaction_marker(base_dir)
    expected_target_key = _filesystem_name_key(marker.name)
    candidates = tuple(
        descriptor
        for descriptor in root_temporaries
        if descriptor.kind == OBS_TRANSACTION_TEMP_WRITE
        and _filesystem_name_key(descriptor.target_name) == expected_target_key
        and descriptor.path.parent == base_dir
    )
    if len(root_temporaries) != 1 or len(candidates) != 1:
        raise _settings_recovery_error(
            base_dir,
            "markerなしで別用途または複数のtransaction一時fileが残っています: "
            + ", ".join(str(descriptor.path) for descriptor in root_temporaries),
        )

    candidate = candidates[0]
    if (
        len(all_temporaries) != 1
        or _filesystem_path_key(all_temporaries[0][0].path)
        != _filesystem_path_key(candidate.path)
    ):
        raise _settings_recovery_error(
            base_dir,
            "markerなしでdata transaction一時fileが残っています。",
        )

    snapshot = preflight_obs_config_file(
        candidate.path,
        label="orphan OBS設定transaction journal temporary",
        max_bytes=OBS_SETTINGS_TRANSACTION_MAX_BYTES,
    )
    if snapshot.payload is None:
        raise _settings_recovery_error(
            base_dir,
            f"検査中にorphan journal temporaryが消失しました: {candidate.path}",
        )
    targets_token = _ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.set(
        frozenset({_filesystem_path_key(candidate.path)})
    )
    try:
        delete_preflighted_obs_config_file(snapshot)
    finally:
        _ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.reset(targets_token)
    return True


def _preflight_settings_recovery_temporaries(
    base_dir: Path,
    journal: _OBSSettingsJournal,
) -> dict[str, OBSConfigFileSnapshot]:
    """Reject foreign temporaries and capture an interrupted journal update."""

    marker_temporary = _transaction_write_temporary_path(
        get_obs_settings_transaction_marker(base_dir),
        journal.owner_token,
    )
    expected: dict[str, str] = {
        _filesystem_path_key(marker_temporary): OBS_TRANSACTION_TEMP_WRITE,
    }
    for entry in journal.entries:
        backup, desired = _settings_temp_paths(entry.target, journal.owner_token)
        if entry.original_exists:
            expected[_filesystem_path_key(backup)] = OBS_TRANSACTION_TEMP_COPY
        expected[_filesystem_path_key(desired)] = OBS_TRANSACTION_TEMP_WRITE

    root = _active_obs_mutation_root_lease()
    if root is None:
        raise OBSPathSafetyError(
            "設定transaction temporary復旧に対応する管理OBS root leaseがありません。"
        )
    try:
        temporaries = _list_root_transaction_temporaries(
            root,
            strict_names=False,
        )
    except (OSError, _UnsafeOBSMigrationPathError) as exc:
        raise _settings_recovery_error(
            base_dir,
            f"設定transaction temporary inventoryを検査できません: {exc}",
        ) from exc
    snapshots: dict[str, OBSConfigFileSnapshot] = {}
    seen: set[str] = set()
    for descriptor, identity in temporaries:
        key = _filesystem_path_key(descriptor.path)
        if (
            descriptor.owner_token != journal.owner_token
            or expected.get(key) != descriptor.kind
            or key in seen
        ):
            raise _settings_recovery_error(
                base_dir,
                f"journal所有外のtransaction一時fileがあります: {descriptor.path}",
            )
        seen.add(key)
        snapshot = preflight_obs_config_file(
            descriptor.path,
            label=(
                "interrupted OBS設定transaction journal update"
                if key == _filesystem_path_key(marker_temporary)
                else "OBS設定transaction temporary"
            ),
            max_bytes=(
                OBS_SETTINGS_TRANSACTION_MAX_BYTES
                if key == _filesystem_path_key(marker_temporary)
                else OBS_BOOTSTRAP_CONFIG_MAX_BYTES
            ),
        )
        if snapshot.payload is None or snapshot.identity != identity:
            raise _settings_recovery_error(
                base_dir,
                f"検査中にtransaction一時fileが消失または置換されました: {descriptor.path}",
            )
        snapshots[key] = snapshot
    return snapshots


def _preflight_settings_recovery_targets(
    base_dir: Path,
    journal: _OBSSettingsJournal,
    temporary_snapshots: dict[str, OBSConfigFileSnapshot],
) -> tuple[OBSConfigFileSnapshot, ...]:
    """Prove recovery feasibility before any managed OBS process is stopped."""

    targets: list[OBSConfigFileSnapshot] = []
    for entry in journal.entries:
        current = _read_settings_file_or_none(entry.target, label=entry.label)
        targets.append(current)
        if journal.phase == "preparing":
            continue
        current_is_original = bool(
            current.payload is not None
            and _settings_entry_matches(
                current.payload,
                size=entry.original_size,
                sha256=entry.original_sha256,
            )
        )
        current_is_desired = bool(
            current.payload is not None
            and _settings_entry_matches(
                current.payload,
                size=entry.desired_size,
                sha256=entry.desired_sha256,
            )
        )
        if journal.phase == "committed":
            if not current_is_desired:
                raise _settings_recovery_error(
                    base_dir,
                    f"commit済みtargetが計画payloadと一致しません: {entry.target}",
                )
            continue
        if entry.original_exists:
            if current_is_original:
                continue
            if not current_is_desired:
                raise _settings_recovery_error(
                    base_dir,
                    f"targetがoriginal／desiredのどちらとも一致しません: {entry.target}",
                )
            backup, _desired = _settings_temp_paths(entry.target, journal.owner_token)
            backup_snapshot = temporary_snapshots.get(_filesystem_path_key(backup))
            if (
                backup_snapshot is None
                or backup_snapshot.payload is None
                or not _settings_entry_matches(
                    backup_snapshot.payload,
                    size=entry.original_size,
                    sha256=entry.original_sha256,
                )
            ):
                raise _settings_recovery_error(
                    base_dir,
                    f"rollbackに必要な完全backupがありません: {backup}",
                )
        elif current.payload is not None and not current_is_desired:
            raise _settings_recovery_error(
                base_dir,
                f"新規targetがdesired payloadと一致しません: {entry.target}",
            )
    return tuple(targets)


def recover_obs_settings_transaction(
    base_dir: str | Path,
    *,
    before_recovery: Callable[[], None] | None = None,
) -> bool:
    """Rollback a prepared transaction or finish cleanup of a committed one."""

    base_path = _absolute_path(base_dir)
    scope = _ACTIVE_OBS_BOOTSTRAP_MUTATION.get()
    if scope is None or scope.base_dir != base_path:
        raise OBSPathSafetyError(
            "OBS設定transactionの復旧には対応するmutation guardが必要です。"
        )
    _validate_active_obs_mutation_boundary()
    marker = get_obs_settings_transaction_marker(base_path)
    marker_snapshot = preflight_obs_config_file(
        marker,
        label="OBS設定transaction journal",
        max_bytes=OBS_SETTINGS_TRANSACTION_MAX_BYTES,
    )
    if marker_snapshot.payload is None:
        return _recover_orphaned_settings_journal_temporary(base_path)
    journal = _parse_settings_journal(base_path, marker_snapshot.payload)
    try:
        temporary_snapshots = _preflight_settings_recovery_temporaries(
            base_path,
            journal,
        )
        target_snapshots = _preflight_settings_recovery_targets(
            base_path,
            journal,
            temporary_snapshots,
        )
        if journal.phase != "preparing":
            if before_recovery is not None:
                before_recovery()
            else:
                try:
                    managed_process_running = OBSProcessManager(
                        base_path
                    ).has_managed_process()
                except Exception as exc:
                    raise _settings_recovery_error(
                        base_path,
                        f"復旧前に管理対象OBSの稼働状態を確認できません: {exc}",
                    ) from exc
                if managed_process_running:
                    raise _settings_recovery_error(
                        base_path,
                        "管理対象OBSが稼働中です。録画監視を停止してOBSを終了してから再試行してください。",
                    )
        revalidate_obs_config_file(marker_snapshot)
        recovery_snapshots: tuple[OBSConfigFileSnapshot, ...]
        if journal.phase == "preparing":
            recovery_snapshots = tuple(temporary_snapshots.values())
        else:
            recovery_snapshots = (*target_snapshots, *temporary_snapshots.values())
        for snapshot in recovery_snapshots:
            revalidate_obs_config_file(snapshot)
        temporary_identities = {
            key: snapshot.identity
            for key, snapshot in temporary_snapshots.items()
            if snapshot.identity is not None
        }
        _recover_settings_journal_locked(
            base_path,
            marker_snapshot,
            journal,
            temporary_identities,
        )
        marker_temporary_key = _filesystem_path_key(
            _transaction_write_temporary_path(marker, journal.owner_token)
        )
        interrupted_journal_update = temporary_snapshots.get(marker_temporary_key)
        if interrupted_journal_update is not None:
            targets_token = _ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.set(
                frozenset({marker_temporary_key})
            )
            try:
                delete_preflighted_obs_config_file(interrupted_journal_update)
            finally:
                _ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.reset(targets_token)
    except OBSSettingsRecoveryRequiredError:
        raise
    except Exception as exc:
        raise _settings_recovery_error(base_path, str(exc)) from exc
    _validate_active_obs_mutation_boundary()
    return True


def execute_obs_config_transaction(
    plan: OBSConfigTransactionPlan,
    *,
    before_commit: Callable[[], None] | None = None,
    validate_plan: Callable[[], None] | None = None,
    run_before_commit_on_noop: bool = False,
) -> tuple[Path, ...]:
    """Prepare and atomically commit one recoverable multi-file OBS update."""

    with obs_config_mutation_guard(
        plan.base_dir,
        before_settings_recovery=before_commit,
    ):
        (
            base_dir,
            directories,
            planned_writes,
            writes,
        ) = _validated_settings_transaction_plan(plan)

        def validate_transaction_state() -> None:
            _validate_active_obs_mutation_boundary()
            if validate_plan is not None:
                validate_plan()
            for planned_write in planned_writes:
                revalidate_obs_config_file(planned_write.snapshot)
            _validate_active_obs_mutation_boundary()

        validate_transaction_state()

        if not writes:
            if run_before_commit_on_noop and before_commit is not None:
                before_commit()
            validate_transaction_state()
            return ()

        for directory in directories:
            ensure_safe_obs_config_directory(directory)
        for write in writes:
            ensure_safe_obs_config_directory(write.snapshot.path.parent)
        validate_transaction_state()

        migration_capability = _ACTIVE_OBS_MIGRATION_CAPABILITY.get()
        if migration_capability is not None:
            if before_commit is not None:
                before_commit()
            validate_transaction_state()
            planned_token = _ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.set(
                frozenset(_filesystem_path_key(write.snapshot.path) for write in writes)
            )
            try:
                for write in writes:
                    write_preflighted_obs_config_file(write.snapshot, write.payload)
            finally:
                _ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.reset(planned_token)
            return tuple(write.snapshot.path for write in writes)

        owner = uuid.uuid4().hex
        marker = get_obs_settings_transaction_marker(base_dir)
        target_keys = {
            _filesystem_path_key(marker),
            *(_filesystem_path_key(write.snapshot.path) for write in writes),
        }
        owner_token = _ACTIVE_OBS_SETTINGS_TRANSACTION_OWNER.set(owner)
        targets_token = _ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.set(frozenset(target_keys))
        try:
            _write_settings_journal(base_dir, owner, "preparing", writes)
            try:
                for write in writes:
                    original = write.snapshot.payload
                    backup, desired = _settings_temp_paths(write.snapshot.path, owner)
                    if original is not None:
                        _write_settings_temporary(backup, original)
                    _write_settings_temporary(desired, write.payload)

                # Reopen every durable temporary and verify the exact bytes
                # before stopping OBS. A disk/ACL failure must not stop a
                # healthy process without proving that commit can proceed.
                prepared_desired_identities: dict[str, tuple[int, int, int]] = {}
                for write in writes:
                    backup, desired = _settings_temp_paths(write.snapshot.path, owner)
                    if write.snapshot.payload is not None:
                        original_size, original_sha256 = _settings_payload_digest(
                            write.snapshot.payload
                        )
                        if (
                            _settings_temporary_snapshot(
                                backup,
                                label=f"{write.snapshot.label} preparing backup",
                                size=original_size,
                                sha256=original_sha256,
                            )
                            is None
                        ):
                            raise OBSPathSafetyError(
                                f"停止前にrollback backupを再検証できません: {backup}"
                            )
                    desired_size, desired_sha256 = _settings_payload_digest(
                        write.payload
                    )
                    desired_snapshot = _settings_temporary_snapshot(
                        desired,
                        label=f"{write.snapshot.label} preparing desired",
                        size=desired_size,
                        sha256=desired_sha256,
                    )
                    if desired_snapshot is None or desired_snapshot.identity is None:
                        raise OBSPathSafetyError(
                            f"停止前にdesired temporaryを再検証できません: {desired}"
                        )
                    prepared_desired_identities[
                        _filesystem_path_key(desired)
                    ] = desired_snapshot.identity

                validate_transaction_state()

                if before_commit is not None:
                    before_commit()
                # OBS shutdown may flush its own INI files. In the preparing
                # phase no target has been replaced, so a mismatch can discard
                # owned temporaries without touching the flushed target.
                validate_transaction_state()

                _write_settings_journal(base_dir, owner, "committing", writes)
                validate_transaction_state()

                committed_identities = {
                    _filesystem_path_key(write.snapshot.path): write.snapshot.identity
                    for write in planned_writes
                    if not write.changed and write.snapshot.identity is not None
                }
                for write in writes:
                    desired_size, desired_sha256 = _settings_payload_digest(write.payload)
                    _backup, desired = _settings_temp_paths(write.snapshot.path, owner)
                    committed_identities[_filesystem_path_key(write.snapshot.path)] = (
                        _replace_settings_temporary(
                            desired,
                            write.snapshot,
                            expected_size=desired_size,
                            expected_sha256=desired_sha256,
                            expected_temporary_identity=(
                                prepared_desired_identities[
                                    _filesystem_path_key(desired)
                                ]
                            ),
                        )
                    )
                _validate_desired_settings_targets(
                    planned_writes,
                    committed_identities,
                )
                try:
                    _write_settings_journal(base_dir, owner, "committed", writes)
                except Exception as exc:
                    raise _OBSSettingsCommitDurabilityUncertainError(
                        "committed journalの永続化結果が不明なため、"
                        "backupとmarkerを保持して次回復旧へ委ねます。"
                    ) from exc
                recover_obs_settings_transaction(
                    base_dir,
                    before_recovery=lambda: None,
                )
            except _OBSSettingsCommitDurabilityUncertainError as exc:
                raise _settings_recovery_error(base_dir, str(exc)) from exc
            except Exception as exc:
                try:
                    recover_obs_settings_transaction(
                        base_dir,
                        before_recovery=lambda: None,
                    )
                except OBSSettingsRecoveryRequiredError:
                    raise
                except Exception as recovery_exc:
                    raise _settings_recovery_error(base_dir, str(recovery_exc)) from exc
                raise
        finally:
            _ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.reset(targets_token)
            _ACTIVE_OBS_SETTINGS_TRANSACTION_OWNER.reset(owner_token)

        return tuple(write.snapshot.path for write in writes)


def _write_obs_migration_journal(
    marker: Path,
    source: Path,
    source_fingerprint: str,
    owner_token: str,
    *,
    phase: str = OBS_MIGRATION_PHASE_COPYING,
    directory_lease: _OBSDirectoryLease | None = None,
    validate_transaction: Callable[[], None] | None = None,
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
    temporary = _transaction_journal_temporary_path(marker, owner_token)
    directory_owned = directory_lease is None
    directory = directory_lease or _OBSDirectoryLease.open_absolute(
        marker.parent,
        mutable=True,
    )
    descriptor: int | None = None
    temporary_identity: tuple[int, int, int] | None = None
    try:
        if directory.path != _absolute_path(marker.parent):
            raise _UnsafeOBSMigrationPathError(
                "journal directory leaseがmarker parentと一致しません: "
                f"{directory.path} != {marker.parent}"
            )
        marker_before = directory.relative_file_identity_or_none(marker.name)
        descriptor = directory.open_file(
            temporary.name,
            write=True,
            create_exclusive=True,
        )
        temporary_stat = os.fstat(descriptor)
        temporary_identity = _file_identity(temporary_stat)
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
        if directory._relative_file_identity(temporary.name) != temporary_identity:
            raise _UnsafeOBSMigrationPathError(
                f"journal一時fileのidentityが変化しました: {temporary}"
            )
        os.close(descriptor)
        descriptor = None
        descriptor = directory.open_file(
            temporary.name,
            write=True,
            create_exclusive=False,
            delete=True,
        )
        if _file_identity(os.fstat(descriptor)) != temporary_identity:
            raise _UnsafeOBSMigrationPathError(
                f"journal一時fileのreopen中にidentityが変化しました: {temporary}"
            )
        if directory.relative_file_identity_or_none(marker.name) != marker_before:
            raise _UnsafeOBSMigrationPathError(
                f"journal更新前にmarker identityが変化しました: {marker}"
            )
        if validate_transaction is not None:
            validate_transaction()
        directory.replace_open_file(descriptor, temporary.name, marker.name)
        if directory._relative_file_identity(marker.name) != temporary_identity:
            raise _UnsafeOBSMigrationPathError(f"marker確定時にidentityが変化しました: {marker}")
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            try:
                if (
                    temporary_identity is not None
                    and directory.relative_file_identity_or_none(temporary.name)
                    is not None
                ):
                    try:
                        directory.unlink_file(
                            temporary.name,
                            expected_identity=temporary_identity,
                        )
                    except _UnsafeOBSMigrationPathError:
                        pass
            finally:
                if directory_owned:
                    directory.close()


def _write_safe_file_bytes_with_migration_lease(
    path: Path,
    payload: bytes,
    root_lease: _OBSDirectoryLease,
    *,
    expected_snapshot: OBSConfigFileSnapshot | None,
) -> None:
    temporary = _safe_write_temporary_path(path)
    parent, parent_owned = _directory_for_descendant_parent(root_lease, path)
    descriptor: int | None = None
    temporary_identity: tuple[int, int, int] | None = None
    try:
        destination_before = parent.relative_file_identity_or_none(path.name)
        descriptor = parent.open_file(
            temporary.name,
            write=True,
            create_exclusive=True,
        )
        temporary_identity = _file_identity(os.fstat(descriptor))
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        if int(os.fstat(descriptor).st_size) != len(payload):
            raise _UnsafeOBSMigrationPathError(
                f"一時fileのsizeが一致しません: {temporary}"
            )
        if expected_snapshot is not None:
            revalidate_obs_config_file(expected_snapshot)
        if parent.relative_file_identity_or_none(path.name) != destination_before:
            raise _UnsafeOBSMigrationPathError(
                f"書き込み前にfile identityが変化しました: {path}"
            )
        if parent._relative_file_identity(temporary.name) != temporary_identity:
            raise _UnsafeOBSMigrationPathError(
                f"一時fileのidentityが変化しました: {temporary}"
            )
        os.close(descriptor)
        descriptor = None
        descriptor = parent.open_file(
            temporary.name,
            write=True,
            create_exclusive=False,
            delete=True,
        )
        if _file_identity(os.fstat(descriptor)) != temporary_identity:
            raise _UnsafeOBSMigrationPathError(
                f"一時fileのreopen中にidentityが変化しました: {temporary}"
            )
        validator = _ACTIVE_OBS_MIGRATION_VALIDATOR.get()
        if validator is not None:
            validator()
        parent.replace_open_file(descriptor, temporary.name, path.name)
        if parent._relative_file_identity(path.name) != temporary_identity:
            raise _UnsafeOBSMigrationPathError(
                f"file確定時にidentityが変化しました: {path}"
            )
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            try:
                if (
                    temporary_identity is not None
                    and parent.relative_file_identity_or_none(temporary.name) is not None
                ):
                    try:
                        parent.unlink_file(
                            temporary.name,
                            expected_identity=temporary_identity,
                        )
                    except _UnsafeOBSMigrationPathError:
                        pass
            finally:
                if parent_owned:
                    parent.close()


def _remove_obs_migration_journal_if_owned(
    marker: Path,
    owner_token: str,
    *,
    directory_lease: _OBSDirectoryLease | None = None,
    validate_transaction: Callable[[], None] | None = None,
) -> None:
    directory_owned = directory_lease is None
    directory = directory_lease or _OBSDirectoryLease.open_absolute(
        marker.parent,
        mutable=True,
    )
    try:
        if directory.path != _absolute_path(marker.parent):
            raise _UnsafeOBSMigrationPathError(
                f"journal directory leaseがmarker parentと一致しません: {directory.path} != {marker.parent}"
            )
        raw, identity = _read_safe_relative_file_bytes(
            directory,
            marker.name,
            max_bytes=OBS_COPY_JOURNAL_MAX_BYTES,
            label="marker",
        )
        payload = json.loads(raw.decode("utf-8"))
        journal = _read_obs_migration_journal(
            marker,
            directory_lease=directory,
        )
        if journal.owner_token != owner_token or payload.get("owner_token") != owner_token:
            raise _migration_recovery_error(
                marker.parent,
                "markerの所有者が変化したため解除しません。",
            )
        if directory._relative_file_identity(marker.name) != identity:
            raise _migration_recovery_error(
                marker.parent,
                "markerのidentityが変化したため解除しません。",
            )
        if validate_transaction is not None:
            validate_transaction()
        directory.unlink_file(marker.name, expected_identity=identity)
    except OBSMigrationError:
        raise
    except Exception as exc:
        raise _migration_recovery_error(
            marker.parent,
            f"OBSのコピー中markerを再検証できません: {exc}",
        ) from exc
    finally:
        if directory_owned:
            directory.close()


@dataclass(frozen=True)
class _OBSBootstrapMutationScope:
    base_dir: Path
    lock: _OBSInterProcessLock | None
    migration_owner_token: str | None = None


def _validate_migration_finalize_capability(base_dir: Path, owner_token: str) -> None:
    owner_token = _validate_migration_owner_token(owner_token)
    marker = get_obs_copy_in_progress_marker(base_dir)
    directory = _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.get()
    if directory is None or directory.path != _absolute_path(base_dir):
        raise _migration_recovery_error(
            base_dir,
            "最終化権限に対応するdestination directory leaseがありません。",
        )
    if directory.relative_file_identity_or_none(marker.name) is None:
        raise _migration_recovery_error(base_dir, "最終化権限に対応するコピー中markerがありません。")
    journal = _read_obs_migration_journal(marker, directory_lease=directory)
    if journal.phase != OBS_MIGRATION_PHASE_FINALIZE_PENDING:
        raise _migration_recovery_error(base_dir, "コピー中markerが最終化待ちphaseではありません。")
    if journal.owner_token != owner_token:
        raise _migration_recovery_error(base_dir, "コピー中markerのowner tokenが最終化権限と一致しません。")


@contextmanager
def _obs_bootstrap_mutation_guard(
    base_dir: Path,
    *,
    before_settings_recovery: Callable[[], None] | None = None,
):
    base_path = _absolute_path(base_dir)
    active_scope = _ACTIVE_OBS_BOOTSTRAP_MUTATION.get()
    if active_scope is not None:
        if active_scope.base_dir != base_path:
            raise OBSPathSafetyError(
                "OBS設定更新中に別の管理destinationへnested writeしようとしました: "
                f"{active_scope.base_dir} -> {base_path}"
            )
        _validate_active_obs_mutation_boundary()
        yield
        _validate_active_obs_mutation_boundary()
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
            _validate_active_obs_mutation_boundary()
            yield
            _validate_active_obs_mutation_boundary()
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
        locked_root = lock.directory_lease
        locked_root.validate_lexical_binding()
        marker_identity = locked_root.relative_file_identity_or_none(marker.name)
        if marker_identity is not None:
            journal = _read_obs_migration_journal(
                marker,
                directory_lease=locked_root,
                expected_identity=marker_identity,
            )
            raise _migration_recovery_error(
                base_path,
                "コピー中markerが残っているため、通常の起動前設定更新は行いません。"
                f" phase={journal.phase}",
            )
        scope = _OBSBootstrapMutationScope(base_path, lock)
        scope_token = _ACTIVE_OBS_BOOTSTRAP_MUTATION.set(scope)
        _validate_active_obs_mutation_boundary()
        recover_obs_settings_transaction(
            base_path,
            before_recovery=before_settings_recovery,
        )
        _validate_active_obs_mutation_boundary()
        yield
        _validate_active_obs_mutation_boundary()
    finally:
        if scope_token is not None:
            _ACTIVE_OBS_BOOTSTRAP_MUTATION.reset(scope_token)
        lock.release()


def _guard_obs_bootstrap_mutation(
    *,
    stop_managed_before_recovery: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(method: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(method)
        def guarded(bootstrapper, *args, **kwargs):
            before_recovery = None
            if stop_managed_before_recovery and kwargs.get(
                "stop_managed_processes",
                True,
            ):
                before_recovery = (
                    bootstrapper._stop_managed_processes_for_settings_recovery
                )
            with _obs_bootstrap_mutation_guard(
                bootstrapper.base_dir,
                before_settings_recovery=before_recovery,
            ):
                return method(bootstrapper, *args, **kwargs)

        return guarded

    return decorate


@contextmanager
def obs_config_mutation_guard(
    base_dir: str | Path,
    *,
    before_settings_recovery: Callable[[], None] | None = None,
):
    """Share the migration/bootstrap lock with additional OBS config writers."""

    with _obs_bootstrap_mutation_guard(
        _absolute_path(base_dir),
        before_settings_recovery=before_settings_recovery,
    ):
        yield


def _inventory_fingerprint(entries: tuple[OBSMigrationInventoryEntry, ...]) -> str:
    def metadata_payload(metadata: _OBSFilesystemMetadata | None) -> dict[str, object] | None:
        if metadata is None:
            return None
        return {
            "permissions": metadata.permissions,
            "owner": metadata.owner,
            "group": metadata.group,
            "attributes": metadata.attributes,
            "creation_time": metadata.creation_time,
            "modification_time": metadata.modification_time,
            "change_time": metadata.change_time,
            "security_descriptor_sha256": metadata.security_descriptor_sha256,
            "extended_attributes_sha256": metadata.extended_attributes_sha256,
        }

    payload = [
        {
            "path": entry.relative_path,
            "kind": entry.kind,
            "size": entry.size,
            "sha256": entry.sha256,
            "metadata": metadata_payload(entry.metadata),
        }
        for entry in entries
    ]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _legacy_inventory_fingerprint(
    entries: tuple[OBSMigrationInventoryEntry, ...],
) -> str:
    payload = [
        {
            "path": entry.relative_path,
            "kind": entry.kind,
            "size": entry.size,
            "sha256": entry.sha256,
        }
        for entry in entries
        if entry.relative_parts
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _inventory_fingerprint_for_schema(
    entries: tuple[OBSMigrationInventoryEntry, ...],
    schema_version: int | None,
) -> str:
    if schema_version == 3:
        return _legacy_inventory_fingerprint(entries)
    return _inventory_fingerprint(entries)


def _inventory_content_key(
    entry: OBSMigrationInventoryEntry,
) -> tuple[str, int | None, str | None]:
    """Cross-tree equality excludes metadata that copy does not preserve."""

    return (entry.kind, entry.size, entry.sha256)


def _inventory_content_matches(
    left: tuple[OBSMigrationInventoryEntry, ...],
    right: tuple[OBSMigrationInventoryEntry, ...],
) -> bool:
    return {
        _filesystem_parts_key(entry.relative_parts): _inventory_content_key(entry)
        for entry in left
    } == {
        _filesystem_parts_key(entry.relative_parts): _inventory_content_key(entry)
        for entry in right
    }


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
        try:
            _validate_single_path_component(part)
        except _UnsafeOBSMigrationPathError as exc:
            raise _UnsafeOBSMigrationPathError(
                f"安全でないpath componentがあります: {path}"
            ) from exc


def _inventory_path(root: Path, entry: OBSMigrationInventoryEntry) -> Path:
    _validate_inventory_parts(entry.relative_parts, root / entry.relative_path)
    return root.joinpath(*entry.relative_parts)


def _is_internal_inventory_path(
    relative_parts: tuple[str, ...],
    ignored_root_name_keys: frozenset[str],
) -> bool:
    return _filesystem_name_key(relative_parts[0]) in ignored_root_name_keys




def _has_transaction_temporary_name_under_lease(
    directory: _OBSDirectoryLease,
) -> bool:
    """Inspect transaction names without opening files before an OS lock exists."""

    def list_children(
        current: _OBSDirectoryLease,
    ) -> tuple[tuple[str, str, tuple[int, int, int]], ...]:
        current.validate_lexical_binding()
        scan_descriptor: int | None = None
        scan_target: str | Path | int
        if os.name == "nt":
            scan_target = current.path
        else:
            scan_descriptor = os.open(
                ".",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current.native_handle,
            )
            scan_target = scan_descriptor
        children: list[tuple[str, str, tuple[int, int, int]]] = []
        try:
            with os.scandir(scan_target) as iterator:
                for entry in iterator:
                    _validate_single_path_component(entry.name)
                    child_path = current.path / entry.name
                    child_stat = (
                        os.stat(child_path, follow_symlinks=False)
                        if os.name == "nt"
                        else entry.stat(follow_symlinks=False)
                    )
                    if _is_reparse_point(child_stat):
                        raise _UnsafeOBSMigrationPathError(
                            f"reparse pointは利用できません: {child_path}"
                        )
                    if stat.S_ISDIR(child_stat.st_mode):
                        with current.open_child_directory(entry.name) as child_directory:
                            child_identity = child_directory.identity
                            scanned_identity = _file_identity(child_stat)
                            identity_matches = (
                                child_identity == scanned_identity
                                if os.name != "nt"
                                else (
                                    int(child_stat.st_ino) == child_identity[1]
                                    and stat.S_IFMT(child_stat.st_mode)
                                    == child_identity[2]
                                )
                            )
                        if not identity_matches:
                            raise _UnsafeOBSMigrationPathError(
                                f"列挙中にdirectory identityが変化しました: {child_path}"
                            )
                        kind = "directory"
                    elif stat.S_ISREG(child_stat.st_mode):
                        kind = "file"
                        child_identity = _file_identity(child_stat)
                    else:
                        raise _UnsafeOBSMigrationPathError(
                            f"特殊entryは利用できません: {child_path}"
                        )
                    children.append((entry.name, kind, child_identity))
        finally:
            if scan_descriptor is not None:
                os.close(scan_descriptor)
        current.validate_lexical_binding()
        return tuple(sorted(children, key=lambda child: (child[0].casefold(), child[0])))

    def visit(current: _OBSDirectoryLease) -> bool:
        children_before = list_children(current)
        found = False
        for name, kind, expected_identity in children_before:
            child_path = current.path / name
            parsed = _parse_transaction_temporary(child_path)
            if parsed is not None:
                if kind != "file":
                    raise _UnsafeOBSMigrationPathError(
                        f"transaction一時pathが通常fileではありません: {child_path}"
                    )
                found = True
            elif kind == "directory":
                with current.open_child_directory(name) as child_directory:
                    if child_directory.identity != expected_identity:
                        raise _UnsafeOBSMigrationPathError(
                            f"列挙後にdirectory identityが変化しました: {child_path}"
                        )
                    found = visit(child_directory) or found
        if list_children(current) != children_before:
            raise _UnsafeOBSMigrationPathError(
                f"走査中にdirectory entryが変化しました: {current.path}"
            )
        return found

    return visit(directory)


def _list_root_journal_temporaries(
    directory: _OBSDirectoryLease,
) -> tuple[tuple[_OBSTransactionTemporaryDescriptor, tuple[int, int, int]], ...]:
    """List only control-journal temporaries directly below a held root."""

    settings_marker = get_obs_settings_transaction_marker(directory.path)
    settings_prefix = _filesystem_name_key(f".{settings_marker.name}.")
    migration_marker = get_obs_copy_in_progress_marker(directory.path)
    migration_prefix = _filesystem_name_key(f"{migration_marker.name}.")

    def scan() -> tuple[
        tuple[_OBSTransactionTemporaryDescriptor, tuple[int, int, int]], ...
    ]:
        directory.validate_lexical_binding()
        scan_descriptor: int | None = None
        scan_target: str | Path | int
        if os.name == "nt":
            scan_target = directory.path
        else:
            scan_descriptor = os.open(
                ".",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory.native_handle,
            )
            scan_target = scan_descriptor
        found: list[
            tuple[_OBSTransactionTemporaryDescriptor, tuple[int, int, int]]
        ] = []
        try:
            with os.scandir(scan_target) as iterator:
                for entry in iterator:
                    _validate_single_path_component(entry.name)
                    name_key = _filesystem_name_key(entry.name)
                    if not (
                        (
                            name_key.startswith(settings_prefix)
                            and name_key.endswith(".write.tmp")
                        )
                        or (
                            name_key.startswith(migration_prefix)
                            and name_key.endswith(".tmp")
                        )
                    ):
                        continue
                    path = directory.path / entry.name
                    parsed = _parse_transaction_temporary(path)
                    if parsed is None:
                        continue
                    descriptor = directory.open_file(
                        entry.name,
                        write=False,
                        create_exclusive=False,
                        share_delete=False,
                    )
                    try:
                        identity = _file_identity(os.fstat(descriptor))
                    finally:
                        os.close(descriptor)
                    found.append((parsed, identity))
        except _UnsafeOBSMigrationPathError:
            raise
        except OSError as exc:
            raise _UnsafeOBSMigrationPathError(
                f"root journal temporaryを走査できません: {directory.path} ({exc})"
            ) from exc
        finally:
            if scan_descriptor is not None:
                os.close(scan_descriptor)
        directory.validate_lexical_binding()
        return tuple(
            sorted(
                found,
                key=lambda item: (item[0].path.name.casefold(), item[0].path.name),
            )
        )

    before = scan()
    if scan() != before:
        raise _UnsafeOBSMigrationPathError(
            f"走査中にroot journal temporaryが変化しました: {directory.path}"
        )
    return before


def _list_root_transaction_temporaries(
    directory: _OBSDirectoryLease,
    *,
    strict_names: bool = True,
) -> tuple[tuple[_OBSTransactionTemporaryDescriptor, tuple[int, int, int]], ...]:
    temporaries: list[
        tuple[_OBSTransactionTemporaryDescriptor, tuple[int, int, int]]
    ] = []

    def parse_candidate(path: Path) -> _OBSTransactionTemporaryDescriptor | None:
        try:
            return _parse_transaction_temporary(path)
        except _UnsafeOBSMigrationPathError:
            if strict_names:
                raise
            return None

    def list_children(
        current: _OBSDirectoryLease,
    ) -> tuple[tuple[str, str, tuple[int, int, int]], ...]:
        current.validate_lexical_binding()
        scan_descriptor: int | None = None
        scan_target: str | Path | int
        if os.name == "nt":
            scan_target = current.path
        else:
            scan_descriptor = os.open(
                ".",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current.native_handle,
            )
            scan_target = scan_descriptor
        children: list[tuple[str, str, tuple[int, int, int]]] = []
        try:
            with os.scandir(scan_target) as iterator:
                for entry in iterator:
                    _validate_single_path_component(entry.name)
                    child_path = current.path / entry.name
                    child_stat = (
                        os.stat(child_path, follow_symlinks=False)
                        if os.name == "nt"
                        else entry.stat(follow_symlinks=False)
                    )
                    if _is_reparse_point(child_stat):
                        if not strict_names:
                            continue
                        raise _UnsafeOBSMigrationPathError(
                            f"reparse pointは利用できません: {child_path}"
                        )
                    if stat.S_ISDIR(child_stat.st_mode):
                        kind = "directory"
                    elif stat.S_ISREG(child_stat.st_mode):
                        kind = "file"
                    else:
                        if not strict_names:
                            continue
                        raise _UnsafeOBSMigrationPathError(
                            f"特殊entryは利用できません: {child_path}"
                        )
                    scanned_identity = _file_identity(child_stat)
                    if kind == "directory":
                        with current.open_child_directory(entry.name) as child_directory:
                            child_identity = child_directory.identity
                            identity_matches = (
                                child_identity == scanned_identity
                                if os.name != "nt"
                                else (
                                    int(child_stat.st_ino) == child_identity[1]
                                    and stat.S_IFMT(child_stat.st_mode)
                                    == child_identity[2]
                                )
                            )
                    elif parse_candidate(child_path) is not None:
                        child_descriptor = current.open_file(
                            entry.name,
                            write=False,
                            create_exclusive=False,
                            share_delete=False,
                        )
                        try:
                            child_identity = _file_identity(os.fstat(child_descriptor))
                            identity_matches = child_identity == scanned_identity
                        finally:
                            os.close(child_descriptor)
                    else:
                        # Only transaction-owned files need an authoritative
                        # open handle here. Generic OBS files may legitimately
                        # be hard-linked; their stable directory entry identity
                        # is still compared by the second inventory pass.
                        child_identity = scanned_identity
                        identity_matches = True
                    if not identity_matches:
                        raise _UnsafeOBSMigrationPathError(
                            f"列挙中にentry identityが変化しました: {child_path}"
                        )
                    children.append((entry.name, kind, child_identity))
        except _UnsafeOBSMigrationPathError:
            raise
        except OSError as exc:
            raise _UnsafeOBSMigrationPathError(
                f"directoryを走査できません: {current.path} ({exc})"
            ) from exc
        finally:
            if scan_descriptor is not None:
                os.close(scan_descriptor)
        current.validate_lexical_binding()
        return tuple(sorted(children, key=lambda child: (child[0].casefold(), child[0])))

    def visit(current: _OBSDirectoryLease) -> None:
        children_before = list_children(current)
        for name, kind, expected_identity in children_before:
            child_path = current.path / name
            parsed = parse_candidate(child_path)
            if parsed is not None:
                if kind != "file":
                    raise _UnsafeOBSMigrationPathError(
                        f"transaction一時pathが通常fileではありません: {child_path}"
                    )
                descriptor = current.open_file(
                    name,
                    write=False,
                    create_exclusive=False,
                    share_delete=False,
                )
                try:
                    identity = _file_identity(os.fstat(descriptor))
                    if identity != expected_identity:
                        raise _UnsafeOBSMigrationPathError(
                            f"列挙後にtransaction一時file identityが変化しました: {child_path}"
                        )
                finally:
                    os.close(descriptor)
                temporaries.append((parsed, identity))
            elif kind == "directory":
                with current.open_child_directory(name) as child_directory:
                    if child_directory.identity != expected_identity:
                        raise _UnsafeOBSMigrationPathError(
                            f"列挙後にdirectory identityが変化しました: {child_path}"
                        )
                    visit(child_directory)
        if list_children(current) != children_before:
            raise _UnsafeOBSMigrationPathError(
                f"走査中にdirectory entryが変化しました: {current.path}"
            )

    visit(directory)
    return tuple(temporaries)


def _recover_prejournal_transaction_temporaries(
    directory: _OBSDirectoryLease,
    *,
    destination: Path,
    allowed_sources: tuple[Path, ...],
    validate_transaction: Callable[[], None],
) -> None:
    temporaries = _list_root_transaction_temporaries(directory)
    if not temporaries:
        return
    if len(temporaries) != 1:
        raise _UnsafeOBSMigrationPathError(
            "markerなしで複数のtransaction一時fileが残っています: "
            + ", ".join(str(descriptor.path) for descriptor, _identity in temporaries)
        )
    descriptor, identity = temporaries[0]
    if (
        descriptor.kind != OBS_TRANSACTION_TEMP_JOURNAL
        or descriptor.path.parent != directory.path
    ):
        raise _UnsafeOBSMigrationPathError(
            f"markerなしでdata transaction一時fileが残っています: {descriptor.path}"
        )
    journal = _read_obs_migration_journal(
        descriptor.path,
        directory_lease=directory,
        expected_identity=identity,
    )
    if journal.phase != OBS_MIGRATION_PHASE_COPYING:
        raise _UnsafeOBSMigrationPathError(
            f"pre-journal一時fileのphaseが不正です: {journal.phase}"
        )
    if journal.owner_token != descriptor.owner_token:
        raise _UnsafeOBSMigrationPathError(
            "pre-journal一時fileのowner tokenがfilenameと一致しません"
        )
    journal_source = _validated_journal_source(journal, allowed_sources, destination)
    source_lease: _OBSDirectoryLease | None = _OBSDirectoryLease.open_absolute(
        journal_source
    )
    source_lock = _OBSInterProcessLock(get_obs_copy_lock_path(journal_source))
    try:
        if not source_lock.acquire(directory_lease=source_lease):
            raise OBSMigrationInProgressError(
                "pre-journalの移行元OBSが別のtransactionまたは設定更新で使用中です。"
                "完了後に再検査してください。"
            )
        source_lease.close()
        source_lease = None
        source_root_lease = source_lock.directory_lease
        source_marker = get_obs_copy_in_progress_marker(journal_source)

        def validate_recovery_transaction() -> None:
            validate_transaction()
            source_lock.validate_ownership()
            if (
                source_root_lease.relative_file_identity_or_none(source_marker.name)
                is not None
            ):
                raise _UnsafeOBSMigrationPathError(
                    f"pre-journalの移行元にコピー中markerがあります: {source_marker}"
                )

        validate_recovery_transaction()
        if not _is_valid_obs_installation_lease(source_root_lease):
            raise _UnsafeOBSMigrationPathError(
                f"pre-journalの移行元に有効なOBSがありません: {journal_source}"
            )
        source_entries = _build_obs_tree_inventory(
            journal_source,
            root_lease=source_root_lease,
        )
        validate_recovery_transaction()
        if (
            _inventory_fingerprint_for_schema(
                source_entries,
                journal.schema_version,
            )
            != journal.source_fingerprint
        ):
            raise _UnsafeOBSMigrationPathError(
                "pre-journal一時fileのsource fingerprintが現在の移行元と一致しません"
            )
        validate_recovery_transaction()
        directory.unlink_file(
            descriptor.path.name,
            expected_identity=identity,
        )
        validate_recovery_transaction()
    finally:
        try:
            if source_lease is not None:
                source_lease.close()
        finally:
            source_lock.release()


def _validate_owned_transaction_temporary(
    path: Path,
    *,
    root_lease: _OBSDirectoryLease | None = None,
) -> tuple[int, int, int]:
    if root_lease is None:
        directory = _OBSDirectoryLease.open_absolute(path.parent, mutable=True)
        directory_owned = True
    else:
        directory, directory_owned = _directory_for_descendant_parent(root_lease, path)
    try:
        descriptor = directory.open_file(
            path.name,
            write=False,
            create_exclusive=False,
        )
        try:
            opened = os.fstat(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if directory_owned:
            directory.close()
    return _file_identity(opened)




def _remove_owned_transaction_temporaries(
    paths: Iterable[Path],
    *,
    root_lease: _OBSDirectoryLease | None = None,
    validate_transaction: Callable[[], None] | None = None,
) -> None:
    validated: list[tuple[Path, tuple[int, int, int]]] = []
    seen: set[str] = set()
    if validate_transaction is not None:
        validate_transaction()
    for path in paths:
        key = _filesystem_path_key(path)
        if key in seen:
            continue
        seen.add(key)
        if root_lease is None:
            if not _path_lexists(path):
                continue
        else:
            try:
                directory, directory_owned = _directory_for_descendant_parent(
                    root_lease,
                    path,
                    mutable=False,
                )
            except FileNotFoundError:
                continue
            try:
                if directory.relative_file_identity_or_none(path.name) is None:
                    continue
            finally:
                if directory_owned:
                    directory.close()
        validated.append(
            (
                path,
                _validate_owned_transaction_temporary(
                    path,
                    root_lease=root_lease,
                ),
            )
        )

    for path, identity in validated:
        if root_lease is None:
            directory = _OBSDirectoryLease.open_absolute(path.parent, mutable=True)
            directory_owned = True
        else:
            directory, directory_owned = _directory_for_descendant_parent(
                root_lease,
                path,
            )
        try:
            if validate_transaction is not None:
                validate_transaction()
            directory.unlink_file(path.name, expected_identity=identity)
            if validate_transaction is not None:
                validate_transaction()
        finally:
            if directory_owned:
                directory.close()
    if validate_transaction is not None:
        validate_transaction()


def _recover_owned_transaction_temporaries(
    destination: Path,
    source_entries: tuple[OBSMigrationInventoryEntry, ...],
    owner_token: str,
    *,
    destination_root_lease: _OBSDirectoryLease | None = None,
    validate_transaction: Callable[[], None] | None = None,
) -> None:
    owner_token = _validate_migration_owner_token(owner_token)
    descriptors = _expected_transaction_temporaries(
        destination,
        source_entries,
        owner_token,
    )
    _remove_owned_transaction_temporaries(
        (descriptor.path for descriptor in descriptors),
        root_lease=destination_root_lease,
        validate_transaction=validate_transaction,
    )


def _expected_transaction_temporaries(
    destination: Path,
    source_entries: tuple[OBSMigrationInventoryEntry, ...],
    owner_token: str,
) -> tuple[_OBSTransactionTemporaryDescriptor, ...]:
    owner_token = _validate_migration_owner_token(owner_token)
    marker = get_obs_copy_in_progress_marker(destination)
    descriptors = [
        _OBSTransactionTemporaryDescriptor(
            kind=OBS_TRANSACTION_TEMP_JOURNAL,
            target_name=marker.name,
            owner_token=owner_token,
            path=_transaction_journal_temporary_path(marker, owner_token),
        )
    ]
    descriptors.extend(
        _OBSTransactionTemporaryDescriptor(
            kind=OBS_TRANSACTION_TEMP_COPY,
            target_name=target.name,
            owner_token=owner_token,
            path=_transaction_copy_temporary_path(target, owner_token),
        )
        for entry in source_entries
        if entry.kind == "file"
        for target in (_inventory_path(destination, entry),)
    )
    finalize_targets = (
        get_portable_marker_path(destination),
        get_legacy_marker_path(destination),
        get_obs_global_ini_path(destination),
        get_obs_user_ini_path(destination),
        get_obs_websocket_config_path(destination),
    )
    descriptors.extend(
        _OBSTransactionTemporaryDescriptor(
            kind=OBS_TRANSACTION_TEMP_WRITE,
            target_name=target.name,
            owner_token=owner_token,
            path=_transaction_write_temporary_path(target, owner_token),
        )
        for target in finalize_targets
    )
    return tuple(descriptors)


def _build_obs_tree_inventory(
    root: Path,
    *,
    root_lease: _OBSDirectoryLease | None = None,
    ignored_root_names: frozenset[str] = OBS_COPY_SKIP_NAMES,
) -> tuple[OBSMigrationInventoryEntry, ...]:
    root = _absolute_path(root)
    lease_owned = root_lease is None
    lease = root_lease or _OBSDirectoryLease.open_absolute(root)
    if lease.path != root:
        if lease_owned:
            lease.close()
        raise _UnsafeOBSMigrationPathError(
            f"inventory root leaseがrootと一致しません: {lease.path} != {root}"
        )
    inventory: list[OBSMigrationInventoryEntry] = []
    seen: set[tuple[str, ...]] = set()
    ignored_root_name_keys = frozenset(
        _filesystem_name_key(name) for name in ignored_root_names
    )

    def list_children(
        directory: _OBSDirectoryLease,
    ) -> tuple[
        tuple[str, str, tuple[int, int, int], _OBSFilesystemMetadata], ...
    ]:
        directory.validate_lexical_binding()
        scan_descriptor: int | None = None
        scan_target: str | Path | int
        if os.name == "nt":
            scan_target = directory.path
        else:
            scan_descriptor = os.open(
                ".",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory.native_handle,
            )
            scan_target = scan_descriptor
        try:
            with os.scandir(scan_target) as iterator:
                children: list[
                    tuple[str, str, tuple[int, int, int], _OBSFilesystemMetadata]
                ] = []
                for entry in iterator:
                    _validate_single_path_component(entry.name)
                    child_path = directory.path / entry.name
                    try:
                        child_stat = entry.stat(follow_symlinks=False)
                        if os.name == "nt":
                            child_stat = os.stat(child_path, follow_symlinks=False)
                    except OSError as exc:
                        raise _UnsafeOBSMigrationPathError(
                            f"directory entryを検査できません: {child_path} ({exc})"
                        ) from exc
                    if _is_reparse_point(child_stat):
                        raise _UnsafeOBSMigrationPathError(
                            f"reparse pointは利用できません: {child_path}"
                        )
                    if stat.S_ISDIR(child_stat.st_mode):
                        kind = "directory"
                    elif stat.S_ISREG(child_stat.st_mode):
                        kind = "file"
                    else:
                        raise _UnsafeOBSMigrationPathError(
                            f"特殊entryは利用できません: {child_path}"
                        )
                    if kind == "directory":
                        with directory.open_child_directory(entry.name) as child_directory:
                            child_identity = child_directory.identity
                            scanned_identity = _file_identity(child_stat)
                            directory_identity_matches = (
                                child_identity == scanned_identity
                                if os.name != "nt"
                                else (
                                    int(child_stat.st_ino) == child_identity[1]
                                    and stat.S_IFMT(child_stat.st_mode)
                                    == child_identity[2]
                                )
                            )
                            if not directory_identity_matches:
                                raise _UnsafeOBSMigrationPathError(
                                    f"列挙中にdirectory identityが変化しました: {child_path}"
                                )
                            child_metadata = _snapshot_open_entry_metadata(
                                child_directory.native_handle,
                                path=child_path,
                                kind="directory",
                                native_windows_handle=os.name == "nt",
                            )
                    else:
                        child_descriptor = directory.open_file(
                            entry.name,
                            write=False,
                            create_exclusive=False,
                            share_delete=False,
                        )
                        try:
                            opened = os.fstat(child_descriptor)
                            child_identity = _file_identity(opened)
                            if child_identity != _file_identity(child_stat):
                                raise _UnsafeOBSMigrationPathError(
                                    f"列挙中にfile identityが変化しました: {child_path}"
                                )
                            child_metadata = _snapshot_open_entry_metadata(
                                child_descriptor,
                                path=child_path,
                                kind="file",
                            )
                        finally:
                            os.close(child_descriptor)
                    children.append(
                        (entry.name, kind, child_identity, child_metadata)
                    )
        except _UnsafeOBSMigrationPathError:
            raise
        except OSError as exc:
            raise _UnsafeOBSMigrationPathError(
                f"directoryを走査できません: {directory.path} ({exc})"
            ) from exc
        finally:
            if scan_descriptor is not None:
                os.close(scan_descriptor)
        directory.validate_lexical_binding()
        return tuple(sorted(children, key=lambda child: (child[0].casefold(), child[0])))

    def hash_relative_file(
        directory: _OBSDirectoryLease,
        name: str,
        expected_identity: tuple[int, int, int],
        expected_metadata: _OBSFilesystemMetadata,
    ) -> tuple[int, str, _OBSFilesystemMetadata]:
        descriptor = directory.open_file(
            name,
            write=False,
            create_exclusive=False,
            share_delete=False,
        )
        try:
            before = os.fstat(descriptor)
            identity = _file_identity(before)
            if identity != expected_identity:
                raise _UnsafeOBSMigrationPathError(
                    f"列挙後にfile identityが変化しました: {directory.path / name}"
                )
            metadata_before = _snapshot_open_entry_metadata(
                descriptor,
                path=directory.path / name,
                kind="file",
            )
            if metadata_before != expected_metadata:
                raise _UnsafeOBSMigrationPathError(
                    f"列挙後にfile metadataが変化しました: {directory.path / name}"
                )
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(descriptor)
            metadata_after = _snapshot_open_entry_metadata(
                descriptor,
                path=directory.path / name,
                kind="file",
            )
            if (
                _file_identity(after) != identity
                or int(after.st_size) != size
                or directory._relative_file_identity(name) != identity
                or metadata_after != metadata_before
            ):
                raise _UnsafeOBSMigrationPathError(
                    f"hash中にfileが変化しました: {directory.path / name}"
                )
            return size, digest.hexdigest(), metadata_after
        finally:
            os.close(descriptor)

    def visit(
        directory: _OBSDirectoryLease,
        relative_parent: tuple[str, ...] = (),
        *,
        include: bool = True,
    ) -> None:
        children_before = list_children(directory)
        for child_name, child_kind, child_identity, child_metadata in children_before:
            child_path = directory.path / child_name
            relative_parts = (*relative_parent, child_name)
            _validate_inventory_parts(relative_parts, child_path)
            temporary = _parse_transaction_temporary(child_path)
            if temporary is not None and temporary.kind == OBS_TRANSACTION_TEMP_JOURNAL:
                if len(relative_parts) != 1:
                    raise _UnsafeOBSMigrationPathError(
                        f"journal一時fileが管理root外の階層にあります: {child_path}"
                    )
                raise _UnsafeOBSMigrationPathError(
                    f"孤立したjournal一時fileがあります: {child_path}"
                )
            if temporary is not None:
                raise _UnsafeOBSMigrationPathError(
                    f"孤立したtransaction一時fileがあります: {child_path}"
                )
            relative_path = "/".join(relative_parts)
            normalized_key = _filesystem_parts_key(relative_parts)
            if normalized_key in seen:
                raise _UnsafeOBSMigrationPathError(
                    f"大文字小文字が衝突するentryがあります: {relative_path}"
                )
            seen.add(normalized_key)
            child_include = include and not _is_internal_inventory_path(
                relative_parts,
                ignored_root_name_keys,
            )
            if child_kind == "directory":
                with directory.open_child_directory(child_name) as child_directory:
                    if child_directory.identity != child_identity:
                        raise _UnsafeOBSMigrationPathError(
                            f"列挙後にdirectory identityが変化しました: {child_path}"
                        )
                    opened_metadata = _snapshot_open_entry_metadata(
                        child_directory.native_handle,
                        path=child_path,
                        kind="directory",
                        native_windows_handle=os.name == "nt",
                    )
                    if opened_metadata != child_metadata:
                        raise _UnsafeOBSMigrationPathError(
                            f"列挙後にdirectory metadataが変化しました: {child_path}"
                        )
                    if child_include:
                        inventory.append(
                            OBSMigrationInventoryEntry(
                                relative_parts,
                                "directory",
                                metadata=opened_metadata,
                            )
                        )
                    visit(
                        child_directory,
                        relative_parts,
                        include=child_include,
                    )
            else:
                if child_include:
                    size, sha256, metadata = hash_relative_file(
                        directory,
                        child_name,
                        child_identity,
                        child_metadata,
                    )
                    inventory.append(
                        OBSMigrationInventoryEntry(
                            relative_parts,
                            "file",
                            size,
                            sha256,
                            metadata,
                        )
                    )
        if list_children(directory) != children_before:
            raise _UnsafeOBSMigrationPathError(
                f"走査中にdirectory entryが変化しました: {directory.path}"
            )

    try:
        root_metadata_before = _snapshot_open_entry_metadata(
            lease.native_handle,
            path=root,
            kind="directory",
            native_windows_handle=os.name == "nt",
        )
        visit(lease)
        lease.validate_lexical_binding()
        root_metadata_after = _snapshot_open_entry_metadata(
            lease.native_handle,
            path=root,
            kind="directory",
            native_windows_handle=os.name == "nt",
        )
        if root_metadata_after != root_metadata_before:
            raise _UnsafeOBSMigrationPathError(
                f"走査中にinventory root metadataが変化しました: {root}"
            )
        inventory.append(
            OBSMigrationInventoryEntry(
                (),
                "directory",
                metadata=root_metadata_after,
            )
        )
        return tuple(
            sorted(
                inventory,
                key=lambda entry: _filesystem_parts_key(entry.relative_parts),
            )
        )
    finally:
        if lease_owned:
            lease.close()


def _validate_destination_subset(
    destination_entries: tuple[OBSMigrationInventoryEntry, ...],
    source_entries: tuple[OBSMigrationInventoryEntry, ...],
    destination: Path,
) -> None:
    source_by_path = {
        _filesystem_parts_key(entry.relative_parts): entry for entry in source_entries
    }
    for entry in destination_entries:
        expected = source_by_path.get(_filesystem_parts_key(entry.relative_parts))
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
    parts = _filesystem_parts_key(entry.relative_parts)
    if entry.kind == "directory":
        return parts in OBS_MIGRATION_FINALIZE_DIRECTORY_KEYS
    return parts in OBS_MIGRATION_FINALIZE_FILE_KEYS


def _validate_finalize_pending_destination(
    destination_entries: tuple[OBSMigrationInventoryEntry, ...],
    source_entries: tuple[OBSMigrationInventoryEntry, ...],
    destination: Path,
) -> None:
    source_unmanaged = {
        _filesystem_parts_key(entry.relative_parts): _inventory_content_key(entry)
        for entry in source_entries
        if not _is_finalize_managed_entry(entry)
    }
    destination_unmanaged = {
        _filesystem_parts_key(entry.relative_parts): _inventory_content_key(entry)
        for entry in destination_entries
        if not _is_finalize_managed_entry(entry)
    }
    if destination_unmanaged != source_unmanaged:
        changed_paths = sorted(
            "/".join(parts)
            for parts in source_unmanaged.keys() | destination_unmanaged.keys()
            if source_unmanaged.get(parts) != destination_unmanaged.get(parts)
        )
        raise _migration_recovery_error(
            destination,
            "最終化allowlist外のentryが移行元inventoryと一致しません: "
            + ", ".join(changed_paths),
        )


def _validate_finalize_changes(
    before_entries: tuple[OBSMigrationInventoryEntry, ...],
    after_entries: tuple[OBSMigrationInventoryEntry, ...],
    destination: Path,
) -> None:
    before = {
        _filesystem_parts_key(entry.relative_parts): entry
        for entry in before_entries
    }
    after = {
        _filesystem_parts_key(entry.relative_parts): entry
        for entry in after_entries
    }
    changed = {
        parts
        for parts in before.keys() | after.keys()
        if before.get(parts) != after.get(parts)
    }
    forbidden = sorted(
        "/".join(parts) or "<root>"
        for parts in changed
        if parts not in OBS_MIGRATION_FINALIZE_FILE_KEYS
        and parts not in OBS_MIGRATION_FINALIZE_DIRECTORY_KEYS
    )
    if forbidden:
        raise _migration_recovery_error(
            destination,
            "finalizerがallowlist外を変更しました: " + ", ".join(forbidden),
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
    *,
    source_root_lease: _OBSDirectoryLease | None = None,
    destination_root_lease: _OBSDirectoryLease | None = None,
    validate_transaction: Callable[[], None] | None = None,
) -> None:
    temporary = _transaction_copy_temporary_path(destination, owner_token)
    with ExitStack() as resources:
        source_parent_owned = source_root_lease is None or bool(expected.relative_parts[:-1])
        source_parent = (
            _OBSDirectoryLease.open_absolute(source.parent)
            if source_root_lease is None
            else (
                source_root_lease.open_descendant_directory(
                    expected.relative_parts[:-1],
                )
                if expected.relative_parts[:-1]
                else source_root_lease
            )
        )
        if source_parent_owned:
            resources.callback(source_parent.close)

        destination_parent_owned = destination_root_lease is None or bool(
            expected.relative_parts[:-1]
        )
        destination_parent = (
            _OBSDirectoryLease.open_absolute(destination.parent, mutable=True)
            if destination_root_lease is None
            else (
                destination_root_lease.open_descendant_directory(
                    expected.relative_parts[:-1],
                    mutable=True,
                )
                if expected.relative_parts[:-1]
                else destination_root_lease
            )
        )
        if destination_parent_owned:
            resources.callback(destination_parent.close)

        source_descriptor = source_parent.open_file(
            source.name,
            write=False,
            create_exclusive=False,
        )
        resources.callback(os.close, source_descriptor)
        source_before = os.fstat(source_descriptor)
        source_metadata_before = _snapshot_open_entry_metadata(
            source_descriptor,
            path=source,
            kind="file",
        )
        if (
            expected.metadata is None
            or source_metadata_before != expected.metadata
        ):
            raise _UnsafeOBSMigrationPathError(
                f"コピー中または直前に移行元file metadataが変化しました: {source}"
            )
        temporary_descriptor: int | None = None
        temporary_identity: tuple[int, int, int] | None = None
        try:
            destination_before = destination_parent.relative_file_identity_or_none(
                destination.name
            )
            temporary_descriptor = destination_parent.open_file(
                temporary.name,
                write=True,
                create_exclusive=True,
            )
            temporary_stat = os.fstat(temporary_descriptor)
            temporary_identity = _file_identity(temporary_stat)

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
            source_metadata_after = _snapshot_open_entry_metadata(
                source_descriptor,
                path=source,
                kind="file",
            )
            if _file_identity(os.fstat(source_descriptor)) != _file_identity(source_before):
                raise _UnsafeOBSMigrationPathError(
                    f"コピー中に移行元file identityが変化しました: {source}"
                )
            if source_metadata_after != source_metadata_before:
                raise _UnsafeOBSMigrationPathError(
                    f"コピー中に移行元file metadataが変化しました: {source}"
                )
            if source_parent._relative_file_identity(source.name) != _file_identity(
                source_before
            ):
                raise _UnsafeOBSMigrationPathError(
                    f"コピー中に移行元file pathが変化しました: {source}"
                )
            if size != expected.size or digest.hexdigest() != expected.sha256:
                raise _UnsafeOBSMigrationPathError(
                    f"コピー中に移行元fileが変化しました: {source}"
                )
            written = os.fstat(temporary_descriptor)
            temporary_metadata = _snapshot_open_entry_metadata(
                temporary_descriptor,
                path=temporary,
                kind="file",
            )
            if int(written.st_size) != size:
                raise _UnsafeOBSMigrationPathError(
                    f"一時コピーのsizeが一致しません: {temporary}"
                )

            destination_current = destination_parent.relative_file_identity_or_none(
                destination.name
            )
            if destination_current != destination_before:
                raise _UnsafeOBSMigrationPathError(
                    f"コピー中にコピー先fileが変化しました: {destination}"
                )
            if destination_parent._relative_file_identity(temporary.name) != temporary_identity:
                raise _UnsafeOBSMigrationPathError(
                    f"一時コピーのidentityが変化しました: {temporary}"
                )
            os.close(temporary_descriptor)
            temporary_descriptor = None
            temporary_descriptor = destination_parent.open_file(
                temporary.name,
                write=True,
                create_exclusive=False,
                delete=True,
            )
            if _file_identity(os.fstat(temporary_descriptor)) != temporary_identity:
                raise _UnsafeOBSMigrationPathError(
                    f"一時コピーのreopen中にidentityが変化しました: {temporary}"
                )
            if _snapshot_open_entry_metadata(
                temporary_descriptor,
                path=temporary,
                kind="file",
            ) != temporary_metadata:
                raise _UnsafeOBSMigrationPathError(
                    f"一時コピーのreopen中にmetadataが変化しました: {temporary}"
                )
            if validate_transaction is not None:
                validate_transaction()
            destination_parent.replace_open_file(
                temporary_descriptor,
                temporary.name,
                destination.name,
            )
            if destination_parent._relative_file_identity(destination.name) != temporary_identity:
                raise _UnsafeOBSMigrationPathError(
                    f"コピー先確定時にidentityが変化しました: {destination}"
                )
        finally:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            if (
                temporary_identity is not None
                and destination_parent.relative_file_identity_or_none(temporary.name)
                is not None
            ):
                try:
                    destination_parent.unlink_file(
                        temporary.name,
                        expected_identity=temporary_identity,
                    )
                except _UnsafeOBSMigrationPathError:
                    pass


def _copy_obs_inventory(
    source: Path,
    destination: Path,
    source_entries: tuple[OBSMigrationInventoryEntry, ...],
    owner_token: str,
    *,
    source_root_lease: _OBSDirectoryLease,
    destination_root_lease: _OBSDirectoryLease,
    validate_transaction: Callable[[], None],
) -> None:
    for entry in source_entries:
        validate_transaction()
        if entry.kind == "directory":
            if not entry.relative_parts:
                continue
            with destination_root_lease.open_descendant_directory(
                entry.relative_parts,
                create=True,
            ):
                pass
    for entry in source_entries:
        validate_transaction()
        if entry.kind != "file":
            continue
        target = _inventory_path(destination, entry)
        _copy_inventory_file(
            _inventory_path(source, entry),
            target,
            entry,
            owner_token,
            source_root_lease=source_root_lease,
            destination_root_lease=destination_root_lease,
            validate_transaction=validate_transaction,
        )
        validate_transaction()


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
    try:
        allowed_sources = _normalized_obs_paths(legacy_candidates, excluded=destination)
    except _UnsafeOBSMigrationPathError as exc:
        raise _migration_recovery_error(destination, str(exc)) from exc
    marker = get_obs_copy_in_progress_marker(destination)
    if not _supports_handle_relative_migration():
        raise _migration_recovery_error(
            destination,
            "このOS/filesystem runtimeはdirectory handle相対の安全な作成・replace・unlinkを提供しません。",
        )

    destination_probe: _OBSDirectoryLease | None = None
    source_probe: _OBSDirectoryLease | None = None
    source: Path | None = None
    prelock_marker_identity: tuple[int, int, int] | None = None
    prelock_journal: OBSMigrationJournal | None = None
    transaction_resources = ExitStack()
    try:
        try:
            try:
                destination_probe = _OBSDirectoryLease.open_absolute(destination)
            except FileNotFoundError:
                destination_probe = None

            if destination_probe is not None:
                prelock_marker_identity = (
                    destination_probe.relative_file_identity_or_none(marker.name)
                )
            marker_exists = prelock_marker_identity is not None
            source_tree_identity_set: set[tuple[int, int, int]] = set()
            if marker_exists:
                if destination_probe is None:
                    raise AssertionError("markerにはdestination leaseが必要です")
                prelock_journal = _read_obs_migration_journal(
                    marker,
                    directory_lease=destination_probe,
                    expected_identity=prelock_marker_identity,
                )
                source = _validated_journal_source(
                    prelock_journal,
                    allowed_sources,
                    destination,
                )
                try:
                    source_probe = _OBSDirectoryLease.open_absolute(source)
                except FileNotFoundError as exc:
                    raise _UnsafeOBSMigrationPathError(
                        "markerの移行元に有効なポータブルOBSがありません: "
                        f"{get_obs_executable_path(source)}"
                    ) from exc
                if not _is_valid_obs_installation_lease(source_probe):
                    raise _UnsafeOBSMigrationPathError(
                        "markerの移行元に有効なポータブルOBSがありません: "
                        f"{get_obs_executable_path(source)}"
                    )
            for candidate in allowed_sources:
                candidate_owned = True
                if (
                    source_probe is not None
                    and source is not None
                    and _filesystem_path_key(candidate)
                    == _filesystem_path_key(source)
                ):
                    candidate_probe = source_probe
                    candidate_owned = False
                else:
                    try:
                        candidate_probe = _OBSDirectoryLease.open_absolute(candidate)
                    except FileNotFoundError:
                        continue
                try:
                    if not _is_valid_obs_installation_lease(candidate_probe):
                        continue
                    source_marker = get_obs_copy_in_progress_marker(candidate)
                    if (
                        candidate_probe.relative_file_identity_or_none(source_marker.name)
                        is not None
                    ):
                        raise _UnsafeOBSMigrationPathError(
                            f"移行元にコピー中markerがあります: {source_marker}"
                        )
                    source_settings_marker = get_obs_settings_transaction_marker(candidate)
                    if (
                        candidate_probe.relative_file_identity_or_none(
                            source_settings_marker.name
                        )
                        is not None
                    ):
                        raise _UnsafeOBSMigrationPathError(
                            f"移行元に起動前設定transaction markerがあります: {source_settings_marker}"
                        )
                    if _has_transaction_temporary_name_under_lease(candidate_probe):
                        raise _UnsafeOBSMigrationPathError(
                            f"移行元にtransaction一時fileがあります: {candidate}"
                        )
                    source_tree_identity_set.update(
                        _validate_distinct_physical_directory_trees(
                            candidate_probe,
                            destination,
                            destination_lease=destination_probe,
                        )
                    )
                    if source_probe is None:
                        source = candidate
                        source_probe = candidate_probe
                        candidate_owned = False
                finally:
                    if candidate_owned:
                        candidate_probe.close()
            if not marker_exists:
                if source_probe is None and destination_probe is None:
                    return None

            source_tree_identities = frozenset(source_tree_identity_set)

            lock = _OBSInterProcessLock(get_obs_copy_lock_path(destination))
            if not lock.acquire(
                directory_lease=destination_probe,
                reject_directory_identities=source_tree_identities,
            ):
                raise OBSMigrationInProgressError(
                    "別のプロセスがOBSのコピー移行を実行中です。完了後に再検査してください。"
                )
            transaction_resources.callback(lock.release)
            if destination_probe is not None:
                destination_probe.close()
                destination_probe = None
        except OBSMigrationError:
            raise
        except (OSError, _UnsafeOBSMigrationPathError) as exc:
            raise _migration_recovery_error(
                destination,
                f"migration lockを安全に確保できません: {exc}",
            ) from exc

        source_lock: _OBSInterProcessLock | None = None
        try:
            destination_root_lease = lock.directory_lease
            lock.validate_ownership()
            settings_marker = get_obs_settings_transaction_marker(destination)
            if (
                destination_root_lease.relative_file_identity_or_none(
                    settings_marker.name
                )
                is not None
            ):
                raise _migration_recovery_error(
                    destination,
                    "起動前設定transaction markerが残っています。設定の再検査で復旧してから移行を再試行してください。",
                )
            stale_journal: OBSMigrationJournal | None = None
            marker_identity = destination_root_lease.relative_file_identity_or_none(
                marker.name
            )
            if marker_identity is None:
                _recover_prejournal_transaction_temporaries(
                    destination_root_lease,
                    destination=destination,
                    allowed_sources=allowed_sources,
                    validate_transaction=lock.validate_ownership,
                )
                marker_identity = destination_root_lease.relative_file_identity_or_none(
                    marker.name
                )
            if prelock_marker_identity is not None:
                if marker_identity != prelock_marker_identity:
                    raise _migration_recovery_error(
                        destination,
                        "migration lock取得中にmarker identityが変化しました。",
                    )
                locked_journal = _read_obs_migration_journal(
                    marker,
                    directory_lease=destination_root_lease,
                    expected_identity=prelock_marker_identity,
                )
                if locked_journal != prelock_journal:
                    raise _migration_recovery_error(
                        destination,
                        "migration lock取得中にjournal内容が変化しました。",
                    )
                locked_source = _validated_journal_source(
                    locked_journal,
                    allowed_sources,
                    destination,
                )
                if source != locked_source or source_probe is None:
                    raise _migration_recovery_error(
                        destination,
                        "migration lock取得中にjournal sourceが変化しました。",
                    )
                source_probe.validate_lexical_binding()
                stale_journal = locked_journal
            elif marker_identity is not None:
                raise _migration_recovery_error(
                    destination,
                    "migration lock取得中に新しいmarkerが出現しました。再試行してください。",
                )
            elif _is_valid_obs_installation_lease(destination_root_lease):
                return None
            else:
                if source is None or source_probe is None:
                    return None

            if not _is_valid_obs_installation_lease(source_probe):
                raise _migration_recovery_error(
                    destination,
                    f"markerの移行元に有効なポータブルOBSがありません: {get_obs_executable_path(source)}",
                )
            _validate_distinct_physical_directory_trees(
                source_probe,
                destination,
                destination_lease=destination_root_lease,
            )
            source_marker = get_obs_copy_in_progress_marker(source)
            if source_probe.relative_file_identity_or_none(source_marker.name) is not None:
                raise _migration_recovery_error(
                    destination,
                    f"移行元にコピー中markerがあります: {source_marker}",
                )
            source_lock = _OBSInterProcessLock(get_obs_copy_lock_path(source))
            if not source_lock.acquire(directory_lease=source_probe):
                raise OBSMigrationInProgressError(
                    "移行元OBSが別のtransactionまたは設定更新で使用中です。完了後に再検査してください。"
                )
            transaction_resources.callback(source_lock.release)
            source_probe.close()
            source_probe = None
            source_root_lease = source_lock.directory_lease

            source_settings_marker = get_obs_settings_transaction_marker(source)
            if (
                source_root_lease.relative_file_identity_or_none(
                    source_settings_marker.name
                )
                is not None
            ):
                raise _migration_recovery_error(
                    destination,
                    "移行元に起動前設定transaction markerがあります: "
                    f"{source_settings_marker}",
                )
            if _has_transaction_temporary_name_under_lease(source_root_lease):
                raise _migration_recovery_error(
                    destination,
                    f"移行元にtransaction一時fileがあります: {source}",
                )

            def validate_transaction_locks() -> None:
                lock.validate_ownership()
                source_lock.validate_ownership()
                if (
                    source_root_lease.relative_file_identity_or_none(
                        source_marker.name
                    )
                    is not None
                ):
                    raise _migration_recovery_error(
                        destination,
                        f"移行元にコピー中markerがあります: {source_marker}",
                    )

            validate_transaction_locks()
            source_root_lease.validate_lexical_binding()
            destination_root_lease.validate_lexical_binding()
            if not _is_valid_obs_installation_lease(source_root_lease):
                raise _migration_recovery_error(
                    destination,
                    f"移行元に有効なポータブルOBSがありません: {get_obs_executable_path(source)}",
                )
            source_marker = get_obs_copy_in_progress_marker(source)
            if source_root_lease.relative_file_identity_or_none(source_marker.name) is not None:
                raise _migration_recovery_error(
                    destination,
                    f"移行元にコピー中markerがあります: {source_marker}",
                )

            owner_token = stale_journal.owner_token if stale_journal is not None else uuid.uuid4().hex
            phase = stale_journal.phase if stale_journal is not None else OBS_MIGRATION_PHASE_COPYING
            if phase == OBS_MIGRATION_PHASE_COPYING and prepare_source is not None:
                prepare_source(source)
            validate_transaction_locks()
            if not _is_valid_obs_installation_lease(source_root_lease):
                raise _migration_recovery_error(
                    destination,
                    f"移行準備中にOBS executable layoutが変化しました: {get_obs_executable_path(source)}",
                )
            source_root_lease.validate_lexical_binding()
            source_entries = _build_obs_tree_inventory(
                source,
                root_lease=source_root_lease,
            )
            source_root_lease.validate_lexical_binding()
            source_fingerprint = _inventory_fingerprint(source_entries)
            if (
                stale_journal is not None
                and _inventory_fingerprint_for_schema(
                    source_entries,
                    stale_journal.schema_version,
                )
                != stale_journal.source_fingerprint
            ):
                raise _migration_recovery_error(
                    destination,
                    "marker作成後に移行元の内容が変化したため、部分配置へoverlayできません。",
                )
            if stale_journal is not None:
                _recover_owned_transaction_temporaries(
                    destination,
                    source_entries,
                    owner_token,
                    destination_root_lease=destination_root_lease,
                    validate_transaction=validate_transaction_locks,
                )

            validate_transaction_locks()
            _write_obs_migration_journal(
                marker,
                source,
                source_fingerprint,
                owner_token,
                phase=phase,
                directory_lease=destination_root_lease,
                validate_transaction=validate_transaction_locks,
            )
            if phase == OBS_MIGRATION_PHASE_COPYING:
                destination_root_lease.validate_lexical_binding()
                destination_entries = _build_obs_tree_inventory(
                    destination,
                    root_lease=destination_root_lease,
                )
                destination_root_lease.validate_lexical_binding()
                _validate_destination_subset(destination_entries, source_entries, destination)

                _copy_obs_inventory(
                    source,
                    destination,
                    source_entries,
                    owner_token,
                    source_root_lease=source_root_lease,
                    destination_root_lease=destination_root_lease,
                    validate_transaction=validate_transaction_locks,
                )
                validate_transaction_locks()
                source_root_lease.validate_lexical_binding()
                if (
                    _build_obs_tree_inventory(
                        source,
                        root_lease=source_root_lease,
                    )
                    != source_entries
                ):
                    raise _migration_recovery_error(destination, "コピー中に移行元の内容が変化しました。")
                source_root_lease.validate_lexical_binding()
                destination_root_lease.validate_lexical_binding()
                copied_entries = _build_obs_tree_inventory(
                    destination,
                    root_lease=destination_root_lease,
                )
                destination_root_lease.validate_lexical_binding()
                if not _inventory_content_matches(copied_entries, source_entries):
                    raise _migration_recovery_error(
                        destination,
                        "コピー後inventoryが移行元と双方向一致しません。余剰または不完全なentryがあります。",
                    )
                if finalize_destination is None:
                    validate_transaction_locks()
                    _remove_obs_migration_journal_if_owned(
                        marker,
                        owner_token,
                        directory_lease=destination_root_lease,
                        validate_transaction=validate_transaction_locks,
                    )
                    return source
                finalize_pending_entries = _build_obs_tree_inventory(
                    destination,
                    root_lease=destination_root_lease,
                    ignored_root_names=OBS_FINALIZE_INVENTORY_SKIP_NAMES,
                )
                _validate_finalize_pending_destination(
                    finalize_pending_entries,
                    source_entries,
                    destination,
                )
                _write_obs_migration_journal(
                    marker,
                    source,
                    source_fingerprint,
                    owner_token,
                    phase=OBS_MIGRATION_PHASE_FINALIZE_PENDING,
                    directory_lease=destination_root_lease,
                    validate_transaction=validate_transaction_locks,
                )
            else:
                if finalize_destination is None:
                    raise _migration_recovery_error(
                        destination,
                        "最終化待ちmarkerがありますが、最終化処理が指定されていません。",
                    )
                destination_entries = _build_obs_tree_inventory(
                    destination,
                    root_lease=destination_root_lease,
                    ignored_root_names=OBS_FINALIZE_INVENTORY_SKIP_NAMES,
                )
                destination_root_lease.validate_lexical_binding()
                _validate_finalize_pending_destination(destination_entries, source_entries, destination)
        except OBSMigrationError:
            raise
        except (OSError, _UnsafeOBSMigrationPathError) as exc:
            raise _migration_recovery_error(destination, f"安全なコピー移行を完了できません: {exc}") from exc

        if finalize_destination is not None:
            validate_transaction_locks()
            try:
                for relative_parts in sorted(
                    OBS_MIGRATION_FINALIZE_DIRECTORY_PARTS,
                    key=lambda parts: (len(parts), parts),
                ):
                    validate_transaction_locks()
                    with destination_root_lease.open_descendant_directory(
                        tuple(relative_parts),
                        create=True,
                    ):
                        pass
                before_finalize_entries = _build_obs_tree_inventory(
                    destination,
                    root_lease=destination_root_lease,
                    ignored_root_names=OBS_FINALIZER_CALLBACK_INVENTORY_SKIP_NAMES,
                )
                destination_root_lease.validate_lexical_binding()
            except Exception as exc:
                raise _migration_finalize_error(destination, exc) from exc
            context_token = _ACTIVE_OBS_MIGRATION_CAPABILITY.set((destination, owner_token))
            lease_context_token = _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.set(
                destination_root_lease
            )
            validator_context_token = _ACTIVE_OBS_MIGRATION_VALIDATOR.set(
                validate_transaction_locks
            )
            try:
                finalize_destination(destination)
                validate_transaction_locks()
            except Exception as exc:
                raise _migration_finalize_error(destination, exc) from exc
            finally:
                _ACTIVE_OBS_MIGRATION_VALIDATOR.reset(validator_context_token)
                _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.reset(lease_context_token)
                _ACTIVE_OBS_MIGRATION_CAPABILITY.reset(context_token)
            try:
                destination_root_lease.validate_lexical_binding()
                after_finalize_entries = _build_obs_tree_inventory(
                    destination,
                    root_lease=destination_root_lease,
                    ignored_root_names=OBS_FINALIZER_CALLBACK_INVENTORY_SKIP_NAMES,
                )
                destination_root_lease.validate_lexical_binding()
                _validate_finalize_changes(
                    before_finalize_entries,
                    after_finalize_entries,
                    destination,
                )
                _recover_owned_transaction_temporaries(
                    destination,
                    source_entries,
                    owner_token,
                    destination_root_lease=destination_root_lease,
                    validate_transaction=validate_transaction_locks,
                )
                validate_transaction_locks()
                _remove_obs_migration_journal_if_owned(
                    marker,
                    owner_token,
                    directory_lease=destination_root_lease,
                    validate_transaction=validate_transaction_locks,
                )
            except OBSMigrationError:
                raise
            except (OSError, _UnsafeOBSMigrationPathError) as exc:
                raise _migration_recovery_error(
                    destination,
                    f"最終化済みmarkerを安全に解除できません: {exc}",
                ) from exc
        return source
    finally:
        try:
            transaction_resources.close()
        finally:
            try:
                if source_probe is not None:
                    source_probe.close()
            finally:
                if destination_probe is not None:
                    destination_probe.close()


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

    def _stop_managed_processes_for_settings_recovery(self) -> None:
        unmanaged = self.process_manager.unmanaged_processes()
        if unmanaged:
            raise _settings_recovery_error(
                self.base_dir,
                "管理対象外のOBSが稼働中のため、停止してから設定復旧を再試行してください。",
            )
        self.process_manager.kill_stale_managed_processes()
        if self.process_manager.has_managed_process():
            raise _settings_recovery_error(
                self.base_dir,
                "管理対象OBSを停止できませんでした。OBSを手動終了してから再試行してください。",
            )
        unmanaged_after = self.process_manager.unmanaged_processes()
        if unmanaged_after:
            raise _settings_recovery_error(
                self.base_dir,
                "管理対象OBSの停止中に別のOBSが起動しました。終了してから再試行してください。",
            )

    @property
    def obs_exe(self) -> Path:
        return get_obs_executable_path(self.base_dir)

    def validate_layout(self, *, include_websocket: bool = True) -> bool:
        migration_root = _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.get()
        if migration_root is not None:
            if migration_root.path != self.base_dir:
                raise OBSPathSafetyError(
                    "OBS移行leaseとbootstrap baseが一致しません: "
                    f"{migration_root.path} != {self.base_dir}"
                )
            validator = _ACTIVE_OBS_MIGRATION_VALIDATOR.get()
            if validator is not None:
                validator()
            if not _is_valid_obs_installation_lease(migration_root):
                return False
            required_directories = [
                ("config",),
                ("config", "obs-studio"),
            ]
            if include_websocket:
                required_directories.extend(
                    [
                        ("config", "obs-studio", "plugin_config"),
                        (
                            "config",
                            "obs-studio",
                            "plugin_config",
                            "obs-websocket",
                        ),
                    ]
                )
            for relative_parts in required_directories:
                with migration_root.open_descendant_directory(relative_parts):
                    pass
            return True
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
        if _safe_config_file_exists(path):
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

    def _prepare_marker_write(self, path: Path, *, label: str) -> OBSConfigPlannedWrite:
        snapshot = preflight_obs_config_file(path, label=label)
        return OBSConfigPlannedWrite(
            snapshot=snapshot,
            payload=snapshot.payload if snapshot.payload is not None else b"",
        )

    def _prepare_obs_ini_write(self, ini_path: Path, *, label: str) -> OBSConfigPlannedWrite:
        snapshot = preflight_obs_config_file(ini_path, label=label)
        parser = new_obs_ini_parser()
        parse_failed = False
        normalized_encoding = False
        if snapshot.payload is not None:
            try:
                parser, normalized_encoding = _parse_obs_ini_payload(snapshot.payload)
            except (OBSPathSafetyError, OSError):
                raise
            except (UnicodeError, configparser.Error) as exc:
                self.logger.warning(
                    "Corrupt OBS %s will be regenerated: %s (%s)",
                    label,
                    ini_path,
                    exc,
                )
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
        if changed or snapshot.payload is None:
            buffer = io.StringIO()
            parser.write(buffer, space_around_delimiters=False)
            payload = buffer.getvalue().encode("utf-8")
        else:
            payload = snapshot.payload
        return OBSConfigPlannedWrite(snapshot=snapshot, payload=payload)

    def _prepare_websocket_write(
        self,
        port: int,
        password: str,
    ) -> OBSConfigPlannedWrite:
        config_path = get_obs_websocket_config_path(self.base_dir)
        snapshot = preflight_obs_config_file(
            config_path,
            label="obs-websocket設定file",
        )
        data: dict[str, Any] = {}
        if snapshot.payload is not None:
            try:
                loaded = json.loads(snapshot.payload.decode("utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OBSPathSafetyError, OSError):
                raise
            except (UnicodeError, json.JSONDecodeError) as exc:
                self.logger.warning(
                    "OBS websocket config was unreadable and will be reset: %s",
                    exc,
                    exc_info=True,
                )

        password_text = str(password or "")
        if not password_text:
            raise ValueError("obs-websocket password must not be empty.")
        desired = dict(data)
        desired.update(
            {
                "server_enabled": True,
                "server_port": max(1, min(65535, int(port))),
                "auth_required": True,
                "server_password": password_text,
            }
        )
        if snapshot.payload is not None and desired == data:
            payload = snapshot.payload
        else:
            payload = json.dumps(desired, indent=4, ensure_ascii=False).encode("utf-8")
        return OBSConfigPlannedWrite(snapshot=snapshot, payload=payload)

    def prepare_apply(self, port: int | None = None, password: str = "") -> OBSBootstrapApplyPlan:
        """Plan every bootstrap target without creating files or stopping OBS."""

        include_websocket = port is not None
        self.validate_layout(include_websocket=include_websocket)
        directories = [
            self.base_dir / "config",
            get_obs_config_dir(self.base_dir),
        ]
        marker = get_portable_marker_path(self.base_dir)
        legacy_marker = get_legacy_marker_path(self.base_dir)
        global_ini_path = get_obs_global_ini_path(self.base_dir)
        user_ini_path = get_obs_user_ini_path(self.base_dir)
        writes = [
            self._prepare_marker_write(marker, label=PORTABLE_OBS_MARKER_NAME),
            self._prepare_marker_write(legacy_marker, label=LEGACY_PORTABLE_OBS_MARKER_NAME),
            self._prepare_obs_ini_write(global_ini_path, label="global.ini"),
            self._prepare_obs_ini_write(user_ini_path, label="user.ini"),
        ]
        websocket_path: Path | None = None
        if port is not None:
            websocket_path = get_obs_websocket_config_path(self.base_dir)
            directories.extend(
                [
                    get_obs_config_dir(self.base_dir) / "plugin_config",
                    websocket_path.parent,
                ]
            )
            writes.append(self._prepare_websocket_write(port, password))
        return OBSBootstrapApplyPlan(
            transaction=OBSConfigTransactionPlan(
                base_dir=self.base_dir,
                directories=tuple(directories),
                writes=tuple(writes),
            ),
            marker=marker,
            config_dir=get_obs_config_dir(self.base_dir),
            global_ini_path=global_ini_path,
            user_ini_path=user_ini_path,
            websocket_path=websocket_path,
        )

    def preflight_apply(self, port: int | None = None) -> None:
        """Validate every full-bootstrap target without creating or changing it."""

        # Password contents are not persisted by planning. Use a throwaway value
        # solely to validate the complete websocket payload shape.
        self.prepare_apply(port=port, password="preflight-only" if port is not None else "")

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

    @_guard_obs_bootstrap_mutation(stop_managed_before_recovery=True)
    def apply(
        self,
        port: int | None = None,
        password: str = "",
        *,
        stop_managed_processes: bool = True,
    ) -> dict[str, Any]:
        plan = self.prepare_apply(port=port, password=password)
        changed_by_path = {
            _filesystem_path_key(write.snapshot.path): write.changed
            for write in plan.transaction.writes
        }
        execute_obs_config_transaction(
            plan.transaction,
            before_commit=(
                self._stop_managed_processes_for_settings_recovery
                if stop_managed_processes
                else None
            ),
            run_before_commit_on_noop=stop_managed_processes,
        )
        websocket_result = (
            None
            if plan.websocket_path is None
            else (
                changed_by_path.get(_filesystem_path_key(plan.websocket_path), False),
                plan.websocket_path,
            )
        )
        return {
            "marker": plan.marker,
            "config_dir": plan.config_dir,
            "global_ini_changed": changed_by_path.get(
                _filesystem_path_key(plan.global_ini_path),
                False,
            ),
            "global_ini_path": plan.global_ini_path,
            "user_ini_changed": changed_by_path.get(
                _filesystem_path_key(plan.user_ini_path),
                False,
            ),
            "user_ini_path": plan.user_ini_path,
            "websocket": websocket_result,
        }

    def bootstrap(self, port: int | None = None, password: str = "") -> dict[str, Any]:
        """Backward-compatible alias for the full repair/setup flow."""
        return self.apply(port=port, password=password)

    @_guard_obs_bootstrap_mutation()
    def ensure_portable_mode_marker(self) -> Path:
        primary_marker = get_portable_marker_path(self.base_dir)
        legacy_marker = get_legacy_marker_path(self.base_dir)
        execute_obs_config_transaction(
            OBSConfigTransactionPlan(
                base_dir=self.base_dir,
                directories=(),
                writes=(
                    self._prepare_marker_write(
                        primary_marker,
                        label=PORTABLE_OBS_MARKER_NAME,
                    ),
                    self._prepare_marker_write(
                        legacy_marker,
                        label=LEGACY_PORTABLE_OBS_MARKER_NAME,
                    ),
                ),
            )
        )
        return primary_marker

    @_guard_obs_bootstrap_mutation()
    def ensure_config_dir(self) -> Path:
        self._prepare_write_layout(include_websocket=False)
        config_dir = get_obs_config_dir(self.base_dir)
        _ensure_safe_directory_chain(config_dir)
        return config_dir

    @_guard_obs_bootstrap_mutation(stop_managed_before_recovery=True)
    def ensure_global_ini(self, *, stop_managed_processes: bool = True) -> tuple[bool, Path]:
        # OBS reads global.ini only at startup and may rewrite it on exit.
        # Stop every process from this managed portable tree before patching.
        path = get_obs_global_ini_path(self.base_dir)
        write = self._prepare_obs_ini_write(path, label="global.ini")
        execute_obs_config_transaction(
            OBSConfigTransactionPlan(
                base_dir=self.base_dir,
                directories=(path.parent,),
                writes=(write,),
            ),
            before_commit=(
                self._stop_managed_processes_for_settings_recovery
                if stop_managed_processes
                else None
            ),
            run_before_commit_on_noop=stop_managed_processes,
        )
        return write.changed, path

    @_guard_obs_bootstrap_mutation(stop_managed_before_recovery=True)
    def ensure_user_ini(self, *, stop_managed_processes: bool = True) -> tuple[bool, Path]:
        # OBS 32.x reads UI startup and tray flags from user.ini.
        path = get_obs_user_ini_path(self.base_dir)
        write = self._prepare_obs_ini_write(path, label="user.ini")
        execute_obs_config_transaction(
            OBSConfigTransactionPlan(
                base_dir=self.base_dir,
                directories=(path.parent,),
                writes=(write,),
            ),
            before_commit=(
                self._stop_managed_processes_for_settings_recovery
                if stop_managed_processes
                else None
            ),
            run_before_commit_on_noop=stop_managed_processes,
        )
        return write.changed, path

    def _ensure_obs_ini(self, ini_path: Path, label: str) -> tuple[bool, Path]:
        with obs_config_mutation_guard(self.base_dir):
            write = self._prepare_obs_ini_write(ini_path, label=label)
            execute_obs_config_transaction(
                OBSConfigTransactionPlan(
                    base_dir=self.base_dir,
                    directories=(ini_path.parent,),
                    writes=(write,),
                )
            )
            return write.changed, ini_path

    @_guard_obs_bootstrap_mutation(stop_managed_before_recovery=True)
    def ensure_websocket_config(
        self,
        port: int,
        password: str,
        *,
        stop_managed_processes: bool = True,
    ) -> tuple[bool, Path]:
        config_path = get_obs_websocket_config_path(self.base_dir)
        write = self._prepare_websocket_write(port, password)
        execute_obs_config_transaction(
            OBSConfigTransactionPlan(
                base_dir=self.base_dir,
                directories=(config_path.parent,),
                writes=(write,),
            ),
            before_commit=(
                self._stop_managed_processes_for_settings_recovery
                if stop_managed_processes
                else None
            ),
            run_before_commit_on_noop=stop_managed_processes,
        )
        return write.changed, config_path
