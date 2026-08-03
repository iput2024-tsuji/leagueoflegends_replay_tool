import errno
import json
import multiprocessing
import os
import stat
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from src import obs_bootstrap
from src.obs_bootstrap import OBSBootstrapper
from src.obs_process import OBSProcessQueryError


@contextmanager
def _planned_config_write(root: Path, target: Path):
    with obs_bootstrap.obs_config_mutation_guard(root):
        token = obs_bootstrap._ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.set(
            frozenset({obs_bootstrap._filesystem_path_key(target.absolute())})
        )
        try:
            yield
        finally:
            obs_bootstrap._ACTIVE_OBS_SETTINGS_TRANSACTION_TARGETS.reset(token)


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


def _write_prejournal_temporary(
    destination: Path,
    source: Path,
    *,
    owner_token: str = "a" * 32,
    payload_updates: dict | None = None,
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
    if payload_updates:
        payload.update(payload_updates)
    temporary = obs_bootstrap._transaction_journal_temporary_path(marker, owner_token)
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    return temporary


def _hold_obs_migration_before_journal_rename(
    source: str,
    destination: str,
    entered,
    release,
    result_queue,
) -> None:
    original_replace = obs_bootstrap._OBSDirectoryLease.replace_open_file
    paused = False

    def pause_before_journal_rename(
        directory,
        descriptor,
        temporary_name,
        target_name,
    ):
        nonlocal paused
        if (
            not paused
            and target_name == obs_bootstrap.OBS_COPY_IN_PROGRESS_MARKER_NAME
        ):
            paused = True
            entered.set()
            if not release.wait(15):
                raise TimeoutError("test did not release journal rename")
        return original_replace(
            directory,
            descriptor,
            temporary_name,
            target_name,
        )

    obs_bootstrap._OBSDirectoryLease.replace_open_file = pause_before_journal_rename
    try:
        migrated = obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
        )
        result_queue.put(("ok", str(migrated)))
    except Exception as exc:  # pragma: no cover - reported to parent process
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


class FakeProcessManager:
    def __init__(self) -> None:
        self.kill_calls = 0
        self.query_calls = 0

    def query_obs_processes_strict(self):
        self.query_calls += 1
        return obs_bootstrap.OBSProcessQuerySnapshot(
            processes=(),
            queried_at=100.0 + self.query_calls,
        )

    def kill_stale_managed_processes(self, timeout_sec: float = 3.0) -> list[int]:
        self.kill_calls += 1
        return []

    def terminate_expected_obs_processes_strict(self, expected):
        killed_pids = tuple(self.kill_stale_managed_processes())
        after = self.query_obs_processes_strict()
        return obs_bootstrap.OBSStrictTerminationResult(
            tuple(process for process in expected if process.pid in killed_pids),
            after,
        )

    def has_managed_process(self) -> bool:
        return False

    def unmanaged_processes(self) -> list[object]:
        return []


def test_apply_stops_managed_obs_once(tmp_path):
    process_manager = FakeProcessManager()
    bootstrapper = OBSBootstrapper(tmp_path / "obs-portable", process_manager=process_manager)

    result = bootstrapper.apply()
    bootstrapper.apply()

    assert process_manager.kill_calls == 2
    assert Path(result["global_ini_path"]).exists()
    assert Path(result["user_ini_path"]).exists()


def test_apply_replans_noop_flush_and_returns_fresh_changed_flags(tmp_path):
    base_dir = (tmp_path / "obs-portable").absolute()
    OBSBootstrapper(base_dir, process_manager=FakeProcessManager()).apply(
        port=4455,
        password="secret-password",
        stop_managed_processes=False,
    )
    global_ini = obs_bootstrap.get_obs_global_ini_path(base_dir)

    class FlushingProcessManager(FakeProcessManager):
        def __init__(self):
            super().__init__()
            self.active = True
            self.process = obs_bootstrap.OBSProcessInfo(
                pid=4312,
                executable_path=base_dir / "bin" / "64bit" / "obs64.exe",
                creation_time=10.0,
            )

        def query_obs_processes_strict(self):
            self.query_calls += 1
            return obs_bootstrap.OBSProcessQuerySnapshot(
                processes=(self.process,) if self.active else (),
                queried_at=100.0 + self.query_calls,
            )

        def kill_stale_managed_processes(self, timeout_sec: float = 3.0):
            self.kill_calls += 1
            global_ini.write_bytes(
                b"[General]\nFirstRun=false\nExternalAfterStop=keep\n\n"
                b"[BasicWindow]\nSysTrayEnabled=false\n"
                b"SysTrayWhenStarted=false\nSysTrayMinimizeToTray=false\n"
            )
            self.active = False
            return [self.process.pid]

    process_manager = FlushingProcessManager()
    result = OBSBootstrapper(
        base_dir,
        process_manager=process_manager,
    ).apply(port=4455, password="secret-password")

    rendered = global_ini.read_text(encoding="utf-8")
    assert result["global_ini_changed"] is True
    assert result["user_ini_changed"] is False
    assert result["websocket"] == (
        False,
        obs_bootstrap.get_obs_websocket_config_path(base_dir),
    )
    assert result["global_ini_path"] == global_ini
    assert "FirstRun=true" in rendered
    assert "ExternalAfterStop=keep" in rendered
    assert process_manager.kill_calls == 1
    assert process_manager.query_calls == 3


