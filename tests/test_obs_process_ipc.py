import json
import multiprocessing
import os
from pathlib import Path

import pytest

from src import obs_process as obs_process_module
from src.obs_process import (
    OBS_PROCESS_LEASE_SCHEMA_VERSION,
    OBS_PROCESS_LEASE_TEMP_PREFIX,
    OBSProcessManager,
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
