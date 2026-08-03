from __future__ import annotations

import csv
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

LOGGER = logging.getLogger("lol_replay.obs_process")

OBS_PROCESS_LEASE_SCHEMA_VERSION = 2
_WINDOWS_FILETIME_UNIX_EPOCH_SECONDS = 11_644_473_600
_OBS_PROCESS_LEASE_LOCK = threading.RLock()
_POPEN_OBS_PROCESS_LEASE_ATTRIBUTE = "_lol_replay_obs_process_lease"
_OWNED_EXIT_STRICT_REQUERY_DELAY_SEC = 0.05


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


def _obs_process_identities_equal(
    expected: OBSProcessInfo,
    actual: OBSProcessInfo,
) -> bool:
    """Prefer exact raw FILETIME, with a legacy millisecond fallback."""

    if expected.pid != actual.pid:
        return False
    if expected.executable_path is None or actual.executable_path is None:
        return False
    if not _obs_process_paths_equal(
        expected.executable_path,
        actual.executable_path,
    ):
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


class OBSProcessManager:
    """アプリ管理OBSだけを対象に起動・終了する安全境界。"""

    def __init__(self, obs_dir: str | Path, logger: logging.Logger | None = None) -> None:
        self.obs_dir = Path(obs_dir).resolve()
        self.obs_exe = (self.obs_dir / "bin" / "64bit" / "obs64.exe").resolve()
        self.working_dir = self.obs_exe.parent
        self.logger = logger or LOGGER
        self.lease_path = self.obs_dir / ".lol_replay_obs_lease.json"

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
        try:
            return process.executable_path.resolve() == self.obs_exe
        except Exception:
            return str(process.executable_path).casefold() == str(self.obs_exe).casefold()

    def managed_processes(self) -> list[OBSProcessInfo]:
        return [process for process in self.list_obs_processes() if self.is_managed_process(process)]

    def has_managed_process(self) -> bool:
        return bool(self.managed_processes())

    def unmanaged_processes(self) -> list[OBSProcessInfo]:
        return [process for process in self.list_obs_processes() if not self.is_managed_process(process)]

    def has_unmanaged_process(self) -> bool:
        return bool(self.unmanaged_processes())

    def find_owned_process(self) -> OBSProcessInfo | None:
        with _OBS_PROCESS_LEASE_LOCK:
            return self._find_owned_process_locked()

    def _find_owned_process_locked(self) -> OBSProcessInfo | None:
        lease = self._read_process_lease_locked()
        if lease is None:
            return None
        _snapshot, process = self._query_owned_process_for_lease(
            lease,
            label="owned process lookup",
        )
        if process is None:
            self._clear_matching_process_lease_locked(lease)
            return None
        if lease.schema_version != OBS_PROCESS_LEASE_SCHEMA_VERSION:
            raise OBSProcessLeaseError(
                "Legacy OBS process lease cannot authorize a live process"
            )
        if not self.is_owned_process(process, lease):
            raise OBSProcessLeaseError(
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
            raise OBSProcessLeaseError(
                "Legacy OBS process lease refers to a live PID"
            )
        if not self.is_owned_process(process, lease):
            raise OBSProcessLeaseError(
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

    def kill_stale_owned_processes(self, timeout_sec: float = 3.0) -> list[int]:
        """Stop only the exact v2 identity recorded by this application's lease."""

        with _OBS_PROCESS_LEASE_LOCK:
            return self._kill_stale_owned_processes_locked(timeout_sec=timeout_sec)

    def _kill_stale_owned_processes_locked(
        self,
        *,
        timeout_sec: float,
    ) -> list[int]:
        lease = self._read_process_lease_locked()
        if lease is None:
            return []
        _snapshot, process = self._query_owned_process_for_lease(
            lease,
            label="stale owned process lookup",
        )
        if process is None:
            self._clear_matching_process_lease_locked(lease)
            return []

        result = self.terminate_owned_process_strict(
            process,
            timeout_sec=timeout_sec,
        )
        self._clear_matching_process_lease_locked(lease)
        return [item.pid for item in result.signaled_processes]

    def terminate_owned_process_strict(
        self,
        expected: OBSProcessInfo,
        *,
        timeout_sec: float = 3.0,
        poll_interval: float = 0.1,
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
            )
        return self._terminate_owned_process_without_handle(
            expected,
            timeout_sec=timeout_sec,
            poll_interval=poll_interval,
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
        if current is not None and not _obs_process_identities_equal(expected, current):
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
    ) -> OBSStrictTerminationResult:
        """Test fallback; Windows production uses the handle-bound implementation."""

        before, current = self._query_owned_target_strict(
            expected,
            label="owned pre-signal",
        )
        if current is None:
            return OBSStrictTerminationResult((), before)

        signaled = False
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
            if not _obs_process_identities_equal(expected, handle_identity):
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
                if not _obs_process_identities_equal(expected, handle_identity):
                    raise OBSProcessQueryError(
                        "Owned OBS handle identity changed before forced termination"
                    )
                if self._wait_process_identity_handle(handle, timeout_ms=0):
                    continue
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

        cmd = [
            str(self.obs_exe),
            "--portable",
            "--multi",
            "--disable-shutdown-check",
            "--disable-updater",
            *(extra_args or []),
        ]
        popen_kwargs: dict[str, Any] = {"cwd": str(self.working_dir), "env": env or os.environ.copy()}
        if hidden:
            popen_kwargs.update(self._hidden_subprocess_kwargs())
        process = subprocess.Popen(cmd, **popen_kwargs)
        try:
            self.write_process_lease(process)
        except BaseException as lease_error:
            cleanup_error: BaseException | None = None
            try:
                self.terminate_process(process)
            except BaseException as exc:
                cleanup_error = exc
            try:
                stopped = process.poll() is not None
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
                stopped = False
            if not stopped:
                try:
                    process.kill()
                    process.wait(timeout=2)
                    stopped = process.poll() is not None
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
            if not stopped:
                cleanup_failure = OBSProcessLeaseCleanupError(
                    "OBSの所有情報を確立できず、起動したOBS "
                    f"(PID {process.pid}) も自動終了できませんでした。"
                    "タスク マネージャーでこのOBSを手動終了してから再実行してください。"
                )
                add_note = getattr(cleanup_failure, "add_note", None)
                if callable(add_note):
                    add_note(
                        "Lease establishment failed first: "
                        f"{type(lease_error).__name__}: {lease_error}"
                    )
                    if cleanup_error is not None:
                        add_note(
                            "Automatic process cleanup also failed: "
                            f"{type(cleanup_error).__name__}: {cleanup_error}"
                        )
                self.logger.critical(
                    "New OBS process remained live after lease failure: pid=%s error=%s",
                    process.pid,
                    cleanup_error,
                )
                raise cleanup_failure from lease_error
            raise
        return process

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
        return self.is_managed_process(process) and _obs_process_identities_equal(
            expected,
            process,
        )

    def terminate_process(self, process: subprocess.Popen[Any] | None, timeout_sec: float = 3.0) -> None:
        if process is None:
            return
        lease = getattr(process, _POPEN_OBS_PROCESS_LEASE_ATTRIBUTE, None)
        if type(lease) is not OBSProcessLease or lease.pid != int(process.pid):
            lease = None
        if process.poll() is not None:
            if lease is not None:
                self._clear_matching_process_lease(lease)
            return
        try:
            process.terminate()
            process.wait(timeout=timeout_sec)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception as e:
                self.logger.warning(
                    "Failed to kill managed OBS process: %s",
                    e,
                    exc_info=True,
                )
                return
        if lease is not None:
            self._clear_matching_process_lease(lease)

    def write_process_lease(self, process: subprocess.Popen[Any]) -> None:
        with _OBS_PROCESS_LEASE_LOCK:
            self._write_process_lease_locked(process)

    def _write_process_lease_locked(self, process: subprocess.Popen[Any]) -> None:
        temporary = self.lease_path.with_name(
            f"{self.lease_path.name}.{int(process.pid)}.{time.time_ns()}.tmp"
        )
        try:
            self.obs_dir.mkdir(parents=True, exist_ok=True)
            process_info = self.query_popen_process_identity(process)
            validate_obs_process_query_snapshot(
                OBSProcessQuerySnapshot((process_info,), time.time()),
                label="new OBS lease identity",
            )
            if (
                process_info.pid != int(process.pid)
                or process_info.executable_path is None
                or not _obs_process_paths_equal(
                    process_info.executable_path,
                    self.obs_exe,
                )
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
            with open(temporary, "x", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.lease_path)
            if self._read_process_lease_locked() != lease:
                raise OBSProcessLeaseError(
                    "Committed OBS process lease does not match its verified identity"
                )
            setattr(process, _POPEN_OBS_PROCESS_LEASE_ATTRIBUTE, lease)
        except Exception as e:
            try:
                temporary.unlink(missing_ok=True)
            except Exception:
                pass
            if isinstance(e, OBSProcessLeaseError):
                raise
            raise OBSProcessLeaseError(
                "Failed to establish the v2 OBS process ownership lease"
            ) from e

    def read_process_lease(self) -> OBSProcessLease | None:
        with _OBS_PROCESS_LEASE_LOCK:
            return self._read_process_lease_locked()

    def _read_process_lease_locked(self) -> OBSProcessLease | None:
        try:
            with open(self.lease_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except Exception as exc:
            raise OBSProcessLeaseError("Cannot read the OBS process lease") from exc

        try:
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

    def _clear_matching_process_lease(self, expected: OBSProcessLease) -> None:
        with _OBS_PROCESS_LEASE_LOCK:
            self._clear_matching_process_lease_locked(expected)

    def _clear_matching_process_lease_locked(
        self,
        expected: OBSProcessLease,
    ) -> None:
        current = self._read_process_lease_locked()
        if current is None:
            return
        if current != expected:
            raise OBSProcessLeaseError(
                "OBS process lease changed before verified cleanup"
            )
        try:
            self.lease_path.unlink()
        except FileNotFoundError:
            return
        except Exception as exc:
            raise OBSProcessLeaseError(
                "Verified OBS process lease could not be cleared"
            ) from exc

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
            if identity.executable_path is None or not _obs_process_paths_equal(
                executable_path,
                identity.executable_path,
            ):
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
        creation_time = creation_ticks / 10_000_000 - 11_644_473_600
        return OBSProcessInfo(
            pid=pid,
            executable_path=Path(path_buffer.value),
            creation_time=creation_time,
            creation_time_filetime=creation_ticks,
        )

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
