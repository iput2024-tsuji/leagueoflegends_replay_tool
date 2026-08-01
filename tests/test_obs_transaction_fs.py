import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import obs_bootstrap, obs_transaction_fs


def test_obs_bootstrap_reexports_filesystem_compatibility_api():
    assert obs_bootstrap.OBSPathSafetyError is obs_transaction_fs.OBSPathSafetyError
    assert obs_bootstrap._OBSDirectoryLease is obs_transaction_fs._OBSDirectoryLease
    assert obs_bootstrap._OBSInterProcessLock is obs_transaction_fs._OBSInterProcessLock
    assert obs_bootstrap.lexical_absolute_path is obs_transaction_fs.lexical_absolute_path
    assert (
        obs_bootstrap._OBSFilesystemMetadata
        is obs_transaction_fs._OBSFilesystemMetadata
    )
    assert (
        obs_bootstrap._validate_distinct_physical_directory_trees
        is obs_transaction_fs._validate_distinct_physical_directory_trees
    )


def test_path_lexists_propagates_permission_error(monkeypatch, tmp_path):
    protected = tmp_path / "protected"

    def deny_lstat(path):
        if Path(path) == protected:
            raise PermissionError("simulated lstat ACL denial")
        raise FileNotFoundError(path)

    monkeypatch.setattr(obs_transaction_fs.os, "lstat", deny_lstat)

    with pytest.raises(PermissionError, match="lstat ACL denial"):
        obs_transaction_fs._path_lexists(protected)
    assert obs_transaction_fs._path_lexists(tmp_path / "missing") is False


def test_posix_handle_relative_capability_requires_rename(monkeypatch):
    required = {
        obs_transaction_fs.os.open,
        obs_transaction_fs.os.mkdir,
        obs_transaction_fs.os.rename,
        obs_transaction_fs.os.unlink,
        obs_transaction_fs.os.stat,
    }
    monkeypatch.setattr(
        obs_transaction_fs.os,
        "supports_fd",
        {obs_transaction_fs.os.scandir},
    )
    monkeypatch.setattr(obs_transaction_fs.os, "supports_dir_fd", required)
    monkeypatch.setattr(obs_transaction_fs.os, "O_DIRECTORY", 0x10000, raising=False)
    monkeypatch.setattr(obs_transaction_fs.os, "O_NOFOLLOW", 0x20000, raising=False)

    assert obs_transaction_fs._supports_posix_handle_relative_migration() is True

    monkeypatch.setattr(obs_transaction_fs.os, "O_NOFOLLOW", 0)

    assert obs_transaction_fs._supports_posix_handle_relative_migration() is False

    monkeypatch.setattr(obs_transaction_fs.os, "O_NOFOLLOW", 0x20000)
    monkeypatch.setattr(
        obs_transaction_fs.os,
        "supports_dir_fd",
        (required - {obs_transaction_fs.os.rename}) | {obs_transaction_fs.os.replace},
    )

    assert obs_transaction_fs._supports_posix_handle_relative_migration() is False


@pytest.mark.skipif(os.name == "nt", reason="POSIXのO_NOFOLLOWエラー分類を検証するため")
def test_posix_directory_lease_rejects_symlink_child_as_path_safety_error(tmp_path):
    root = tmp_path / "root"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    (root / "linked").symlink_to(external, target_is_directory=True)

    with obs_transaction_fs._OBSDirectoryLease.open_absolute(root, mutable=True) as lease:
        with pytest.raises(obs_transaction_fs.OBSPathSafetyError, match="reparse point"):
            lease.open_child_directory("linked", create=True, mutable=True)

    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name == "nt", reason="POSIXのdirectory作成raceを検証するため")
def test_posix_directory_lease_rejects_symlink_injected_after_mkdir(monkeypatch, tmp_path):
    root = tmp_path / "root"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    real_mkdir = obs_transaction_fs.os.mkdir

    with obs_transaction_fs._OBSDirectoryLease.open_absolute(root, mutable=True) as lease:
        def replace_created_directory_with_symlink(path, mode=0o777, *, dir_fd=None):
            real_mkdir(path, mode, dir_fd=dir_fd)
            obs_transaction_fs.os.rmdir(path, dir_fd=dir_fd)
            (root / str(path)).symlink_to(external, target_is_directory=True)

        monkeypatch.setattr(
            obs_transaction_fs.os,
            "mkdir",
            replace_created_directory_with_symlink,
        )

        with pytest.raises(
            obs_transaction_fs.OBSPathSafetyError,
            match="symbolic link|reparse point",
        ):
            lease.open_child_directory("injected", create=True, mutable=True)

    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    ("kind", "target_name"),
    [
        (obs_transaction_fs.OBS_TRANSACTION_TEMP_COPY, "payload.bin"),
        (obs_transaction_fs.OBS_TRANSACTION_TEMP_WRITE, "settings.json"),
        (obs_transaction_fs.OBS_TRANSACTION_TEMP_JOURNAL, ".migration-journal"),
    ],
)
def test_transaction_temporary_name_round_trip(tmp_path, kind, target_name):
    owner_token = "a" * 32
    target = tmp_path / target_name
    temporary = obs_transaction_fs._transaction_temporary_path(
        target,
        owner_token,
        kind=kind,
    )

    parsed = obs_transaction_fs._parse_transaction_temporary(
        temporary,
        journal_target_name=".migration-journal",
    )

    assert parsed == obs_transaction_fs._OBSTransactionTemporaryDescriptor(
        kind=kind,
        target_name=target_name,
        owner_token=owner_token,
        path=temporary,
    )


