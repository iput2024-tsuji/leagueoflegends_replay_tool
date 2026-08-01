import errno
import json
import multiprocessing
import os
import subprocess
import time
from pathlib import Path

import pytest

from src import obs_bootstrap
from src.obs_bootstrap import OBSBootstrapper


def _hold_obs_migration(source: str, destination: str, entered, release, result_queue) -> None:
    def wait_before_copy(_source: Path) -> None:
        entered.set()
        if not release.wait(15):
            raise TimeoutError("test did not release migration")

    try:
        migrated = obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            prepare_source=wait_before_copy,
        )
        result_queue.put(("ok", str(migrated)))
    except Exception as exc:  # pragma: no cover - reported to the parent process
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _hold_obs_migration_during_copy(source: str, destination: str, entered) -> None:
    original_write_all = obs_bootstrap._write_all

    def hold_after_copy_write(descriptor: int, payload: bytes) -> None:
        original_write_all(descriptor, payload)
        if payload == b"terminate-during-copy":
            entered.set()
            time.sleep(60)

    obs_bootstrap._write_all = hold_after_copy_write
    obs_bootstrap.migrate_legacy_obs_installation(destination, [source])


def _write_fake_obs(source: Path, contents: bytes = b"obs") -> Path:
    executable = source / "bin" / "64bit" / "obs64.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(contents)
    return executable


def _create_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr or completed.stdout or "mklink /J failed")
        return
    os.symlink(target, link, target_is_directory=True)


def _write_json_journal(
    destination: Path,
    source: Path,
    *,
    owner_token: str = "a" * 32,
    source_fingerprint: str | None = None,
    phase: str = obs_bootstrap.OBS_MIGRATION_PHASE_COPYING,
) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    if source_fingerprint is None:
        source_fingerprint = (
            obs_bootstrap._inventory_fingerprint(obs_bootstrap._build_obs_tree_inventory(source))
            if source.is_dir()
            else "0" * 64
        )
    marker.write_text(
        json.dumps(
            {
                "schema_version": obs_bootstrap.OBS_COPY_JOURNAL_SCHEMA_VERSION,
                "source": str(source.resolve()),
                "source_fingerprint": source_fingerprint,
                "phase": phase,
                "owner_pid": os.getpid(),
                "owner_token": owner_token,
                "started_at": 1.0,
            }
        ),
        encoding="utf-8",
    )
    return marker


class FakeProcessManager:
    def __init__(self) -> None:
        self.kill_calls = 0

    def kill_stale_managed_processes(self, timeout_sec: float = 3.0) -> list[int]:
        self.kill_calls += 1
        return []


def test_path_lexists_propagates_permission_error(monkeypatch, tmp_path):
    protected = tmp_path / "protected"

    def deny_lstat(path):
        if Path(path) == protected:
            raise PermissionError("simulated lstat ACL denial")
        raise FileNotFoundError(path)

    monkeypatch.setattr(obs_bootstrap.os, "lstat", deny_lstat)

    with pytest.raises(PermissionError, match="lstat ACL denial"):
        obs_bootstrap._path_lexists(protected)
    assert obs_bootstrap._path_lexists(tmp_path / "missing") is False


def test_apply_stops_managed_obs_once(tmp_path):
    process_manager = FakeProcessManager()
    bootstrapper = OBSBootstrapper(tmp_path / "obs-portable", process_manager=process_manager)

    result = bootstrapper.apply()

    assert process_manager.kill_calls == 1
    assert Path(result["global_ini_path"]).exists()
    assert Path(result["user_ini_path"]).exists()


def test_standalone_ini_repairs_still_stop_managed_obs(tmp_path):
    process_manager = FakeProcessManager()
    bootstrapper = OBSBootstrapper(tmp_path / "obs-portable", process_manager=process_manager)

    bootstrapper.ensure_global_ini()
    bootstrapper.ensure_user_ini()

    assert process_manager.kill_calls == 2


def test_websocket_config_requires_password_authentication(tmp_path):
    bootstrapper = OBSBootstrapper(tmp_path / "obs-portable", process_manager=FakeProcessManager())

    changed, config_path = bootstrapper.ensure_websocket_config(4455, "secret-password")

    text = config_path.read_text(encoding="utf-8")
    assert changed is True
    assert '"server_enabled": true' in text
    assert '"auth_required": true' in text
    assert '"server_password": "secret-password"' in text


def test_websocket_config_rejects_empty_password(tmp_path):
    bootstrapper = OBSBootstrapper(tmp_path / "obs-portable", process_manager=FakeProcessManager())

    try:
        bootstrapper.ensure_websocket_config(4455, "")
    except ValueError as exc:
        assert "password" in str(exc)
    else:
        raise AssertionError("empty obs-websocket password should be rejected")


def test_bootstrapper_rejects_root_reparse_without_writing_external_target(tmp_path):
    managed = tmp_path / "obs-portable"
    external = tmp_path / "external-obs"
    process_manager = FakeProcessManager()
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    _create_directory_link(managed, external)
    bootstrapper = OBSBootstrapper(managed, process_manager=process_manager)

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="reparse point"):
        bootstrapper.check()
    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="reparse point"):
        bootstrapper.apply()

    assert process_manager.kill_calls == 0
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (external / obs_bootstrap.PORTABLE_OBS_MARKER_NAME).exists()
    assert not (external / "config" / "obs-studio" / "global.ini").exists()


