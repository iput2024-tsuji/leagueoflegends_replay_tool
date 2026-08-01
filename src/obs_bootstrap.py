from __future__ import annotations

import configparser
import ctypes
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
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

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
OBS_FINALIZE_INVENTORY_SKIP_NAMES = frozenset(
    {OBS_COPY_IN_PROGRESS_MARKER_NAME, OBS_COPY_LOCK_NAME}
)
OBS_FINALIZER_CALLBACK_INVENTORY_SKIP_NAMES = frozenset({OBS_COPY_LOCK_NAME})
OBS_COPY_JOURNAL_SCHEMA_VERSION = 3
OBS_MIGRATION_PHASE_COPYING = "copying"
OBS_MIGRATION_PHASE_FINALIZE_PENDING = "finalize_pending"
OBS_COPY_JOURNAL_MAX_BYTES = 64 * 1024
OBS_BOOTSTRAP_CONFIG_MAX_BYTES = 16 * 1024 * 1024
WINDOWS_LOCK_CONTENTION_ERRORS = frozenset({32, 33})
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
OBS_TRANSACTION_TEMP_COPY = "copy"
OBS_TRANSACTION_TEMP_WRITE = "write"
OBS_TRANSACTION_TEMP_JOURNAL = "journal"
WINDOWS_RESERVED_PATH_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
    | {f"com{number}" for number in "¹²³"}
    | {f"lpt{number}" for number in "¹²³"}
)


def _filesystem_name_key(name: str) -> str:
    """Match the native filesystem's spelling rules used by this transaction."""

    return name.casefold() if os.name == "nt" else name


def _filesystem_parts_key(parts: Iterable[str]) -> tuple[str, ...]:
    return tuple(_filesystem_name_key(part) for part in parts)


def _filesystem_path_key(path: str | Path) -> str:
    return _filesystem_name_key(str(_absolute_path(path)))


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


@dataclass(frozen=True)
class _OBSTransactionTemporaryDescriptor:
    kind: str
    target_name: str
    owner_token: str
    path: Path


class OBSPathSafetyError(RuntimeError):
    """Raised when an OBS path could escape the managed lexical boundary."""


class _UnsafeOBSMigrationPathError(OBSPathSafetyError):
    pass


class _OBSMigrationLockBusyError(RuntimeError):
    pass


if os.name == "nt":
    _WINDOWS_INVALID_HANDLE = ctypes.c_void_p(-1).value
    _WINDOWS_GENERIC_READ = 0x80000000
    _WINDOWS_GENERIC_WRITE = 0x40000000
    _WINDOWS_DELETE = 0x00010000
    _WINDOWS_SYNCHRONIZE = 0x00100000
    _WINDOWS_MAXIMUM_ALLOWED = 0x02000000
    _WINDOWS_FILE_READ_DATA = 0x0001
    _WINDOWS_FILE_WRITE_DATA = 0x0002
    _WINDOWS_FILE_LIST_DIRECTORY = 0x0001
    _WINDOWS_FILE_ADD_FILE = 0x0002
    _WINDOWS_FILE_ADD_SUBDIRECTORY = 0x0004
    _WINDOWS_FILE_TRAVERSE = 0x0020
    _WINDOWS_FILE_READ_ATTRIBUTES = 0x0080
    _WINDOWS_FILE_WRITE_ATTRIBUTES = 0x0100
    _WINDOWS_FILE_SHARE_READ_WRITE = 0x0001 | 0x0002
    _WINDOWS_FILE_SHARE_ALL = _WINDOWS_FILE_SHARE_READ_WRITE | 0x0004
    _WINDOWS_OPEN_EXISTING = 3
    _WINDOWS_FILE_OPEN = 1
    _WINDOWS_FILE_CREATE = 2
    _WINDOWS_FILE_OPEN_IF = 3
    _WINDOWS_FILE_DIRECTORY_FILE = 0x00000001
    _WINDOWS_FILE_NON_DIRECTORY_FILE = 0x00000040
    _WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _WINDOWS_FILE_OPEN_REPARSE_POINT = 0x00200000
    _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _WINDOWS_OBJ_CASE_INSENSITIVE = 0x00000040
    _WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
    _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _WINDOWS_FILE_RENAME_INFO = 3
    _WINDOWS_FILE_DISPOSITION_INFO = 4
    _WINDOWS_NT_FILE_RENAME_INFORMATION = 10

    class _WindowsUnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _WindowsObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_WindowsUnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _WindowsIOStatusBlock(ctypes.Structure):
        _fields_ = [
            ("Status", ctypes.c_void_p),
            ("Information", ctypes.c_size_t),
        ]

    class _WindowsByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _WindowsFileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BYTE)]

    class _WindowsFileRenameInfoHeader(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]


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


if os.name == "nt":
    _WINDOWS_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _WINDOWS_NTDLL = ctypes.WinDLL("ntdll", use_last_error=True)

    _WINDOWS_KERNEL32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _WINDOWS_KERNEL32.CreateFileW.restype = wintypes.HANDLE
    _WINDOWS_KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _WINDOWS_KERNEL32.CloseHandle.restype = wintypes.BOOL
    _WINDOWS_KERNEL32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_WindowsByHandleFileInformation),
    ]
    _WINDOWS_KERNEL32.GetFileInformationByHandle.restype = wintypes.BOOL
    _WINDOWS_KERNEL32.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _WINDOWS_KERNEL32.SetFileInformationByHandle.restype = wintypes.BOOL
    _WINDOWS_KERNEL32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _WINDOWS_KERNEL32.FlushFileBuffers.restype = wintypes.BOOL
    _WINDOWS_NTDLL.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_WindowsObjectAttributes),
        ctypes.POINTER(_WindowsIOStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    ]
    _WINDOWS_NTDLL.NtCreateFile.restype = ctypes.c_long
    _WINDOWS_NTDLL.NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_WindowsIOStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    ]
    _WINDOWS_NTDLL.NtSetInformationFile.restype = ctypes.c_long
    _WINDOWS_NTDLL.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    _WINDOWS_NTDLL.RtlNtStatusToDosError.restype = wintypes.ULONG


