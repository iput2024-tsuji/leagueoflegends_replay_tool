"""Low-level filesystem primitives for the OBS migration transaction."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
    from ctypes import wintypes

WINDOWS_LOCK_CONTENTION_ERRORS = frozenset({32, 33})

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
            if _is_reparse_point(opened):
                os.close(descriptor)
                raise _UnsafeOBSMigrationPathError(
                    f"symbolic link／reparse pointは利用できません: {self.path / name}"
                )
            if not stat.S_ISREG(opened.st_mode):
                os.close(descriptor)
                raise _UnsafeOBSMigrationPathError(
                    f"通常ファイルではありません: {self.path / name}"
                )
            if int(opened.st_nlink) != 1:
                os.close(descriptor)
                raise _UnsafeOBSMigrationPathError(
                    f"hardlinkされたファイルは利用できません: {self.path / name}"
                )
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

def _parse_transaction_temporary(
    path: Path,
    *,
    journal_target_name: str,
) -> _OBSTransactionTemporaryDescriptor | None:
    name = path.name
    journal_prefix = f"{journal_target_name}."
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
            target_name=journal_target_name,
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
