from pathlib import Path

import pytest

from src import recordtest
from src.obs_process import OBSProcessLeaseError, OBSProcessQueryError
from src.obs_runtime import OBSRuntimeManager, RecorderRuntime


class FakeRecorder:
    def __init__(self, *args, **kwargs):
        self.open_called = 0
        self.shutdown_called = 0
        self.disconnect_called = 0
        self.finalize_called = 0
        self.fail_finalize = False
        self.fail_disconnect = False
        self.open_error = None
        self.shutdown_error = None

    def open(self):
        self.open_called += 1
        if self.open_error is not None:
            raise self.open_error

    def shutdown_obs(self):
        self.shutdown_called += 1
        if self.shutdown_error is not None:
            raise self.shutdown_error

    def disconnect_obs(self):
        self.disconnect_called += 1
        if self.fail_disconnect:
            raise RuntimeError("disconnect failed")

    def finalize_session(self):
        self.finalize_called += 1
        if self.fail_finalize:
            raise RuntimeError("save failed")


def test_runtime_closes_owned_obs_process_with_shutdown(monkeypatch):
    recorder = FakeRecorder()
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: object())
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda *args, **kwargs: recorder)

    runtime = OBSRuntimeManager().open_recorder(recordtest.AppConfig.from_dict({}), auto_launch=True)
    runtime.close(finalize_session=True)

    assert runtime.owns_process is True
    assert recorder.open_called == 1
    assert recorder.finalize_called == 1
    assert recorder.shutdown_called == 1
    assert recorder.disconnect_called == 0


def test_runtime_closes_borrowed_obs_connection_with_disconnect(monkeypatch):
    recorder = FakeRecorder()
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(recordtest.OBSProcessManager, "has_owned_process", lambda self: True)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: (_ for _ in ()).throw(AssertionError("no launch")))
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda *args, **kwargs: recorder)

    runtime = OBSRuntimeManager().open_recorder(recordtest.AppConfig.from_dict({}), auto_launch=True)
    runtime.close()

    assert runtime.owns_process is False
    assert recorder.open_called == 1
    assert recorder.finalize_called == 0
    assert recorder.shutdown_called == 0
    assert recorder.disconnect_called == 1


def test_runtime_waits_for_starting_owned_obs_before_auto_launch(monkeypatch):
    recorder = FakeRecorder()
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "starting"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: True)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: (_ for _ in ()).throw(AssertionError("no launch")))
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda *args, **kwargs: recorder)

    runtime = OBSRuntimeManager().open_recorder(recordtest.AppConfig.from_dict({}), auto_launch=True)
    runtime.close()

    assert runtime.owns_process is False
    assert recorder.open_called == 1
    assert recorder.shutdown_called == 0
    assert recorder.disconnect_called == 1


def test_runtime_force_launch_can_take_over_existing_owned_obs(monkeypatch):
    recorder = FakeRecorder()
    kill_calls = []

    class FakeProcessManager:
        def __init__(self, obs_dir):
            self.obs_dir = obs_dir

        def kill_stale_owned_processes(self):
            kill_calls.append(self.obs_dir)
            return [123]

    monkeypatch.setattr(recordtest, "OBSProcessManager", FakeProcessManager)
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: True)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: (_ for _ in ()).throw(AssertionError("no launch")))
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda *args, **kwargs: recorder)

    runtime = OBSRuntimeManager().open_recorder(recordtest.AppConfig.from_dict({}), force_launch=True)
    runtime.close()

    assert runtime.owns_process is True
    assert runtime.owns_existing_process is True
    assert recorder.open_called == 1
    assert recorder.shutdown_called == 0
    assert recorder.disconnect_called == 1
    assert kill_calls == [recordtest.AppConfig.from_dict({}).obs.obs_dir]


def test_runtime_rejects_unowned_existing_obs_connection(monkeypatch):
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (True, "ok"))
    monkeypatch.setattr(recordtest.OBSProcessManager, "has_owned_process", lambda self: False)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: (_ for _ in ()).throw(AssertionError("no launch")))

    with pytest.raises(recordtest.RecorderError, match="管理対象OBSではありません"):
        OBSRuntimeManager().open_recorder(recordtest.AppConfig.from_dict({}), auto_launch=True)


def test_runtime_still_closes_obs_when_finalize_fails(monkeypatch):
    recorder = FakeRecorder()
    recorder.fail_finalize = True
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: object())
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda *args, **kwargs: recorder)

    runtime = OBSRuntimeManager().open_recorder(recordtest.AppConfig.from_dict({}), force_launch=True)

    try:
        runtime.close(finalize_session=True)
    except RuntimeError as exc:
        assert str(exc) == "save failed"
    else:
        raise AssertionError("finalize_session failure should be propagated")

    assert recorder.finalize_called == 1
    assert recorder.shutdown_called == 1
    assert recorder.disconnect_called == 0