def _validate_single_path_component(name: str) -> None:
    unsafe_characters = '<>:"/\\|?*'
    device_stem = name.split(".", 1)[0].casefold()
    if (
        not name
        or name in {".", ".."}
        or any(character in unsafe_characters or ord(character) < 32 for character in name)
        or name.endswith((".", " "))
        or device_stem in WINDOWS_RESERVED_PATH_NAMES
    ):
        raise _UnsafeOBSMigrationPathError(f"安全でないpath componentがあります: {name}")


def _windows_raise_ntstatus(status: int, path: str) -> None:
    error_code = int(_WINDOWS_NTDLL.RtlNtStatusToDosError(status))
    error = ctypes.WinError(error_code)
    if error_code in {2, 3}:
        raise FileNotFoundError(error_code, str(error), path)
    if error_code in {80, 183}:
        raise FileExistsError(error_code, str(error), path)
    raise error


def _windows_directory_access(*, mutable: bool) -> int:
    access = (
        _WINDOWS_FILE_LIST_DIRECTORY
        | _WINDOWS_FILE_TRAVERSE
        | _WINDOWS_FILE_READ_ATTRIBUTES
        | _WINDOWS_SYNCHRONIZE
    )
    if mutable:
        access |= (
            _WINDOWS_FILE_ADD_FILE
            | _WINDOWS_FILE_ADD_SUBDIRECTORY
            | _WINDOWS_FILE_WRITE_ATTRIBUTES
        )
    return access


