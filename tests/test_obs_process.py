import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import obs_process as obs_process_module
from src.obs_process import (
    OBS_PROCESS_LEASE_SCHEMA_VERSION,
    OBSProcessInfo,
    OBSProcessLeaseCleanupError,
    OBSProcessLeaseError,
    OBSProcessManager,
    OBSProcessQueryError,
    OBSProcessQuerySnapshot,
    OBSProcessTerminationError,
)


def _raw_filetime(unix_seconds: float) -> int:
    return int((unix_seconds + 11_644_473_600) * 10_000_000)


def _identity(
    manager: OBSProcessManager,
    *,
    pid: int = 100,
    unix_seconds: float = 10.0,
    filetime: int | None = None,
    executable_path: Path | None = None,
) -> OBSProcessInfo:
    return OBSProcessInfo(
        pid=pid,
        executable_path=executable_path or manager.obs_exe,
        creation_time=unix_seconds,
        creation_time_filetime=filetime or _raw_filetime(unix_seconds),
    )


def _snapshot(*processes: OBSProcessInfo) -> OBSProcessQuerySnapshot:
    return OBSProcessQuerySnapshot(tuple(processes), 100.0)


def _assert_actionable_lease_recovery(
    error: BaseException,
    manager: OBSProcessManager,
) -> None:
    message = str(error)
    assert str(manager.lease_path) in message
    assert str(manager.lease_lock_path) in message
    assert "すべてのOBS Studio" in message
    assert "再試行" in message


def _forbid_popen_pid_fallbacks(monkeypatch, manager: OBSProcessManager) -> None:
    def fail_pid_enumeration(*_args, **_kwargs):
        pytest.fail("Popen termination must not enumerate process IDs")

    def fail_strict_query(*_args, **_kwargs):
        pytest.fail("Popen termination must not run a strict process query")

    def fail_taskkill(*_args, **_kwargs):
        pytest.fail("Popen termination must not invoke taskkill")

    monkeypatch.setattr(manager, "list_obs_processes", fail_pid_enumeration)
    monkeypatch.setattr(manager, "_list_obs_processes_windows", fail_pid_enumeration)
    monkeypatch.setattr(manager, "query_obs_processes_strict", fail_strict_query)
    monkeypatch.setattr(
        manager,
        "_list_obs_processes_powershell_strict",
        fail_strict_query,
    )
    monkeypatch.setattr(manager, "_terminate_pid", fail_taskkill)


class _InstrumentedLeaseThreadLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._watched_thread: threading.Thread | None = None
        self.attempted = threading.Event()
        self.acquired = threading.Event()

    def watch(self, thread: threading.Thread) -> None:
        self._watched_thread = thread

    def __enter__(self):
        watched = threading.current_thread() is self._watched_thread
        if watched:
            self.attempted.set()
        self._lock.acquire()
        if watched:
            self.acquired.set()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self._lock.release()


