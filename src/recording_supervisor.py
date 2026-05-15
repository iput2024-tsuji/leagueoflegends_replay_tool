from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

try:
    from . import recordtest
    from .controllers import ConfigController, RecordingController
    from .recording_state import FinalizeResult, RecordingOutcome
except ImportError:
    import recordtest
    from controllers import ConfigController, RecordingController
    from recording_state import FinalizeResult, RecordingOutcome


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
        self.session_finalize_attempted = False

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
                self.session_finalize_attempted = False
                started = await self.recorder.wait_for_game_start_async()
                if not started or stop_event.is_set():
                    break
                try:
                    await self.recorder.start_recording_async()
                    if stop_event.is_set():
                        self._finalize_aborted("stop requested after recording started")
                        break
                    outcome = await self.recorder.record_until_end_async()
                except Exception as e:
                    if self._has_session_data():
                        self._mark_failed_partial(e)
                        result = self._finalize_current_session(RecordingOutcome.FAILED_PARTIAL, e)
                        if self._finalize_success(result):
                            self._emit("⚠️ 録画セッションを部分保存しました。")
                        else:
                            self._emit(f"⚠️ 部分保存に失敗しました: {self._finalize_error(result)}")
                    raise
                if outcome != RecordingOutcome.COMPLETED:
                    if self._has_session_data():
                        self._finalize_aborted("recording was cancelled")
                    else:
                        self._emit("⏹️ 録画セッションを中断しました。")
                    break
                self.session_completed = True
                result = self._finalize_current_session(RecordingOutcome.COMPLETED)
                if not self._finalize_success(result):
                    self._emit(f"⚠️ セッション保存に失敗しました: {self._finalize_error(result)}")
                    break
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
        if self._has_session_data() and not self._is_session_finalized() and not self.session_finalize_attempted:
            self._finalize_aborted("shutdown requested")
        elif not self._is_session_finalized():
            self.recorder.stop_recording()
        if self.runtime:
            self.runtime.close(finalize_session=False)
        else:
            self.recorder.shutdown_obs()
        self.session_completed = False
        self.session_should_finalize = False
        self.session_finalize_attempted = False
        self.runtime = None
        self.recorder = None

    def _finalize_aborted(self, reason: str) -> Any:
        marker = getattr(self.recorder, "mark_session_aborted", None)
        if callable(marker):
            marker(reason)
        result = self._finalize_current_session(RecordingOutcome.ABORTED, reason)
        if self._finalize_success(result):
            self._emit("⏹️ 録画セッションを中断ログとして保存しました。")
        else:
            self._emit(f"⚠️ 中断ログの保存に失敗しました: {self._finalize_error(result)}")
        return result

    def _finalize_current_session(self, outcome: RecordingOutcome, reason: Any | None = None) -> Any:
        self.session_should_finalize = False
        self.session_finalize_attempted = True
        try:
            return self.recorder.finalize_session(outcome=outcome, failure_reason=reason)
        except Exception as e:
            return FinalizeResult(success=False, outcome=outcome, error=f"{type(e).__name__}: {e}")

    def _finalize_success(self, result: Any) -> bool:
        if result is None:
            return True
        return bool(getattr(result, "success", True))

    def _finalize_error(self, result: Any) -> str:
        if result is None:
            return "unknown error"
        return str(getattr(result, "error", None) or "unknown error")

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

    def _is_session_finalized(self) -> bool:
        return bool(getattr(self.recorder, "session_finalized", False))

    def _mark_failed_partial(self, error: BaseException) -> None:
        marker = getattr(self.recorder, "mark_session_failed", None)
        if callable(marker):
            marker(error)
