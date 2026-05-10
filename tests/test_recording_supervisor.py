import asyncio
from types import SimpleNamespace

import pytest

from src import recordtest
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

    def create_recorder(self, config_data, status_cb=None):
        self.created_with = (config_data, status_cb)
        return self.recorder


class FakeRecorder:
    def __init__(self):
        self.config = SimpleNamespace()
        self.calls = []
        self.stop_event = None
        self.wait_count = 0
        self.session_has_data = False

    def set_stop_event(self, stop_event):
        self.stop_event = stop_event
        self.calls.append("set_stop_event")

    def apply_audio_profile(self, config):
        self.calls.append("apply_audio_profile")

    def reset_session(self):
        self.calls.append("reset_session")
        self.session_has_data = False

    async def wait_for_game_start_async(self):
        self.calls.append("wait_for_game_start_async")
        self.wait_count += 1
        return self.wait_count == 1

    async def start_recording_async(self):
        self.calls.append("start_recording_async")
        self.session_has_data = True

    async def record_until_end_async(self):
        self.calls.append("record_until_end_async")
        return True

    def stop_recording(self):
        self.calls.append("stop_recording")

    def has_session_data(self):
        return self.session_has_data

    def save_json(self):
        self.calls.append("save_json")
        self.session_has_data = False

    def finalize_session(self):
        self.calls.append("finalize_session")
        if self.session_has_data:
            self.save_json()

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
    recorder = FakeRecorder()
    config_controller = FakeConfigController()
    recording_controller = FakeRecordingController(recorder)
    supervisor = RecordingSupervisor(
        config_controller=config_controller,
        recording_controller=recording_controller,
        status_cb=statuses.append,
    )

    run(run_supervisor(supervisor))

    assert config_controller.saved == [{"ok": True}]
    assert recording_controller.created_with[0] == {"ok": True}
    assert "🛠️ defaults applied" in statuses
    assert "⚠️ mpv missing" in statuses
    assert "🔊 音声設定をOBSへ適用しました。" in statuses
    assert "✅ 試合記録完了。次の試合を待機します。" in statuses
    assert recorder.calls == [
        "set_stop_event",
        "apply_audio_profile",
        "reset_session",
        "wait_for_game_start_async",
        "start_recording_async",
        "record_until_end_async",
        "finalize_session",
        "save_json",
        "reset_session",
        "wait_for_game_start_async",
        "request_stop",
        "finalize_session",
        "shutdown_obs",
    ]


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
