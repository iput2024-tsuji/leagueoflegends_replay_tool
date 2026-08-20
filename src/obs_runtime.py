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
    lease_lock_path = getattr(process_manager, "lease_lock_path", None)
    if lease_path is None and lease_lock_path is None:
        return (
            "解決しない場合は、OBSが完全に終了したことを確認してから、"
            "OBS所有情報ファイルを退避または削除して再実行してください。"
        )

    def resolved(value: Any) -> Path | None:
        if value is None:
            return None
        try:
            return Path(value).resolve()
        except Exception:
            return Path(str(value))

    resolved_lease = resolved(lease_path)
    resolved_lock = resolved(lease_lock_path)
    paths = []
    if resolved_lease is not None:
        paths.append(f"所有情報ファイル: {resolved_lease}")
    if resolved_lock is not None:
        paths.append(f"所有情報lock: {resolved_lock}")
    return (
        "解決しない場合は、OBSが完全に終了したことを確認してから、"
        "表示されたOBS所有情報ファイルを退避または削除し、lockも確認して"
        "再実行してください。\n"
        + "\n".join(paths)
    )


@dataclass
class RecorderRuntime:
    recorder: recordtest.LoLAutoRecorder
    owns_process: bool = False
    owns_existing_process: bool = False
    process_manager: Any | None = None

    def close(
        self, finalize_session: bool = False, *, allow_force: bool = True
    ) -> None:
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
                    self._close_existing_owned_process(allow_force=allow_force)
                elif self.owns_process:
                    if allow_force:
                        self.recorder.shutdown_obs()
                    else:
                        self.recorder.shutdown_obs(allow_force=False)
                else:
                    self.recorder.disconnect_obs()
        except BaseException as exc:
            cleanup_error = exc

        if primary_error is not None:
            if cleanup_error is not None:
                selected_error = recordtest._select_cleanup_control_flow_error(
                    primary_error,
                    cleanup_error,
                    logger=LOGGER,
                    context="OBS cleanup also failed",
                )
                if selected_error is cleanup_error:
                    raise cleanup_error
            raise primary_error
        if cleanup_error is not None:
            raise cleanup_error

    def _close_existing_owned_process(self, *, allow_force: bool = True) -> None:
        failures: list[BaseException] = []
        selected_error: BaseException | None = None

        def record_failure(error: BaseException, *, context: str) -> None:
            nonlocal selected_error
            failures.append(error)
            if selected_error is None:
                selected_error = error
            else:
                selected_error = recordtest._select_cleanup_control_flow_error(
                    selected_error,
                    error,
                    logger=LOGGER,
                    context=context,
                )

        try:
            self.recorder.disconnect_obs()
        except BaseException as exc:
            record_failure(
                exc,
                context="管理対象OBS終了前のWebSocket切断にも失敗しました",
            )

        if self.process_manager is None:
            record_failure(
                RuntimeError("OBS process manager is unavailable"),
                context="管理対象OBSのlease検証cleanupを開始できませんでした",
            )
        else:
            try:
                if allow_force:
                    self.process_manager.kill_stale_owned_processes()
                else:
                    self.process_manager.kill_stale_owned_processes(allow_force=False)
            except BaseException as exc:
                record_failure(
                    exc,
                    context="管理対象OBSのlease検証cleanupにも失敗しました",
                )

        if not failures:
            return
        if selected_error is not None and not isinstance(selected_error, Exception):
            detail = (
                "管理対象OBSのcleanupが中断されました。OBSを手動で終了し、"
                "アプリを再実行してください。\n"
                f"{_manual_lease_recovery_guidance(self.process_manager)}"
            )
            add_note = getattr(selected_error, "add_note", None)
            if callable(add_note):
                add_note(detail)
            LOGGER.error("%s", detail)
            raise selected_error
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
        obs_client = None
        recorder = None
        try:
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
                            "OBS WebSocketには接続できますが、このアプリが起動した"
                            "管理対象OBSではありません。\n"
                            f"接続先: {config.obs.host}:{config.obs.port}\n"
                            "既存のOBSを終了してから再実行してください。"
                        )
                elif self._wait_for_owned_obs_connection(config, process_manager):
                    pass
                else:
                    launched_process = recordtest.launch_obs(config)
            elif recordtest.is_tcp_port_open(
                config.obs.host,
                config.obs.port,
                timeout=0.3,
            ):
                if not self._has_owned_process(process_manager):
                    raise recordtest.RecorderError(
                        "OBS WebSocketポートは使用中ですが、このアプリが起動した"
                        "管理対象OBSではありません。\n"
                        f"接続先: {config.obs.host}:{config.obs.port}\n"
                        "既存のOBSを終了してから再実行してください。"
                    )

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
            return RecorderRuntime(
                recorder=recorder,
                owns_process=launched_process is not None or owns_existing_process,
                owns_existing_process=owns_existing_process,
                process_manager=process_manager,
            )
        except BaseException as primary_error:
            selected_error = primary_error

            def record_cleanup_failure(
                cleanup_error: BaseException,
                *,
                context: str,
            ) -> None:
                nonlocal selected_error
                selected_error = recordtest._select_cleanup_control_flow_error(
                    selected_error,
                    cleanup_error,
                    logger=LOGGER,
                    context=context,
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
                if launched_process is not None:
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
            if owns_existing_process:
                try:
                    process_manager.kill_stale_owned_processes()
                except BaseException as cleanup_error:
                    record_cleanup_failure(
                        cleanup_error,
                        context=(
                            "引き継いだ管理対象OBSのlease検証cleanupにも失敗しました。"
                            "PID fallbackや管理対象外OBSへのsignalは行っていません。"
                            "OBSを手動で終了してから再試行してください。"
                            f"{_manual_lease_recovery_guidance(process_manager)}"
                        ),
                    )
            if selected_error is not primary_error:
                # Preserve the injected control-flow object's existing chain.
                raise selected_error  # noqa: B904
            raise

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
