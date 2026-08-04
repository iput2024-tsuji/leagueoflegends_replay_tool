import json
import multiprocessing
import os
from multiprocessing.connection import wait as wait_for_connections
from pathlib import Path

import pytest

from src import obs_process as obs_process_module
from src.obs_process import (
    OBS_PROCESS_LEASE_SCHEMA_VERSION,
    OBS_PROCESS_LEASE_TEMP_PREFIX,
    OBSProcessInfo,
    OBSProcessManager,
    OBSProcessQuerySnapshot,
)

pytestmark = pytest.mark.skipif(
    os.name != "nt",
    reason="Windows inter-process lease handle semantics",
)


def _lease_payload(obs_dir: str, *, pid: int, unix_seconds: float) -> bytes:
    manager = OBSProcessManager(obs_dir)
    filetime = int((unix_seconds + 11_644_473_600) * 10_000_000)
    return json.dumps(
        {
            "version": OBS_PROCESS_LEASE_SCHEMA_VERSION,
            "pid": pid,
            "executable_path": str(manager.obs_exe),
            "created_at": unix_seconds,
            "process_creation_time": unix_seconds,
            "process_creation_time_filetime": filetime,
        }
    ).encode("utf-8")


def _crash_while_holding_lease_lock(obs_dir: str, connection) -> None:
    manager = OBSProcessManager(obs_dir)
    context = manager._process_lease_transaction(mutating=True)
    transaction = context.__enter__()
    temporary_name = f"{OBS_PROCESS_LEASE_TEMP_PREFIX}{'c' * 32}"
    descriptor = transaction.root_lease.open_file(
        temporary_name,
        write=True,
        create_exclusive=True,
        delete=True,
        share_write=False,
        share_delete=False,
    )
    os.write(descriptor, b'{"partial":')
    os.fsync(descriptor)
    connection.send(("locked", temporary_name))
    if connection.recv() != "crash":
        raise RuntimeError("parent did not request the crash")
    os._exit(17)


def _hold_lease_transaction(obs_dir: str, connection) -> None:
    manager = OBSProcessManager(obs_dir)
    with manager._process_lease_transaction(mutating=True):
        connection.send("locked")
        if connection.recv() != "release":
            raise RuntimeError("parent did not release the transaction")
    connection.send("released")


def _read_after_observed_contention(obs_dir: str, connection) -> None:
    manager = OBSProcessManager(obs_dir)
    real_acquire = obs_process_module._OBSInterProcessLock.acquire
    attempts: list[bool] = []

    def reporting_acquire(lock, *args, **kwargs):
        acquired = real_acquire(lock, *args, **kwargs)
        attempts.append(acquired)
        connection.send(("attempt", len(attempts), acquired))
        return acquired

    obs_process_module._OBSInterProcessLock.acquire = reporting_acquire
    connection.send("started")
    lease = manager.read_process_lease()
    connection.send(("done", lease is None, tuple(attempts)))


def _write_lease_after_signal(path: str, payload: bytes, connection) -> None:
    connection.send("ready")
    if connection.recv() != "write":
        raise RuntimeError("parent did not authorize the replacement lease")
    Path(path).write_bytes(payload)
    connection.send("written")