def test_bootstrapper_rejects_config_reparse_before_marker_or_external_write(tmp_path):
    managed = tmp_path / "obs-portable"
    external = tmp_path / "external-config"
    process_manager = FakeProcessManager()
    managed.mkdir()
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    _create_directory_link(managed / "config", external)

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="reparse point"):
        OBSBootstrapper(managed, process_manager=process_manager).apply()

    assert process_manager.kill_calls == 0
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (managed / obs_bootstrap.PORTABLE_OBS_MARKER_NAME).exists()
    assert not (external / "obs-studio" / "global.ini").exists()


def test_bootstrapper_rejects_hardlinked_ini_before_process_stop_or_write(tmp_path):
    managed = tmp_path / "obs-portable"
    external = tmp_path / "external-global.ini"
    process_manager = FakeProcessManager()
    global_ini = obs_bootstrap.get_obs_global_ini_path(managed)
    global_ini.parent.mkdir(parents=True)
    external.write_bytes(b"external settings")
    os.link(external, global_ini)

    bootstrapper = OBSBootstrapper(managed, process_manager=process_manager)
    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="hardlink"):
        bootstrapper.apply()
    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="hardlink"):
        bootstrapper.ensure_global_ini()

    assert process_manager.kill_calls == 0
    assert external.read_bytes() == b"external settings"
    assert not (managed / obs_bootstrap.PORTABLE_OBS_MARKER_NAME).exists()


def test_bootstrap_mutations_are_blocked_while_migration_lock_is_live(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    process_manager = FakeProcessManager()
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_hold_obs_migration,
        args=(str(source), str(destination), entered, release, result_queue),
    )
    process.start()
    try:
        assert entered.wait(15), "migration did not acquire its lock"
        bootstrapper = OBSBootstrapper(destination, process_manager=process_manager)
        with pytest.raises(obs_bootstrap.OBSMigrationInProgressError, match="別のプロセス"):
            bootstrapper.apply()
        with pytest.raises(obs_bootstrap.OBSMigrationInProgressError, match="別のプロセス"):
            bootstrapper.ensure_global_ini()

        assert process_manager.kill_calls == 0
        assert not obs_bootstrap.get_portable_marker_path(destination).exists()
        assert not obs_bootstrap.get_obs_global_ini_path(destination).exists()
    finally:
        release.set()
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert process.exitcode == 0
    try:
        assert result_queue.get(timeout=5) == ("ok", str(source.resolve()))
    finally:
        result_queue.close()
        result_queue.join_thread()


def test_bootstrap_mutations_reject_stale_migration_marker_before_process_stop(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    _write_fake_obs(destination)
    marker = _write_json_journal(destination, source)
    marker_before = marker.read_bytes()
    process_manager = FakeProcessManager()
    bootstrapper = OBSBootstrapper(destination, process_manager=process_manager)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="コピー中marker"):
        bootstrapper.apply()
    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="コピー中marker"):
        bootstrapper.ensure_global_ini()

    assert process_manager.kill_calls == 0
    assert marker.read_bytes() == marker_before
    assert not obs_bootstrap.get_portable_marker_path(destination).exists()
    assert not obs_bootstrap.get_obs_global_ini_path(destination).exists()


def test_apply_propagates_ini_read_permission_error_before_process_stop_or_write(monkeypatch, tmp_path):
    managed = tmp_path / "obs-portable"
    process_manager = FakeProcessManager()
    global_ini = obs_bootstrap.get_obs_global_ini_path(managed)
    global_ini.parent.mkdir(parents=True)
    original = b"[General]\nKeep=original\n"
    global_ini.write_bytes(original)
    real_read = obs_bootstrap._read_safe_file_bytes
    replace_calls = []

    def deny_global_ini(path, **kwargs):
        if Path(path) == global_ini:
            raise PermissionError("simulated config ACL denial")
        return real_read(path, **kwargs)

    def record_replace(source, destination):
        replace_calls.append((Path(source), Path(destination)))
        raise AssertionError("permission failure must not replace the existing config")

    monkeypatch.setattr(obs_bootstrap, "_read_safe_file_bytes", deny_global_ini)
    monkeypatch.setattr(obs_bootstrap.os, "replace", record_replace)

    with pytest.raises(PermissionError, match="ACL denial"):
        OBSBootstrapper(managed, process_manager=process_manager).apply()

    assert process_manager.kill_calls == 0
    assert replace_calls == []
    assert global_ini.read_bytes() == original
    assert not (managed / obs_bootstrap.PORTABLE_OBS_MARKER_NAME).exists()


def test_apply_propagates_path_probe_permission_error_before_process_stop_or_replace(monkeypatch, tmp_path):
    managed = tmp_path / "obs-portable"
    process_manager = FakeProcessManager()
    global_ini = obs_bootstrap.get_obs_global_ini_path(managed)
    global_ini.parent.mkdir(parents=True)
    original = b"[General]\nKeep=original\n"
    global_ini.write_bytes(original)
    real_lexists = obs_bootstrap._path_lexists
    replace_calls = []

    def deny_global_ini_probe(path):
        if Path(path) == global_ini:
            raise PermissionError("simulated path probe ACL denial")
        return real_lexists(path)

    def fail_replace(source, destination):
        replace_calls.append((Path(source), Path(destination)))
        raise AssertionError("permission failure must not replace the existing config")

    monkeypatch.setattr(obs_bootstrap, "_path_lexists", deny_global_ini_probe)
    monkeypatch.setattr(obs_bootstrap.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="probe ACL denial"):
        OBSBootstrapper(managed, process_manager=process_manager).apply()

    assert process_manager.kill_calls == 0
    assert replace_calls == []
    assert global_ini.read_bytes() == original
    assert not obs_bootstrap.get_portable_marker_path(managed).exists()


