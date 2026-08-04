import ctypes
import json
import os
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from src import obs_bootstrap, obs_process as obs_process_module, obs_transaction_fs
from src.obs_process import (
    OBSProcessInfo,
    OBSProcessLeaseError,
    OBSProcessManager,
    OBSProcessQuerySnapshot,
)

_POSIX_MOUNT_ID_AVAILABLE = (
    os.name != "nt" and Path("/proc/self/fdinfo").is_dir()
)


def _write_fake_obs(root: Path, payload: bytes = b"obs") -> Path:
    executable = root / "bin" / "64bit" / "obs64.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(payload)
    return executable


def _assert_no_transaction_artifacts(destination: Path) -> None:
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert not obs_bootstrap.get_obs_copy_lock_path(destination).exists()
    assert not destination.exists()


def _physical_identity(path: Path) -> tuple[int, int, int]:
    return obs_transaction_fs._file_identity(path.stat())


def _assert_runtime_alias_rejected_before_popen(
    monkeypatch,
    managed_root: Path,
    alias_executable: Path,
    *,
    supported: bool,
) -> None:
    manager = OBSProcessManager(managed_root)
    process = OBSProcessInfo(
        pid=10300,
        executable_path=alias_executable,
        creation_time=103.0,
        creation_time_filetime=116_444_737_030_000_000,
    )
    snapshot = OBSProcessQuerySnapshot((process,), 103.0)
    manager.lease_lock_path.write_bytes(b"\0")
    lock_raw = manager.lease_lock_path.read_bytes()
    lock_identity = _physical_identity(manager.lease_lock_path)
    managed_identity = _physical_identity(manager.obs_exe)
    alias_identity = _physical_identity(alias_executable)
    assert alias_identity == managed_identity
    if supported:
        with (
            obs_process_module._pin_obs_executable_identity(
                manager.obs_exe
            ) as managed_pin,
            obs_process_module._pin_obs_executable_identity(
                alias_executable
            ) as alias_pin,
        ):
            assert alias_pin.physical_identity == managed_pin.physical_identity

    with monkeypatch.context() as admission_patch:
        admission_patch.setattr(
            manager,
            "query_obs_processes_strict",
            lambda: snapshot,
        )
        admission_patch.setattr(
            obs_process_module.subprocess,
            "Popen",
            lambda *args, **kwargs: pytest.fail(
                "physical alias must prevent Popen"
            ),
        )
        admission_patch.setattr(
            manager,
            "_terminate_pid",
            lambda *args, **kwargs: pytest.fail(
                "admission must not signal a PID"
            ),
        )

        expected = r"PID 10300" if supported else "物理identity"
        with pytest.raises(OBSProcessLeaseError, match=expected):
            manager.start_obs(hidden=False)

    assert snapshot.processes == (process,)
    assert not manager.lease_path.exists()
    assert manager.lease_lock_path.read_bytes() == lock_raw
    assert _physical_identity(manager.lease_lock_path) == lock_identity
    assert _physical_identity(manager.obs_exe) == managed_identity
    assert _physical_identity(alias_executable) == alias_identity


def _assert_strict_row_alias_bound(
    monkeypatch,
    managed_root: Path,
    cim_path: Path,
    handle_path: Path,
) -> None:
    manager = OBSProcessManager(managed_root)
    identity = OBSProcessInfo(
        pid=10301,
        executable_path=handle_path,
        creation_time=103.01,
        creation_time_filetime=116_444_737_030_100_000,
    )
    wait_calls = []
    closed = []
    monkeypatch.setattr(
        manager,
        "_open_process_identity_handle",
        lambda pid: "handle-10301",
    )
    monkeypatch.setattr(
        manager,
        "_query_process_identity_from_handle",
        lambda handle, pid: identity,
    )
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

    bound = manager._bind_strict_process_row_to_handle(identity.pid, cim_path)

    assert bound is identity
    assert wait_calls == [("handle-10301", 0), ("handle-10301", 0)]
    assert closed == ["handle-10301"]