def _write_v2_lease(
    manager: OBSProcessManager,
    process: OBSProcessInfo,
) -> None:
    manager.obs_dir.mkdir(parents=True, exist_ok=True)
    manager.lease_path.write_text(
        json.dumps(
            {
                "version": OBS_PROCESS_LEASE_SCHEMA_VERSION,
                "pid": process.pid,
                "executable_path": str(process.executable_path),
                "created_at": 1.0,
                "process_creation_time": process.creation_time,
                "process_creation_time_filetime": process.creation_time_filetime,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("operation", ["read", "find", "kill"])
def test_absent_lease_operations_do_not_create_managed_root(tmp_path, operation):
    manager = OBSProcessManager(tmp_path / "missing-obs-portable")

    if operation == "read":
        assert manager.read_process_lease() is None
    elif operation == "find":
        assert manager.find_owned_process() is None
    else:
        assert manager.kill_stale_owned_processes(timeout_sec=0) == []

    assert not manager.obs_dir.exists()
    assert not manager.lease_lock_path.exists()


def test_absent_lease_in_existing_root_does_not_create_persistent_lock(tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_dir.mkdir(parents=True)

    assert manager.read_process_lease() is None

    assert manager.obs_dir.is_dir()
    assert not manager.lease_lock_path.exists()


def test_absent_lease_probe_reports_root_appearance_with_recovery_paths(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    calls = 0

    class AppearedProbe:
        def close(self):
            return None

    def open_absolute(_cls, path, **kwargs):
        nonlocal calls
        assert Path(path) == manager.obs_dir
        assert kwargs == {}
        calls += 1
        if calls == 1:
            raise FileNotFoundError(manager.obs_dir)
        return AppearedProbe()

    monkeypatch.setattr(
        obs_process_module._OBSDirectoryLease,
        "open_absolute",
        classmethod(open_absolute),
    )

    with pytest.raises(OBSProcessLeaseError) as captured:
        manager.read_process_lease()

    message = str(captured.value)
    assert "不存在確認中に出現" in message
    assert str(manager.lease_path) in message
    assert str(manager.lease_lock_path) in message
    assert "すべてのOBS Studio" in message
    assert "再試行" in message


def test_absent_lease_probe_close_failure_reports_recovery_paths(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_dir.mkdir(parents=True)

    class FailingCloseProbe:
        path = manager.obs_dir

        def validate_lexical_binding(self):
            return None

        def relative_file_identity_or_none(self, name):
            assert name in {manager.lease_path.name, manager.lease_lock_path.name}
            return None

        def close(self):
            raise OSError("simulated normal probe close failure")

    monkeypatch.setattr(
        obs_process_module._OBSDirectoryLease,
        "open_absolute",
        classmethod(lambda _cls, path, **kwargs: FailingCloseProbe()),
    )

    with pytest.raises(OBSProcessLeaseError) as captured:
        manager.read_process_lease()

    message = str(captured.value)
    assert "probeを安全にcloseできません" in message
    assert str(manager.lease_path) in message
    assert str(manager.lease_lock_path) in message
    assert "すべてのOBS Studio" in message
    assert "再試行" in message
    assert isinstance(captured.value.__cause__, OSError)


def test_process_manager_writes_v2_lease_from_popen_handle_identity(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    process = SimpleNamespace(pid=100, poll=lambda: None, _handle="owned-handle")
    identity = _identity(manager)
    monkeypatch.setattr(
        manager,
        "query_popen_process_identity",
        lambda candidate: identity if candidate is process else pytest.fail("wrong Popen"),
    )

    manager.write_process_lease(process)

    payload = json.loads(manager.lease_path.read_text(encoding="utf-8"))
    assert payload["version"] == OBS_PROCESS_LEASE_SCHEMA_VERSION
    assert payload["pid"] == identity.pid
    assert payload["executable_path"] == str(manager.obs_exe)
    assert payload["process_creation_time"] == identity.creation_time
    assert payload["process_creation_time_filetime"] == identity.creation_time_filetime
    lease = manager.read_process_lease()
    assert lease is not None
    assert lease.schema_version == OBS_PROCESS_LEASE_SCHEMA_VERSION
    assert lease.process_creation_time_filetime == identity.creation_time_filetime
    assert process._lol_replay_obs_process_lease == lease


def test_start_obs_stops_only_new_popen_when_v2_lease_cannot_be_established(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    events = []
    query_calls = []
    lock_released = False
    real_release = obs_process_module._OBSInterProcessLock.release

    def release_after_recording(lock):
        nonlocal lock_released
        real_release(lock)
        lock_released = True

    class FakePopen:
        pid = 100

        def __init__(self):
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            assert not lock_released
            events.append("terminate")
            self.alive = False

        def wait(self, timeout):
            assert not lock_released
            events.append(("wait", timeout))
            return 0

        def kill(self):
            events.append("kill")
            self.alive = False

    process = FakePopen()
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: query_calls.append("query") or _snapshot(),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda *args, **kwargs: pytest.fail("cleanup must not signal by PID"),
    )
    monkeypatch.setattr(
        manager,
        "terminate_process",
        lambda *args, **kwargs: pytest.fail("cleanup must not reenter public termination"),
    )
    monkeypatch.setattr(
        obs_process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        manager,
        "query_popen_process_identity",
        lambda candidate: (_ for _ in ()).throw(
            OBSProcessLeaseError("identity unavailable")
        ),
    )
    monkeypatch.setattr(
        obs_process_module._OBSInterProcessLock,
        "release",
        release_after_recording,
    )

    with pytest.raises(OBSProcessLeaseError, match="identity unavailable"):
        manager.start_obs(env={"TEST": "1"}, hidden=False)

    assert process.poll() == 0
    assert events == ["terminate", ("wait", 3.0)]
    assert query_calls == ["query"]
    assert lock_released


def test_start_obs_requires_manual_stop_when_lease_failure_process_survives(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    lock_released = False
    real_release = obs_process_module._OBSInterProcessLock.release

    def release_after_recording(lock):
        nonlocal lock_released
        real_release(lock)
        lock_released = True

    class StubbornPopen:
        pid = 321

        def poll(self):
            return None

        def terminate(self):
            assert not lock_released
            raise OSError("terminate failed")

        def wait(self, timeout):
            assert not lock_released
            raise TimeoutError("wait failed")

        def kill(self):
            raise OSError("kill failed")

    process = StubbornPopen()
    lease_error = OBSProcessLeaseError("identity unavailable")
    monkeypatch.setattr(manager, "query_obs_processes_strict", lambda: _snapshot())
    monkeypatch.setattr(
        obs_process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(
        manager,
        "_write_process_lease_locked",
        lambda transaction, candidate: (_ for _ in ()).throw(lease_error),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda *args, **kwargs: pytest.fail("cleanup must not signal by PID"),
    )
    monkeypatch.setattr(
        manager,
        "terminate_process",
        lambda *args, **kwargs: pytest.fail("cleanup must not reenter public termination"),
    )
    monkeypatch.setattr(
        obs_process_module._OBSInterProcessLock,
        "release",
        release_after_recording,
    )

    with pytest.raises(
        OBSProcessLeaseCleanupError,
        match=r"PID 321.*手動終了",
    ) as captured:
        manager.start_obs(hidden=False)

    assert captured.value.__cause__ is lease_error
    assert process.poll() is None
    assert lock_released


@pytest.mark.parametrize("lease_kind", ["v2", "legacy", "malformed"])
def test_start_obs_existing_lease_fails_before_popen_without_changing_lease(
    monkeypatch,
    tmp_path,
    lease_kind,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    existing_identity = _identity(manager, pid=100, unix_seconds=10.0)
    if lease_kind == "v2":
        _write_v2_lease(manager, existing_identity)
    elif lease_kind == "legacy":
        manager.lease_path.write_text(
            json.dumps(
                {
                    "pid": existing_identity.pid,
                    "executable_path": str(existing_identity.executable_path),
                    "created_at": 1.0,
                    "process_creation_time": existing_identity.creation_time,
                }
            ),
            encoding="utf-8",
        )
    else:
        manager.lease_path.write_bytes(b"{malformed")
    existing_raw = manager.lease_path.read_bytes()
    existing_file_identity = obs_process_module._file_identity(
        manager.lease_path.stat()
    )
    monkeypatch.setattr(
        obs_process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("existing lease must prevent Popen"),
    )
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: pytest.fail("existing lease must prevent process query"),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda *args, **kwargs: pytest.fail("admission must not signal a PID"),
    )

    with pytest.raises(OBSProcessLeaseError):
        manager.start_obs(hidden=False)

    assert manager.lease_path.read_bytes() == existing_raw
    assert (
        obs_process_module._file_identity(manager.lease_path.stat())
        == existing_file_identity
    )


def test_start_obs_live_managed_process_without_lease_fails_before_popen_or_signal(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    managed = _identity(manager, pid=200, unix_seconds=20.0)
    unmanaged = _identity(
        manager,
        pid=300,
        unix_seconds=30.0,
        executable_path=(tmp_path / "regular-obs" / "obs64.exe").resolve(),
    )
    snapshot = _snapshot(unmanaged, managed)
    query_calls = []
    signal_calls = []
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: query_calls.append("query") or snapshot,
    )
    monkeypatch.setattr(
        obs_process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("managed process must prevent Popen"),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda *args, **kwargs: signal_calls.append((args, kwargs)),
    )

    with pytest.raises(OBSProcessLeaseError, match=r"PID 200") as captured:
        manager.start_obs(hidden=False)

    _assert_actionable_lease_recovery(captured.value, manager)
    assert "手動確認" in str(captured.value)
    assert query_calls == ["query"]
    assert signal_calls == []
    assert snapshot.processes == (unmanaged, managed)
    assert not manager.lease_path.exists()


def test_start_obs_strict_query_failure_prevents_popen(monkeypatch, tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    query_error = OBSProcessQueryError("query failed")
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: (_ for _ in ()).throw(query_error),
    )
    monkeypatch.setattr(
        obs_process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("query failure must prevent Popen"),
    )

    with pytest.raises(OBSProcessLeaseError, match="strict process") as captured:
        manager.start_obs(hidden=False)

    assert captured.value.__cause__ is query_error
    _assert_actionable_lease_recovery(captured.value, manager)


def test_start_obs_malformed_strict_snapshot_prevents_popen(monkeypatch, tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    malformed = OBSProcessInfo(
        pid=200,
        executable_path=None,
        creation_time=20.0,
        creation_time_filetime=_raw_filetime(20.0),
    )
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: _snapshot(malformed),
    )
    monkeypatch.setattr(
        obs_process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("malformed query must prevent Popen"),
    )

    with pytest.raises(OBSProcessLeaseError, match="strict process") as captured:
        manager.start_obs(hidden=False)

    assert isinstance(captured.value.__cause__, OBSProcessQueryError)
    _assert_actionable_lease_recovery(captured.value, manager)


@pytest.mark.parametrize("lock_failure", ["busy", "permission"])
def test_start_obs_lock_failure_prevents_popen(
    monkeypatch,
    tmp_path,
    lock_failure,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    acquire_calls = []

    class RejectingLock:
        def __init__(self, path):
            assert path == manager.lease_lock_path

        def acquire(self, *, create_parent):
            acquire_calls.append(create_parent)
            if lock_failure == "permission":
                raise PermissionError("lock denied")
            return False

        def release(self):
            pytest.fail("unacquired lock must not be released")

    monkeypatch.setattr(obs_process_module, "_OBSInterProcessLock", RejectingLock)
    monkeypatch.setattr(obs_process_module, "_OBS_PROCESS_LEASE_LOCK_TIMEOUT_SEC", 0.0)
    monkeypatch.setattr(
        obs_process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("lock failure must prevent Popen"),
    )

    with pytest.raises(OBSProcessLeaseError) as captured:
        manager.start_obs(hidden=False)

    _assert_actionable_lease_recovery(captured.value, manager)
    assert acquire_calls == [True]


def test_start_obs_rechecks_lease_absence_after_strict_query(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    replacement = _identity(manager, pid=400, unix_seconds=40.0)

    def query_and_create_noncooperative_lease():
        _write_v2_lease(manager, replacement)
        return _snapshot()

    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        query_and_create_noncooperative_lease,
    )
    monkeypatch.setattr(
        obs_process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("appeared lease must prevent Popen"),
    )

    with pytest.raises(OBSProcessLeaseError, match="出現"):
        manager.start_obs(hidden=False)

    lease = manager.read_process_lease()
    assert lease is not None
    assert lease.pid == replacement.pid


def test_start_obs_allows_only_unmanaged_rows_and_binds_lease_before_unlock(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    identity = _identity(manager, pid=500, unix_seconds=50.0)
    unmanaged = _identity(
        manager,
        pid=600,
        unix_seconds=60.0,
        executable_path=(tmp_path / "regular-obs" / "obs64.exe").resolve(),
    )
    events = []

    class FakePopen:
        pid = identity.pid

        def poll(self):
            return None

    process = FakePopen()
    real_release = obs_process_module._OBSInterProcessLock.release

    def release_after_asserting_bind(lock):
        assert process._lol_replay_obs_process_lease.pid == identity.pid
        assert manager.lease_path.exists()
        events.append("release")
        real_release(lock)

    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: events.append("query") or _snapshot(unmanaged),
    )
    monkeypatch.setattr(
        manager,
        "query_popen_process_identity",
        lambda candidate: identity if candidate is process else pytest.fail("wrong Popen"),
    )
    monkeypatch.setattr(
        obs_process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: events.append("popen") or process,
    )
    monkeypatch.setattr(
        obs_process_module._OBSInterProcessLock,
        "release",
        release_after_asserting_bind,
    )
    monkeypatch.setattr(
        manager,
        "write_process_lease",
        lambda *args, **kwargs: pytest.fail("start must not reenter public lease writer"),
    )

    assert manager.start_obs(hidden=False) is process

    assert events == ["query", "popen", "release"]
    assert process._lol_replay_obs_process_lease.pid == identity.pid


def test_start_obs_popen_failure_releases_transaction_without_cleanup(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    popen_error = OSError("Popen failed")
    monkeypatch.setattr(manager, "query_obs_processes_strict", lambda: _snapshot())
    monkeypatch.setattr(
        obs_process_module.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(popen_error),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_unleased_popen_process",
        lambda process: pytest.fail("failed Popen has no handle to clean up"),
    )

    with pytest.raises(OSError, match="Popen failed") as captured:
        manager.start_obs(hidden=False)

    assert captured.value is popen_error
    assert manager.read_process_lease() is None


@pytest.mark.parametrize("failure_stage", ["pre-commit", "setattr"])
def test_start_obs_lease_establishment_fault_cleans_original_handle(
    monkeypatch,
    tmp_path,
    failure_stage,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    identity = _identity(manager, pid=700, unix_seconds=70.0)
    events = []
    query_calls = []
    lock_released = False
    real_release = obs_process_module._OBSInterProcessLock.release

    def release_after_recording(lock):
        nonlocal lock_released
        real_release(lock)
        lock_released = True

    class FakePopen:
        pid = identity.pid

        def __init__(self):
            object.__setattr__(self, "alive", True)

        def __setattr__(self, name, value):
            if (
                failure_stage == "setattr"
                and name == "_lol_replay_obs_process_lease"
            ):
                raise OSError("simulated lease bind failure")
            object.__setattr__(self, name, value)

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            assert not lock_released
            events.append("terminate")
            self.alive = False

        def wait(self, timeout):
            assert not lock_released
            events.append(("wait", timeout))
            return 0

        def kill(self):
            pytest.fail("graceful cleanup should be sufficient")

    process = FakePopen()
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: query_calls.append("query") or _snapshot(),
    )
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    monkeypatch.setattr(obs_process_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda *args, **kwargs: pytest.fail("cleanup must not signal by PID"),
    )
    monkeypatch.setattr(
        manager,
        "terminate_process",
        lambda *args, **kwargs: pytest.fail("cleanup must not reenter public termination"),
    )
    monkeypatch.setattr(
        obs_process_module._OBSInterProcessLock,
        "release",
        release_after_recording,
    )
    if failure_stage == "pre-commit":
        monkeypatch.setattr(
            obs_process_module._OBSDirectoryLease,
            "publish_open_file_no_replace",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("simulated pre-commit publish failure")
            ),
        )

    with pytest.raises(OBSProcessLeaseError):
        manager.start_obs(hidden=False)

    assert events == ["terminate", ("wait", 3.0)]
    assert query_calls == ["query"]
    assert lock_released
    temporary_paths = tuple(
        manager.obs_dir.glob(f"{obs_process_module.OBS_PROCESS_LEASE_TEMP_PREFIX}*")
    )
    assert temporary_paths == ()
    assert manager.lease_path.exists() is (failure_stage == "setattr")


def test_start_obs_post_commit_failure_blocks_next_starter_until_cleanup_finishes(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    next_manager = OBSProcessManager(manager.obs_dir)
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    identity = _identity(manager, pid=800, unix_seconds=80.0)
    cleanup_entered = threading.Event()
    allow_cleanup = threading.Event()
    start_results = []
    start_errors = []
    popen_calls = []
    query_calls = []

    class FakePopen:
        pid = identity.pid

        def __init__(self):
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            cleanup_entered.set()
            assert allow_cleanup.wait(timeout=15)
            self.alive = False

        def wait(self, timeout):
            return 0

        def kill(self):
            pytest.fail("graceful cleanup should be sufficient")

    process = FakePopen()
    real_publish = obs_process_module._OBSDirectoryLease.publish_open_file_no_replace

    def publish_then_fail(directory, *args, **kwargs):
        real_publish(directory, *args, **kwargs)
        raise OSError("simulated post-commit publish report")

    def popen(*args, **kwargs):
        popen_calls.append((args, kwargs))
        return process

    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: query_calls.append("first") or _snapshot(),
    )
    monkeypatch.setattr(
        next_manager,
        "query_obs_processes_strict",
        lambda: query_calls.append("second") or _snapshot(),
    )
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    monkeypatch.setattr(obs_process_module.subprocess, "Popen", popen)
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda *args, **kwargs: pytest.fail("cleanup must not signal by PID"),
    )
    monkeypatch.setattr(
        next_manager,
        "_terminate_pid",
        lambda *args, **kwargs: pytest.fail("admission must not signal by PID"),
    )
    monkeypatch.setattr(
        obs_process_module._OBSDirectoryLease,
        "publish_open_file_no_replace",
        publish_then_fail,
    )
    thread_lock = _InstrumentedLeaseThreadLock()
    monkeypatch.setattr(obs_process_module, "_OBS_PROCESS_LEASE_LOCK", thread_lock)

    def start(candidate):
        try:
            start_results.append(candidate.start_obs(hidden=False))
        except BaseException as exc:
            start_errors.append(exc)

    first_thread = threading.Thread(target=start, args=(manager,))
    second_thread = threading.Thread(target=start, args=(next_manager,))
    thread_lock.watch(second_thread)
    first_thread.start()
    assert cleanup_entered.wait(timeout=15)
    second_thread.start()
    assert thread_lock.attempted.wait(timeout=15)
    assert not thread_lock.acquired.is_set()
    assert len(popen_calls) == 1
    allow_cleanup.set()
    first_thread.join(timeout=15)
    second_thread.join(timeout=15)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert start_results == []
    assert len(start_errors) == 2
    assert all(isinstance(error, OBSProcessLeaseError) for error in start_errors)
    assert len(popen_calls) == 1
    assert query_calls == ["first"]
    assert process.poll() == 0
    assert manager.lease_path.exists()
    assert tuple(
        manager.obs_dir.glob(f"{obs_process_module.OBS_PROCESS_LEASE_TEMP_PREFIX}*")
    ) == ()


def test_terminate_popen_clears_only_its_exact_bound_lease(monkeypatch, tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)

    class FakePopen:
        pid = identity.pid

        def __init__(self):
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.alive = False

        def wait(self, timeout):
            return 0

    process = FakePopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: pytest.fail("Popen termination must not enumerate OBS processes"),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda *args, **kwargs: pytest.fail("Popen termination must not signal a PID"),
    )
    manager.write_process_lease(process)

    manager.terminate_process(process)

    assert process.poll() == 0
    assert manager.read_process_lease() is None


def test_terminate_popen_natural_exit_clears_lease_without_signal(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    signals = []

    class FakePopen:
        pid = identity.pid

        def poll(self):
            return 0

        def terminate(self):
            signals.append("terminate")

        def kill(self):
            signals.append("kill")

    process = FakePopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    manager.write_process_lease(process)

    manager.terminate_process(process)

    assert signals == []
    assert manager.read_process_lease() is None


@pytest.mark.skipif(os.name != "nt", reason="Windows Popen handle regression")
def test_terminate_real_exited_windows_popen_uses_filetime_without_path_requery(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_exe = Path(sys.executable).resolve()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.5)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        manager.write_process_lease(process)
        process.wait(timeout=5)
        assert process.poll() is not None
        assert manager._wait_process_identity_handle(
            process._handle,
            timeout_ms=0,
        ) is True
        monkeypatch.setattr(
            manager,
            "_query_process_identity_from_handle",
            lambda *args: pytest.fail(
                "exited Windows Popen must not re-query executable path"
            ),
        )

        manager.terminate_process(process)

        assert manager.read_process_lease() is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_terminate_popen_graceful_timeout_uses_owned_force_handle(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    calls = []

    class FakePopen:
        pid = identity.pid

        def __init__(self):
            self.alive = True
            self.wait_count = 0

        def poll(self):
            calls.append("poll")
            return None if self.alive else 1

        def terminate(self):
            calls.append("terminate")

        def wait(self, timeout):
            calls.append(("wait", timeout))
            self.wait_count += 1
            if self.wait_count == 1:
                raise subprocess.TimeoutExpired("obs64.exe", timeout)
            return 1

        def kill(self):
            calls.append("kill")
            self.alive = False

    process = FakePopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: pytest.fail("replacement listings are outside the Popen contract"),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda *args, **kwargs: pytest.fail("replacement PID must not be signaled"),
    )
    manager.write_process_lease(process)

    manager.terminate_process(process, timeout_sec=0.25)

    assert calls.count("terminate") == 1
    assert calls.count("kill") == 1
    assert ("wait", 0.25) in calls
    assert ("wait", 2) in calls
    assert manager.read_process_lease() is None


@pytest.mark.parametrize(
    "failure_stage",
    ["terminate", "wait", "poll"],
)
def test_terminate_popen_api_failure_is_typed_even_when_force_proves_exit(
    monkeypatch,
    tmp_path,
    failure_stage,
):
    manager = OBSProcessManager(tmp_path / f"obs-portable-{failure_stage}")
    identity = _identity(manager)
    calls = []

    class FakePopen:
        pid = identity.pid

        def __init__(self):
            self.alive = True
            self.poll_count = 0

        def poll(self):
            calls.append("poll")
            self.poll_count += 1
            if failure_stage == "poll" and self.poll_count == 1:
                raise OSError("poll failed")
            return None if self.alive else 1

        def terminate(self):
            calls.append("terminate")
            if failure_stage == "terminate":
                raise OSError("terminate failed")

        def wait(self, timeout):
            calls.append(("wait", timeout))
            if timeout != 2:
                if failure_stage == "wait":
                    raise OSError("wait failed")
                raise subprocess.TimeoutExpired("obs64.exe", timeout)
            return 1

        def kill(self):
            calls.append("kill")
            self.alive = False

    process = FakePopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    manager.write_process_lease(process)

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process, timeout_sec=0)

    notes = getattr(captured.value, "__notes__", ())
    assert any(failure_stage in note.lower() for note in notes)
    assert calls.count("kill") == 1
    assert process.poll() == 1
    assert manager.read_process_lease() is None


@pytest.mark.parametrize(
    ("failure_kind", "expected_cause", "failure_text"),
    [
        ("error", OSError, "simulated force wait failure"),
        ("timeout", subprocess.TimeoutExpired, "timed out after 2 seconds"),
    ],
    ids=("non-timeout-error", "timeout"),
)
def test_terminate_popen_force_wait_failure_is_typed_and_keeps_live_lease(
    monkeypatch,
    tmp_path,
    failure_kind,
    expected_cause,
    failure_text,
):
    manager = OBSProcessManager(tmp_path / f"obs-portable-{failure_kind}")
    identity = _identity(manager)
    signals = []
    waits = []
    injected = (
        subprocess.TimeoutExpired("obs64.exe", 2)
        if failure_kind == "timeout"
        else OSError(failure_text)
    )

    class StubbornPopen:
        pid = identity.pid

        def poll(self):
            return None

        def terminate(self):
            signals.append("terminate")

        def wait(self, timeout):
            waits.append(timeout)
            if len(waits) == 1:
                raise subprocess.TimeoutExpired("obs64.exe", timeout)
            raise injected

        def kill(self):
            signals.append("kill")

    process = StubbornPopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    manager.write_process_lease(process)
    lease_before = manager.lease_path.read_bytes()
    _forbid_popen_pid_fallbacks(monkeypatch, manager)

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process, timeout_sec=0.25)

    assert type(captured.value.__cause__) is expected_cause
    assert captured.value.__cause__ is injected
    assert failure_text in str(captured.value.__cause__)
    assert any(
        "Popen.wait after kill" in note and failure_text in note
        for note in captured.value.__notes__
    )
    assert any(
        "final owned-handle verification" in note
        for note in captured.value.__notes__
    )
    assert signals == ["terminate", "kill"]
    assert waits == [0.25, 2]
    assert manager.lease_path.read_bytes() == lease_before
    _assert_actionable_lease_recovery(captured.value, manager)


def test_terminate_popen_final_poll_failure_is_typed_after_proven_exit(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    signals = []
    waits = []
    poll_failure = OSError("simulated final poll failure")

    class FinalPollFaultPopen:
        pid = identity.pid

        def __init__(self):
            self.force_completed = False
            self.final_poll_fault_injected = False

        def poll(self):
            if self.force_completed and not self.final_poll_fault_injected:
                self.final_poll_fault_injected = True
                raise poll_failure
            return None

        def terminate(self):
            signals.append("terminate")

        def wait(self, timeout):
            waits.append(timeout)
            if timeout != 2:
                raise subprocess.TimeoutExpired("obs64.exe", timeout)
            self.force_completed = True
            return 0

        def kill(self):
            signals.append("kill")

    process = FinalPollFaultPopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    manager.write_process_lease(process)
    _forbid_popen_pid_fallbacks(monkeypatch, manager)

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process, timeout_sec=0)

    assert captured.value.__cause__ is poll_failure
    assert str(captured.value.__cause__) == "simulated final poll failure"
    assert any(
        "final poll: OSError: simulated final poll failure" in note
        for note in captured.value.__notes__
    )
    assert signals == ["terminate", "kill"]
    assert waits == [0, 2]
    assert process.force_completed is True
    assert process.final_poll_fault_injected is True
    assert manager.read_process_lease() is None
    _assert_actionable_lease_recovery(captured.value, manager)


def test_terminate_popen_final_owned_handle_wait_failure_is_typed_after_exit(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    signals = []
    waits = []
    handle_waits = []
    handle_wait_failure = OSError("simulated final owned handle wait failure")

    class FinalHandleWaitFaultPopen:
        pid = identity.pid
        _handle = "owned-handle"

        def __init__(self):
            self.alive = True
            self.force_completed = False
            self.final_handle_wait_fault_injected = False

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            signals.append("terminate")

        def wait(self, timeout):
            waits.append(timeout)
            if timeout != 2:
                raise subprocess.TimeoutExpired("obs64.exe", timeout)
            self.force_completed = True
            return 0

        def kill(self):
            signals.append("kill")
            self.alive = False

    process = FinalHandleWaitFaultPopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    manager.write_process_lease(process)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: identity,
    )

    def fail_final_handle_wait(handle, timeout_ms):
        handle_waits.append((handle, timeout_ms))
        if (
            process.force_completed
            and not process.final_handle_wait_fault_injected
        ):
            process.final_handle_wait_fault_injected = True
            raise handle_wait_failure
        return False

    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        fail_final_handle_wait,
    )
    _forbid_popen_pid_fallbacks(monkeypatch, manager)

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process, timeout_sec=0)

    assert captured.value.__cause__ is handle_wait_failure
    assert str(captured.value.__cause__) == (
        "simulated final owned handle wait failure"
    )
    assert any(
        "final poll owned handle wait: OSError: "
        "simulated final owned handle wait failure" in note
        for note in captured.value.__notes__
    )
    assert signals == ["terminate", "kill"]
    assert waits == [0, 2]
    assert handle_waits
    assert all(call == ("owned-handle", 0) for call in handle_waits)
    assert process.force_completed is True
    assert process.final_handle_wait_fault_injected is True
    assert manager.read_process_lease() is None
    _assert_actionable_lease_recovery(captured.value, manager)


def test_terminate_popen_kill_failure_and_final_live_keep_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)

    class StubbornPopen:
        pid = identity.pid

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("obs64.exe", timeout)

        def kill(self):
            raise OSError("kill failed")

    process = StubbornPopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    manager.write_process_lease(process)
    lease_before = manager.lease_path.read_bytes()

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process, timeout_sec=0)

    assert any("Popen.kill" in note for note in captured.value.__notes__)
    assert "手動で終了" in str(captured.value)
    assert "再試行" in str(captured.value)
    assert process.poll() is None
    assert manager.lease_path.read_bytes() == lease_before


def test_terminate_popen_missing_bound_lease_is_not_success(tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")

    class ExitedPopen:
        pid = 100

        def poll(self):
            return 0

    with pytest.raises(OBSProcessTerminationError, match="終了処理") as captured:
        manager.terminate_process(ExitedPopen())

    assert isinstance(captured.value.__cause__, OBSProcessLeaseError)


def test_terminate_popen_malformed_bound_lease_keeps_disk_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    process = SimpleNamespace(pid=identity.pid, poll=lambda: 0)
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    manager.write_process_lease(process)
    lease_before = manager.lease_path.read_bytes()
    process._lol_replay_obs_process_lease = {"pid": identity.pid}

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process)

    assert isinstance(captured.value.__cause__, OBSProcessLeaseError)
    assert manager.lease_path.read_bytes() == lease_before


def test_terminate_popen_handle_identity_change_keeps_bound_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    replacement = _identity(
        manager,
        filetime=identity.creation_time_filetime + 10,
    )
    signals = []

    class ExitedPopen:
        pid = identity.pid
        _handle = "popen-owned"

        def poll(self):
            return 0

        def terminate(self):
            signals.append("terminate")

        def kill(self):
            signals.append("kill")

    process = ExitedPopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    manager.write_process_lease(process)
    lease_before = manager.lease_path.read_bytes()
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "_query_process_creation_from_handle",
        lambda handle, pid: (
            replacement.creation_time,
            replacement.creation_time_filetime,
        ),
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda *args: pytest.fail("exited Popen must not re-query executable path"),
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: True,
    )

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process)

    assert isinstance(captured.value.__cause__, OBSProcessLeaseError)
    assert signals == []
    assert manager.lease_path.read_bytes() == lease_before


def test_terminate_popen_live_handle_identity_mismatch_signals_nothing(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    replacement = _identity(
        manager,
        filetime=identity.creation_time_filetime + 10,
    )
    signals = []

    class LivePopen:
        pid = identity.pid
        _handle = "replacement-handle"

        def poll(self):
            return None

        def terminate(self):
            signals.append("terminate")

        def kill(self):
            signals.append("kill")

        def wait(self, timeout):
            signals.append(("wait", timeout))

    process = LivePopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    manager.write_process_lease(process)
    lease_before = manager.lease_path.read_bytes()
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: False,
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: replacement,
    )
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: pytest.fail("leased Popen cleanup must not enumerate PIDs"),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda *args, **kwargs: pytest.fail("replacement PID must not be signaled"),
    )

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process)

    assert isinstance(captured.value.__cause__, OBSProcessLeaseError)
    assert signals == []
    assert manager.lease_path.read_bytes() == lease_before