def test_zero_byte_stale_lock_is_reinitialized(tmp_path):
    lock_path = tmp_path / "lock-parent" / "migration.lock"
    lock_path.parent.mkdir()
    lock_path.write_bytes(b"")
    lock = obs_transaction_fs._OBSInterProcessLock(lock_path)

    assert lock.acquire() is True
    try:
        lock.validate_ownership()
    finally:
        lock.release()

    assert lock_path.read_bytes() == b"\0"


def test_lock_acquire_failure_closes_directory_lease(monkeypatch, tmp_path):
    parent = tmp_path / "lock-parent"
    renamed = tmp_path / "renamed-parent"
    lock_name = "migration.lock"
    parent.mkdir()
    lock = obs_transaction_fs._OBSInterProcessLock(parent / lock_name)
    real_probe = obs_transaction_fs._OBSDirectoryLease.relative_file_identity_or_none

    def fail_probe(directory, name):
        if directory.path == parent.resolve() and name == lock_name:
            raise PermissionError("simulated lock probe failure")
        return real_probe(directory, name)

    monkeypatch.setattr(
        obs_transaction_fs._OBSDirectoryLease,
        "relative_file_identity_or_none",
        fail_probe,
    )
    with pytest.raises(PermissionError, match="lock probe failure"):
        lock.acquire()

    parent.rename(renamed)
    assert renamed.is_dir()


def test_native_created_directory_is_reopenable_from_another_process(tmp_path):
    created = tmp_path / "native" / "child"
    with obs_transaction_fs._OBSDirectoryLease.open_absolute(
        created,
        create=True,
        mutable=True,
    ):
        pass
    script = (
        "from pathlib import Path; import sys; "
        "p=Path(sys.argv[1]); "
        "f=p/'probe.txt'; f.write_text('ok', encoding='utf-8'); "
        "assert f.read_text(encoding='utf-8') == 'ok'; f.unlink(); list(p.iterdir())"
    )

    completed = subprocess.run(
        [os.fspath(Path(os.sys.executable)), "-c", script, str(created)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


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


@pytest.mark.skipif(os.name != "nt", reason="Windows Native API sharing semantics")
def test_windows_nested_directory_lease_pins_name_against_cross_process_rename(tmp_path):
    root = tmp_path / "tree"
    nested = root / "nested"
    parked = root / "parked"
    nested.mkdir(parents=True)
    script = "import os, sys; os.replace(sys.argv[1], sys.argv[2])"

    with obs_transaction_fs._OBSDirectoryLease.open_absolute(root) as root_lease:
        with root_lease.open_child_directory("nested"):
            blocked = subprocess.run(
                [sys.executable, "-c", script, str(nested), str(parked)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert blocked.returncode != 0
            assert nested.is_dir()
            assert not parked.exists()

    completed = subprocess.run(
        [sys.executable, "-c", script, str(nested), str(parked)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert parked.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows Native API sharing semantics")
def test_windows_relative_replace_and_unlink_release_handles_for_subprocess_reopen(tmp_path):
    root = tmp_path / "managed"
    root.mkdir()
    target = root / "state.bin"
    target.write_bytes(b"old")

    with obs_transaction_fs._OBSDirectoryLease.open_absolute(root, mutable=True) as lease:
        descriptor = lease.open_file(
            "replacement.tmp",
            write=True,
            create_exclusive=True,
        )
        try:
            os.write(descriptor, b"new")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        descriptor = lease.open_file(
            "replacement.tmp",
            write=True,
            create_exclusive=False,
            delete=True,
        )
        try:
            lease.replace_open_file(descriptor, "replacement.tmp", target.name)
        finally:
            os.close(descriptor)
        assert target.read_bytes() == b"new"
        target_identity = lease._relative_file_identity(target.name)
        lease.unlink_file(target.name, expected_identity=target_identity)
        assert not target.exists()

    _assert_directory_reopenable_from_subprocess(root)
    script = (
        "from pathlib import Path; import sys; "
        "p=Path(sys.argv[1]); p.write_bytes(b'reopened'); "
        "assert p.read_bytes() == b'reopened'"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert target.read_bytes() == b"reopened"


@pytest.mark.skipif(os.name != "nt", reason="Windows access mask regression")
def test_windows_mutable_directory_handle_uses_least_privilege_access():
    file_delete_child = 0x0040

    access = obs_transaction_fs._windows_directory_access(mutable=True)

    assert access & obs_transaction_fs._WINDOWS_FILE_ADD_FILE
    assert access & obs_transaction_fs._WINDOWS_FILE_ADD_SUBDIRECTORY
    assert access & file_delete_child == 0


@pytest.mark.parametrize(
    "name",
    [
        ".",
        "..",
        "x:y",
        "bad<name",
        "bad\x01",
        "trailing.",
        "trailing ",
        "CON",
        "con.txt",
        "COM¹",
        "LPT³.log",
    ],
)
def test_migration_rejects_unsafe_native_path_components(name):
    with pytest.raises(obs_transaction_fs.OBSPathSafetyError):
        obs_transaction_fs._validate_single_path_component(name)
