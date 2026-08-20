from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import secrets
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

try:
    from . import obs_transaction_fs as _transaction_fs
except ImportError:
    import obs_transaction_fs as _transaction_fs

LOGGER = logging.getLogger("lol_replay.obs_process")

OBS_PROCESS_LEASE_SCHEMA_VERSION = 2
OBS_PROCESS_LEASE_MAX_BYTES = 64 * 1024
OBS_PROCESS_LEASE_FILE_NAME = _transaction_fs.OBS_PROCESS_LEASE_FILE_NAME
OBS_PROCESS_LEASE_LOCK_NAME = _transaction_fs.OBS_PROCESS_LEASE_LOCK_NAME
OBS_PROCESS_LEASE_TEMP_PREFIX = _transaction_fs.OBS_PROCESS_LEASE_TEMP_PREFIX
_WINDOWS_FILETIME_UNIX_EPOCH_SECONDS = 11_644_473_600
_OBS_PROCESS_LEASE_LOCK = threading.RLock()
_POPEN_OBS_PROCESS_LEASE_ATTRIBUTE = "_lol_replay_obs_process_lease"
_OWNED_EXIT_STRICT_REQUERY_DELAY_SEC = 0.05
_OBS_PROCESS_LEASE_LOCK_TIMEOUT_SEC = 10.0
_OBS_PROCESS_LEASE_LOCK_POLL_SEC = 0.02
_OBS_PROCESS_QUERY_COMMAND_TIMEOUT_SEC = 10.0
_OBS_PROCESS_TERMINATE_COMMAND_TIMEOUT_SEC = 10.0
_OBS_PROCESS_LEASE_TEMP_NAME_PATTERN = re.compile(
    rf"^{re.escape(OBS_PROCESS_LEASE_TEMP_PREFIX)}[0-9a-f]{{32}}$"
)

_OBSDirectoryLease = _transaction_fs._OBSDirectoryLease
_OBSInterProcessLock = _transaction_fs._OBSInterProcessLock
_OBSMigrationLockBusyError = _transaction_fs._OBSMigrationLockBusyError
OBSPathSafetyError = _transaction_fs.OBSPathSafetyError
_file_identity = _transaction_fs._file_identity
_filesystem_name_key = _transaction_fs._filesystem_name_key


@dataclass(frozen=True)
class OBSProcessInfo:
    pid: int
    executable_path: Path | None
    creation_time: float | None = None
    creation_time_filetime: int | None = None


@dataclass(frozen=True)
class OBSProcessQuerySnapshot:
    """A process snapshot whose query is known to have completed successfully."""

    processes: tuple[OBSProcessInfo, ...]
    queried_at: float


@dataclass(frozen=True)
class OBSStrictTerminationResult:
    """Identity-bearing result of one fail-closed OBS termination pass."""

    signaled_processes: tuple[OBSProcessInfo, ...]
    after: OBSProcessQuerySnapshot


class OBSProcessQueryError(RuntimeError):
    """Raised when OBS process absence cannot be established reliably."""


class _OBSOwnedProcessIdentityMismatchError(OBSProcessQueryError):
    """Raised when a strict owned-process query finds a replacement identity."""


class OBSProcessLeaseError(OBSProcessQueryError):
    """Raised when an OBS ownership lease is missing required identity data."""


class OBSProcessLeaseCleanupError(OBSProcessLeaseError):
    """Raised when a lease failure leaves the newly started process live."""


class OBSProcessTerminationError(OBSProcessQueryError):
    """Raised when a Popen-owned OBS cleanup cannot be proven complete."""


_NATIVE_WINDOWS = os.name == "nt"
_WINDOWS_FILE_READ_ATTRIBUTES = 0x0080
_WINDOWS_FILE_SHARE_READ_WRITE = 0x0001 | 0x0002
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_ID_INFO_CLASS = 18
_WINDOWS_VOLUME_NAME_GUID = 0x00000001


@dataclass(frozen=True)
class _OBSExecutableIdentityPin:
    physical_identity: tuple[int, int | bytes, int]
    launch_path: Path
    revalidate: Callable[[], None]


def _record_obs_executable_pin_close_failure(
    *,
    primary_error: BaseException | None,
    selected_control_flow: BaseException | None,
    close_error: BaseException,
    label: str,
) -> BaseException | None:
    """Record one close failure and preserve the first control-flow exception."""

    target: BaseException | None
    if primary_error is not None and not isinstance(primary_error, Exception):
        target = primary_error
    elif selected_control_flow is not None:
        target = selected_control_flow
    elif not isinstance(close_error, Exception):
        selected_control_flow = close_error
        target = close_error
        if primary_error is not None:
            add_note = getattr(close_error, "add_note", None)
            if callable(add_note):
                add_note(
                    "Earlier executable pin failure before close was interrupted: "
                    f"{type(primary_error).__name__}: {primary_error}"
                )
    else:
        target = primary_error

    if target is not None and target is not close_error:
        add_note = getattr(target, "add_note", None)
        if callable(add_note):
            add_note(f"{label}: {type(close_error).__name__}: {close_error}")
    LOGGER.critical("%s: %s", label, close_error)
    return selected_control_flow


def _windows_close_obs_executable_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(wintypes.HANDLE(handle)):
        raise ctypes.WinError(ctypes.get_last_error())