def _windows_short_path(path: Path) -> Path:
    if os.name != "nt":
        pytest.skip("Windows 8.3 aliasの実APIテスト")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_short_path = kernel32.GetShortPathNameW
    get_short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    get_short_path.restype = ctypes.c_ulong
    required = int(get_short_path(str(path), None, 0))
    if required == 0:
        pytest.skip("このvolumeでは8.3 short nameを取得できません")
    buffer = ctypes.create_unicode_buffer(required)
    written = int(get_short_path(str(path), buffer, required))
    if written == 0 or written >= required:
        pytest.skip("このvolumeでは8.3 short nameを安定して取得できません")
    short_path = Path(buffer.value)
    if os.path.normcase(str(short_path)) == os.path.normcase(str(path)):
        pytest.skip("このdirectoryには別表記の8.3 short nameがありません")
    return short_path


def _windows_volume_guid_path(path: Path) -> Path:
    if os.name != "nt":
        pytest.skip("Windows volume GUID aliasの実APIテスト")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_volume_name = kernel32.GetVolumeNameForVolumeMountPointW
    get_volume_name.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_ulong]
    get_volume_name.restype = ctypes.c_int
    volume_name = ctypes.create_unicode_buffer(1024)
    if not get_volume_name(path.anchor, volume_name, len(volume_name)):
        pytest.skip(
            "volume GUID pathを取得できません: "
            f"WinError {ctypes.get_last_error()}"
        )
    relative = path.resolve().relative_to(Path(path.anchor).resolve())
    return Path(volume_name.value).joinpath(relative)


