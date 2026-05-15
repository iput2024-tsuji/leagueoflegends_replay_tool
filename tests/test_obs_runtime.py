import pytest

from src import recordtest
from src.obs_runtime import OBSRuntimeManager


class FakeRecorder:
    def __init__(self, *args, **kwargs):
        self.open_called = 0
        self.shutdown_called = 0
        self.disconnect_called = 0
        self.finalize_called = 0
        self.fail_finalize = False

    def open(self):
        self.open_called += 1

    def shutdown_obs(self):
        self.shutdown_called += 1

    def disconnect_obs(self):
        self.disconnect_called += 1

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
