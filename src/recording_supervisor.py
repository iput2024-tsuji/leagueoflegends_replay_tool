from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

try:
    from . import recordtest
    from .controllers import ConfigController, RecordingController
except ImportError:
    import recordtest
    from controllers import ConfigController, RecordingController


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
        self.stop_event: asyncio.Event | None = None

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

            self.recorder = self.recording_controller.create_recorder(report["config"], status_cb=self.status_cb)
            self.recorder.set_stop_event(stop_event)
            self._apply_audio_profile()

            while not stop_event.is_set():
                self.recorder.reset_session()
                started = await self.recorder.wait_for_game_start_async()
                if not started or stop_event.is_set():
                    break
                await self.recorder.start_recording_async()
                if stop_event.is_set():
                    break
                await self.recorder.record_until_end_async()
                self.recorder.finalize_session()
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
        self.recorder.finalize_session()
        self.recorder.shutdown_obs()
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