def test_runtime_open_keeps_primary_and_notes_shutdown_failure(monkeypatch):
    recorder = FakeRecorder()
    primary_error = recordtest.RecorderError("recorder open failed")
    cleanup_error = OBSProcessQueryError("owned process cleanup failed")
    recorder.open_error = primary_error
    recorder.shutdown_error = cleanup_error
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: object())
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda *args, **kwargs: recorder)

    with pytest.raises(recordtest.RecorderError) as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            auto_launch=True,
        )

    assert captured.value is primary_error
    assert recorder.shutdown_called == 1
    assert any("owned process cleanup failed" in note for note in primary_error.__notes__)


def test_runtime_open_does_not_repeat_recorder_owned_cleanup(monkeypatch):
    recorder = FakeRecorder()
    primary_error = recordtest.RecorderError("recorder open failed")
    recorder.open_error = primary_error
    recorder._open_cleanup_attempted = True
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: object())
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda *args, **kwargs: recorder)

    with pytest.raises(recordtest.RecorderError) as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            auto_launch=True,
        )

    assert captured.value is primary_error
    assert recorder.shutdown_called == 0


def test_runtime_constructor_failure_cleans_launched_process_once(monkeypatch):
    launched_process = object()
    primary_error = recordtest.RecorderError("recorder constructor failed")
    cleanup_calls = []
    disconnect_calls = []

    class ProcessManager:
        def __init__(self, obs_dir):
            pass

        def terminate_process(self, process):
            cleanup_calls.append(process)

    class ObsClient:
        def disconnect(self):
            disconnect_calls.append("disconnect")

    obs_client = ObsClient()
    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: launched_process)
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", lambda *args, **kwargs: obs_client)
    monkeypatch.setattr(
        recordtest,
        "LoLAutoRecorder",
        lambda *args, **kwargs: (_ for _ in ()).throw(primary_error),
    )

    with pytest.raises(recordtest.RecorderError) as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            auto_launch=True,
        )

    assert captured.value is primary_error
    assert cleanup_calls == [launched_process]
    assert disconnect_calls == ["disconnect"]


def test_runtime_borrowed_constructor_failure_disconnects_partial_client(monkeypatch):
    primary_error = recordtest.RecorderError("recorder constructor failed")
    disconnect_calls = []

    class ProcessManager:
        def __init__(self, obs_dir):
            pass

        def terminate_process(self, process):
            raise AssertionError("borrowed startup must not terminate a process")

    class ObsClient:
        def disconnect(self):
            disconnect_calls.append("disconnect")

    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(recordtest, "is_tcp_port_open", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", lambda *args, **kwargs: ObsClient())
    monkeypatch.setattr(
        recordtest,
        "LoLAutoRecorder",
        lambda *args, **kwargs: (_ for _ in ()).throw(primary_error),
    )

    with pytest.raises(recordtest.RecorderError) as captured:
        OBSRuntimeManager().open_recorder(recordtest.AppConfig.from_dict({}))

    assert captured.value is primary_error
    assert disconnect_calls == ["disconnect"]


def test_runtime_borrowed_open_base_exception_runs_cleanup_and_keeps_primary(monkeypatch):
    class StartupAbort(BaseException):
        pass

    recorder = FakeRecorder()
    primary_error = StartupAbort("recorder open aborted")
    cleanup_error = OBSProcessQueryError("borrowed connection cleanup failed")
    recorder.open_error = primary_error
    recorder.shutdown_error = cleanup_error
    monkeypatch.setattr(recordtest, "is_tcp_port_open", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda *args, **kwargs: recorder)

    with pytest.raises(StartupAbort) as captured:
        OBSRuntimeManager().open_recorder(recordtest.AppConfig.from_dict({}))

    assert captured.value is primary_error
    assert recorder.shutdown_called == 1
    assert any("borrowed connection cleanup failed" in note for note in primary_error.__notes__)


def test_runtime_constructor_failure_notes_process_cleanup_failure(monkeypatch):
    launched_process = object()
    primary_error = recordtest.RecorderError("recorder constructor failed")
    cleanup_error = OBSProcessQueryError("owned process cleanup failed")
    cleanup_calls = []

    class ProcessManager:
        def __init__(self, obs_dir):
            pass

        def terminate_process(self, process):
            cleanup_calls.append(process)
            raise cleanup_error

    class ObsClient:
        def disconnect(self):
            pass

    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: launched_process)
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", lambda *args, **kwargs: ObsClient())
    monkeypatch.setattr(
        recordtest,
        "LoLAutoRecorder",
        lambda *args, **kwargs: (_ for _ in ()).throw(primary_error),
    )

    with pytest.raises(recordtest.RecorderError) as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            auto_launch=True,
        )

    assert captured.value is primary_error
    assert cleanup_calls == [launched_process]
    assert any("owned process cleanup failed" in note for note in primary_error.__notes__)


