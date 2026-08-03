from __future__ import annotations

import ctypes
import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src import obs_bootstrap

_CHILD_WAIT_SECONDS = 120
_PROCESS_START_TIMEOUT_SECONDS = 30


def _write_fake_obs(root: Path, contents: bytes = b"obs") -> Path:
    executable = root / "bin" / "64bit" / "obs64.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(contents)
    return executable


def _write_valid_prejournal_temporary(
    destination: Path,
    source: Path,
    *,
    owner_token: str = "a" * 32,
) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    source_fingerprint = obs_bootstrap._inventory_fingerprint(
        obs_bootstrap._build_obs_tree_inventory(source)
    )
    payload = {
        "schema_version": obs_bootstrap.OBS_COPY_JOURNAL_SCHEMA_VERSION,
        "source": str(source.resolve()),
        "source_fingerprint": source_fingerprint,
        "phase": obs_bootstrap.OBS_MIGRATION_PHASE_COPYING,
        "owner_pid": os.getpid(),
        "owner_token": owner_token,
        "started_at": 1.0,
    }
    temporary = obs_bootstrap._transaction_journal_temporary_path(marker, owner_token)
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    return temporary


def _noop_finalizer(_destination: Path) -> None:
    return


def _hold_migration_after_source_lock(
    source: str,
    destination: str,
    entered,
    release,
    result_queue,
) -> None:
    def hold(_source: Path) -> None:
        entered.set()
        if not release.wait(_CHILD_WAIT_SECONDS):
            raise TimeoutError("test did not release source lock")

    try:
        migrated = obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            prepare_source=hold,
        )
        result_queue.put(("ok", str(migrated)))
    except Exception as exc:  # pragma: no cover - surfaced to the parent process
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _interrupt_migration_at_boundary(
    source: str,
    destination: str,
    boundary: str,
    entered,
) -> None:
    """Pause a child only after the durable operation named by ``boundary``."""

    if boundary in {"journal_pre_rename", "copy_replace"}:
        original_replace = obs_bootstrap._OBSDirectoryLease.replace_open_file
        paused = False

        def pause_replace(directory, descriptor, temporary_name, target_name):
            nonlocal paused
            if (
                boundary == "journal_pre_rename"
                and not paused
                and target_name == obs_bootstrap.OBS_COPY_IN_PROGRESS_MARKER_NAME
            ):
                paused = True
                entered.set()
                time.sleep(_CHILD_WAIT_SECONDS)
            result = original_replace(
                directory,
                descriptor,
                temporary_name,
                target_name,
            )
            if boundary == "copy_replace" and not paused and target_name == "payload.bin":
                paused = True
                entered.set()
                time.sleep(_CHILD_WAIT_SECONDS)
            return result

        obs_bootstrap._OBSDirectoryLease.replace_open_file = pause_replace
    elif boundary == "phase_update":
        original_write_journal = obs_bootstrap._write_obs_migration_journal
        paused = False

        def pause_after_phase_update(*args, **kwargs):
            nonlocal paused
            result = original_write_journal(*args, **kwargs)
            if (
                not paused
                and kwargs.get("phase")
                == obs_bootstrap.OBS_MIGRATION_PHASE_FINALIZE_PENDING
            ):
                paused = True
                entered.set()
                time.sleep(_CHILD_WAIT_SECONDS)
            return result

        obs_bootstrap._write_obs_migration_journal = pause_after_phase_update
    elif boundary == "marker_unlink":
        original_remove_journal = obs_bootstrap._remove_obs_migration_journal_if_owned
        paused = False

        def pause_after_marker_unlink(*args, **kwargs):
            nonlocal paused
            result = original_remove_journal(*args, **kwargs)
            if not paused:
                paused = True
                entered.set()
                time.sleep(_CHILD_WAIT_SECONDS)
            return result

        obs_bootstrap._remove_obs_migration_journal_if_owned = pause_after_marker_unlink
    else:  # pragma: no cover - guarded by the parametrized test
        raise ValueError(f"unsupported interruption boundary: {boundary}")

    finalize_destination = _noop_finalizer if boundary == "phase_update" else None
    obs_bootstrap.migrate_legacy_obs_installation(
        destination,
        [source],
        finalize_destination=finalize_destination,
    )


def _kill_after_boundary(process, entered) -> int:
    assert entered.wait(_PROCESS_START_TIMEOUT_SECONDS), (
        f"migration did not reach interruption boundary; exitcode={process.exitcode}"
    )
    process.kill()
    process.join(15)
    if process.is_alive():  # pragma: no cover - defensive cleanup for a wedged child
        process.terminate()
        process.join(5)
    exitcode = process.exitcode
    process.close()
    assert exitcode is not None and exitcode != 0
    return exitcode


