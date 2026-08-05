from __future__ import annotations

import importlib
import logging
import re
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import obsws_python as obs

try:
    from .recorder_config import AppConfig
except ImportError:
    from recorder_config import AppConfig


@dataclass(frozen=True)
class OBSRecordingEncoderSelection:
    profile_value: str
    encoder_kind: str
    display_name: str
    hardware: bool


class OBSClient(ABC):
    """OBS制御だけを担当する抽象インターフェース。"""

    @property
    @abstractmethod
    def raw_client(self) -> Any:
        pass

    @abstractmethod
    def connect(self) -> None:
        pass

    @abstractmethod
    def disconnect(self) -> None:
        pass

    @abstractmethod
    def setup_record_output(self) -> None:
        pass

    @abstractmethod
    def setup_sync_elements(self) -> None:
        pass

    @abstractmethod
    def apply_record_output_settings(self) -> bool:
        pass

    @abstractmethod
    def apply_audio_profile(self, cfg: AppConfig, scene_name: str | None = None) -> bool:
        pass

    @abstractmethod
    def get_audio_device_catalog(
        self,
        cfg: AppConfig | None = None,
        scene_name: str | None = None,
    ) -> dict[str, Any]:
        pass

    @abstractmethod
    def get_sync_source_id(self) -> int | None:
        pass

    @abstractmethod
    def set_sync_marker_enabled(self, enabled: bool, source_id: int | None = None) -> None:
        pass

    @abstractmethod
    def start_recording(self) -> None:
        pass

    @abstractmethod
    def stop_recording(self) -> str | None:
        pass

    @abstractmethod
    def is_recording_active(self) -> bool | None:
        pass

    @abstractmethod
    def get_record_status_details(self) -> dict[str, Any]:
        pass

    @abstractmethod
    def shutdown(self) -> None:
        pass


def _recordtest_module() -> Any:
    """Resolve the compatibility facade without creating an import cycle."""

    module_name = f"{__package__}.recordtest" if __package__ else "recordtest"
    module = sys.modules.get(module_name)
    if module is not None:
        return module
    return importlib.import_module(module_name)


def _compat(name: str) -> Any:
    """Keep the established ``recordtest`` monkeypatch boundary working."""

    return getattr(_recordtest_module(), name)


def _recorder_error(message: str) -> Exception:
    return _compat("RecorderError")(message)