@pytest.mark.parametrize("settings_api", ("global", "user", "websocket"))
def test_standalone_settings_api_replans_stop_flush_and_returns_fresh_result(
    tmp_path,
    settings_api,
):
    base_dir = (tmp_path / "obs-portable").absolute()
    OBSBootstrapper(base_dir, process_manager=FakeProcessManager()).apply(
        port=4455,
        password="old-password",
        stop_managed_processes=False,
    )
    targets = {
        "global": obs_bootstrap.get_obs_global_ini_path(base_dir),
        "user": obs_bootstrap.get_obs_user_ini_path(base_dir),
        "websocket": obs_bootstrap.get_obs_websocket_config_path(base_dir),
    }
    target = targets[settings_api]

    class FlushingProcessManager(FakeProcessManager):
        def __init__(self):
            super().__init__()
            self.active = True
            self.process = obs_bootstrap.OBSProcessInfo(
                pid=4312,
                executable_path=base_dir / "bin" / "64bit" / "obs64.exe",
                creation_time=10.0,
            )

        def query_obs_processes_strict(self):
            self.query_calls += 1
            return obs_bootstrap.OBSProcessQuerySnapshot(
                processes=(self.process,) if self.active else (),
                queried_at=100.0 + self.query_calls,
            )

        def kill_stale_managed_processes(self, timeout_sec=3.0):
            self.kill_calls += 1
            if settings_api == "websocket":
                target.write_text(
                    json.dumps(
                        {
                            "server_enabled": False,
                            "server_port": 4454,
                            "auth_required": False,
                            "server_password": "old-password",
                            "external_after_stop": "keep",
                        },
                        indent=4,
                    ),
                    encoding="utf-8",
                )
            else:
                target.write_bytes(
                    b"[General]\nFirstRun=false\nExternalAfterStop=keep\n\n"
                    b"[BasicWindow]\nSysTrayEnabled=true\n"
                    b"SysTrayWhenStarted=false\nSysTrayMinimizeToTray=false\n"
                )
            self.active = False
            return [self.process.pid]

    class CountingBootstrapper(OBSBootstrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.prepare_calls = 0

        def _prepare_obs_ini_write(self, ini_path, *, label):
            if settings_api in {"global", "user"}:
                self.prepare_calls += 1
            return super()._prepare_obs_ini_write(ini_path, label=label)

        def _prepare_websocket_write(self, port, password):
            if settings_api == "websocket":
                self.prepare_calls += 1
            return super()._prepare_websocket_write(port, password)

    process_manager = FlushingProcessManager()
    bootstrapper = CountingBootstrapper(
        base_dir,
        process_manager=process_manager,
    )
    if settings_api == "global":
        result = bootstrapper.ensure_global_ini()
    elif settings_api == "user":
        result = bootstrapper.ensure_user_ini()
    else:
        result = bootstrapper.ensure_websocket_config(4455, "old-password")

    assert result == (True, target)
    assert bootstrapper.prepare_calls == 2
    assert process_manager.kill_calls == 1
    assert process_manager.query_calls == 3
    if settings_api == "websocket":
        rendered = json.loads(target.read_text(encoding="utf-8"))
        assert rendered["server_enabled"] is True
        assert rendered["server_port"] == 4455
        assert rendered["auth_required"] is True
        assert rendered["external_after_stop"] == "keep"
    else:
        rendered = target.read_text(encoding="utf-8")
        assert "FirstRun=true" in rendered
        assert "SysTrayEnabled=false" in rendered
        assert "ExternalAfterStop=keep" in rendered
    assert not obs_bootstrap.get_obs_settings_transaction_marker(base_dir).exists()


@pytest.mark.parametrize("retry_outcome", ("query-error", "managed", "unmanaged"))
def test_apply_replan_fails_closed_on_retry_query_failure_or_reappearance(
    tmp_path,
    retry_outcome,
):
    base_dir = (tmp_path / "obs-portable").absolute()
    OBSBootstrapper(base_dir, process_manager=FakeProcessManager()).apply(
        port=4455,
        password="old-password",
        stop_managed_processes=False,
    )
    global_ini = obs_bootstrap.get_obs_global_ini_path(base_dir)

    class RetryFailureProcessManager(FakeProcessManager):
        def __init__(self):
            super().__init__()
            self.process = obs_bootstrap.OBSProcessInfo(
                pid=4312,
                executable_path=base_dir / "bin" / "64bit" / "obs64.exe",
                creation_time=10.0,
            )

        def query_obs_processes_strict(self):
            self.query_calls += 1
            if self.query_calls == 1:
                processes = (self.process,)
            elif self.query_calls == 2:
                processes = ()
            elif retry_outcome == "query-error":
                raise OBSProcessQueryError("retry query failed")
            elif retry_outcome == "managed":
                processes = (self.process,)
            else:
                processes = (
                    obs_bootstrap.OBSProcessInfo(
                        pid=9912,
                        executable_path=(tmp_path / "regular-obs" / "obs64.exe").absolute(),
                        creation_time=11.0,
                    ),
                )
            return obs_bootstrap.OBSProcessQuerySnapshot(
                processes=processes,
                queried_at=100.0 + self.query_calls,
            )

        def kill_stale_managed_processes(self, timeout_sec: float = 3.0):
            self.kill_calls += 1
            global_ini.write_bytes(
                b"[General]\nFirstRun=false\nExternalAfterStop=keep\n\n"
                b"[BasicWindow]\nSysTrayEnabled=false\n"
                b"SysTrayWhenStarted=false\nSysTrayMinimizeToTray=false\n"
            )
            return [self.process.pid]

    process_manager = RetryFailureProcessManager()
    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="再計画commit前"):
        OBSBootstrapper(base_dir, process_manager=process_manager).apply(
            port=4455,
            password="old-password",
        )

    assert process_manager.kill_calls == 1
    assert process_manager.query_calls == 3
    assert b"ExternalAfterStop=keep" in global_ini.read_bytes()
    assert not obs_bootstrap.get_obs_settings_transaction_marker(base_dir).exists()