def test_terminate_popen_exit_during_path_query_uses_bound_path_and_filetime(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    state = {"exited": False, "wait_calls": 0}
    signals = []

    class RacingPopen:
        pid = identity.pid
        _handle = "owned-handle"

        def poll(self):
            return 0 if state["exited"] else None

        def terminate(self):
            signals.append("terminate")

        def kill(self):
            signals.append("kill")

    process = RacingPopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    manager.write_process_lease(process)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)

    def wait_handle(handle, timeout_ms):
        state["wait_calls"] += 1
        if state["wait_calls"] >= 2:
            state["exited"] = True
        return state["exited"]

    monkeypatch.setattr(manager, "_wait_process_identity_handle", wait_handle)
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: (_ for _ in ()).throw(
            OSError(31, "QueryFullProcessImageNameW failed after exit")
        ),
    )
    monkeypatch.setattr(
        manager,
        "_query_process_creation_from_handle",
        lambda handle, pid: (
            identity.creation_time,
            identity.creation_time_filetime,
        ),
    )

    manager.terminate_process(process)

    assert state["wait_calls"] >= 3
    assert signals == []
    assert manager.read_process_lease() is None


def test_terminate_popen_live_path_query_error_fails_closed_without_signal(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    signals = []

    class LivePopen:
        pid = identity.pid
        _handle = "owned-handle"

        def poll(self):
            return None

        def terminate(self):
            signals.append("terminate")

        def kill(self):
            signals.append("kill")

    process = LivePopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    manager.write_process_lease(process)
    lease_before = manager.lease_path.read_bytes()
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: False,
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: (_ for _ in ()).throw(
            OSError(5, "live identity query denied")
        ),
    )

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process)

    assert isinstance(captured.value.__cause__, OSError)
    assert signals == []
    assert manager.lease_path.read_bytes() == lease_before


def test_terminate_popen_windows_handle_wait_failure_is_typed_after_cleanup(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)

    class FakePopen:
        pid = identity.pid
        _handle = "owned-handle"

        def __init__(self):
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.alive = False

        def wait(self, timeout):
            return 0

        def kill(self):
            pytest.fail("graceful cleanup should be sufficient")

    process = FakePopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    manager.write_process_lease(process)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: identity,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: (_ for _ in ()).throw(
            OSError("WaitForSingleObject failed")
        ),
    )
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: pytest.fail("Popen cleanup must not enumerate processes"),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda *args, **kwargs: pytest.fail("Popen cleanup must not signal by PID"),
    )

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process)

    assert any("owned handle wait" in note for note in captured.value.__notes__)
    assert process.poll() == 0
    assert manager.read_process_lease() is None


@pytest.mark.parametrize(
    ("poll_results", "handle_results", "expected_signals", "expected_waits"),
    [
        ([None], [True], [], []),
        ([0, 0, 0], [False, False, True], ["terminate"], [3.0]),
    ],
    ids=("popen-live-handle-exited", "popen-exited-handle-live"),
)
def test_terminate_popen_poll_handle_disagreement_is_typed_and_handle_authoritative(
    monkeypatch,
    tmp_path,
    poll_results,
    handle_results,
    expected_signals,
    expected_waits,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    signals = []
    waits = []

    class FakePopen:
        pid = identity.pid
        _handle = "owned-handle"

        def poll(self):
            if not poll_results:
                pytest.fail("unexpected additional Popen.poll call")
            return poll_results.pop(0)

        def terminate(self):
            signals.append("terminate")

        def wait(self, timeout):
            waits.append(timeout)
            return 0

        def kill(self):
            signals.append("kill")

    process = FakePopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    manager.write_process_lease(process)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)

    def observe_handle(handle, timeout_ms):
        assert handle == "owned-handle"
        assert timeout_ms == 0
        if not handle_results:
            pytest.fail("unexpected additional owned handle wait")
        return handle_results.pop(0)

    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        observe_handle,
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: identity,
    )
    monkeypatch.setattr(
        manager,
        "_query_process_creation_from_handle",
        lambda handle, pid: (
            identity.creation_time,
            identity.creation_time_filetime,
        ),
    )
    _forbid_popen_pid_fallbacks(monkeypatch, manager)

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process)

    assert isinstance(captured.value.__cause__, RuntimeError)
    assert any("state agreement" in note for note in captured.value.__notes__)
    assert signals == expected_signals
    assert waits == expected_waits
    assert poll_results == []
    assert handle_results == []
    assert manager.read_process_lease() is None


def test_terminate_popen_malformed_disk_lease_is_not_cleared(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    process = SimpleNamespace(pid=identity.pid, poll=lambda: 0)
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    manager.write_process_lease(process)
    manager.lease_path.write_bytes(b"{malformed")

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process)

    assert isinstance(captured.value.__cause__, OBSProcessLeaseError)
    assert manager.lease_path.read_bytes() == b"{malformed"


def test_terminate_popen_exact_lease_delete_boundary_permission_error_is_typed(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    signals = []

    class ExitedPopen:
        pid = identity.pid

        def poll(self):
            return 0

        def terminate(self):
            signals.append("terminate")

        def kill(self):
            signals.append("kill")

    process = ExitedPopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    manager.write_process_lease(process)
    lease_before = manager.lease_path.read_bytes()
    lease_identity_before = obs_process_module._file_identity(
        manager.lease_path.stat()
    )
    boundary_calls = []
    deletion_failure = PermissionError("simulated exact lease deletion denial")
    real_mark_for_deletion = (
        obs_process_module._OBSDirectoryLease.mark_open_file_for_deletion
    )

    def deny_exact_lease_deletion(
        directory_lease,
        descriptor,
        name,
        *,
        expected_identity,
    ):
        if directory_lease.path == manager.obs_dir and name == manager.lease_path.name:
            descriptor_identity = obs_process_module._file_identity(
                os.fstat(descriptor)
            )
            path_identity = directory_lease.relative_file_identity_or_none(name)
            boundary_calls.append(
                {
                    "descriptor": descriptor,
                    "name": name,
                    "expected_identity": expected_identity,
                    "descriptor_identity": descriptor_identity,
                    "path_identity": path_identity,
                }
            )
            assert descriptor_identity == expected_identity == lease_identity_before
            assert path_identity == lease_identity_before
            raise deletion_failure
        return real_mark_for_deletion(
            directory_lease,
            descriptor,
            name,
            expected_identity=expected_identity,
        )

    monkeypatch.setattr(
        obs_process_module._OBSDirectoryLease,
        "mark_open_file_for_deletion",
        deny_exact_lease_deletion,
    )
    _forbid_popen_pid_fallbacks(monkeypatch, manager)

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process)

    assert isinstance(captured.value.__cause__, OBSProcessLeaseError)
    assert captured.value.__cause__.__cause__ is deletion_failure
    assert str(captured.value.__cause__.__cause__) == (
        "simulated exact lease deletion denial"
    )
    assert any(
        "matching v2 lease cleanup: OBSProcessLeaseError" in note
        for note in captured.value.__notes__
    )
    assert len(boundary_calls) == 1
    assert boundary_calls[0]["name"] == manager.lease_path.name
    assert boundary_calls[0]["expected_identity"] == lease_identity_before
    assert boundary_calls[0]["descriptor_identity"] == lease_identity_before
    assert boundary_calls[0]["path_identity"] == lease_identity_before
    assert signals == []
    assert manager.lease_path.read_bytes() == lease_before
    assert obs_process_module._file_identity(manager.lease_path.stat()) == (
        lease_identity_before
    )


@pytest.mark.parametrize(
    ("fail_on_revalidation", "expected_signals"),
    [(1, []), (2, ["terminate"])],
    ids=("before-graceful", "between-graceful-and-force"),
)
def test_terminate_popen_revalidates_pinned_authorization_before_each_signal(
    monkeypatch,
    tmp_path,
    fail_on_revalidation,
    expected_signals,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    signals = []

    class StubbornPopen:
        pid = identity.pid

        def poll(self):
            return None

        def terminate(self):
            signals.append("terminate")

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("obs", timeout)

        def kill(self):
            signals.append("kill")

    process = StubbornPopen()
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    manager.write_process_lease(process)
    lease_before = manager.lease_path.read_bytes()
    real_revalidate = manager._revalidate_process_lease_snapshot_locked
    calls = 0

    def fail_selected_revalidation(transaction, snapshot):
        nonlocal calls
        calls += 1
        if calls == fail_on_revalidation:
            raise OBSProcessLeaseError("simulated authorization loss")
        return real_revalidate(transaction, snapshot)

    monkeypatch.setattr(
        manager,
        "_revalidate_process_lease_snapshot_locked",
        fail_selected_revalidation,
    )

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process, timeout_sec=0)

    assert signals == expected_signals
    assert manager.lease_path.read_bytes() == lease_before
    assert str(manager.lease_path) in str(captured.value)
    assert str(manager.lease_lock_path) in str(captured.value)


def test_terminate_popen_preserves_a_replaced_same_pid_lease(monkeypatch, tmp_path):
    obs_dir = tmp_path / "obs-portable"
    manager = OBSProcessManager(obs_dir)
    writer_manager = OBSProcessManager(obs_dir)
    old_identity = _identity(manager)
    replacement = _identity(
        writer_manager,
        filetime=old_identity.creation_time_filetime + 10,
    )
    signals = []

    class ExitedPopen:
        pid = old_identity.pid

        def poll(self):
            return 0

        def terminate(self):
            signals.append("terminate")

        def kill(self):
            signals.append("kill")

    process = ExitedPopen()
    monkeypatch.setattr(
        manager,
        "query_popen_process_identity",
        lambda candidate: old_identity,
    )
    manager.write_process_lease(process)
    _write_v2_lease(writer_manager, replacement)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: pytest.fail("replacement cleanup must not enumerate PIDs"),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda *args, **kwargs: pytest.fail("replacement PID must not be signaled"),
    )

    lease_before = writer_manager.lease_path.read_bytes()

    with pytest.raises(OBSProcessTerminationError) as captured:
        manager.terminate_process(process)

    assert isinstance(captured.value.__cause__, OBSProcessLeaseError)
    assert "differs" in str(captured.value.__cause__)
    assert signals == []
    assert writer_manager.lease_path.read_bytes() == lease_before
    final_lease = manager.read_process_lease()
    assert final_lease is not None
    assert final_lease.process_creation_time_filetime == (
        replacement.creation_time_filetime
    )


def test_process_manager_owned_kill_requires_lease(monkeypatch, tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    calls = []
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: pytest.fail("no query without lease"),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda pid, force: calls.append((pid, force)) or True,
    )

    assert manager.kill_stale_owned_processes(timeout_sec=0) == []
    assert calls == []