def test_ini_repair_does_not_replace_config_when_safe_read_is_denied(monkeypatch, tmp_path):
    managed = tmp_path / "obs-portable"
    global_ini = obs_bootstrap.get_obs_global_ini_path(managed)
    global_ini.parent.mkdir(parents=True)
    original = b"[General]\nKeep=original\n"
    global_ini.write_bytes(original)

    def deny_read(*_args, **_kwargs):
        raise PermissionError("simulated config ACL denial")

    def fail_replace(*_args, **_kwargs):
        raise AssertionError("permission failure must not replace the existing config")

    monkeypatch.setattr(obs_bootstrap, "_read_safe_file_bytes", deny_read)
    monkeypatch.setattr(obs_bootstrap.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="ACL denial"):
        OBSBootstrapper(managed, process_manager=FakeProcessManager())._ensure_obs_ini(
            global_ini,
            label="global.ini",
        )

    assert global_ini.read_bytes() == original


def test_websocket_repair_does_not_replace_config_when_safe_read_is_denied(monkeypatch, tmp_path):
    managed = tmp_path / "obs-portable"
    config_path = obs_bootstrap.get_obs_websocket_config_path(managed)
    config_path.parent.mkdir(parents=True)
    original = b'{"server_enabled": false, "keep": "original"}'
    config_path.write_bytes(original)

    def deny_read(*_args, **_kwargs):
        raise PermissionError("simulated websocket ACL denial")

    def fail_replace(*_args, **_kwargs):
        raise AssertionError("permission failure must not replace the existing config")

    monkeypatch.setattr(obs_bootstrap, "_read_safe_file_bytes", deny_read)
    monkeypatch.setattr(obs_bootstrap.os, "replace", fail_replace)

    with pytest.raises(PermissionError, match="ACL denial"):
        OBSBootstrapper(managed, process_manager=FakeProcessManager()).ensure_websocket_config(
            4455,
            "secret-password",
        )

    assert config_path.read_bytes() == original


def test_migration_capability_cannot_tag_write_outside_its_destination(tmp_path):
    destination = (tmp_path / "obs-portable").absolute()
    outside = (tmp_path / "outside" / "config.ini").absolute()
    owner_token = "a" * 32
    capability_token = obs_bootstrap._ACTIVE_OBS_MIGRATION_CAPABILITY.set(
        (destination, owner_token)
    )
    scope_token = obs_bootstrap._ACTIVE_OBS_BOOTSTRAP_MUTATION.set(
        obs_bootstrap._OBSBootstrapMutationScope(destination, None, owner_token)
    )
    try:
        with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="destination外"):
            obs_bootstrap._write_safe_file_bytes(outside, b"unsafe")
    finally:
        obs_bootstrap._ACTIVE_OBS_BOOTSTRAP_MUTATION.reset(scope_token)
        obs_bootstrap._ACTIVE_OBS_MIGRATION_CAPABILITY.reset(capability_token)

    assert not outside.parent.exists()


def test_obs_migration_excludes_a_second_process_and_keeps_source(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source)
    user_file = source / "config" / "obs-studio" / "basic" / "profiles" / "user.txt"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("keep me", encoding="utf-8")

    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_hold_obs_migration,
        args=(str(source), str(destination), entered, release, result_queue),
    )
    process.start()
    try:
        assert entered.wait(15), "first migration did not acquire the lock"
        assert obs_bootstrap.is_obs_copy_in_progress(destination) is True
        with pytest.raises(obs_bootstrap.OBSMigrationInProgressError, match="別のプロセス"):
            obs_bootstrap.migrate_legacy_obs_installation(destination, [source])
        assert obs_bootstrap.is_obs_copy_in_progress(destination) is True
    finally:
        release.set()
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert process.exitcode == 0
    try:
        child_result = result_queue.get(timeout=5)
    finally:
        result_queue.close()
        result_queue.join_thread()
    assert child_result == ("ok", str(source.resolve()))
    assert obs_bootstrap.is_obs_copy_in_progress(destination) is False
    assert source_executable.read_bytes() == b"obs"
    assert user_file.read_text(encoding="utf-8") == "keep me"
    assert (destination / user_file.relative_to(source)).read_text(encoding="utf-8") == "keep me"


def test_obs_migration_resumes_after_process_termination_leaves_copy_temporary(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source, b"terminate-during-copy")
    destination_executable = destination / source_executable.relative_to(source)
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    process = context.Process(
        target=_hold_obs_migration_during_copy,
        args=(str(source), str(destination), entered),
    )
    process.start()
    child_exitcode = None
    try:
        assert entered.wait(15), "migration did not pause after writing the copy temporary"
        assert process.is_alive()
        marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
        journal = json.loads(marker.read_text(encoding="utf-8"))
        owner_token = journal["owner_token"]
        copy_temporary = obs_bootstrap._transaction_copy_temporary_path(
            destination_executable,
            owner_token,
        )
        assert copy_temporary.read_bytes() == b"terminate-during-copy"
        process.terminate()
        process.join(15)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        child_exitcode = process.exitcode
        process.close()

    assert child_exitcode not in {None, 0}
    assert marker.exists()
    assert copy_temporary.exists()

    assert obs_bootstrap.migrate_legacy_obs_installation(destination, [source]) == source.resolve()

    assert destination_executable.read_bytes() == b"terminate-during-copy"
    assert not copy_temporary.exists()
    assert not marker.exists()


