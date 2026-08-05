import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import obs_bootstrap, obs_transaction_fs

_POSIX_MOUNT_ID_AVAILABLE = (
    os.name != "nt" and Path("/proc/self/fdinfo").is_dir()
)


def _create_file_after_event(path: str, create_now, created) -> None:
    if not create_now.wait(15):
        raise TimeoutError("parent did not release target creation")
    Path(path).write_bytes(b"concurrent")
    created.set()


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


def test_recursive_change_monitor_observes_nested_names_without_blocking_changes(
    tmp_path,
):
    root = tmp_path / "tree"
    nested = root / "nested"
    nested.mkdir(parents=True)
    with obs_transaction_fs._OBSDirectoryLease.open_absolute(root) as root_lease:
        try:
            monitor = obs_transaction_fs._OBSRecursiveChangeMonitor.open(root_lease)
        except obs_transaction_fs._OBSRecursiveChangeMonitorUnavailable:
            pytest.skip("recursive change notification is unavailable")
        try:
            with root_lease.open_child_directory(nested.name) as nested_lease:
                monitor.watch_directory(nested_lease)
            assert monitor.has_changes() is False

            temporary = nested / "payload.tmp"
            published = nested / "payload.bin"
            temporary.write_bytes(b"payload")
            temporary.replace(published)
            published.unlink()

            assert monitor.has_changes() is True
        finally:
            monitor.close()

    renamed = tmp_path / "renamed-tree"
    root.rename(renamed)
    assert renamed.is_dir()


@pytest.mark.skipif(
    not _POSIX_MOUNT_ID_AVAILABLE,
    reason="Linux /proc mount identityのFD close回帰",
)
def test_posix_anchor_constructor_failure_closes_descriptor(monkeypatch, tmp_path):
    captured_descriptors = []

    def deny_mount_identity(descriptor, *, path):
        captured_descriptors.append(descriptor)
        raise obs_transaction_fs.OBSPathSafetyError(
            f"simulated mount identity denial: {path}"
        )

    monkeypatch.setattr(
        obs_transaction_fs,
        "_posix_mount_id_for_descriptor",
        deny_mount_identity,
    )

    with pytest.raises(
        obs_transaction_fs.OBSPathSafetyError,
        match="mount identity denial",
    ):
        obs_transaction_fs._OBSDirectoryLease.open_absolute(tmp_path)

    assert captured_descriptors
    with pytest.raises(OSError):
        os.fstat(captured_descriptors[-1])


