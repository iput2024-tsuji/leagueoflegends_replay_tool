from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from . import recordtest
except ImportError:
    import recordtest


@dataclass
class RecorderRuntime:
    recorder: recordtest.LoLAutoRecorder
    owns_process: bool = False

    def close(self, finalize_session: bool = False) -> None:
        try:
            if finalize_session:
                self.recorder.finalize_session()
        finally:
            if self.owns_process:
                self.recorder.shutdown_obs()
            else:
                self.recorder.disconnect_obs()


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
        launched_process = None
        if force_launch:
            launched_process = recordtest.launch_obs(config)
        elif auto_launch:
            ok, _detail = recordtest.test_obs_connection(
                config.obs.host,
                config.obs.port,
                config.obs.password,
                timeout=1.5,
            )
            if not ok:
                launched_process = recordtest.launch_obs(config)

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
        try:
            recorder.open()
        except Exception:
            if launched_process:
                try:
                    recorder.shutdown_obs()
                except Exception:
                    pass
            raise
        return RecorderRuntime(recorder=recorder, owns_process=bool(launched_process))
