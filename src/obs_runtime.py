from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from . import recordtest
    from .obs_process import OBSProcessQueryError
except ImportError:
    import recordtest
    from obs_process import OBSProcessQueryError


LOGGER = logging.getLogger("lol_replay.obs_runtime")


def _manual_lease_recovery_guidance(process_manager: Any) -> str:
    lease_path = getattr(process_manager, "lease_path", None)
    if lease_path is None:
        return (
            "解決しない場合は、OBSが完全に終了したことを確認してから、"
            "OBS所有情報ファイルを退避または削除して再実行してください。"
        )
    try:
        resolved_path = Path(lease_path).resolve()
    except Exception:
        resolved_path = Path(str(lease_path))
    return (
        "解決しない場合は、OBSが完全に終了したことを確認してから、"
        "次のOBS所有情報ファイルを退避または削除して再実行してください。\n"
        f"所有情報ファイル: {resolved_path}"
    )


@dataclass
class RecorderRuntime:
    recorder: recordtest.LoLAutoRecorder
    owns_process: bool = False
    owns_existing_process: bool = False
    process_manager: Any | None = None

    def close(self, finalize_session: bool = False) -> None:
        primary_error: BaseException | None = None
        try:
            if finalize_session:
                self.recorder.finalize_session()
        except BaseException as exc:
            primary_error = exc

        cleanup_error: BaseException | None = None
        try:
            with recordtest.OBS_OPERATION_LOCK:
                if self.owns_existing_process:
                    self._close_existing_owned_process()
                elif self.owns_process:
                    self.recorder.shutdown_obs()
                else:
                    self.recorder.disconnect_obs()
        except BaseException as exc:
            cleanup_error = exc

        if primary_error is not None:
            if cleanup_error is not None:
                add_note = getattr(primary_error, "add_note", None)
                if callable(add_note):
                    add_note(
                        "OBS cleanup also failed: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                LOGGER.error(
                    "OBS cleanup failed while preserving an earlier runtime error: %s",
                    cleanup_error,
                )
            raise primary_error
        if cleanup_error is not None:
            raise cleanup_error

    def _close_existing_owned_process(self) -> None:
        failures: list[BaseException] = []
        try:
            self.recorder.disconnect_obs()
        except BaseException as exc:
            failures.append(exc)

        if self.process_manager is None:
            failures.append(RuntimeError("OBS process manager is unavailable"))
        else:
            try:
                self.process_manager.kill_stale_owned_processes()
            except BaseException as exc:
                failures.append(exc)

        if not failures:
            return
        error = recordtest.RecorderError(
            "管理対象OBSを安全に終了できませんでした。OBSを手動で終了し、"
            "アプリを再実行してください。\n"
            f"{_manual_lease_recovery_guidance(self.process_manager)}"
        )
        add_note = getattr(error, "add_note", None)
        if callable(add_note):
            for failure in failures:
                add_note(f"{type(failure).__name__}: {failure}")
        raise error from failures[0]