def test_apply_replan_failure_chain_never_retains_requested_password(tmp_path):
    base_dir = (tmp_path / "obs-portable").absolute()
    OBSBootstrapper(base_dir, process_manager=FakeProcessManager()).apply(
        port=4455,
        password="old-password",
        stop_managed_processes=False,
    )
    global_ini = obs_bootstrap.get_obs_global_ini_path(base_dir)
    requested_password = "phase3-new-secret-must-not-leak"

    class RetryQueryFailureManager(FakeProcessManager):
        def __init__(self):
            super().__init__()
            self.process = obs_bootstrap.OBSProcessInfo(
                pid=4312,
                executable_path=base_dir / "bin" / "64bit" / "obs64.exe",
                creation_time=10.0,
            )

        def query_obs_processes_strict(self):
            self.query_calls += 1
            if self.query_calls == 3:
                raise OBSProcessQueryError("retry query failed")
            return obs_bootstrap.OBSProcessQuerySnapshot(
                processes=(self.process,) if self.query_calls == 1 else (),
                queried_at=100.0 + self.query_calls,
            )

        def kill_stale_managed_processes(self, timeout_sec: float = 3.0):
            self.kill_calls += 1
            global_ini.write_bytes(
                b"[General]\nFirstRun=false\nExternalAfterStop=keep\n\n"
                b"[BasicWindow]\nSysTrayEnabled=false\n"
                b"SysTrayWhenStarted=false\nSysTrayMinimizeToTray=false\n"
            )
            return [self.process.pid]

    with pytest.raises(obs_bootstrap.OBSPathSafetyError) as exc_info:
        OBSBootstrapper(
            base_dir,
            process_manager=RetryQueryFailureManager(),
        ).apply(port=4455, password=requested_password)

    pending = [exc_info.value]
    seen = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        diagnostic = f"{error!s}\n{error!r}\n{vars(error)!r}"
        assert requested_password not in diagnostic
        if error.__cause__ is not None:
            pending.append(error.__cause__)
        if error.__context__ is not None:
            pending.append(error.__context__)
    assert not obs_bootstrap.get_obs_settings_transaction_marker(base_dir).exists()


def test_standalone_ini_repairs_still_stop_managed_obs(tmp_path):
    process_manager = FakeProcessManager()
    bootstrapper = OBSBootstrapper(tmp_path / "obs-portable", process_manager=process_manager)

    bootstrapper.ensure_global_ini()
    bootstrapper.ensure_user_ini()

    assert process_manager.kill_calls == 2


def test_websocket_config_requires_password_authentication(tmp_path):
    process_manager = FakeProcessManager()
    bootstrapper = OBSBootstrapper(
        tmp_path / "obs-portable",
        process_manager=process_manager,
    )

    changed, config_path = bootstrapper.ensure_websocket_config(4455, "secret-password")

    text = config_path.read_text(encoding="utf-8")
    assert changed is True
    assert '"server_enabled": true' in text
    assert '"auth_required": true' in text
    assert '"server_password": "secret-password"' in text
    assert process_manager.kill_calls == 1


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


def test_preflighted_config_write_rejects_in_place_content_change(tmp_path):
    target = tmp_path / "user.ini"
    target.write_bytes(b"original")
    snapshot = obs_bootstrap.preflight_obs_config_file(target, label="user.ini")
    target.write_bytes(b"changed in place")

    with _planned_config_write(tmp_path, target):
        with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="内容が変化"):
            obs_bootstrap.write_preflighted_obs_config_file(snapshot, b"replacement")

    assert target.read_bytes() == b"changed in place"


def test_preflighted_config_write_requires_guard_and_planned_target(tmp_path):
    target = tmp_path / "user.ini"
    target.write_bytes(b"original")
    snapshot = obs_bootstrap.preflight_obs_config_file(target, label="user.ini")

    with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="mutation_guard"):
        obs_bootstrap.write_preflighted_obs_config_file(snapshot, b"replacement")

    with obs_bootstrap.obs_config_mutation_guard(tmp_path):
        with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="計画されていない"):
            obs_bootstrap.write_preflighted_obs_config_file(snapshot, b"replacement")

    assert target.read_bytes() == b"original"


def test_preflighted_config_write_rejects_identity_swap(tmp_path):
    target = tmp_path / "basic.ini"
    original = tmp_path / "original-basic.ini"
    target.write_bytes(b"original")
    snapshot = obs_bootstrap.preflight_obs_config_file(target, label="basic.ini")
    os.replace(target, original)
    target.write_bytes(b"intruder")

    with _planned_config_write(tmp_path, target):
        with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="identityが変化"):
            obs_bootstrap.write_preflighted_obs_config_file(snapshot, b"replacement")

    assert target.read_bytes() == b"intruder"
    assert original.read_bytes() == b"original"


