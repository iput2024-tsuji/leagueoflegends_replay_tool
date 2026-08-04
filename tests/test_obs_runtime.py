import asyncio
from pathlib import Path

import pytest

from src import obs_runtime, recordtest
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


@pytest.mark.parametrize(
    "error_type",
    [KeyboardInterrupt, SystemExit, asyncio.CancelledError],
)
def test_runtime_client_constructor_control_flow_cleans_launched_process_once(
    monkeypatch,
    error_type,
):
    launched_process = object()
    primary_error = error_type("client construction interrupted")
    cleanup_calls = []

    class ProcessManager:
        def __init__(self, obs_dir):
            pass

        def terminate_process(self, process):
            cleanup_calls.append(process)

    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: launched_process)
    monkeypatch.setattr(
        recordtest,
        "ObsWebSocketClient",
        lambda *args, **kwargs: (_ for _ in ()).throw(primary_error),
    )

    with pytest.raises(error_type) as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            auto_launch=True,
        )

    assert captured.value is primary_error
    assert cleanup_calls == [launched_process]


def test_runtime_handoff_construction_interruption_shuts_down_recorder_once(
    monkeypatch,
):
    recorder = FakeRecorder()
    launched_process = object()
    primary_error = SystemExit("runtime construction interrupted")
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: launched_process)
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", lambda *args, **kwargs: recorder)
    monkeypatch.setattr(
        obs_runtime,
        "RecorderRuntime",
        lambda *args, **kwargs: (_ for _ in ()).throw(primary_error),
    )

    with pytest.raises(SystemExit) as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            auto_launch=True,
        )

    assert captured.value is primary_error
    assert recorder.open_called == 1
    assert recorder.shutdown_called == 1


def test_runtime_control_flow_keeps_primary_when_raw_process_cleanup_fails(
    monkeypatch,
):
    launched_process = object()
    primary_error = KeyboardInterrupt("client construction interrupted")
    cleanup_error = OBSProcessQueryError("owned process cleanup failed")
    cleanup_calls = []

    class ProcessManager:
        def __init__(self, obs_dir):
            pass

        def terminate_process(self, process):
            cleanup_calls.append(process)
            raise cleanup_error

    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(recordtest, "test_obs_connection", lambda *args, **kwargs: (False, "down"))
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: False)
    monkeypatch.setattr(recordtest, "launch_obs", lambda config: launched_process)
    monkeypatch.setattr(
        recordtest,
        "ObsWebSocketClient",
        lambda *args, **kwargs: (_ for _ in ()).throw(primary_error),
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            auto_launch=True,
        )

    assert captured.value is primary_error
    assert cleanup_calls == [launched_process]
    assert any("owned process cleanup failed" in note for note in primary_error.__notes__)


def test_runtime_cleanup_control_flow_supersedes_normal_constructor_failure(
    monkeypatch,
):
    launched_process = object()
    primary_error = recordtest.RecorderError("recorder construction failed")
    first_cleanup_error = SystemExit("process cleanup interrupted")
    later_cleanup_error = KeyboardInterrupt("disconnect interrupted")
    cleanup_calls = []
    disconnect_calls = []

    class ProcessManager:
        def __init__(self, obs_dir):
            pass

        def terminate_process(self, process):
            cleanup_calls.append(process)
            raise first_cleanup_error

    class ObsClient:
        def disconnect(self):
            disconnect_calls.append("disconnect")
            raise later_cleanup_error

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

    with pytest.raises(SystemExit) as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            auto_launch=True,
        )

    assert captured.value is first_cleanup_error
    assert cleanup_calls == [launched_process]
    assert disconnect_calls == ["disconnect"]
    notes = getattr(first_cleanup_error, "__notes__", [])
    assert any("recorder construction failed" in note for note in notes)
    assert any("disconnect interrupted" in note for note in notes)