def _windows_open_absolute_directory(path: Path, *, mutable: bool = False) -> int:
    handle = _WINDOWS_KERNEL32.CreateFileW(
        str(path),
        _windows_directory_access(mutable=mutable),
        _WINDOWS_FILE_SHARE_READ_WRITE,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle_value = int(handle) if handle is not None else 0
    if handle_value == _WINDOWS_INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle_value


def _windows_open_relative_handle(
    parent_handle: int,
    name: str,
    *,
    desired_access: int,
    disposition: int,
    directory: bool,
    share_delete: bool = True,
) -> int:
    _validate_single_path_component(name)
    name_buffer = ctypes.create_unicode_buffer(name)
    name_bytes = len(name.encode("utf-16-le"))
    unicode_name = _WindowsUnicodeString(
        Length=name_bytes,
        MaximumLength=name_bytes + 2,
        Buffer=ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = _WindowsObjectAttributes(
        Length=ctypes.sizeof(_WindowsObjectAttributes),
        RootDirectory=wintypes.HANDLE(parent_handle),
        ObjectName=ctypes.pointer(unicode_name),
        Attributes=_WINDOWS_OBJ_CASE_INSENSITIVE,
        SecurityDescriptor=None,
        SecurityQualityOfService=None,
    )
    io_status = _WindowsIOStatusBlock()
    handle = wintypes.HANDLE()
    create_options = (
        (_WINDOWS_FILE_DIRECTORY_FILE if directory else _WINDOWS_FILE_NON_DIRECTORY_FILE)
        | _WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT
        | _WINDOWS_FILE_OPEN_REPARSE_POINT
    )
    status = int(
        _WINDOWS_NTDLL.NtCreateFile(
            ctypes.byref(handle),
            desired_access | _WINDOWS_SYNCHRONIZE,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            _WINDOWS_FILE_ATTRIBUTE_NORMAL,
            0x0001 | 0x0002 | (0x0004 if share_delete else 0),
            disposition,
            create_options,
            None,
            0,
        )
    )
    if status < 0:
        _windows_raise_ntstatus(status, name)
    if not handle.value:
        raise OSError(f"NtCreateFile returned an empty handle: {name}")
    return int(handle.value)


def _windows_handle_details(handle: int) -> tuple[tuple[int, int, int], int, int, int]:
    information = _WindowsByHandleFileInformation()
    if not _WINDOWS_KERNEL32.GetFileInformationByHandle(
        wintypes.HANDLE(handle),
        ctypes.byref(information),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = int(information.dwFileAttributes)
    mode = stat.S_IFDIR if attributes & int(getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)) else stat.S_IFREG
    identity = (
        int(information.dwVolumeSerialNumber),
        (int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow),
        mode,
    )
    size = (int(information.nFileSizeHigh) << 32) | int(information.nFileSizeLow)
    return identity, size, int(information.nNumberOfLinks), attributes


def _windows_close_handle(handle: int) -> None:
    if not _WINDOWS_KERNEL32.CloseHandle(wintypes.HANDLE(handle)):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_rename_open_file(
    file_handle: int,
    destination_directory_handle: int,
    target_name: str,
) -> None:
    _validate_single_path_component(target_name)
    name_bytes = target_name.encode("utf-16-le")
    root_offset = _WindowsFileRenameInfoHeader.RootDirectory.offset
    length_offset = _WindowsFileRenameInfoHeader.FileNameLength.offset
    buffer_offset = _WindowsFileRenameInfoHeader.FileName.offset
    buffer_size = buffer_offset + len(name_bytes)
    buffer = ctypes.create_string_buffer(buffer_size)
    ctypes.memset(buffer, 0, len(buffer))
    ctypes.cast(buffer, ctypes.POINTER(wintypes.BYTE))[0] = 1
    ctypes.cast(
        ctypes.byref(buffer, root_offset),
        ctypes.POINTER(wintypes.HANDLE),
    )[0] = wintypes.HANDLE(destination_directory_handle)
    ctypes.cast(
        ctypes.byref(buffer, length_offset),
        ctypes.POINTER(wintypes.DWORD),
    )[0] = len(name_bytes)
    ctypes.memmove(ctypes.byref(buffer, buffer_offset), name_bytes, len(name_bytes))
    io_status = _WindowsIOStatusBlock()
    status = int(_WINDOWS_NTDLL.NtSetInformationFile(
        wintypes.HANDLE(file_handle),
        ctypes.byref(io_status),
        buffer,
        len(buffer),
        _WINDOWS_NT_FILE_RENAME_INFORMATION,
    ))
    if status < 0:
        _windows_raise_ntstatus(status, target_name)


def _windows_mark_open_file_for_deletion(file_handle: int) -> None:
    disposition = _WindowsFileDispositionInfo(DeleteFile=True)
    if not _WINDOWS_KERNEL32.SetFileInformationByHandle(
        wintypes.HANDLE(file_handle),
        _WINDOWS_FILE_DISPOSITION_INFO,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise ctypes.WinError(ctypes.get_last_error())


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


class _OBSDirectoryLease:
    """A verified directory handle used as the root of mutation operations."""

    def __init__(
        self,
        path: Path,
        native_handle: int,
        identity: tuple[int, int, int],
        *,
        mutable: bool,
    ) -> None:
        self.path = _absolute_path(path)
        self._native_handle = native_handle
        self.identity = identity
        self.mutable = mutable
        self._closed = False

    @classmethod
    def open_absolute(
        cls,
        path: str | Path,
        *,
        create: bool = False,
        mutable: bool = False,
    ) -> _OBSDirectoryLease:
        absolute = _absolute_path(path)
        anchor = Path(absolute.anchor)
        if not anchor.anchor:
            raise _UnsafeOBSMigrationPathError(f"absolute directoryではありません: {absolute}")
        lease = cls._open_anchor(anchor)
        try:
            for part in absolute.parts[1:]:
                child = lease.open_child_directory(part, create=create)
                lease.close()
                lease = child
            if mutable and not lease.mutable:
                mutable_lease = lease.mutable_clone()
                lease.close()
                lease = mutable_lease
            lease.validate_lexical_binding()
            return lease
        except Exception:
            lease.close()
            raise

    @classmethod
    def _open_anchor(cls, anchor: Path) -> _OBSDirectoryLease:
        if os.name == "nt":
            handle = _windows_open_absolute_directory(anchor)
            try:
                identity, _size, _links, attributes = _windows_handle_details(handle)
                if attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
                    raise _UnsafeOBSMigrationPathError(f"reparse pointは利用できません: {anchor}")
                if identity[2] != stat.S_IFDIR:
                    raise _UnsafeOBSMigrationPathError(f"通常ディレクトリではありません: {anchor}")
                return cls(anchor, handle, identity, mutable=False)
            except Exception:
                _windows_close_handle(handle)
                raise
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(anchor, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            os.close(descriptor)
            raise _UnsafeOBSMigrationPathError(f"通常ディレクトリではありません: {anchor}")
        return cls(anchor, descriptor, _file_identity(opened), mutable=False)

    def mutable_clone(self) -> _OBSDirectoryLease:
        self.validate_lexical_binding()
        if os.name == "nt":
            handle = _windows_open_absolute_directory(self.path, mutable=True)
            try:
                identity, _size, _links, attributes = _windows_handle_details(handle)
                if attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
                    raise _UnsafeOBSMigrationPathError(f"reparse pointは利用できません: {self.path}")
            except Exception:
                _windows_close_handle(handle)
                raise
        else:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            handle = os.open(self.path, flags)
            identity = _file_identity(os.fstat(handle))
        if identity != self.identity:
            if os.name == "nt":
                _windows_close_handle(handle)
            else:
                os.close(handle)
            raise _UnsafeOBSMigrationPathError(f"mutable lease取得中にancestorが入れ替わりました: {self.path}")
        return _OBSDirectoryLease(self.path, handle, identity, mutable=True)

    @property
    def native_handle(self) -> int:
        if self._closed:
            raise _UnsafeOBSMigrationPathError(f"directory handleは既に閉じられています: {self.path}")
        return self._native_handle

    def close(self) -> None:
        if self._closed:
            return
        handle = self._native_handle
        self._closed = True
        if os.name == "nt":
            _windows_close_handle(handle)
        else:
            os.close(handle)

    def __enter__(self) -> _OBSDirectoryLease:
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def validate_lexical_binding(self) -> None:
        if os.name == "nt":
            current_handle = _windows_open_absolute_directory(self.path, mutable=False)
            try:
                identity, _size, _links, attributes = _windows_handle_details(current_handle)
            finally:
                _windows_close_handle(current_handle)
            if attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
                raise _UnsafeOBSMigrationPathError(f"reparse pointは利用できません: {self.path}")
        else:
            current = os.stat(self.path, follow_symlinks=False)
            if _is_reparse_point(current) or not stat.S_ISDIR(current.st_mode):
                raise _UnsafeOBSMigrationPathError(f"通常ディレクトリではありません: {self.path}")
            identity = _file_identity(current)
            opened = os.fstat(self.native_handle)
            if _file_identity(opened) != self.identity:
                raise _UnsafeOBSMigrationPathError(f"directory handle identityが変化しました: {self.path}")
        if identity != self.identity:
            raise _UnsafeOBSMigrationPathError(f"ancestor directoryが入れ替わりました: {self.path}")

    def _open_relative_directory_handle(
        self,
        name: str,
        *,
        create: bool,
        mutable: bool,
    ) -> int:
        _validate_single_path_component(name)
        if os.name == "nt":
            try:
                return _windows_open_relative_handle(
                    self.native_handle,
                    name,
                    desired_access=_windows_directory_access(mutable=mutable),
                    disposition=_WINDOWS_FILE_OPEN,
                    directory=True,
                    share_delete=False,
                )
            except FileNotFoundError:
                if not create:
                    raise
                mutable_parent = self if self.mutable else self.mutable_clone()
                try:
                    handle = _windows_open_relative_handle(
                        mutable_parent.native_handle,
                        name,
                        desired_access=_windows_directory_access(mutable=mutable),
                        disposition=_WINDOWS_FILE_CREATE,
                        directory=True,
                        share_delete=False,
                    )
                    mutable_parent.flush_metadata()
                    return handle
                finally:
                    if mutable_parent is not self:
                        mutable_parent.close()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

        def open_directory_handle() -> int:
            try:
                return os.open(name, flags, dir_fd=self.native_handle)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise _UnsafeOBSMigrationPathError(
                        "symbolic link／reparse pointまたは通常ディレクトリではありません: "
                        f"{self.path / name}"
                    ) from exc
                raise

        try:
            return open_directory_handle()
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(name, dir_fd=self.native_handle)
            except FileExistsError as exc:
                raise _UnsafeOBSMigrationPathError(
                    f"directory作成中に別entryが挿入されました: {self.path / name}"
                ) from exc
            self.flush_metadata()
            return open_directory_handle()

    def open_child_directory(
        self,
        name: str,
        *,
        create: bool = False,
        mutable: bool = False,
    ) -> _OBSDirectoryLease:
        self.validate_lexical_binding()
        handle = self._open_relative_directory_handle(
            name,
            create=create,
            mutable=mutable,
        )
        child_path = self.path / name
        try:
            if os.name == "nt":
                identity, _size, _links, attributes = _windows_handle_details(handle)
                if attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
                    raise _UnsafeOBSMigrationPathError(f"reparse pointは利用できません: {child_path}")
                if identity[2] != stat.S_IFDIR:
                    raise _UnsafeOBSMigrationPathError(f"通常ディレクトリではありません: {child_path}")
            else:
                opened = os.fstat(handle)
                if _is_reparse_point(opened) or not stat.S_ISDIR(opened.st_mode):
                    raise _UnsafeOBSMigrationPathError(f"通常ディレクトリではありません: {child_path}")
                identity = _file_identity(opened)
            child = _OBSDirectoryLease(
                child_path,
                handle,
                identity,
                mutable=mutable,
            )
            child.validate_lexical_binding()
            self.validate_lexical_binding()
            return child
        except Exception:
            if os.name == "nt":
                _windows_close_handle(handle)
            else:
                os.close(handle)
            raise

    def open_descendant_directory(
        self,
        relative_parts: tuple[str, ...],
        *,
        create: bool = False,
        mutable: bool = False,
    ) -> _OBSDirectoryLease:
        if not relative_parts:
            raise ValueError("relative directory path must not be empty")
        current: _OBSDirectoryLease | None = None
        parent = self
        try:
            for index, part in enumerate(relative_parts):
                child = parent.open_child_directory(
                    part,
                    create=create,
                    mutable=mutable and index == len(relative_parts) - 1,
                )
                if current is not None:
                    current.close()
                current = child
                parent = child
            if current is None:
                raise AssertionError("relative directory traversal did not open a directory")
            return current
        except Exception:
            if current is not None:
                current.close()
            raise

    def _relative_file_identity(self, name: str) -> tuple[int, int, int]:
        descriptor = self.open_file(name, write=False, create_exclusive=False, delete=False)
        try:
            return _file_identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)

    def relative_file_identity_or_none(
        self,
        name: str,
    ) -> tuple[int, int, int] | None:
        try:
            return self._relative_file_identity(name)
        except FileNotFoundError:
            return None

    def open_file(
        self,
        name: str,
        *,
        write: bool,
        create_exclusive: bool,
        delete: bool = False,
        share_delete: bool = True,
    ) -> int:
        _validate_single_path_component(name)
        if (write or create_exclusive or delete) and not self.mutable:
            raise _UnsafeOBSMigrationPathError(
                f"read-only directory leaseではfileを変更できません: {self.path / name}"
            )
        self.validate_lexical_binding()
        if os.name == "nt":
            desired_access = _WINDOWS_FILE_READ_DATA | _WINDOWS_FILE_READ_ATTRIBUTES
            if write:
                desired_access |= _WINDOWS_FILE_WRITE_DATA | _WINDOWS_FILE_WRITE_ATTRIBUTES
            if delete:
                desired_access |= _WINDOWS_DELETE
            disposition = _WINDOWS_FILE_CREATE if create_exclusive else _WINDOWS_FILE_OPEN
            handle = _windows_open_relative_handle(
                self.native_handle,
                name,
                desired_access=desired_access,
                disposition=disposition,
                directory=False,
                share_delete=share_delete,
            )
            try:
                identity, _size, links, attributes = _windows_handle_details(handle)
                if attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
                    raise _UnsafeOBSMigrationPathError(f"reparse pointは利用できません: {self.path / name}")
                if identity[2] != stat.S_IFREG:
                    raise _UnsafeOBSMigrationPathError(f"通常ファイルではありません: {self.path / name}")
                if links != 1:
                    raise _UnsafeOBSMigrationPathError(f"hardlinkされたファイルは利用できません: {self.path / name}")
                flags = (os.O_RDWR if write else os.O_RDONLY) | int(getattr(os, "O_BINARY", 0))
                descriptor = msvcrt.open_osfhandle(handle, flags)
                handle = 0
            finally:
                if handle:
                    _windows_close_handle(handle)
        else:
            flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NOFOLLOW
            if create_exclusive:
                flags |= os.O_CREAT | os.O_EXCL
            descriptor = os.open(name, flags, 0o600, dir_fd=self.native_handle)
            opened = os.fstat(descriptor)
            if _is_reparse_point(opened) or not stat.S_ISREG(opened.st_mode) or int(opened.st_nlink) != 1:
                os.close(descriptor)
                raise _UnsafeOBSMigrationPathError(f"安全な通常ファイルではありません: {self.path / name}")
        try:
            opened_identity = _file_identity(os.fstat(descriptor))
            lexical = _validate_existing_entry(self.path / name, expected_kind="file")
            if _file_identity(lexical) != opened_identity:
                raise _UnsafeOBSMigrationPathError(f"relative open後にidentityが変化しました: {self.path / name}")
            self.validate_lexical_binding()
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def replace_open_file(self, descriptor: int, temporary_name: str, target_name: str) -> None:
        _validate_single_path_component(temporary_name)
        _validate_single_path_component(target_name)
        if not self.mutable:
            raise _UnsafeOBSMigrationPathError(
                f"read-only directory leaseではreplaceできません: {self.path / target_name}"
            )
        self.validate_lexical_binding()
        expected_identity = _file_identity(os.fstat(descriptor))
        if self._relative_file_identity(temporary_name) != expected_identity:
            raise _UnsafeOBSMigrationPathError(
                f"replace前に一時file identityが変化しました: {self.path / temporary_name}"
            )
        if os.name == "nt":
            _windows_rename_open_file(
                msvcrt.get_osfhandle(descriptor),
                self.native_handle,
                target_name,
            )
        else:
            os.rename(
                temporary_name,
                target_name,
                src_dir_fd=self.native_handle,
                dst_dir_fd=self.native_handle,
            )
        if self._relative_file_identity(target_name) != expected_identity:
            raise _UnsafeOBSMigrationPathError(
                f"replace後に確定file identityが変化しました: {self.path / target_name}"
            )
        os.fsync(descriptor)
        self.flush_metadata()
        self.validate_lexical_binding()

    def unlink_file(self, name: str, *, expected_identity: tuple[int, int, int]) -> None:
        _validate_single_path_component(name)
        if not self.mutable:
            raise _UnsafeOBSMigrationPathError(
                f"read-only directory leaseではunlinkできません: {self.path / name}"
            )
        self.validate_lexical_binding()
        descriptor = self.open_file(name, write=False, create_exclusive=False, delete=True)
        try:
            if _file_identity(os.fstat(descriptor)) != expected_identity:
                raise _UnsafeOBSMigrationPathError(
                    f"unlink前にfile identityが変化しました: {self.path / name}"
                )
            if os.name == "nt":
                _windows_mark_open_file_for_deletion(msvcrt.get_osfhandle(descriptor))
            else:
                os.unlink(name, dir_fd=self.native_handle)
        finally:
            os.close(descriptor)
        if self.relative_file_identity_or_none(name) is not None:
            raise _UnsafeOBSMigrationPathError(
                f"unlink後もfileが残っています: {self.path / name}"
            )
        self.flush_metadata()
        self.validate_lexical_binding()

    def flush_metadata(self) -> bool:
        """Flush directory metadata when supported; Windows relies on flushed file handles."""

        if os.name == "nt":
            if _WINDOWS_KERNEL32.FlushFileBuffers(wintypes.HANDLE(self.native_handle)):
                return True
            error_code = ctypes.get_last_error()
            if error_code in {5, 6, 50}:
                return False
            raise ctypes.WinError(error_code)
        os.fsync(self.native_handle)
        return True


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


def _supports_posix_handle_relative_migration() -> bool:
    required_dir_fd_functions = (os.open, os.mkdir, os.rename, os.unlink, os.stat)
    return (
        bool(getattr(os, "O_DIRECTORY", 0))
        and bool(getattr(os, "O_NOFOLLOW", 0))
        and os.scandir in os.supports_fd
        and all(function in os.supports_dir_fd for function in required_dir_fd_functions)
    )


def _supports_handle_relative_migration() -> bool:
    if os.name == "nt":
        return all(
            name in globals()
            for name in (
                "_WINDOWS_KERNEL32",
                "_WINDOWS_NTDLL",
                "_windows_open_relative_handle",
                "_windows_rename_open_file",
                "_windows_mark_open_file_for_deletion",
            )
        )
    return _supports_posix_handle_relative_migration()


class _OBSInterProcessLock:
    """Small cross-platform advisory lock held for the whole migration."""

    def __init__(self, path: Path) -> None:
        self.path = _absolute_path(path)
        self._file: Any | None = None
        self._directory_lease: _OBSDirectoryLease | None = None

    def acquire(
        self,
        *,
        create_parent: bool = True,
        directory_lease: _OBSDirectoryLease | None = None,
        initialize_empty: bool = True,
    ) -> bool:
        if not create_parent and not _path_lexists(self.path.parent):
            return True
        directory: _OBSDirectoryLease | None = None
        descriptor: int | None = None
        lock_file: Any | None = None
        try:
            if directory_lease is not None:
                if directory_lease.path != self.path.parent:
                    raise _UnsafeOBSMigrationPathError(
                        "lock directory leaseがlock parentと一致しません: "
                        f"{directory_lease.path} != {self.path.parent}"
                    )
                directory_lease.validate_lexical_binding()
                directory = directory_lease.mutable_clone()
            else:
                directory = _OBSDirectoryLease.open_absolute(
                    self.path.parent,
                    create=create_parent,
                    mutable=True,
                )
            if (
                not create_parent
                and directory.relative_file_identity_or_none(self.path.name) is None
            ):
                return True

            def open_existing_lock() -> tuple[int, os.stat_result]:
                try:
                    opened_descriptor = directory.open_file(
                        self.path.name,
                        write=True,
                        create_exclusive=False,
                        share_delete=False,
                    )
                except OSError as exc:
                    if _is_windows_lock_contention_error(exc):
                        raise _OBSMigrationLockBusyError from exc
                    raise
                return opened_descriptor, os.fstat(opened_descriptor)

            try:
                try:
                    lock_identity = directory.relative_file_identity_or_none(
                        self.path.name
                    )
                except OSError as exc:
                    if _is_windows_lock_contention_error(exc):
                        raise _OBSMigrationLockBusyError from exc
                    raise
                if lock_identity is not None:
                    descriptor, before = open_existing_lock()
                else:
                    try:
                        descriptor = directory.open_file(
                            self.path.name,
                            write=True,
                            create_exclusive=True,
                            share_delete=False,
                        )
                        before = os.fstat(descriptor)
                    except FileExistsError:
                        descriptor, before = open_existing_lock()
            except _OBSMigrationLockBusyError:
                return False
            opened = os.fstat(descriptor)
            if _file_identity(opened) != _file_identity(before):
                raise _UnsafeOBSMigrationPathError(f"lock open中にidentityが変化しました: {self.path}")
            if directory._relative_file_identity(self.path.name) != _file_identity(opened):
                raise _UnsafeOBSMigrationPathError(f"lock path identityが変化しました: {self.path}")
            directory.validate_lexical_binding()
            os.lseek(descriptor, 0, os.SEEK_SET)
            lock_file = os.fdopen(descriptor, "r+b", buffering=0)
            descriptor = None
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return False
                raise
            current_size = int(os.fstat(lock_file.fileno()).st_size)
            if current_size == 0 and initialize_empty:
                lock_file.seek(0)
                _write_all(lock_file.fileno(), b"\0")
                os.fsync(lock_file.fileno())
                directory.flush_metadata()
                opened = os.fstat(lock_file.fileno())
                current_size = int(opened.st_size)
            if current_size != 1:
                raise _UnsafeOBSMigrationPathError(
                    f"lock fileのsizeが不正です: {self.path}"
                )
            lock_file.seek(0)
            if lock_file.read(1) != b"\0":
                raise _UnsafeOBSMigrationPathError(f"lock fileの内容が不正です: {self.path}")
            if _file_identity(os.fstat(lock_file.fileno())) != _file_identity(opened):
                raise _UnsafeOBSMigrationPathError(f"lock中にidentityが変化しました: {self.path}")
            if directory._relative_file_identity(self.path.name) != _file_identity(opened):
                raise _UnsafeOBSMigrationPathError(f"lock path identityが変化しました: {self.path}")
            directory.validate_lexical_binding()
            self._file = lock_file
            self._directory_lease = directory
            lock_file = None
            directory = None
            return True
        finally:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            finally:
                try:
                    if lock_file is not None:
                        lock_file.close()
                finally:
                    if directory is not None:
                        directory.close()

    @property
    def directory_lease(self) -> _OBSDirectoryLease:
        if self._directory_lease is None:
            raise _UnsafeOBSMigrationPathError(
                f"lockに対応するdirectory leaseがありません: {self.path}"
            )
        return self._directory_lease

    def validate_ownership(self) -> None:
        lock_file = self._file
        directory = self._directory_lease
        if lock_file is None or directory is None:
            raise _UnsafeOBSMigrationPathError(f"lockを保持していません: {self.path}")
        opened = os.fstat(lock_file.fileno())
        if int(opened.st_size) != 1:
            raise _UnsafeOBSMigrationPathError(f"保持中lock fileのsizeが不正です: {self.path}")
        if directory._relative_file_identity(self.path.name) != _file_identity(opened):
            raise _UnsafeOBSMigrationPathError(f"保持中lock path identityが変化しました: {self.path}")
        directory.validate_lexical_binding()
        current_position = lock_file.tell()
        try:
            lock_file.seek(0)
            if lock_file.read(1) != b"\0":
                raise _UnsafeOBSMigrationPathError(f"保持中lock fileの内容が不正です: {self.path}")
        finally:
            lock_file.seek(current_position)

    def release(self) -> None:
        lock_file = self._file
        self._file = None
        directory = self._directory_lease
        self._directory_lease = None
        if lock_file is None:
            if directory is not None:
                directory.close()
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
            try:
                lock_file.close()
            finally:
                if directory is not None:
                    directory.close()


def is_obs_copy_in_progress(base_dir: str | Path) -> bool:
    base_path = _absolute_path(base_dir)
    marker = get_obs_copy_in_progress_marker(base_path)
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
            has_temporary = _has_transaction_temporary_name_under_lease(
                destination_probe
            )
            return (
                has_temporary
                or destination_probe.relative_file_identity_or_none(marker.name)
                is not None
                or destination_probe.relative_file_identity_or_none(lock_path.name)
                is not None
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
    allowed_by_key = {_filesystem_path_key(path): path for path in allowed_sources}
    source_key = _filesystem_path_key(source)
    if source_key not in allowed_by_key:
        raise _migration_recovery_error(destination, f"markerの移行元は許可済みlegacy候補ではありません: {source}")
    return allowed_by_key[source_key]


def _active_migration_parent_for_file(
    path: Path,
) -> tuple[_OBSDirectoryLease, bool] | None:
    root_lease = _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.get()
    if root_lease is None:
        return None
    absolute = _absolute_path(path)
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
    return _directory_for_descendant_parent(
        root_lease,
        absolute,
        mutable=False,
    )


def _safe_config_file_exists(path: Path) -> bool:
    active_parent = _active_migration_parent_for_file(path)
    if active_parent is None:
        return _path_lexists(path)
    parent, parent_owned = active_parent
    try:
        return parent.relative_file_identity_or_none(path.name) is not None
    finally:
        if parent_owned:
            parent.close()


def _read_safe_file_bytes(
    path: Path,
    *,
    max_bytes: int,
    label: str,
) -> tuple[bytes, tuple[int, int, int]]:
    active_parent = _active_migration_parent_for_file(path)
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
    if _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.get() is None:
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
    if _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.get() is None:
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


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("file write made no progress")
        view = view[written:]


def _fsync_parent_directory(path: Path) -> None:
    with _OBSDirectoryLease.open_absolute(path) as directory:
        directory.flush_metadata()


def _transaction_temporary_path(
    target: Path,
    owner_token: str,
    *,
    kind: str,
) -> Path:
    owner_token = _validate_migration_owner_token(owner_token)
    if kind == OBS_TRANSACTION_TEMP_JOURNAL:
        return target.with_name(f"{target.name}.{owner_token}.tmp")
    if kind not in {OBS_TRANSACTION_TEMP_COPY, OBS_TRANSACTION_TEMP_WRITE}:
        raise ValueError(f"unsupported transaction temporary kind: {kind}")
    return target.with_name(f".{target.name}.{owner_token}.{kind}.tmp")


def _transaction_copy_temporary_path(
    destination: Path,
    owner_token: str,
) -> Path:
    return _transaction_temporary_path(
        destination,
        owner_token,
        kind=OBS_TRANSACTION_TEMP_COPY,
    )


def _transaction_write_temporary_path(
    path: Path,
    owner_token: str,
) -> Path:
    return _transaction_temporary_path(
        path,
        owner_token,
        kind=OBS_TRANSACTION_TEMP_WRITE,
    )


def _transaction_journal_temporary_path(
    marker: Path,
    owner_token: str,
) -> Path:
    return _transaction_temporary_path(
        marker,
        owner_token,
        kind=OBS_TRANSACTION_TEMP_JOURNAL,
    )


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
    migration_directory_lease = _ACTIVE_OBS_MIGRATION_DIRECTORY_LEASE.get()
    if migration_directory_lease is not None:
        _write_safe_file_bytes_with_migration_lease(
            path,
            payload,
            migration_directory_lease,
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


def _parse_transaction_temporary(path: Path) -> _OBSTransactionTemporaryDescriptor | None:
    name = path.name
    journal_prefix = f"{OBS_COPY_IN_PROGRESS_MARKER_NAME}."
    name_key = _filesystem_name_key(name)
    if name_key.startswith(_filesystem_name_key(journal_prefix)) and name_key.endswith(".tmp"):
        owner_token = name[len(journal_prefix) : -len(".tmp")]
        try:
            owner_token = _validate_migration_owner_token(owner_token)
        except ValueError as exc:
            raise _UnsafeOBSMigrationPathError(
                f"不正なtransaction owner tokenがあります: {name}"
            ) from exc
        return _OBSTransactionTemporaryDescriptor(
            kind=OBS_TRANSACTION_TEMP_JOURNAL,
            target_name=OBS_COPY_IN_PROGRESS_MARKER_NAME,
            owner_token=owner_token,
            path=path,
        )
    for kind, suffix in (
        (OBS_TRANSACTION_TEMP_COPY, ".copy.tmp"),
        (OBS_TRANSACTION_TEMP_WRITE, ".write.tmp"),
    ):
        if not name.startswith(".") or not name_key.endswith(suffix):
            continue
        target_and_token = name[1 : -len(suffix)]
        target_name, separator, owner_token = target_and_token.rpartition(".")
        if not separator or not target_name:
            raise _UnsafeOBSMigrationPathError(f"不正なtransaction一時file名です: {name}")
        try:
            owner_token = _validate_migration_owner_token(owner_token)
        except ValueError as exc:
            raise _UnsafeOBSMigrationPathError(f"不正なtransaction owner tokenがあります: {name}") from exc
        return _OBSTransactionTemporaryDescriptor(
            kind=kind,
            target_name=target_name,
            owner_token=owner_token,
            path=path,
        )
    return None


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


def _list_root_transaction_temporaries(
    directory: _OBSDirectoryLease,
) -> tuple[tuple[_OBSTransactionTemporaryDescriptor, tuple[int, int, int]], ...]:
    temporaries: list[
        tuple[_OBSTransactionTemporaryDescriptor, tuple[int, int, int]]
    ] = []

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
                        kind = "directory"
                    elif stat.S_ISREG(child_stat.st_mode):
                        kind = "file"
                    else:
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
                    else:
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
            parsed = _parse_transaction_temporary(child_path)
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
        if _inventory_fingerprint(source_entries) != journal.source_fingerprint:
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


def _read_safe_relative_file_bytes(
    directory: _OBSDirectoryLease,
    name: str,
    *,
    max_bytes: int,
    label: str,
) -> tuple[bytes, tuple[int, int, int]]:
    descriptor = directory.open_file(
        name,
        write=False,
        create_exclusive=False,
        share_delete=False,
    )
    try:
        before = os.fstat(descriptor)
        if int(before.st_size) > max_bytes:
            raise _UnsafeOBSMigrationPathError(
                f"{label}が{max_bytes} bytesを超えています: {directory.path / name}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise _UnsafeOBSMigrationPathError(
                    f"{label}が読み取り中に{max_bytes} bytesを超えました: {directory.path / name}"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity = _file_identity(before)
        if _file_identity(after) != identity or int(after.st_size) != total:
            raise _UnsafeOBSMigrationPathError(
                f"{label}が読み取り中に変化しました: {directory.path / name}"
            )
        if directory._relative_file_identity(name) != identity:
            raise _UnsafeOBSMigrationPathError(
                f"{label}のpath identityが読み取り中に変化しました: {directory.path / name}"
            )
        return b"".join(chunks), identity
    finally:
        os.close(descriptor)


def _directory_for_descendant_parent(
    root_lease: _OBSDirectoryLease,
    path: Path,
    *,
    mutable: bool = True,
) -> tuple[_OBSDirectoryLease, bool]:
    absolute = _absolute_path(path)
    try:
        relative_parts = absolute.parent.relative_to(root_lease.path).parts
    except ValueError as exc:
        raise _UnsafeOBSMigrationPathError(
            f"管理root外のtransaction pathです: {absolute}"
        ) from exc
    if not relative_parts:
        return root_lease, False
    return (
        root_lease.open_descendant_directory(
            tuple(relative_parts),
            mutable=mutable,
        ),
        True,
    )


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
    ) -> tuple[tuple[str, str, tuple[int, int, int]], ...]:
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
                children: list[tuple[str, str, tuple[int, int, int]]] = []
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
                        finally:
                            os.close(child_descriptor)
                    children.append((entry.name, kind, child_identity))
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
    ) -> tuple[int, str]:
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
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(descriptor)
            if (
                _file_identity(after) != identity
                or int(after.st_size) != size
                or directory._relative_file_identity(name) != identity
            ):
                raise _UnsafeOBSMigrationPathError(
                    f"hash中にfileが変化しました: {directory.path / name}"
                )
            return size, digest.hexdigest()
        finally:
            os.close(descriptor)

    def visit(
        directory: _OBSDirectoryLease,
        relative_parent: tuple[str, ...] = (),
        *,
        include: bool = True,
    ) -> None:
        children_before = list_children(directory)
        for child_name, child_kind, child_identity in children_before:
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
                    if child_include:
                        inventory.append(
                            OBSMigrationInventoryEntry(relative_parts, "directory")
                        )
                    visit(
                        child_directory,
                        relative_parts,
                        include=child_include,
                    )
            else:
                if child_include:
                    size, sha256 = hash_relative_file(
                        directory,
                        child_name,
                        child_identity,
                    )
                    inventory.append(
                        OBSMigrationInventoryEntry(
                            relative_parts,
                            "file",
                            size,
                            sha256,
                        )
                    )
        if list_children(directory) != children_before:
            raise _UnsafeOBSMigrationPathError(
                f"走査中にdirectory entryが変化しました: {directory.path}"
            )

    try:
        visit(lease)
        lease.validate_lexical_binding()
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
        _filesystem_parts_key(entry.relative_parts): entry
        for entry in source_entries
        if not _is_finalize_managed_entry(entry)
    }
    destination_unmanaged = {
        _filesystem_parts_key(entry.relative_parts): entry
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
        "/".join(parts)
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
            if _file_identity(os.fstat(source_descriptor)) != _file_identity(source_before):
                raise _UnsafeOBSMigrationPathError(
                    f"コピー中に移行元file identityが変化しました: {source}"
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
    transaction_resources = ExitStack()
    try:
        try:
            try:
                destination_probe = _OBSDirectoryLease.open_absolute(destination)
            except FileNotFoundError:
                destination_probe = None

            marker_exists = (
                destination_probe is not None
                and destination_probe.relative_file_identity_or_none(marker.name) is not None
            )
            destination_appeared_valid = (
                destination_probe is not None
                and not marker_exists
                and _is_valid_obs_installation_lease(destination_probe)
            )
            if not marker_exists and not destination_appeared_valid:
                for candidate in allowed_sources:
                    try:
                        candidate_probe = _OBSDirectoryLease.open_absolute(candidate)
                    except FileNotFoundError:
                        continue
                    try:
                        if not _is_valid_obs_installation_lease(candidate_probe):
                            candidate_probe.close()
                            continue
                        source_marker = get_obs_copy_in_progress_marker(candidate)
                        if (
                            candidate_probe.relative_file_identity_or_none(source_marker.name)
                            is not None
                        ):
                            raise _UnsafeOBSMigrationPathError(
                                f"移行元にコピー中markerがあります: {source_marker}"
                            )
                    except Exception:
                        candidate_probe.close()
                        raise
                    source = candidate
                    source_probe = candidate_probe
                    break
                if source_probe is None and destination_probe is None:
                    return None

            lock = _OBSInterProcessLock(get_obs_copy_lock_path(destination))
            if not lock.acquire(directory_lease=destination_probe):
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
            if marker_identity is not None:
                stale_journal = _read_obs_migration_journal(
                    marker,
                    directory_lease=destination_root_lease,
                )
                source = _validated_journal_source(stale_journal, allowed_sources, destination)
                if source_probe is not None:
                    source_probe.close()
                try:
                    source_probe = _OBSDirectoryLease.open_absolute(source)
                except FileNotFoundError as exc:
                    raise _migration_recovery_error(
                        destination,
                        "markerの移行元に有効なポータブルOBSがありません: "
                        f"{get_obs_executable_path(source)}",
                    ) from exc
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
            if stale_journal is not None and source_fingerprint != stale_journal.source_fingerprint:
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
                if copied_entries != source_entries:
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
        primary_snapshot = preflight_obs_config_file(
            primary_marker,
            label=PORTABLE_OBS_MARKER_NAME,
        )
        if primary_snapshot.payload is None:
            _write_safe_file_bytes(
                primary_marker,
                b"",
                expected_snapshot=primary_snapshot,
            )

        legacy_marker = get_legacy_marker_path(self.base_dir)
        legacy_snapshot = preflight_obs_config_file(
            legacy_marker,
            label=LEGACY_PORTABLE_OBS_MARKER_NAME,
        )
        if legacy_snapshot.payload is None:
            _write_safe_file_bytes(
                legacy_marker,
                b"",
                expected_snapshot=legacy_snapshot,
            )
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
        snapshot = preflight_obs_config_file(ini_path, label=label)
        exists = snapshot.payload is not None
        parser = new_obs_ini_parser()

        parse_failed = False
        normalized_encoding = False
        if exists:
            try:
                parser, normalized_encoding = _parse_obs_ini_payload(snapshot.payload)
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

        if changed or not exists:
            buffer = io.StringIO()
            parser.write(buffer, space_around_delimiters=False)
            _write_safe_file_bytes(
                ini_path,
                buffer.getvalue().encode("utf-8"),
                expected_snapshot=snapshot,
            )
        return changed, ini_path

    @_guard_obs_bootstrap_mutation
    def ensure_websocket_config(self, port: int, password: str) -> tuple[bool, Path]:
        self._prepare_write_layout(include_websocket=True)
        config_path = get_obs_websocket_config_path(self.base_dir)
        _ensure_safe_directory_chain(config_path.parent)
        snapshot = preflight_obs_config_file(
            config_path,
            label="obs-websocket設定file",
        )
        exists = snapshot.payload is not None

        data = {}
        if exists:
            try:
                loaded = json.loads(snapshot.payload.decode("utf-8"))
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
            _write_safe_file_bytes(
                config_path,
                payload,
                expected_snapshot=snapshot,
            )
        return changed, config_path