def test_preflighted_config_replace_failure_preserves_old_bytes_and_cleans_temp(monkeypatch, tmp_path):
    target = tmp_path / "basic.ini"
    target.write_bytes(b"original")
    snapshot = obs_bootstrap.preflight_obs_config_file(target, label="basic.ini")
    real_replace = obs_bootstrap._OBSDirectoryLease.replace_open_file

    def fail_target_replace(directory, descriptor, temporary_name, target_name):
        if directory.path / target_name == target:
            raise PermissionError("simulated replace denial")
        return real_replace(directory, descriptor, temporary_name, target_name)

    monkeypatch.setattr(
        obs_bootstrap._OBSDirectoryLease,
        "replace_open_file",
        fail_target_replace,
    )

    with _planned_config_write(tmp_path, target):
        with pytest.raises(PermissionError, match="replace denial"):
            obs_bootstrap.write_preflighted_obs_config_file(snapshot, b"replacement")

    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob(".basic.ini.*.write.tmp")) == []


def test_preflighted_config_write_rejects_existing_temp_without_touching_external(monkeypatch, tmp_path):
    target = tmp_path / "basic.ini"
    target.write_bytes(b"original")
    snapshot = obs_bootstrap.preflight_obs_config_file(target, label="basic.ini")
    external = tmp_path / "external.ini"
    external.write_bytes(b"external")
    temporary = tmp_path / ".basic.ini.collision.write.tmp"
    os.link(external, temporary)
    monkeypatch.setattr(obs_bootstrap, "_safe_write_temporary_path", lambda _path: temporary)

    with _planned_config_write(tmp_path, target):
        with pytest.raises(FileExistsError):
            obs_bootstrap.write_preflighted_obs_config_file(snapshot, b"replacement")

    assert target.read_bytes() == b"original"
    assert external.read_bytes() == b"external"
    assert temporary.read_bytes() == b"external"


def test_preflighted_config_temp_identity_failure_preserves_destination(monkeypatch, tmp_path):
    target = tmp_path / "user.ini"
    target.write_bytes(b"original")
    snapshot = obs_bootstrap.preflight_obs_config_file(target, label="user.ini")
    real_identity = obs_bootstrap._OBSDirectoryLease._relative_file_identity
    temporary_validations = 0

    def fail_second_temp_validation(directory, name):
        nonlocal temporary_validations
        if name.startswith(".user.ini."):
            temporary_validations += 1
            if temporary_validations == 2:
                raise obs_bootstrap.OBSPathSafetyError("simulated temp identity race")
        return real_identity(directory, name)

    monkeypatch.setattr(
        obs_bootstrap._OBSDirectoryLease,
        "_relative_file_identity",
        fail_second_temp_validation,
    )

    with _planned_config_write(tmp_path, target):
        with pytest.raises(obs_bootstrap.OBSPathSafetyError, match="temp identity race"):
            obs_bootstrap.write_preflighted_obs_config_file(snapshot, b"replacement")

    assert target.read_bytes() == b"original"
    assert list(tmp_path.glob(".user.ini.*.write.tmp")) == []


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


def test_copy_progress_query_does_not_mutate_clean_destination(tmp_path):
    destination = tmp_path / "obs-portable"
    executable = _write_fake_obs(destination, b"clean destination")
    lock_path = obs_bootstrap.get_obs_copy_lock_path(destination)

    before = tuple(
        sorted(
            (
                path.relative_to(destination).as_posix(),
                "directory" if path.is_dir() else path.read_bytes(),
            )
            for path in destination.rglob("*")
        )
    )

    assert obs_bootstrap.is_obs_copy_in_progress(destination) is False

    after = tuple(
        sorted(
            (
                path.relative_to(destination).as_posix(),
                "directory" if path.is_dir() else path.read_bytes(),
            )
            for path in destination.rglob("*")
        )
    )
    assert after == before
    assert executable.read_bytes() == b"clean destination"
    assert not lock_path.exists()


def test_copy_progress_query_does_not_recreate_lock_after_probe_race(
    monkeypatch,
    tmp_path,
):
    destination = tmp_path / "obs-portable"
    executable = _write_fake_obs(destination, b"clean destination")
    lock_path = obs_bootstrap.get_obs_copy_lock_path(destination)
    real_probe = obs_bootstrap._OBSDirectoryLease.relative_file_identity_or_none
    simulated_lock_probes = 0

    def report_lock_once_then_missing(directory, name):
        nonlocal simulated_lock_probes
        if directory.path == destination.resolve() and name == lock_path.name:
            simulated_lock_probes += 1
            if simulated_lock_probes == 1:
                return (1, 2, stat.S_IFREG)
        return real_probe(directory, name)

    monkeypatch.setattr(
        obs_bootstrap._OBSDirectoryLease,
        "relative_file_identity_or_none",
        report_lock_once_then_missing,
    )

    assert obs_bootstrap.is_obs_copy_in_progress(destination) is False

    assert simulated_lock_probes >= 2
    assert executable.read_bytes() == b"clean destination"
    assert not lock_path.exists()