def _start_obs_with_serialized_admission(
    obs_dir: str,
    worker_id: int,
    start_event,
    allow_publish,
    connection,
) -> None:
    manager = OBSProcessManager(obs_dir)
    unix_seconds = 100.0 + worker_id
    identity = OBSProcessInfo(
        pid=1000 + worker_id,
        executable_path=manager.obs_exe,
        creation_time=unix_seconds,
        creation_time_filetime=int(
            (unix_seconds + 11_644_473_600) * 10_000_000
        ),
    )
    real_acquire = obs_process_module._OBSInterProcessLock.acquire

    def reporting_acquire(lock, *args, **kwargs):
        acquired = real_acquire(lock, *args, **kwargs)
        connection.send(("attempt", worker_id, acquired))
        return acquired

    class FakePopen:
        pid = identity.pid

        def __init__(self) -> None:
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            connection.send(("signal", worker_id, "terminate"))
            self.alive = False

        def wait(self, timeout):
            return 0

        def kill(self):
            connection.send(("signal", worker_id, "kill"))
            self.alive = False

    def query():
        connection.send(("query", worker_id))
        return OBSProcessQuerySnapshot((), 100.0 + worker_id)

    def popen(*_args, **_kwargs):
        connection.send(("popen", worker_id))
        if not allow_publish.wait(timeout=15):
            raise TimeoutError("parent did not authorize lease publication")
        return FakePopen()

    obs_process_module._OBSInterProcessLock.acquire = reporting_acquire
    obs_process_module.subprocess.Popen = popen
    manager.query_obs_processes_strict = query
    manager.query_popen_process_identity = lambda candidate: identity
    connection.send(("ready", worker_id))
    if not start_event.wait(timeout=15):
        connection.send(("result", worker_id, "setup-timeout"))
        return
    try:
        process = manager.start_obs(hidden=False)
    except BaseException as exc:
        connection.send(
            ("result", worker_id, "error", type(exc).__name__, str(exc))
        )
    else:
        connection.send(("result", worker_id, "success", process.pid))


def _receive(connection, *, timeout: float = 15.0):
    assert connection.poll(timeout), "child did not send its bounded response"
    return connection.recv()


def _join_cleanly(process, *, expected_exitcode: int = 0) -> None:
    process.join(timeout=15)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("spawn child did not exit")
    assert process.exitcode == expected_exitcode


def test_windows_lease_lock_recovers_after_crash_and_collects_partial_temp(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_crash_while_holding_lease_lock,
        args=(str(obs_dir), child),
    )
    process.start()
    try:
        state, temporary_name = _receive(parent)
        assert state == "locked"
        assert (obs_dir / temporary_name).exists()
        parent.send("crash")
        _join_cleanly(process, expected_exitcode=17)
        assert (obs_dir / temporary_name).read_bytes() == b'{"partial":'

        manager = OBSProcessManager(obs_dir)
        assert manager.read_process_lease() is None
        assert manager.lease_lock_path.read_bytes() == b"\0"
        assert not (obs_dir / temporary_name).exists()
    finally:
        parent.close()
        child.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        process.close()