def test_existing_owned_runtime_propagates_cleanup_failure_with_manual_guidance():
    recorder = FakeRecorder()
    calls = []

    class FailingManager:
        lease_path = Path("C:/managed/obs/.lol_replay_obs_lease.json")

        def kill_stale_owned_processes(self):
            calls.append("cleanup")
            raise OBSProcessQueryError("strict cleanup failed")

    runtime = RecorderRuntime(
        recorder=recorder,
        owns_process=True,
        owns_existing_process=True,
        process_manager=FailingManager(),
    )

    with pytest.raises(recordtest.RecorderError, match="手動で終了") as captured:
        runtime.close()

    assert recorder.disconnect_called == 1
    assert calls == ["cleanup"]
    assert isinstance(captured.value.__cause__, OBSProcessQueryError)
    assert str(FailingManager.lease_path) in str(captured.value)
    assert "退避または削除" in str(captured.value)


def test_existing_owned_runtime_runs_cleanup_even_when_disconnect_fails():
    recorder = FakeRecorder()
    recorder.fail_disconnect = True
    calls = []

    class Manager:
        def kill_stale_owned_processes(self):
            calls.append("cleanup")
            return [100]

    runtime = RecorderRuntime(
        recorder=recorder,
        owns_process=True,
        owns_existing_process=True,
        process_manager=Manager(),
    )

    with pytest.raises(recordtest.RecorderError, match="安全に終了") as captured:
        runtime.close()

    assert recorder.disconnect_called == 1
    assert calls == ["cleanup"]
    assert isinstance(captured.value.__cause__, RuntimeError)


def test_runtime_preserves_finalize_error_and_records_cleanup_failure():
    recorder = FakeRecorder()
    recorder.fail_finalize = True

    class FailingManager:
        def kill_stale_owned_processes(self):
            raise OBSProcessQueryError("strict cleanup failed")

    runtime = RecorderRuntime(
        recorder=recorder,
        owns_process=True,
        owns_existing_process=True,
        process_manager=FailingManager(),
    )

    with pytest.raises(RuntimeError, match="save failed") as captured:
        runtime.close(finalize_session=True)

    assert recorder.finalize_called == 1
    assert recorder.disconnect_called == 1
    notes = getattr(captured.value, "__notes__", [])
    assert any("cleanup also failed" in note for note in notes)


def test_runtime_wraps_legacy_owned_lease_error_with_manual_guidance(monkeypatch):
    class ProcessManager:
        def __init__(self, obs_dir):
            self.obs_dir = obs_dir
            self.lease_path = Path(obs_dir).resolve() / ".lol_replay_obs_lease.json"

    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(
        recordtest,
        "wait_for_owned_obs_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OBSProcessLeaseError("legacy live lease")
        ),
    )

    with pytest.raises(recordtest.RecorderError, match="手動で終了") as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            force_launch=True,
        )

    assert isinstance(captured.value.__cause__, OBSProcessLeaseError)
    assert str(ProcessManager(recordtest.AppConfig.from_dict({}).obs.obs_dir).lease_path) in str(
        captured.value
    )
    assert "退避または削除" in str(captured.value)


def test_runtime_wraps_live_handle_os_error_with_manual_guidance(monkeypatch):
    class ProcessManager:
        def __init__(self, obs_dir):
            self.lease_path = Path(obs_dir).resolve() / ".lol_replay_obs_lease.json"

        def has_owned_process(self):
            raise OSError(5, "OpenProcess access denied")

    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(
        recordtest,
        "test_obs_connection",
        lambda *args, **kwargs: (True, "connected"),
    )

    with pytest.raises(recordtest.RecorderError, match="手動で終了") as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            auto_launch=True,
        )

    assert isinstance(captured.value.__cause__, OSError)
    assert "所有情報ファイル:" in str(captured.value)


def test_runtime_wraps_wait_for_owned_os_error_with_manual_guidance(monkeypatch):
    class ProcessManager:
        def __init__(self, obs_dir):
            self.lease_path = Path(obs_dir).resolve() / ".lol_replay_obs_lease.json"

    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(
        recordtest,
        "wait_for_owned_obs_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError(5, "handle identity denied")
        ),
    )

    with pytest.raises(recordtest.RecorderError, match="手動で終了") as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            force_launch=True,
        )

    assert isinstance(captured.value.__cause__, OSError)
    assert "所有情報ファイル:" in str(captured.value)


def test_runtime_cleanup_holds_obs_operation_lock(monkeypatch):
    recorder = FakeRecorder()

    class TrackingLock:
        active = False

        def __enter__(self):
            assert not self.active
            self.active = True
            return self

        def __exit__(self, exc_type, exc, traceback):
            assert self.active
            self.active = False

    lock = TrackingLock()

    class Manager:
        def kill_stale_owned_processes(self):
            assert lock.active
            return [100]

    monkeypatch.setattr(recordtest, "OBS_OPERATION_LOCK", lock)
    runtime = RecorderRuntime(
        recorder=recorder,
        owns_process=True,
        owns_existing_process=True,
        process_manager=Manager(),
    )

    runtime.close()

    assert recorder.disconnect_called == 1
    assert lock.active is False