def test_legacy_lease_is_readable_but_live_process_is_never_signaled(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_dir.mkdir(parents=True)
    manager.lease_path.write_text(
        json.dumps(
            {
                "pid": 100,
                "executable_path": str(manager.obs_exe),
                "created_at": 1.0,
                "process_creation_time": 10.0,
            }
        ),
        encoding="utf-8",
    )
    target = _identity(manager)
    signals = []
    monkeypatch.setattr(manager, "query_obs_processes_strict", lambda: _snapshot(target))
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda pid, force: signals.append((pid, force)) or True,
    )

    lease = manager.read_process_lease()
    assert lease is not None
    assert lease.schema_version == 1
    assert lease.process_creation_time_filetime is None
    with pytest.raises(OBSProcessLeaseError, match="Legacy"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert signals == []
    assert manager.lease_path.exists()


def test_legacy_lease_is_cleared_only_after_strict_pid_absence(monkeypatch, tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_dir.mkdir(parents=True)
    manager.lease_path.write_text(
        json.dumps(
            {
                "pid": 100,
                "executable_path": str(manager.obs_exe),
                "created_at": 1.0,
                "process_creation_time": 10.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(manager, "query_obs_processes_strict", lambda: _snapshot())

    assert manager.kill_stale_owned_processes(timeout_sec=0) == []
    assert not manager.lease_path.exists()


@pytest.mark.parametrize(
    "payload",
    [
        '{"version": 2, "pid": true}',
        json.dumps(
            {
                "version": 2,
                "pid": 100,
                "executable_path": "relative/obs64.exe",
                "created_at": 1.0,
                "process_creation_time": 10.0,
                "process_creation_time_filetime": _raw_filetime(10.0),
            }
        ),
        json.dumps(
            {
                "version": 2,
                "pid": 100,
                "executable_path": "C:/obs/bin/64bit/obs64.exe",
                "created_at": 1.0,
                "process_creation_time": 10.0,
            }
        ),
        json.dumps(
            {
                "version": 2,
                "pid": 100,
                "executable_path": "C:/obs/bin/64bit/obs64.exe",
                "created_at": 1.0,
                "process_creation_time": 10.0,
                "process_creation_time_filetime": _raw_filetime(11.0),
            }
        ),
        json.dumps(
            {
                "version": 3,
                "pid": 100,
                "executable_path": "C:/obs/bin/64bit/obs64.exe",
                "created_at": 1.0,
                "process_creation_time": 10.0,
                "process_creation_time_filetime": _raw_filetime(10.0),
            }
        ),
    ],
    ids=("invalid-pid", "relative-path", "missing-filetime", "time-mismatch", "future-version"),
)
def test_malformed_lease_fails_without_query_clear_or_signal(
    monkeypatch,
    tmp_path,
    payload,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    manager.obs_dir.mkdir(parents=True)
    manager.lease_path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: pytest.fail("must not query"),
    )
    monkeypatch.setattr(manager, "_terminate_pid", lambda *args: pytest.fail("must not signal"))

    with pytest.raises(OBSProcessLeaseError, match="malformed") as captured:
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert manager.lease_path.exists()
    assert str(manager.lease_path) in str(captured.value)
    assert str(manager.lease_lock_path) in str(captured.value)


def test_owned_lookup_fails_on_same_pid_path_with_different_filetime(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    replacement = _identity(manager, filetime=target.creation_time_filetime + 1)
    _write_v2_lease(manager, target)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: _snapshot(replacement),
    )
    monkeypatch.setattr(manager, "_terminate_pid", lambda *args: pytest.fail("must not signal"))

    with pytest.raises(OBSProcessLeaseError, match="reused|identity"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert manager.lease_path.exists()


def test_owned_lookup_query_failure_keeps_lease_and_does_not_signal(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: (_ for _ in ()).throw(OBSProcessQueryError("query failed")),
    )
    monkeypatch.setattr(manager, "_terminate_pid", lambda *args: pytest.fail("must not signal"))

    with pytest.raises(OBSProcessQueryError, match="query failed"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert manager.lease_path.exists()


def _install_owned_windows_harness(
    monkeypatch,
    manager: OBSProcessManager,
    target: OBSProcessInfo,
    snapshots: list[OBSProcessQuerySnapshot | BaseException],
    waits: list[bool | BaseException],
    *,
    graceful_result: bool = True,
    force_result: bool = True,
) -> list[tuple]:
    events: list[tuple] = []
    remaining_snapshots = list(snapshots)
    remaining_waits = list(waits)

    def query_snapshot():
        if not remaining_snapshots:
            pytest.fail("unexpected strict process query")
        snapshot = remaining_snapshots.pop(0)
        if isinstance(snapshot, BaseException):
            events.append(("snapshot-error", str(snapshot)))
            raise snapshot
        events.append(("snapshot", tuple(item.pid for item in snapshot.processes)))
        return snapshot

    def wait_handle(handle, *, timeout_ms):
        assert handle == "owned-handle"
        assert timeout_ms == 0
        if not remaining_waits:
            pytest.fail("unexpected handle wait")
        result = remaining_waits.pop(0)
        events.append(("wait", result))
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(manager, "query_obs_processes_strict", query_snapshot)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "_open_process_identity_handle",
        lambda pid: events.append(("open", pid)) or "owned-handle",
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: events.append(("handle-identity", handle, pid)) or target,
    )
    monkeypatch.setattr(manager, "_wait_process_identity_handle", wait_handle)
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda pid, force: events.append(("signal", pid, force)) or graceful_result,
    )
    monkeypatch.setattr(
        manager,
        "_terminate_process_identity_handle",
        lambda handle: events.append(("force", handle)) or force_result,
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: events.append(("close", handle)),
    )
    return events


@pytest.mark.parametrize(
    ("fail_on_revalidation", "graceful_expected"),
    [(1, False), (2, True)],
    ids=("before-graceful", "before-force"),
)
def test_stale_owned_cleanup_revalidates_lease_before_each_signal(
    monkeypatch,
    tmp_path,
    fail_on_revalidation,
    graceful_expected,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    lease_before = manager.lease_path.read_bytes()
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
        ],
        [False, False, False, False, False, False],
    )
    real_revalidate = manager._revalidate_process_lease_snapshot_locked
    calls = 0

    def fail_selected_revalidation(transaction, snapshot):
        nonlocal calls
        calls += 1
        if calls == fail_on_revalidation:
            raise OBSProcessLeaseError("simulated stale authorization loss")
        return real_revalidate(transaction, snapshot)

    monkeypatch.setattr(
        manager,
        "_revalidate_process_lease_snapshot_locked",
        fail_selected_revalidation,
    )

    with pytest.raises(OBSProcessLeaseError, match="authorization loss"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    graceful_signals = [event for event in events if event[:1] == ("signal",)]
    force_signals = [event for event in events if event[:1] == ("force",)]
    assert bool(graceful_signals) is graceful_expected
    assert force_signals == []
    assert manager.lease_path.read_bytes() == lease_before


def test_owned_cleanup_stops_only_leased_identity_and_leaves_unrelated_obs(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    unrelated = _identity(
        manager,
        pid=200,
        unix_seconds=20.0,
        executable_path=Path("C:/Program Files/obs-studio/bin/64bit/obs64.exe"),
    )
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target, unrelated),
            _snapshot(target, unrelated),
            _snapshot(target, unrelated),
            _snapshot(unrelated),
        ],
        [False, False, False, True],
    )

    assert manager.kill_stale_owned_processes(timeout_sec=1) == [target.pid]

    assert ("signal", target.pid, False) in events
    assert not any(event[:2] == ("signal", unrelated.pid) for event in events)
    assert events[-1] == ("close", "owned-handle")
    assert not manager.lease_path.exists()


def test_owned_cleanup_accepts_natural_exit_without_reporting_a_signal(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    unrelated = _identity(manager, pid=200, unix_seconds=20.0)
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target, unrelated),
            _snapshot(target, unrelated),
            _snapshot(unrelated),
        ],
        [True, True],
    )

    assert manager.kill_stale_owned_processes(timeout_sec=0) == []
    assert not any(event[0] in {"signal", "force"} for event in events)
    assert not manager.lease_path.exists()


def test_owned_cleanup_accepts_natural_exit_during_handle_open(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    snapshots = iter([_snapshot(target), _snapshot(target), _snapshot()])
    monkeypatch.setattr(manager, "query_obs_processes_strict", lambda: next(snapshots))
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "_open_process_identity_handle",
        lambda pid: (_ for _ in ()).throw(OSError("process exited")),
    )
    monkeypatch.setattr(manager, "_terminate_pid", lambda *args: pytest.fail("must not signal"))

    assert manager.kill_stale_owned_processes(timeout_sec=0) == []
    assert not manager.lease_path.exists()


def test_owned_cleanup_handle_identity_mismatch_fails_before_signal(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    replacement = _identity(manager, filetime=target.creation_time_filetime + 1)
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [_snapshot(target), _snapshot(target)],
        [False],
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: replacement,
    )

    with pytest.raises(OBSProcessQueryError, match="handle identity"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert not any(event[0] in {"signal", "force"} for event in events)
    assert manager.lease_path.exists()


def test_owned_cleanup_force_uses_the_same_verified_handle(monkeypatch, tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(),
        ],
        [False, False, False, False, False, False, True],
    )

    assert manager.kill_stale_owned_processes(timeout_sec=0) == [target.pid]
    assert ("force", "owned-handle") in events
    assert events[-1] == ("close", "owned-handle")
    assert not manager.lease_path.exists()


def test_owned_cleanup_rejects_same_pid_replacement_before_signal(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    replacement = _identity(manager, filetime=target.creation_time_filetime + 10)
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [_snapshot(target), _snapshot(target), _snapshot(replacement)],
        [False, False],
    )

    with pytest.raises(OBSProcessQueryError, match="replaced"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert not any(event[0] in {"signal", "force"} for event in events)
    assert manager.lease_path.exists()


def test_owned_cleanup_rejects_replacement_during_graceful_wait(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    replacement = _identity(manager, filetime=target.creation_time_filetime + 10)
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(replacement),
        ],
        [False, False, False],
    )

    with pytest.raises(OBSProcessQueryError, match="replaced"):
        manager.kill_stale_owned_processes(timeout_sec=1)

    assert events.count(("signal", target.pid, False)) == 1
    assert not any(event[0] == "force" for event in events)
    assert manager.lease_path.exists()


def test_owned_cleanup_rejects_same_pid_replacement_immediately_before_force(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    replacement = _identity(manager, filetime=target.creation_time_filetime + 10)
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(replacement),
        ],
        [False, False, False, False],
    )

    with pytest.raises(OBSProcessQueryError, match="replaced"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert events.count(("signal", target.pid, False)) == 1
    assert not any(event[0] == "force" for event in events)
    assert manager.lease_path.exists()


def test_owned_cleanup_keeps_lease_when_query_fails_after_graceful_signal(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    query_results = iter(
        [
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            OBSProcessQueryError("post-signal query failed"),
        ]
    )
    waits = iter([False, False, False, False])
    signals = []
    closed = []

    def query():
        result = next(query_results)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "owned-handle")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: target,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, *, timeout_ms: next(waits),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda pid, force: signals.append((pid, force)) or True,
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )
    monkeypatch.setattr(
        obs_process_module.time,
        "sleep",
        lambda delay: pytest.fail("a live handle must fail without retry delay"),
    )

    with pytest.raises(OBSProcessQueryError, match="post-signal query failed"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert signals == [(target.pid, False)]
    assert closed == ["owned-handle"]
    assert manager.lease_path.exists()


@pytest.mark.parametrize("failure_kind", ["query-error", "malformed-target-row"])
def test_owned_cleanup_retries_once_after_exited_handle_query_failure(
    monkeypatch,
    tmp_path,
    failure_kind,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    unrelated = _identity(
        manager,
        pid=200,
        unix_seconds=20.0,
        executable_path=Path("C:/Program Files/obs-studio/bin/64bit/obs64.exe"),
    )
    malformed_target = OBSProcessInfo(
        pid=target.pid,
        executable_path=None,
        creation_time=target.creation_time,
        creation_time_filetime=target.creation_time_filetime,
    )
    transient_failure = (
        OBSProcessQueryError("transient strict query failure")
        if failure_kind == "query-error"
        else _snapshot(malformed_target)
    )
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target, unrelated),
            _snapshot(target, unrelated),
            _snapshot(target, unrelated),
            transient_failure,
            _snapshot(unrelated),
        ],
        [False, False, False, True, True],
    )
    delays = []
    monkeypatch.setattr(
        obs_process_module.time,
        "sleep",
        lambda delay: delays.append(delay),
    )

    assert manager.kill_stale_owned_processes(timeout_sec=0) == [target.pid]

    assert delays == [obs_process_module._OWNED_EXIT_STRICT_REQUERY_DELAY_SEC]
    assert events.count(("signal", target.pid, False)) == 1
    assert not any(event[:2] == ("signal", unrelated.pid) for event in events)
    assert not manager.lease_path.exists()


def test_owned_cleanup_retries_natural_exit_query_failure_once(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target),
            _snapshot(target),
            OBSProcessQueryError("transient natural-exit query failure"),
            _snapshot(),
        ],
        [True, True, True],
    )
    delays = []
    monkeypatch.setattr(
        obs_process_module.time,
        "sleep",
        lambda delay: delays.append(delay),
    )

    assert manager.kill_stale_owned_processes(timeout_sec=0) == []

    assert delays == [obs_process_module._OWNED_EXIT_STRICT_REQUERY_DELAY_SEC]
    assert not any(event[0] in {"signal", "force"} for event in events)
    assert not manager.lease_path.exists()


def test_owned_cleanup_repeated_exit_query_failure_keeps_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            OBSProcessQueryError("first transient query failure"),
            OBSProcessQueryError("repeated query failure"),
        ],
        [False, False, False, True, True],
    )
    monkeypatch.setattr(obs_process_module.time, "sleep", lambda delay: None)

    with pytest.raises(OBSProcessQueryError, match="repeated query failure"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert events.count(("signal", target.pid, False)) == 1
    assert not any(event[0] == "force" for event in events)
    assert manager.lease_path.exists()


def test_owned_cleanup_does_not_retry_after_retained_exited_row_then_error(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            OBSProcessQueryError("query failed after retained exited row"),
            _snapshot(),
        ],
        [False, False, False, True],
    )
    monkeypatch.setattr(
        obs_process_module.time,
        "sleep",
        lambda delay: pytest.fail("the shared exit retry budget is exhausted"),
    )

    with pytest.raises(
        OBSProcessQueryError,
        match="query failed after retained exited row",
    ):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert ("snapshot", ()) not in events
    assert events.count(("signal", target.pid, False)) == 1
    assert not any(event[0] == "force" for event in events)
    assert manager.lease_path.exists()