def test_windows_lease_operations_serialize_after_observed_contention(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    context = multiprocessing.get_context("spawn")
    holder_parent, holder_child = context.Pipe()
    reader_parent, reader_child = context.Pipe()
    holder = context.Process(
        target=_hold_lease_transaction,
        args=(str(obs_dir), holder_child),
    )
    reader = context.Process(
        target=_read_after_observed_contention,
        args=(str(obs_dir), reader_child),
    )
    holder.start()
    try:
        assert _receive(holder_parent) == "locked"
        reader.start()
        assert _receive(reader_parent) == "started"
        assert _receive(reader_parent) == ("attempt", 1, False)
        assert _receive(reader_parent) == ("attempt", 2, False)

        holder_parent.send("release")
        assert _receive(holder_parent) == "released"
        reader_messages = []
        while True:
            message = _receive(reader_parent)
            reader_messages.append(message)
            if message[0] == "done":
                break
        done = reader_messages[-1]
        assert done[1] is True
        attempt_results = done[2]
        assert len(attempt_results) >= 3
        assert attempt_results[:2] == (False, False)
        assert attempt_results[-1] is True
        assert any(message[0] == "attempt" and message[2] is True for message in reader_messages)
        _join_cleanly(holder)
        _join_cleanly(reader)
    finally:
        holder_parent.close()
        holder_child.close()
        reader_parent.close()
        reader_child.close()
        for process in (holder, reader):
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            process.close()


def test_windows_new_lease_created_after_pinned_close_is_preserved(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    manager = OBSProcessManager(obs_dir)
    obs_dir.mkdir(parents=True)
    manager.lease_path.write_bytes(
        _lease_payload(str(obs_dir), pid=100, unix_seconds=10.0)
    )
    replacement_payload = _lease_payload(
        str(obs_dir),
        pid=200,
        unix_seconds=20.0,
    )
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    writer = context.Process(
        target=_write_lease_after_signal,
        args=(str(manager.lease_path), replacement_payload, child),
    )
    writer.start()
    try:
        assert _receive(parent) == "ready"
        with manager._process_lease_transaction() as transaction:
            snapshot = manager._open_process_lease_snapshot_locked(transaction)
            assert snapshot is not None
            assert snapshot.lease.pid == 100
            manager._delete_process_lease_snapshot_locked(transaction, snapshot)
            parent.send("write")
            assert _receive(parent) == "written"
            assert manager.lease_path.read_bytes() == replacement_payload

        _join_cleanly(writer)
        replacement = manager.read_process_lease()
        assert replacement is not None
        assert replacement.pid == 200
        assert manager.lease_path.read_bytes() == replacement_payload
    finally:
        parent.close()
        child.close()
        if writer.is_alive():
            writer.terminate()
            writer.join(timeout=5)
        writer.close()


def test_windows_concurrent_starters_create_one_popen_and_one_lease(tmp_path):
    obs_dir = tmp_path / "obs-portable"
    manager = OBSProcessManager(obs_dir)
    manager.obs_exe.parent.mkdir(parents=True)
    manager.obs_exe.write_bytes(b"fake obs")
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    allow_publish = context.Event()
    pipes = [context.Pipe() for _ in range(2)]
    parents = [pair[0] for pair in pipes]
    children = [pair[1] for pair in pipes]
    processes = [
        context.Process(
            target=_start_obs_with_serialized_admission,
            args=(
                str(obs_dir),
                worker_id,
                start_event,
                allow_publish,
                children[worker_id],
            ),
        )
        for worker_id in range(2)
    ]
    messages = []
    results = []
    popen_workers = []
    try:
        for process in processes:
            process.start()
        ready_workers = set()
        while len(ready_workers) < 2:
            ready = wait_for_connections(parents, timeout=15)
            assert ready, "children did not report readiness"
            for connection in ready:
                message = connection.recv()
                messages.append(message)
                if message[0] == "ready":
                    ready_workers.add(message[1])

        start_event.set()
        contended_workers = set()
        while not popen_workers or not (
            contended_workers - set(popen_workers)
        ):
            ready = wait_for_connections(parents, timeout=15)
            assert ready, "starters did not reach causal lock contention"
            for connection in ready:
                message = connection.recv()
                messages.append(message)
                if message[0] == "popen":
                    popen_workers.append(message[1])
                elif message[0] == "attempt" and message[2] is False:
                    contended_workers.add(message[1])
            assert len(popen_workers) <= 1

        allow_publish.set()
        while len(results) < 2:
            ready = wait_for_connections(parents, timeout=15)
            assert ready, "starters did not report bounded results"
            for connection in ready:
                message = connection.recv()
                messages.append(message)
                if message[0] == "popen":
                    popen_workers.append(message[1])
                elif message[0] == "result":
                    results.append(message)

        for process in processes:
            _join_cleanly(process)

        successes = [message for message in results if message[2] == "success"]
        failures = [message for message in results if message[2] == "error"]
        assert len(successes) == 1
        assert len(failures) == 1
        assert failures[0][3] == "OBSProcessLeaseError"
        assert "既存のOBS所有情報" in failures[0][4]
        losing_worker = failures[0][1]
        assert any(
            message == ("attempt", losing_worker, True)
            for message in messages
        )
        assert popen_workers == [successes[0][1]]
        assert not any(message[0] == "signal" for message in messages)
        lease = manager.read_process_lease()
        assert lease is not None
        assert lease.schema_version == OBS_PROCESS_LEASE_SCHEMA_VERSION
        assert lease.pid == successes[0][3]
        assert tuple(obs_dir.glob(f"{OBS_PROCESS_LEASE_TEMP_PREFIX}*")) == ()
        assert manager.lease_lock_path.read_bytes() == b"\0"
    finally:
        start_event.set()
        allow_publish.set()
        for parent in parents:
            parent.close()
        for child in children:
            child.close()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            process.close()
