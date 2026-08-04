import asyncio
from types import SimpleNamespace

import pytest

from src import recordtest
from src.recording_state import RecordingOutcome
from src.recording_supervisor import RecordingSupervisor


class FakeConfigController:
    def __init__(self, report=None):
        self.saved = []
        self.report = report or {
            "config": {"ok": True},
            "changed": True,
            "notes": ["defaults applied"],
            "warnings": ["mpv missing"],
            "errors": [],
        }

    def load_config(self):
        return {"loaded": True}

    def run_preflight(self, config_data, auto_fix=True, force_obs_detect=True):
        return self.report

    def save_config(self, data):
        self.saved.append(data)


class FakeRecordingController:
    def __init__(self, recorder):
        self.recorder = recorder
        self.created_with = None
        self.runtime = FakeRuntime(recorder)

    def create_runtime(self, config_data, status_cb=None):
        self.created_with = (config_data, status_cb)
        return self.runtime


class FakeRuntime:
    def __init__(self, recorder):
        self.recorder = recorder
        self.close_calls = []

    def close(self, finalize_session=False):
        self.close_calls.append(finalize_session)
        self.recorder.calls.append("runtime_close")
        if finalize_session:
            self.recorder.finalize_session()


class FakeRecorder:
    def __init__(self):
        self.config = SimpleNamespace()
        self.calls = []
        self.stop_event = None
        self.wait_count = 0
        self.session_has_data = False
        self.failure_reason = None
        self.finalize_outcomes = []
        self.session_finalized = False

    def set_stop_event(self, stop_event):
        self.stop_event = stop_event
        self.calls.append("set_stop_event")

    def apply_audio_profile(self, config):
        self.calls.append("apply_audio_profile")

    def reset_session(self):
        self.calls.append("reset_session")
        self.session_has_data = False
        self.session_finalized = False

    async def wait_for_game_start_async(self):
        self.calls.append("wait_for_game_start_async")
        self.wait_count += 1
        return self.wait_count == 1

    async def start_recording_async(self):
        self.calls.append("start_recording_async")
        self.session_has_data = True

    async def record_until_end_async(self):
        self.calls.append("record_until_end_async")
        return RecordingOutcome.COMPLETED

    async def wait_for_previous_game_clear_async(self):
        self.calls.append("wait_for_previous_game_clear_async")
        return True

    def stop_recording(self):
        self.calls.append("stop_recording")

    def has_session_data(self):
        return self.session_has_data

    def save_json(self):
        self.calls.append("save_json")
        self.session_has_data = False
        self.session_finalized = True

    def finalize_session(self, outcome=None, failure_reason=None):
        self.calls.append("finalize_session")
        self.finalize_outcomes.append(outcome)
        if failure_reason:
            self.failure_reason = str(failure_reason)
        if self.session_has_data:
            self.save_json()
        return SimpleNamespace(success=True, error=None)

    def mark_session_failed(self, reason):
        self.calls.append("mark_session_failed")
        self.failure_reason = str(reason)

    def mark_session_aborted(self, reason):
        self.calls.append("mark_session_aborted")
        self.failure_reason = str(reason)

    def defer_current_game_until_clear(self):
        self.calls.append("defer_current_game_until_clear")

    def request_stop(self):
        self.calls.append("request_stop")

    def shutdown_obs(self):
        self.calls.append("shutdown_obs")


def run(coro):
    return asyncio.run(coro)


async def run_supervisor(supervisor):
    await supervisor.run(asyncio.Event())