def test_live_lock_blocks_readiness_before_journal_is_written(tmp_path):
    destination = tmp_path / "obs-portable"
    lock = obs_bootstrap._OBSInterProcessLock(obs_bootstrap.get_obs_copy_lock_path(destination))

    assert lock.acquire() is True
    try:
        assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
        assert obs_bootstrap.is_obs_copy_in_progress(destination) is True
    finally:
        lock.release()

    assert obs_bootstrap.is_obs_copy_in_progress(destination) is False


def test_stale_journal_with_reused_live_pid_is_resumed_when_os_lock_is_free(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source, b"complete")
    _write_fake_obs(destination, b"partial")
    marker = _write_json_journal(destination, source)

    migrated = obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert migrated == source.resolve()
    assert not marker.exists()
    assert (destination / "bin" / "64bit" / "obs64.exe").read_bytes() == b"complete"


def test_obs_migration_keeps_retry_journal_until_copy_completes(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_file = _write_fake_obs(source)
    original_copy_file = obs_bootstrap._copy_inventory_file

    def fail_copy_file(*args, **kwargs):
        original_copy_file(*args, **kwargs)
        raise OSError("simulated copy failure")

    monkeypatch.setattr(obs_bootstrap, "_copy_inventory_file", fail_copy_file)
    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="copy failure"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert obs_bootstrap.is_obs_copy_in_progress(destination) is True
    assert source_file.is_file()
    journal = json.loads(obs_bootstrap.get_obs_copy_in_progress_marker(destination).read_text(encoding="utf-8"))
    assert Path(journal["source"]) == source.resolve()

    monkeypatch.setattr(obs_bootstrap, "_copy_inventory_file", original_copy_file)
    assert obs_bootstrap.migrate_legacy_obs_installation(destination, [source]) == source.resolve()

    assert obs_bootstrap.is_obs_copy_in_progress(destination) is False
    assert (destination / "bin" / "64bit" / "obs64.exe").read_bytes() == b"obs"


def test_obs_migration_rejects_marker_source_outside_allowed_candidates(tmp_path):
    allowed = tmp_path / "allowed"
    unauthorized = tmp_path / "unauthorized"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(allowed)
    _write_fake_obs(unauthorized)
    marker = _write_json_journal(destination, unauthorized)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="許可済みlegacy候補"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [allowed])

    assert marker.exists()
    assert not (destination / "bin" / "64bit" / "obs64.exe").exists()
    assert (unauthorized / "bin" / "64bit" / "obs64.exe").is_file()


def test_obs_migration_rejects_missing_marker_source_without_fallback(tmp_path):
    missing_source = tmp_path / "missing-legacy"
    fallback = tmp_path / "other-legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(fallback)
    marker = _write_json_journal(destination, missing_source)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="有効なポータブルOBS"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [missing_source, fallback])

    assert marker.exists()
    assert not (destination / "bin" / "64bit" / "obs64.exe").exists()


def test_obs_migration_rejects_corrupt_journal_and_keeps_it(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    destination.mkdir()
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    marker.write_text("{broken json", encoding="utf-8")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="壊れています"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert marker.read_text(encoding="utf-8") == "{broken json"


def test_obs_migration_rejects_oversized_journal_without_loading_it(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    destination.mkdir()
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    marker.write_bytes(b"{" * (obs_bootstrap.OBS_COPY_JOURNAL_MAX_BYTES + 1))

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="bytesを超えています"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert marker.stat().st_size == obs_bootstrap.OBS_COPY_JOURNAL_MAX_BYTES + 1


@pytest.mark.parametrize("failure_point", ["write", "fsync"])
def test_obs_journal_failure_removes_owned_temporary(monkeypatch, tmp_path, failure_point):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    owner_token = "a" * 32
    _write_fake_obs(source)
    destination.mkdir()
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    temporary = marker.with_name(f"{marker.name}.{owner_token}.tmp")

    def fail_after_partial_write(descriptor, payload):
        os.write(descriptor, payload[:8])
        raise OSError("simulated journal write failure")

    def fail_fsync(_descriptor):
        raise OSError("simulated journal fsync failure")

    if failure_point == "write":
        monkeypatch.setattr(obs_bootstrap, "_write_all", fail_after_partial_write)
    else:
        monkeypatch.setattr(obs_bootstrap.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match=f"journal {failure_point} failure"):
        obs_bootstrap._write_obs_migration_journal(
            marker,
            source,
            "0" * 64,
            owner_token,
        )

    assert not marker.exists()
    assert not temporary.exists()


@pytest.mark.parametrize("owner_token", ["A" * 32, "a" * 31, "../" + "a" * 29])
def test_obs_migration_rejects_noncanonical_owner_token(tmp_path, owner_token):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    outside = tmp_path / "outside-sentinel"
    _write_fake_obs(source)
    outside.write_bytes(b"keep outside")
    marker = _write_json_journal(destination, source, owner_token=owner_token)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="owner_token"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert marker.exists()
    assert outside.read_bytes() == b"keep outside"


def test_obs_migration_only_removes_journal_owned_by_its_token(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)

    def replace_owner(_destination: Path) -> None:
        _write_json_journal(destination, source, owner_token="b" * 32)

    with pytest.raises(obs_bootstrap.OBSMigrationError, match="所有者が変化"):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=replace_owner,
        )

    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    assert marker.exists()
    assert json.loads(marker.read_text(encoding="utf-8"))["owner_token"] == "b" * 32


def test_obs_migration_keeps_journal_when_finalize_marker_removal_is_denied(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source, b"source executable")
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    real_unlink = Path.unlink

    def deny_marker_unlink(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("simulated marker ACL denial")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_marker_unlink)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="marker.*解除"):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=lambda _destination: None,
        )

    assert marker.exists()
    assert obs_bootstrap.is_obs_copy_in_progress(destination) is True
    assert source_executable.read_bytes() == b"source executable"
    assert (destination / "bin" / "64bit" / "obs64.exe").read_bytes() == b"source executable"