def test_copy_progress_query_does_not_initialize_zero_byte_stale_lock(tmp_path):
    destination = tmp_path / "obs-portable"
    executable = _write_fake_obs(destination, b"clean destination")
    lock_path = obs_bootstrap.get_obs_copy_lock_path(destination)
    lock_path.write_bytes(b"")
    before_paths = tuple(
        sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*"))
    )

    assert obs_bootstrap.is_obs_copy_in_progress(destination) is True

    assert lock_path.read_bytes() == b""
    assert executable.read_bytes() == b"clean destination"
    assert tuple(
        sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*"))
    ) == before_paths


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

    with pytest.raises(obs_bootstrap.OBSMigrationError, match="所有者が変化|allowlist外"):
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
    real_unlink = obs_bootstrap._OBSDirectoryLease.unlink_file

    def deny_marker_unlink(directory, name, *args, **kwargs):
        if directory.path == destination.resolve() and name == marker.name:
            raise PermissionError("simulated marker ACL denial")
        return real_unlink(directory, name, *args, **kwargs)

    monkeypatch.setattr(obs_bootstrap._OBSDirectoryLease, "unlink_file", deny_marker_unlink)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="marker"):
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

    def capture_owner_token(
        source_path,
        destination_path,
        expected,
        current_owner_token,
        **kwargs,
    ):
        used_owner_tokens.append(current_owner_token)
        real_copy(
            source_path,
            destination_path,
            expected,
            current_owner_token,
            **kwargs,
        )

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


def test_owned_temporary_is_kept_when_lock_validation_fails_before_unlink(tmp_path):
    destination = tmp_path / "obs-portable"
    owner_token = "a" * 32
    temporary = obs_bootstrap._transaction_write_temporary_path(
        obs_bootstrap.get_portable_marker_path(destination),
        owner_token,
    )
    temporary.parent.mkdir(parents=True)
    temporary.write_bytes(b"preserve")
    validation_calls = 0

    def fail_immediately_before_unlink() -> None:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 2:
            raise RuntimeError("simulated lock ownership loss")

    with obs_bootstrap._OBSDirectoryLease.open_absolute(
        destination,
        mutable=True,
    ) as root_lease:
        with pytest.raises(RuntimeError, match="ownership loss"):
            obs_bootstrap._remove_owned_transaction_temporaries(
                [temporary],
                root_lease=root_lease,
                validate_transaction=fail_immediately_before_unlink,
            )

    assert validation_calls == 2
    assert temporary.read_bytes() == b"preserve"


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
        managed_file = obs_bootstrap.get_obs_global_ini_path(current_destination)
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
    assert obs_bootstrap.get_obs_global_ini_path(destination).read_text(encoding="utf-8") == "attempt=1"

    migrated = obs_bootstrap.migrate_legacy_obs_installation(
        destination,
        [source],
        finalize_destination=finalize,
    )

    assert migrated == source.resolve()
    assert finalize_calls == 2
    assert obs_bootstrap.is_obs_copy_in_progress(destination) is False
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert obs_bootstrap.get_obs_global_ini_path(destination).read_text(encoding="utf-8") == "attempt=2"


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

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="bin/64bit/obs64.exe"):
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

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="migration lock"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert sentinel.read_text(encoding="utf-8") == "keep me"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


def test_obs_migration_rejects_directory_at_lock_path(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    lock_path = obs_bootstrap.get_obs_copy_lock_path(destination)
    lock_path.mkdir(parents=True)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="migration lock"):
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
    real_probe = obs_bootstrap._OBSDirectoryLease.relative_file_identity_or_none
    real_replace = obs_bootstrap._OBSDirectoryLease.replace_open_file
    replaced_destinations = []

    def deny_destination_probe(directory, name):
        if directory.path / name == destination_executable.resolve():
            raise PermissionError("simulated target probe ACL denial")
        return real_probe(directory, name)

    def track_replace(directory, descriptor, temporary_name, target_name):
        replaced_destinations.append(directory.path / target_name)
        return real_replace(directory, descriptor, temporary_name, target_name)

    monkeypatch.setattr(
        obs_bootstrap._OBSDirectoryLease,
        "relative_file_identity_or_none",
        deny_destination_probe,
    )
    monkeypatch.setattr(
        obs_bootstrap._OBSDirectoryLease,
        "replace_open_file",
        track_replace,
    )

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
    real_open = obs_bootstrap._OBSDirectoryLease.open_file
    real_close = obs_bootstrap.os.close
    source_descriptors = []
    closed_descriptors = []

    def track_open(directory, name, *args, **kwargs):
        descriptor = real_open(directory, name, *args, **kwargs)
        if directory.path / name == source_executable.resolve():
            source_descriptors.append(descriptor)
        return descriptor

    def track_close(descriptor):
        closed_descriptors.append(descriptor)
        return real_close(descriptor)

    monkeypatch.setattr(obs_bootstrap._OBSDirectoryLease, "open_file", track_open)
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
    real_replace = obs_bootstrap._OBSDirectoryLease.replace_open_file

    def add_hardlink_immediately_before_replace(
        directory,
        descriptor,
        temporary_name,
        target_name,
    ):
        if directory.path / target_name == destination_executable.resolve() and not external.exists():
            os.link(destination_executable, external)
        return real_replace(directory, descriptor, temporary_name, target_name)

    monkeypatch.setattr(
        obs_bootstrap._OBSDirectoryLease,
        "replace_open_file",
        add_hardlink_immediately_before_replace,
    )

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
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


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
    lock_path = obs_bootstrap.get_obs_copy_lock_path(destination)
    assert not destination.exists()
    assert not lock_path.exists()
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert obs_bootstrap.is_obs_copy_in_progress(destination) is False


@pytest.mark.parametrize("reserved_name", sorted(obs_bootstrap.OBS_COPY_SKIP_NAMES))
@pytest.mark.skipif(os.name != "nt", reason="Windows names are case-insensitive")
def test_obs_inventory_skips_reserved_root_names_case_insensitively(tmp_path, reserved_name):
    source = tmp_path / "legacy"
    _write_fake_obs(source)
    mixed_case_name = "".join(character.upper() if character.islower() else character.lower() for character in reserved_name)
    reserved_file = source / mixed_case_name / "user-file.txt"
    reserved_file.parent.mkdir()
    reserved_file.write_text("reserved", encoding="utf-8")

    entries = obs_bootstrap._build_obs_tree_inventory(source)

    assert all(
        not entry.relative_parts
        or entry.relative_parts[0].casefold() != reserved_name.casefold()
        for entry in entries
    )