def test_recording_supervisor_runs_one_session_and_cleans_up():
    statuses = []
    notifications = []
    recorder = FakeRecorder()
    config_controller = FakeConfigController()
    recording_controller = FakeRecordingController(recorder)
    supervisor = RecordingSupervisor(
        config_controller=config_controller,
        recording_controller=recording_controller,
        status_cb=statuses.append,
        notification_cb=lambda *args: notifications.append(args),
    )

    run(run_supervisor(supervisor))

    assert config_controller.saved == [{"ok": True}]
    assert recording_controller.created_with[0] == {"ok": True}
    assert "🛠️ defaults applied" in statuses
    assert "⚠️ mpv missing" in statuses
    assert "🔊 音声設定をOBSへ適用しました。" in statuses
    assert "✅ 試合記録完了。次の試合を待機します。" in statuses
    assert [item[0] for item in notifications] == ["recording_started", "recording_completed"]
    assert recorder.calls == [
        "set_stop_event",
        "apply_audio_profile",
        "reset_session",
        "wait_for_game_start_async",
        "start_recording_async",
        "record_until_end_async",
        "finalize_session",
        "save_json",
        "wait_for_previous_game_clear_async",
        "reset_session",
        "wait_for_game_start_async",
        "request_stop",
        "stop_recording",
        "runtime_close",
    ]
    assert recording_controller.runtime.close_calls == [False]


def test_recording_supervisor_keeps_body_primary_when_shutdown_fails():
    primary_error = recordtest.RecorderError("wait for game failed")
    cleanup_error = RuntimeError("owned OBS cleanup failed")

    class FailingRecorder(FakeRecorder):
        async def wait_for_game_start_async(self):
            self.calls.append("wait_for_game_start_async")
            raise primary_error

    recorder = FailingRecorder()
    recording_controller = FakeRecordingController(recorder)

    def fail_close(finalize_session=False):
        recording_controller.runtime.close_calls.append(finalize_session)
        recorder.calls.append("runtime_close")
        raise cleanup_error

    recording_controller.runtime.close = fail_close
    supervisor = RecordingSupervisor(
        config_controller=FakeConfigController(),
        recording_controller=recording_controller,
    )

    with pytest.raises(recordtest.RecorderError) as captured:
        run(run_supervisor(supervisor))

    assert captured.value is primary_error
    assert recording_controller.runtime.close_calls == [False]
    assert any("owned OBS cleanup failed" in note for note in primary_error.__notes__)
    assert any("手動で終了" in note for note in primary_error.__notes__)


def test_recording_supervisor_notifies_completion_after_game_process_clears():
    notifications = []
    recorder = FakeRecorder()
    supervisor = RecordingSupervisor(
        config_controller=FakeConfigController(),
        recording_controller=FakeRecordingController(recorder),
        notification_cb=lambda *args: notifications.append((args[0], list(recorder.calls))),
    )

    run(run_supervisor(supervisor))

    completed = next(item for item in notifications if item[0] == "recording_completed")
    assert "wait_for_previous_game_clear_async" in completed[1]
    assert completed[1].index("wait_for_previous_game_clear_async") > completed[1].index("save_json")


def test_recording_supervisor_raises_preflight_errors_without_creating_recorder():
    report = {"config": {}, "changed": False, "notes": [], "warnings": [], "errors": ["OBS missing"]}
    recorder = FakeRecorder()
    recording_controller = FakeRecordingController(recorder)
    supervisor = RecordingSupervisor(
        config_controller=FakeConfigController(report=report),
        recording_controller=recording_controller,
    )

    with pytest.raises(recordtest.RecorderError, match="OBS missing"):
        run(run_supervisor(supervisor))

    assert recording_controller.created_with is None
    assert recorder.calls == []
    assert recording_controller.runtime.close_calls == []


def test_recording_supervisor_does_not_finalize_cancelled_session():
    class CancelledRecorder(FakeRecorder):
        async def record_until_end_async(self):
            self.calls.append("record_until_end_async")
            return RecordingOutcome.CANCELLED

    statuses = []
    recorder = CancelledRecorder()
    recording_controller = FakeRecordingController(recorder)
    supervisor = RecordingSupervisor(
        config_controller=FakeConfigController(),
        recording_controller=recording_controller,
        status_cb=statuses.append,
    )

    run(run_supervisor(supervisor))

    assert "⏹️ 録画セッションを中断ログとして保存しました。" in statuses
    assert "save_json" in recorder.calls
    assert recorder.finalize_outcomes == [RecordingOutcome.ABORTED]
    assert recorder.failure_reason == "recording was cancelled"
    assert recorder.calls[-2:] == ["request_stop", "runtime_close"]
    assert recording_controller.runtime.close_calls == [False]