def test_owned_cleanup_exit_requery_rejects_replacement_and_keeps_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    replacement = _identity(
        manager,
        filetime=target.creation_time_filetime + 1,
    )
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            OBSProcessQueryError("transient query failure"),
            _snapshot(replacement),
        ],
        [False, False, False, True],
    )
    monkeypatch.setattr(obs_process_module.time, "sleep", lambda delay: None)

    with pytest.raises(OBSProcessQueryError, match="replaced"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert events.count(("signal", target.pid, False)) == 1
    assert not any(event[0] == "force" for event in events)
    assert manager.lease_path.exists()


def test_owned_cleanup_repeated_malformed_target_row_keeps_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    malformed_target = OBSProcessInfo(
        pid=target.pid,
        executable_path=None,
        creation_time=target.creation_time,
        creation_time_filetime=target.creation_time_filetime,
    )
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(malformed_target),
            _snapshot(malformed_target),
        ],
        [False, False, False, True, True],
    )
    monkeypatch.setattr(obs_process_module.time, "sleep", lambda delay: None)

    with pytest.raises(OBSProcessQueryError, match="missing or not absolute"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert events.count(("signal", target.pid, False)) == 1
    assert not any(event[0] == "force" for event in events)
    assert manager.lease_path.exists()


def test_owned_cleanup_keeps_lease_when_live_handle_identity_query_fails(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [_snapshot(target), _snapshot(target)],
        [False, False],
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: (_ for _ in ()).throw(OSError("identity query failed")),
    )

    with pytest.raises(OSError, match="identity query failed"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert not any(event[0] in {"signal", "force"} for event in events)
    assert events[-1] == ("close", "owned-handle")
    assert manager.lease_path.exists()


def test_owned_cleanup_keeps_lease_when_graceful_signal_fails_live(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [_snapshot(target), _snapshot(target), _snapshot(target)],
        [False, False, False, False],
        graceful_result=False,
    )

    with pytest.raises(OBSProcessQueryError, match="graceful termination failed"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert manager.lease_path.exists()


def test_owned_cleanup_keeps_lease_when_force_signal_fails_live(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
        ],
        [False, False, False, False, False, False, False],
        force_result=False,
    )

    with pytest.raises(OBSProcessQueryError, match="Handle-bound"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert manager.lease_path.exists()


def test_owned_cleanup_keeps_lease_when_final_target_survives_force(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
            _snapshot(target),
        ],
        [False, False, False, False, False, False, False],
    )

    with pytest.raises(OBSProcessQueryError, match="survived"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert manager.lease_path.exists()


def test_owned_cleanup_close_failure_is_not_success_and_keeps_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [_snapshot(target), _snapshot(target), _snapshot(target), _snapshot()],
        [False, False, False, True],
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: (_ for _ in ()).throw(OSError("close failed")),
    )

    with pytest.raises(OBSProcessQueryError, match="could not be closed"):
        manager.kill_stale_owned_processes(timeout_sec=1)

    assert manager.lease_path.exists()


def test_owned_cleanup_wait_failure_does_not_signal_or_clear_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    target = _identity(manager)
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [_snapshot(target), _snapshot(target)],
        [OSError("wait failed")],
    )

    with pytest.raises(OSError, match="wait failed"):
        manager.kill_stale_owned_processes(timeout_sec=0)

    assert not any(event[0] in {"signal", "force"} for event in events)
    assert manager.lease_path.exists()


def test_matching_lease_clear_cannot_delete_a_concurrent_new_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    old_identity = _identity(manager, pid=100, unix_seconds=10.0)
    new_identity = _identity(manager, pid=200, unix_seconds=20.0)
    _write_v2_lease(manager, old_identity)
    old_lease = manager.read_process_lease()
    assert old_lease is not None

    delete_entered = threading.Event()
    allow_delete = threading.Event()
    writer_finished = threading.Event()
    original_delete = manager._delete_process_lease_snapshot_locked

    def blocking_delete(transaction, snapshot):
        delete_entered.set()
        assert allow_delete.wait(timeout=2)
        return original_delete(transaction, snapshot)

    monkeypatch.setattr(
        manager,
        "_delete_process_lease_snapshot_locked",
        blocking_delete,
    )
    thread_lock = _InstrumentedLeaseThreadLock()
    monkeypatch.setattr(
        obs_process_module,
        "_OBS_PROCESS_LEASE_LOCK",
        thread_lock,
    )
    monkeypatch.setattr(
        manager,
        "query_popen_process_identity",
        lambda process: new_identity,
    )
    new_process = SimpleNamespace(pid=new_identity.pid)

    clear_thread = threading.Thread(
        target=manager._clear_matching_process_lease,
        args=(old_lease,),
    )

    def write_new_lease():
        manager.write_process_lease(new_process)
        writer_finished.set()

    writer_thread = threading.Thread(target=write_new_lease)
    thread_lock.watch(writer_thread)
    clear_thread.start()
    assert delete_entered.wait(timeout=2)
    writer_thread.start()
    assert thread_lock.attempted.wait(timeout=2)
    assert not thread_lock.acquired.is_set()
    assert not writer_finished.is_set()
    allow_delete.set()
    clear_thread.join(timeout=2)
    writer_thread.join(timeout=2)

    assert not clear_thread.is_alive()
    assert not writer_thread.is_alive()
    assert thread_lock.acquired.is_set()
    assert writer_finished.is_set()
    final_lease = manager.read_process_lease()
    assert final_lease is not None
    assert final_lease.pid == new_identity.pid
    assert final_lease.process_creation_time_filetime == (
        new_identity.creation_time_filetime
    )


def test_owned_cleanup_serializes_lease_replacement_before_any_signal(
    monkeypatch,
    tmp_path,
):
    obs_dir = tmp_path / "obs-portable"
    manager = OBSProcessManager(obs_dir)
    writer_manager = OBSProcessManager(obs_dir)
    target = _identity(manager, pid=100, unix_seconds=10.0)
    replacement = _identity(writer_manager, pid=200, unix_seconds=20.0)
    _write_v2_lease(manager, target)
    events = _install_owned_windows_harness(
        monkeypatch,
        manager,
        target,
        [_snapshot(target), _snapshot(target), _snapshot(target), _snapshot()],
        [False, False, False, True],
    )
    original_query = manager.query_obs_processes_strict
    cleanup_query_entered = threading.Event()
    allow_cleanup_query = threading.Event()
    writer_finished = threading.Event()
    cleanup_result = []
    thread_errors = []
    first_query = True

    def blocking_query():
        nonlocal first_query
        if first_query:
            first_query = False
            cleanup_query_entered.set()
            assert allow_cleanup_query.wait(timeout=2)
        return original_query()

    monkeypatch.setattr(manager, "query_obs_processes_strict", blocking_query)
    thread_lock = _InstrumentedLeaseThreadLock()
    monkeypatch.setattr(
        obs_process_module,
        "_OBS_PROCESS_LEASE_LOCK",
        thread_lock,
    )
    monkeypatch.setattr(
        writer_manager,
        "query_popen_process_identity",
        lambda process: replacement,
    )
    new_process = SimpleNamespace(pid=replacement.pid)

    def cleanup():
        try:
            cleanup_result.extend(manager.kill_stale_owned_processes(timeout_sec=1))
        except BaseException as exc:
            thread_errors.append(exc)

    def replace_lease():
        try:
            writer_manager.write_process_lease(new_process)
            writer_finished.set()
        except BaseException as exc:
            thread_errors.append(exc)

    cleanup_thread = threading.Thread(target=cleanup)
    writer_thread = threading.Thread(target=replace_lease)
    thread_lock.watch(writer_thread)
    cleanup_thread.start()
    assert cleanup_query_entered.wait(timeout=2)
    writer_thread.start()
    assert thread_lock.attempted.wait(timeout=2)
    assert not thread_lock.acquired.is_set()
    assert not writer_finished.is_set()
    allow_cleanup_query.set()
    cleanup_thread.join(timeout=2)
    writer_thread.join(timeout=2)

    assert not cleanup_thread.is_alive()
    assert not writer_thread.is_alive()
    assert thread_lock.acquired.is_set()
    assert thread_errors == []
    assert cleanup_result == [target.pid]
    assert ("signal", target.pid, False) in events
    assert writer_finished.is_set()
    final_lease = manager.read_process_lease()
    assert final_lease is not None
    assert final_lease.pid == replacement.pid


def test_lease_transaction_cleanup_preserves_body_primary_and_notes_all_failures(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    process = SimpleNamespace(pid=identity.pid)
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    manager.write_process_lease(process)
    real_close = obs_process_module.os.close
    real_release = obs_process_module._OBSInterProcessLock.release
    snapshot_descriptor = None

    def close_then_fail(descriptor):
        real_close(descriptor)
        if descriptor == snapshot_descriptor:
            raise OSError("simulated snapshot close failure")

    def release_then_fail(lock):
        real_release(lock)
        raise OSError("simulated lock release failure")

    monkeypatch.setattr(obs_process_module.os, "close", close_then_fail)
    monkeypatch.setattr(
        obs_process_module._OBSInterProcessLock,
        "release",
        release_then_fail,
    )

    with pytest.raises(RuntimeError, match="body primary") as captured:
        with manager._process_lease_transaction() as transaction:
            assert transaction is not None
            snapshot = manager._open_process_lease_snapshot_locked(transaction)
            assert snapshot is not None
            snapshot_descriptor = snapshot.descriptor
            raise RuntimeError("body primary")

    notes = getattr(captured.value, "__notes__", [])
    assert any("snapshot close failure" in note for note in notes)
    assert any("lock release failure" in note for note in notes)


def test_lease_transaction_raises_first_cleanup_failure_after_successful_body(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    process = SimpleNamespace(pid=identity.pid)
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    manager.write_process_lease(process)
    real_close = obs_process_module.os.close
    real_release = obs_process_module._OBSInterProcessLock.release
    snapshot_descriptor = None

    def close_then_fail(descriptor):
        real_close(descriptor)
        if descriptor == snapshot_descriptor:
            raise OSError("simulated successful-body close failure")

    def release_then_fail(lock):
        real_release(lock)
        raise OSError("simulated later release failure")

    monkeypatch.setattr(obs_process_module.os, "close", close_then_fail)
    monkeypatch.setattr(
        obs_process_module._OBSInterProcessLock,
        "release",
        release_then_fail,
    )

    with pytest.raises(
        OBSProcessLeaseError,
        match="終了処理を安全に完了",
    ) as captured:
        with manager._process_lease_transaction() as transaction:
            assert transaction is not None
            snapshot = manager._open_process_lease_snapshot_locked(transaction)
            assert snapshot is not None
            snapshot_descriptor = snapshot.descriptor

    _assert_actionable_lease_recovery(captured.value, manager)
    assert isinstance(captured.value.__cause__, OSError)
    assert "successful-body close failure" in str(captured.value.__cause__)
    assert any(
        "later release failure" in note
        for note in getattr(captured.value, "__notes__", [])
    )


@pytest.mark.parametrize("changed_owner", ["lock", "root"])
def test_orphan_inventory_revalidates_ownership_before_opening_temporary(
    monkeypatch,
    tmp_path,
    changed_owner,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    _write_v2_lease(manager, identity)
    temporary_path = manager.obs_dir / (
        f"{obs_process_module.OBS_PROCESS_LEASE_TEMP_PREFIX}{'d' * 32}"
    )
    temporary_path.write_bytes(b"orphaned temporary lease")
    lease_bytes = manager.lease_path.read_bytes()
    lease_identity = obs_process_module._file_identity(manager.lease_path.stat())
    temporary_bytes = temporary_path.read_bytes()
    temporary_identity = obs_process_module._file_identity(temporary_path.stat())
    inventory_finished = False
    failure = OSError(f"simulated {changed_owner} change during inventory")
    opened_names = []
    real_scandir = obs_process_module.os.scandir

    class InventoryFaultLock:
        def validate_ownership(self):
            if changed_owner == "lock" and inventory_finished:
                raise failure

    class ScandirWithInventoryFault:
        def __init__(self, path):
            self._entries = real_scandir(path)

        def __enter__(self):
            return self._entries.__enter__()

        def __exit__(self, exc_type, exc, traceback):
            nonlocal inventory_finished
            try:
                return self._entries.__exit__(exc_type, exc, traceback)
            finally:
                inventory_finished = True

    with obs_process_module._OBSDirectoryLease.open_absolute(
        manager.obs_dir,
        mutable=True,
    ) as root_lease:
        transaction = obs_process_module._OBSProcessLeaseTransaction(
            lock=InventoryFaultLock(),
            root_lease=root_lease,
        )
        real_validate_root = root_lease.validate_lexical_binding
        real_open_file = root_lease.open_file

        def validate_root():
            if changed_owner == "root" and inventory_finished:
                raise failure
            return real_validate_root()

        def track_open_file(name, **kwargs):
            opened_names.append(name)
            return real_open_file(name, **kwargs)

        def scandir_inventory(path):
            assert Path(path) == root_lease.path
            return ScandirWithInventoryFault(path)

        with monkeypatch.context() as patch:
            patch.setattr(
                root_lease,
                "validate_lexical_binding",
                validate_root,
            )
            patch.setattr(root_lease, "open_file", track_open_file)
            patch.setattr(obs_process_module.os, "scandir", scandir_inventory)
            with pytest.raises(OBSProcessLeaseError) as captured:
                manager._recover_process_lease_temporaries_locked(transaction)

    _assert_actionable_lease_recovery(captured.value, manager)
    assert captured.value.__cause__ is failure
    assert opened_names == []
    assert transaction.commit_occurred is False
    assert transaction.recovered_temporary_paths == []
    assert manager.lease_path.read_bytes() == lease_bytes
    assert obs_process_module._file_identity(manager.lease_path.stat()) == lease_identity
    assert temporary_path.read_bytes() == temporary_bytes
    assert obs_process_module._file_identity(temporary_path.stat()) == temporary_identity


def test_orphan_recovery_reports_later_snapshot_close_failure_without_removing_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    _write_v2_lease(manager, identity)
    temporary_path = manager.obs_dir / (
        f"{obs_process_module.OBS_PROCESS_LEASE_TEMP_PREFIX}{'a' * 32}"
    )
    temporary_path.write_bytes(b"orphaned temporary lease")
    real_open = manager._open_process_lease_snapshot_locked
    real_close = obs_process_module.os.close
    snapshot_descriptor = None

    def capture_snapshot(transaction):
        nonlocal snapshot_descriptor
        snapshot = real_open(transaction)
        assert snapshot is not None
        snapshot_descriptor = snapshot.descriptor
        return snapshot

    def close_then_fail(descriptor):
        real_close(descriptor)
        if descriptor == snapshot_descriptor:
            raise OSError("simulated snapshot close after orphan recovery")

    with monkeypatch.context() as patch:
        patch.setattr(
            manager,
            "_open_process_lease_snapshot_locked",
            capture_snapshot,
        )
        patch.setattr(obs_process_module.os, "close", close_then_fail)
        with pytest.raises(OBSProcessLeaseError) as captured:
            manager.read_process_lease()

    _assert_actionable_lease_recovery(captured.value, manager)
    assert isinstance(captured.value.__cause__, OSError)
    assert "snapshot close after orphan recovery" in str(captured.value.__cause__)
    assert any(
        "削除済み" in note and str(temporary_path) in note
        for note in getattr(captured.value, "__notes__", [])
    )
    assert not temporary_path.exists()
    assert manager.lease_path.exists()
    assert manager.read_process_lease() is not None


def test_orphan_recovery_reports_later_lock_release_failure_without_removing_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    _write_v2_lease(manager, identity)
    temporary_path = manager.obs_dir / (
        f"{obs_process_module.OBS_PROCESS_LEASE_TEMP_PREFIX}{'b' * 32}"
    )
    temporary_path.write_bytes(b"orphaned temporary lease")
    real_release = obs_process_module._OBSInterProcessLock.release

    def release_then_fail(lock):
        real_release(lock)
        raise OSError("simulated lock release after orphan recovery")

    with monkeypatch.context() as patch:
        patch.setattr(
            obs_process_module._OBSInterProcessLock,
            "release",
            release_then_fail,
        )
        with pytest.raises(OBSProcessLeaseError) as captured:
            manager.read_process_lease()

    _assert_actionable_lease_recovery(captured.value, manager)
    assert isinstance(captured.value.__cause__, OSError)
    assert "lock release after orphan recovery" in str(captured.value.__cause__)
    assert any(
        "削除済み" in note and str(temporary_path) in note
        for note in getattr(captured.value, "__notes__", [])
    )
    assert not temporary_path.exists()
    assert manager.lease_path.exists()
    assert manager.read_process_lease() is not None


@pytest.mark.parametrize("operation", ["fstat", "lseek", "read"])
def test_lease_snapshot_io_failure_is_actionable_and_preserves_lease(
    monkeypatch,
    tmp_path,
    operation,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    _write_v2_lease(manager, identity)
    failure = OSError(f"simulated lease {operation} failure")
    lease_descriptor = None
    real_open_file = obs_process_module._OBSDirectoryLease.open_file
    real_operation = getattr(obs_process_module.os, operation)

    def capture_open_file(directory, name, **kwargs):
        nonlocal lease_descriptor
        descriptor = real_open_file(directory, name, **kwargs)
        if name == manager.lease_path.name and lease_descriptor is None:
            lease_descriptor = descriptor
        return descriptor

    def fail_lease_operation(*args):
        if args[0] == lease_descriptor:
            raise failure
        return real_operation(*args)

    with pytest.raises(OBSProcessLeaseError) as captured:
        with manager._process_lease_transaction(mutating=True) as transaction:
            assert transaction is not None
            with monkeypatch.context() as patch:
                patch.setattr(
                    obs_process_module._OBSDirectoryLease,
                    "open_file",
                    capture_open_file,
                )
                patch.setattr(obs_process_module.os, operation, fail_lease_operation)
                manager._open_process_lease_snapshot_locked(transaction)

    _assert_actionable_lease_recovery(captured.value, manager)
    assert captured.value.__cause__ is failure
    assert manager.lease_path.exists()
    assert manager.read_process_lease() is not None


def test_lease_snapshot_revalidation_io_failure_is_actionable_and_preserves_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    _write_v2_lease(manager, identity)
    failure = OSError("simulated lease revalidation failure")

    with pytest.raises(OBSProcessLeaseError) as captured:
        with manager._process_lease_transaction(mutating=True) as transaction:
            assert transaction is not None
            snapshot = manager._open_process_lease_snapshot_locked(transaction)
            assert snapshot is not None

            def fail_revalidation(_transaction, _descriptor, **_kwargs):
                raise failure

            with monkeypatch.context() as patch:
                patch.setattr(
                    manager,
                    "_read_process_lease_descriptor_bytes_locked",
                    fail_revalidation,
                )
                manager._revalidate_process_lease_snapshot_locked(
                    transaction,
                    snapshot,
                )

    _assert_actionable_lease_recovery(captured.value, manager)
    assert captured.value.__cause__ is failure
    assert manager.lease_path.exists()
    assert manager.read_process_lease() is not None


def test_lease_snapshot_ownership_io_failure_is_actionable_and_preserves_lease(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    _write_v2_lease(manager, identity)
    failure = OSError("simulated lease ownership revalidation failure")

    with pytest.raises(OBSProcessLeaseError) as captured:
        with manager._process_lease_transaction(mutating=True) as transaction:
            assert transaction is not None

            def fail_ownership():
                raise failure

            with monkeypatch.context() as patch:
                patch.setattr(
                    transaction,
                    "validate_ownership",
                    fail_ownership,
                )
                manager._open_process_lease_snapshot_locked(transaction)

    _assert_actionable_lease_recovery(captured.value, manager)
    assert captured.value.__cause__ is failure
    assert manager.lease_path.exists()
    assert manager.read_process_lease() is not None


def test_lease_delete_consumes_descriptor_before_reraising_base_exception(
    monkeypatch,
    tmp_path,
):
    class CloseReportedControlFlow(BaseException):
        pass

    manager = OBSProcessManager(tmp_path / "obs-portable")
    old_identity = _identity(manager)
    _write_v2_lease(manager, old_identity)
    real_close = obs_process_module.os.close
    snapshot_descriptor = None
    close_count = 0

    def close_then_report(descriptor):
        nonlocal close_count
        real_close(descriptor)
        if descriptor == snapshot_descriptor:
            close_count += 1
            raise CloseReportedControlFlow("simulated post-delete close report")

    with pytest.raises(CloseReportedControlFlow, match="post-delete close report"):
        with manager._process_lease_transaction(mutating=True) as transaction:
            assert transaction is not None
            snapshot = manager._open_process_lease_snapshot_locked(transaction)
            assert snapshot is not None
            snapshot_descriptor = snapshot.descriptor
            with monkeypatch.context() as patch:
                patch.setattr(obs_process_module.os, "close", close_then_report)
                manager._delete_process_lease_snapshot_locked(transaction, snapshot)

    assert snapshot.deletion_marked is True
    assert transaction.snapshot is None
    assert transaction.commit_occurred is True
    assert close_count == 1
    assert not manager.lease_path.exists()

    new_identity = _identity(manager, pid=101, unix_seconds=11.0)
    process = SimpleNamespace(pid=new_identity.pid)
    monkeypatch.setattr(
        manager,
        "query_popen_process_identity",
        lambda candidate: new_identity,
    )
    manager.write_process_lease(process)
    assert manager.read_process_lease() == process._lol_replay_obs_process_lease


def test_orphan_delete_consumes_descriptor_before_reraising_base_exception(
    monkeypatch,
    tmp_path,
):
    class CloseReportedControlFlow(BaseException):
        pass

    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    _write_v2_lease(manager, identity)
    temporary_name = (
        f"{obs_process_module.OBS_PROCESS_LEASE_TEMP_PREFIX}{'c' * 32}"
    )
    temporary_path = manager.obs_dir / temporary_name
    temporary_path.write_bytes(b"orphaned temporary lease")
    real_open_file = obs_process_module._OBSDirectoryLease.open_file
    real_close = obs_process_module.os.close
    temporary_descriptor = None
    close_count = 0

    def capture_open_file(directory, name, **kwargs):
        nonlocal temporary_descriptor
        descriptor = real_open_file(directory, name, **kwargs)
        if name == temporary_name and temporary_descriptor is None:
            temporary_descriptor = descriptor
        return descriptor

    def close_then_report(descriptor):
        nonlocal close_count
        real_close(descriptor)
        if descriptor == temporary_descriptor:
            close_count += 1
            raise CloseReportedControlFlow("simulated orphan close report")

    with monkeypatch.context() as patch:
        patch.setattr(
            obs_process_module._OBSDirectoryLease,
            "open_file",
            capture_open_file,
        )
        patch.setattr(obs_process_module.os, "close", close_then_report)
        with pytest.raises(CloseReportedControlFlow, match="orphan close report"):
            manager.read_process_lease()

    assert close_count == 1
    assert not temporary_path.exists()
    assert manager.lease_path.exists()
    assert manager.read_process_lease() is not None


def test_committed_lease_write_does_not_report_late_lock_release_failure(
    monkeypatch,
    tmp_path,
):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    identity = _identity(manager)
    process = SimpleNamespace(pid=identity.pid)
    monkeypatch.setattr(manager, "query_popen_process_identity", lambda candidate: identity)
    real_release = obs_process_module._OBSInterProcessLock.release

    def release_then_fail(lock):
        real_release(lock)
        raise OSError("simulated post-commit release failure")

    monkeypatch.setattr(
        obs_process_module._OBSInterProcessLock,
        "release",
        release_then_fail,
    )

    manager.write_process_lease(process)

    payload = json.loads(manager.lease_path.read_text(encoding="utf-8"))
    assert payload["pid"] == identity.pid
    assert process._lol_replay_obs_process_lease.pid == identity.pid


def test_process_manager_reads_latest_portable_mode_log(tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    logs_dir = manager.obs_dir / "config" / "obs-studio" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    old_log = logs_dir / "2026-01-01 00-00-00.txt"
    new_log = logs_dir / "2026-01-01 00-00-01.txt"
    old_log.write_text("Portable mode: false\n", encoding="utf-8")
    new_log.write_text("Portable mode: true\n", encoding="utf-8")
    os.utime(old_log, (1, 1))
    os.utime(new_log, (2, 2))

    assert manager.latest_log_portable_mode() is True


def test_process_manager_reads_available_encoder_kinds_from_latest_log(tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    logs_dir = manager.obs_dir / "config" / "obs-studio" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "latest.txt").write_text(
        "12:00:00.001: Available Encoders:\n"
        "12:00:00.001:   Video Encoders:\n"
        "12:00:00.001: \t- obs_nvenc_hevc_tex (NVIDIA NVENC HEVC)\n"
        "12:00:00.001: \t- obs_nvenc_h264_tex (NVIDIA NVENC H.264)\n"
        "12:00:00.001: \t- obs_x264 (x264)\n"
        "12:00:00.001:   Audio Encoders:\n"
        "12:00:00.001: \t- ffmpeg_aac (FFmpeg AAC)\n"
        "12:00:00.001: Selected encoder: obs_nvenc_h264_tex\n",
        encoding="utf-8",
    )

    assert manager.latest_log_encoder_kinds() == [
        "obs_nvenc_hevc_tex",
        "obs_nvenc_h264_tex",
        "obs_x264",
    ]


def test_process_manager_reads_recording_diagnostics_after_timestamp(tmp_path):
    manager = OBSProcessManager(tmp_path / "obs-portable")
    logs_dir = manager.obs_dir / "config" / "obs-studio" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "2026-06-20 14-25-44.txt"
    log_path.write_text(
        "14:25:48.901: Available Encoders:\n"
        "14:25:48.901: \t- obs_nvenc_h264_tex (NVIDIA NVENC H.264)\n"
        "14:30:12.000: ==== Recording Start ===============================================\n"
        "14:30:13.000: [jim-nvenc: 'simple_video_recording'] failed to start\n",
        encoding="utf-8",
    )

    diagnostics = manager.latest_log_recording_diagnostics(
        since=datetime(2026, 6, 20, 14, 30, 11).timestamp()
    )

    assert diagnostics == [
        "14:30:12.000: ==== Recording Start ===============================================",
        "14:30:13.000: [jim-nvenc: 'simple_video_recording'] failed to start",
    ]


def test_process_manager_reports_unmanaged_obs_process(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/unmanaged_obs_detect").resolve())
    other_exe = Path("C:/Program Files/obs-studio/bin/64bit/obs64.exe")
    monkeypatch.setattr(
        manager,
        "list_obs_processes",
        lambda: [
            OBSProcessInfo(pid=100, executable_path=manager.obs_exe, creation_time=10.0),
            OBSProcessInfo(pid=200, executable_path=other_exe, creation_time=20.0),
        ],
    )

    unmanaged = manager.unmanaged_processes()

    assert [process.pid for process in unmanaged] == [200]


def test_hide_main_windows_noops_outside_windows(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/hide_obs_non_windows").resolve())
    monkeypatch.setattr("src.obs_process.os.name", "posix")

    assert manager.hide_main_windows(SimpleNamespace(pid=1234), timeout_sec=0) == 0


def test_windows_process_queries_run_hidden(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/hidden_obs_query").resolve())
    calls = []

    class FakeStartupInfo:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = None

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="Node,CreationDate,ExecutablePath,ProcessId\nhost,,C:/obs/bin/64bit/obs64.exe,123\n",
        )

    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr("src.obs_process.subprocess.STARTUPINFO", FakeStartupInfo, raising=False)
    monkeypatch.setattr("src.obs_process.subprocess.STARTF_USESHOWWINDOW", 1, raising=False)
    monkeypatch.setattr("src.obs_process.subprocess.CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr("src.obs_process.subprocess.run", fake_run)

    processes = manager._list_obs_processes_windows()

    assert [process.pid for process in processes] == [123]
    assert len(calls) == 1
    _command, kwargs = calls[0]
    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["startupinfo"].dwFlags & 1
    assert kwargs["startupinfo"].wShowWindow == 0
    assert kwargs["timeout"] == obs_process_module._OBS_PROCESS_QUERY_COMMAND_TIMEOUT_SEC


def test_wmic_timeout_falls_back_to_bounded_powershell_query(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/wmic_query_timeout").resolve())
    calls = []
    timeout_error = subprocess.TimeoutExpired(
        "wmic",
        obs_process_module._OBS_PROCESS_QUERY_COMMAND_TIMEOUT_SEC,
    )

    def run_query(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "wmic":
            raise timeout_error
        assert command[0] == "powershell"
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"ProcessId":456,"ExecutablePath":'
                '"C:/obs/bin/64bit/obs64.exe","CreationDate":null}'
            ),
        )

    monkeypatch.setattr(manager, "_run_hidden", run_query)

    processes = manager._list_obs_processes_windows()

    assert [process.pid for process in processes] == [456]
    assert [command[0] for command, _kwargs in calls] == ["wmic", "powershell"]
    assert all(
        kwargs["timeout"]
        == obs_process_module._OBS_PROCESS_QUERY_COMMAND_TIMEOUT_SEC
        for _command, kwargs in calls
    )


def test_strict_process_query_distinguishes_successful_empty_result(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_empty").resolve())
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=""),
    )

    snapshot = manager.query_obs_processes_strict()

    assert snapshot.processes == ()
    assert snapshot.queried_at > 0


def test_strict_process_query_returns_complete_process_snapshot(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_process").resolve())
    handle_identity = OBSProcessInfo(
        pid=123,
        executable_path=Path("C:/obs/bin/64bit/obs64.exe"),
        creation_time=123.5,
        creation_time_filetime=116444737235000000,
    )
    closed = []
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"ProcessId":123,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        ),
    )
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-123")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: handle_identity,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: False,
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    snapshot = manager.query_obs_processes_strict()

    assert snapshot.processes == (
        handle_identity,
    )
    assert closed == ["handle-123"]


@pytest.mark.parametrize("failure", ["open", "path", "filetime", "close"])
def test_strict_process_query_fails_closed_when_row_handle_binding_fails(
    monkeypatch,
    failure,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_binding").resolve())
    closed = []
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"ProcessId":123,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        ),
    )

    def open_handle(pid):
        if failure == "open":
            raise OSError("OpenProcess failed")
        return "handle-123"

    executable = (
        Path("C:/other/obs64.exe")
        if failure == "path"
        else Path("C:/obs/bin/64bit/obs64.exe")
    )
    identity = OBSProcessInfo(
        123,
        executable,
        123.5,
        None if failure == "filetime" else 116444737235000000,
    )

    def close_handle(handle):
        closed.append(handle)
        if failure == "close":
            raise OSError("CloseHandle failed")

    monkeypatch.setattr(manager, "_open_process_identity_handle", open_handle)
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: identity,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: False,
    )
    monkeypatch.setattr(manager, "_close_process_identity_handle", close_handle)

    with pytest.raises((OSError, OBSProcessQueryError)):
        manager.query_obs_processes_strict()

    assert closed == ([] if failure == "open" else ["handle-123"])


def test_strict_process_query_filters_cim_row_whose_handle_already_exited(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_zombie").resolve())
    identity = OBSProcessInfo(
        123,
        Path("C:/obs/bin/64bit/obs64.exe"),
        123.5,
        116444737235000000,
    )
    closed = []
    identity_queries = []
    wait_calls = []
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"ProcessId":123,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        ),
    )
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-123")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: identity_queries.append((handle, pid)) or identity,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: wait_calls.append((handle, timeout_ms)) or True,
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    snapshot = manager.query_obs_processes_strict()

    assert snapshot.processes == ()
    assert closed == ["handle-123"]
    assert identity_queries == []
    assert wait_calls == [("handle-123", 0)]


def test_strict_process_query_filters_row_that_exits_during_identity_query(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_exit_race").resolve())
    closed = []
    waits = iter((False, True))
    wait_calls = []
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"ProcessId":123,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        ),
    )
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-123")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: (_ for _ in ()).throw(
            OSError(5, "Access is denied after process exit")
        ),
    )
    def wait_handle(handle, timeout_ms):
        wait_calls.append((handle, timeout_ms))
        return next(waits)

    monkeypatch.setattr(manager, "_wait_process_identity_handle", wait_handle)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    snapshot = manager.query_obs_processes_strict()

    assert snapshot.processes == ()
    assert closed == ["handle-123"]
    assert wait_calls == [("handle-123", 0), ("handle-123", 0)]


@pytest.mark.parametrize(
    "query_error",
    (
        OSError(5, "Access is denied for live process"),
        OSError(22, "General identity query failure for live process"),
    ),
    ids=("access-denied", "general-os-error"),
)
def test_strict_process_query_does_not_ignore_os_error_for_live_handle(
    monkeypatch,
    query_error,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_live_denied").resolve())
    closed = []
    waits = iter((False, False))
    wait_calls = []
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout='{"ProcessId":123,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        ),
    )
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-123")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: (_ for _ in ()).throw(query_error),
    )

    def wait_handle(handle, timeout_ms):
        wait_calls.append((handle, timeout_ms))
        return next(waits)

    monkeypatch.setattr(manager, "_wait_process_identity_handle", wait_handle)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    with pytest.raises(OSError, match="live process") as raised:
        manager.query_obs_processes_strict()

    assert raised.value is query_error
    assert closed == ["handle-123"]
    assert wait_calls == [("handle-123", 0), ("handle-123", 0)]


def test_row_exit_race_still_fails_closed_when_handle_close_fails(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_race_close").resolve())
    executable = Path("C:/obs/bin/64bit/obs64.exe")
    waits = iter((False, True))
    wait_calls = []
    closed = []
    query_error = OSError(5, "identity query failed after exit")

    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-123")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: (_ for _ in ()).throw(query_error),
    )

    def wait_handle(handle, timeout_ms):
        wait_calls.append((handle, timeout_ms))
        return next(waits)

    def fail_close(handle):
        closed.append(handle)
        raise OSError("CloseHandle failed")

    monkeypatch.setattr(manager, "_wait_process_identity_handle", wait_handle)
    monkeypatch.setattr(manager, "_close_process_identity_handle", fail_close)

    with pytest.raises(
        OBSProcessQueryError,
        match="row identity handle could not be closed",
    ):
        manager._bind_strict_process_row_to_handle(123, executable)

    assert wait_calls == [("handle-123", 0), ("handle-123", 0)]
    assert closed == ["handle-123"]


@pytest.mark.parametrize("validation_failure", ("pid-mismatch", "invalid-identity"))
def test_row_non_os_validation_failure_is_not_suppressed_by_later_exit(
    monkeypatch,
    validation_failure,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_validation").resolve())
    executable = Path("C:/obs/bin/64bit/obs64.exe")
    wait_calls = []
    closed = []

    def query_identity(handle, pid):
        if validation_failure == "pid-mismatch":
            raise OBSProcessQueryError("Windows process handle PID mismatch")
        return OBSProcessInfo(
            pid=pid,
            executable_path=executable,
            creation_time=None,
            creation_time_filetime=116444737235000000,
        )

    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-123")
    monkeypatch.setattr(manager, "_query_process_identity_from_handle", query_identity)
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: wait_calls.append((handle, timeout_ms)) or False,
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    with pytest.raises(OBSProcessQueryError):
        manager._bind_strict_process_row_to_handle(123, executable)

    assert wait_calls == [("handle-123", 0)]
    assert closed == ["handle-123"]


def test_row_identity_query_wait_failure_keeps_query_error_as_context(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_wait_failure").resolve())
    executable = Path("C:/obs/bin/64bit/obs64.exe")
    query_error = OSError(5, "identity query failed")
    wait_error = OSError(6, "WaitForSingleObject failed")
    waits = iter((False, wait_error))
    wait_calls = []
    closed = []

    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-123")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: (_ for _ in ()).throw(query_error),
    )

    def wait_handle(handle, timeout_ms):
        wait_calls.append((handle, timeout_ms))
        outcome = next(waits)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(manager, "_wait_process_identity_handle", wait_handle)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    with pytest.raises(OSError, match="WaitForSingleObject failed") as raised:
        manager._bind_strict_process_row_to_handle(123, executable)

    assert raised.value is wait_error
    assert raised.value.__context__ is query_error
    assert wait_calls == [("handle-123", 0), ("handle-123", 0)]
    assert closed == ["handle-123"]


def test_strict_process_query_raises_when_query_fails(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_failure").resolve())
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    with pytest.raises(OBSProcessQueryError, match="exit code 1"):
        manager.query_obs_processes_strict()


def test_strict_process_query_rejects_stderr_only_cim_failure(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_stderr").resolve())
    commands = []
    monkeypatch.setattr("src.obs_process.os.name", "nt")

    def fail_on_stderr(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="Get-CimInstance: access denied",
        )

    monkeypatch.setattr(manager, "_run_hidden", fail_on_stderr)

    with pytest.raises(OBSProcessQueryError, match="stderr"):
        manager.query_obs_processes_strict()

    command, kwargs = commands[0]
    script = command[-1]
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "Get-CimInstance" in script and "-ErrorAction Stop" in script
    assert "Name='ProcessId'" in script and "[long]$_.ProcessId" in script
    assert "Get-Process" not in script
    assert kwargs["timeout"] == obs_process_module._OBS_PROCESS_QUERY_COMMAND_TIMEOUT_SEC


def test_strict_process_query_timeout_is_a_query_error(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_timeout").resolve())
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    timeout_error = subprocess.TimeoutExpired("powershell", 10)
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: (_ for _ in ()).throw(timeout_error),
    )

    with pytest.raises(OBSProcessQueryError, match="could not be started") as captured:
        manager.query_obs_processes_strict()

    assert captured.value.__cause__ is timeout_error


@pytest.mark.parametrize(
    "stdout",
    [
        "not-json",
        "null",
        '"unexpected"',
        '{"ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        '{"ProcessId":"invalid","ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        '{"ProcessId":true,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe"}',
        '{"ProcessId":123,"ExecutablePath":null,"CreationDate":"invalid"}',
        '{"ProcessId":123,"ExecutablePath":null,"CreationDate":"123.5"}',
        (
            '{"ProcessId":123,"ExecutablePath":"relative/obs64.exe",'
            '"CreationDate":"123.5"}'
        ),
        (
            '[{"ProcessId":123,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe",'
            '"CreationDate":"123.5"},'
            '{"ProcessId":123,"ExecutablePath":"C:/obs/bin/64bit/obs64.exe",'
            '"CreationDate":"123.5"}]'
        ),
    ],
)
def test_strict_process_query_raises_for_malformed_results(monkeypatch, stdout):
    manager = OBSProcessManager(Path("tests/_tmp/strict_obs_query_malformed").resolve())
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout),
    )

    with pytest.raises(OBSProcessQueryError):
        manager.query_obs_processes_strict()


def test_taskkill_runs_hidden_on_windows(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/hidden_taskkill").resolve())
    calls = []

    class FakeStartupInfo:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = None

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr("src.obs_process.subprocess.STARTUPINFO", FakeStartupInfo, raising=False)
    monkeypatch.setattr("src.obs_process.subprocess.STARTF_USESHOWWINDOW", 1, raising=False)
    monkeypatch.setattr("src.obs_process.subprocess.CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr("src.obs_process.subprocess.run", fake_run)

    assert manager._terminate_pid(123, force=True) is True

    command, kwargs = calls[0]
    assert command == ["taskkill", "/pid", "123", "/f"]
    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["startupinfo"].dwFlags & 1
    assert kwargs["timeout"] == (
        obs_process_module._OBS_PROCESS_TERMINATE_COMMAND_TIMEOUT_SEC
    )


def test_taskkill_nonzero_exit_is_not_reported_as_signaled(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/taskkill_failure").resolve())
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: SimpleNamespace(returncode=5),
    )

    assert manager._terminate_pid(123, force=False) is False


def test_taskkill_timeout_is_not_reported_as_signaled(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/taskkill_timeout").resolve())
    monkeypatch.setattr(
        manager,
        "_run_hidden",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("taskkill", 10)
        ),
    )

    assert manager._terminate_pid(123, force=False) is False


def test_strict_termination_signals_each_exact_identity(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_termination").resolve())
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    first = OBSProcessInfo(101, manager.obs_exe, 10.0)
    second = OBSProcessInfo(202, manager.obs_exe, 20.0)
    current = {first.pid: first, second.pid: second}
    query_clock = 100.0
    signals = []

    def query():
        nonlocal query_clock
        query_clock += 1.0
        return OBSProcessQuerySnapshot(
            processes=tuple(current.values()),
            queried_at=query_clock,
        )

    def terminate(pid, force):
        signals.append((pid, force))
        current.pop(pid)
        return True

    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(manager, "_terminate_pid", terminate)

    result = manager.terminate_expected_obs_processes_strict((first, second))

    assert result.signaled_processes == (first, second)
    assert result.after.processes == ()
    assert signals == [(101, False), (202, False)]


def test_strict_termination_rejects_same_pid_replacement_before_first_signal(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_termination_reuse").resolve())
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    expected = OBSProcessInfo(101, manager.obs_exe, 10.0)
    replacement = OBSProcessInfo(101, manager.obs_exe, 99.0)
    signals = []
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot((replacement,), 100.0),
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda pid, force: signals.append((pid, force)) or True,
    )

    with pytest.raises(OBSProcessQueryError, match="replaced|disappeared"):
        manager.terminate_expected_obs_processes_strict((expected,))

    assert signals == []


def test_strict_termination_rechecks_later_identity_before_each_signal(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/strict_termination_mid_reuse").resolve())
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    first = OBSProcessInfo(101, manager.obs_exe, 10.0)
    second = OBSProcessInfo(202, manager.obs_exe, 20.0)
    replacement = OBSProcessInfo(202, manager.obs_exe, 99.0)
    current = {first.pid: first, second.pid: second}
    signals = []

    def query():
        return OBSProcessQuerySnapshot(tuple(current.values()), 100.0)

    def terminate(pid, force):
        signals.append((pid, force))
        if pid == first.pid:
            current.pop(first.pid)
            current[second.pid] = replacement
        return True

    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(manager, "_terminate_pid", terminate)

    with pytest.raises(OBSProcessQueryError, match="replaced"):
        manager.terminate_expected_obs_processes_strict((first, second))

    assert signals == [(first.pid, False)]


def test_strict_termination_rejects_nonmanaged_expected_path_before_query(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_termination_path").resolve())
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    unexpected = OBSProcessInfo(
        101,
        Path("C:/Program Files/obs-studio/bin/64bit/obs64.exe"),
        10.0,
    )
    query_calls = 0

    def query():
        nonlocal query_calls
        query_calls += 1
        return OBSProcessQuerySnapshot((unexpected,), 100.0)

    monkeypatch.setattr(manager, "query_obs_processes_strict", query)

    with pytest.raises(OBSProcessQueryError, match="non-managed"):
        manager.terminate_expected_obs_processes_strict((unexpected,))

    assert query_calls == 0


def test_strict_termination_does_not_attribute_failed_signal_to_natural_exit(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/strict_termination_signal_failure").resolve())
    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: False)
    expected = OBSProcessInfo(101, manager.obs_exe, 10.0)
    current = {expected.pid: expected}

    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot(tuple(current.values()), 100.0),
    )

    def failed_signal(pid, force):
        current.pop(pid)
        return False

    monkeypatch.setattr(manager, "_terminate_pid", failed_signal)

    with pytest.raises(OBSProcessQueryError, match="signal failed"):
        manager.terminate_expected_obs_processes_strict((expected,))


def test_windows_handle_bound_termination_holds_identity_through_zero_snapshot(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_stop").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.1234, 116444746001234000)
    current = {expected.pid: expected}
    exited = {"handle-101": False}
    events = []
    query_clock = 100.0

    def query():
        nonlocal query_clock
        query_clock += 1.0
        events.append(("query", tuple(current)))
        return OBSProcessQuerySnapshot(tuple(current.values()), query_clock)

    def terminate(pid, force):
        events.append(("graceful", pid))
        current.pop(pid)
        exited[f"handle-{pid}"] = True
        return True

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(
        manager,
        "_open_process_identity_handle",
        lambda pid: events.append(("open", pid)) or f"handle-{pid}",
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: OBSProcessInfo(
            pid,
            Path("\\\\?\\" + str(manager.obs_exe)),
            1000.12349,
            116444746001234000,
        ),
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )
    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: events.append(("close", handle)),
    )

    result = manager.terminate_expected_obs_processes_strict((expected,))

    assert result.signaled_processes == (expected,)
    assert result.after.processes == ()
    assert events[-2:] == [("query", ()), ("close", "handle-101")]


def test_windows_handle_bound_termination_fails_closed_when_final_close_fails(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_close").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.1234, 116444746001234000)
    current = {expected.pid: expected}
    exited = {"handle-101": False}
    signals = []
    closed = []
    query_clock = 100.0

    def query():
        nonlocal query_clock
        query_clock += 1.0
        return OBSProcessQuerySnapshot(tuple(current.values()), query_clock)

    def terminate(pid, force):
        signals.append((pid, force))
        current.pop(pid)
        exited[f"handle-{pid}"] = True
        return True

    def fail_close(handle):
        closed.append(handle)
        raise OSError("CloseHandle failed")

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(
        manager,
        "_open_process_identity_handle",
        lambda pid: f"handle-{pid}",
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: expected,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )
    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(manager, "_close_process_identity_handle", fail_close)

    with pytest.raises(
        OBSProcessQueryError,
        match="identity handle could not be closed",
    ):
        manager.terminate_expected_obs_processes_strict((expected,))

    assert signals == [(101, False)]
    assert current == {}
    assert closed == ["handle-101"]


def test_windows_handle_close_failure_does_not_replace_termination_error(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_unwind").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.1234, 116444746001234000)
    closed = []
    critical_messages = []
    manager.logger = SimpleNamespace(
        critical=lambda message, *args: critical_messages.append(
            message % args if args else message
        )
    )

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot((expected,), 100.0),
    )
    monkeypatch.setattr(
        manager,
        "_open_process_identity_handle",
        lambda pid: f"handle-{pid}",
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: expected,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: False,
    )
    monkeypatch.setattr(manager, "_terminate_pid", lambda pid, force: False)

    def fail_close(handle):
        closed.append(handle)
        raise OSError("CloseHandle failed during unwind")

    monkeypatch.setattr(manager, "_close_process_identity_handle", fail_close)

    with pytest.raises(
        OBSProcessQueryError,
        match="termination signal failed",
    ) as raised:
        manager.terminate_expected_obs_processes_strict((expected,))

    assert closed == ["handle-101"]
    assert any(
        "CloseHandle also failed while unwinding strict OBS termination" in message
        for message in critical_messages
    )
    notes = getattr(raised.value, "__notes__", None)
    if notes is not None:
        assert any("CloseHandle also failed" in note for note in notes)


def test_windows_handle_bound_termination_requires_expected_filetime(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_filetime").resolve())
    incomplete = OBSProcessInfo(101, manager.obs_exe, 1000.1234)
    query_calls = 0

    def query():
        nonlocal query_calls
        query_calls += 1
        return OBSProcessQuerySnapshot((incomplete,), 100.0)

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(manager, "query_obs_processes_strict", query)

    with pytest.raises(OBSProcessQueryError, match="FILETIME"):
        manager.terminate_expected_obs_processes_strict((incomplete,))

    assert query_calls == 0


def test_popen_identity_uses_owned_windows_handle_without_closing_it(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/popen_handle_identity").resolve())
    identity = OBSProcessInfo(
        101,
        manager.obs_exe,
        1000.1234,
        116444746001234000,
    )
    process = SimpleNamespace(pid=101, _handle="popen-owned", poll=lambda: None)
    queried = []
    monkeypatch.setattr("src.obs_process.os.name", "nt")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: queried.append((handle, pid)) or identity,
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: pytest.fail("Popen owns this handle; it must not be closed here"),
    )

    assert manager.query_popen_process_identity(process) == identity
    assert queried == [("popen-owned", 101)]


@pytest.mark.parametrize("failure", ["open", "path", "creation", "natural-exit", "signal"])
def test_windows_handle_bound_termination_fails_closed_and_closes_handle(
    monkeypatch,
    failure,
):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_failure").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.1234, 116444746001234000)
    signals = []
    closed = []

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot((expected,), 100.0),
    )

    def open_handle(pid):
        if failure == "open":
            raise OSError("OpenProcess failed")
        return f"handle-{pid}"

    actual = expected
    if failure == "path":
        actual = OBSProcessInfo(
            expected.pid,
            Path("C:/other/obs64.exe"),
            expected.creation_time,
            expected.creation_time_filetime,
        )
    elif failure == "creation":
        actual = OBSProcessInfo(
            expected.pid,
            expected.executable_path,
            expected.creation_time,
            expected.creation_time_filetime + 1,
        )
    monkeypatch.setattr(manager, "_open_process_identity_handle", open_handle)
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: actual,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: failure == "natural-exit",
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda pid, force: signals.append((pid, force)) or failure != "signal",
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    with pytest.raises((OSError, OBSProcessQueryError)):
        manager.terminate_expected_obs_processes_strict((expected,))

    assert signals == ([] if failure in {"open", "path", "creation", "natural-exit"} else [(101, False)])
    assert closed == ([] if failure == "open" else ["handle-101"])


def test_windows_handle_detects_replacement_between_targets_before_signal(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_mid_reuse").resolve())
    first = OBSProcessInfo(101, manager.obs_exe, 1000.100, 116444746001000000)
    second = OBSProcessInfo(202, manager.obs_exe, 1000.200, 116444746002000000)
    replacement = OBSProcessInfo(202, manager.obs_exe, 1000.999, 116444746009990000)
    current = {first.pid: first, second.pid: second}
    exited = {"handle-101": False, "handle-202": False}
    opened = []
    closed = []
    signals = []

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot(tuple(current.values()), 100.0),
    )

    def open_handle(pid):
        opened.append(pid)
        return f"handle-{pid}"

    def query_handle(handle, pid):
        if pid == second.pid:
            return replacement
        return first

    def terminate(pid, force):
        signals.append((pid, force))
        current.pop(pid)
        exited[f"handle-{pid}"] = True
        return True

    monkeypatch.setattr(manager, "_open_process_identity_handle", open_handle)
    monkeypatch.setattr(manager, "_query_process_identity_from_handle", query_handle)
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )
    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    with pytest.raises(OBSProcessQueryError, match="handle identity"):
        manager.terminate_expected_obs_processes_strict((first, second))

    assert opened == [101, 202]
    assert signals == [(101, False)]
    assert closed == ["handle-202", "handle-101"]


def test_windows_handle_force_uses_validated_handle_instead_of_pid(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_force").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.123, 116444746001230000)
    current = {expected.pid: expected}
    exited = {"handle-101": False}
    graceful = []
    forced = []
    closed = []

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot(tuple(current.values()), 100.0),
    )
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: f"handle-{pid}")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: expected,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda pid, force: graceful.append((pid, force)) or True,
    )

    def force_handle(handle):
        forced.append(handle)
        exited[handle] = True
        current.clear()
        return True

    monkeypatch.setattr(manager, "_terminate_process_identity_handle", force_handle)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    result = manager.terminate_expected_obs_processes_strict(
        (expected,),
        timeout_sec=0,
    )

    assert result.after.processes == ()
    assert graceful == [(101, False)]
    assert forced == ["handle-101"]
    assert closed == ["handle-101"]


@pytest.mark.parametrize(
    "force_race",
    (
        "pre-query-exit",
        "query-exit",
        "live-os-error",
        "missing-signaled",
        "missing-live",
        "terminate-false-signaled",
        "terminate-false-live",
        "terminate-false-wait-failure",
        "identity-mismatch",
        "invalid-identity",
        "query-wait-failure",
    ),
)
def test_windows_handle_force_only_ignores_query_error_after_handle_exit(
    monkeypatch,
    force_race,
):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_force_race").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.123, 116444746001230000)
    current = {expected.pid: expected}
    exited = False
    query_calls = 0
    handle_query_calls = []
    wait_calls = []
    graceful = []
    forced = []
    closed = []
    query_error = OSError(5, "force identity query failed for live handle")
    wait_error = OSError(6, "force identity wait failed")
    terminate_wait_error = OSError(6, "post-TerminateProcess wait failed")
    replacement = OBSProcessInfo(
        expected.pid,
        expected.executable_path,
        expected.creation_time,
        expected.creation_time_filetime + 1,
    )

    def query():
        nonlocal exited, query_calls
        query_calls += 1
        if query_calls == 3 and force_race in {"missing-signaled", "missing-live"}:
            current.clear()
            exited = force_race == "missing-signaled"
        return OBSProcessQuerySnapshot(tuple(current.values()), 100.0 + query_calls)

    def query_handle(handle, pid):
        nonlocal exited
        handle_query_calls.append((handle, pid))
        if len(handle_query_calls) == 1:
            return expected
        if force_race == "query-exit":
            exited = True
            current.clear()
            raise OSError(5, "force identity query failed after exit")
        if force_race == "live-os-error":
            raise query_error
        if force_race == "query-wait-failure":
            raise query_error
        if force_race == "identity-mismatch":
            return replacement
        if force_race == "invalid-identity":
            return OBSProcessInfo(
                expected.pid,
                expected.executable_path,
                creation_time=None,
                creation_time_filetime=expected.creation_time_filetime,
            )
        if force_race.startswith("terminate-false-"):
            return expected
        raise AssertionError("pre-query exit must not query the force identity")

    def wait_handle(handle, timeout_ms):
        nonlocal exited
        wait_calls.append((handle, timeout_ms))
        if force_race == "pre-query-exit" and len(wait_calls) == 3:
            exited = True
            current.clear()
        if force_race == "query-wait-failure" and len(wait_calls) == 4:
            raise wait_error
        if force_race == "terminate-false-wait-failure" and len(wait_calls) == 5:
            raise terminate_wait_error
        return exited

    def force_handle(handle):
        nonlocal exited
        forced.append(handle)
        if force_race == "terminate-false-signaled":
            exited = True
            current.clear()
            return False
        if force_race in {"terminate-false-live", "terminate-false-wait-failure"}:
            return False
        return True

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-101")
    monkeypatch.setattr(manager, "_query_process_identity_from_handle", query_handle)
    monkeypatch.setattr(manager, "_wait_process_identity_handle", wait_handle)
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda pid, force: graceful.append((pid, force)) or True,
    )
    monkeypatch.setattr(
        manager,
        "_terminate_process_identity_handle",
        force_handle,
    )
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    failure_expectations = {
        "live-os-error": (OSError, "live handle"),
        "missing-live": (OBSProcessQueryError, "Live OBS handle is missing"),
        "terminate-false-live": (
            OBSProcessQueryError,
            "Handle-bound forced termination failed",
        ),
        "terminate-false-wait-failure": (
            OSError,
            "post-TerminateProcess wait failed",
        ),
        "identity-mismatch": (OBSProcessQueryError, "identity changed"),
        "invalid-identity": (OBSProcessQueryError, "identity changed"),
        "query-wait-failure": (OSError, "force identity wait failed"),
    }
    if force_race in failure_expectations:
        expected_error, expected_message = failure_expectations[force_race]
        with pytest.raises(expected_error, match=expected_message) as raised:
            manager.terminate_expected_obs_processes_strict(
                (expected,),
                timeout_sec=0,
            )
        if force_race == "live-os-error":
            assert raised.value is query_error
        elif force_race == "query-wait-failure":
            assert raised.value is wait_error
            assert raised.value.__context__ is query_error
        elif force_race == "terminate-false-wait-failure":
            assert raised.value is terminate_wait_error
    else:
        result = manager.terminate_expected_obs_processes_strict(
            (expected,),
            timeout_sec=0,
        )
        assert result.after.processes == ()

    assert graceful == [(101, False)]
    assert forced == (
        ["handle-101"]
        if force_race
        in {
            "terminate-false-signaled",
            "terminate-false-live",
            "terminate-false-wait-failure",
        }
        else []
    )
    assert closed == ["handle-101"]
    assert handle_query_calls == (
        [("handle-101", 101)]
        if force_race in {"pre-query-exit", "missing-signaled", "missing-live"}
        else [("handle-101", 101), ("handle-101", 101)]
    )
    expected_wait_count = {
        "pre-query-exit": 4,
        "query-exit": 5,
        "live-os-error": 4,
        "missing-signaled": 4,
        "missing-live": 3,
        "terminate-false-signaled": 6,
        "terminate-false-live": 5,
        "terminate-false-wait-failure": 5,
        "identity-mismatch": 3,
        "invalid-identity": 3,
        "query-wait-failure": 4,
    }[force_race]
    assert wait_calls == [("handle-101", 0)] * expected_wait_count


def test_windows_handle_force_continues_other_target_after_one_exits(
    monkeypatch,
):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_force_multi_race").resolve())
    first = OBSProcessInfo(101, manager.obs_exe, 1000.1, 116444746001000000)
    second = OBSProcessInfo(202, manager.obs_exe, 1000.2, 116444746002000000)
    current = {first.pid: first, second.pid: second}
    exited = {"handle-101": False, "handle-202": False}
    per_handle_waits = {"handle-101": 0, "handle-202": 0}
    query_clock = 100.0
    handle_queries = []
    graceful = []
    forced = []
    closed = []

    def query():
        nonlocal query_clock
        query_clock += 1.0
        return OBSProcessQuerySnapshot(tuple(current.values()), query_clock)

    def query_handle(handle, pid):
        handle_queries.append((handle, pid))
        return first if pid == first.pid else second

    def wait_handle(handle, timeout_ms):
        assert timeout_ms == 0
        per_handle_waits[handle] += 1
        if handle == "handle-101" and per_handle_waits[handle] == 3:
            exited[handle] = True
            current.pop(first.pid)
        return exited[handle]

    def force_handle(handle):
        forced.append(handle)
        exited[handle] = True
        current.pop(second.pid)
        return True

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(
        manager,
        "_open_process_identity_handle",
        lambda pid: f"handle-{pid}",
    )
    monkeypatch.setattr(manager, "_query_process_identity_from_handle", query_handle)
    monkeypatch.setattr(manager, "_wait_process_identity_handle", wait_handle)
    monkeypatch.setattr(
        manager,
        "_terminate_pid",
        lambda pid, force: graceful.append((pid, force)) or True,
    )
    monkeypatch.setattr(manager, "_terminate_process_identity_handle", force_handle)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    result = manager.terminate_expected_obs_processes_strict(
        (first, second),
        timeout_sec=0,
    )

    assert result.after.processes == ()
    assert graceful == [(101, False), (202, False)]
    assert forced == ["handle-202"]
    assert handle_queries == [
        ("handle-101", 101),
        ("handle-202", 202),
        ("handle-202", 202),
    ]
    assert per_handle_waits == {"handle-101": 4, "handle-202": 5}
    assert closed == ["handle-202", "handle-101"]


def test_windows_handle_requeries_one_transitional_exited_identity_row(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_zombie_transition").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.123, 116444746001230000)
    exited = {"handle-101": False}
    query_calls = 0
    closed = []

    def query():
        nonlocal query_calls
        query_calls += 1
        processes = (expected,) if query_calls <= 2 else ()
        return OBSProcessQuerySnapshot(processes, 100.0 + query_calls)

    def terminate(pid, force):
        exited["handle-101"] = True
        return True

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(manager, "query_obs_processes_strict", query)
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-101")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: expected,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )
    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    result = manager.terminate_expected_obs_processes_strict((expected,))

    assert query_calls == 3
    assert result.after.processes == ()
    assert closed == ["handle-101"]


def test_windows_handle_bounds_exited_row_requery_per_identity(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_zombie_multi").resolve())
    first = OBSProcessInfo(101, manager.obs_exe, 1000.1, 116444746001000000)
    second = OBSProcessInfo(202, manager.obs_exe, 1000.2, 116444746002000000)
    exited = {"handle-101": False, "handle-202": False}
    snapshots = iter(
        (
            (first, second),
            (first, second),
            (first,),
            (second,),
            (),
        )
    )
    closed = []

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot(next(snapshots), 100.0),
    )
    monkeypatch.setattr(
        manager,
        "_open_process_identity_handle",
        lambda pid: f"handle-{pid}",
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: first if pid == first.pid else second,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )

    def terminate(pid, force):
        exited[f"handle-{pid}"] = True
        return True

    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(
        manager,
        "_close_process_identity_handle",
        lambda handle: closed.append(handle),
    )

    result = manager.terminate_expected_obs_processes_strict((first, second))

    assert result.after.processes == ()
    assert closed == ["handle-202", "handle-101"]


def test_windows_handle_rejects_repeated_exited_identity_row(monkeypatch):
    manager = OBSProcessManager(Path("tests/_tmp/windows_handle_zombie_repeat").resolve())
    expected = OBSProcessInfo(101, manager.obs_exe, 1000.1, 116444746001000000)
    exited = {"handle-101": False}

    monkeypatch.setattr(manager, "_uses_windows_process_identity_handles", lambda: True)
    monkeypatch.setattr(
        manager,
        "query_obs_processes_strict",
        lambda: OBSProcessQuerySnapshot((expected,), 100.0),
    )
    monkeypatch.setattr(manager, "_open_process_identity_handle", lambda pid: "handle-101")
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: expected,
    )
    monkeypatch.setattr(
        manager,
        "_wait_process_identity_handle",
        lambda handle, timeout_ms: exited[handle],
    )

    def terminate(pid, force):
        exited["handle-101"] = True
        return True

    monkeypatch.setattr(manager, "_terminate_pid", terminate)
    monkeypatch.setattr(manager, "_close_process_identity_handle", lambda handle: None)

    with pytest.raises(OBSProcessQueryError, match="repeatedly retained"):
        manager.terminate_expected_obs_processes_strict((expected,))


def test_windows_creation_time_parser_preserves_fraction_and_timezone():
    value = "20260803123456.123456+540"
    expected = datetime(
        2026,
        8,
        3,
        12,
        34,
        56,
        123456,
        tzinfo=timezone(timedelta(hours=9)),
    ).timestamp()

    assert obs_process_module._parse_windows_process_creation_time(value) == expected
    assert obs_process_module._parse_windows_process_creation_time("/Date(1234567890123)/") == 1234567890.123