class OBSRuntimeManager:
    """OBSプロセス所有権とRecorder起動を一箇所で扱う。"""

    def open_recorder(
        self,
        config: recordtest.AppConfig,
        *,
        auto_launch: bool = False,
        force_launch: bool = False,
        auto_setup: bool = False,
        status_cb: Any | None = None,
        max_retries: int = 2,
        retry_delay: float = 0.5,
    ) -> RecorderRuntime:
        with recordtest.OBS_OPERATION_LOCK:
            return self._open_recorder_locked(
                config,
                auto_launch=auto_launch,
                force_launch=force_launch,
                auto_setup=auto_setup,
                status_cb=status_cb,
                max_retries=max_retries,
                retry_delay=retry_delay,
            )

    def _open_recorder_locked(
        self,
        config: recordtest.AppConfig,
        *,
        auto_launch: bool = False,
        force_launch: bool = False,
        auto_setup: bool = False,
        status_cb: Any | None = None,
        max_retries: int = 2,
        retry_delay: float = 0.5,
    ) -> RecorderRuntime:
        launched_process = None
        owns_existing_process = False
        process_manager = recordtest.OBSProcessManager(config.obs.obs_dir)
        if force_launch:
            if self._wait_for_owned_obs_connection(config, process_manager):
                owns_existing_process = True
            else:
                launched_process = recordtest.launch_obs(config)
        elif auto_launch:
            ok, _detail = recordtest.test_obs_connection(
                config.obs.host,
                config.obs.port,
                config.obs.password,
                timeout=1.5,
            )
            if ok:
                if not self._has_owned_process(process_manager):
                    raise recordtest.RecorderError(
                        "OBS WebSocketには接続できますが、このアプリが起動した管理対象OBSではありません。\n"
                        f"接続先: {config.obs.host}:{config.obs.port}\n"
                        "既存のOBSを終了してから再実行してください。"
                    )
            elif self._wait_for_owned_obs_connection(config, process_manager):
                pass
            else:
                launched_process = recordtest.launch_obs(config)
        elif recordtest.is_tcp_port_open(config.obs.host, config.obs.port, timeout=0.3):
            if not self._has_owned_process(process_manager):
                raise recordtest.RecorderError(
                    "OBS WebSocketポートは使用中ですが、このアプリが起動した管理対象OBSではありません。\n"
                    f"接続先: {config.obs.host}:{config.obs.port}\n"
                    "既存のOBSを終了してから再実行してください。"
                )

        obs_client = None
        recorder = None
        try:
            obs_client = recordtest.ObsWebSocketClient(
                config=config,
                obs_process=launched_process,
                status_cb=status_cb,
                max_retries=max_retries,
                retry_delay=retry_delay,
            )
            recorder = recordtest.LoLAutoRecorder(
                config=config,
                obs_process=launched_process,
                status_cb=status_cb,
                auto_setup=auto_setup,
                obs_client=obs_client,
            )
            recorder.open()
        except BaseException as primary_error:
            def record_cleanup_failure(
                cleanup_error: BaseException,
                *,
                context: str,
                primary: BaseException = primary_error,
            ) -> None:
                add_note = getattr(primary, "add_note", None)
                if callable(add_note):
                    add_note(
                        f"{context}: {type(cleanup_error).__name__}: "
                        f"{cleanup_error}"
                    )
                LOGGER.error(
                    "%s while preserving the original startup error: %s",
                    context,
                    cleanup_error,
                )

            if recorder is not None:
                if not getattr(recorder, "_open_cleanup_attempted", False):
                    try:
                        recorder.shutdown_obs()
                    except BaseException as cleanup_error:
                        record_cleanup_failure(
                            cleanup_error,
                            context="Recorder起動失敗後のOBS cleanupにも失敗しました",
                        )
            else:
                if launched_process:
                    try:
                        process_manager.terminate_process(launched_process)
                    except BaseException as cleanup_error:
                        record_cleanup_failure(
                            cleanup_error,
                            context="Recorder構築失敗後のOBS cleanupにも失敗しました",
                        )
                if obs_client is not None:
                    try:
                        obs_client.disconnect()
                    except BaseException as cleanup_error:
                        record_cleanup_failure(
                            cleanup_error,
                            context=(
                                "Recorder構築失敗後のWebSocket cleanupにも"
                                "失敗しました"
                            ),
                        )
            raise
        return RecorderRuntime(
            recorder=recorder,
            owns_process=bool(launched_process) or owns_existing_process,
            owns_existing_process=owns_existing_process,
            process_manager=process_manager,
        )

    @staticmethod
    def _owned_process_error(
        exc: BaseException,
        process_manager: Any,
    ) -> recordtest.RecorderError:
        error = recordtest.RecorderError(
            "このアプリが起動したOBSの所有情報を安全に確認できません。\n"
            "OBSを手動で終了してから再実行してください。\n"
            f"{_manual_lease_recovery_guidance(process_manager)}"
        )
        add_note = getattr(error, "add_note", None)
        if callable(add_note):
            add_note(f"{type(exc).__name__}: {exc}")
        return error

    def _has_owned_process(self, process_manager: Any) -> bool:
        try:
            return bool(process_manager.has_owned_process())
        except (OBSProcessQueryError, OSError) as exc:
            raise self._owned_process_error(exc, process_manager) from exc

    def _wait_for_owned_obs_connection(
        self,
        config: recordtest.AppConfig,
        process_manager: Any,
    ) -> bool:
        try:
            return recordtest.wait_for_owned_obs_connection(
                config,
                process_manager=process_manager,
            )
        except (OBSProcessQueryError, OSError) as exc:
            raise self._owned_process_error(exc, process_manager) from exc
