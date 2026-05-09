from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:
    from . import recordtest
    from .analytics import GameDataAnalyzer
    from .app_paths import get_app_root
    from .config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
except ImportError:
    import recordtest
    from analytics import GameDataAnalyzer
    from app_paths import get_app_root
    from config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository


ROOT_DIR = get_app_root()


class ConfigController:
    """設定ファイル、補完、プレフライトをUIから分離して扱う。"""

    def __init__(self, repository: ConfigRepository | None = None) -> None:
        self.repository = repository or ConfigRepository(CONFIG_PATH, SAMPLE_CONFIG_PATH)

    def apply_auto_defaults(
        self, data: dict[str, Any] | None, force_obs_detect: bool = False
    ) -> tuple[dict[str, Any], bool, list[str]]:
        changed = False
        notes = []

        if not isinstance(data, dict):
            data = {}
            changed = True

        obs = data.setdefault("obs", {})
        paths = data.setdefault("paths", {})
        polling = data.setdefault("polling", {})
        storage = data.setdefault("storage", {})
        app_cfg = data.setdefault("app", {})
        audio_cfg = data.setdefault("audio", {})

        defaults_obs = {
            "host": recordtest.DEFAULT_OBS_HOST,
            "port": recordtest.DEFAULT_OBS_PORT,
            "fps": recordtest.DEFAULT_OBS_FPS,
            "scene_name": recordtest.DEFAULT_OBS_SCENE_NAME,
            "source_name": recordtest.DEFAULT_OBS_SOURCE_NAME,
            "source_color": recordtest.DEFAULT_OBS_SOURCE_COLOR,
            "game_capture_name": recordtest.DEFAULT_OBS_GAME_CAPTURE_NAME,
            "game_capture_window": recordtest.DEFAULT_OBS_GAME_CAPTURE_WINDOW,
        }
        for key, value in defaults_obs.items():
            if obs.get(key) in (None, ""):
                obs[key] = value
                changed = True

        if str(obs.get("password", "")).strip() == "your_password_here":
            obs["password"] = ""
            changed = True
            notes.append("OBSパスワードのプレースホルダを空欄にしました")

        if obs.get("dir") != recordtest.DEFAULT_OBS_DIR:
            obs["dir"] = recordtest.DEFAULT_OBS_DIR
            changed = True
            notes.append(f"OBSフォルダを固定しました: {recordtest.DEFAULT_OBS_DIR}")

        has_valid_dir = bool(recordtest.detect_obs_dir())

        defaults_paths = {
            "bin_dir": recordtest.DEFAULT_BIN_DIR,
            "recordings_dir": recordtest.DEFAULT_RECORDINGS_DIR,
            "json_dir": recordtest.DEFAULT_JSON_DIR,
            "champion_icons_dir": recordtest.DEFAULT_CHAMPION_ICONS_DIR,
            "champion_aliases_path": "config/champion_aliases.json",
        }
        for key, value in defaults_paths.items():
            if paths.get(key) in (None, ""):
                paths[key] = value
                changed = True

        defaults_polling = {
            "end_error_limit": recordtest.DEFAULT_END_ERROR_LIMIT,
            "end_poll_sec": recordtest.DEFAULT_END_POLL_SEC,
            "event_poll_sec": recordtest.DEFAULT_EVENT_POLL_SEC,
        }
        for key, value in defaults_polling.items():
            if polling.get(key) in (None, ""):
                polling[key] = value
                changed = True

        if storage.get("max_size_gb") in (None, ""):
            storage["max_size_gb"] = recordtest.DEFAULT_MAX_STORAGE_GB
            changed = True

        if app_cfg.get("setup_completed") is None:
            app_cfg["setup_completed"] = bool(has_valid_dir)
            changed = True
        elif not bool(app_cfg.get("setup_completed")) and has_valid_dir:
            app_cfg["setup_completed"] = True
            changed = True
        if app_cfg.get("minimize_to_tray") is None:
            app_cfg["minimize_to_tray"] = True
            changed = True

        audio_defaults = recordtest.get_audio_config_defaults()
        for key, defaults in audio_defaults.items():
            slot = audio_cfg.setdefault(key, {})
            if not isinstance(slot, dict):
                audio_cfg[key] = {}
                slot = audio_cfg[key]
                changed = True
            for field, value in defaults.items():
                if slot.get(field) in (None, ""):
                    slot[field] = value
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
        ok, detail = recordtest.test_obs_connection(
            config.obs.host,
            config.obs.port,
            config.obs.password,
        )
        return report, ok, detail

    def total_storage_size(self, config_data: dict[str, Any] | None = None) -> int:
        config = recordtest.AppConfig.from_dict(config_data or self.load_config())
        return recordtest.total_storage_size(config)