def _windows_obs_executable_handle_identity(
    handle: int,
    *,
    path: Path,
    directory: bool,
) -> tuple[int, bytes, int]:
    import ctypes
    from ctypes import wintypes

    class FileId128(ctypes.Structure):
        _fields_ = [("Identifier", wintypes.BYTE * 16)]

    class FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", FileId128),
        ]

    class ByHandleFileInformation(ctypes.Structure):
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

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    basic = ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(
        wintypes.HANDLE(handle),
        ctypes.byref(basic),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    attributes = int(basic.dwFileAttributes)
    if attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT:
        raise OBSProcessQueryError(
            f"OBS executable namespace contains an unsupported reparse point: {path}"
        )
    is_directory = bool(attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
    if is_directory != directory:
        expected = "directory" if directory else "regular file"
        raise OBSProcessQueryError(
            f"OBS executable namespace entry is not a {expected}: {path}"
        )

    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    file_id_info = FileIdInfo()
    if not kernel32.GetFileInformationByHandleEx(
        wintypes.HANDLE(handle),
        _WINDOWS_FILE_ID_INFO_CLASS,
        ctypes.byref(file_id_info),
        ctypes.sizeof(file_id_info),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    volume_serial = int(file_id_info.VolumeSerialNumber)
    file_id = bytes(file_id_info.FileId.Identifier)
    if volume_serial <= 0 or not any(file_id):
        raise OBSProcessQueryError(
            f"Windows did not provide a usable physical file ID: {path}"
        )
    kind = stat.S_IFDIR if directory else stat.S_IFREG
    return (volume_serial, file_id, kind)


def _windows_open_obs_executable_entry(path: Path, *, directory: bool) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    flags = _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
    handle = kernel32.CreateFileW(
        str(path),
        _WINDOWS_FILE_READ_ATTRIBUTES,
        _WINDOWS_FILE_SHARE_READ_WRITE,
        None,
        _WINDOWS_OPEN_EXISTING,
        flags,
        None,
    )
    handle_value = int(handle) if handle is not None else 0
    invalid_handle = int(ctypes.c_void_p(-1).value)
    if handle_value in {0, invalid_handle}:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle_value


def _windows_obs_executable_final_path(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    required = int(
        kernel32.GetFinalPathNameByHandleW(
            wintypes.HANDLE(handle),
            None,
            0,
            _WINDOWS_VOLUME_NAME_GUID,
        )
    )
    if required <= 0:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = int(
        kernel32.GetFinalPathNameByHandleW(
            wintypes.HANDLE(handle),
            buffer,
            len(buffer),
            _WINDOWS_VOLUME_NAME_GUID,
        )
    )
    if written <= 0:
        raise ctypes.WinError(ctypes.get_last_error())
    if written >= len(buffer):
        raise OBSProcessQueryError(
            "Windows executable final path changed while allocating its buffer"
        )
    final_path = Path(buffer.value)
    if not _is_absolute_obs_process_path(final_path):
        raise OBSProcessQueryError(
            f"Windows executable final path is not absolute: {final_path}"
        )
    return final_path


def _windows_obs_dos_device_snapshot(
    path: Path,
) -> tuple[str, tuple[str, ...]] | None:
    import ctypes
    from ctypes import wintypes

    drive = PureWindowsPath(str(path)).drive
    if drive.startswith("\\\\?\\"):
        drive = drive[4:]
    if re.fullmatch(r"[A-Za-z]:", drive) is None:
        return None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryDosDeviceW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.QueryDosDeviceW.restype = wintypes.DWORD
    capacity = 32768
    buffer = ctypes.create_unicode_buffer(capacity)
    length = int(kernel32.QueryDosDeviceW(drive, buffer, capacity))
    if length <= 0:
        raise ctypes.WinError(ctypes.get_last_error())
    targets = tuple(
        target
        for target in "".join(buffer[:length]).split("\0")
        if target
    )
    if not targets:
        raise OBSProcessQueryError(
            f"Windows DOS device mapping is empty for {drive}"
        )
    if any(
        target.casefold().startswith(("\\??\\", "\\dosdevices\\"))
        for target in targets
    ):
        raise OBSProcessQueryError(
            f"SUBST/DOS-device OBS executable aliases are unsupported: {path}"
        )
    return (drive.upper(), targets)


def _windows_obs_executable_components(
    path: Path,
) -> tuple[tuple[Path, bool], ...]:
    if not _is_absolute_obs_process_path(path):
        raise OBSProcessQueryError(
            f"OBS executable path is not absolute: {path}"
        )
    parts = path.parts
    if len(parts) < 2:
        raise OBSProcessQueryError(
            f"OBS executable path has no file component: {path}"
        )
    current = Path(parts[0])
    components: list[tuple[Path, bool]] = [(current, True)]
    for part in parts[1:-1]:
        if ":" in part:
            raise OBSProcessQueryError(
                f"OBS executable path contains an unsupported stream component: {path}"
            )
        current /= part
        components.append((current, True))
    if ":" in parts[-1]:
        raise OBSProcessQueryError(
            f"OBS executable path contains an unsupported stream: {path}"
        )
    components.append((current / parts[-1], False))
    return tuple(components)


@contextmanager
def _pin_obs_executable_identity(path: Path):
    """Pin an executable namespace and physical file until the caller exits."""

    primary_error: BaseException | None = None
    if _NATIVE_WINDOWS:
        handles: list[int] = []
        records: list[tuple[Path, bool, tuple[int, bytes, int]]] = []
        close_control_flow_error: BaseException | None = None
        try:
            dos_snapshot = _windows_obs_dos_device_snapshot(path)
            for component_path, directory in _windows_obs_executable_components(path):
                handle = _windows_open_obs_executable_entry(
                    component_path,
                    directory=directory,
                )
                handles.append(handle)
                identity = _windows_obs_executable_handle_identity(
                    handle,
                    path=component_path,
                    directory=directory,
                )
                records.append((component_path, directory, identity))
            if _windows_obs_dos_device_snapshot(path) != dos_snapshot:
                raise OBSProcessQueryError(
                    f"Windows DOS device mapping changed while pinning: {path}"
                )

            def revalidate() -> None:
                if _windows_obs_dos_device_snapshot(path) != dos_snapshot:
                    raise OBSProcessQueryError(
                        f"Windows DOS device mapping changed while pinned: {path}"
                    )
                for component_path, directory, expected_identity in records:
                    verification_handle = _windows_open_obs_executable_entry(
                        component_path,
                        directory=directory,
                    )
                    verification_error: BaseException | None = None
                    try:
                        actual_identity = _windows_obs_executable_handle_identity(
                            verification_handle,
                            path=component_path,
                            directory=directory,
                        )
                    except BaseException as exc:
                        verification_error = exc
                        raise
                    finally:
                        try:
                            _windows_close_obs_executable_handle(verification_handle)
                        except BaseException as close_error:
                            close_control_flow_error = (
                                _record_obs_executable_pin_close_failure(
                                    primary_error=verification_error,
                                    selected_control_flow=None,
                                    close_error=close_error,
                                    label=(
                                        "OBS executable verification handle close "
                                        "failed"
                                    ),
                                )
                            )
                            if close_control_flow_error is not None and (
                                verification_error is None
                                or isinstance(verification_error, Exception)
                            ):
                                raise close_control_flow_error  # noqa: B904
                            if verification_error is None:
                                raise
                    if actual_identity != expected_identity:
                        raise OBSProcessQueryError(
                            "OBS executable namespace identity changed while pinned: "
                            f"{component_path}"
                        )
                if _windows_obs_dos_device_snapshot(path) != dos_snapshot:
                    raise OBSProcessQueryError(
                        f"Windows DOS device mapping changed during revalidation: {path}"
                    )

            final_identity = records[-1][2]
            final_path = _windows_obs_executable_final_path(handles[-1])
            pin = _OBSExecutableIdentityPin(
                physical_identity=final_identity,
                launch_path=final_path,
                revalidate=revalidate,
            )
            pin.revalidate()
            yield pin
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            for handle in reversed(handles):
                try:
                    _windows_close_obs_executable_handle(handle)
                except BaseException as close_error:
                    close_control_flow_error = (
                        _record_obs_executable_pin_close_failure(
                            primary_error=primary_error,
                            selected_control_flow=close_control_flow_error,
                            close_error=close_error,
                            label="Pinned OBS executable handle close failed",
                        )
                    )
            if close_control_flow_error is not None and (
                primary_error is None or isinstance(primary_error, Exception)
            ):
                raise close_control_flow_error
        return

    descriptor: int | None = None
    close_control_flow_error: BaseException | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OBSProcessQueryError(
                f"OBS executable path is not a regular file: {path}"
            )
        identity = _file_identity(opened)

        def revalidate_posix() -> None:
            current = path.stat(follow_symlinks=True)
            if not stat.S_ISREG(current.st_mode) or _file_identity(current) != identity:
                raise OBSProcessQueryError(
                    f"OBS executable path identity changed while pinned: {path}"
                )

        pin = _OBSExecutableIdentityPin(
            physical_identity=identity,
            launch_path=path,
            revalidate=revalidate_posix,
        )
        pin.revalidate()
        yield pin
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException as close_error:
                close_control_flow_error = _record_obs_executable_pin_close_failure(
                    primary_error=primary_error,
                    selected_control_flow=close_control_flow_error,
                    close_error=close_error,
                    label="Pinned OBS executable descriptor close failed",
                )
        if close_control_flow_error is not None and (
            primary_error is None or isinstance(primary_error, Exception)
        ):
            raise close_control_flow_error


def _obs_executable_paths_same_physical_file(left: Path, right: Path) -> bool:
    """Compare two executable paths while both physical files stay pinned."""

    with ExitStack() as stack:
        left_pin = stack.enter_context(_pin_obs_executable_identity(left))
        right_pin = stack.enter_context(_pin_obs_executable_identity(right))
        left_pin.revalidate()
        right_pin.revalidate()
        return left_pin.physical_identity == right_pin.physical_identity


def _is_absolute_obs_process_path(path: Path) -> bool:
    """Accept host-absolute paths and absolute Windows paths parsed on POSIX."""

    return path.is_absolute() or PureWindowsPath(str(path)).is_absolute()


def _obs_process_paths_equal(left: Path, right: Path) -> bool:
    """Compare lexically equivalent host or Windows process paths."""

    left_text = str(left)
    right_text = str(right)
    if left_text.startswith("\\\\?\\"):
        left_text = left_text[4:]
    if right_text.startswith("\\\\?\\"):
        right_text = right_text[4:]
    if (
        PureWindowsPath(left_text).is_absolute()
        or PureWindowsPath(right_text).is_absolute()
    ):
        return PureWindowsPath(left_text) == PureWindowsPath(right_text)
    return Path(left_text) == Path(right_text)


def _obs_process_lifetimes_equal(
    expected: OBSProcessInfo,
    actual: OBSProcessInfo,
) -> bool:
    """Compare PID and creation time without weakening raw FILETIME identity."""

    if expected.pid != actual.pid:
        return False
    if expected.creation_time is None or actual.creation_time is None:
        return False
    if (
        expected.creation_time_filetime is not None
        or actual.creation_time_filetime is not None
    ):
        return (
            expected.creation_time_filetime is not None
            and expected.creation_time_filetime == actual.creation_time_filetime
        )
    return round(float(expected.creation_time) * 1000) == round(
        float(actual.creation_time) * 1000
    )


def _obs_process_identities_equal(
    expected: OBSProcessInfo,
    actual: OBSProcessInfo,
) -> bool:
    """Compare the lexical image path and exact process lifetime identity."""

    if expected.executable_path is None or actual.executable_path is None:
        return False
    return _obs_process_paths_equal(
        expected.executable_path,
        actual.executable_path,
    ) and _obs_process_lifetimes_equal(expected, actual)


def validate_obs_process_query_snapshot(
    snapshot: OBSProcessQuerySnapshot,
    *,
    label: str,
) -> tuple[OBSProcessInfo, ...]:
    """Validate that a strict snapshot contains complete immutable identities."""

    if type(snapshot) is not OBSProcessQuerySnapshot:
        raise OBSProcessQueryError(f"{label} OBS process query snapshot is malformed")
    queried_at = snapshot.queried_at
    if (
        isinstance(queried_at, bool)
        or not isinstance(queried_at, (int, float))
        or not math.isfinite(float(queried_at))
        or float(queried_at) <= 0
    ):
        raise OBSProcessQueryError(f"{label} OBS process query time is malformed")
    if type(snapshot.processes) is not tuple:
        raise OBSProcessQueryError(f"{label} OBS process query result is not a tuple")

    seen_pids: set[int] = set()
    for process in snapshot.processes:
        if type(process) is not OBSProcessInfo:
            raise OBSProcessQueryError(f"{label} OBS process row is malformed")
        if type(process.pid) is not int or process.pid <= 0:
            raise OBSProcessQueryError(f"{label} OBS process id is malformed")
        if process.pid in seen_pids:
            raise OBSProcessQueryError(f"{label} OBS process ids are duplicated")
        seen_pids.add(process.pid)
        executable_path = process.executable_path
        if (
            not isinstance(executable_path, Path)
            or not _is_absolute_obs_process_path(executable_path)
        ):
            raise OBSProcessQueryError(
                f"{label} OBS executable path is missing or not absolute"
            )
        creation_time = process.creation_time
        if (
            isinstance(creation_time, bool)
            or not isinstance(creation_time, (int, float))
            or not math.isfinite(float(creation_time))
            or float(creation_time) <= 0
        ):
            raise OBSProcessQueryError(
                f"{label} OBS process creation time is missing or malformed"
            )
        creation_time_filetime = process.creation_time_filetime
        if creation_time_filetime is not None and (
            type(creation_time_filetime) is not int
            or creation_time_filetime <= 0
        ):
            raise OBSProcessQueryError(
                f"{label} OBS process FILETIME is malformed"
            )
    return snapshot.processes


@dataclass(frozen=True)
class OBSProcessLease:
    schema_version: int
    pid: int
    executable_path: Path
    created_at: float
    process_creation_time: float | None = None
    process_creation_time_filetime: int | None = None


@dataclass
class _OBSProcessLeaseFileSnapshot:
    descriptor: int
    identity: tuple[int, int, int]
    raw_bytes: bytes
    lease: OBSProcessLease
    deletion_marked: bool = False


@dataclass
class _OBSProcessLeaseTransaction:
    lock: Any
    root_lease: Any
    snapshot: _OBSProcessLeaseFileSnapshot | None = None
    commit_occurred: bool = False
    recovered_temporary_paths: list[Path] = field(default_factory=list)

    def validate_ownership(self) -> None:
        self.lock.validate_ownership()
        self.root_lease.validate_lexical_binding()


@dataclass(frozen=True)
class _OBSStartAdmissionGuard:
    managed_pin: _OBSExecutableIdentityPin
    candidate_pins: tuple[_OBSExecutableIdentityPin, ...]
    launch_path: Path

    def revalidate(self) -> None:
        self.managed_pin.revalidate()
        for pin in self.candidate_pins:
            pin.revalidate()


class OBSProcessManager:
    """アプリ管理OBSだけを対象に起動・終了する安全境界。"""

    def __init__(self, obs_dir: str | Path, logger: logging.Logger | None = None) -> None:
        self.obs_dir = Path(obs_dir).resolve()
        self.obs_exe = (self.obs_dir / "bin" / "64bit" / "obs64.exe").resolve()
        self.logger = logger or LOGGER
        self.lease_path = self.obs_dir / OBS_PROCESS_LEASE_FILE_NAME
        self.lease_lock_path = self.obs_dir / OBS_PROCESS_LEASE_LOCK_NAME

    def _lease_recovery_error(
        self,
        detail: str,
        *,
        cause: BaseException | None = None,
    ) -> OBSProcessLeaseError:
        error = OBSProcessLeaseError(
            f"{detail} lease={self.lease_path} lock={self.lease_lock_path}。"
            "すべてのOBS Studioと関連toolを終了し、同じ操作を再試行してください。"
        )
        if cause is not None:
            error.__cause__ = cause
        return error

    def _close_descriptor_preserving_primary(
        self,
        descriptor: int,
        *,
        primary_error: BaseException | None,
        label: str,
    ) -> None:
        try:
            os.close(descriptor)
        except BaseException as close_error:
            if primary_error is None:
                raise
            add_note = getattr(primary_error, "add_note", None)
            if callable(add_note):
                add_note(
                    f"{label}: {type(close_error).__name__}: {close_error}"
                )
            self.logger.critical("%s: %s", label, close_error)

    def _select_control_flow_cleanup_failure(
        self,
        primary_error: BaseException,
        cleanup_error: BaseException,
        *,
        context: str,
    ) -> BaseException | None:
        """Record cleanup failure and select the first control-flow exception."""

        if not isinstance(primary_error, Exception):
            add_note = getattr(primary_error, "add_note", None)
            if callable(add_note):
                add_note(
                    f"{context}: {type(cleanup_error).__name__}: {cleanup_error}"
                )
            self.logger.critical(
                "%s while preserving the original interruption: %s",
                context,
                cleanup_error,
            )
            return primary_error
        if not isinstance(cleanup_error, Exception):
            add_note = getattr(cleanup_error, "add_note", None)
            if callable(add_note):
                add_note(
                    f"{context}. Earlier failure before cleanup was interrupted: "
                    f"{type(primary_error).__name__}: {primary_error}"
                )
            self.logger.critical(
                "%s was interrupted after an earlier failure: %s",
                context,
                cleanup_error,
            )
            return cleanup_error
        return None

    def _nonmutating_process_lease_transaction_required(self) -> bool:
        """Probe a stable absent state without creating the managed root/lock."""

        probe_error: BaseException | None = None
        try:
            probe = _OBSDirectoryLease.open_absolute(self.obs_dir)
        except FileNotFoundError:
            try:
                appeared = _OBSDirectoryLease.open_absolute(self.obs_dir)
            except FileNotFoundError:
                return False
            else:
                race_error = self._lease_recovery_error(
                    "OBS rootが所有情報の不存在確認中に出現しました。"
                )
                try:
                    appeared.close()
                except BaseException as close_error:
                    add_note = getattr(race_error, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "Appeared OBS root probe close also failed: "
                            f"{type(close_error).__name__}: {close_error}"
                        )
                    self.logger.critical(
                        "Appeared OBS root probe close also failed: %s",
                        close_error,
                    )
                raise race_error
        except (OSError, OBSPathSafetyError) as exc:
            raise self._lease_recovery_error(
                "OBS rootの所有情報を安全に確認できません。",
                cause=exc,
            ) from exc

        try:
            def observe() -> tuple[
                tuple[int, int, int] | None,
                tuple[int, int, int] | None,
                tuple[str, ...],
            ]:
                probe.validate_lexical_binding()
                lease_identity = probe.relative_file_identity_or_none(
                    self.lease_path.name
                )
                lock_identity = probe.relative_file_identity_or_none(
                    self.lease_lock_path.name
                )
                temporary_names: list[str] = []
                with os.scandir(probe.path) as entries:
                    prefix_key = _filesystem_name_key(
                        OBS_PROCESS_LEASE_TEMP_PREFIX
                    )
                    for entry in entries:
                        if not _filesystem_name_key(entry.name).startswith(
                            prefix_key
                        ):
                            continue
                        if (
                            _OBS_PROCESS_LEASE_TEMP_NAME_PATTERN.fullmatch(
                                entry.name
                            )
                            is None
                        ):
                            raise self._lease_recovery_error(
                                "予約されたOBS所有一時file名が不正です: "
                                f"{probe.path / entry.name}。"
                            )
                        temporary_names.append(entry.name)
                probe.validate_lexical_binding()
                return (
                    lease_identity,
                    lock_identity,
                    tuple(
                        sorted(
                            temporary_names,
                            key=lambda name: (_filesystem_name_key(name), name),
                        )
                    ),
                )

            before = observe()
            after = observe()
            if after != before:
                raise self._lease_recovery_error(
                    "OBS所有control namespaceが不存在確認中に変化しました。"
                )
            return any(item for item in before)
        except OBSProcessLeaseError as exc:
            probe_error = exc
            raise
        except (OSError, OBSPathSafetyError) as exc:
            error = self._lease_recovery_error(
                "OBS所有control namespaceを安全に確認できません。",
                cause=exc,
            )
            probe_error = error
            raise error from exc
        except BaseException as exc:
            probe_error = exc
            raise
        finally:
            try:
                probe.close()
            except BaseException as close_error:
                if probe_error is None:
                    raise self._lease_recovery_error(
                        "OBS root probeを安全にcloseできません。",
                        cause=close_error,
                    ) from close_error
                add_note = getattr(probe_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "OBS root probe close also failed: "
                        f"{type(close_error).__name__}: {close_error}"
                    )
                self.logger.critical(
                    "OBS root probe close also failed: %s",
                    close_error,
                )

    @contextmanager
    def _process_lease_transaction(self, *, mutating: bool = False):
        """Serialize every semantic lease operation across threads/processes."""

        with _OBS_PROCESS_LEASE_LOCK:
            if (
                not mutating
                and not self._nonmutating_process_lease_transaction_required()
            ):
                yield None
                return

            lock = _OBSInterProcessLock(self.lease_lock_path)
            deadline = time.monotonic() + _OBS_PROCESS_LEASE_LOCK_TIMEOUT_SEC
            transaction: _OBSProcessLeaseTransaction | None = None
            primary_error: BaseException | None = None
            cleanup_error: BaseException | None = None
            cleanup_control_flow_error: BaseException | None = None
            cleanup_failures: list[tuple[str, BaseException]] = []
            lock_acquired = False

            def record_cleanup_error(label: str, error: BaseException) -> None:
                nonlocal cleanup_error, cleanup_control_flow_error
                cleanup_failures.append((label, error))
                target = (
                    primary_error
                    if primary_error is not None
                    and not isinstance(primary_error, Exception)
                    else cleanup_control_flow_error
                    or primary_error
                    or cleanup_error
                )
                if target is not None:
                    add_note = getattr(target, "add_note", None)
                    if callable(add_note):
                        add_note(
                            f"{label}: {type(error).__name__}: {error}"
                        )
                    self.logger.critical("%s: %s", label, error)
                else:
                    cleanup_error = error
                if (
                    (primary_error is None or isinstance(primary_error, Exception))
                    and not isinstance(error, Exception)
                    and cleanup_control_flow_error is None
                ):
                    cleanup_control_flow_error = error
                    add_note = getattr(error, "add_note", None)
                    if callable(add_note):
                        if primary_error is not None:
                            add_note(
                                "Earlier transaction body failure: "
                                f"{type(primary_error).__name__}: {primary_error}"
                            )
                        for previous_label, previous_error in cleanup_failures[:-1]:
                            add_note(
                                "Earlier transaction cleanup failure: "
                                f"{previous_label}: "
                                f"{type(previous_error).__name__}: {previous_error}"
                            )
                    for previous_label, previous_error in cleanup_failures[:-1]:
                        self.logger.critical(
                            "Earlier transaction cleanup failure before control-flow "
                            "interruption (%s): %s",
                            previous_label,
                            previous_error,
                        )

            try:
                while True:
                    try:
                        acquired = lock.acquire(create_parent=True)
                    except (OSError, OBSPathSafetyError) as exc:
                        raise self._lease_recovery_error(
                            "OBS所有情報のprocess間lockを安全に取得できません。",
                            cause=exc,
                        ) from exc
                    if acquired:
                        lock_acquired = True
                        break
                    if time.monotonic() >= deadline:
                        raise self._lease_recovery_error(
                            "OBS所有情報のprocess間lockが使用中です。"
                        )
                    time.sleep(_OBS_PROCESS_LEASE_LOCK_POLL_SEC)

                transaction = _OBSProcessLeaseTransaction(
                    lock=lock,
                    root_lease=lock.directory_lease,
                )
                try:
                    transaction.validate_ownership()
                    self._recover_process_lease_temporaries_locked(transaction)
                except OBSProcessLeaseError:
                    raise
                except (OSError, OBSPathSafetyError) as exc:
                    raise self._lease_recovery_error(
                        "OBS所有transactionのroot/lockを安全に検証できません。",
                        cause=exc,
                    ) from exc
                yield transaction
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                if transaction is not None:
                    snapshot = transaction.snapshot
                    transaction.snapshot = None
                    if snapshot is not None:
                        try:
                            os.close(snapshot.descriptor)
                        except BaseException as close_error:
                            record_cleanup_error(
                                "Pinned lease descriptor close failed",
                                close_error,
                            )
                if lock_acquired:
                    try:
                        lock.release()
                    except BaseException as release_error:
                        record_cleanup_error(
                            "OBS lease IPC lock release failed",
                            release_error,
                        )
                if cleanup_control_flow_error is not None and (
                    primary_error is None or isinstance(primary_error, Exception)
                ):
                    raise cleanup_control_flow_error
                if primary_error is None and cleanup_error is not None:
                    if transaction is not None and transaction.commit_occurred:
                        self.logger.critical(
                            "OBS lease cleanup failed after an irreversible commit: %s",
                            cleanup_error,
                        )
                    else:
                        error = self._lease_recovery_error(
                            "OBS所有transactionの終了処理を安全に完了できません。",
                            cause=cleanup_error,
                        )
                        add_note = getattr(error, "add_note", None)
                        if callable(add_note):
                            for label, failure in cleanup_failures:
                                add_note(
                                    f"{label}: {type(failure).__name__}: {failure}"
                                )
                            if transaction is not None:
                                for path in transaction.recovered_temporary_paths:
                                    add_note(
                                        "孤立したOBS所有一時fileは削除済みです: "
                                        f"{path}"
                                    )
                        raise error from cleanup_error

    def _list_process_lease_temporary_names_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
    ) -> tuple[str, ...]:
        names: list[str] = []
        prefix_key = _filesystem_name_key(OBS_PROCESS_LEASE_TEMP_PREFIX)
        try:
            transaction.validate_ownership()
            with os.scandir(transaction.root_lease.path) as entries:
                for entry in entries:
                    name = entry.name
                    if not _filesystem_name_key(name).startswith(prefix_key):
                        continue
                    if _OBS_PROCESS_LEASE_TEMP_NAME_PATTERN.fullmatch(name) is None:
                        raise self._lease_recovery_error(
                            f"予約されたOBS所有一時file名が不正です: "
                            f"{transaction.root_lease.path / name}。"
                        )
                    names.append(name)
            transaction.validate_ownership()
        except OBSProcessLeaseError:
            raise
        except (OSError, OBSPathSafetyError) as exc:
            raise self._lease_recovery_error(
                "OBS所有一時fileを列挙できません。",
                cause=exc,
            ) from exc
        return tuple(sorted(names, key=lambda name: (_filesystem_name_key(name), name)))

    def _recover_process_lease_temporaries_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
    ) -> None:
        for name in self._list_process_lease_temporary_names_locked(transaction):
            temporary_path = transaction.root_lease.path / name
            descriptor: int | None = None
            primary_error: BaseException | None = None
            try:
                descriptor = transaction.root_lease.open_file(
                    name,
                    write=False,
                    create_exclusive=False,
                    delete=True,
                    share_write=False,
                    share_delete=False,
                )
                identity = _file_identity(os.fstat(descriptor))
                self._read_process_lease_descriptor_bytes_locked(
                    transaction,
                    descriptor,
                    name=name,
                    expected_identity=identity,
                    allow_empty=True,
                )
                close_failure = transaction.root_lease.delete_open_file_on_close(
                    descriptor,
                    name,
                    expected_identity=identity,
                )
                descriptor = None
                transaction.recovered_temporary_paths.append(temporary_path)
                if close_failure is not None:
                    self.logger.critical(
                        "OBS lease temporary deletion committed but close failed: %s: %s",
                        temporary_path,
                        close_failure,
                    )
                    if not isinstance(close_failure, Exception):
                        raise close_failure
            except (OSError, OBSPathSafetyError) as exc:
                error = self._lease_recovery_error(
                    f"孤立したOBS所有一時fileを安全に回収できません: "
                    f"{temporary_path}。",
                    cause=exc,
                )
                primary_error = error
                raise error from exc
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                if descriptor is not None:
                    self._close_descriptor_preserving_primary(
                        descriptor,
                        primary_error=primary_error,
                        label="OBS lease temporary descriptor close also failed",
                    )

        if self._list_process_lease_temporary_names_locked(transaction):
            raise self._lease_recovery_error(
                "回収中に新しいOBS所有一時fileが出現しました。"
            )

    def _read_process_lease_descriptor_bytes_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
        descriptor: int,
        *,
        name: str,
        expected_identity: tuple[int, int, int],
        allow_empty: bool = False,
    ) -> bytes:
        try:
            return self._read_process_lease_descriptor_bytes_unwrapped_locked(
                transaction,
                descriptor,
                name=name,
                expected_identity=expected_identity,
                allow_empty=allow_empty,
            )
        except OBSProcessLeaseError:
            raise
        except (OSError, OBSPathSafetyError) as exc:
            raise self._lease_recovery_error(
                "OBS所有fileを固定handleから安全に読み取れません: "
                f"{transaction.root_lease.path / name}。",
                cause=exc,
            ) from exc

    def _read_process_lease_descriptor_bytes_unwrapped_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
        descriptor: int,
        *,
        name: str,
        expected_identity: tuple[int, int, int],
        allow_empty: bool = False,
    ) -> bytes:
        transaction.validate_ownership()
        before = os.fstat(descriptor)
        if (
            _file_identity(before) != expected_identity
            or not stat.S_ISREG(before.st_mode)
            or int(before.st_nlink) != 1
            or int(before.st_size) > OBS_PROCESS_LEASE_MAX_BYTES
            or (not allow_empty and int(before.st_size) <= 0)
        ):
            raise self._lease_recovery_error(
                f"OBS所有fileの物理状態が不正です: {transaction.root_lease.path / name}。"
            )
        if transaction.root_lease._relative_file_identity(name) != expected_identity:
            raise self._lease_recovery_error(
                f"OBS所有fileのpath bindingが変化しました: "
                f"{transaction.root_lease.path / name}。"
            )
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, OBS_PROCESS_LEASE_MAX_BYTES + 1)
        after = os.fstat(descriptor)
        if (
            len(raw) > OBS_PROCESS_LEASE_MAX_BYTES
            or len(raw) != int(before.st_size)
            or _file_identity(after) != expected_identity
            or int(after.st_size) != int(before.st_size)
            or int(after.st_mtime_ns) != int(before.st_mtime_ns)
            or transaction.root_lease._relative_file_identity(name)
            != expected_identity
        ):
            raise self._lease_recovery_error(
                f"OBS所有fileが読み取り中に変化しました: "
                f"{transaction.root_lease.path / name}。"
            )
        transaction.validate_ownership()
        return raw

    def _parse_process_lease_bytes(self, raw: bytes) -> OBSProcessLease:
        try:
            if not raw or len(raw) > OBS_PROCESS_LEASE_MAX_BYTES:
                raise ValueError("lease payload size is invalid")
            data = json.loads(raw.decode("utf-8"))
            if type(data) is not dict:
                raise ValueError("lease payload is not an object")
            raw_version = data.get("version", 1)
            if type(raw_version) is not int or raw_version not in {
                1,
                OBS_PROCESS_LEASE_SCHEMA_VERSION,
            }:
                raise ValueError("unsupported lease version")
            raw_pid = data.get("pid")
            if type(raw_pid) is not int or raw_pid <= 0:
                raise ValueError("invalid lease pid")
            raw_path = data.get("executable_path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError("invalid lease executable path")
            executable_path = Path(raw_path.strip())
            if not _is_absolute_obs_process_path(executable_path):
                raise ValueError("lease executable path is not absolute")
            if executable_path.is_absolute():
                executable_path = executable_path.resolve()

            raw_created_at = data.get("created_at")
            if (
                isinstance(raw_created_at, bool)
                or not isinstance(raw_created_at, (int, float))
                or not math.isfinite(float(raw_created_at))
                or float(raw_created_at) <= 0
            ):
                raise ValueError("invalid lease creation time")

            raw_process_time = data.get("process_creation_time")
            process_creation_time = None
            if raw_process_time is not None:
                if (
                    isinstance(raw_process_time, bool)
                    or not isinstance(raw_process_time, (int, float))
                    or not math.isfinite(float(raw_process_time))
                    or float(raw_process_time) <= 0
                ):
                    raise ValueError("invalid process creation time")
                process_creation_time = float(raw_process_time)

            raw_filetime = data.get("process_creation_time_filetime")
            process_creation_time_filetime = None
            if raw_filetime is not None:
                if type(raw_filetime) is not int or raw_filetime <= 0:
                    raise ValueError("invalid raw process creation FILETIME")
                process_creation_time_filetime = raw_filetime

            if raw_version == OBS_PROCESS_LEASE_SCHEMA_VERSION:
                if (
                    process_creation_time is None
                    or process_creation_time_filetime is None
                ):
                    raise ValueError("v2 lease is missing complete process identity")
                filetime_seconds = (
                    process_creation_time_filetime / 10_000_000
                    - _WINDOWS_FILETIME_UNIX_EPOCH_SECONDS
                )
                if abs(process_creation_time - filetime_seconds) > 0.001:
                    raise ValueError("v2 lease creation values disagree")

            return OBSProcessLease(
                schema_version=raw_version,
                pid=raw_pid,
                executable_path=executable_path,
                created_at=float(raw_created_at),
                process_creation_time=process_creation_time,
                process_creation_time_filetime=process_creation_time_filetime,
            )
        except OBSProcessLeaseError:
            raise
        except Exception as exc:
            raise OBSProcessLeaseError("OBS process lease is malformed") from exc

    def _open_process_lease_snapshot_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
    ) -> _OBSProcessLeaseFileSnapshot | None:
        if transaction.snapshot is not None:
            self._revalidate_process_lease_snapshot_locked(
                transaction,
                transaction.snapshot,
            )
            return transaction.snapshot

        try:
            transaction.validate_ownership()
        except (OSError, OBSPathSafetyError) as exc:
            raise self._lease_recovery_error(
                "OBS所有情報を固定する前にroot/lockを再検証できません。",
                cause=exc,
            ) from exc
        descriptor: int | None = None
        primary_error: BaseException | None = None
        try:
            descriptor = transaction.root_lease.open_file(
                self.lease_path.name,
                write=False,
                create_exclusive=False,
                delete=True,
                share_write=False,
                share_delete=False,
            )
        except FileNotFoundError:
            return None
        except (OSError, OBSPathSafetyError) as exc:
            raise self._lease_recovery_error(
                "OBS所有情報を同一handleへ固定できません。",
                cause=exc,
            ) from exc

        try:
            identity = _file_identity(os.fstat(descriptor))
            raw = self._read_process_lease_descriptor_bytes_locked(
                transaction,
                descriptor,
                name=self.lease_path.name,
                expected_identity=identity,
            )
            try:
                lease = self._parse_process_lease_bytes(raw)
            except OBSProcessLeaseError as exc:
                raise self._lease_recovery_error(
                    "OBS所有情報のJSON/schemaがmalformedです。",
                    cause=exc,
                ) from exc
            snapshot = _OBSProcessLeaseFileSnapshot(
                descriptor=descriptor,
                identity=identity,
                raw_bytes=raw,
                lease=lease,
            )
            transaction.snapshot = snapshot
            descriptor = None
            return snapshot
        except OBSProcessLeaseError as exc:
            primary_error = exc
            raise
        except (OSError, OBSPathSafetyError) as exc:
            error = self._lease_recovery_error(
                "OBS所有情報を固定handleから安全に読み取れません。",
                cause=exc,
            )
            primary_error = error
            raise error from exc
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if descriptor is not None:
                self._close_descriptor_preserving_primary(
                    descriptor,
                    primary_error=primary_error,
                    label="Pinned OBS lease descriptor close also failed",
                )

    def _revalidate_process_lease_snapshot_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
        snapshot: _OBSProcessLeaseFileSnapshot,
    ) -> None:
        try:
            if transaction.snapshot is not snapshot or snapshot.deletion_marked:
                raise self._lease_recovery_error(
                    "OBS所有情報の固定snapshotは現在のtransactionに属しません。"
                )
            raw = self._read_process_lease_descriptor_bytes_locked(
                transaction,
                snapshot.descriptor,
                name=self.lease_path.name,
                expected_identity=snapshot.identity,
            )
            if (
                raw != snapshot.raw_bytes
                or self._parse_process_lease_bytes(raw) != snapshot.lease
            ):
                raise self._lease_recovery_error(
                    "OBS所有情報がtransaction中に変化しました。"
                )
        except OBSProcessLeaseError as exc:
            if (
                str(self.lease_path) in str(exc)
                and str(self.lease_lock_path) in str(exc)
            ):
                raise
            raise self._lease_recovery_error(
                "OBS所有情報の固定snapshotを再検証できません。",
                cause=exc,
            ) from exc
        except (OSError, OBSPathSafetyError) as exc:
            raise self._lease_recovery_error(
                "OBS所有情報の固定snapshotを再検証できません。",
                cause=exc,
            ) from exc

    def _delete_process_lease_snapshot_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
        snapshot: _OBSProcessLeaseFileSnapshot,
    ) -> None:
        self._revalidate_process_lease_snapshot_locked(transaction, snapshot)
        try:
            close_failure = transaction.root_lease.delete_open_file_on_close(
                snapshot.descriptor,
                self.lease_path.name,
                expected_identity=snapshot.identity,
            )
            snapshot.deletion_marked = True
            transaction.commit_occurred = True
            if close_failure is not None:
                self.logger.critical(
                    "OBS lease deletion committed but close failed: %s: %s",
                    self.lease_path,
                    close_failure,
                )
                if not isinstance(close_failure, Exception):
                    raise close_failure
        except (OSError, OBSPathSafetyError) as exc:
            raise self._lease_recovery_error(
                "検証済みOBS所有情報を同一handleから削除できません。",
                cause=exc,
            ) from exc
        finally:
            if snapshot.deletion_marked:
                transaction.snapshot = None

    def list_obs_processes(self) -> list[OBSProcessInfo]:
        if os.name != "nt":
            return []
        return self._list_obs_processes_windows()

    def query_obs_processes_strict(self) -> OBSProcessQuerySnapshot:
        """Return a successful process-query snapshot or raise.

        ``list_obs_processes`` intentionally remains best-effort for legacy callers.
        Safety-sensitive callers use this method so an empty result cannot conceal a
        failed or malformed Windows process query.
        """
        if os.name != "nt":
            return OBSProcessQuerySnapshot(processes=(), queried_at=time.time())
        return OBSProcessQuerySnapshot(
            processes=tuple(self._list_obs_processes_powershell_strict()),
            queried_at=time.time(),
        )

    def is_managed_process(self, process: OBSProcessInfo) -> bool:
        if process.executable_path is None:
            return False
        if _obs_process_paths_equal(process.executable_path, self.obs_exe):
            return True
        try:
            return _obs_executable_paths_same_physical_file(
                process.executable_path,
                self.obs_exe,
            )
        except Exception:
            try:
                return process.executable_path.resolve() == self.obs_exe
            except Exception:
                return (
                    str(process.executable_path).casefold()
                    == str(self.obs_exe).casefold()
                )

    def _is_managed_process_strict(
        self,
        process: OBSProcessInfo,
        *,
        label: str,
    ) -> bool:
        """Classify one row without treating an unknown physical path as unmanaged."""

        executable_path = process.executable_path
        if executable_path is None:
            raise OBSProcessQueryError(
                f"{label} process path is missing"
            )
        if _obs_process_paths_equal(executable_path, self.obs_exe):
            return True
        try:
            return _obs_executable_paths_same_physical_file(
                executable_path,
                self.obs_exe,
            )
        except Exception as exc:
            raise OBSProcessQueryError(
                f"{label} could not establish executable physical "
                f"identity for PID {process.pid}: {executable_path}"
            ) from exc

    def _owned_process_identities_equal_strict(
        self,
        expected: OBSProcessInfo,
        actual: OBSProcessInfo,
        *,
        label: str,
    ) -> bool:
        """Compare an owned lifetime while allowing only a physical managed alias."""

        if (
            expected.executable_path is None
            or not _obs_process_paths_equal(expected.executable_path, self.obs_exe)
            or not _obs_process_lifetimes_equal(expected, actual)
        ):
            return False
        return self._is_managed_process_strict(actual, label=label)

    def managed_processes(self) -> list[OBSProcessInfo]:
        return [process for process in self.list_obs_processes() if self.is_managed_process(process)]

    def has_managed_process(self) -> bool:
        return bool(self.managed_processes())

    def query_managed_processes_strict(self) -> tuple[OBSProcessInfo, ...]:
        """Return managed OBS identities without hiding query or path failures."""

        snapshot = self.query_obs_processes_strict()
        processes = validate_obs_process_query_snapshot(
            snapshot,
            label="managed OBS verification",
        )
        return tuple(
            process
            for process in processes
            if self._is_managed_process_strict(
                process,
                label="managed OBS verification",
            )
        )

    def unmanaged_processes(self) -> list[OBSProcessInfo]:
        return [process for process in self.list_obs_processes() if not self.is_managed_process(process)]

    def has_unmanaged_process(self) -> bool:
        return bool(self.unmanaged_processes())

    def find_owned_process(self) -> OBSProcessInfo | None:
        with self._process_lease_transaction() as transaction:
            if transaction is None:
                return None
            return self._find_owned_process_locked(transaction)

    def _find_owned_process_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
    ) -> OBSProcessInfo | None:
        lease_snapshot = self._open_process_lease_snapshot_locked(transaction)
        if lease_snapshot is None:
            return None
        lease = lease_snapshot.lease
        _snapshot, process = self._query_owned_process_for_lease(
            lease,
            label="owned process lookup",
        )
        if process is None:
            self._delete_process_lease_snapshot_locked(
                transaction,
                lease_snapshot,
            )
            return None
        if lease.schema_version != OBS_PROCESS_LEASE_SCHEMA_VERSION:
            raise self._lease_recovery_error(
                "Legacy OBS process lease cannot authorize a live process"
            )
        if not self.is_owned_process(process, lease):
            raise self._lease_recovery_error(
                "Live OBS process identity does not match its ownership lease"
            )
        return process

    def has_owned_process(self) -> bool:
        return self.find_owned_process() is not None

    def _query_owned_process_for_lease(
        self,
        lease: OBSProcessLease,
        *,
        label: str,
    ) -> tuple[OBSProcessQuerySnapshot, OBSProcessInfo | None]:
        """Resolve the leased PID from one complete strict snapshot.

        Unrelated OBS processes are intentionally allowed. A row with the leased
        PID must either be the exact v2 lease identity or cause a fail-closed
        error; it is never treated as a harmless stale lease.
        """

        snapshot = self.query_obs_processes_strict()
        processes = validate_obs_process_query_snapshot(snapshot, label=label)
        process = next((item for item in processes if item.pid == lease.pid), None)
        if process is None:
            return snapshot, None
        if lease.schema_version != OBS_PROCESS_LEASE_SCHEMA_VERSION:
            raise self._lease_recovery_error(
                "Legacy OBS process lease refers to a live PID"
            )
        if not self.is_owned_process(process, lease):
            raise self._lease_recovery_error(
                "OBS process lease PID was reused or its identity changed"
            )
        return snapshot, process

    def terminate_expected_obs_processes_strict(
        self,
        expected: tuple[OBSProcessInfo, ...],
        timeout_sec: float = 3.0,
        poll_interval: float = 0.1,
    ) -> OBSStrictTerminationResult:
        """Signal only the exact strict identities selected by the caller.

        A fresh strict query immediately precedes the first signal. During the
        wait and force phases, a new PID or a reused PID with different path or
        creation time aborts without signaling that unexpected process.
        """

        expected_processes = validate_obs_process_query_snapshot(
            OBSProcessQuerySnapshot(
                processes=expected,
                queried_at=time.time(),
            ),
            label="expected termination",
        )
        if any(
            process.executable_path is None
            or not _obs_process_paths_equal(process.executable_path, self.obs_exe)
            for process in expected_processes
        ):
            raise OBSProcessQueryError(
                "Strict termination expected set contains a non-managed OBS path"
            )
        use_windows_handles = self._uses_windows_process_identity_handles()
        if use_windows_handles and any(
            process.creation_time_filetime is None
            for process in expected_processes
        ):
            raise OBSProcessQueryError(
                "Windows strict termination requires creation FILETIME identities"
            )
        expected_by_pid = {process.pid: process for process in expected_processes}

        if not expected_processes:
            initial = self.query_obs_processes_strict()
            initial_processes = validate_obs_process_query_snapshot(
                initial,
                label="pre-signal",
            )
            if initial_processes:
                raise OBSProcessQueryError(
                    "Unexpected OBS process appeared before empty termination pass"
                )
            return OBSStrictTerminationResult(signaled_processes=(), after=initial)

        def strict_current(
            *,
            label: str,
            required: tuple[OBSProcessInfo, ...] = (),
        ) -> tuple[OBSProcessQuerySnapshot, tuple[OBSProcessInfo, ...]]:
            snapshot = self.query_obs_processes_strict()
            processes = validate_obs_process_query_snapshot(snapshot, label=label)
            current_by_pid = {process.pid: process for process in processes}
            if any(expected_by_pid.get(process.pid) != process for process in processes):
                raise OBSProcessQueryError(
                    "Unexpected or replaced OBS process appeared during termination"
                )
            if any(current_by_pid.get(process.pid) != process for process in required):
                raise OBSProcessQueryError(
                    "Expected OBS identity disappeared before its termination signal"
                )
            return snapshot, processes

        if use_windows_handles:
            return self._terminate_expected_obs_processes_with_windows_handles(
                expected_processes,
                strict_current,
                timeout_sec=timeout_sec,
                poll_interval=poll_interval,
            )
        return self._terminate_expected_obs_processes_without_handles(
            expected_processes,
            strict_current,
            timeout_sec=timeout_sec,
            poll_interval=poll_interval,
        )

    def _terminate_expected_obs_processes_without_handles(
        self,
        expected_processes: tuple[OBSProcessInfo, ...],
        strict_current: Callable[..., tuple[OBSProcessQuerySnapshot, tuple[OBSProcessInfo, ...]]],
        *,
        timeout_sec: float,
        poll_interval: float,
    ) -> OBSStrictTerminationResult:
        """Testable non-Windows fallback with per-signal strict revalidation."""

        signaled: list[OBSProcessInfo] = []
        for index, process in enumerate(expected_processes):
            strict_current(
                label="pre-signal",
                required=expected_processes[index:],
            )
            if not self._terminate_pid(process.pid, force=False):
                raise OBSProcessQueryError(
                    f"OBS termination signal failed for pid={process.pid}"
                )
            signaled.append(process)

        deadline = time.monotonic() + max(0.0, timeout_sec)
        force_sent = False
        while True:
            after, remaining = strict_current(label="post-signal")
            if not remaining:
                return OBSStrictTerminationResult(
                    signaled_processes=tuple(signaled),
                    after=after,
                )
            if time.monotonic() < deadline:
                time.sleep(max(0.01, poll_interval))
                continue
            if force_sent:
                raise OBSProcessQueryError(
                    "Expected OBS processes survived strict forced termination"
                )
            for process in remaining:
                _snapshot, current = strict_current(label="pre-force-signal")
                current_by_pid = {item.pid: item for item in current}
                if current_by_pid.get(process.pid) is None:
                    continue
                if not self._terminate_pid(process.pid, force=True):
                    raise OBSProcessQueryError(
                        f"OBS forced termination signal failed for pid={process.pid}"
                    )
            force_sent = True
            deadline = time.monotonic() + max(0.0, timeout_sec)

    def _terminate_expected_obs_processes_with_windows_handles(
        self,
        expected_processes: tuple[OBSProcessInfo, ...],
        strict_current: Callable[..., tuple[OBSProcessQuerySnapshot, tuple[OBSProcessInfo, ...]]],
        *,
        timeout_sec: float,
        poll_interval: float,
    ) -> OBSStrictTerminationResult:
        """Bind each Windows signal to a validated process handle.

        The handle remains open through the final strict zero snapshot. This
        prevents the PID from being reused between identity validation and the
        PID-based graceful ``taskkill`` signal. Forced termination uses the
        already validated handle directly.
        """

        opened: list[tuple[OBSProcessInfo, Any]] = []
        signaled: list[OBSProcessInfo] = []
        operation_error: BaseException | None = None
        try:
            for index, process in enumerate(expected_processes):
                strict_current(
                    label="pre-signal",
                    required=expected_processes[index:],
                )
                handle = self._open_process_identity_handle(process.pid)
                opened.append((process, handle))
                handle_identity = self._query_process_identity_from_handle(
                    handle,
                    process.pid,
                )
                validate_obs_process_query_snapshot(
                    OBSProcessQuerySnapshot(
                        (handle_identity,),
                        time.time(),
                    ),
                    label="handle identity",
                )
                if not _obs_process_identities_equal(process, handle_identity):
                    raise OBSProcessQueryError(
                        "Windows process handle identity does not match strict query"
                    )
                if self._wait_process_identity_handle(handle, timeout_ms=0):
                    raise OBSProcessQueryError(
                        "Expected OBS process exited before its termination signal"
                    )
                if not self._terminate_pid(process.pid, force=False):
                    raise OBSProcessQueryError(
                        f"OBS termination signal failed for pid={process.pid}"
                    )
                signaled.append(process)

            deadline = time.monotonic() + max(0.0, timeout_sec)
            force_sent = False
            exited_row_requeries: set[OBSProcessInfo] = set()
            while True:
                after, remaining = strict_current(label="post-signal")
                current_by_pid = {process.pid: process for process in remaining}
                live: list[tuple[OBSProcessInfo, Any]] = []
                retained_exited_rows: list[OBSProcessInfo] = []
                for process, handle in opened:
                    exited = self._wait_process_identity_handle(handle, timeout_ms=0)
                    current = current_by_pid.get(process.pid)
                    if exited:
                        if current is not None:
                            retained_exited_rows.append(process)
                    else:
                        if current != process:
                            raise OBSProcessQueryError(
                                "Live OBS handle is missing from the strict process snapshot"
                            )
                        live.append((process, handle))
                if retained_exited_rows:
                    if any(
                        process in exited_row_requeries
                        for process in retained_exited_rows
                    ):
                        raise OBSProcessQueryError(
                            "Strict query repeatedly retained an exited OBS identity"
                        )
                    exited_row_requeries.update(retained_exited_rows)
                    continue
                if not live:
                    if remaining:
                        raise OBSProcessQueryError(
                            "Unexpected OBS process remained after all handles exited"
                        )
                    return OBSStrictTerminationResult(
                        signaled_processes=tuple(signaled),
                        after=after,
                    )
                if time.monotonic() < deadline:
                    time.sleep(max(0.01, poll_interval))
                    continue
                if force_sent:
                    raise OBSProcessQueryError(
                        "Expected OBS processes survived handle-bound forced termination"
                    )
                for process, handle in live:
                    _snapshot, current = strict_current(
                        label="pre-force-signal",
                    )
                    current_by_pid = {item.pid: item for item in current}
                    if current_by_pid.get(process.pid) is None:
                        if self._wait_process_identity_handle(handle, timeout_ms=0):
                            continue
                        raise OBSProcessQueryError(
                            "Live OBS handle is missing before forced termination"
                        )
                    if self._wait_process_identity_handle(handle, timeout_ms=0):
                        continue
                    try:
                        handle_identity = self._query_process_identity_from_handle(
                            handle,
                            process.pid,
                        )
                    except OSError:
                        if self._wait_process_identity_handle(handle, timeout_ms=0):
                            continue
                        raise
                    if not _obs_process_identities_equal(process, handle_identity):
                        raise OBSProcessQueryError(
                            "Windows process handle identity changed before forced termination"
                        )
                    if self._wait_process_identity_handle(handle, timeout_ms=0):
                        continue
                    if not self._terminate_process_identity_handle(handle):
                        if self._wait_process_identity_handle(handle, timeout_ms=0):
                            continue
                        raise OBSProcessQueryError(
                            f"Handle-bound forced termination failed for pid={process.pid}"
                        )
                force_sent = True
                deadline = time.monotonic() + max(0.0, timeout_sec)
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            close_error: Exception | None = None
            for _process, handle in reversed(opened):
                try:
                    self._close_process_identity_handle(handle)
                except Exception as exc:
                    if close_error is None:
                        close_error = exc
            if close_error is not None:
                if operation_error is None:
                    raise OBSProcessQueryError(
                        "Windows OBS process identity handle could not be closed"
                    ) from close_error
                add_note = getattr(operation_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "CloseHandle also failed while unwinding strict OBS termination: "
                        f"{type(close_error).__name__}: {close_error}"
                    )
                self.logger.critical(
                    "CloseHandle also failed while unwinding strict OBS termination: %s",
                    close_error,
                )

    def kill_stale_owned_processes(
        self, timeout_sec: float = 3.0, *, allow_force: bool = True
    ) -> list[int]:
        """Stop only the exact v2 identity recorded by this application's lease."""

        with self._process_lease_transaction() as transaction:
            if transaction is None:
                return []
            return self._kill_stale_owned_processes_locked(
                transaction,
                timeout_sec=timeout_sec,
                allow_force=allow_force,
            )

    def _kill_stale_owned_processes_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
        *,
        timeout_sec: float,
        allow_force: bool,
    ) -> list[int]:
        lease_snapshot = self._open_process_lease_snapshot_locked(transaction)
        if lease_snapshot is None:
            return []
        lease = lease_snapshot.lease
        _snapshot, process = self._query_owned_process_for_lease(
            lease,
            label="stale owned process lookup",
        )
        if process is None:
            self._delete_process_lease_snapshot_locked(
                transaction,
                lease_snapshot,
            )
            return []

        def validate_authorization() -> None:
            self._revalidate_process_lease_snapshot_locked(
                transaction,
                lease_snapshot,
            )
            if lease_snapshot.lease != lease or not self.is_owned_process(
                process,
                lease,
            ):
                raise self._lease_recovery_error(
                    "OBS停止認可のv2 identityが一致しません。"
                )

        result = self.terminate_owned_process_strict(
            process,
            timeout_sec=timeout_sec,
            allow_force=allow_force,
            validate_authorization=validate_authorization,
        )
        validate_authorization()
        self._delete_process_lease_snapshot_locked(
            transaction,
            lease_snapshot,
        )
        return [item.pid for item in result.signaled_processes]

    def terminate_owned_process_strict(
        self,
        expected: OBSProcessInfo,
        *,
        timeout_sec: float = 3.0,
        poll_interval: float = 0.1,
        validate_authorization: Callable[[], None] | None = None,
        allow_force: bool = True,
    ) -> OBSStrictTerminationResult:
        """Stop one fixed owned identity while leaving unrelated OBS untouched."""

        validate_obs_process_query_snapshot(
            OBSProcessQuerySnapshot((expected,), time.time()),
            label="owned termination target",
        )
        if expected.executable_path is None or not _obs_process_paths_equal(
            expected.executable_path,
            self.obs_exe,
        ):
            raise OBSProcessQueryError(
                "Owned termination target does not use the managed OBS executable"
            )
        if self._uses_windows_process_identity_handles():
            if expected.creation_time_filetime is None:
                raise OBSProcessQueryError(
                    "Windows owned termination requires a raw creation FILETIME"
                )
            return self._terminate_owned_process_with_windows_handle(
                expected,
                timeout_sec=timeout_sec,
                poll_interval=poll_interval,
                validate_authorization=validate_authorization,
                allow_force=allow_force,
            )
        return self._terminate_owned_process_without_handle(
            expected,
            timeout_sec=timeout_sec,
            poll_interval=poll_interval,
            validate_authorization=validate_authorization,
            allow_force=allow_force,
        )

    def _query_owned_target_strict(
        self,
        expected: OBSProcessInfo,
        *,
        label: str,
    ) -> tuple[OBSProcessQuerySnapshot, OBSProcessInfo | None]:
        snapshot = self.query_obs_processes_strict()
        processes = validate_obs_process_query_snapshot(snapshot, label=label)
        current = next((item for item in processes if item.pid == expected.pid), None)
        if current is not None:
            if not self._owned_process_identities_equal_strict(
                expected,
                current,
                label=label,
            ):
                raise _OBSOwnedProcessIdentityMismatchError(
                    "Owned OBS PID was replaced during strict termination"
                )
        return snapshot, current

    def _terminate_owned_process_without_handle(
        self,
        expected: OBSProcessInfo,
        *,
        timeout_sec: float,
        poll_interval: float,
        validate_authorization: Callable[[], None] | None,
        allow_force: bool,
    ) -> OBSStrictTerminationResult:
        """Test fallback; Windows production uses the handle-bound implementation."""

        before, current = self._query_owned_target_strict(
            expected,
            label="owned pre-signal",
        )
        if current is None:
            return OBSStrictTerminationResult((), before)

        signaled = False
        if validate_authorization is not None:
            validate_authorization()
        if self._terminate_pid(expected.pid, force=False):
            signaled = True
        else:
            after, current = self._query_owned_target_strict(
                expected,
                label="owned graceful-signal failure",
            )
            if current is None:
                return OBSStrictTerminationResult((), after)
            raise OBSProcessQueryError(
                f"Owned OBS graceful termination failed for pid={expected.pid}"
            )

        deadline = time.monotonic() + max(0.0, timeout_sec)
        force_sent = False
        while True:
            after, current = self._query_owned_target_strict(
                expected,
                label="owned post-signal",
            )
            if current is None:
                return OBSStrictTerminationResult(
                    (expected,) if signaled else (),
                    after,
                )
            if time.monotonic() < deadline:
                time.sleep(max(0.01, poll_interval))
                continue
            if not allow_force:
                raise OBSProcessQueryError(
                    "Owned OBS survived graceful termination"
                )
            if force_sent:
                raise OBSProcessQueryError(
                    "Owned OBS survived strict forced termination"
                )
            _snapshot, current = self._query_owned_target_strict(
                expected,
                label="owned pre-force-signal",
            )
            if current is None:
                continue
            if validate_authorization is not None:
                validate_authorization()
            if not self._terminate_pid(expected.pid, force=True):
                after, current = self._query_owned_target_strict(
                    expected,
                    label="owned force-signal failure",
                )
                if current is None:
                    return OBSStrictTerminationResult(
                        (expected,) if signaled else (),
                        after,
                    )
                raise OBSProcessQueryError(
                    f"Owned OBS forced termination failed for pid={expected.pid}"
                )
            signaled = True
            force_sent = True
            deadline = time.monotonic() + max(0.0, timeout_sec)

    def _terminate_owned_process_with_windows_handle(
        self,
        expected: OBSProcessInfo,
        *,
        timeout_sec: float,
        poll_interval: float,
        validate_authorization: Callable[[], None] | None,
        allow_force: bool,
    ) -> OBSStrictTerminationResult:
        """Bind graceful verification and forced termination to one held handle."""

        handle: Any | None = None
        operation_error: BaseException | None = None
        signaled = False
        try:
            before, current = self._query_owned_target_strict(
                expected,
                label="owned pre-handle",
            )
            if current is None:
                return OBSStrictTerminationResult((), before)

            try:
                handle = self._open_process_identity_handle(expected.pid)
            except OSError:
                after, current = self._query_owned_target_strict(
                    expected,
                    label="owned handle-open failure",
                )
                if current is None:
                    return OBSStrictTerminationResult((), after)
                raise
            if self._wait_process_identity_handle(handle, timeout_ms=0):
                return self._finish_naturally_exited_owned_handle(expected, handle)
            try:
                handle_identity = self._query_process_identity_from_handle(
                    handle,
                    expected.pid,
                )
            except OSError:
                if self._wait_process_identity_handle(handle, timeout_ms=0):
                    return self._finish_naturally_exited_owned_handle(
                        expected,
                        handle,
                    )
                raise
            validate_obs_process_query_snapshot(
                OBSProcessQuerySnapshot((handle_identity,), time.time()),
                label="owned handle identity",
            )
            if not self._owned_process_identities_equal_strict(
                expected,
                handle_identity,
                label="owned handle identity",
            ):
                raise OBSProcessQueryError(
                    "Owned OBS handle identity does not match its lease target"
                )
            if self._wait_process_identity_handle(handle, timeout_ms=0):
                return self._finish_naturally_exited_owned_handle(expected, handle)

            try:
                _snapshot, current = self._query_owned_target_strict(
                    expected,
                    label="owned pre-signal",
                )
            except _OBSOwnedProcessIdentityMismatchError:
                raise
            except OBSProcessQueryError as query_error:
                if self._wait_process_identity_handle(handle, timeout_ms=0):
                    return self._finish_naturally_exited_owned_handle(
                        expected,
                        handle,
                        initial_query_error=query_error,
                    )
                raise
            if current is None:
                if self._wait_process_identity_handle(handle, timeout_ms=0):
                    return self._finish_naturally_exited_owned_handle(
                        expected,
                        handle,
                    )
                raise OBSProcessQueryError(
                    "Live owned OBS handle disappeared before graceful termination"
                )
            if self._wait_process_identity_handle(handle, timeout_ms=0):
                return self._finish_naturally_exited_owned_handle(expected, handle)
            if validate_authorization is not None:
                validate_authorization()
            if self._terminate_pid(expected.pid, force=False):
                signaled = True
            else:
                if self._wait_process_identity_handle(handle, timeout_ms=0):
                    return self._finish_naturally_exited_owned_handle(expected, handle)
                raise OBSProcessQueryError(
                    f"Owned OBS graceful termination failed for pid={expected.pid}"
                )

            deadline = time.monotonic() + max(0.0, timeout_sec)
            force_sent = False
            retained_exited_row = False
            while True:
                try:
                    after, current = self._query_owned_target_strict(
                        expected,
                        label="owned post-signal",
                    )
                except _OBSOwnedProcessIdentityMismatchError:
                    raise
                except OBSProcessQueryError as query_error:
                    if retained_exited_row:
                        raise
                    if self._wait_process_identity_handle(handle, timeout_ms=0):
                        return self._finish_naturally_exited_owned_handle(
                            expected,
                            handle,
                            initial_query_error=query_error,
                            signal_was_sent=signaled,
                        )
                    raise
                exited = self._wait_process_identity_handle(handle, timeout_ms=0)
                if exited:
                    if current is None:
                        return OBSStrictTerminationResult(
                            (expected,) if signaled else (),
                            after,
                        )
                    if retained_exited_row:
                        raise OBSProcessQueryError(
                            "Strict query repeatedly retained an exited owned OBS identity"
                        )
                    retained_exited_row = True
                    continue
                if current is None:
                    raise OBSProcessQueryError(
                        "Live owned OBS handle is missing from the strict snapshot"
                    )
                if time.monotonic() < deadline:
                    time.sleep(max(0.01, poll_interval))
                    continue
                if not allow_force:
                    raise OBSProcessQueryError(
                        "Owned OBS survived graceful termination"
                    )
                if force_sent:
                    raise OBSProcessQueryError(
                        "Owned OBS survived handle-bound forced termination"
                    )

                try:
                    _snapshot, current = self._query_owned_target_strict(
                        expected,
                        label="owned pre-force-signal",
                    )
                except _OBSOwnedProcessIdentityMismatchError:
                    raise
                except OBSProcessQueryError as query_error:
                    if self._wait_process_identity_handle(handle, timeout_ms=0):
                        return self._finish_naturally_exited_owned_handle(
                            expected,
                            handle,
                            initial_query_error=query_error,
                            signal_was_sent=signaled,
                        )
                    raise
                if current is None:
                    if self._wait_process_identity_handle(handle, timeout_ms=0):
                        continue
                    raise OBSProcessQueryError(
                        "Live owned OBS handle disappeared before forced termination"
                    )
                if self._wait_process_identity_handle(handle, timeout_ms=0):
                    continue
                try:
                    handle_identity = self._query_process_identity_from_handle(
                        handle,
                        expected.pid,
                    )
                except OSError:
                    if self._wait_process_identity_handle(handle, timeout_ms=0):
                        continue
                    raise
                if not self._owned_process_identities_equal_strict(
                    expected,
                    handle_identity,
                    label="owned pre-force handle identity",
                ):
                    raise OBSProcessQueryError(
                        "Owned OBS handle identity changed before forced termination"
                    )
                if self._wait_process_identity_handle(handle, timeout_ms=0):
                    continue
                if validate_authorization is not None:
                    validate_authorization()
                if self._terminate_process_identity_handle(handle):
                    signaled = True
                elif not self._wait_process_identity_handle(handle, timeout_ms=0):
                    raise OBSProcessQueryError(
                        f"Handle-bound owned OBS termination failed for pid={expected.pid}"
                    )
                force_sent = True
                deadline = time.monotonic() + max(0.0, timeout_sec)
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            if handle is not None:
                try:
                    self._close_process_identity_handle(handle)
                except Exception as close_error:
                    if operation_error is None:
                        raise OBSProcessQueryError(
                            "Owned OBS process identity handle could not be closed"
                        ) from close_error
                    add_note = getattr(operation_error, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "CloseHandle also failed while unwinding owned OBS termination: "
                            f"{type(close_error).__name__}: {close_error}"
                        )
                    self.logger.critical(
                        "CloseHandle also failed while unwinding owned OBS termination: %s",
                        close_error,
                    )

    def _finish_naturally_exited_owned_handle(
        self,
        expected: OBSProcessInfo,
        handle: Any,
        *,
        initial_query_error: OBSProcessQueryError | None = None,
        signal_was_sent: bool = False,
    ) -> OBSStrictTerminationResult:
        """Require strict PID absence, allowing one bounded strict requery."""

        attempts = 1 if initial_query_error is not None else 2
        if initial_query_error is not None:
            time.sleep(_OWNED_EXIT_STRICT_REQUERY_DELAY_SEC)

        for attempt in range(attempts):
            try:
                after, current = self._query_owned_target_strict(
                    expected,
                    label="owned natural exit",
                )
            except _OBSOwnedProcessIdentityMismatchError:
                raise
            except OBSProcessQueryError:
                if not self._wait_process_identity_handle(handle, timeout_ms=0):
                    raise
                if attempt + 1 < attempts:
                    time.sleep(_OWNED_EXIT_STRICT_REQUERY_DELAY_SEC)
                    continue
                raise
            if not self._wait_process_identity_handle(handle, timeout_ms=0):
                raise OBSProcessQueryError(
                    "Owned OBS handle became live again during exit verification"
                )
            if current is None:
                return OBSStrictTerminationResult(
                    (expected,) if signal_was_sent else (),
                    after,
                )
            if attempt + 1 < attempts:
                time.sleep(_OWNED_EXIT_STRICT_REQUERY_DELAY_SEC)
                continue
        raise OBSProcessQueryError(
            "Strict query repeatedly retained a naturally exited owned OBS identity"
        )

    def wait_until_no_managed_processes(self, timeout_sec: float = 5.0, poll_interval: float = 0.2) -> bool:
        """管理OBSプロセスが完全に消えるまでブロッキング待機する。"""
        deadline = time.monotonic() + max(0.0, timeout_sec)
        while True:
            remaining = [process for process in self.list_obs_processes() if self.is_managed_process(process)]
            if not remaining:
                return True
            if time.monotonic() >= deadline:
                self.logger.warning(
                    "Managed OBS processes are still running: %s",
                    ", ".join(str(process.pid) for process in remaining),
                )
                return False
            time.sleep(max(0.05, poll_interval))

    def start_obs(
        self,
        env: dict[str, str] | None = None,
        hidden: bool = True,
        extra_args: list[str] | None = None,
    ) -> subprocess.Popen[Any]:
        if not self.obs_exe.exists():
            raise FileNotFoundError(f"obs64.exe was not found: {self.obs_exe}")

        process: subprocess.Popen[Any] | None = None
        process_cleanup_attempted = False
        try:
            with self._process_lease_transaction(mutating=True) as transaction:
                if transaction is None:
                    raise AssertionError(
                        "mutating lease transaction did not acquire a lock"
                    )
                try:
                    with self._obs_start_admission_guard_locked(
                        transaction
                    ) as admission_guard:
                        cmd = [
                            str(admission_guard.launch_path),
                            "--portable",
                            "--multi",
                            "--disable-shutdown-check",
                            "--disable-updater",
                            *(extra_args or []),
                        ]
                        popen_kwargs: dict[str, Any] = {
                            "cwd": str(admission_guard.launch_path.parent),
                            "env": env or os.environ.copy(),
                        }
                        if hidden:
                            popen_kwargs.update(self._hidden_subprocess_kwargs())
                        process = subprocess.Popen(cmd, **popen_kwargs)
                        self._revalidate_obs_start_guard_locked(
                            transaction,
                            admission_guard,
                            label="OBS start post-Popen guard",
                        )
                        self._validate_started_popen_against_admission_guard(
                            process,
                            admission_guard,
                        )
                        self._write_process_lease_locked(
                            transaction,
                            process,
                            expected_executable_identity=(
                                admission_guard.managed_pin.physical_identity
                            ),
                        )
                except BaseException as lease_error:
                    if process is None:
                        raise
                    process_cleanup_attempted = True
                    try:
                        self._terminate_unleased_popen_process(process)
                    except BaseException as cleanup_error:
                        control_flow_error = (
                            self._select_control_flow_cleanup_failure(
                                lease_error,
                                cleanup_error,
                                context=(
                                    "Automatic cleanup of the newly started OBS "
                                    f"failed for PID {process.pid}. "
                                    "タスク マネージャーでOBSを確認し、残っている"
                                    "場合は手動終了してから再試行してください。 "
                                    f"lease={self.lease_path} "
                                    f"lock={self.lease_lock_path}"
                                ),
                            )
                        )
                        if control_flow_error is cleanup_error:
                            raise
                        if control_flow_error is None:
                            cleanup_failure = OBSProcessLeaseCleanupError(
                                "OBSの所有情報を確立できず、起動したOBSの終了処理も"
                                f"安全に完了できませんでした (PID {process.pid})。"
                                "タスク マネージャーでOBSを確認し、残っている場合は"
                                "手動終了してから再実行してください。"
                            )
                            add_note = getattr(cleanup_failure, "add_note", None)
                            if callable(add_note):
                                add_note(
                                    "Lease establishment failed first: "
                                    f"{type(lease_error).__name__}: {lease_error}"
                                )
                                add_note(
                                    "Automatic process cleanup also failed: "
                                    f"{type(cleanup_error).__name__}: "
                                    f"{cleanup_error}"
                                )
                            self.logger.critical(
                                "New OBS process cleanup failed after lease failure: "
                                "pid=%s error=%s",
                                process.pid,
                                cleanup_error,
                            )
                            raise cleanup_failure from lease_error
                    raise
        except BaseException as start_error:
            if (
                process is not None
                and not process_cleanup_attempted
                and not isinstance(start_error, Exception)
            ):
                process_cleanup_attempted = True
                try:
                    self._terminate_unleased_popen_process(process)
                except BaseException as cleanup_error:
                    control_flow_error = self._select_control_flow_cleanup_failure(
                        start_error,
                        cleanup_error,
                        context=(
                            "Automatic cleanup after the OBS lease transaction "
                            f"interruption failed for PID {process.pid}. "
                            "タスク マネージャーでOBSを確認し、残っている場合は"
                            "手動終了してから再試行してください。 "
                            f"lease={self.lease_path} lock={self.lease_lock_path}"
                        )
                    )
                    if control_flow_error is cleanup_error:
                        raise
            raise
        if process is None:
            raise AssertionError("OBS Popen was not established")
        return process

    @contextmanager
    def _obs_start_admission_guard_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
    ):
        """Pin every admission path until the new Popen has a verified lease."""

        if self._open_process_lease_snapshot_locked(transaction) is not None:
            raise self._lease_recovery_error(
                "既存のOBS所有情報があるため新しいPopenを生成しません。"
            )

        try:
            snapshot = self.query_obs_processes_strict()
            processes = validate_obs_process_query_snapshot(
                snapshot,
                label="OBS start admission",
            )
        except (OBSProcessQueryError, OSError, OBSPathSafetyError) as exc:
            raise self._lease_recovery_error(
                "OBS起動前のstrict process確認を安全に完了できないため"
                "新しいPopenを生成しません。",
                cause=exc,
            ) from exc

        with ExitStack() as stack:
            try:
                managed_launch_path = self.obs_exe
                if not _is_absolute_obs_process_path(managed_launch_path):
                    raise OBSProcessQueryError(
                        "OBS start managed executable path is not absolute"
                    )
                managed_pin = stack.enter_context(
                    _pin_obs_executable_identity(managed_launch_path)
                )
                candidate_pins: list[_OBSExecutableIdentityPin] = []
                managed_processes: list[OBSProcessInfo] = []
                for process in processes:
                    executable_path = process.executable_path
                    if executable_path is None:
                        raise OBSProcessQueryError(
                            "OBS start admission process path is missing"
                        )
                    if _obs_process_paths_equal(
                        executable_path,
                        managed_launch_path,
                    ):
                        managed_processes.append(process)
                        continue
                    candidate_pin = stack.enter_context(
                        _pin_obs_executable_identity(executable_path)
                    )
                    candidate_pin.revalidate()
                    if (
                        candidate_pin.physical_identity
                        == managed_pin.physical_identity
                    ):
                        managed_processes.append(process)
                    else:
                        candidate_pins.append(candidate_pin)
                if managed_processes:
                    pids = ", ".join(
                        str(process.pid) for process in managed_processes
                    )
                    raise self._lease_recovery_error(
                        "OBS所有情報がない状態で管理対象OBSが既に実行中です。"
                        "新しいPopenを生成せず、既存processへsignalしません。"
                        f"タスク マネージャーで手動確認してください (PID {pids})。"
                    )
                guard = _OBSStartAdmissionGuard(
                    managed_pin=managed_pin,
                    candidate_pins=tuple(candidate_pins),
                    launch_path=managed_launch_path,
                )
                self._revalidate_obs_start_guard_locked(
                    transaction,
                    guard,
                    label="OBS start pre-Popen guard",
                )
            except OBSProcessLeaseError:
                raise
            except (OBSProcessQueryError, OSError, OBSPathSafetyError) as exc:
                raise self._lease_recovery_error(
                    "OBS起動前の実行file物理identityを安全に固定できないため"
                    "新しいPopenを生成しません。",
                    cause=exc,
                ) from exc
            yield guard

    def _revalidate_obs_start_guard_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
        guard: _OBSStartAdmissionGuard,
        *,
        label: str,
    ) -> None:
        try:
            guard.revalidate()
            transaction.validate_ownership()
            if self._open_process_lease_snapshot_locked(transaction) is not None:
                raise self._lease_recovery_error(
                    f"{label}中にOBS所有情報が出現したため"
                    "新しいPopenを成功扱いにしません。"
                )
        except OBSProcessLeaseError:
            raise
        except (OBSProcessQueryError, OSError, OBSPathSafetyError) as exc:
            raise self._lease_recovery_error(
                f"{label}を安全に再検証できません。",
                cause=exc,
            ) from exc

    def _validate_started_popen_against_admission_guard(
        self,
        process: subprocess.Popen[Any],
        guard: _OBSStartAdmissionGuard,
    ) -> OBSProcessInfo:
        try:
            process_info = self.query_popen_process_identity(process)
            validate_obs_process_query_snapshot(
                OBSProcessQuerySnapshot((process_info,), time.time()),
                label="new OBS physical executable binding",
            )
            if process_info.pid != int(process.pid):
                raise OBSProcessQueryError(
                    "New OBS Popen identity has an unexpected PID"
                )
            executable_path = process_info.executable_path
            if executable_path is None:
                raise OBSProcessQueryError(
                    "New OBS Popen identity is missing its executable path"
                )
            with _pin_obs_executable_identity(executable_path) as process_pin:
                process_pin.revalidate()
                if (
                    process_pin.physical_identity
                    != guard.managed_pin.physical_identity
                ):
                    raise OBSProcessQueryError(
                        "New OBS Popen image does not match the pinned managed "
                        "executable"
                    )
            guard.revalidate()
            if process.poll() is not None:
                raise OBSProcessQueryError(
                    "New OBS Popen exited during physical executable validation"
                )
            return process_info
        except (OBSProcessQueryError, OSError, OBSPathSafetyError) as exc:
            raise self._lease_recovery_error(
                "起動したOBSを固定済みmanaged executableへ安全に結び付けられません。",
                cause=exc,
            ) from exc

    def latest_log_path(self, since: float | None = None) -> Path | None:
        logs_dir = self.obs_dir / "config" / "obs-studio" / "logs"
        try:
            candidates = [path for path in logs_dir.glob("*.txt") if path.is_file()]
        except Exception:
            return None
        if since is not None:
            candidates = [path for path in candidates if path.stat().st_mtime >= since]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def latest_log_portable_mode(self, since: float | None = None) -> bool | None:
        log_path = self.latest_log_path(since=since)
        if log_path is None:
            return None
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            self.logger.debug("Failed to read OBS log for portable mode check: %s", e)
            return None
        if "Portable mode: true" in text:
            return True
        if "Portable mode: false" in text:
            return False
        return None

    def latest_log_encoder_kinds(self, since: float | None = None) -> list[str]:
        log_path = self.latest_log_path(since=since)
        if log_path is None:
            return []
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            self.logger.debug("Failed to read OBS log for encoder detection: %s", e)
            return []

        _prefix, separator, encoder_section = text.partition("Available Encoders:")
        if not separator:
            return []
        encoder_section = encoder_section.partition("Audio Encoders:")[0]
        encoder_pattern = re.compile(
            r"^(?:\d{2}:\d{2}:\d{2}(?:\.\d+)?:\s*)?\s*-\s+([A-Za-z0-9_]+)\b",
            re.IGNORECASE | re.MULTILINE,
        )
        result = []
        seen = set()
        for match in encoder_pattern.finditer(encoder_section):
            encoder_kind = match.group(1)
            normalized = encoder_kind.casefold()
            if not any(name in normalized for name in ("nvenc", "qsv", "quicksync", "amf", "x264")):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(encoder_kind)
        return result

    def latest_log_recording_diagnostics(
        self,
        since: float | None = None,
        max_lines: int = 8,
        tail_lines: int = 500,
    ) -> list[str]:
        log_path = self.latest_log_path()
        if log_path is None:
            return []
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            self.logger.debug("Failed to read OBS log for recording diagnostics: %s", e)
            return []

        relevant_tokens = (
            "record",
            "recording",
            "output",
            "encoder",
            "nvenc",
            "x264",
            "ffmpeg",
            "failed",
            "error",
            "could not",
        )
        noisy_tokens = (
            "available encoders",
            "video encoders",
            "audio encoders",
            "game dvr background recording",
            "loaded modules",
            "output resolution",
            "output 0:",
            "output 1:",
            "decklink",
            "aja",
            "nvidia audio effects",
            "nvidia video fx",
        )

        diagnostics: list[str] = []
        for line in lines[-max(1, int(tail_lines)) :]:
            if since is not None and not self._is_log_line_at_or_after(log_path, line, since):
                continue
            normalized = line.casefold()
            if not any(token in normalized for token in relevant_tokens):
                continue
            if any(token in normalized for token in noisy_tokens):
                continue
            diagnostics.append(line.strip())
        return diagnostics[-max(1, int(max_lines)) :]

    def _is_log_line_at_or_after(self, log_path: Path, line: str, since: float) -> bool:
        timestamp = self._parse_obs_log_line_timestamp(log_path, line)
        if timestamp is None:
            return False
        return timestamp >= since

    @staticmethod
    def _parse_obs_log_line_timestamp(log_path: Path, line: str) -> float | None:
        match = re.match(r"^(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?:", line)
        if not match:
            return None
        date_match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", log_path.stem)
        if not date_match:
            return None
        microsecond_text = (match.group(4) or "0")[:6].ljust(6, "0")
        try:
            value = datetime(
                int(date_match.group(1)),
                int(date_match.group(2)),
                int(date_match.group(3)),
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
                int(microsecond_text),
            )
        except ValueError:
            return None
        return value.timestamp()

    def hide_main_windows(
        self,
        process: subprocess.Popen[Any] | int,
        timeout_sec: float = 3.0,
        poll_interval: float = 0.1,
    ) -> int:
        if os.name != "nt":
            return 0
        pid = int(process.pid if hasattr(process, "pid") else process)
        deadline = time.monotonic() + max(0.0, timeout_sec)
        hidden = 0
        while True:
            hidden += self._hide_windows_by_pid_windows(pid)
            if hidden > 0 or time.monotonic() >= deadline:
                return hidden
            time.sleep(max(0.05, poll_interval))

    def _hide_windows_by_pid_windows(self, pid: int) -> int:
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:
            return 0

        user32 = ctypes.windll.user32
        target_pid = int(pid)
        hidden_count = 0
        enum_windows_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd: int, _lparam: int) -> bool:
            nonlocal hidden_count
            window_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            if int(window_pid.value) == target_pid and user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 0)  # SW_HIDE
                hidden_count += 1
            return True

        try:
            user32.EnumWindows(enum_windows_proc(callback), 0)
        except Exception as e:
            self.logger.debug("Failed to hide OBS windows for pid=%s: %s", pid, e)
            return hidden_count
        return hidden_count

    def is_owned_process(self, process: OBSProcessInfo, lease: OBSProcessLease) -> bool:
        if lease.schema_version != OBS_PROCESS_LEASE_SCHEMA_VERSION:
            return False
        if (
            lease.process_creation_time is None
            or lease.process_creation_time_filetime is None
        ):
            return False
        if not _obs_process_paths_equal(lease.executable_path, self.obs_exe):
            return False
        expected = OBSProcessInfo(
            pid=lease.pid,
            executable_path=lease.executable_path,
            creation_time=lease.process_creation_time,
            creation_time_filetime=lease.process_creation_time_filetime,
        )
        try:
            return self._owned_process_identities_equal_strict(
                expected,
                process,
                label="owned OBS identity",
            )
        except OBSProcessQueryError:
            return False

    def terminate_process(
        self,
        process: subprocess.Popen[Any] | None,
        timeout_sec: float = 3.0,
        *,
        allow_force: bool = True,
    ) -> None:
        """Stop exactly one Popen-owned process and prove its handle exited.

        This path deliberately does not enumerate processes or signal by PID. The
        ``Popen`` methods operate on the handle owned by the caller, so a later
        same-PID replacement cannot become a termination target. A bound v2
        lease is removed only after that same handle is proven exited.
        """

        if process is None:
            return
        try:
            with self._process_lease_transaction() as transaction:
                lease_snapshot = (
                    None
                    if transaction is None
                    else self._open_process_lease_snapshot_locked(transaction)
                )
                self._terminate_popen_process(
                    process,
                    timeout_sec=timeout_sec,
                    allow_force=allow_force,
                    require_bound_lease=True,
                    transaction=transaction,
                    lease_snapshot=lease_snapshot,
                )
        except OBSProcessTerminationError:
            raise
        except OBSProcessLeaseError as exc:
            try:
                pid = int(process.pid)
            except Exception:
                pid = -1
            error = OBSProcessTerminationError(
                "OBS Popenの停止認可を固定できなかったためsignalを送信しませんでした。"
                f" PID={pid} lease={self.lease_path} lock={self.lease_lock_path}。"
                "すべてのOBS Studioと関連toolを終了し、再試行してください。"
            )
            raise error from exc

    def _terminate_unleased_popen_process(
        self,
        process: subprocess.Popen[Any] | None,
        timeout_sec: float = 3.0,
    ) -> None:
        """Stop a newly created Popen whose lease establishment failed."""

        self._terminate_popen_process(
            process,
            timeout_sec=timeout_sec,
            require_bound_lease=False,
            transaction=None,
            lease_snapshot=None,
        )

    def _terminate_popen_process(
        self,
        process: subprocess.Popen[Any] | None,
        *,
        timeout_sec: float,
        require_bound_lease: bool,
        transaction: _OBSProcessLeaseTransaction | None,
        lease_snapshot: _OBSProcessLeaseFileSnapshot | None,
        allow_force: bool = True,
    ) -> None:
        """Shared handle-only termination primitive for leased and new Popen."""

        if process is None:
            return

        try:
            pid = int(process.pid)
            if pid <= 0:
                raise ValueError("PID must be positive")
        except Exception as exc:
            raise OBSProcessTerminationError(
                "OBS PopenのPIDを安全に確認できませんでした。"
                "OBSを手動で終了してから再試行してください。"
            ) from exc

        failures: list[tuple[str, BaseException]] = []
        exited = self._observe_popen_exit(
            process,
            pid=pid,
            label="initial poll",
            failures=failures,
        )
        raw_lease = getattr(process, _POPEN_OBS_PROCESS_LEASE_ATTRIBUTE, None)
        lease: OBSProcessLease | None = None
        if raw_lease is None:
            if require_bound_lease:
                failures.append(
                    (
                        "bound v2 lease validation",
                        OBSProcessLeaseError(
                            "Popen is missing its bound v2 OBS process lease"
                        ),
                    )
                )
        else:
            try:
                lease = self._validate_bound_popen_lease(
                    process,
                    raw_lease,
                    pid,
                    process_exited=exited is True,
                )
            except Exception as exc:
                failures.append(("bound v2 lease validation", exc))

        if require_bound_lease:
            if lease_snapshot is None:
                failures.append(
                    (
                        "disk v2 lease authorization",
                        OBSProcessLeaseError(
                            "The bound Popen has no pinned disk ownership lease"
                        ),
                    )
                )
            elif lease is not None and lease_snapshot.lease != lease:
                failures.append(
                    (
                        "disk v2 lease authorization",
                        OBSProcessLeaseError(
                            "The pinned disk lease differs from the bound Popen lease"
                        ),
                    )
                )

        if require_bound_lease and (
            lease is None
            or lease_snapshot is None
            or lease_snapshot.lease != lease
        ):
            self._raise_popen_termination_error(
                pid=pid,
                exited=exited,
                failures=failures,
            )

        def validate_signal_authorization() -> None:
            if not require_bound_lease:
                return
            if transaction is None or lease_snapshot is None or lease is None:
                raise OBSProcessLeaseError(
                    "Popen termination is missing its pinned lease transaction"
                )
            self._revalidate_process_lease_snapshot_locked(
                transaction,
                lease_snapshot,
            )
            current_bound = self._validate_bound_popen_lease(
                process,
                getattr(process, _POPEN_OBS_PROCESS_LEASE_ATTRIBUTE, None),
                pid,
                process_exited=False,
            )
            if current_bound != lease or lease_snapshot.lease != lease:
                raise OBSProcessLeaseError(
                    "Popen termination authorization changed before its signal"
                )

        if exited is not True:
            observed_after_lease = self._observe_popen_exit(
                process,
                pid=pid,
                label="post-lease-validation poll",
                failures=failures,
            )
            if observed_after_lease is True:
                exited = True

        graceful_wait_succeeded = False
        if exited is not True:
            try:
                validate_signal_authorization()
            except Exception as exc:
                failures.append(("pre-terminate authorization", exc))
                self._raise_popen_termination_error(
                    pid=pid,
                    exited=exited,
                    failures=failures,
                )
            try:
                process.terminate()
            except Exception as exc:
                failures.append(("Popen.terminate", exc))
            else:
                try:
                    process.wait(timeout=max(0.0, timeout_sec))
                    graceful_wait_succeeded = True
                except subprocess.TimeoutExpired:
                    # A graceful timeout is the expected transition to force cleanup.
                    pass
                except Exception as exc:
                    failures.append(("Popen.wait after terminate", exc))
            exited = self._observe_popen_exit(
                process,
                pid=pid,
                label="post-terminate poll",
                failures=failures,
            )
            if exited is None and graceful_wait_succeeded:
                exited = True

        if not allow_force and exited is not True:
            failures.append(
                (
                    "graceful termination timeout",
                    RuntimeError("OBS process survived graceful termination"),
                )
            )
            self._raise_popen_termination_error(
                pid=pid,
                exited=exited,
                failures=failures,
            )

        force_wait_succeeded = False
        if exited is not True:
            try:
                validate_signal_authorization()
            except Exception as exc:
                failures.append(("pre-kill authorization", exc))
                self._raise_popen_termination_error(
                    pid=pid,
                    exited=exited,
                    failures=failures,
                )
            try:
                process.kill()
            except Exception as exc:
                failures.append(("Popen.kill", exc))
            else:
                try:
                    process.wait(timeout=2)
                    force_wait_succeeded = True
                except subprocess.TimeoutExpired as exc:
                    failures.append(("Popen.wait after kill", exc))
                except Exception as exc:
                    failures.append(("Popen.wait after kill", exc))
            exited = self._observe_popen_exit(
                process,
                pid=pid,
                label="final poll",
                failures=failures,
            )
            if exited is None and force_wait_succeeded:
                exited = True

        if exited is not True:
            failures.append(
                (
                    "final owned-handle verification",
                    RuntimeError("the original Popen handle is still live or unknown"),
                )
            )

        if (
            exited is True
            and lease is not None
            and transaction is not None
            and lease_snapshot is not None
        ):
            try:
                self._revalidate_process_lease_snapshot_locked(
                    transaction,
                    lease_snapshot,
                )
                if lease_snapshot.lease != lease:
                    raise OBSProcessLeaseError(
                        "Pinned lease changed before Popen cleanup"
                    )
                self._delete_process_lease_snapshot_locked(
                    transaction,
                    lease_snapshot,
                )
            except Exception as exc:
                failures.append(("matching v2 lease cleanup", exc))

        if failures:
            self._raise_popen_termination_error(
                pid=pid,
                exited=exited,
                failures=failures,
            )

    def _raise_popen_termination_error(
        self,
        *,
        pid: int,
        exited: bool | None,
        failures: list[tuple[str, BaseException]],
    ) -> None:
        state = (
            "元のOBS processの終了は確認しましたが、終了処理を完全に確定できませんでした。"
            if exited is True
            else "元のOBS processが終了したことを確認できませんでした。"
        )
        error = OBSProcessTerminationError(
            f"{state} PID={pid}。OBSが残っている場合は手動で終了し、"
            "OBS所有情報を確認してから再試行してください。 "
            f"lease={self.lease_path} lock={self.lease_lock_path}。"
            "解決しない場合はすべてのOBS Studioと関連toolを終了してから"
            "再試行してください。"
        )
        add_note = getattr(error, "add_note", None)
        if callable(add_note):
            for label, failure in failures:
                add_note(f"{label}: {type(failure).__name__}: {failure}")
        self.logger.error(
            "Popen-owned OBS cleanup failed for pid=%s: %s",
            pid,
            "; ".join(
                f"{label}={type(failure).__name__}: {failure}"
                for label, failure in failures
            ),
        )
        raise error from failures[0][1]

    def _validate_bound_popen_lease(
        self,
        process: subprocess.Popen[Any],
        raw_lease: Any,
        pid: int,
        *,
        process_exited: bool,
    ) -> OBSProcessLease:
        """Validate a Popen-attached v2 lease without consulting PID listings."""

        if type(raw_lease) is not OBSProcessLease:
            raise OBSProcessLeaseError("Bound OBS process lease is malformed")
        lease = raw_lease
        if (
            lease.schema_version != OBS_PROCESS_LEASE_SCHEMA_VERSION
            or lease.pid != pid
            or isinstance(lease.created_at, bool)
            or not isinstance(lease.created_at, (int, float))
            or not math.isfinite(float(lease.created_at))
            or float(lease.created_at) <= 0
            or lease.process_creation_time is None
            or lease.process_creation_time_filetime is None
            or not _obs_process_paths_equal(lease.executable_path, self.obs_exe)
        ):
            raise OBSProcessLeaseError(
                "Bound OBS process lease does not contain the complete Popen identity"
            )
        expected = OBSProcessInfo(
            pid=lease.pid,
            executable_path=lease.executable_path,
            creation_time=lease.process_creation_time,
            creation_time_filetime=lease.process_creation_time_filetime,
        )
        validate_obs_process_query_snapshot(
            OBSProcessQuerySnapshot((expected,), time.time()),
            label="bound Popen lease",
        )
        filetime_seconds = (
            lease.process_creation_time_filetime / 10_000_000
            - _WINDOWS_FILETIME_UNIX_EPOCH_SECONDS
        )
        if abs(float(lease.process_creation_time) - filetime_seconds) > 0.001:
            raise OBSProcessLeaseError(
                "Bound OBS process lease creation values disagree"
            )

        # A real Windows Popen retains its process handle after exit. Querying
        # that handle binds lease cleanup to the original kernel object without
        # a PID lookup. Lightweight test doubles may not expose ``_handle``.
        handle = getattr(process, "_handle", None)
        if self._uses_windows_process_identity_handles() and handle is not None:
            if process_exited:
                creation_time, creation_filetime = (
                    self._query_process_creation_from_handle(handle, pid)
                )
                # QueryFullProcessImageNameW can fail after exit. The executable
                # path was verified while live before this frozen lease was
                # attached to the same Popen; PID and raw FILETIME remain
                # queryable from the retained handle after exit.
                actual = OBSProcessInfo(
                    pid=pid,
                    executable_path=lease.executable_path,
                    creation_time=creation_time,
                    creation_time_filetime=creation_filetime,
                )
            else:
                try:
                    actual = self._query_process_identity_from_handle(handle, pid)
                except OSError:
                    # The process can exit after the initial handle wait but
                    # before QueryFullProcessImageNameW. Only a newly signaled
                    # same handle may fall back to the immutable live-bound path
                    # plus PID/raw creation FILETIME.
                    if not self._wait_process_identity_handle(
                        handle,
                        timeout_ms=0,
                    ):
                        raise
                    creation_time, creation_filetime = (
                        self._query_process_creation_from_handle(handle, pid)
                    )
                    actual = OBSProcessInfo(
                        pid=pid,
                        executable_path=lease.executable_path,
                        creation_time=creation_time,
                        creation_time_filetime=creation_filetime,
                    )
            validate_obs_process_query_snapshot(
                OBSProcessQuerySnapshot((actual,), time.time()),
                label="bound Popen handle identity",
            )
            try:
                executable_matches = self._is_managed_process_strict(
                    actual,
                    label="bound Popen executable identity",
                )
            except OBSProcessQueryError as exc:
                raise OBSProcessLeaseError(
                    "Bound OBS executable physical identity cannot be verified"
                ) from exc
            if not executable_matches or not _obs_process_lifetimes_equal(
                expected,
                actual,
            ):
                raise OBSProcessLeaseError(
                    "Bound OBS process lease does not match the Popen handle identity"
                )
        return lease

    def _observe_popen_exit(
        self,
        process: subprocess.Popen[Any],
        *,
        pid: int,
        label: str,
        failures: list[tuple[str, BaseException]],
    ) -> bool | None:
        """Observe only the caller-owned Popen/handle; never resolve its PID."""

        poll_exited: bool | None = None
        try:
            poll_exited = process.poll() is not None
        except Exception as exc:
            failures.append((label, exc))

        handle_exited: bool | None = None
        handle = getattr(process, "_handle", None)
        if self._uses_windows_process_identity_handles() and handle is not None:
            try:
                handle_exited = self._wait_process_identity_handle(
                    handle,
                    timeout_ms=0,
                )
            except Exception as exc:
                failures.append((f"{label} owned handle wait", exc))

        if handle_exited is not None:
            if poll_exited is not None and poll_exited != handle_exited:
                failures.append(
                    (
                        f"{label} state agreement",
                        RuntimeError(
                            f"Popen.poll and owned handle disagree for pid={pid}"
                        ),
                    )
                )
            return handle_exited
        return poll_exited

    def write_process_lease(self, process: subprocess.Popen[Any]) -> None:
        with self._process_lease_transaction(mutating=True) as transaction:
            if transaction is None:
                raise AssertionError("mutating lease transaction did not acquire a lock")
            self._write_process_lease_locked(transaction, process)

    def _write_process_lease_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
        process: subprocess.Popen[Any],
        *,
        expected_executable_identity: tuple[int, int | bytes, int] | None = None,
    ) -> None:
        if self._open_process_lease_snapshot_locked(transaction) is not None:
            raise self._lease_recovery_error(
                "既存のOBS所有情報があるため新しいleaseを上書きしません。"
            )

        temporary_name = f"{OBS_PROCESS_LEASE_TEMP_PREFIX}{secrets.token_hex(16)}"
        temporary_path = transaction.root_lease.path / temporary_name
        descriptor: int | None = None
        temporary_identity: tuple[int, int, int] | None = None
        published = False
        operation_error: BaseException | None = None
        try:
            process_info = self.query_popen_process_identity(process)
            validate_obs_process_query_snapshot(
                OBSProcessQuerySnapshot((process_info,), time.time()),
                label="new OBS lease identity",
            )
            executable_path = process_info.executable_path
            executable_matches = False
            if executable_path is not None:
                if expected_executable_identity is None:
                    executable_matches = _obs_process_paths_equal(
                        executable_path,
                        self.obs_exe,
                    )
                else:
                    with _pin_obs_executable_identity(executable_path) as process_pin:
                        process_pin.revalidate()
                        executable_matches = (
                            process_pin.physical_identity
                            == expected_executable_identity
                        )
            if (
                process_info.pid != int(process.pid)
                or not executable_matches
                or process_info.creation_time_filetime is None
            ):
                raise OBSProcessLeaseError(
                    "New OBS Popen identity does not match the managed executable"
                )
            lease = OBSProcessLease(
                schema_version=OBS_PROCESS_LEASE_SCHEMA_VERSION,
                pid=process_info.pid,
                executable_path=self.obs_exe,
                created_at=time.time(),
                process_creation_time=float(process_info.creation_time),
                process_creation_time_filetime=process_info.creation_time_filetime,
            )
            payload = {
                "version": lease.schema_version,
                "pid": lease.pid,
                "executable_path": str(lease.executable_path),
                "created_at": lease.created_at,
                "process_creation_time": lease.process_creation_time,
                "process_creation_time_filetime": lease.process_creation_time_filetime,
            }
            raw = json.dumps(payload, indent=2).encode("utf-8")
            if not raw or len(raw) > OBS_PROCESS_LEASE_MAX_BYTES:
                raise OBSProcessLeaseError(
                    "Generated OBS process lease exceeds the safety limit"
                )
            descriptor = transaction.root_lease.open_file(
                temporary_name,
                write=True,
                create_exclusive=True,
                delete=True,
                share_write=False,
                share_delete=False,
            )
            temporary_identity = _file_identity(os.fstat(descriptor))
            _transaction_fs._write_all(descriptor, raw)
            os.fsync(descriptor)
            if self._read_process_lease_descriptor_bytes_locked(
                transaction,
                descriptor,
                name=temporary_name,
                expected_identity=temporary_identity,
            ) != raw:
                raise OBSProcessLeaseError(
                    "Temporary OBS process lease readback differs from its payload"
                )
            transaction.validate_ownership()
            transaction.root_lease.publish_open_file_no_replace(
                descriptor,
                temporary_name,
                self.lease_path.name,
            )
            published = True
            transaction.commit_occurred = True
            committed = self._read_process_lease_descriptor_bytes_locked(
                transaction,
                descriptor,
                name=self.lease_path.name,
                expected_identity=temporary_identity,
            )
            if committed != raw or self._parse_process_lease_bytes(committed) != lease:
                raise OBSProcessLeaseError(
                    "Committed OBS process lease does not match its verified identity"
                )
            setattr(process, _POPEN_OBS_PROCESS_LEASE_ATTRIBUTE, lease)
        except BaseException as e:
            operation_error = e
            if descriptor is not None and temporary_identity is not None and not published:
                try:
                    target_identity = (
                        transaction.root_lease.relative_file_identity_or_none(
                            self.lease_path.name
                        )
                    )
                    temporary_current_identity = (
                        transaction.root_lease.relative_file_identity_or_none(
                            temporary_name
                        )
                    )
                    if target_identity == temporary_identity:
                        published = True
                        transaction.commit_occurred = True
                    elif (
                        temporary_current_identity == temporary_identity
                        and target_identity is None
                    ):
                        close_failure = (
                            transaction.root_lease.delete_open_file_on_close(
                                descriptor,
                                temporary_name,
                                expected_identity=temporary_identity,
                            )
                        )
                        descriptor = None
                        if close_failure is not None:
                            self.logger.critical(
                                "OBS lease temporary cleanup committed but close failed: "
                                "%s: %s",
                                temporary_path,
                                close_failure,
                            )
                            add_note = getattr(e, "add_note", None)
                            if callable(add_note):
                                add_note(
                                    "Temporary lease cleanup close reported after commit: "
                                    f"{type(close_failure).__name__}: {close_failure}"
                                )
                            if not isinstance(close_failure, Exception):
                                raise close_failure
                    else:
                        add_note = getattr(e, "add_note", None)
                        if callable(add_note):
                            add_note(
                                "OBS lease publish state is commit-uncertain; neither "
                                "pathname was modified during recovery."
                            )
                except BaseException as cleanup_error:
                    if isinstance(e, Exception) and not isinstance(
                        cleanup_error, Exception
                    ):
                        operation_error = cleanup_error
                    control_flow_error = self._select_control_flow_cleanup_failure(
                        e,
                        cleanup_error,
                        context="Temporary OBS lease cleanup failed",
                    )
                    if control_flow_error is cleanup_error:
                        raise
                    if control_flow_error is None:
                        add_note = getattr(e, "add_note", None)
                        if callable(add_note):
                            add_note(
                                "Temporary lease cleanup also failed: "
                                f"{type(cleanup_error).__name__}: {cleanup_error}"
                            )
                        self.logger.critical(
                            "Temporary lease cleanup also failed: %s",
                            cleanup_error,
                        )
            if not isinstance(e, Exception):
                raise
            if isinstance(e, OBSProcessLeaseError):
                if (
                    str(self.lease_path) in str(e)
                    and str(self.lease_lock_path) in str(e)
                ):
                    raise
                raise self._lease_recovery_error(
                    f"Failed to establish the v2 OBS process ownership lease: {e}",
                    cause=e,
                ) from e
            raise self._lease_recovery_error(
                "Failed to establish the v2 OBS process ownership lease",
                cause=e,
            ) from e
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as close_error:
                    if operation_error is not None:
                        control_flow_error = (
                            self._select_control_flow_cleanup_failure(
                                operation_error,
                                close_error,
                                context="OBS lease descriptor close failed",
                            )
                        )
                        if control_flow_error is close_error:
                            raise
                        if control_flow_error is None:
                            add_note = getattr(operation_error, "add_note", None)
                            if callable(add_note):
                                add_note(
                                    "Temporary lease descriptor close also failed: "
                                    f"{type(close_error).__name__}: {close_error}"
                                )
                            self.logger.critical(
                                "OBS lease descriptor close failed while preserving "
                                "the primary operation failure: %s",
                                close_error,
                            )
                    elif published and isinstance(close_error, Exception):
                        self.logger.critical(
                            "Committed OBS lease descriptor close failed: %s: %s",
                            self.lease_path,
                            close_error,
                        )
                    else:
                        raise

    def read_process_lease(self) -> OBSProcessLease | None:
        with self._process_lease_transaction() as transaction:
            if transaction is None:
                return None
            return self._read_process_lease_locked(transaction)

    def _read_process_lease_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
    ) -> OBSProcessLease | None:
        snapshot = self._open_process_lease_snapshot_locked(transaction)
        return None if snapshot is None else snapshot.lease

    def _clear_matching_process_lease(self, expected: OBSProcessLease) -> None:
        with self._process_lease_transaction() as transaction:
            if transaction is None:
                return
            self._clear_matching_process_lease_locked(transaction, expected)

    def _clear_matching_process_lease_locked(
        self,
        transaction: _OBSProcessLeaseTransaction,
        expected: OBSProcessLease,
    ) -> None:
        snapshot = self._open_process_lease_snapshot_locked(transaction)
        if snapshot is None:
            return
        if snapshot.lease != expected:
            raise self._lease_recovery_error(
                "OBS process lease changed before verified cleanup"
            )
        self._delete_process_lease_snapshot_locked(transaction, snapshot)

    def isolated_env(self) -> dict[str, str]:
        isolated_root = self.obs_dir / "temp_appdata"
        isolated_roaming = isolated_root / "Roaming"
        isolated_local = isolated_root / "Local"
        isolated_profile = isolated_root / "UserProfile"
        for path in (isolated_root, isolated_roaming, isolated_local, isolated_profile):
            path.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["APPDATA"] = str(isolated_roaming)
        env["LOCALAPPDATA"] = str(isolated_local)
        env["USERPROFILE"] = str(isolated_profile)
        return env

    def _list_obs_processes_windows(self) -> list[OBSProcessInfo]:
        command = [
            "wmic",
            "process",
            "where",
            "name='obs64.exe'",
            "get",
            "ProcessId,ExecutablePath,CreationDate",
            "/format:csv",
        ]
        try:
            completed = self._run_hidden(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_OBS_PROCESS_QUERY_COMMAND_TIMEOUT_SEC,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                return self._parse_wmic_csv(completed.stdout)
        except Exception as e:
            self.logger.debug("WMIC process query failed: %s", e)
        return self._list_obs_processes_powershell()

    def _list_obs_processes_powershell(self) -> list[OBSProcessInfo]:
        script = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process -Filter \"Name='obs64.exe'\" "
            "| Select-Object ProcessId,ExecutablePath,CreationDate | ConvertTo-Json -Compress"
        )
        try:
            completed = self._run_hidden(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_OBS_PROCESS_QUERY_COMMAND_TIMEOUT_SEC,
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                return []
            data = json.loads(completed.stdout)
        except Exception as e:
            self.logger.debug("PowerShell process query failed: %s", e)
            return []

        rows = data if isinstance(data, list) else [data]
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = row.get("ProcessId")
            try:
                pid_int = int(pid)
            except Exception:
                continue
            exe_text = str(row.get("ExecutablePath") or "").strip()
            result.append(
                OBSProcessInfo(
                    pid=pid_int,
                    executable_path=Path(exe_text) if exe_text else None,
                    creation_time=_parse_windows_process_creation_time(row.get("CreationDate")),
                )
            )
        return result

    def _list_obs_processes_powershell_strict(self) -> list[OBSProcessInfo]:
        script = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$ErrorActionPreference = 'Stop'; "
            "Get-CimInstance Win32_Process -Filter \"Name='obs64.exe'\" -ErrorAction Stop "
            "| Select-Object @{Name='ProcessId';Expression={[long]$_.ProcessId}},ExecutablePath "
            "| ConvertTo-Json -Compress"
        )
        try:
            completed = self._run_hidden(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_OBS_PROCESS_QUERY_COMMAND_TIMEOUT_SEC,
            )
        except Exception as exc:
            raise OBSProcessQueryError("PowerShell OBS process query could not be started") from exc
        if completed.returncode != 0:
            raise OBSProcessQueryError(
                f"PowerShell OBS process query failed with exit code {completed.returncode}"
            )
        stderr = str(getattr(completed, "stderr", "") or "").strip()
        if stderr:
            raise OBSProcessQueryError(
                "PowerShell OBS process query reported an error on stderr"
            )

        output = completed.stdout.strip()
        if not output:
            return []
        try:
            data = json.loads(output)
        except (TypeError, json.JSONDecodeError) as exc:
            raise OBSProcessQueryError("PowerShell OBS process query returned invalid JSON") from exc
        if data is None:
            raise OBSProcessQueryError(
                "PowerShell OBS process query returned an invalid null result"
            )
        if isinstance(data, dict):
            rows = [data]
        elif isinstance(data, list):
            rows = data
        else:
            raise OBSProcessQueryError("PowerShell OBS process query returned an invalid result")

        rows_to_bind: list[tuple[int, Path]] = []
        seen_pids: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise OBSProcessQueryError("PowerShell OBS process query returned a malformed row")
            raw_pid = row.get("ProcessId")
            if type(raw_pid) is not int or raw_pid <= 0:
                raise OBSProcessQueryError(
                    "PowerShell OBS process query returned a malformed process id"
                )
            pid = raw_pid
            if pid in seen_pids:
                raise OBSProcessQueryError(
                    "PowerShell OBS process query returned duplicate process ids"
                )
            seen_pids.add(pid)

            exe_text = str(row.get("ExecutablePath") or "").strip()
            executable_path = Path(exe_text) if exe_text else None
            if executable_path is None or not _is_absolute_obs_process_path(
                executable_path
            ):
                raise OBSProcessQueryError(
                    "PowerShell OBS process query returned a missing or non-absolute executable path"
                )
            rows_to_bind.append((pid, executable_path))
        result: list[OBSProcessInfo] = []
        for pid, executable_path in rows_to_bind:
            identity = self._bind_strict_process_row_to_handle(
                pid,
                executable_path,
            )
            if identity is not None:
                result.append(identity)
        return result

    def _bind_strict_process_row_to_handle(
        self,
        pid: int,
        executable_path: Path,
    ) -> OBSProcessInfo | None:
        """Replace a CIM PID/path row with one complete handle identity."""

        handle: Any | None = None
        operation_error: BaseException | None = None
        try:
            handle = self._open_process_identity_handle(pid)
            if self._wait_process_identity_handle(handle, timeout_ms=0):
                return None
            try:
                identity = self._query_process_identity_from_handle(handle, pid)
            except OSError:
                if self._wait_process_identity_handle(handle, timeout_ms=0):
                    return None
                raise
            validate_obs_process_query_snapshot(
                OBSProcessQuerySnapshot((identity,), time.time()),
                label="PowerShell row handle binding",
            )
            if identity.executable_path is None:
                raise OBSProcessQueryError(
                    "PowerShell OBS row handle is missing its executable path"
                )
            path_matches = _obs_process_paths_equal(
                executable_path,
                identity.executable_path,
            )
            if not path_matches:
                path_matches = _obs_executable_paths_same_physical_file(
                    executable_path,
                    identity.executable_path,
                )
            if not path_matches:
                raise OBSProcessQueryError(
                    "PowerShell OBS row path does not match its process handle"
                )
            if identity.creation_time_filetime is None:
                raise OBSProcessQueryError(
                    "PowerShell OBS row handle is missing creation FILETIME"
                )
            if self._wait_process_identity_handle(handle, timeout_ms=0):
                return None
            return identity
        except BaseException as exc:
            operation_error = exc
            raise
        finally:
            if handle is not None:
                try:
                    self._close_process_identity_handle(handle)
                except Exception as close_error:
                    if operation_error is None:
                        raise OBSProcessQueryError(
                            "PowerShell OBS row identity handle could not be closed"
                        ) from close_error
                    add_note = getattr(operation_error, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "CloseHandle also failed while unwinding strict OBS row binding: "
                            f"{type(close_error).__name__}: {close_error}"
                        )
                    self.logger.critical(
                        "CloseHandle also failed while unwinding strict OBS row binding: %s",
                        close_error,
                    )

    def _parse_wmic_csv(self, text: str) -> list[OBSProcessInfo]:
        result = []
        for row in csv.DictReader(line for line in text.splitlines() if line.strip()):
            pid_text = (row.get("ProcessId") or "").strip()
            if not pid_text.isdigit():
                continue
            exe_text = (row.get("ExecutablePath") or "").strip()
            result.append(
                OBSProcessInfo(
                    pid=int(pid_text),
                    executable_path=Path(exe_text) if exe_text else None,
                    creation_time=_parse_windows_process_creation_time(row.get("CreationDate")),
                )
            )
        return result

    def _find_process_by_pid(self, pid: int) -> OBSProcessInfo | None:
        for process in self.list_obs_processes():
            if process.pid == int(pid):
                return process
        return None

    def _uses_windows_process_identity_handles(self) -> bool:
        return os.name == "nt"

    def query_popen_process_identity(
        self,
        process: subprocess.Popen[Any],
    ) -> OBSProcessInfo:
        """Read identity from a Popen-owned handle without closing that handle."""

        if os.name != "nt":
            raise OBSProcessQueryError(
                "Popen process-handle identity is available only on Windows"
            )
        if process.poll() is not None:
            raise OBSProcessQueryError(
                "Popen process exited before handle identity validation"
            )
        handle = getattr(process, "_handle", None)
        if handle is None:
            raise OBSProcessQueryError("Popen does not expose its Windows process handle")
        identity = self._query_process_identity_from_handle(handle, int(process.pid))
        validate_obs_process_query_snapshot(
            OBSProcessQuerySnapshot((identity,), time.time()),
            label="Popen handle identity",
        )
        if identity.creation_time_filetime is None:
            raise OBSProcessQueryError(
                "Popen handle identity is missing creation FILETIME"
            )
        if process.poll() is not None:
            raise OBSProcessQueryError(
                "Popen process exited during handle identity validation"
            )
        return identity

    def _open_process_identity_handle(self, pid: int) -> Any:
        import ctypes
        from ctypes import wintypes

        process_terminate = 0x0001
        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(
            process_terminate | process_query_limited_information | synchronize,
            False,
            int(pid),
        )
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def _query_process_identity_from_handle(
        self,
        handle: Any,
        expected_pid: int,
    ) -> OBSProcessInfo:
        import ctypes
        from ctypes import wintypes

        creation_time, creation_ticks = self._query_process_creation_from_handle(
            handle,
            expected_pid,
        )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        capacity = wintypes.DWORD(32768)
        path_buffer = ctypes.create_unicode_buffer(capacity.value)
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            path_buffer,
            ctypes.byref(capacity),
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        return OBSProcessInfo(
            pid=int(expected_pid),
            executable_path=Path(path_buffer.value),
            creation_time=creation_time,
            creation_time_filetime=creation_ticks,
        )

    def _query_process_creation_from_handle(
        self,
        handle: Any,
        expected_pid: int,
    ) -> tuple[float, int]:
        """Read PID and creation FILETIME, including after process exit."""

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
        kernel32.GetProcessId.restype = wintypes.DWORD
        pid = int(kernel32.GetProcessId(handle))
        if pid <= 0:
            raise ctypes.WinError(ctypes.get_last_error())
        if pid != int(expected_pid):
            raise OBSProcessQueryError(
                "Windows process handle PID does not match the expected PID"
            )

        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        creation_ticks = (
            int(creation.dwHighDateTime) << 32
        ) | int(creation.dwLowDateTime)
        creation_time = (
            creation_ticks / 10_000_000
            - _WINDOWS_FILETIME_UNIX_EPOCH_SECONDS
        )
        return creation_time, creation_ticks

    def _wait_process_identity_handle(self, handle: Any, *, timeout_ms: int) -> bool:
        import ctypes
        from ctypes import wintypes

        wait_object_0 = 0x00000000
        wait_timeout = 0x00000102
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        result = int(kernel32.WaitForSingleObject(handle, max(0, int(timeout_ms))))
        if result == wait_object_0:
            return True
        if result == wait_timeout:
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def _terminate_process_identity_handle(self, handle: Any) -> bool:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        return bool(kernel32.TerminateProcess(handle, 1))

    def _close_process_identity_handle(self, handle: Any) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        if not kernel32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def _terminate_pid(self, pid: int, force: bool) -> bool:
        command = ["taskkill", "/pid", str(int(pid))]
        if force:
            command.append("/f")
        try:
            completed = self._run_hidden(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_OBS_PROCESS_TERMINATE_COMMAND_TIMEOUT_SEC,
            )
            if completed.returncode == 0:
                return True
            self.logger.warning(
                "Failed to terminate OBS pid=%s: taskkill exit code %s",
                pid,
                completed.returncode,
            )
            return False
        except Exception as e:
            self.logger.warning("Failed to terminate OBS pid=%s: %s", pid, e, exc_info=True)
            return False

    def _run_hidden(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        run_kwargs = self._hidden_subprocess_kwargs()
        run_kwargs.update(kwargs)
        return subprocess.run(command, **run_kwargs)

    def _hidden_subprocess_kwargs(self) -> dict[str, Any]:
        if os.name != "nt":
            return {}
        kwargs: dict[str, Any] = {}
        startupinfo = self._startupinfo_hidden()
        if startupinfo is not None:
            kwargs["startupinfo"] = startupinfo
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creationflags:
            kwargs["creationflags"] = creationflags
        return kwargs

    def _startupinfo_hidden(self) -> subprocess.STARTUPINFO | None:
        if os.name != "nt" or not hasattr(subprocess, "STARTUPINFO"):
            return None
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = 0  # SW_HIDE
        return startupinfo


def _parse_windows_process_creation_time(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    json_date = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d+)?\)/", text)
    if json_date is not None:
        return int(json_date.group(1)) / 1000.0
    try:
        return float(text)
    except Exception:
        pass
    try:
        match = re.fullmatch(
            r"(\d{14})\.(\d{6})([+-])(\d{3})",
            text,
        )
        if match is None:
            return None
        parsed = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(
            microsecond=int(match.group(2)),
        )
        offset_minutes = int(match.group(4))
        if match.group(3) == "-":
            offset_minutes = -offset_minutes
        return parsed.replace(
            tzinfo=timezone(timedelta(minutes=offset_minutes))
        ).timestamp()
    except Exception:
        return None