@contextmanager
def _windows_subst_root(target: Path):
    if os.name != "nt":
        pytest.skip("Windows SUBST aliasの実APIテスト")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetLogicalDrives.argtypes = []
    kernel32.GetLogicalDrives.restype = ctypes.c_ulong
    used_mask = int(kernel32.GetLogicalDrives())
    drive_letter = next(
        (
            letter
            for letter in reversed("DEFGHIJKLMNOPQRSTUVWXYZ")
            if not used_mask & (1 << (ord(letter) - ord("A")))
        ),
        None,
    )
    if drive_letter is None:
        pytest.skip("SUBSTに利用できるdrive letterがありません")
    drive = f"{drive_letter}:"
    completed = subprocess.run(
        ["subst", drive, str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(completed.stderr or completed.stdout or "subst failed")
    try:
        yield Path(f"{drive}\\")
    finally:
        subprocess.run(
            ["subst", drive, "/D"],
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 runtime aliasの実APIテスト")
def test_windows_runtime_admission_rejects_short_name_physical_alias(
    monkeypatch,
    tmp_path,
):
    managed_root = tmp_path / "managed-runtime-installation-long-name"
    executable = _write_fake_obs(managed_root, b"runtime-short-name")
    alias = _windows_short_path(executable)

    _assert_strict_row_alias_bound(
        monkeypatch,
        managed_root,
        executable,
        alias,
    )
    _assert_runtime_alias_rejected_before_popen(
        monkeypatch,
        managed_root,
        alias,
        supported=True,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows volume GUID runtime aliasの実APIテスト")
def test_windows_runtime_admission_rejects_volume_guid_physical_alias(
    monkeypatch,
    tmp_path,
):
    managed_root = tmp_path / "managed-runtime-volume-guid"
    executable = _write_fake_obs(managed_root, b"runtime-volume-guid")
    alias = _windows_volume_guid_path(executable)

    _assert_strict_row_alias_bound(
        monkeypatch,
        managed_root,
        executable,
        alias,
    )
    _assert_runtime_alias_rejected_before_popen(
        monkeypatch,
        managed_root,
        alias,
        supported=True,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows SUBST runtime aliasの実APIテスト")
def test_windows_runtime_admission_fails_closed_for_subst_alias(
    monkeypatch,
    tmp_path,
):
    managed_root = tmp_path / "managed-runtime-subst"
    executable = _write_fake_obs(managed_root, b"runtime-subst")
    with _windows_subst_root(tmp_path) as alias_root:
        alias = alias_root / executable.relative_to(tmp_path)
        _assert_runtime_alias_rejected_before_popen(
            monkeypatch,
            managed_root,
            alias,
            supported=False,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction runtime aliasの実APIテスト")
def test_windows_runtime_admission_fails_closed_for_junction_alias(
    monkeypatch,
    tmp_path,
):
    managed_root = tmp_path / "managed-runtime-junction"
    executable = _write_fake_obs(managed_root, b"runtime-junction")
    alias_root = tmp_path / "runtime-junction-alias"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias_root), str(managed_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(completed.stderr or completed.stdout or "mklink /J failed")
    alias = alias_root / executable.relative_to(managed_root)
    try:
        _assert_runtime_alias_rejected_before_popen(
            monkeypatch,
            managed_root,
            alias,
            supported=False,
        )
    finally:
        os.rmdir(alias_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 aliasの実APIテスト")
def test_windows_short_name_source_ancestor_is_rejected_before_artifacts(tmp_path):
    source = tmp_path / "legacy-observation-installation-long-name"
    executable = _write_fake_obs(source, b"keep")
    destination = _windows_short_path(source) / "nested-destination"

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="physical directory|ancestor alias",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert executable.read_bytes() == b"keep"
    _assert_no_transaction_artifacts(destination)


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 aliasの実APIテスト")
def test_windows_short_name_destination_ancestor_is_rejected_before_artifacts(tmp_path):
    destination = tmp_path / "destination-container-long-name"
    destination.mkdir()
    short_destination = _windows_short_path(destination)
    source = short_destination / "legacy-child"
    executable = _write_fake_obs(source, b"keep")

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="physical directory|ancestor alias",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert executable.read_bytes() == b"keep"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert not obs_bootstrap.get_obs_copy_lock_path(destination).exists()


def _assert_windows_alias_rejects_both_ancestor_directions(
    tmp_path: Path,
    alias_root: Path,
) -> None:
    source = tmp_path / "source-ancestor"
    executable = _write_fake_obs(source, b"source-keep")
    alias_source = alias_root / source.relative_to(tmp_path)
    nested_destination = alias_source / "nested-destination"

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="physical directory|ancestor alias",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(
            nested_destination,
            [source],
        )

    assert executable.read_bytes() == b"source-keep"
    _assert_no_transaction_artifacts(nested_destination)

    destination = tmp_path / "destination-ancestor"
    destination.mkdir()
    alias_destination = alias_root / destination.relative_to(tmp_path)
    nested_source = alias_destination / "nested-source"
    nested_executable = _write_fake_obs(nested_source, b"nested-keep")

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="physical directory|ancestor alias",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [nested_source],
        )

    assert nested_executable.read_bytes() == b"nested-keep"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert not obs_bootstrap.get_obs_copy_lock_path(destination).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows SUBST aliasの実APIテスト")
def test_windows_subst_alias_rejects_both_ancestor_directions(tmp_path):
    with _windows_subst_root(tmp_path) as alias_root:
        _assert_windows_alias_rejects_both_ancestor_directions(
            tmp_path,
            alias_root,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows volume GUID aliasの実APIテスト")
def test_windows_volume_guid_alias_rejects_both_ancestor_directions(tmp_path):
    _assert_windows_alias_rejects_both_ancestor_directions(
        tmp_path,
        _windows_volume_guid_path(tmp_path),
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 aliasの実APIテスト")
def test_stale_journal_alias_is_rejected_before_lock_creation(tmp_path):
    source = tmp_path / "legacy-stale-journal-long-name"
    executable = _write_fake_obs(source, b"keep")
    destination = _windows_short_path(source) / "nested-destination"
    destination.mkdir()
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    marker.write_text(
        json.dumps(
            {
                "schema_version": obs_bootstrap.OBS_COPY_JOURNAL_SCHEMA_VERSION,
                "source": str(source.resolve()),
                "source_fingerprint": "0" * 64,
                "phase": obs_bootstrap.OBS_MIGRATION_PHASE_COPYING,
                "owner_pid": os.getpid(),
                "owner_token": "a" * 32,
                "started_at": 1.0,
            }
        ),
        encoding="utf-8",
    )
    lock = obs_bootstrap.get_obs_copy_lock_path(destination)

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="physical directory|ancestor alias",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert executable.read_bytes() == b"keep"
    assert marker.exists()
    assert not lock.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 aliasの実APIテスト")
def test_stale_journal_checks_every_valid_allowed_alias_before_lock(tmp_path):
    source_a = tmp_path / "journal-source-a"
    source_b = tmp_path / "legacy-candidate-b-long-name"
    executable_a = _write_fake_obs(source_a, b"source-a")
    executable_b = _write_fake_obs(source_b, b"source-b")
    destination = source_b / "nested-destination"
    destination.mkdir()
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    marker.write_text(
        json.dumps(
            {
                "schema_version": obs_bootstrap.OBS_COPY_JOURNAL_SCHEMA_VERSION,
                "source": str(source_a.resolve()),
                "source_fingerprint": "0" * 64,
                "phase": obs_bootstrap.OBS_MIGRATION_PHASE_COPYING,
                "owner_pid": os.getpid(),
                "owner_token": "c" * 32,
                "started_at": 1.0,
            }
        ),
        encoding="utf-8",
    )
    lock = obs_bootstrap.get_obs_copy_lock_path(destination)
    source_b_alias = _windows_short_path(source_b)

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="physical directory|ancestor alias",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source_a, source_b_alias],
        )

    assert executable_a.read_bytes() == b"source-a"
    assert executable_b.read_bytes() == b"source-b"
    assert marker.exists()
    assert not lock.exists()
    assert not (destination / executable_a.relative_to(source_a)).exists()


def test_stale_journal_rejects_temporary_in_other_valid_candidate_before_lock(
    tmp_path,
):
    source_a = tmp_path / "journal-source-a"
    source_b = tmp_path / "candidate-b"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source_a, b"source-a")
    _write_fake_obs(source_b, b"source-b")
    temporary_target = source_b / "config" / "obs-studio" / "global.ini"
    temporary_target.parent.mkdir(parents=True)
    temporary = obs_bootstrap._transaction_copy_temporary_path(
        temporary_target,
        "d" * 32,
    )
    temporary.write_bytes(b"preserve")
    destination.mkdir()
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    marker.write_text(
        json.dumps(
            {
                "schema_version": obs_bootstrap.OBS_COPY_JOURNAL_SCHEMA_VERSION,
                "source": str(source_a.resolve()),
                "source_fingerprint": "0" * 64,
                "phase": obs_bootstrap.OBS_MIGRATION_PHASE_COPYING,
                "owner_pid": os.getpid(),
                "owner_token": "e" * 32,
                "started_at": 1.0,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="transaction一時file",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source_a, source_b],
        )

    assert temporary.read_bytes() == b"preserve"
    assert marker.exists()
    assert not obs_bootstrap.get_obs_copy_lock_path(destination).exists()


def test_marker_appearing_during_lock_acquire_requires_retry_without_copy(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    executable = _write_fake_obs(source, b"keep")
    destination.mkdir()
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    lock_path = obs_bootstrap.get_obs_copy_lock_path(destination)
    real_acquire = obs_bootstrap._OBSInterProcessLock.acquire
    injected = False

    def acquire_with_marker(lock, *args, **kwargs):
        nonlocal injected
        if lock.path == lock_path and not injected:
            marker.write_text(
                json.dumps(
                    {
                        "schema_version": obs_bootstrap.OBS_COPY_JOURNAL_SCHEMA_VERSION,
                        "source": str(source.resolve()),
                        "source_fingerprint": "0" * 64,
                        "phase": obs_bootstrap.OBS_MIGRATION_PHASE_COPYING,
                        "owner_pid": os.getpid(),
                        "owner_token": "b" * 32,
                        "started_at": 1.0,
                    }
                ),
                encoding="utf-8",
            )
            injected = True
        return real_acquire(lock, *args, **kwargs)

    monkeypatch.setattr(
        obs_bootstrap._OBSInterProcessLock,
        "acquire",
        acquire_with_marker,
    )

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="新しいmarker.*再試行",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert injected is True
    assert executable.read_bytes() == b"keep"
    assert marker.exists()
    assert not (destination / executable.relative_to(source)).exists()


def test_directory_link_alias_is_rejected_before_external_write(tmp_path):
    source = tmp_path / "legacy"
    external = tmp_path / "external"
    alias = tmp_path / "alias"
    executable = _write_fake_obs(source, b"keep")
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    if os.name == "nt":
        import subprocess

        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(external)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.skip(completed.stderr or completed.stdout or "mklink /J failed")
    else:
        alias.symlink_to(external, target_is_directory=True)
    destination = alias / "obs-portable"

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="reparse point|symbolic link",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert executable.read_bytes() == b"keep"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (external / "obs-portable").exists()


@pytest.mark.skipif(
    not _POSIX_MOUNT_ID_AVAILABLE,
    reason="Linux /proc mount identityのCIテスト",
)
def test_posix_physical_identity_rejects_both_ancestor_directions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    nested_destination = source / "nested-destination"
    with obs_transaction_fs._OBSDirectoryLease.open_absolute(source) as source_lease:
        with pytest.raises(
            obs_transaction_fs.OBSPathSafetyError,
            match="physical directory|ancestor alias",
        ):
            obs_transaction_fs._validate_distinct_physical_directory_trees(
                source_lease,
                nested_destination,
                destination_lease=None,
            )

    destination = tmp_path / "destination"
    nested_source = destination / "nested-source"
    nested_source.mkdir(parents=True)
    with (
        obs_transaction_fs._OBSDirectoryLease.open_absolute(
            nested_source
        ) as source_lease,
        obs_transaction_fs._OBSDirectoryLease.open_absolute(
            destination
        ) as destination_lease,
    ):
        with pytest.raises(
            obs_transaction_fs.OBSPathSafetyError,
            match="physical directory|ancestor alias",
        ):
            obs_transaction_fs._validate_distinct_physical_directory_trees(
                source_lease,
                destination,
                destination_lease=destination_lease,
            )


def _simulate_posix_mount_transition(monkeypatch, target: Path) -> None:
    target_identity = obs_transaction_fs._file_identity(
        target.stat(follow_symlinks=False)
    )
    real_mount_id = obs_transaction_fs._posix_mount_id_for_descriptor

    def changed_mount_id(descriptor, *, path):
        mount_id = real_mount_id(descriptor, path=path)
        if obs_transaction_fs._file_identity(os.fstat(descriptor)) == target_identity:
            return mount_id + 1
        return mount_id

    monkeypatch.setattr(
        obs_transaction_fs,
        "_posix_mount_id_for_descriptor",
        changed_mount_id,
    )


@pytest.mark.skipif(
    not _POSIX_MOUNT_ID_AVAILABLE,
    reason="Linux /proc mount boundaryのCIテスト",
)
def test_posix_source_nested_mount_is_rejected_before_destination_creation(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    executable = _write_fake_obs(source, b"keep")
    nested_mount = source / "nested-mount"
    nested_mount.mkdir()
    sentinel = nested_mount / "external-sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    _simulate_posix_mount_transition(monkeypatch, nested_mount)

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="mount境界",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert executable.read_bytes() == b"keep"
    assert sentinel.read_text(encoding="utf-8") == "keep"
    _assert_no_transaction_artifacts(destination)


@pytest.mark.skipif(
    not _POSIX_MOUNT_ID_AVAILABLE,
    reason="Linux /proc mount boundaryのCIテスト",
)
def test_posix_destination_nested_mount_is_rejected_before_external_write(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source, b"keep")
    (source / "nested-mount").mkdir()
    nested_mount = destination / "nested-mount"
    nested_mount.mkdir(parents=True)
    sentinel = nested_mount / "external-sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    _simulate_posix_mount_transition(monkeypatch, nested_mount)

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="mount境界",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert not obs_bootstrap.get_obs_copy_lock_path(destination).exists()


@pytest.mark.skipif(
    not _POSIX_MOUNT_ID_AVAILABLE,
    reason="Linux /proc file mount boundaryのCIテスト",
)
def test_posix_managed_lease_rejects_nested_file_mount_identity(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "managed"
    root.mkdir()
    mounted_file = root / "mounted-file.bin"
    mounted_file.write_bytes(b"keep")
    with obs_transaction_fs._OBSDirectoryLease.open_absolute(root) as root_lease:
        _simulate_posix_mount_transition(monkeypatch, mounted_file)
        with pytest.raises(obs_transaction_fs.OBSPathSafetyError, match="mount境界"):
            root_lease.open_file(
                mounted_file.name,
                write=False,
                create_exclusive=False,
            )


@pytest.mark.skipif(os.name != "nt", reason="NTFS ADSの実APIテスト")
def test_windows_named_ads_is_rejected_by_handle_inventory(tmp_path):
    root = tmp_path / "managed"
    target = _write_fake_obs(root, b"default-stream")
    try:
        with open(f"{target}:issue83-test", "wb") as stream:
            stream.write(b"hidden-stream")
    except OSError as exc:
        pytest.skip(f"このfilesystemではnamed ADSを作成できません: {exc}")

    with pytest.raises(
        obs_bootstrap.OBSPathSafetyError,
        match="alternate data stream",
    ):
        obs_bootstrap._build_obs_tree_inventory(root)

    assert target.read_bytes() == b"default-stream"


@pytest.mark.skipif(os.name != "nt", reason="NTFS root ADSの実APIテスト")
def test_finalizer_rejects_named_ads_added_to_managed_root(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    stream_path = f"{destination}:issue83-root"

    def add_root_stream(current_destination: Path) -> None:
        with open(f"{current_destination}:issue83-root", "wb") as stream:
            stream.write(b"hidden")

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="alternate data stream|安全に解除できません",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=add_root_stream,
        )

    assert Path(stream_path).read_bytes() == b"hidden"
    assert obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows root attributesの実APIテスト")
def test_finalizer_rejects_managed_root_attribute_only_change(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)

    def make_root_read_only(current_destination: Path) -> None:
        os.chmod(current_destination, stat.S_IREAD)

    try:
        with pytest.raises(
            obs_bootstrap.OBSMigrationRecoveryRequiredError,
            match="allowlist外.*<root>",
        ):
            obs_bootstrap.migrate_legacy_obs_installation(
                destination,
                [source],
                finalize_destination=make_root_read_only,
            )

        assert destination.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
        assert obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    finally:
        if destination.exists():
            os.chmod(destination, stat.S_IWRITE)


def test_finalizer_rejects_managed_root_security_descriptor_change(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    finalizer_active = False
    real_snapshot = obs_bootstrap._snapshot_open_entry_metadata

    def snapshot_with_changed_root_security(
        descriptor,
        *,
        path,
        kind,
        native_windows_handle=False,
    ):
        metadata = real_snapshot(
            descriptor,
            path=path,
            kind=kind,
            native_windows_handle=native_windows_handle,
        )
        if finalizer_active and Path(path) == destination and kind == "directory":
            replacement = (
                "f" * 64
                if metadata.security_descriptor_sha256 != "f" * 64
                else "e" * 64
            )
            return replace(
                metadata,
                security_descriptor_sha256=replacement,
            )
        return metadata

    def change_security_descriptor(_current_destination: Path) -> None:
        nonlocal finalizer_active
        finalizer_active = True

    monkeypatch.setattr(
        obs_bootstrap,
        "_snapshot_open_entry_metadata",
        snapshot_with_changed_root_security,
    )

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="allowlist外.*<root>",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=change_security_descriptor,
        )

    assert obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


def test_inventory_uses_exact_metadata_within_tree_but_content_across_trees(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source_file = source / "same.bin"
    destination_file = destination / "same.bin"
    source.mkdir()
    destination.mkdir()
    source_file.write_bytes(b"same")
    destination_file.write_bytes(b"same")
    destination_stat = destination_file.stat()
    os.utime(
        destination_file,
        ns=(destination_stat.st_atime_ns, destination_stat.st_mtime_ns + 2_000_000_000),
    )

    source_inventory = obs_bootstrap._build_obs_tree_inventory(source)
    destination_inventory = obs_bootstrap._build_obs_tree_inventory(destination)

    assert source_inventory != destination_inventory
    assert source_inventory[0].metadata is not None
    assert destination_inventory[0].metadata is not None
    assert obs_bootstrap._inventory_content_matches(
        source_inventory,
        destination_inventory,
    )


@pytest.mark.skipif(
    not _POSIX_MOUNT_ID_AVAILABLE,
    reason="Linux /proc xattr/ACL inventoryのCIテスト",
)
def test_posix_xattr_only_difference_is_exact_metadata_change(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    source_file = source / "same.bin"
    destination_file = destination / "same.bin"
    source_file.write_bytes(b"same")
    destination_file.write_bytes(b"same")
    try:
        os.setxattr(source_file, "user.issue83", b"source")
        os.setxattr(destination_file, "user.issue83", b"destination")
    except OSError as exc:
        pytest.skip(f"このfilesystemではuser xattrを設定できません: {exc}")

    source_inventory = obs_bootstrap._build_obs_tree_inventory(source)
    destination_inventory = obs_bootstrap._build_obs_tree_inventory(destination)

    assert source_inventory != destination_inventory
    assert obs_bootstrap._inventory_content_matches(
        source_inventory,
        destination_inventory,
    )


def test_finalizer_rejects_unmanaged_file_metadata_only_change(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    unmanaged = source / "obs-plugins" / "64bit" / "plugin.dll"
    unmanaged.parent.mkdir(parents=True)
    unmanaged.write_bytes(b"unchanged")

    def change_metadata_only(current_destination: Path) -> None:
        target = current_destination / unmanaged.relative_to(source)
        target_stat = target.stat()
        os.utime(
            target,
            ns=(target_stat.st_atime_ns, target_stat.st_mtime_ns + 2_000_000_000),
        )

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="allowlist外",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=change_metadata_only,
        )

    assert unmanaged.read_bytes() == b"unchanged"
    assert obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


def test_metadata_capability_denial_fails_before_destination_creation(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    executable = _write_fake_obs(source, b"keep")
    real_snapshot = obs_transaction_fs._snapshot_open_entry_metadata

    def deny_source_metadata(descriptor, *, path, kind, native_windows_handle=False):
        if Path(path) == source:
            raise obs_transaction_fs._UnsafeOBSMigrationPathError(
                "simulated metadata capability denial"
            )
        return real_snapshot(
            descriptor,
            path=path,
            kind=kind,
            native_windows_handle=native_windows_handle,
        )

    monkeypatch.setattr(
        obs_transaction_fs,
        "_snapshot_open_entry_metadata",
        deny_source_metadata,
    )

    with pytest.raises(
        obs_bootstrap.OBSMigrationRecoveryRequiredError,
        match="metadata capability denial",
    ):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert executable.read_bytes() == b"keep"
    _assert_no_transaction_artifacts(destination)


def test_schema_v3_content_journal_resumes_and_upgrades(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    executable = _write_fake_obs(source, b"schema-v3")
    source_inventory = obs_bootstrap._build_obs_tree_inventory(source)
    destination.mkdir()
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    marker.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "source": str(source.resolve()),
                "source_fingerprint": obs_bootstrap._legacy_inventory_fingerprint(
                    source_inventory
                ),
                "phase": obs_bootstrap.OBS_MIGRATION_PHASE_COPYING,
                "owner_pid": os.getpid(),
                "owner_token": "a" * 32,
                "started_at": 1.0,
            }
        ),
        encoding="utf-8",
    )

    migrated = obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert migrated == source.resolve()
    assert (destination / executable.relative_to(source)).read_bytes() == b"schema-v3"
    assert not marker.exists()
