from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

try:
    from . import recordtest
    from .controllers import ConfigController, RecordingController
    from .recording_state import RecordingOutcome
except ImportError:
    import recordtest
    from controllers import ConfigController, RecordingController
    from recording_state import RecordingOutcome


class RecordingSupervisor:
    """録画監視のアプリケーションフローをUIワーカーから分離する。"""

    def __init__(
        self,
        config_controller: ConfigController | None = None,
        recording_controller: RecordingController | None = None,
        status_cb: Callable[[str], None] | None = None,
    ) -> None:
        self.config_controller = config_controller or ConfigController()
        self.recording_controller = recording_controller or RecordingController()
        self.status_cb = status_cb
        self.recorder: Any | None = None
        self.runtime: Any | None = None
        self.stop_event: asyncio.Event | None = None
        self.session_completed = False
        self.session_should_finalize = False

    async def run(self, stop_event: asyncio.Event) -> None:
        self.stop_event = stop_event
        try:
            settings = self.config_controller.load_config()
            report = self.config_controller.run_preflight(settings, auto_fix=True, force_obs_detect=True)
            if report.get("changed"):
                self.config_controller.save_config(report["config"])

            self._emit_report(report)

            errors = report.get("errors", [])
            if errors:
                raise recordtest.RecorderError("\n".join(errors))

            if hasattr(self.recording_controller, "create_runtime"):
                self.runtime = self.recording_controller.create_runtime(report["config"], status_cb=self.status_cb)
                self.recorder = self.runtime.recorder
            else:
                self.recorder = self.recording_controller.create_recorder(report["config"], status_cb=self.status_cb)
            self.recorder.set_stop_event(stop_event)
            self._apply_audio_profile()

            while not stop_event.is_set():
                self.recorder.reset_session()
                self.session_completed = False
                self.session_should_finalize = False
                started = await self.recorder.wait_for_game_start_async()
                if not started or stop_event.is_set():
                    break
                try:
                    await self.recorder.start_recording_async()
                    if stop_event.is_set():
                        break
                    outcome = await self.recorder.record_until_end_async()
                except Exception as e:
                    if self._has_session_data():
                        self._mark_failed_partial(e)
                        self.session_should_finalize = True
                        self.recorder.finalize_session(
                            outcome=RecordingOutcome.FAILED_PARTIAL,
                            failure_reason=e,
                        )
                        self.session_should_finalize = False
                        self._emit("⚠️ 録画セッションを部分保存しました。")
                    raise
                if outcome != RecordingOutcome.COMPLETED:
                    self._emit("⏹️ 録画セッションを中断しました。")
                    break
                self.session_completed = True
                self.session_should_finalize = True
                self.recorder.finalize_session()
                self.session_should_finalize = False
                self._emit("✅ 試合記録完了。次の試合を待機します。")
        finally:
            self.shutdown()

    def request_stop(self) -> None:
        if self.stop_event:
            self.stop_event.set()
        if self.recorder:
            self.recorder.request_stop()

    def shutdown(self) -> None:
        if not self.recorder:
            return
        self.recorder.request_stop()
        if not self.session_should_finalize:
            self.recorder.stop_recording()
        if self.runtime:
            self.runtime.close(finalize_session=self.session_should_finalize)
        else:
            if self.session_should_finalize:
                self.recorder.finalize_session()
            self.recorder.shutdown_obs()
        self.session_completed = False
        self.session_should_finalize = False
        self.runtime = None
        self.recorder = None

    def _apply_audio_profile(self) -> None:
        try:
            self.recorder.apply_audio_profile(self.recorder.config)
            self._emit("🔊 音声設定をOBSへ適用しました。")
        except Exception as e:
            self._emit(f"⚠️ 音声設定の適用に失敗: {e}")

    def _emit_report(self, report: dict[str, Any]) -> None:
        for note in report.get("notes", []):
            self._emit(f"🛠️ {note}")
        for warning in report.get("warnings", []):
            self._emit(f"⚠️ {warning}")

    def _emit(self, message: str) -> None:
        if self.status_cb:
            self.status_cb(message)

    def _has_session_data(self) -> bool:
        has_session_data = getattr(self.recorder, "has_session_data", None)
        if callable(has_session_data):
            return bool(has_session_data())
        return False

    def _mark_failed_partial(self, error: BaseException) -> None:
        marker = getattr(self.recorder, "mark_session_failed", None)
        if callable(marker):
            marker(error)