@pytest.mark.parametrize(
    ("stage", "error_type"),
    [
        ("client", KeyboardInterrupt),
        ("recorder", SystemExit),
        ("runtime", asyncio.CancelledError),
    ],
)
def test_runtime_force_launch_failure_cleans_existing_owned_obs_safely_once(
    monkeypatch,
    tmp_path,
    stage,
    error_type,
):
    primary_error = error_type(f"{stage} construction interrupted")
    original_cause = RuntimeError("construction cause")
    original_context = LookupError("construction context")
    primary_error.__cause__ = original_cause
    primary_error.__context__ = original_context
    primary_error.__suppress_context__ = True
    kill_calls = []
    terminate_calls = []
    pid_signal_calls = []
    disconnect_calls = []
    shutdown_calls = []

    class ProcessManager:
        def __init__(self, obs_dir):
            self.lease_path = tmp_path / ".lol_replay_obs_lease.json"
            self.lease_lock_path = tmp_path / ".lol_replay_obs_lease.lock"

        def kill_stale_owned_processes(self):
            kill_calls.append("lease-safe-cleanup")
            return [123]

        def terminate_process(self, process):
            terminate_calls.append(process)

        def _terminate_pid(self, pid, force=False):
            pid_signal_calls.append((pid, force))

    class ObsClient:
        def disconnect(self):
            disconnect_calls.append("disconnect")

    obs_client = ObsClient()

    class Recorder:
        _open_cleanup_attempted = False

        def open(self):
            pass

        def shutdown_obs(self):
            shutdown_calls.append("shutdown")
            obs_client.disconnect()

    recorder = Recorder()

    def construct_client(*args, **kwargs):
        if stage == "client":
            raise primary_error
        return obs_client

    def construct_recorder(*args, **kwargs):
        if stage == "recorder":
            raise primary_error
        return recorder

    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        recordtest,
        "launch_obs",
        lambda config: (_ for _ in ()).throw(AssertionError("no launch")),
    )
    monkeypatch.setattr(recordtest, "ObsWebSocketClient", construct_client)
    monkeypatch.setattr(recordtest, "LoLAutoRecorder", construct_recorder)
    if stage == "runtime":
        monkeypatch.setattr(
            obs_runtime,
            "RecorderRuntime",
            lambda *args, **kwargs: (_ for _ in ()).throw(primary_error),
        )

    with pytest.raises(error_type) as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            force_launch=True,
        )

    assert captured.value is primary_error
    assert primary_error.__cause__ is original_cause
    assert primary_error.__context__ is original_context
    assert primary_error.__suppress_context__ is True
    assert kill_calls == ["lease-safe-cleanup"]
    assert terminate_calls == []
    assert pid_signal_calls == []
    if stage == "client":
        assert disconnect_calls == []
        assert shutdown_calls == []
    elif stage == "recorder":
        assert disconnect_calls == ["disconnect"]
        assert shutdown_calls == []
    else:
        assert disconnect_calls == ["disconnect"]
        assert shutdown_calls == ["shutdown"]


def test_runtime_existing_owned_cleanup_control_flow_supersedes_normal_failure(
    monkeypatch,
    tmp_path,
):
    primary_error = recordtest.RecorderError("client construction failed")
    cleanup_error = SystemExit("owned lease cleanup interrupted")
    cleanup_cause = RuntimeError("owned cleanup cause")
    cleanup_error.__cause__ = cleanup_cause
    cleanup_error.__suppress_context__ = True
    kill_calls = []

    class ProcessManager:
        def __init__(self, obs_dir):
            self.lease_path = tmp_path / ".lol_replay_obs_lease.json"
            self.lease_lock_path = tmp_path / ".lol_replay_obs_lease.lock"

        def kill_stale_owned_processes(self):
            kill_calls.append("lease-safe-cleanup")
            raise cleanup_error

        def terminate_process(self, process):
            pytest.fail("existing owned cleanup must not use a Popen/PID fallback")

    monkeypatch.setattr(recordtest, "OBSProcessManager", ProcessManager)
    monkeypatch.setattr(recordtest, "wait_for_owned_obs_connection", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        recordtest,
        "ObsWebSocketClient",
        lambda *args, **kwargs: (_ for _ in ()).throw(primary_error),
    )

    with pytest.raises(SystemExit) as captured:
        OBSRuntimeManager().open_recorder(
            recordtest.AppConfig.from_dict({}),
            force_launch=True,
        )

    assert captured.value is cleanup_error
    assert cleanup_error.__cause__ is cleanup_cause
    assert cleanup_error.__suppress_context__ is True
    assert kill_calls == ["lease-safe-cleanup"]
    notes = getattr(cleanup_error, "__notes__", [])
    assert any("client construction failed" in note for note in notes)
    assert any("手動で終了" in note for note in notes)
    assert any(str((tmp_path / ".lol_replay_obs_lease.json").resolve()) in note for note in notes)
    assert any(str((tmp_path / ".lol_replay_obs_lease.lock").resolve()) in note for note in notes)


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
        lease_lock_path = Path("C:/managed/obs/.lol_replay_obs_lease.lock")

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
    assert str(FailingManager.lease_lock_path) in str(captured.value)
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