def _assert_directory_reopenable_from_subprocess(directory: Path) -> None:
    script = (
        "from pathlib import Path; import sys; "
        "root=Path(sys.argv[1]); probe=root/'reopen-probe.bin'; "
        "probe.write_bytes(b'ok'); assert probe.read_bytes() == b'ok'; probe.unlink()"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(directory)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _process_handle_count() -> int | None:
    if os.name == "nt":
        count = ctypes.c_ulong()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetProcessHandleCount.restype = ctypes.c_int
        process_handle = kernel32.GetCurrentProcess()
        if not kernel32.GetProcessHandleCount(process_handle, ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(count.value)
    descriptor_root = Path("/proc/self/fd")
    if descriptor_root.is_dir():
        return len(list(descriptor_root.iterdir()))
    return None


def test_same_source_lock_excludes_migration_to_a_different_destination(tmp_path):
    source = tmp_path / "legacy"
    first_destination = tmp_path / "obs-portable-a"
    second_destination = tmp_path / "obs-portable-b"
    source_executable = _write_fake_obs(source, b"shared-source")
    source_sentinel = source / "source-sentinel.txt"
    source_sentinel.write_text("keep source", encoding="utf-8")

    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_hold_migration_after_source_lock,
        args=(str(source), str(first_destination), entered, release, result_queue),
    )
    process.start()
    try:
        assert entered.wait(_PROCESS_START_TIMEOUT_SECONDS), "first migration did not acquire the source lock"
        with pytest.raises(obs_bootstrap.OBSMigrationInProgressError, match="移行元OBS"):
            obs_bootstrap.migrate_legacy_obs_installation(second_destination, [source])
        assert not obs_bootstrap.get_obs_copy_in_progress_marker(second_destination).exists()
        assert source_executable.read_bytes() == b"shared-source"
        assert source_sentinel.read_text(encoding="utf-8") == "keep source"
    finally:
        release.set()
        process.join(15)
        if process.is_alive():  # pragma: no cover - defensive cleanup
            process.terminate()
            process.join(5)

    assert process.exitcode == 0
    try:
        assert result_queue.get(timeout=5) == ("ok", str(source.resolve()))
    finally:
        result_queue.close()
        result_queue.join_thread()
        process.close()

    assert obs_bootstrap.migrate_legacy_obs_installation(second_destination, [source]) == source.resolve()
    assert (second_destination / "source-sentinel.txt").read_text(encoding="utf-8") == "keep source"


def test_valid_prejournal_recovery_preserves_temporary_while_source_lock_is_live(tmp_path):
    source = tmp_path / "legacy"
    active_destination = tmp_path / "obs-portable-active"
    recovering_destination = tmp_path / "obs-portable-recovering"
    _write_fake_obs(source, b"shared-source")
    source_sentinel = source / "source-sentinel.txt"
    source_sentinel.write_text("keep source", encoding="utf-8")
    temporary = _write_valid_prejournal_temporary(recovering_destination, source)
    temporary_before = temporary.read_bytes()

    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_hold_migration_after_source_lock,
        args=(str(source), str(active_destination), entered, release, result_queue),
    )
    process.start()
    try:
        assert entered.wait(_PROCESS_START_TIMEOUT_SECONDS), "first migration did not acquire the source lock"
        with pytest.raises(
            obs_bootstrap.OBSMigrationInProgressError,
            match="移行元",
        ):
            obs_bootstrap.migrate_legacy_obs_installation(
                recovering_destination,
                [source],
            )
        assert temporary.read_bytes() == temporary_before
        assert not obs_bootstrap.get_obs_copy_in_progress_marker(recovering_destination).exists()
        assert not (recovering_destination / "bin" / "64bit" / "obs64.exe").exists()
        assert not (recovering_destination / source_sentinel.name).exists()
    finally:
        release.set()
        process.join(15)
        if process.is_alive():  # pragma: no cover - defensive cleanup
            process.terminate()
            process.join(5)

    assert process.exitcode == 0
    try:
        assert result_queue.get(timeout=5) == ("ok", str(source.resolve()))
    finally:
        result_queue.close()
        result_queue.join_thread()
        process.close()

    assert (
        obs_bootstrap.migrate_legacy_obs_installation(recovering_destination, [source])
        == source.resolve()
    )
    assert not temporary.exists()
    assert (recovering_destination / source_sentinel.name).read_text(encoding="utf-8") == "keep source"