@pytest.mark.parametrize("reserved_name", sorted(obs_bootstrap.OBS_COPY_SKIP_NAMES))
@pytest.mark.skipif(os.name == "nt", reason="POSIX names are case-sensitive")
def test_obs_inventory_keeps_differently_cased_reserved_names_on_posix(tmp_path, reserved_name):
    source = tmp_path / "legacy"
    _write_fake_obs(source)
    mixed_case_name = reserved_name.upper()
    reserved_file = source / mixed_case_name / "user-file.txt"
    reserved_file.parent.mkdir()
    reserved_file.write_text("reserved", encoding="utf-8")

    entries = obs_bootstrap._build_obs_tree_inventory(source)

    assert any(
        entry.relative_parts and entry.relative_parts[0] == mixed_case_name
        for entry in entries
    )


def test_obs_inventory_rejects_orphaned_journal_temporary(tmp_path):
    source = tmp_path / "legacy"
    _write_fake_obs(source)
    orphan = source / f"{obs_bootstrap.OBS_COPY_IN_PROGRESS_MARKER_NAME}.orphan.tmp"
    orphan.write_text("orphan", encoding="utf-8")

    with pytest.raises(
        obs_bootstrap.OBSPathSafetyError,
        match="journal一時file|transaction owner token",
    ):
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

    def change_then_copy(source_path, destination_path, expected, owner_token, **kwargs):
        source_path.write_bytes(b"changed during copy")
        real_copy_file(source_path, destination_path, expected, owner_token, **kwargs)

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

    def copy_then_add_extra(source_path, destination_path, expected, owner_token, **kwargs):
        real_copy_file(source_path, destination_path, expected, owner_token, **kwargs)
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
    real_open = obs_bootstrap._OBSDirectoryLease.open_file

    def deny_lock(directory, name, *args, **kwargs):
        if directory.path / name == lock_path.resolve():
            raise PermissionError("simulated permission denial")
        return real_open(directory, name, *args, **kwargs)

    monkeypatch.setattr(obs_bootstrap._OBSDirectoryLease, "open_file", deny_lock)
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
    real_probe = obs_bootstrap._OBSDirectoryLease.relative_file_identity_or_none
    real_replace = obs_bootstrap._OBSDirectoryLease.replace_open_file
    replace_calls = []
    finalize_called = False

    def deny_protected_probe(directory, name):
        if directory.path / name == protected_path.resolve():
            raise PermissionError(f"simulated {probe_name} probe ACL denial")
        return real_probe(directory, name)

    def track_replace(directory, descriptor, temporary_name, target_name):
        replace_calls.append((directory.path / temporary_name, directory.path / target_name))
        return real_replace(directory, descriptor, temporary_name, target_name)

    def unexpected_finalize(_destination):
        nonlocal finalize_called
        finalize_called = True

    monkeypatch.setattr(
        obs_bootstrap._OBSDirectoryLease,
        "relative_file_identity_or_none",
        deny_protected_probe,
    )
    monkeypatch.setattr(
        obs_bootstrap._OBSDirectoryLease,
        "replace_open_file",
        track_replace,
    )

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
    real_open = obs_bootstrap._OBSDirectoryLease.open_file

    def deny_existing_lock(directory, name, *args, **kwargs):
        if directory.path / name == lock_path.resolve():
            raise PermissionError(errno.EACCES, "simulated ACL denial", str(lock_path))
        return real_open(directory, name, *args, **kwargs)

    monkeypatch.setattr(obs_bootstrap._OBSDirectoryLease, "open_file", deny_existing_lock)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="migration lock"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])


def test_obs_migration_treats_windows_sharing_violation_as_contention(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    lock_path = obs_bootstrap.get_obs_copy_lock_path(destination)
    _write_fake_obs(source)
    destination.mkdir()
    lock_path.write_bytes(b"\0")
    real_open = obs_bootstrap._OBSDirectoryLease.open_file

    def deny_shared_lock(directory, name, *args, **kwargs):
        if directory.path / name == lock_path.resolve():
            error = PermissionError(errno.EACCES, "simulated sharing violation", str(lock_path))
            error.winerror = 32
            raise error
        return real_open(directory, name, *args, **kwargs)

    monkeypatch.setattr(obs_bootstrap._OBSDirectoryLease, "open_file", deny_shared_lock)

    with pytest.raises(obs_bootstrap.OBSMigrationInProgressError, match="別のプロセス"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])


def test_obs_migration_recovers_valid_prejournal_temporary(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source, b"pre-journal recovery")
    temporary = _write_prejournal_temporary(destination, source)

    migrated = obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert migrated == source.resolve()
    assert not temporary.exists()
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert (destination / source_executable.relative_to(source)).read_bytes() == b"pre-journal recovery"


def test_prejournal_scan_rejects_file_swapped_between_listing_and_relative_open(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    temporary = _write_prejournal_temporary(destination, source)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b"untrusted replacement")
    real_open = obs_bootstrap._OBSDirectoryLease.open_file
    swapped = False

    def swap_before_open(directory, name, *args, **kwargs):
        nonlocal swapped
        if not swapped and directory.path / name == temporary.resolve():
            swapped = True
            replacement.replace(temporary)
        return real_open(directory, name, *args, **kwargs)

    monkeypatch.setattr(obs_bootstrap._OBSDirectoryLease, "open_file", swap_before_open)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="identity"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert swapped is True
    assert temporary.read_bytes() == b"untrusted replacement"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