def _safe_int(
    value: Any,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> tuple[int, bool]:
    try:
        parsed = int(value)
    except Exception:
        return default, False
    if minimum is not None and parsed < minimum:
        return default, False
    if maximum is not None and parsed > maximum:
        return default, False
    return parsed, True


def test_obs_connection(
    host: str | None,
    port: int | str | None,
    password: str | None,
    timeout: float = 2.5,
) -> tuple[bool, str]:
    host_text = str(host or "").strip() or _compat("DEFAULT_OBS_HOST")
    try:
        port_num = int(port)
    except Exception:
        return False, f"OBSポートが不正です: {port}"

    host_candidates = [host_text]
    if host_text == "localhost":
        host_candidates.append("127.0.0.1")
    elif host_text == "127.0.0.1":
        host_candidates.append("localhost")

    last_error = None
    for candidate in host_candidates:
        client = None
        primary_error: BaseException | None = None
        try:
            client = obs.ReqClient(
                host=candidate,
                port=port_num,
                password=password or "",
                timeout=timeout,
            )
            version = client.get_version()
            suffix = f" (host={candidate})" if candidate != host_text else ""
            return True, f"接続成功: OBS {version.obs_version}{suffix}"
        except BaseException as exc:
            primary_error = exc
            if not isinstance(exc, Exception):
                raise
            last_error = exc
            message = f"{type(exc).__name__}: {exc}".lower()
            if any(token in message for token in ("auth", "authentication", "password", "identify")):
                return False, "OBSには到達しましたが認証に失敗しました。WebSocketパスワードを確認してください。"
        finally:
            if client is not None:
                try:
                    client.disconnect()
                except BaseException as cleanup_error:
                    logger = _compat("LOGGER")
                    if primary_error is None:
                        if not isinstance(cleanup_error, Exception):
                            raise
                        logger.error(
                            "OBS接続確認後のWebSocket切断に失敗しました: %s: %s",
                            type(cleanup_error).__name__,
                            cleanup_error,
                        )
                    else:
                        selected_error = _compat("_select_cleanup_control_flow_error")(
                            primary_error,
                            cleanup_error,
                            logger=logger,
                            context="OBS接続確認後のWebSocket切断にも失敗しました",
                        )
                        if selected_error is cleanup_error:
                            raise

    if last_error:
        return (
            False,
            "OBS WebSocket に接続できません。OBS設定で WebSocket有効化 / ポート番号 を確認してください。\n"
            f"詳細: {type(last_error).__name__}: {last_error}",
        )
    return False, "OBS接続テストに失敗しました。"


def connect_obs_client(
    host: str | None,
    port: int | str | None,
    password: str | None,
    timeout: float = 2.5,
) -> tuple[Any, str]:
    host_text = str(host or "").strip() or _compat("DEFAULT_OBS_HOST")
    port_num, ok = _safe_int(
        port,
        _compat("DEFAULT_OBS_PORT"),
        minimum=1,
        maximum=65535,
    )
    if not ok:
        raise _recorder_error(f"OBSポートが不正です: {port}")

    host_candidates = [host_text]
    if host_text == "localhost":
        host_candidates.append("127.0.0.1")
    elif host_text == "127.0.0.1":
        host_candidates.append("localhost")

    last_error = None
    for candidate in host_candidates:
        try:
            client = obs.ReqClient(
                host=candidate,
                port=port_num,
                password=password or "",
                timeout=timeout,
            )
            return client, candidate
        except Exception as exc:
            last_error = exc
            message = f"{type(exc).__name__}: {exc}".lower()
            if any(token in message for token in ("auth", "authentication", "password", "identify")):
                raise _recorder_error(
                    "OBSには到達しましたが認証に失敗しました。WebSocketパスワードを確認してください。"
                ) from exc

    raise _recorder_error(
        f"OBS WebSocket に接続できません。\n接続先: {host_text}:{port_num}\n詳細: {last_error}"
    ) from last_error


def _obs_raw(client: Any, request_type: str, payload: dict[str, Any] | None = None) -> Any:
    try:
        return client.send(request_type, payload or {}, raw=True)
    except TypeError:
        return client.send(request_type, payload or {})


def _obs_response_value(response: Any, *keys: str) -> Any:
    if isinstance(response, dict):
        for key in keys:
            if key in response:
                return response[key]
    for key in keys:
        if hasattr(response, key):
            return getattr(response, key)
    return None


def _camel_to_snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _raise_for_obs_request_status(response: Any, request_type: str) -> None:
    if not isinstance(response, dict):
        return
    status = response.get("requestStatus")
    if not isinstance(status, dict) or status.get("result") is not False:
        return
    code = status.get("code", "?")
    comment = status.get("comment") or status.get("message") or response
    raise _recorder_error(f"OBS request failed: request={request_type}, code={code}, detail={comment}")


class ObsWebSocketClient(OBSClient):
    """obs-websocketを使う本番用OBSクライアント。"""

    def __init__(
        self,
        config: AppConfig | None = None,
        obs_process: subprocess.Popen[Any] | None = None,
        status_cb: Callable[[str], None] | None = None,
        max_retries: int = 5,
        retry_delay: float = 2.0,
    ) -> None:
        self.config = config or _compat("load_app_config")()
        self.client = None
        self.obs_process = obs_process
        self.status_cb = status_cb
        self.max_retries = int(max_retries)
        self.retry_delay = float(retry_delay)
        self.logger = logging.getLogger(f"lol_replay.obs.{id(self)}")
        handler_type = _compat("StatusCallbackLogHandler")
        self._status_handler = handler_type(status_cb) if status_cb else None
        self.last_recording_encoder_selection: OBSRecordingEncoderSelection | None = None
        if self._status_handler:
            self.logger.addHandler(self._status_handler)
        self.logger.propagate = True

    @property
    def raw_client(self) -> Any:
        return self.client

    def log(self, message: str) -> None:
        self.logger.info(message)

    def connect(self) -> None:
        retry_count = 0
        last_error = None
        max_retries = max(1, self.max_retries)
        while retry_count < max_retries:
            try:
                self.client, used_host = _compat("connect_obs_client")(
                    self.config.obs.host,
                    self.config.obs.port,
                    self.config.obs.password,
                )
                version = self.client.get_version()
                host_note = f" host={used_host}" if used_host != self.config.obs.host else ""
                self.log(f"✅ OBS接続成功 (v{version.obs_version}{host_note})")
                return
            except Exception as exc:
                last_error = exc
                retry_count += 1
                self.logger.info("Connection retrying... (%s/%s)", retry_count, max_retries)
                if retry_count < max_retries and self.retry_delay > 0:
                    time.sleep(self.retry_delay)

        raise _recorder_error(
            "OBS WebSocketへの接続に失敗しました。\n"
            f"接続先: {self.config.obs.host}:{self.config.obs.port}\n"
            f"パスワード設定: {'あり' if self.config.obs.password else 'なし'}\n"
            f"詳細: {last_error}"
        )

    def disconnect(self) -> None:
        try:
            if self.client is not None:
                try:
                    self.client.disconnect()
                finally:
                    self.client = None
        finally:
            if self._status_handler is not None:
                try:
                    self.logger.removeHandler(self._status_handler)
                except Exception:
                    pass
                self._status_handler = None

    def setup_record_output(self) -> None:
        self._apply_record_output_basics()

        try:
            _compat("apply_obs_video_settings")(
                self.client,
                self.config.obs.fps_numerator,
                fps_denominator=self.config.obs.fps_denominator,
                base_width=self.config.obs.base_width,
                base_height=self.config.obs.base_height,
                output_width=self.config.obs.output_width,
                output_height=self.config.obs.output_height,
            )
            self.log(
                "🎞️ OBS映像設定を適用しました: "
                f"{self.config.obs.base_width}x{self.config.obs.base_height} -> "
                f"{self.config.obs.output_width}x{self.config.obs.output_height} / "
                f"{self.config.obs.fps_numerator}/{self.config.obs.fps_denominator} FPS"
            )
        except Exception as exc:
            self.log(f"⚠️ OBS映像設定の適用に失敗: {exc}")

        self._apply_recording_quality_settings()

    def _apply_record_output_basics(self) -> None:
        _compat("disable_obs_global_audio_devices")(self.client)
        self.log("🔇 OBSのデスクトップ音声とグローバル音声入力を無効化しました。")

        if self.config.paths.recordings_dir:
            record_dir = _compat("validate_recording_directory")(self.config.paths.recordings_dir)
            _compat("apply_record_directory_to_obs")(self.client, record_dir)

    def _apply_recording_quality_settings(
        self,
        recording_encoder: str | None = None,
        *,
        raise_on_error: bool = False,
    ) -> OBSRecordingEncoderSelection | None:
        try:
            requested_encoder = self.config.obs.recording_encoder if recording_encoder is None else recording_encoder
            record_dir = None
            if self.config.paths.recordings_dir:
                record_dir = _compat("validate_recording_directory")(self.config.paths.recordings_dir)
            selected_encoder = _compat("apply_obs_recording_quality_settings")(
                self.client,
                scale_type=self.config.obs.scale_type,
                recording_quality=self.config.obs.recording_quality,
                recording_encoder=requested_encoder,
                obs_dir=self.config.obs.obs_dir,
                record_dir=record_dir,
            )
            self.last_recording_encoder_selection = selected_encoder
            self.log(
                "🎞️ OBS録画品質を適用しました: "
                f"quality={self.config.obs.recording_quality}, scale={self.config.obs.scale_type}, "
                f"encoder={selected_encoder.display_name} ({selected_encoder.encoder_kind}), "
                f"format={_compat('DEFAULT_OBS_RECORDING_FORMAT')}, "
                f"mode={_compat('DEFAULT_OBS_OUTPUT_MODE')}"
            )
            return selected_encoder
        except Exception as exc:
            self.log(f"⚠️ OBS録画品質設定の適用に失敗: {exc}")
            if raise_on_error:
                raise
            return None

    def apply_record_output_settings(self) -> bool:
        self.setup_record_output()
        return True

    def apply_audio_profile(self, cfg: AppConfig, scene_name: str | None = None) -> bool:
        return _compat("apply_audio_profile_from_config")(
            self.client,
            cfg,
            scene_name=scene_name or self.config.obs.scene_name,
            status_cb=self.log,
        )

    def get_audio_device_catalog(
        self,
        cfg: AppConfig | None = None,
        scene_name: str | None = None,
    ) -> dict[str, Any]:
        return _compat("get_audio_device_catalog")(
            self.client,
            cfg=cfg,
            scene_name=scene_name or self.config.obs.scene_name,
            status_cb=self.log,
        )

    def setup_sync_elements(self) -> None:
        try:
            self._ensure_scene_exists()
            self._set_current_scene()
            window_capture_item_id = self._ensure_window_capture_exists()
            self._fit_window_capture_to_canvas(window_capture_item_id)
            sync_source_item_id = self._ensure_sync_source_exists()
            self._remove_legacy_game_capture_sources()
            self._apply_scene_item_z_order(window_capture_item_id, sync_source_item_id)
            self._remove_empty_initial_scenes()
        except Exception as exc:
            self.logger.exception("OBS同期要素セットアップに失敗しました。")
            if isinstance(exc, _compat("RecorderError")):
                raise
            raise _recorder_error(f"OBS同期要素セットアップに失敗しました: {exc}") from exc

    def _ensure_scene_exists(self) -> None:
        scene_name = self.config.obs.scene_name
        try:
            _compat("ensure_obs_scene_exists")(self.client, scene_name, status_cb=self.log)
        except Exception as exc:
            raise _recorder_error(f"シーン '{scene_name}' の自動作成に失敗しました: {exc}") from exc

    def _set_current_scene(self) -> None:
        scene_name = self.config.obs.scene_name
        try:
            self.client.set_current_program_scene(scene_name)
        except Exception as exc:
            self.log(f"⚠️ 現在シーンの切り替えに失敗: {exc}")

    def _remove_empty_initial_scenes(self) -> None:
        target_scene = self.config.obs.scene_name
        try:
            scene_resp = self.client.get_scene_list()
            scenes = getattr(scene_resp, "scenes", []) or []
        except Exception as exc:
            self.log(f"⚠️ 初期シーン一覧の取得に失敗: {exc}")
            return

        initial_names = _compat("DEFAULT_OBS_INITIAL_SCENE_NAMES")
        for item in scenes:
            if not isinstance(item, dict):
                continue
            scene_name = str(item.get("sceneName") or "")
            if scene_name == target_scene or scene_name not in initial_names:
                continue
            try:
                scene_items = getattr(self.client.get_scene_item_list(scene_name), "scene_items", []) or []
            except Exception as exc:
                self.log(f"⚠️ 初期シーン '{scene_name}' の中身を確認できません: {exc}")
                continue
            if scene_items:
                continue
            try:
                self.client.remove_scene(scene_name)
                self.log(f"ℹ️ OBS初期シーン '{scene_name}' を削除しました。")
            except Exception as exc:
                self.log(f"⚠️ OBS初期シーン '{scene_name}' の削除に失敗: {exc}")

    def _get_input_kind(self, source_name: str) -> str | None:
        try:
            input_resp = self.client.get_input_list()
            input_items = getattr(input_resp, "inputs", []) or []
            for item in input_items:
                if isinstance(item, dict) and item.get("inputName") == source_name:
                    return str(item.get("inputKind") or "")
        except Exception as exc:
            self.log(f"⚠️ OBS入力一覧の取得に失敗: {exc}")
        return None

    def _remove_legacy_game_capture_sources(self) -> None:
        legacy_name = _compat("DEFAULT_OBS_GAME_CAPTURE_NAME")
        if self.config.obs.window_capture_name == legacy_name:
            return
        input_kind = self._get_input_kind(legacy_name)
        if input_kind != "game_capture":
            return
        try:
            self.client.remove_input(legacy_name)
            self.log(f"ℹ️ 旧ゲームキャプチャ '{legacy_name}' を削除しました。")
        except Exception as exc:
            self.log(f"⚠️ 旧ゲームキャプチャ '{legacy_name}' の削除に失敗: {exc}")

    def _ensure_window_capture_exists(self) -> int:
        scene_name = self.config.obs.scene_name
        source_name = self.config.obs.window_capture_name
        settings = {
            "window": self.config.obs.window_capture_window,
            "method": self.config.obs.window_capture_method,
            "priority": _compat("DEFAULT_OBS_WINDOW_CAPTURE_PRIORITY"),
            "cursor": False,
            "client_area": True,
            "capture_audio": _compat("DEFAULT_OBS_WINDOW_CAPTURE_AUDIO"),
            "force_sdr": False,
        }

        input_kind = self._get_input_kind(source_name)
        input_exists = input_kind is not None
        if input_exists and input_kind != "window_capture":
            try:
                self.client.remove_input(source_name)
                input_exists = False
            except Exception as exc:
                raise _recorder_error(
                    f"ウィンドウキャプチャ名 '{source_name}' は存在しますが、"
                    f"種別が window_capture ではありません: {exc}"
                ) from exc

        if not input_exists:
            self.log(f"ℹ️ ウィンドウキャプチャ '{source_name}' を自動作成します。")
            try:
                self.client.create_input(scene_name, source_name, "window_capture", settings, True)
            except Exception:
                self.logger.warning(
                    "ウィンドウキャプチャ '%s' の詳細設定付き作成に失敗しました。最小設定で再試行します。",
                    source_name,
                    exc_info=True,
                )
                fallback_settings = self._window_capture_fallback_settings()
                try:
                    self.client.create_input(
                        scene_name,
                        source_name,
                        "window_capture",
                        fallback_settings,
                        True,
                    )
                except Exception as fallback_error:
                    raise _recorder_error(
                        f"ウィンドウキャプチャ '{source_name}' の自動作成に失敗しました: "
                        f"{fallback_error}"
                    ) from fallback_error
        else:
            try:
                self.client.set_input_settings(source_name, settings, overlay=True)
            except Exception:
                fallback_settings = self._window_capture_fallback_settings()
                try:
                    self.client.set_input_settings(source_name, fallback_settings, overlay=True)
                except Exception as fallback_error:
                    raise _recorder_error(
                        f"ウィンドウキャプチャ '{source_name}' の設定更新に失敗しました: "
                        f"{fallback_error}"
                    ) from fallback_error

        scene_item_id = self._get_scene_item_id(source_name)
        if scene_item_id is None:
            try:
                self.client.create_scene_item(scene_name, source_name, True)
                scene_item_id = self._get_scene_item_id(source_name)
            except Exception as exc:
                raise _recorder_error(
                    f"ウィンドウキャプチャ '{source_name}' をシーン '{scene_name}' に配置できませんでした: "
                    f"{exc}"
                ) from exc

        if scene_item_id is None:
            raise _recorder_error(
                f"ウィンドウキャプチャ '{source_name}' は存在しますが、"
                f"シーン '{scene_name}' で見つかりません。"
            )
        return scene_item_id

    def _window_capture_fallback_settings(self) -> dict[str, Any]:
        return {
            "window": self.config.obs.window_capture_window,
            "method": self.config.obs.window_capture_method,
            "priority": _compat("DEFAULT_OBS_WINDOW_CAPTURE_PRIORITY"),
            "capture_audio": _compat("DEFAULT_OBS_WINDOW_CAPTURE_AUDIO"),
        }

    def _fit_window_capture_to_canvas(self, scene_item_id: int) -> None:
        scene_name = self.config.obs.scene_name
        transform = {
            "positionX": 0.0,
            "positionY": 0.0,
            "alignment": 5,
            "boundsType": "OBS_BOUNDS_SCALE_INNER",
            "boundsAlignment": 0,
            "boundsWidth": float(self.config.obs.base_width),
            "boundsHeight": float(self.config.obs.base_height),
        }
        try:
            self.client.set_scene_item_transform(scene_name, scene_item_id, transform)
        except Exception as exc:
            self.log(f"⚠️ ウィンドウキャプチャをキャンバスへフィットできませんでした: {exc}")

    def _ensure_sync_source_exists(self) -> int:
        scene_name = self.config.obs.scene_name
        source_name = self.config.obs.source_name
        try:
            input_resp = self.client.get_input_list()
            input_items = getattr(input_resp, "inputs", []) or []
            input_exists = any(
                isinstance(item, dict) and item.get("inputName") == source_name
                for item in input_items
            )
        except Exception:
            input_exists = False

        if not input_exists:
            self.log(f"ℹ️ 色ソース '{source_name}' を自動作成します。")
            settings = {"color": self.config.obs.source_color, "width": 100, "height": 100}
            last_error = None
            for kind in ("color_source_v3", "color_source"):
                try:
                    self.client.create_input(scene_name, source_name, kind, settings, False)
                    input_exists = True
                    break
                except Exception as exc:
                    last_error = exc
            if not input_exists:
                raise _recorder_error(f"色ソース '{source_name}' の自動作成に失敗しました: {last_error}")
        else:
            try:
                self.client.set_input_settings(
                    source_name,
                    {"color": self.config.obs.source_color},
                    overlay=True,
                )
            except Exception:
                pass

        scene_item_id = self.get_sync_source_id()
        if scene_item_id is None:
            try:
                self.client.create_scene_item(scene_name, source_name, False)
                scene_item_id = self.get_sync_source_id()
            except Exception as exc:
                raise _recorder_error(
                    f"色ソース '{source_name}' をシーン '{scene_name}' に配置できませんでした: {exc}"
                ) from exc

        if scene_item_id is None:
            raise _recorder_error(
                f"色ソース '{source_name}' は存在しますが、シーン '{scene_name}' で見つかりません。"
            )

        try:
            self.client.set_scene_item_transform(
                scene_name,
                scene_item_id,
                {"positionX": 0.0, "positionY": 0.0, "alignment": 5},
            )
        except Exception:
            pass

        try:
            self.set_sync_marker_enabled(False, scene_item_id)
        except Exception:
            pass
        return scene_item_id

    def _get_scene_item_id(self, source_name: str) -> int | None:
        try:
            items = self.client.get_scene_item_list(self.config.obs.scene_name).scene_items
            for item in items:
                if not isinstance(item, dict) or item.get("sourceName") != source_name:
                    continue
                item_id = item.get("sceneItemId")
                return int(item_id) if item_id is not None else None
        except Exception as exc:
            self.logger.warning("⚠️ シーンアイテム取得エラー: %s", exc)
        return None

    def _apply_scene_item_z_order(self, capture_source_item_id: int, sync_source_item_id: int) -> None:
        scene_name = self.config.obs.scene_name
        try:
            self.client.set_scene_item_index(scene_name, capture_source_item_id, 0)
            items = getattr(self.client.get_scene_item_list(scene_name), "scene_items", []) or []
            self.client.set_scene_item_index(scene_name, sync_source_item_id, max(0, len(items) - 1))
        except Exception as exc:
            self.log(f"⚠️ シーンアイテムの重なり順制御に失敗: {exc}")

    def get_sync_source_id(self) -> int | None:
        return self._get_scene_item_id(self.config.obs.source_name)

    def set_sync_marker_enabled(self, enabled: bool, source_id: int | None = None) -> None:
        item_id = source_id if source_id is not None else self.get_sync_source_id()
        if item_id is None:
            raise _recorder_error(
                f"同期用ソース '{self.config.obs.source_name}' が"
                f"シーン '{self.config.obs.scene_name}' に見つかりません。"
            )
        self.client.set_scene_item_enabled(self.config.obs.scene_name, item_id, bool(enabled))

    def start_recording(self) -> None:
        response = _obs_raw(self.client, "StartRecord")
        _raise_for_obs_request_status(response, "StartRecord")

    def toggle_recording(self) -> None:
        response = _obs_raw(self.client, "ToggleRecord")
        _raise_for_obs_request_status(response, "ToggleRecord")

    def prepare_recording_start(self) -> None:
        self._apply_record_output_basics()
        self._apply_recording_quality_settings()

    def set_recording_encoder(self, recording_encoder: str) -> OBSRecordingEncoderSelection:
        selected_encoder = self._apply_recording_quality_settings(
            recording_encoder=recording_encoder,
            raise_on_error=True,
        )
        if selected_encoder is None:
            raise _recorder_error("OBS録画エンコーダの切り替えに失敗しました。")
        self.log(
            "🎞️ OBS録画エンコーダを切り替えました: "
            f"encoder={selected_encoder.display_name} ({selected_encoder.encoder_kind})"
        )
        return selected_encoder

    def stop_recording(self) -> str | None:
        response = self.client.stop_record()
        return getattr(response, "output_path", None)

    def is_recording_active(self) -> bool | None:
        status = self.client.get_record_status()
        return getattr(status, "output_active", None)

    def get_record_status_details(self) -> dict[str, Any]:
        status = self.client.get_record_status()
        details = {
            "output_active": getattr(status, "output_active", None),
            "output_paused": getattr(status, "output_paused", None),
            "output_timecode": getattr(status, "output_timecode", None),
            "output_duration": getattr(status, "output_duration", None),
            "output_bytes": getattr(status, "output_bytes", None),
        }
        for category, name in (
            ("Output", "Mode"),
            ("SimpleOutput", "FilePath"),
            ("SimpleOutput", "RecFormat2"),
            ("SimpleOutput", "RecQuality"),
            ("SimpleOutput", "RecEncoder"),
            ("AdvOut", "RecEncoder"),
        ):
            value = _compat("get_obs_profile_parameter_value")(self.client, category, name)
            if value is not None:
                details[f"{category}.{name}"] = value

        try:
            profile_list = _obs_raw(self.client, "GetProfileList")
            details["OBS.current_profile"] = _obs_response_value(
                profile_list,
                "currentProfileName",
                "current_profile_name",
            )
        except Exception as exc:
            details["OBS.current_profile_error"] = f"{type(exc).__name__}: {exc}"

        try:
            scene_collections = _obs_raw(self.client, "GetSceneCollectionList")
            details["OBS.current_scene_collection"] = _obs_response_value(
                scene_collections,
                "currentSceneCollectionName",
                "current_scene_collection_name",
            )
        except Exception as exc:
            details["OBS.current_scene_collection_error"] = f"{type(exc).__name__}: {exc}"

        try:
            output_list = _obs_raw(self.client, "GetOutputList")
            outputs = _obs_response_value(output_list, "outputs") or []
            output_summaries = []
            for item in outputs:
                if not isinstance(item, dict):
                    continue
                name = item.get("outputName") or item.get("output_name")
                kind = item.get("outputKind") or item.get("output_kind")
                active = item.get("outputActive") if "outputActive" in item else item.get("output_active")
                if name:
                    output_summaries.append(f"{name}({kind}, active={active})")
            if output_summaries:
                details["OBS.outputs"] = "; ".join(output_summaries)
        except Exception as exc:
            details["OBS.outputs_error"] = f"{type(exc).__name__}: {exc}"

        try:
            simple_status = _obs_raw(
                self.client,
                "GetOutputStatus",
                {"outputName": "simple_file_output"},
            )
            for key, label in (
                ("outputActive", "active"),
                ("outputBytes", "bytes"),
                ("outputDuration", "duration"),
                ("outputSkippedFrames", "skipped_frames"),
                ("outputTotalFrames", "total_frames"),
            ):
                value = _obs_response_value(simple_status, key, _camel_to_snake(key))
                if value is not None:
                    details[f"simple_file_output.{label}"] = value
        except Exception as exc:
            details["simple_file_output.status_error"] = f"{type(exc).__name__}: {exc}"

        try:
            simple_settings = _obs_raw(
                self.client,
                "GetOutputSettings",
                {"outputName": "simple_file_output"},
            )
            output_settings = _obs_response_value(
                simple_settings,
                "outputSettings",
                "output_settings",
            )
            if isinstance(output_settings, dict):
                for key in ("path", "muxer_settings"):
                    if output_settings.get(key) not in (None, ""):
                        details[f"simple_file_output.{key}"] = output_settings.get(key)
        except Exception as exc:
            details["simple_file_output.settings_error"] = f"{type(exc).__name__}: {exc}"
        return details

    def shutdown(self) -> None:
        termination_error: BaseException | None = None
        if self.obs_process is not None:
            self.log("🧹 OBSを終了しています...")
            try:
                _compat("OBSProcessManager")(
                    self.config.obs.obs_dir,
                    logger=self.logger,
                ).terminate_process(self.obs_process)
                self.obs_process = None
            except BaseException as exc:
                termination_error = exc

        disconnect_error: BaseException | None = None
        try:
            self.disconnect()
        except BaseException as exc:
            disconnect_error = exc

        if termination_error is not None:
            if disconnect_error is not None:
                termination_error = _compat("_select_cleanup_control_flow_error")(
                    termination_error,
                    disconnect_error,
                    logger=self.logger,
                    context="OBS process終了失敗後のWebSocket切断にも失敗しました",
                )
            raise termination_error
        if disconnect_error is not None:
            raise disconnect_error