def test_stale_zero_byte_source_lock_is_reinitialized_and_migration_succeeds(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    source_lock = obs_bootstrap.get_obs_copy_lock_path(source)
    source_lock.write_bytes(b"")

    assert obs_bootstrap.migrate_legacy_obs_installation(destination, [source]) == source.resolve()

    assert source_lock.read_bytes() == b"\0"
    assert (destination / "bin" / "64bit" / "obs64.exe").read_bytes() == b"obs"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


def test_source_marker_injected_after_source_lock_is_rejected(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source)
    source_marker = obs_bootstrap.get_obs_copy_in_progress_marker(source)

    def inject_marker(_source: Path) -> None:
        source_marker.write_text("foreign transaction", encoding="utf-8")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="移行元にコピー中marker"):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            prepare_source=inject_marker,
        )

    assert source_marker.read_text(encoding="utf-8") == "foreign transaction"
    assert source_executable.read_bytes() == b"obs"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


@pytest.mark.parametrize(
    "pending_state",
    ["settings_marker", "settings_journal_temporary", "settings_data_temporary"],
)
def test_source_settings_state_injected_before_source_lock_is_rejected(
    monkeypatch,
    tmp_path,
    pending_state,
):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source)
    settings_marker = obs_bootstrap.get_obs_settings_transaction_marker(source)
    settings_journal_temporary = obs_bootstrap._transaction_write_temporary_path(
        settings_marker,
        "a" * 32,
    )
    settings_data_temporary = obs_bootstrap._transaction_write_temporary_path(
        source / "config" / "obs-studio" / "global.ini",
        "a" * 32,
    )
    artifacts = {
        "settings_marker": settings_marker,
        "settings_journal_temporary": settings_journal_temporary,
        "settings_data_temporary": settings_data_temporary,
    }
    artifact = artifacts[pending_state]
    source_lock_path = obs_bootstrap.get_obs_copy_lock_path(source)
    real_acquire = obs_bootstrap._OBSInterProcessLock.acquire
    injected = False

    def inject_pending_state_before_source_lock(lock, *args, **kwargs):
        nonlocal injected
        if not injected and lock.path == source_lock_path:
            injected = True
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("stale settings transaction", encoding="utf-8")
        return real_acquire(lock, *args, **kwargs)

    monkeypatch.setattr(
        obs_bootstrap._OBSInterProcessLock,
        "acquire",
        inject_pending_state_before_source_lock,
    )

    expected_error = (
        "起動前設定transaction marker"
        if pending_state == "settings_marker"
        else "transaction一時file"
    )
    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match=expected_error,
    ):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert injected is True
    assert artifact.read_text(encoding="utf-8") == "stale settings transaction"
    assert source_executable.read_bytes() == b"obs"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert not (destination / "bin" / "64bit" / "obs64.exe").exists()


@pytest.mark.parametrize(
    "boundary",
    ["journal_pre_rename", "copy_replace", "phase_update", "marker_unlink"],
)
def test_hard_kill_at_durable_boundary_is_recoverable(tmp_path, boundary):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    external = tmp_path / "external-sentinel.txt"
    _write_fake_obs(source, b"durable-obs")
    (source / "payload.bin").write_bytes(b"durable payload")
    source_sentinel = source / "source-sentinel.txt"
    source_sentinel.write_text("source remains", encoding="utf-8")
    external.write_text("outside remains", encoding="utf-8")

    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    process = context.Process(
        target=_interrupt_migration_at_boundary,
        args=(str(source), str(destination), boundary, entered),
    )
    process.start()
    _kill_after_boundary(process, entered)

    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    journal_temporaries = list(destination.glob(f"{marker.name}.*.tmp"))
    if boundary == "journal_pre_rename":
        assert not marker.exists()
        assert len(journal_temporaries) == 1
    elif boundary == "copy_replace":
        assert marker.exists()
        assert json.loads(marker.read_text(encoding="utf-8"))["phase"] == (
            obs_bootstrap.OBS_MIGRATION_PHASE_COPYING
        )
        assert (destination / "payload.bin").read_bytes() == b"durable payload"
    elif boundary == "phase_update":
        assert json.loads(marker.read_text(encoding="utf-8"))["phase"] == (
            obs_bootstrap.OBS_MIGRATION_PHASE_FINALIZE_PENDING
        )
    else:
        assert not marker.exists()

    finalize_destination = _noop_finalizer if boundary == "phase_update" else None
    result = obs_bootstrap.migrate_legacy_obs_installation(
        destination,
        [source],
        finalize_destination=finalize_destination,
    )

    if boundary == "marker_unlink":
        assert result is None
    else:
        assert result == source.resolve()
    assert (destination / "bin" / "64bit" / "obs64.exe").read_bytes() == b"durable-obs"
    assert (destination / "payload.bin").read_bytes() == b"durable payload"
    assert (destination / "source-sentinel.txt").read_text(encoding="utf-8") == "source remains"
    assert source_sentinel.read_text(encoding="utf-8") == "source remains"
    assert external.read_text(encoding="utf-8") == "outside remains"
    assert not marker.exists()
    assert not list(destination.rglob("*.tmp"))


