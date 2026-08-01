import ctypes
import json
import os
from pathlib import Path

import pytest

from src import obs_bootstrap, obs_transaction_fs


def _write_fake_obs(root: Path, payload: bytes = b"obs") -> Path:
    executable = root / "bin" / "64bit" / "obs64.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(payload)
    return executable


def _assert_no_transaction_artifacts(destination: Path) -> None:
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert not obs_bootstrap.get_obs_copy_lock_path(destination).exists()
    assert not destination.exists()


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX device/inode chainのCIテスト")
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


@pytest.mark.skipif(os.name == "nt", reason="POSIX xattr/ACL inventoryのCIテスト")
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
