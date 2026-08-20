from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

try:
    from . import recordtest
    from .controllers import ConfigController, RecordingController
    from .notifications import NotificationEvent
    from .recording_state import FinalizeResult, RecordingOutcome
except ImportError:
    import recordtest
    from controllers import ConfigController, RecordingController
    from notifications import NotificationEvent
    from recording_state import FinalizeResult, RecordingOutcome


class RecordingSupervisor:
    """録画監視のアプリケーションフローをUIワーカーから分離する。"""

    def __init__(
        self,
        config_controller: ConfigController | None = None,
        recording_controller: RecordingController | None = None,
        status_cb: Callable[[str], None] | None = None,
        notification_cb: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self.config_controller = config_controller or ConfigController()
        self.recording_controller = recording_controller or RecordingController()
        self.status_cb = status_cb
        self.notification_cb = notification_cb
        self.recorder: Any | None = None
        self.runtime: Any | None = None
        self.stop_event: asyncio.Event | None = None
        self.session_completed = False
        self.session_should_finalize = False
        self.session_finalize_attempted = False
        self.shutdown_error: BaseException | None = None
        self._recording_state_lock = threading.Lock()
        self._recording_active = False
        self._update_shutdown_reserved = False

    async def run(self, stop_event: asyncio.Event) -> None:
        self.stop_event = stop_event
        primary_error: BaseException | None = None
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
                if not self._begin_recording():
                    break
                try:
                    try:
                        await self.recorder.start_recording_async()
                        self._notify(
                            NotificationEvent.RECORDING_STARTED,
                            "録画を開始しました",
                            "League of Legendsの録画を開始しました。",
                        )
                        if stop_event.is_set():
                            self._finalize_aborted("stop requested after recording started")
                            break
                        outcome = await self.recorder.record_until_end_async()
                    except Exception as e:
                        should_continue = not stop_event.is_set()
                        if self._has_session_data():
                            self._mark_failed_partial(e)
                            result = self._finalize_current_session(RecordingOutcome.FAILED_PARTIAL, e)
                            if self._finalize_success(result):
                                self._emit("⚠️ 録画セッションを部分保存しました。")
                            else:
                                self._emit(f"⚠️ 部分保存に失敗しました: {self._finalize_error(result)}")
                                should_continue = False
                        self._notify(
                            NotificationEvent.RECORDING_FAILED,
                            "録画に失敗しました",
                            self._notification_error_message(e),
                        )
                        if should_continue:
                            self._defer_current_game_after_failure()
                            self._emit("⚠️ 録画エラーが発生したため、この試合中の再試行を停止します。")
                            self._finish_recording()
                            await self._wait_for_post_game_notification_window()
                            if stop_event.is_set():
                                break
                            self._emit("⚠️ 録画エラー後も次の試合監視を継続します。")
                            continue
                        break
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
                        self._notify(
                            NotificationEvent.RECORDING_FAILED,
                            "録画の保存に失敗しました",
                            f"録画セッションを保存できませんでした: {self._finalize_error(result)}",
                        )
                        break
                    self._emit("✅ 試合記録完了。次の試合を待機します。")
                finally:
                    self._finish_recording()
                await self._wait_for_post_game_notification_window()
                self._notify(
                    NotificationEvent.RECORDING_COMPLETED,
                    "録画が完了しました",
                    "試合の録画とセッション情報を保存しました。",
                )
        except BaseException as exc:
            primary_error = exc
        finally:
            cleanup_error: BaseException | None = None
            try:
                self.shutdown()
            except BaseException as exc:
                cleanup_error = exc

        selected_error = primary_error
        if cleanup_error is not None:
            if selected_error is None:
                selected_error = cleanup_error
            else:
                selected_error = recordtest._select_cleanup_control_flow_error(
                    selected_error,
                    cleanup_error,
                    logger=recordtest.LOGGER,
                    context=(
                        "録画監視失敗後のOBS cleanupにも失敗しました。"
                        "OBSを手動で終了してから再試行してください"
                    ),
                )
        if selected_error is not None:
            raise selected_error

    def request_stop(self) -> None:
        if self.stop_event is not None:
            self.stop_event.set()
        if self.recorder is not None:
            self.recorder.request_stop()

    def reserve_update_shutdown(self) -> bool:
        """Reserve shutdown unless a recording transition already owns the session."""

        with self._recording_state_lock:
            if self._recording_active:
                return False
            self._update_shutdown_reserved = True
            return True

    def _begin_recording(self) -> bool:
        with self._recording_state_lock:
            if self._update_shutdown_reserved:
                return False
            self._recording_active = True
            return True

    def _finish_recording(self) -> None:
        with self._recording_state_lock:
            self._recording_active = False

    def shutdown(self) -> None:
        recorder = self.recorder
        runtime = self.runtime
        selected_error: BaseException | None = None

        def remember_cleanup_error(error: BaseException, *, context: str) -> None:
            nonlocal selected_error
            if selected_error is None:
                selected_error = error
                return
            selected_error = recordtest._select_cleanup_control_flow_error(
                selected_error,
                error,
                logger=recordtest.LOGGER,
                context=context,
            )

        try:
            if recorder is not None:
                try:
                    recorder.request_stop()
                except BaseException as cleanup_error:
                    remember_cleanup_error(
                        cleanup_error,
                        context="録画監視終了時の停止要求にも失敗しました",
                    )

                try:
                    if (
                        self._has_session_data()
                        and not self._is_session_finalized()
                        and not self.session_finalize_attempted
                    ):
                        self._finalize_aborted("shutdown requested")
                    elif not self._is_session_finalized():
                        recorder.stop_recording()
                except BaseException as cleanup_error:
                    remember_cleanup_error(
                        cleanup_error,
                        context="録画監視終了時のセッション停止処理にも失敗しました",
                    )

            try:
                if runtime is not None:
                    if self._update_shutdown_reserved:
                        runtime.close(finalize_session=False, allow_force=False)
                    else:
                        runtime.close(finalize_session=False)
                elif recorder is not None:
                    if self._update_shutdown_reserved:
                        recorder.shutdown_obs(allow_force=False)
                    else:
                        recorder.shutdown_obs()
            except BaseException as cleanup_error:
                owner_cleanup_context = (
                    "録画監視終了時の所有OBS cleanupにも失敗しました。"
                    "OBSを手動で終了してから再試行してください"
                )
                if selected_error is None:
                    selected_error = cleanup_error
                    add_note = getattr(cleanup_error, "add_note", None)
                    if callable(add_note):
                        add_note(owner_cleanup_context)
                    recordtest.LOGGER.error(
                        "%s: %s: %s",
                        owner_cleanup_context,
                        type(cleanup_error).__name__,
                        cleanup_error,
                    )
                else:
                    remember_cleanup_error(
                        cleanup_error,
                        context=owner_cleanup_context,
                    )
        finally:
            self.session_completed = False
            self.session_should_finalize = False
            self.session_finalize_attempted = False
            self.runtime = None
            self.recorder = None

        self.shutdown_error = selected_error
        if selected_error is not None:
            raise selected_error

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

    def _notify(self, event: NotificationEvent, title: str, message: str) -> None:
        if self.notification_cb:
            recordtest.LOGGER.info("Notification requested: event=%s", event.value)
            self.notification_cb(event.value, title, message)

    async def _wait_for_post_game_notification_window(self) -> None:
        waiter = getattr(self.recorder, "wait_for_previous_game_clear_async", None)
        if not callable(waiter):
            return
        try:
            cleared = await waiter()
        except Exception:
            recordtest.LOGGER.warning(
                "Failed while waiting to send recording completion notification",
                exc_info=True,
            )
            return
        if cleared:
            recordtest.LOGGER.info("Post-game process cleared before completion notification")
        else:
            recordtest.LOGGER.info("Post-game notification wait ended before process clear")

    def _notification_error_message(self, error: BaseException) -> str:
        detail = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
        return f"録画処理を継続できませんでした: {detail}"

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

    def _defer_current_game_after_failure(self) -> None:
        defer = getattr(self.recorder, "defer_current_game_until_clear", None)
        if callable(defer):
            defer()