@pytest.mark.parametrize("fault_target", ["config_writer", "journal_writer"])
def test_fault_cleanup_releases_parent_for_immediate_rename_and_subprocess_reopen(
    monkeypatch,
    tmp_path,
    fault_target,
):
    parent = tmp_path / "managed"
    renamed = tmp_path / "renamed-managed"
    parent.mkdir()

    with monkeypatch.context() as patch:
        if fault_target == "config_writer":
            target = parent / "config" / "obs-studio" / "global.ini"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"old")

            def fail_replace(_directory, _descriptor, _temporary_name, _target_name):
                raise OSError("simulated config replace fault")

            patch.setattr(
                obs_bootstrap._OBSDirectoryLease,
                "replace_open_file",
                fail_replace,
            )
            with obs_bootstrap._OBSDirectoryLease.open_absolute(
                parent,
                mutable=True,
            ) as root_lease:
                with pytest.raises(OSError, match="config replace fault"):
                    obs_bootstrap._write_safe_file_bytes_with_migration_lease(
                        target,
                        b"new",
                        root_lease,
                        expected_snapshot=None,
                    )
            assert target.read_bytes() == b"old"
        else:
            marker = obs_bootstrap.get_obs_copy_in_progress_marker(parent)
            source = tmp_path / "legacy"
            _write_fake_obs(source)

            def fail_journal_replace(_directory, _descriptor, _temporary_name, _target_name):
                raise OSError("simulated journal replace fault")

            patch.setattr(
                obs_bootstrap._OBSDirectoryLease,
                "replace_open_file",
                fail_journal_replace,
            )
            with pytest.raises(OSError, match="journal replace fault"):
                obs_bootstrap._write_obs_migration_journal(
                    marker,
                    source.resolve(),
                    "0" * 64,
                    "a" * 32,
                )
            assert not marker.exists()

    assert not list(parent.rglob("*.tmp"))
    parent.rename(renamed)
    _assert_directory_reopenable_from_subprocess(renamed)


def test_recursive_inventory_rejects_same_name_directory_to_file_swap(monkeypatch, tmp_path):
    root = tmp_path / "tree"
    nested = root / "nested"
    parked = root / "parked-nested"
    nested.mkdir(parents=True)
    (nested / "sentinel.bin").write_bytes(b"original")
    real_open_child = obs_bootstrap._OBSDirectoryLease.open_child_directory
    matching_calls = 0

    def swap_before_recursive_open(directory, name, *args, **kwargs):
        nonlocal matching_calls
        if directory.path == root.resolve() and name == "nested":
            matching_calls += 1
            if matching_calls == 2:
                nested.rename(parked)
                nested.write_bytes(b"attacker replacement")
        return real_open_child(directory, name, *args, **kwargs)

    monkeypatch.setattr(
        obs_bootstrap._OBSDirectoryLease,
        "open_child_directory",
        swap_before_recursive_open,
    )

    with pytest.raises((obs_bootstrap.OBSPathSafetyError, OSError)):
        obs_bootstrap._build_obs_tree_inventory(root)

    assert matching_calls >= 2
    assert nested.read_bytes() == b"attacker replacement"
    assert (parked / "sentinel.bin").read_bytes() == b"original"


