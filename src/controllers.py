from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:
    from . import config_schema, recordtest
    from .config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
    from .obs_runtime import OBSRuntimeManager, RecorderRuntime
except ImportError:
    import config_schema
    import recordtest
    from config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
    from obs_runtime import OBSRuntimeManager, RecorderRuntime


def _close_runtime_preserving_primary(
    runtime: RecorderRuntime,
    primary_error: BaseException | None,
) -> None:
    try:
        runtime.close()
    except BaseException as cleanup_error:
        if primary_error is None:
            raise
        recordtest._record_cleanup_failure(
            primary_error,
            cleanup_error,
            logger=recordtest.LOGGER,
            context=(
                "OBS設定操作失敗後のruntime cleanupにも失敗しました。"
                "OBSを手動で終了してから再試行してください"
            ),
        )


class ConfigController:
    """設定ファイル、補完、プレフライトをUIから分離して扱う。"""

    def __init__(
        self,
        repository: ConfigRepository | None = None,
        runtime_manager: OBSRuntimeManager | None = None,
    ) -> None:
        self.repository = repository or ConfigRepository(CONFIG_PATH, SAMPLE_CONFIG_PATH)
        self.runtime_manager = runtime_manager or OBSRuntimeManager()

    def apply_auto_defaults(
        self, data: dict[str, Any] | None, force_obs_detect: bool = False
    ) -> tuple[dict[str, Any], bool, list[str]]:
        normalized = config_schema.normalize_config(
            data,
            auto_fix=True,
            password_factory=recordtest.generate_obs_password,
        )
        data = normalized.config
        changed = normalized.changed
        notes = list(normalized.notes) + list(normalized.warnings)

        app_cfg = data.setdefault("app", {})
        has_valid_dir = bool(recordtest.detect_obs_dir())
        if app_cfg.get("setup_completed") is None:
            app_cfg["setup_completed"] = bool(has_valid_dir)
            changed = True
        elif force_obs_detect and bool(app_cfg.get("setup_completed")) != has_valid_dir:
            app_cfg["setup_completed"] = has_valid_dir
            notes.append("OBSフォルダの検出結果に合わせて初期設定状態を更新しました。")
            changed = True
        elif not bool(app_cfg.get("setup_completed")) and has_valid_dir:
            app_cfg["setup_completed"] = True
            changed = True

        return data, changed, notes

    def format_report_lines(self, lines: list[str] | tuple[str, ...] | None) -> str:
        if not lines:
            return "- なし"
        return "\n".join(f"- {line}" for line in lines)

    def load_config(self) -> dict[str, Any]:
        data = self.repository.load(create_if_missing=True)
        data, changed, _ = self.apply_auto_defaults(data, force_obs_detect=False)
        if changed:
            self.save_config(data)
        return data

    def save_config(self, data: dict[str, Any]) -> None:
        self.repository.save(data)

    def run_preflight(
        self, config_data: dict[str, Any] | None = None, auto_fix: bool = True, force_obs_detect: bool = True
    ) -> dict[str, Any]:
        data = config_data if config_data is not None else self.load_config()
        data, changed_defaults, default_notes = self.apply_auto_defaults(
            data,
            force_obs_detect=force_obs_detect,
        )
        report = recordtest.run_preflight_checks(data, auto_fix=auto_fix, ensure_dirs=True)
        report["changed"] = bool(changed_defaults or report.get("changed"))
        report["notes"] = list(default_notes) + list(report.get("notes", []))
        return report

    def run_guided_auto_setup(
        self, config_data: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        report = self.run_preflight(config_data, auto_fix=True, force_obs_detect=True)
        if report.get("errors"):
            return report, None

        try:
            info = recordtest.setup_obs_sync_elements(report["config"])
        except recordtest.RecorderError as e:
            report["errors"].append(str(e))
            return report, None
        except Exception as e:
            report["errors"].append(f"{type(e).__name__}: {e}")
            return report, None

        report["config"].setdefault("app", {})["setup_completed"] = True
        self.save_config(report["config"])
        return report, info

    def test_obs_connection(self, config_data: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool, str]:
        report = self.run_preflight(config_data, auto_fix=True, force_obs_detect=True)
        if report.get("changed"):
            self.save_config(report["config"])
        if report.get("errors"):
            return report, False, self.format_report_lines(report.get("errors", []))

        config = recordtest.AppConfig.from_dict(report["config"])
        runtime = None
        with recordtest.OBS_OPERATION_LOCK:
            try:
                runtime = self.runtime_manager.open_recorder(
                    config,
                    auto_launch=True,
                    auto_setup=False,
                    status_cb=None,
                    max_retries=5,
                    retry_delay=0.5,
                )
                return report, True, "接続成功: 管理対象OBS WebSocket"
            except Exception as e:
                return report, False, str(e)
            finally:
                if runtime:
                    runtime.close()

    def total_storage_size(self, config_data: dict[str, Any] | None = None) -> int:
        config = recordtest.AppConfig.from_dict(config_data or self.load_config())
        return recordtest.total_storage_size(config)


class AudioSettingsController:
    """OBS音声・録画出力のインフラ操作をUIから分離する。"""

    def __init__(
        self,
        config_controller: ConfigController | None = None,
        runtime_manager: OBSRuntimeManager | None = None,
    ) -> None:
        self.config_controller = config_controller or ConfigController()
        self.runtime_manager = runtime_manager or OBSRuntimeManager()

    def _prepare_config(
        self, data: dict[str, Any], auto_fix: bool = True, force_obs_detect: bool = True
    ) -> tuple[dict[str, Any], recordtest.AppConfig]:
        report = self.config_controller.run_preflight(
            data,
            auto_fix=auto_fix,
            force_obs_detect=force_obs_detect,
        )
        if report.get("changed"):
            self.config_controller.save_config(report["config"])
        if report.get("errors"):
            raise recordtest.RecorderError("\n".join(report.get("errors", [])))
        config = recordtest.AppConfig.from_dict(report["config"])
        recordtest.ensure_recording_dirs(config)
        return report, config

    def _open_recorder(
        self, config: recordtest.AppConfig, auto_launch: bool = False, max_retries: int = 2, retry_delay: float = 0.5
    ) -> RecorderRuntime:
        return self.runtime_manager.open_recorder(
            config,
            auto_launch=auto_launch,
            auto_setup=False,
            status_cb=None,
            max_retries=max_retries,
            retry_delay=retry_delay,
        )

    def refresh_audio_devices(self, data: dict[str, Any], auto_launch: bool = True) -> dict[str, Any]:
        report, config = self._prepare_config(data, auto_fix=True, force_obs_detect=True)
        runtime = None
        primary_error: BaseException | None = None
        with recordtest.OBS_OPERATION_LOCK:
            try:
                runtime = self._open_recorder(config, auto_launch=auto_launch)
                catalog = runtime.recorder.get_audio_device_catalog(cfg=config)
                return {
                    "catalog": catalog,
                    "config": report["config"],
                    "obs_launched": runtime.owns_process,
                }
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                if runtime:
                    _close_runtime_preserving_primary(runtime, primary_error)

    def apply_audio_settings(self, data: dict[str, Any], auto_launch: bool = True) -> dict[str, Any]:
        report, config = self._prepare_config(data, auto_fix=True, force_obs_detect=False)
        runtime = None
        primary_error: BaseException | None = None
        with recordtest.OBS_OPERATION_LOCK:
            try:
                runtime = self._open_recorder(config, auto_launch=auto_launch)
                runtime.recorder.apply_audio_profile(config)
                self.config_controller.save_config(report["config"])
                return {"config": report["config"], "obs_launched": runtime.owns_process}
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                if runtime:
                    _close_runtime_preserving_primary(runtime, primary_error)

    def apply_runtime_output_settings(self, data: dict[str, Any]) -> bool:
        report, config = self._prepare_config(data, auto_fix=True, force_obs_detect=False)
        with recordtest.OBS_OPERATION_LOCK:
            runtime = None
            primary_error: BaseException | None = None
            try:
                runtime = self._open_recorder(
                    config,
                    auto_launch=True,
                    max_retries=5,
                    retry_delay=0.5,
                )
                runtime.recorder.apply_record_output_settings()
                return True
            except BaseException as exc:
                primary_error = exc
                raise
            finally:
                if runtime:
                    _close_runtime_preserving_primary(runtime, primary_error)


class RecordingController:
    """録画監視ワーカーが使う録画ランタイム生成を担当する。"""

    def __init__(self, runtime_manager: OBSRuntimeManager | None = None) -> None:
        self.runtime_manager = runtime_manager or OBSRuntimeManager()

    def create_runtime(
        self, config_data: dict[str, Any], status_cb: Callable[[str], None] | None = None
    ) -> RecorderRuntime:
        config = recordtest.AppConfig.from_dict(config_data)
        recordtest.ensure_recording_dirs(config)
        return self.runtime_manager.open_recorder(
            config,
            force_launch=True,
            auto_setup=True,
            status_cb=status_cb,
        )

    def create_recorder(
        self, config_data: dict[str, Any], status_cb: Callable[[str], None] | None = None
    ) -> recordtest.LoLAutoRecorder:
        return self.create_runtime(config_data, status_cb=status_cb).recorder


class AnalyticsController:
    """録画JSON分析をUIから分離して提供する。"""

    def __init__(self, config_controller: ConfigController | None = None) -> None:
        self.config_controller = config_controller or ConfigController()

    def load_summary(self) -> dict[str, Any]:
        try:
            from .analytics import GameDataAnalyzer
        except ImportError:
            from analytics import GameDataAnalyzer

        config_data = self.config_controller.load_config()
        config = recordtest.AppConfig.from_dict(config_data)
        analyzer = GameDataAnalyzer(config=config)
        df = analyzer.load_dataframe()
        horde_result = analyzer.horde_kill_15min_winrate_correlation(df)
        tactical_insights = analyzer.extract_tactical_insights(df)

        if df.empty:
            return {
                "total_matches": 0,
                "win_rate": None,
                "horde": horde_result,
                "horde_rows": [],
                "tactical_insights": tactical_insights,
                "invalid_logs": list(analyzer.load_errors),
            }

        matches = df.drop_duplicates("match_id").copy()
        known_results = matches.dropna(subset=["is_win"])
        total_matches = int(matches["match_id"].nunique())
        win_rate = None
        if not known_results.empty:
            win_rate = float(known_results["is_win"].mean())

        event_counts = horde_result.get("winrate_by_event_count", {})
        count_sizes = {}
        if "event_name" in df.columns and "event_time" in df.columns:
            horde_rows = df[(df["event_name"] == "HordeKill") & (df["event_time"].fillna(float("inf")) <= 15 * 60)]
            counts = horde_rows.groupby("match_id").size()
            match_counts = counts.reindex(matches["match_id"], fill_value=0).astype(int)
            count_sizes = match_counts.value_counts().sort_index().to_dict()

        horde_rows = [
            {
                "count": int(count),
                "win_rate": float(rate),
                "matches": int(count_sizes.get(count, 0)),
            }
            for count, rate in sorted(event_counts.items(), key=lambda item: int(item[0]))
        ]

        return {
            "total_matches": total_matches,
            "win_rate": win_rate,
            "horde": horde_result,
            "horde_rows": horde_rows,
            "tactical_insights": tactical_insights,
            "invalid_logs": list(analyzer.load_errors),
        }
