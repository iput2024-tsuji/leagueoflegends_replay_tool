from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Any

try:
    from .app_paths import APP_NAME, get_user_data_root
except ImportError:
    from app_paths import APP_NAME, get_user_data_root

ERROR_ALREADY_EXISTS = 183


def _create_windows_mutex(name: str) -> Any | None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool

    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def _close_windows_handle(handle: Any) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.CloseHandle(handle)


class SingleInstanceGuard:
    """Keeps one application process active per signed-in user."""

    def __init__(self, *, name: str | None = None, lock_path: Path | None = None) -> None:
        self.name = name or f"Local\\{APP_NAME}.SingleInstance"
        self.lock_path = lock_path or (get_user_data_root() / ".app-instance.lock")
        self._windows_handle = None
        self._lock_file = None

    def acquire(self) -> bool:
        if self._windows_handle is not None or self._lock_file is not None:
            return True
        if os.name == "nt":
            self._windows_handle = _create_windows_mutex(self.name)
            return self._windows_handle is not None
        return self._acquire_posix_lock()

    def _acquire_posix_lock(self) -> bool:
        import fcntl

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_file.close()
            return False
        self._lock_file = lock_file
        return True

    def release(self) -> None:
        if self._windows_handle is not None:
            _close_windows_handle(self._windows_handle)
            self._windows_handle = None
        if self._lock_file is not None:
            import fcntl

            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    def __enter__(self) -> SingleInstanceGuard:
        if not self.acquire():
            raise RuntimeError("another application instance is already running")
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.release()