def test_recursive_temporary_scanner_rejects_entry_added_between_enumerations(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "tree"
    nested = root / "nested"
    nested.mkdir(parents=True)
    target = nested / "data.bin"
    temporary = obs_bootstrap._transaction_copy_temporary_path(target, "a" * 32)
    temporary.write_bytes(b"stable temporary")
    injected = nested / "late.bin"
    real_open_file = obs_bootstrap._OBSDirectoryLease.open_file
    matching_calls = 0

    def inject_during_hash(directory, name, *args, **kwargs):
        nonlocal matching_calls
        descriptor = real_open_file(directory, name, *args, **kwargs)
        if directory.path == nested.resolve() and name == temporary.name:
            matching_calls += 1
            if matching_calls == 2:
                injected.write_bytes(b"late entry")
        return descriptor

    monkeypatch.setattr(obs_bootstrap._OBSDirectoryLease, "open_file", inject_during_hash)

    with obs_bootstrap._OBSDirectoryLease.open_absolute(root) as root_lease:
        with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="entryが変化"):
            obs_bootstrap._list_root_transaction_temporaries(root_lease)

    assert matching_calls >= 2
    assert temporary.read_bytes() == b"stable temporary"
    assert injected.read_bytes() == b"late entry"


def test_actual_migration_read_amplification_and_handles_are_bounded(
    monkeypatch,
    tmp_path,
    record_property,
):
    source = tmp_path / "large-legacy"
    destination = tmp_path / "large-managed"
    expected_files: dict[tuple[str, ...], int] = {}

    def write_relative(relative_parts: tuple[str, ...], payload: bytes) -> None:
        path = source.joinpath(*relative_parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        expected_files[relative_parts] = len(payload)

    write_relative(("bin", "64bit", "obs64.exe"), b"synthetic obs executable\n")
    small_file_count = 2996
    for index in range(small_file_count):
        if index % 3 == 0:
            relative_parts = ("config", f"root-{index:05d}.json")
        elif index % 3 == 1:
            relative_parts = (
                "config",
                "obs-studio",
                "basic",
                "profiles",
                f"profile-{index % 16:02d}",
                f"scene-{index:05d}.json",
            )
        else:
            relative_parts = (
                "obs-plugins",
                "64bit",
                "plugin_config",
                f"group-{index % 32:02d}",
                "presets",
                f"preset-{index:05d}.json",
            )
        payload = json.dumps(
            {
                "enabled": index % 2 == 0,
                "index": index,
                "name": f"scene-{index:05d}",
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        write_relative(relative_parts, payload)
    for index, size in enumerate((1024 * 1024, 1536 * 1024, 2 * 1024 * 1024)):
        write_relative(
            ("data", "cache", f"blob-{index}.bin"),
            bytes([0x41 + index]) * size,
        )

    expected_file_count = len(expected_files)
    expected_total_bytes = sum(expected_files.values())
    maximum_relative_depth = max(len(parts) for parts in expected_files)
    source_root = source.resolve()
    destination_root = destination.resolve()
    assert expected_file_count == 3000

    descriptor_origins: dict[int, str] = {}
    live_file_descriptors: set[int] = set()
    live_source_descriptors: set[int] = set()
    counters = {
        "source_open_file_calls": 0,
        "destination_open_file_calls": 0,
        "other_open_file_calls": 0,
        "source_read_calls": 0,
        "destination_read_calls": 0,
        "other_read_calls": 0,
        "source_read_bytes": 0,
        "destination_read_bytes": 0,
        "other_read_bytes": 0,
        "peak_live_file_descriptors": 0,
        "peak_live_source_descriptors": 0,
    }
    real_open_file = obs_bootstrap._OBSDirectoryLease.open_file
    real_open_child = obs_bootstrap._OBSDirectoryLease.open_child_directory
    real_read = obs_bootstrap.os.read
    real_close = obs_bootstrap.os.close
    handles_start = _process_handle_count()
    handles_peak = handles_start

    def classify(path: Path) -> str:
        try:
            path.relative_to(source_root)
            return "source"
        except ValueError:
            pass
        try:
            path.relative_to(destination_root)
            return "destination"
        except ValueError:
            return "other"

    def sample_process_handles() -> None:
        nonlocal handles_peak
        current = _process_handle_count()
        if current is not None and (handles_peak is None or current > handles_peak):
            handles_peak = current

    def track_open_file(directory, name, *args, **kwargs):
        descriptor = real_open_file(directory, name, *args, **kwargs)
        origin = classify(directory.path / name)
        descriptor_origins[descriptor] = origin
        live_file_descriptors.add(descriptor)
        counters[f"{origin}_open_file_calls"] += 1
        if origin == "source":
            live_source_descriptors.add(descriptor)
        file_peak_changed = len(live_file_descriptors) > counters["peak_live_file_descriptors"]
        source_peak_changed = (
            len(live_source_descriptors) > counters["peak_live_source_descriptors"]
        )
        counters["peak_live_file_descriptors"] = max(
            counters["peak_live_file_descriptors"],
            len(live_file_descriptors),
        )
        counters["peak_live_source_descriptors"] = max(
            counters["peak_live_source_descriptors"],
            len(live_source_descriptors),
        )
        if file_peak_changed or source_peak_changed:
            sample_process_handles()
        return descriptor

    def track_open_child(directory, name, *args, **kwargs):
        child = real_open_child(directory, name, *args, **kwargs)
        sample_process_handles()
        return child

    def track_read(descriptor, count):
        data = real_read(descriptor, count)
        origin = descriptor_origins.get(descriptor, "other")
        counters[f"{origin}_read_calls"] += 1
        counters[f"{origin}_read_bytes"] += len(data)
        return data

    def track_close(descriptor):
        origin = descriptor_origins.pop(descriptor, None)
        try:
            return real_close(descriptor)
        finally:
            live_file_descriptors.discard(descriptor)
            if origin == "source":
                live_source_descriptors.discard(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(obs_bootstrap._OBSDirectoryLease, "open_file", track_open_file)
        patch.setattr(
            obs_bootstrap._OBSDirectoryLease,
            "open_child_directory",
            track_open_child,
        )
        patch.setattr(obs_bootstrap.os, "read", track_read)
        patch.setattr(obs_bootstrap.os, "close", track_close)
        started = time.perf_counter()
        migrated = obs_bootstrap.migrate_legacy_obs_installation(destination, [source])
        elapsed = time.perf_counter() - started
        handles_end = _process_handle_count()

    assert migrated == source.resolve()
    source_inventory = obs_bootstrap._build_obs_tree_inventory(source)
    destination_inventory = obs_bootstrap._build_obs_tree_inventory(destination)
    assert obs_bootstrap._inventory_content_matches(
        destination_inventory,
        source_inventory,
    )
    source_files = [entry for entry in source_inventory if entry.kind == "file"]
    assert len(source_files) == expected_file_count
    assert sum(entry.size or 0 for entry in source_files) == expected_total_bytes
    assert max(len(entry.relative_parts) for entry in source_files) == maximum_relative_depth

    source_read_multiplier = counters["source_read_bytes"] / expected_total_bytes
    total_read_bytes = (
        counters["source_read_bytes"]
        + counters["destination_read_bytes"]
        + counters["other_read_bytes"]
    )
    peak_handle_growth = (
        None
        if handles_start is None or handles_peak is None
        else handles_peak - handles_start
    )
    ending_handle_growth = (
        None
        if handles_start is None or handles_end is None
        else handles_end - handles_start
    )
    metrics = {
        "files": expected_file_count,
        "bytes": expected_total_bytes,
        "maximum_relative_depth": maximum_relative_depth,
        "elapsed_seconds": elapsed,
        "elapsed_per_file": elapsed / expected_file_count,
        "elapsed_per_byte": elapsed / expected_total_bytes,
        "source_open_file_calls": counters["source_open_file_calls"],
        "destination_open_file_calls": counters["destination_open_file_calls"],
        "other_open_file_calls": counters["other_open_file_calls"],
        "source_read_calls": counters["source_read_calls"],
        "destination_read_calls": counters["destination_read_calls"],
        "other_read_calls": counters["other_read_calls"],
        "source_read_bytes": counters["source_read_bytes"],
        "destination_read_bytes": counters["destination_read_bytes"],
        "other_read_bytes": counters["other_read_bytes"],
        "total_read_bytes": total_read_bytes,
        "source_read_multiplier": source_read_multiplier,
        "peak_live_file_descriptors": counters["peak_live_file_descriptors"],
        "peak_live_source_descriptors": counters["peak_live_source_descriptors"],
        "process_handles_start": handles_start,
        "process_handles_peak": handles_peak,
        "process_handles_end": handles_end,
        "process_handle_peak_growth": peak_handle_growth,
        "process_handle_ending_growth": ending_handle_growth,
    }
    record_property("obs_migration_integration", json.dumps(metrics, sort_keys=True))

    # The current transaction hashes the source before and after copying and
    # reads it once for the copy. Keep that bounded without a flaky time limit.
    assert source_read_multiplier <= 3.0
    assert counters["source_read_bytes"] == expected_total_bytes * 3
    assert counters["destination_read_bytes"] >= expected_total_bytes
    if peak_handle_growth is not None:
        assert peak_handle_growth <= maximum_relative_depth + 24
    if ending_handle_growth is not None:
        assert ending_handle_growth <= 4