def test_prejournal_scan_rejects_entry_added_after_listing(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    temporary = _write_prejournal_temporary(destination, source)
    added = obs_bootstrap._transaction_journal_temporary_path(
        obs_bootstrap.get_obs_copy_in_progress_marker(destination),
        "b" * 32,
    )
    real_parse = obs_bootstrap._parse_transaction_temporary
    injected = False

    def add_after_listing(path):
        nonlocal injected
        if not injected and Path(path) == temporary:
            injected = True
            added.write_bytes(b"unverified")
        return real_parse(path)

    monkeypatch.setattr(obs_bootstrap, "_parse_transaction_temporary", add_after_listing)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="走査中"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert injected is True
    assert temporary.exists()
    assert added.read_bytes() == b"unverified"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


@pytest.mark.parametrize("case", ["empty", "token", "fingerprint", "source"])
def test_obs_migration_keeps_unverified_prejournal_temporary(tmp_path, case):
    source = tmp_path / "legacy"
    unauthorized = tmp_path / "unauthorized"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    _write_fake_obs(unauthorized, b"unauthorized")
    updates = {}
    if case == "token":
        updates["owner_token"] = "b" * 32
    elif case == "fingerprint":
        updates["source_fingerprint"] = "0" * 64
    elif case == "source":
        updates["source"] = str(unauthorized.resolve())
    temporary = _write_prejournal_temporary(
        destination,
        source,
        payload_updates=updates,
    )
    if case == "empty":
        temporary.write_bytes(b"")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert temporary.exists()
    assert (unauthorized / "bin" / "64bit" / "obs64.exe").read_bytes() == b"unauthorized"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


def test_obs_migration_keeps_multiple_prejournal_temporaries(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    first = _write_prejournal_temporary(destination, source, owner_token="a" * 32)
    second = _write_prejournal_temporary(destination, source, owner_token="b" * 32)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="複数"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert first.exists()
    assert second.exists()


def test_obs_migration_keeps_prejournal_temporary_when_source_is_missing(tmp_path):
    source = tmp_path / "legacy"
    missing = tmp_path / "missing"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    temporary = _write_prejournal_temporary(
        destination,
        source,
        payload_updates={"source": str(missing.resolve())},
    )

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [missing])

    assert temporary.exists()
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


@pytest.mark.parametrize("kind", ["copy", "write"])
def test_markerless_nested_transaction_temporary_is_visible_and_preserved(
    monkeypatch,
    tmp_path,
    kind,
):
    destination = tmp_path / "obs-portable"
    missing = tmp_path / "missing"
    _write_fake_obs(destination)
    target = destination / "config" / "obs-studio" / "global.ini"
    target.parent.mkdir(parents=True)
    temporary = (
        obs_bootstrap._transaction_copy_temporary_path(target, "a" * 32)
        if kind == "copy"
        else obs_bootstrap._transaction_write_temporary_path(target, "a" * 32)
    )
    temporary.write_bytes(b"preserve")
    sentinel = destination / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    opened_names = []
    real_open = obs_bootstrap._OBSDirectoryLease.open_file

    def track_open(directory, name, *args, **kwargs):
        opened_names.append(name)
        return real_open(directory, name, *args, **kwargs)

    monkeypatch.setattr(obs_bootstrap._OBSDirectoryLease, "open_file", track_open)

    assert obs_bootstrap.is_obs_copy_in_progress(destination) is True
    assert temporary.name not in opened_names
    assert not obs_bootstrap.get_obs_copy_lock_path(destination).exists()
    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="transaction一時"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [missing])

    assert temporary.read_bytes() == b"preserve"
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_prelock_name_scan_does_not_open_live_journal_temporary(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_hold_obs_migration_before_journal_rename,
        args=(str(source), str(destination), entered, release, result_queue),
    )
    process.start()
    try:
        assert entered.wait(15), "migration did not pause before journal rename"
        with pytest.raises(obs_bootstrap.OBSMigrationInProgressError):
            obs_bootstrap.migrate_legacy_obs_installation(destination, [source])
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
    assert not list(destination.rglob("*.tmp"))


def test_obs_migration_fails_before_mutation_without_handle_relative_support(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    monkeypatch.setattr(obs_bootstrap, "_supports_handle_relative_migration", lambda: False)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="directory handle"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert not destination.exists()


@pytest.mark.parametrize("relation", ["source_parent", "destination_parent"])
def test_obs_migration_rejects_overlapping_source_and_destination(tmp_path, relation):
    if relation == "source_parent":
        source = tmp_path / "legacy"
        destination = source / "obs-portable"
    else:
        destination = tmp_path / "obs-portable"
        source = destination / "legacy"
    source_executable = _write_fake_obs(source, b"keep")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="ancestor"):
        obs_bootstrap.migrate_legacy_obs_installation(destination, [source])

    assert source_executable.read_bytes() == b"keep"
    assert not obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()
    assert not obs_bootstrap.get_obs_copy_lock_path(destination).exists()