class AudioSettingsController:
    """OBS音声・録画出力のインフラ操作をUIから分離する。"""

    def __init__(self, config_controller: ConfigController | None = None) -> None:
        self.config_controller = config_controller or ConfigController()

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
        recordtest.setup_environment(config)
        return report, config

    def _open_recorder(
        self, config: recordtest.AppConfig, auto_launch: bool = False, max_retries: int = 2, retry_delay: float = 0.5
    ) -> tuple[recordtest.LoLAutoRecorder, Any | None]:
        ok, _detail = recordtest.test_obs_connection(
            config.obs.host,
            config.obs.port,
            config.obs.password,
            timeout=1.5,
        )

        launched_process = None
        if not ok and auto_launch:
            launched_process = recordtest.launch_obs(config)

        obs_client = recordtest.ObsWebSocketClient(
            config=config,
            obs_process=launched_process,
            status_cb=None,
            max_retries=max_retries,
            retry_delay=retry_delay,
        )
        recorder = recordtest.LoLAutoRecorder(
            config=config,
            obs_process=launched_process,
            status_cb=None,
            auto_setup=False,
            obs_client=obs_client,
        )
        return recorder, launched_process

    def refresh_audio_devices(self, data: dict[str, Any], auto_launch: bool = True) -> dict[str, Any]:
        report, config = self._prepare_config(data, auto_fix=True, force_obs_detect=True)
        recorder = None
        launched_process = None
        try:
            recorder, launched_process = self._open_recorder(config, auto_launch=auto_launch)
            catalog = recorder.get_audio_device_catalog(cfg=config)
            return {
                "catalog": catalog,
                "config": report["config"],
                "obs_launched": bool(launched_process),
            }
        finally:
            if recorder:
                recorder.disconnect_obs()

    def apply_audio_settings(self, data: dict[str, Any], auto_launch: bool = True) -> dict[str, Any]:
        report, config = self._prepare_config(data, auto_fix=True, force_obs_detect=False)
        recorder = None
        launched_process = None
        try:
            recorder, launched_process = self._open_recorder(config, auto_launch=auto_launch)
            recorder.apply_audio_profile(config)
            self.config_controller.save_config(report["config"])
            return {"config": report["config"], "obs_launched": bool(launched_process)}
        finally:
            if recorder:
                recorder.disconnect_obs()

    def apply_runtime_output_settings(self, data: dict[str, Any]) -> bool:
        report, config = self._prepare_config(data, auto_fix=True, force_obs_detect=False)
        ok, _detail = recordtest.test_obs_connection(
            config.obs.host,
            config.obs.port,
            config.obs.password,
            timeout=1.0,
        )
        if not ok:
            return False

        recorder = None
        try:
            obs_client = recordtest.ObsWebSocketClient(
                config=config,
                status_cb=None,
                max_retries=1,
                retry_delay=0.0,
            )
            recorder = recordtest.LoLAutoRecorder(
                config=config,
                status_cb=None,
                auto_setup=False,
                obs_client=obs_client,
            )
            recorder.apply_record_output_settings()
            return True
        finally:
            if recorder:
                recorder.disconnect_obs()


class RecordingController:
    """録画監視ワーカーが使う録画ランタイム生成を担当する。"""

    def create_recorder(
        self, config_data: dict[str, Any], status_cb: Callable[[str], None] | None = None
    ) -> recordtest.LoLAutoRecorder:
        config = recordtest.AppConfig.from_dict(config_data)
        recordtest.setup_environment(config)
        obs_process = recordtest.launch_obs(config)
        return recordtest.LoLAutoRecorder(
            config=config,
            obs_process=obs_process,
            status_cb=status_cb,
        )


class AnalyticsController:
    """録画JSON分析をUIから分離して提供する。"""

    def __init__(self, config_controller: ConfigController | None = None) -> None:
        self.config_controller = config_controller or ConfigController()

    def load_summary(self) -> dict[str, Any]:
        config_data = self.config_controller.load_config()
        config = recordtest.AppConfig.from_dict(config_data)
        analyzer = GameDataAnalyzer(config=config)
        df = analyzer.load_dataframe()
        horde_result = analyzer.horde_kill_15min_winrate_correlation(df)
        tactical_insights = analyzer.extract_tactical_insights()

        if df.empty:
            return {
                "total_matches": 0,
                "win_rate": None,
                "horde": horde_result,
                "horde_rows": [],
                "tactical_insights": tactical_insights,
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
        }