def test_obs_migration_recovers_owned_copy_and_journal_temporaries(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    owner_token = "a" * 32
    source_executable = _write_fake_obs(source, b"complete executable")
    destination.mkdir()
    marker = _write_json_journal(destination, source, owner_token=owner_token)
    destination_executable = destination / source_executable.relative_to(source)
    destination_executable.parent.mkdir(parents=True)
    copy_temporary = obs_bootstrap._transaction_copy_temporary_path(
        destination_executable,
        owner_token,
    )
    copy_temporary.write_bytes(b"interrupted executable copy")
    journal_temporary = marker.with_name(f"{marker.name}.{owner_token}.tmp")
    journal_temporary.write_bytes(b"interrupted journal update")
    used_owner_tokens = []
    real_copy = obs_bootstrap._copy_inventory_file

    def capture_owner_token(source_path, destination_path, expected, current_owner_token):
        used_owner_tokens.append(current_owner_token)
        real_copy(source_path, destination_path, expected, current_owner_token)

    monkeypatch.setattr(obs_bootstrap, "_copy_inventory_file", capture_owner_token)

    assert obs_bootstrap.migrate_legacy_obs_installation(destination, [source]) == source.resolve()

    assert set(used_owner_tokens) == {owner_token}
    assert destination_executable.read_bytes() == b"complete executable"
    assert not copy_temporary.exists()
    assert not journal_temporary.exists()
    assert not marker.exists()


def test_obs_migration_recovers_owned_finalize_write_temporaries(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    owner_token = "a" * 32
    _write_fake_obs(source, b"complete executable")
    _write_fake_obs(destination, b"complete executable")
    marker = _write_json_journal(
        destination,
        source,
        owner_token=owner_token,
        phase=obs_bootstrap.OBS_MIGRATION_PHASE_FINALIZE_PENDING,
    )
    root_temporary = obs_bootstrap._transaction_write_temporary_path(
        obs_bootstrap.get_portable_marker_path(destination),
        owner_token,
    )
    root_temporary.write_bytes(b"")
    websocket_temporary = obs_bootstrap._transaction_write_temporary_path(
        obs_bootstrap.get_obs_websocket_config_path(destination),
        owner_token,
    )
    websocket_temporary.parent.mkdir(parents=True)
    websocket_temporary.write_text('{"server_password": "secret"}', encoding="utf-8")
    finalize_calls = 0

    def finalize(_destination: Path) -> None:
        nonlocal finalize_calls
        finalize_calls += 1

    assert (
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=finalize,
        )
        == source.resolve()
    )

    assert finalize_calls == 1
    assert not root_temporary.exists()
    assert not websocket_temporary.exists()
    assert not marker.exists()


def test_obs_migration_does_not_remove_hardlinked_owned_copy_temporary(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    external = tmp_path / "external-copy-temp"
    owner_token = "a" * 32
    source_executable = _write_fake_obs(source)
    _write_json_journal(destination, source, owner_token=owner_token)
    copy_temporary = obs_bootstrap._transaction_copy_temporary_path(
        destination / source_executable.relative_to(source),
        owner_token,
    )
    copy_temporary.parent.mkdir(parents=True)
    external.write_bytes(b"external temporary data")
    os.link(external, copy_temporary)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="hardlink"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert external.read_bytes() == b"external temporary data"
    assert copy_temporary.read_bytes() == b"external temporary data"


def test_obs_migration_does_not_follow_reparse_parent_to_owned_copy_temporary(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    external = tmp_path / "external-copy-directory"
    owner_token = "a" * 32
    source_executable = _write_fake_obs(source)
    _write_json_journal(destination, source, owner_token=owner_token)
    (destination / "bin").mkdir()
    external.mkdir()
    external_temporary = obs_bootstrap._transaction_copy_temporary_path(
        external / source_executable.name,
        owner_token,
    )
    external_temporary.write_bytes(b"external temporary data")
    _create_directory_link(destination / "bin" / "64bit", external)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="reparse point"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert external_temporary.read_bytes() == b"external temporary data"


def test_obs_migration_does_not_remove_other_owner_finalize_temporary(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    owner_token = "a" * 32
    other_owner_token = "b" * 32
    _write_fake_obs(source)
    _write_fake_obs(destination)
    marker = _write_json_journal(
        destination,
        source,
        owner_token=owner_token,
        phase=obs_bootstrap.OBS_MIGRATION_PHASE_FINALIZE_PENDING,
    )
    other_temporary = obs_bootstrap._transaction_write_temporary_path(
        obs_bootstrap.get_obs_websocket_config_path(destination),
        other_owner_token,
    )
    other_temporary.parent.mkdir(parents=True)
    other_temporary.write_text('{"server_password": "other secret"}', encoding="utf-8")
    finalize_called = False

    def unexpected_finalize(_destination: Path) -> None:
        nonlocal finalize_called
        finalize_called = True

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="transaction一時file"):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=unexpected_finalize,
        )

    assert finalize_called is False
    assert marker.exists()
    assert other_temporary.read_text(encoding="utf-8") == '{"server_password": "other secret"}'


def test_obs_migration_retries_finalize_after_copy_transaction_completes(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    finalize_calls = 0

    def finalize(current_destination: Path) -> None:
        nonlocal finalize_calls
        finalize_calls += 1
        assert obs_bootstrap.is_obs_copy_in_progress(current_destination) is True
        marker = obs_bootstrap.get_obs_copy_in_progress_marker(current_destination)
        assert json.loads(marker.read_text(encoding="utf-8"))["phase"] == (
            obs_bootstrap.OBS_MIGRATION_PHASE_FINALIZE_PENDING
        )
        managed_file = current_destination / "config" / "obs-studio" / "managed.ini"
        managed_file.parent.mkdir(parents=True, exist_ok=True)
        managed_file.write_text(f"attempt={finalize_calls}", encoding="utf-8")
        if finalize_calls == 1:
            raise ValueError("simulated bootstrap failure")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="bootstrap failure"):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=finalize,
        )

    assert obs_bootstrap.is_obs_copy_in_progress(destination) is True
    assert obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert (destination / "config" / "obs-studio" / "managed.ini").read_text(encoding="utf-8") == "attempt=1"

    migrated = obs_bootstrap.migrate_legacy_obs_installation(
        destination,
        [source],
        finalize_destination=finalize,
    )

    assert migrated == source.resolve()
    assert finalize_calls == 2
    assert obs_bootstrap.is_obs_copy_in_progress(destination) is False
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert (destination / "config" / "obs-studio" / "managed.ini").read_text(encoding="utf-8") == "attempt=2"


def test_obs_migration_capability_allows_bootstrap_finalizer_without_relocking(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    process_manager = FakeProcessManager()

    migrated = obs_bootstrap.migrate_legacy_obs_installation(
        destination,
        [source],
        finalize_destination=lambda current_destination: OBSBootstrapper(
            current_destination,
            process_manager=process_manager,
        ).apply(port=4455, password="secret-password"),
    )

    assert migrated == source.resolve()
    assert process_manager.kill_calls == 1
    assert obs_bootstrap.get_portable_marker_path(destination).is_file()
    assert obs_bootstrap.get_obs_global_ini_path(destination).is_file()
    assert obs_bootstrap.get_obs_user_ini_path(destination).is_file()
    assert obs_bootstrap.get_obs_websocket_config_path(destination).is_file()
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


def test_obs_migration_capability_rejects_bootstrap_of_another_destination(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    other_destination = tmp_path / "other-obs-portable"
    _write_fake_obs(source)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="別の管理destination"):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=lambda _destination: OBSBootstrapper(
                other_destination,
                process_manager=FakeProcessManager(),
            ).apply(),
        )

    assert obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert not other_destination.exists()


def test_obs_migration_rejects_unmanaged_change_while_finalize_is_pending(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source, b"expected executable")
    destination_executable = _write_fake_obs(destination, b"tampered executable")
    marker = _write_json_journal(
        destination,
        source,
        phase=obs_bootstrap.OBS_MIGRATION_PHASE_FINALIZE_PENDING,
    )
    finalize_called = False

    def unexpected_finalize(_destination: Path) -> None:
        nonlocal finalize_called
        finalize_called = True

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="bin/plugin"):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=unexpected_finalize,
        )

    assert finalize_called is False
    assert marker.exists()
    assert source_executable.read_bytes() == b"expected executable"
    assert destination_executable.read_bytes() == b"tampered executable"