def test_obs_migration_rejects_finalizer_write_outside_exact_allowlist(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    unmanaged = destination / "config" / "obs-studio" / "managed.ini"

    def finalize(_destination: Path) -> None:
        unmanaged.write_text("forbidden", encoding="utf-8")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="allowlist外"):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=finalize,
        )

    assert unmanaged.read_text(encoding="utf-8") == "forbidden"
    assert obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


@pytest.mark.parametrize("field", ["phase", "source", "source_fingerprint"])
def test_obs_migration_rejects_finalizer_journal_changes(field, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    source_executable = _write_fake_obs(source, b"source stays intact")
    marker = obs_bootstrap.get_obs_copy_in_progress_marker(destination)
    original_owner_token = None

    def tamper_with_journal(_destination: Path) -> None:
        nonlocal original_owner_token
        payload = json.loads(marker.read_text(encoding="utf-8"))
        original_owner_token = payload["owner_token"]
        if field == "phase":
            payload[field] = obs_bootstrap.OBS_MIGRATION_PHASE_COPYING
        elif field == "source":
            payload[field] = str((tmp_path / "attacker-source").resolve())
        else:
            payload[field] = "0" * 64
        marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="allowlist外"):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=tamper_with_journal,
        )

    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert original_owner_token is not None
    assert payload["owner_token"] == original_owner_token
    assert source_executable.read_bytes() == b"source stays intact"
    assert (destination / source_executable.relative_to(source)).read_bytes() == (
        b"source stays intact"
    )


@pytest.mark.parametrize("guarded_root", [".lol_replay_obs_lease.json", "temp_appdata"])
def test_finalize_retry_rejects_prior_change_to_copy_skipped_root(tmp_path, guarded_root):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    guarded = destination / guarded_root

    def fail_after_change(_destination: Path) -> None:
        if guarded_root == "temp_appdata":
            guarded.mkdir()
            (guarded / "state.json").write_text("changed", encoding="utf-8")
        else:
            guarded.write_text("changed", encoding="utf-8")
        raise RuntimeError("simulated finalizer termination")

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match="termination"):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=fail_after_change,
        )

    retried = False

    def unexpected_retry(_destination: Path) -> None:
        nonlocal retried
        retried = True

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError, match=guarded_root):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=unexpected_retry,
        )

    assert retried is False
    assert obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


def test_finalizer_rejects_destination_ancestor_swap_without_reading_attacker_config(tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    parked = tmp_path / "parked-destination"
    attacker = tmp_path / "attacker"
    _write_fake_obs(source)
    attacker_global = obs_bootstrap.get_obs_global_ini_path(attacker)
    attacker_global.parent.mkdir(parents=True)
    attacker_global.write_text("[General]\nAttacker=true\n", encoding="utf-8")

    def swap_before_read(current_destination: Path) -> None:
        try:
            current_destination.rename(parked)
        except PermissionError as exc:
            raise RuntimeError("destination swap blocked") from exc
        attacker.rename(current_destination)
        try:
            OBSBootstrapper(
                current_destination,
                process_manager=FakeProcessManager(),
            ).ensure_global_ini(stop_managed_processes=False)
        finally:
            current_destination.rename(attacker)
            parked.rename(current_destination)

    with pytest.raises(obs_bootstrap.OBSMigrationRecoveryRequiredError):
        obs_bootstrap.migrate_legacy_obs_installation(
            destination,
            [source],
            finalize_destination=swap_before_read,
        )

    assert attacker_global.read_text(encoding="utf-8") == "[General]\nAttacker=true\n"
    destination_global = obs_bootstrap.get_obs_global_ini_path(destination)
    assert not destination_global.exists() or "Attacker=true" not in destination_global.read_text(
        encoding="utf-8"
    )
    assert obs_bootstrap.get_obs_copy_in_progress_marker(destination).exists()


def test_migration_inventory_calls_are_rooted(monkeypatch, tmp_path):
    source = tmp_path / "legacy"
    destination = tmp_path / "obs-portable"
    _write_fake_obs(source)
    real_inventory = obs_bootstrap._build_obs_tree_inventory
    rooted_calls = []

    def require_root_lease(root, **kwargs):
        rooted_calls.append(kwargs.get("root_lease"))
        assert kwargs.get("root_lease") is not None
        return real_inventory(root, **kwargs)

    monkeypatch.setattr(obs_bootstrap, "_build_obs_tree_inventory", require_root_lease)

    assert obs_bootstrap.migrate_legacy_obs_installation(destination, [source]) == source.resolve()
    assert rooted_calls


def test_copy_progress_probe_close_failure_still_releases_lock(monkeypatch, tmp_path):
    destination = tmp_path / "obs-portable"
    renamed = tmp_path / "renamed-obs-portable"
    _write_fake_obs(destination)
    destination_path = destination.resolve()
    real_close = obs_bootstrap._OBSDirectoryLease.close
    injected = False

    def fail_read_only_destination_probe_close(directory):
        nonlocal injected
        should_fail = (
            not injected
            and directory.path == destination_path
            and not directory.mutable
        )
        real_close(directory)
        if should_fail:
            injected = True
            raise OSError("simulated destination probe close failure")

    monkeypatch.setattr(
        obs_bootstrap._OBSDirectoryLease,
        "close",
        fail_read_only_destination_probe_close,
    )

    with pytest.raises(OSError, match="probe close failure"):
        obs_bootstrap.is_obs_copy_in_progress(destination)

    assert injected is True
    destination.rename(renamed)
    lock = obs_bootstrap._OBSInterProcessLock(
        obs_bootstrap.get_obs_copy_lock_path(renamed)
    )
    assert lock.acquire() is True
    lock.release()