def test_existing_owned_runtime_keeps_disconnect_control_flow_and_runs_cleanup():
    disconnect_error = KeyboardInterrupt("disconnect interrupted")
    original_cause = RuntimeError("disconnect cause")
    original_context = LookupError("disconnect context")
    disconnect_error.__cause__ = original_cause
    disconnect_error.__context__ = original_context
    disconnect_error.__suppress_context__ = True
    disconnect_calls = []
    cleanup_calls = []

    class Recorder:
        def disconnect_obs(self):
            disconnect_calls.append("disconnect")
            raise disconnect_error

    class Manager:
        def kill_stale_owned_processes(self):
            cleanup_calls.append("lease-safe-cleanup")
            return [100]

    runtime = RecorderRuntime(
        recorder=Recorder(),
        owns_process=True,
        owns_existing_process=True,
        process_manager=Manager(),
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        runtime.close()

    assert captured.value is disconnect_error
    assert disconnect_error.__cause__ is original_cause
    assert disconnect_error.__context__ is original_context
    assert disconnect_error.__suppress_context__ is True
    assert disconnect_calls == ["disconnect"]
    assert cleanup_calls == ["lease-safe-cleanup"]
    assert any(
        "手動で終了" in note
        for note in getattr(disconnect_error, "__notes__", [])
    )


def test_existing_owned_runtime_cleanup_control_flow_supersedes_disconnect_failure():
    disconnect_error = OSError("disconnect failed")
    cleanup_error = SystemExit("lease cleanup interrupted")
    cleanup_cause = RuntimeError("lease cleanup cause")
    cleanup_error.__cause__ = cleanup_cause
    cleanup_error.__suppress_context__ = True
    cleanup_calls = []

    class Recorder:
        def disconnect_obs(self):
            raise disconnect_error

    class Manager:
        def kill_stale_owned_processes(self):
            cleanup_calls.append("lease-safe-cleanup")
            raise cleanup_error

    runtime = RecorderRuntime(
        recorder=Recorder(),
        owns_process=True,
        owns_existing_process=True,
        process_manager=Manager(),
    )

    with pytest.raises(SystemExit) as captured:
        runtime.close()

    assert captured.value is cleanup_error
    assert cleanup_error.__cause__ is cleanup_cause
    assert cleanup_error.__suppress_context__ is True
    assert cleanup_calls == ["lease-safe-cleanup"]
    notes = getattr(cleanup_error, "__notes__", [])
    assert any("disconnect failed" in note for note in notes)
    assert any("手動で終了" in note for note in notes)


def test_runtime_cleanup_control_flow_supersedes_normal_finalize_failure():
    recorder = FakeRecorder()
    recorder.fail_finalize = True
    cleanup_error = asyncio.CancelledError("runtime shutdown cancelled")
    cleanup_cause = RuntimeError("runtime shutdown cause")
    cleanup_error.__cause__ = cleanup_cause
    cleanup_error.__suppress_context__ = True
    recorder.shutdown_error = cleanup_error
    runtime = RecorderRuntime(
        recorder=recorder,
        owns_process=True,
    )

    with pytest.raises(asyncio.CancelledError) as captured:
        runtime.close(finalize_session=True)

    assert captured.value is cleanup_error
    assert cleanup_error.__cause__ is cleanup_cause
    assert cleanup_error.__suppress_context__ is True
    assert recorder.finalize_called == 1
    assert recorder.shutdown_called == 1
    assert any(
        "save failed" in note
        for note in getattr(cleanup_error, "__notes__", [])
    )


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