def test_obs_migration_rejects_hardlinked_lock_without_modifying_external_file(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    external = tmp_path / "external-lock-target"
    _write_fake_obs(source)
    destination.mkdir()
    external.write_bytes(b"X")
    os.link(external, obs_bootstrap.get_obs_copy_lock_path(destination))

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="hardlink"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert external.read_bytes() == b"X"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert not (destination / "bin" / "64bit" / "obs64.exe").exists()


def test_obs_migration_rejects_reparse_lock_without_modifying_external_directory(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    external = tmp_path / "external-lock-directory"
    _write_fake_obs(source)
    destination.mkdir()
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("keep me", encoding="utf-8")
    _create_directory_link(obs_bootstrap.get_obs_copy_lock_path(destination), external)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="reparse point"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert sentinel.read_text(encoding="utf-8") == "keep me"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


def test_obs_migration_rejects_directory_at_lock_path(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    lock_path = obs_bootstrap.get_obs_copy_lock_path(destination)
    lock_path.mkdir(parents=True)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="通常ファイル"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert lock_path.is_dir()


def test_obs_migration_rejects_hardlinked_journal_without_reading_or_replacing_target(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    external = tmp_path / "external-journal-target"
    _write_fake_obs(source)
    destination.mkdir()
    external.write_text("external journal data", encoding="utf-8")
    os.link(external, obs_bootstrap.get_obs_copy_in_progress_marker(destination))

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="hardlink"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert external.read_text(encoding="utf-8") == "external journal data"


def test_obs_migration_rejects_hardlinked_destination_file_without_modifying_target(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    external = tmp_path / "external-plugin.dll"
    _write_fake_obs(source)
    plugin = source / "obs-plugins" / "64bit" / "plugin.dll"
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(b"new plugin")
    destination_plugin = destination / plugin.relative_to(source)
    destination_plugin.parent.mkdir(parents=True)
    external.write_bytes(b"external plugin")
    os.link(external, destination_plugin)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="hardlink"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert external.read_bytes() == b"external plugin"
    assert destination_plugin.read_bytes() == b"external plugin"
    assert obs_bootstrap.is_obs_copy_in_progress(destination) is True


def test_obs_migration_does_not_replace_existing_target_when_path_probe_is_denied(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source, b"new executable")
    destination_executable = _write_fake_obs(destination, b"original executable")
    marker = _write_json_journal(destination, source)
    real_lexists = obs_bootstrap._path_lexists
    real_replace = obs_bootstrap.os.replace
    replaced_destinations = []

    def deny_destination_probe(path):
        if Path(path) == destination_executable:
            raise PermissionError("simulated target probe ACL denial")
        return real_lexists(path)

    def track_replace(source_path, destination_path):
        replaced_destinations.append(Path(destination_path))
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(obs_bootstrap, "_path_lexists", deny_destination_probe)
    monkeypatch.setattr(obs_bootstrap.os, "replace", track_replace)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="target probe ACL denial"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert destination_executable not in replaced_destinations
    assert destination_executable.read_bytes() == b"original executable"
    assert source_executable.read_bytes() == b"new executable"
    assert marker.exists()


def test_copy_validation_failure_closes_source_descriptor(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    external = tmp_path / "external-obs.exe"
    source_executable = _write_fake_obs(source)
    destination_executable = destination / source_executable.relative_to(source)
    destination_executable.parent.mkdir(parents=True)
    external.write_bytes(b"external executable")
    os.link(external, destination_executable)
    expected = next(
        entry
        for entry in obs_bootstrap._build_obs_tree_inventory(source)
        if entry.kind == "file" and entry.relative_parts == ("bin", "64bit", "obs64.exe")
    )
    real_open = obs_bootstrap.os.open
    real_close = obs_bootstrap.os.close
    source_descriptors = []
    closed_descriptors = []

    def track_open(path, *args, **kwargs):
        descriptor = real_open(path, *args, **kwargs)
        if Path(path) == source_executable:
            source_descriptors.append(descriptor)
        return descriptor

    def track_close(descriptor):
        closed_descriptors.append(descriptor)
        return real_close(descriptor)

    monkeypatch.setattr(obs_bootstrap.os, "open", track_open)
    monkeypatch.setattr(obs_bootstrap.os, "close", track_close)

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="hardlink"):
        obs_bootstrap._copy_inventory_file(
            source_executable,
            destination_executable,
            expected,
            "a" * 32,
        )

    assert len(source_descriptors) == 1
    assert source_descriptors[0] in closed_descriptors
    assert external.read_bytes() == b"external executable"


def test_obs_migration_atomic_replace_preserves_raced_hardlink_target(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    external = tmp_path / "external-obsolete-obs.exe"
    source_executable = _write_fake_obs(source, b"new executable")
    destination_executable = _write_fake_obs(destination, b"old executable")
    marker = _write_json_journal(destination, source)
    real_replace = obs_bootstrap.os.replace

    def add_hardlink_immediately_before_replace(source_path, destination_path):
        if Path(destination_path) == destination_executable and not external.exists():
            os.link(destination_executable, external)
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(obs_bootstrap.os, "replace", add_hardlink_immediately_before_replace)

    assert obs_bootstrap.migrate_legacy_obs_installation(destination, [source]) == source.resolve()

    assert not marker.exists()
    assert source_executable.read_bytes() == b"new executable"
    assert destination_executable.read_bytes() == b"new executable"
    assert external.read_bytes() == b"old executable"


def test_obs_migration_rejects_destination_symlink_without_writing_outside(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    external = tmp_path / "external-directory"
    _write_fake_obs(source)
    plugin = source / "obs-plugins" / "64bit" / "plugin.dll"
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(b"new plugin")
    external.mkdir()
    external_file = external / "plugin.dll"
    external_file.write_bytes(b"external plugin")
    destination.mkdir()
    _create_directory_link(destination / "obs-plugins", external)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="reparse point"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert external_file.read_bytes() == b"external plugin"
    assert obs_bootstrap.is_obs_copy_in_progress(destination) is True


def test_obs_migration_rejects_source_symlink_without_reading_external_target(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    external = tmp_path / "external-source"
    _write_fake_obs(source)
    external.mkdir()
    external_file = external / "plugin.dll"
    external_file.write_bytes(b"external source")
    _create_directory_link(source / "obs-plugins", external)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="reparse point"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert external_file.read_bytes() == b"external source"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


def test_obs_migration_rejects_source_with_copy_in_progress_marker(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source)
    source_marker = obs_bootstrap.get_obs_copy_in_progress_marker(source)
    source_marker.write_text("unfinished source migration", encoding="utf-8")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="移行元にコピー中marker"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert source_marker.read_text(encoding="utf-8") == "unfinished source migration"
    assert source_executable.read_bytes() == b"obs"
    assert not destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="backslash is a separator on Windows")
def test_obs_migration_rejects_backslash_component_without_writing_outside_destination(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    external = tmp_path / "victim"
    _write_fake_obs(source)
    (source / "..\\victim").write_bytes(b"unsafe source")
    external.write_bytes(b"keep external")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="path component"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert external.read_bytes() == b"keep external"
    assert not destination.exists()


@pytest.mark.parametrize("reserved_name", sorted(obs_bootstrap.OBS_COPY_SKIP_NAMES))
def test_obs_inventory_skips_reserved_root_names_case_insensitively(tmp_path, reserved_name):
    source = tmp_path / "legacy"
    _write_fake_obs(source)
    mixed_case_name = "".join(character.upper() if character.islower() else character.lower() for character in reserved_name)
    reserved_file = source / mixed_case_name / "user-file.txt"
    reserved_file.parent.mkdir()
    reserved_file.write_text("reserved", encoding="utf-8")

    entries = obs_bootstrap._build_obs_tree_inventory(source)

    assert all(entry.relative_parts[0].casefold() != reserved_name.casefold() for entry in entries)


def test_obs_inventory_rejects_orphaned_journal_temporary(tmp_path):
    source = tmp_path / "legacy"
    _write_fake_obs(source)
    orphan = source / f"{obs_bootstrap.OBS_COPY_IN_PROGRESS_MARKER_NAME}.orphan.tmp"
    orphan.write_text("orphan", encoding="utf-8")

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="journal一時file"):
        obs_bootstrap._build_obs_tree_inventory(source)


def test_obs_migration_rejects_changed_stale_source_fingerprint(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source, b"original")
    partial_executable = _write_fake_obs(destination, b"partial")
    marker = _write_json_journal(destination, source)
    source_executable.write_bytes(b"replacement")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="移行元の内容が変化"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert marker.exists()
    assert partial_executable.read_bytes() == b"partial"


def test_obs_migration_rejects_destination_entry_absent_from_source(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source)
    extra = destination / "unrelated-user-file.txt"
    extra.parent.mkdir(parents=True)
    extra.write_text("keep me", encoding="utf-8")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="存在しないentry"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert source_executable.is_file()
    assert extra.read_text(encoding="utf-8") == "keep me"
    assert obs_bootstrap.is_obs_copy_in_progress(destination) is True


def test_obs_migration_detects_source_change_during_copy(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source, b"original")
    real_copy_file = obs_bootstrap._copy_inventory_file

    def change_then_copy(source_path, destination_path, expected, owner_token):
        source_path.write_bytes(b"changed during copy")
        real_copy_file(source_path, destination_path, expected, owner_token)

    monkeypatch.setattr(obs_bootstrap, "_copy_inventory_file", change_then_copy)
    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="コピー中"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert source_executable.read_bytes() == b"changed during copy"
    assert obs_bootstrap.is_obs_copy_in_progress(destination) is True


def test_obs_migration_detects_post_copy_extra_inventory(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    real_copy_file = obs_bootstrap._copy_inventory_file

    def copy_then_add_extra(source_path, destination_path, expected, owner_token):
        real_copy_file(source_path, destination_path, expected, owner_token)
        (destination / "unexpected.dll").write_bytes(b"unexpected")

    monkeypatch.setattr(obs_bootstrap, "_copy_inventory_file", copy_then_add_extra)
    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="双方向一致"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert (destination / "unexpected.dll").read_bytes() == b"unexpected"
    assert obs_bootstrap.is_obs_copy_in_progress(destination) is True


def test_obs_migration_rejects_plaintext_legacy_journal(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    destination.mkdir()
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    marker.write_text(str(source), encoding="utf-8")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="fingerprint"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert marker.read_text(encoding="utf-8") == str(source)


def test_obs_migration_wraps_lock_permission_error_with_recovery_guidance(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    lock_path = obs_bootstrap.get_obs_copy_lock_path(destination)
    _write_fake_obs(source)
    real_open = obs_bootstrap.os.open

    def deny_lock(path, *args, **kwargs):
        if Path(path) == lock_path:
            raise PermissionError("simulated permission denial")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(obs_bootstrap.os, "open", deny_lock)
    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="migration lock"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])


@pytest.mark.parametrize("probe_name", ["marker", "lock"])
def test_obs_migration_fails_closed_when_marker_or_lock_probe_is_denied(
    monkeypatch,
    tmp_path,
    probe_name,
):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    if probe_name == "marker":
        protected_path = _write_json_journal(destination, source)
    else:
        destination.mkdir()
        protected_path = obs_bootstrap.get_obs_copy_lock_path(destination)
        protected_path.write_bytes(b"\0")
    protected_before = protected_path.read_bytes()
    real_lexists = obs_bootstrap._path_lexists
    real_replace = obs_bootstrap.os.replace
    replace_calls = []
    finalize_called = False

    def deny_protected_probe(path):
        if Path(path) == protected_path:
            raise PermissionError(f"simulated {probe_name} probe ACL denial")
        return real_lexists(path)

    def track_replace(source_path, destination_path):
        replace_calls.append((Path(source_path), Path(destination_path)))
        return real_replace(source_path, destination_path)

    def unexpected_finalize(_destination):
        nonlocal finalize_called
        finalize_called = True

    monkeypatch.setattr(obs_bootstrap, "_path_lexists", deny_protected_probe)
    monkeypatch.setattr(obs_bootstrap.os, "replace", track_replace)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="probe ACL denial"):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=unexpected_finalize,
        )

    assert finalize_called is False
    assert replace_calls == []
    assert protected_path.read_bytes() == protected_before


def test_obs_migration_does_not_treat_existing_lock_acl_denial_as_contention(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    lock_path = obs_bootstrap.get_obs_copy_lock_path(destination)
    _write_fake_obs(source)
    destination.mkdir()
    lock_path.write_bytes(b"\0")
    real_open = obs_bootstrap.os.open

    def deny_existing_lock(path, *args, **kwargs):
        if Path(path) == lock_path:
            raise PermissionError(errno.EACCES, "simulated ACL denial", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(obs_bootstrap.os, "open", deny_existing_lock)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="migration lock"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])


def test_obs_migration_treats_windows_sharing_violation_as_contention(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    lock_path = obs_bootstrap.get_obs_copy_lock_path(destination)
    _write_fake_obs(source)
    destination.mkdir()
    lock_path.write_bytes(b"\0")
    real_open = obs_bootstrap.os.open

    def deny_shared_lock(path, *args, **kwargs):
        if Path(path) == lock_path:
            error = PermissionError(errno.EACCES, "simulated sharing violation", str(path))
            error.winerror = 32
            raise error
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(obs_bootstrap.os, "open", deny_shared_lock)

    with pytest.raises(obs_bootstrap.OBSMigrationInProgressError, match="別のプロセス"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])