def test_recording_supervisor_saves_partial_session_when_recording_fails():
    class FailingRecorder(FakeRecorder):
        async def record_until_end_async(self):
            self.calls.append("record_until_end_async")
            raise RuntimeError("sync marker failed")

    statuses = []
    notifications = []
    recorder = FailingRecorder()
    recording_controller = FakeRecordingController(recorder)
    supervisor = RecordingSupervisor(
        config_controller=FakeConfigController(),
        recording_controller=recording_controller,
        status_cb=statuses.append,
        notification_cb=lambda *args: notifications.append(args),
    )

    run(run_supervisor(supervisor))

    assert "⚠️ 録画セッションを部分保存しました。" in statuses
    assert "⚠️ 録画エラーが発生したため、この試合中の再試行を停止します。" in statuses
    assert "⚠️ 録画エラー後も次の試合監視を継続します。" in statuses
    assert "mark_session_failed" in recorder.calls
    assert "defer_current_game_until_clear" in recorder.calls
    assert "wait_for_previous_game_clear_async" in recorder.calls
    assert "save_json" in recorder.calls
    assert recorder.finalize_outcomes == [RecordingOutcome.FAILED_PARTIAL]
    assert recorder.failure_reason == "sync marker failed"
    assert recorder.calls.count("wait_for_game_start_async") == 2
    assert recorder.calls[-3:] == ["request_stop", "stop_recording", "runtime_close"]
    assert recording_controller.runtime.close_calls == [False]
    assert [item[0] for item in notifications] == ["recording_started", "recording_failed"]


def test_recording_supervisor_does_not_emit_started_notification_when_start_fails():
    class StartFailingRecorder(FakeRecorder):
        async def start_recording_async(self):
            self.calls.append("start_recording_async")
            raise recordtest.RecorderError("OBS busy")

    notifications = []
    recorder = StartFailingRecorder()
    recording_controller = FakeRecordingController(recorder)
    supervisor = RecordingSupervisor(
        config_controller=FakeConfigController(),
        recording_controller=recording_controller,
        notification_cb=lambda *args: notifications.append(args),
    )

    run(run_supervisor(supervisor))

    assert [item[0] for item in notifications] == ["recording_failed"]
    assert recorder.calls.count("wait_for_game_start_async") == 2
    assert "defer_current_game_until_clear" in recorder.calls
    assert "wait_for_previous_game_clear_async" in recorder.calls


def test_recording_supervisor_does_not_finalize_twice_when_save_fails():
    class SaveFailingRecorder(FakeRecorder):
        def finalize_session(self, outcome=None, failure_reason=None):
            self.calls.append("finalize_session")
            self.finalize_outcomes.append(outcome)
            return SimpleNamespace(success=False, error="disk full")

    statuses = []
    notifications = []
    recorder = SaveFailingRecorder()
    recording_controller = FakeRecordingController(recorder)
    supervisor = RecordingSupervisor(
        config_controller=FakeConfigController(),
        recording_controller=recording_controller,
        status_cb=statuses.append,
        notification_cb=lambda *args: notifications.append(args),
    )

    run(run_supervisor(supervisor))

    assert "⚠️ セッション保存に失敗しました: disk full" in statuses
    assert recorder.calls.count("finalize_session") == 1
    assert recorder.calls[-3:] == ["request_stop", "stop_recording", "runtime_close"]
    assert recording_controller.runtime.close_calls == [False]
    assert [item[0] for item in notifications] == ["recording_started", "recording_failed"]