@pytest.mark.skipif(
    not _POSIX_MOUNT_ID_AVAILABLE,
    reason="Linux /proc mutable clone FD close回帰",
)
def test_posix_mutable_clone_constructor_failure_closes_descriptor(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "managed"
    root.mkdir()
    captured_descriptors = []

    with obs_transaction_fs._OBSDirectoryLease.open_absolute(root) as root_lease:

        def fail_constructor(
            _self,
            _path,
            native_handle,
            _identity,
            *,
            mutable,
            mount_boundary=None,
        ):
            del mutable, mount_boundary
            captured_descriptors.append(native_handle)
            raise RuntimeError("simulated lease constructor failure")

        monkeypatch.setattr(
            obs_transaction_fs._OBSDirectoryLease,
            "__init__",
            fail_constructor,
        )

        with pytest.raises(RuntimeError, match="constructor failure"):
            root_lease.mutable_clone()

        assert captured_descriptors
        with pytest.raises(OSError):
            os.fstat(captured_descriptors[-1])


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


@pytest.mark.skipif(os.name != "nt", reason="Windows lease file sharing semantics")
def test_windows_pin_rejects_external_replace_in_place_write_delete_until_close(
    tmp_path,
):
    root = tmp_path / "managed"
    root.mkdir()
    target = root / "lease.json"
    replacement = root / "replacement.json"
    target.write_bytes(b"owned")
    replacement.write_bytes(b"replacement")
    replace_script = (
        "import os, sys; "
        "\ntry: os.replace(sys.argv[1], sys.argv[2])"
        "\nexcept OSError as exc:"
        "\n assert exc.winerror in {5, 32, 33}, repr(exc); print('blocked')"
        "\nelse: raise AssertionError('replace unexpectedly succeeded')"
    )
    write_script = (
        "import ctypes, sys; from ctypes import wintypes; "
        "kernel32=ctypes.WinDLL('kernel32', use_last_error=True); "
        "kernel32.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,"
        "wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,"
        "wintypes.HANDLE]; kernel32.CreateFileW.restype=wintypes.HANDLE; "
        "handle=kernel32.CreateFileW(sys.argv[1],0x40000000,0x7,None,3,0x80,None); "
        "invalid=ctypes.c_void_p(-1).value; "
        "assert int(handle)==invalid and ctypes.get_last_error() in {5,32,33}, "
        "(handle,ctypes.get_last_error()); print('blocked')"
    )
    delete_script = (
        "import os, sys; "
        "\ntry: os.unlink(sys.argv[1])"
        "\nexcept OSError as exc:"
        "\n assert exc.winerror in {5, 32, 33}, repr(exc); print('blocked')"
        "\nelse: raise AssertionError('delete unexpectedly succeeded')"
    )

    with obs_transaction_fs._OBSDirectoryLease.open_absolute(
        root,
        mutable=True,
    ) as lease:
        descriptor = lease.open_file(
            target.name,
            write=False,
            create_exclusive=False,
            delete=True,
            share_write=False,
            share_delete=False,
        )
        try:
            replaced = subprocess.run(
                [sys.executable, "-c", replace_script, str(replacement), str(target)],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            written = subprocess.run(
                [sys.executable, "-c", write_script, str(target)],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            deleted = subprocess.run(
                [sys.executable, "-c", delete_script, str(target)],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            assert replaced.returncode == 0, (replaced.stdout, replaced.stderr)
            assert written.returncode == 0, (written.stdout, written.stderr)
            assert deleted.returncode == 0, (deleted.stdout, deleted.stderr)
            assert replaced.stdout.strip() == "blocked"
            assert written.stdout.strip() == "blocked"
            assert deleted.stdout.strip() == "blocked"
            assert replacement.read_bytes() == b"replacement"
            assert target.exists()
            os.lseek(descriptor, 0, os.SEEK_SET)
            assert os.read(descriptor, 16) == b"owned"
        finally:
            os.close(descriptor)

    os.replace(replacement, target)
    assert target.read_bytes() == b"replacement"


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-on-close semantics")
def test_windows_marks_the_same_open_identity_for_delete_on_close(tmp_path):
    root = tmp_path / "managed"
    root.mkdir()
    target = root / "lease.json"
    target.write_bytes(b"owned")

    with obs_transaction_fs._OBSDirectoryLease.open_absolute(
        root,
        mutable=True,
    ) as lease:
        descriptor = lease.open_file(
            target.name,
            write=False,
            create_exclusive=False,
            delete=True,
            share_write=False,
            share_delete=False,
        )
        identity = obs_transaction_fs._file_identity(os.fstat(descriptor))
        close_error = lease.delete_open_file_on_close(
            descriptor,
            target.name,
            expected_identity=identity,
        )
        assert close_error is None
        assert not target.exists()

        target.write_bytes(b"new lease")

    assert target.read_bytes() == b"new lease"


@pytest.mark.skipif(os.name != "nt", reason="Windows delete commit boundary")
def test_windows_delete_commit_returns_close_failure_as_diagnostic(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "managed"
    root.mkdir()
    target = root / "lease.json"
    target.write_bytes(b"owned")
    real_close = obs_transaction_fs.os.close

    with obs_transaction_fs._OBSDirectoryLease.open_absolute(
        root,
        mutable=True,
    ) as lease:
        descriptor = lease.open_file(
            target.name,
            write=False,
            create_exclusive=False,
            delete=True,
            share_write=False,
            share_delete=False,
        )
        identity = obs_transaction_fs._file_identity(os.fstat(descriptor))

        def close_then_report_failure(candidate):
            real_close(candidate)
            if candidate == descriptor:
                raise OSError("simulated close report failure")

        monkeypatch.setattr(obs_transaction_fs.os, "close", close_then_report_failure)
        close_error = lease.delete_open_file_on_close(
            descriptor,
            target.name,
            expected_identity=identity,
        )

        assert isinstance(close_error, OSError)
        assert "close report failure" in str(close_error)
        assert not target.exists()


def test_delete_commit_consumes_descriptor_after_non_exception_close_report(
    monkeypatch,
    tmp_path,
):
    class CloseReportedControlFlow(BaseException):
        pass

    root = tmp_path / "managed"
    root.mkdir()
    target = root / "lease.json"
    target.write_bytes(b"old lease")
    reported = CloseReportedControlFlow("simulated post-close control flow")
    real_close = obs_transaction_fs.os.close

    with obs_transaction_fs._OBSDirectoryLease.open_absolute(
        root,
        mutable=True,
    ) as lease:
        descriptor = lease.open_file(
            target.name,
            write=False,
            create_exclusive=False,
            delete=True,
            share_write=False,
            share_delete=False,
        )
        identity = obs_transaction_fs._file_identity(os.fstat(descriptor))
        close_calls = 0

        def close_then_report(candidate):
            nonlocal close_calls
            if candidate == descriptor:
                close_calls += 1
                real_close(candidate)
                raise reported
            real_close(candidate)

        monkeypatch.setattr(obs_transaction_fs.os, "close", close_then_report)
        close_failure = lease.delete_open_file_on_close(
            descriptor,
            target.name,
            expected_identity=identity,
        )

        assert close_failure is reported
        assert close_calls == 1
        assert not target.exists()
        target.write_bytes(b"new lease")
        assert target.read_bytes() == b"new lease"


@pytest.mark.skipif(os.name != "nt", reason="Windows no-clobber rename semantics")
def test_windows_no_clobber_publish_rejects_target_created_after_precheck(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "managed"
    root.mkdir()
    temporary = root / "lease.tmp"
    target = root / "lease.json"
    context = multiprocessing.get_context("spawn")
    create_now = context.Event()
    created = context.Event()
    child = context.Process(
        target=_create_file_after_event,
        args=(str(target), create_now, created),
    )
    child.start()
    real_rename = obs_transaction_fs._windows_rename_open_file

    def create_target_then_rename(*args, **kwargs):
        create_now.set()
        assert created.wait(15)
        return real_rename(*args, **kwargs)

    monkeypatch.setattr(
        obs_transaction_fs,
        "_windows_rename_open_file",
        create_target_then_rename,
    )
    descriptor = None
    try:
        with obs_transaction_fs._OBSDirectoryLease.open_absolute(
            root,
            mutable=True,
        ) as lease:
            descriptor = lease.open_file(
                temporary.name,
                write=True,
                create_exclusive=True,
                delete=True,
            )
            os.write(descriptor, b"temporary")
            os.fsync(descriptor)
            identity = obs_transaction_fs._file_identity(os.fstat(descriptor))

            with pytest.raises(FileExistsError):
                lease.publish_open_file_no_replace(
                    descriptor,
                    temporary.name,
                    target.name,
                )

            assert target.read_bytes() == b"concurrent"
            assert lease._relative_file_identity(temporary.name) == identity
            os.lseek(descriptor, 0, os.SEEK_SET)
            assert os.read(descriptor, 32) == b"temporary"
    finally:
        if descriptor is not None:
            os.close(descriptor)
        create_now.set()
        child.join(timeout=15)
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
            child.close()
            pytest.fail("target creator child did not exit")
        exitcode = child.exitcode
        child.close()
        assert exitcode == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows publish commit boundary")
def test_windows_no_clobber_publish_reports_post_native_failure_as_commit_uncertain(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "managed"
    root.mkdir()
    temporary = root / "lease.tmp"
    target = root / "lease.json"
    real_rename = obs_transaction_fs._windows_rename_open_file

    def rename_then_fail(*args, **kwargs):
        real_rename(*args, **kwargs)
        raise OSError("simulated post-publish failure")

    monkeypatch.setattr(
        obs_transaction_fs,
        "_windows_rename_open_file",
        rename_then_fail,
    )
    with obs_transaction_fs._OBSDirectoryLease.open_absolute(
        root,
        mutable=True,
    ) as lease:
        descriptor = lease.open_file(
            temporary.name,
            write=True,
            create_exclusive=True,
            delete=True,
        )
        try:
            os.write(descriptor, b"published")
            os.fsync(descriptor)
            identity = obs_transaction_fs._file_identity(os.fstat(descriptor))

            with pytest.raises(OSError, match="post-publish failure"):
                lease.publish_open_file_no_replace(
                    descriptor,
                    temporary.name,
                    target.name,
                )

            assert not temporary.exists()
            os.lseek(descriptor, 0, os.SEEK_SET)
            assert os.read(descriptor, 32) == b"published"
            assert lease._relative_file_identity(target.name) == identity
        finally:
            os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX link/unlink commit boundary")
def test_posix_no_clobber_publish_keeps_commit_uncertain_double_link(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "managed"
    root.mkdir()
    temporary = root / "lease.tmp"
    target = root / "lease.json"
    real_unlink = obs_transaction_fs.os.unlink

    def fail_temporary_unlink(path, *, dir_fd=None):
        if path == temporary.name and dir_fd is not None:
            raise OSError("simulated temporary unlink failure")
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(obs_transaction_fs.os, "unlink", fail_temporary_unlink)
    with obs_transaction_fs._OBSDirectoryLease.open_absolute(
        root,
        mutable=True,
    ) as lease:
        descriptor = lease.open_file(
            temporary.name,
            write=True,
            create_exclusive=True,
            delete=True,
        )
        try:
            os.write(descriptor, b"published")
            os.fsync(descriptor)
            with pytest.raises(OSError, match="temporary unlink failure"):
                lease.publish_open_file_no_replace(
                    descriptor,
                    temporary.name,
                    target.name,
                )
            assert temporary.read_bytes() == b"published"
            assert target.read_bytes() == b"published"
            assert os.stat(temporary).st_ino == os.stat(target).st_ino
            assert os.fstat(descriptor).st_nlink == 2
        finally:
            os.close(descriptor)


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
