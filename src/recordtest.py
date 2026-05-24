from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import aiohttp
import obsws_python as obs
import urllib3
from obsws_python.error import OBSSDKRequestError

try:
    from .app_paths import get_app_root, get_user_data_root
    from .config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
    from .mpv_support import has_mpv_dll
    from .obs_bootstrap import (
        OBSBootstrapper as SharedOBSBootstrapper,
        copy_obs_tree_contents,
        get_obs_config_dir as shared_get_obs_config_dir,
        get_obs_global_ini_path as shared_get_obs_global_ini_path,
        get_obs_user_ini_path as shared_get_obs_user_ini_path,
        get_obs_websocket_config_path as shared_get_obs_websocket_config_path,
        get_portable_marker_path as shared_get_portable_marker_path,
    )
    from .obs_process import OBSProcessManager
    from .recording_state import FinalizeResult, RecordingEndDetector, RecordingOutcome, RecordingPhase
    from .session_log import SessionLogV1, load_session_payload, save_session_payload
except ImportError:
    from app_paths import get_app_root, get_user_data_root
    from config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
    from mpv_support import has_mpv_dll
    from obs_bootstrap import (
        OBSBootstrapper as SharedOBSBootstrapper,
        copy_obs_tree_contents,
        get_obs_config_dir as shared_get_obs_config_dir,
        get_obs_global_ini_path as shared_get_obs_global_ini_path,
        get_obs_user_ini_path as shared_get_obs_user_ini_path,
        get_obs_websocket_config_path as shared_get_obs_websocket_config_path,
        get_portable_marker_path as shared_get_portable_marker_path,
    )
    from obs_process import OBSProcessManager
    from recording_state import FinalizeResult, RecordingEndDetector, RecordingOutcome, RecordingPhase
    from session_log import SessionLogV1, load_session_payload, save_session_payload

ROOT_DIR = get_app_root()
DATA_DIR = get_user_data_root()
CONFIG_REPOSITORY = ConfigRepository(CONFIG_PATH, SAMPLE_CONFIG_PATH)

LIVECLIENT_BASE = "https://127.0.0.1:2999/liveclientdata"
ACTIVE_PLAYER_URL = f"{LIVECLIENT_BASE}/activeplayername"
EVENT_URL = f"{LIVECLIENT_BASE}/eventdata"
ALL_GAME_URL = f"{LIVECLIENT_BASE}/allgamedata"

DEFAULT_OBS_PASSWORD_LENGTH = 24
DEFAULT_OBS_SCENE_NAME = "lol_seen"
DEFAULT_OBS_SOURCE_NAME = "color"
# OBS color_source の color 値は ABGR。赤は 0xFF0000FF。
DEFAULT_OBS_SOURCE_COLOR = 0xFF0000FF
LEGACY_OBS_SOURCE_COLOR_BLUE = 0xFFFF0000
DEFAULT_OBS_WINDOW_CAPTURE_NAME = "lol_window_capture"
DEFAULT_OBS_WINDOW_CAPTURE_WINDOW = "League of Legends (TM) Client:RiotWindowClass:League of Legends.exe"
DEFAULT_OBS_WINDOW_CAPTURE_METHOD = 2  # Windows Graphics Capture
DEFAULT_OBS_WINDOW_CAPTURE_PRIORITY = 2  # Match by executable
DEFAULT_OBS_INITIAL_SCENE_NAMES = frozenset({"Scene", "シーン"})
DEFAULT_OBS_GAME_CAPTURE_NAME = "lol_game_capture"
DEFAULT_OBS_LEGACY_GAME_CAPTURE_WINDOW = "League of Legends (TM) Client:League of Legends.exe:League of Legends.exe"
DEFAULT_OBS_GAME_CAPTURE_WINDOW = DEFAULT_OBS_LEGACY_GAME_CAPTURE_WINDOW
DEFAULT_OBS_DIR = "obs-portable"
LEGACY_OBS_DIR = "bin/OBS-Studio"
DEFAULT_BIN_DIR = "bin"
DEFAULT_RECORDINGS_DIR = "recordings"
DEFAULT_JSON_DIR = "recordings/json"
DEFAULT_CHAMPION_ICONS_DIR = "assets/champions/icons"
DEFAULT_OBS_HOST = "localhost"
DEFAULT_OBS_PORT = 4455
DEFAULT_OBS_FPS = 60
DEFAULT_END_ERROR_LIMIT = 3
DEFAULT_END_MISSING_GRACE_SEC = 60.0
DEFAULT_END_POLL_SEC = 5
DEFAULT_EVENT_POLL_SEC = 1
DEFAULT_MAX_STORAGE_GB = 50
DEFAULT_AUDIO_DESKTOP_INPUT_NAME = "lol_desktop_audio"
DEFAULT_AUDIO_MIC_INPUT_NAME = "lol_mic_audio"
DEFAULT_AUDIO_DEVICE_ID = "default"
DEFAULT_AUDIO_DEVICE_NAME = "Default"
DEFAULT_AUDIO_DESKTOP_VOLUME_DB = 0.0
DEFAULT_AUDIO_MIC_VOLUME_DB = 0.0
DEFAULT_AUDIO_DESKTOP_MUTE = False
DEFAULT_AUDIO_MIC_MUTE = False

MANAGED_PORTABLE_OBS_DIR = (DATA_DIR / DEFAULT_OBS_DIR).resolve()
LEGACY_MANAGED_OBS_DIR = (ROOT_DIR / LEGACY_OBS_DIR).resolve()
LEGACY_ROOT_OBS_DIR = (ROOT_DIR / DEFAULT_OBS_DIR).resolve()
LEGACY_DATA_BIN_OBS_DIR = (DATA_DIR / LEGACY_OBS_DIR).resolve()
PORTABLE_OBS_MARKER_NAME = "obs_portable_mode.txt"
LEGACY_PORTABLE_OBS_MARKER_NAME = "portable_mode.txt"
MANAGED_AUDIO_INPUTS = {
    "desktop": {
        "label": "デスクトップ音声",
        "input_name": DEFAULT_AUDIO_DESKTOP_INPUT_NAME,
        "input_kind": "wasapi_output_capture",
        "default_volume_db": DEFAULT_AUDIO_DESKTOP_VOLUME_DB,
        "default_mute": DEFAULT_AUDIO_DESKTOP_MUTE,
    },
    "mic": {
        "label": "マイク入力",
        "input_name": DEFAULT_AUDIO_MIC_INPUT_NAME,
        "input_kind": "wasapi_input_capture",
        "default_volume_db": DEFAULT_AUDIO_MIC_VOLUME_DB,
        "default_mute": DEFAULT_AUDIO_MIC_MUTE,
    },
}

LOG_DIR = DATA_DIR / "logs"
LOGGER = logging.getLogger("lol_replay")
OBS_OPERATION_LOCK = threading.RLock()


class StatusCallbackLogHandler(logging.Handler):
    """UIへログメッセージを転送する軽量ハンドラ。"""

    def __init__(self, callback: Callable[[str], None] | None) -> None:
        super().__init__(level=logging.INFO)
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        if not self.callback:
            return
        try:
            self.callback(record.getMessage())
        except Exception:
            self.handleError(record)


def configure_logging() -> None:
    if getattr(configure_logging, "_configured", False):
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler = TimedRotatingFileHandler(
        LOG_DIR / "app.log",
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    LOGGER.addHandler(stream_handler)

    configure_logging._configured = True


configure_logging()


class RecorderError(RuntimeError):
    pass


def generate_obs_password(length: int = DEFAULT_OBS_PASSWORD_LENGTH) -> str:
    """Generate a local-only obs-websocket password for managed OBS."""

    token = secrets.token_urlsafe(max(18, int(length)))
    return token[: max(12, int(length))]


def is_missing_obs_password(value: Any) -> bool:
    return str(value or "").strip() in {"", "your_password_here"}


def ensure_obs_password_value(value: Any) -> tuple[str, bool]:
    if is_missing_obs_password(value):
        return generate_obs_password(), True
    return str(value).strip(), False


class RiotPollStatus(str, Enum):
    IN_GAME = "in_game"
    NOT_IN_GAME = "not_in_game"
    TEMPORARY_FAILURE = "temporary_failure"


@dataclass(frozen=True)
class RiotPollResult:
    status: RiotPollStatus
    payload: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class OBSSettings:
    host: str
    port: int
    password: str
    scene_name: str
    source_name: str
    source_color: int
    window_capture_name: str
    window_capture_window: str
    window_capture_method: int
    fps: int
    obs_dir: Path

    @property
    def game_capture_name(self) -> str:
        return self.window_capture_name

    @property
    def game_capture_window(self) -> str:
        return self.window_capture_window


@dataclass(frozen=True)
class PathsSettings:
    bin_dir: Path
    recordings_dir: Path
    json_dir: Path
    champion_icons_dir: Path


@dataclass(frozen=True)
class PollingSettings:
    end_error_limit: int
    end_missing_grace_sec: float
    end_poll_sec: float
    event_poll_sec: float


@dataclass(frozen=True)
class StorageSettings:
    max_size_gb: float
    max_size_bytes: int | None


@dataclass(frozen=True)
class AudioSlotSettings:
    input_name: str
    device_id: str
    device_name: str
    volume_db: float
    mute: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_name": self.input_name,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "volume_db": self.volume_db,
            "mute": self.mute,
        }


@dataclass(frozen=True)
class AudioSettings:
    desktop: AudioSlotSettings
    mic: AudioSlotSettings

    def to_dict(self) -> dict[str, Any]:
        return {
            "desktop": self.desktop.to_dict(),
            "mic": self.mic.to_dict(),
        }


@dataclass(frozen=True)
class AppConfig:
    obs: OBSSettings
    paths: PathsSettings
    polling: PollingSettings
    storage: StorageSettings
    audio: AudioSettings

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AppConfig:
        source = data if isinstance(data, dict) else {}
        obs_cfg = source.get("obs", {}) if isinstance(source.get("obs", {}), dict) else {}
        paths_cfg = source.get("paths", {}) if isinstance(source.get("paths", {}), dict) else {}
        polling_cfg = source.get("polling", {}) if isinstance(source.get("polling", {}), dict) else {}
        storage_cfg = source.get("storage", {}) if isinstance(source.get("storage", {}), dict) else {}

        source_color, _ = parse_obs_source_color(
            obs_cfg.get("source_color"),
            default=DEFAULT_OBS_SOURCE_COLOR,
        )
        fps, _ = _safe_int(obs_cfg.get("fps"), DEFAULT_OBS_FPS, minimum=1, maximum=240)
        port, _ = _safe_int(obs_cfg.get("port"), DEFAULT_OBS_PORT, minimum=1, maximum=65535)
        window_capture_method, _ = _safe_int(
            obs_cfg.get("window_capture_method"),
            DEFAULT_OBS_WINDOW_CAPTURE_METHOD,
            minimum=0,
            maximum=2,
        )
        window_capture_name = normalize_window_capture_source_name(
            obs_cfg.get("window_capture_name"),
            legacy_value=obs_cfg.get("game_capture_name"),
        )
        window_capture_window = normalize_window_capture_window_selector(
            obs_cfg.get("window_capture_window"),
            legacy_value=obs_cfg.get("game_capture_window"),
        )
        end_limit, _ = _safe_int(
            polling_cfg.get("end_error_limit"),
            DEFAULT_END_ERROR_LIMIT,
            minimum=1,
        )
        end_missing_grace, _ = _safe_float(
            polling_cfg.get("end_missing_grace_sec"),
            DEFAULT_END_MISSING_GRACE_SEC,
            minimum=0.0,
        )
        end_poll, _ = _safe_float(
            polling_cfg.get("end_poll_sec"),
            DEFAULT_END_POLL_SEC,
            minimum=0.1,
        )
        event_poll, _ = _safe_float(
            polling_cfg.get("event_poll_sec"),
            DEFAULT_EVENT_POLL_SEC,
            minimum=0.1,
        )
        max_size_gb, _ = _safe_float(
            storage_cfg.get("max_size_gb"),
            DEFAULT_MAX_STORAGE_GB,
            minimum=0.1,
        )

        recordings_dir = resolve_path(paths_cfg.get("recordings_dir", DEFAULT_RECORDINGS_DIR), DATA_DIR)
        json_dir = resolve_path(paths_cfg.get("json_dir", DEFAULT_JSON_DIR), DATA_DIR)
        if json_dir is None and recordings_dir is not None:
            json_dir = recordings_dir / "json"
        bin_dir = resolve_path(paths_cfg.get("bin_dir", DEFAULT_BIN_DIR), DATA_DIR)
        icons_dir = resolve_path(paths_cfg.get("champion_icons_dir", DEFAULT_CHAMPION_ICONS_DIR), DATA_DIR)

        if recordings_dir is None:
            recordings_dir = (DATA_DIR / DEFAULT_RECORDINGS_DIR).resolve()
        if json_dir is None:
            json_dir = (recordings_dir / "json").resolve()
        if bin_dir is None:
            bin_dir = (DATA_DIR / DEFAULT_BIN_DIR).resolve()
        if icons_dir is None:
            icons_dir = (DATA_DIR / DEFAULT_CHAMPION_ICONS_DIR).resolve()

        normalized_storage_cfg = dict(storage_cfg)
        normalized_storage_cfg["max_size_gb"] = max_size_gb

        return cls(
            obs=OBSSettings(
                host=str(obs_cfg.get("host") or DEFAULT_OBS_HOST),
                port=port,
                password=ensure_obs_password_value(obs_cfg.get("password"))[0],
                scene_name=str(obs_cfg.get("scene_name") or DEFAULT_OBS_SCENE_NAME),
                source_name=str(obs_cfg.get("source_name") or DEFAULT_OBS_SOURCE_NAME),
                source_color=source_color,
                window_capture_name=window_capture_name,
                window_capture_window=window_capture_window,
                window_capture_method=window_capture_method,
                fps=fps,
                obs_dir=MANAGED_PORTABLE_OBS_DIR,
            ),
            paths=PathsSettings(
                bin_dir=bin_dir,
                recordings_dir=recordings_dir,
                json_dir=json_dir,
                champion_icons_dir=icons_dir,
            ),
            polling=PollingSettings(
                end_error_limit=end_limit,
                end_missing_grace_sec=end_missing_grace,
                end_poll_sec=end_poll,
                event_poll_sec=event_poll,
            ),
            storage=StorageSettings(
                max_size_gb=max_size_gb,
                max_size_bytes=parse_max_storage_bytes(normalized_storage_cfg),
            ),
            audio=AudioSettings(
                desktop=_audio_slot_from_config(source, "desktop"),
                mic=_audio_slot_from_config(source, "mic"),
            ),
        )

    @classmethod
    def load(cls) -> AppConfig:
        return cls.from_dict(load_settings())

    def audio_to_dict(self) -> dict[str, Any]:
        return {"audio": self.audio.to_dict()}


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
    def get_audio_device_catalog(self, cfg: AppConfig | None = None, scene_name: str | None = None) -> dict[str, Any]:
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
    def shutdown(self) -> None:
        pass


class RiotAPIClient(ABC):
    """LoL Live Client APIの取得とパースだけを担当する抽象インターフェース。"""

    @abstractmethod
    async def get_active_player_name(self) -> str | None:
        pass

    @abstractmethod
    async def get_event_data(self) -> dict[str, Any] | None:
        pass

    @abstractmethod
    async def get_all_game_data(self) -> dict[str, Any] | None:
        pass

    @abstractmethod
    async def get_all_game_data_result(self) -> RiotPollResult:
        pass


class RecordingSessionManager(ABC):
    """OBSClientとRiotAPIClientを注入され、録画ワークフローを管理する抽象インターフェース。"""

    @abstractmethod
    def open(self) -> None:
        pass

    @abstractmethod
    def reset_session(self) -> None:
        pass

    @abstractmethod
    def request_stop(self) -> None:
        pass

    @abstractmethod
    def has_session_data(self) -> bool:
        pass

    @abstractmethod
    def apply_record_output_settings(self) -> bool:
        pass

    @abstractmethod
    def apply_audio_profile(self, cfg: AppConfig) -> bool:
        pass

    @abstractmethod
    def get_audio_device_catalog(self, cfg: AppConfig | None = None) -> dict[str, Any]:
        pass

    @abstractmethod
    async def wait_for_game_start_async(self) -> bool:
        pass

    @abstractmethod
    async def start_recording_async(self) -> None:
        pass

    @abstractmethod
    async def record_until_end_async(self) -> RecordingOutcome:
        pass

    @abstractmethod
    def stop_recording(self) -> None:
        pass

    @abstractmethod
    def save_json(self) -> None:
        pass

    @abstractmethod
    def finalize_session(
        self,
        outcome: RecordingOutcome | None = None,
        failure_reason: str | BaseException | None = None,
    ) -> FinalizeResult:
        pass

    @abstractmethod
    def shutdown_obs(self) -> None:
        pass

    @abstractmethod
    def disconnect_obs(self) -> None:
        pass


def obs_executable_path(base_dir: str | Path | None) -> Path | None:
    if not base_dir:
        return None
    return Path(base_dir) / "bin" / "64bit" / "obs64.exe"


def is_valid_obs_dir(base_dir: str | Path | None) -> bool:
    obs_exe = obs_executable_path(base_dir)
    return bool(obs_exe and obs_exe.exists())


def legacy_managed_obs_dirs() -> tuple[Path, ...]:
    seen = set()
    result = []
    for path in (LEGACY_ROOT_OBS_DIR, LEGACY_MANAGED_OBS_DIR, LEGACY_DATA_BIN_OBS_DIR):
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        key = str(resolved).casefold()
        if key == str(MANAGED_PORTABLE_OBS_DIR).casefold() or key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return tuple(result)


def detect_obs_dir() -> str | None:
    if is_valid_obs_dir(MANAGED_PORTABLE_OBS_DIR):
        return str(MANAGED_PORTABLE_OBS_DIR)
    for legacy_dir in legacy_managed_obs_dirs():
        if is_valid_obs_dir(legacy_dir):
            return str(legacy_dir)
    return None


def is_managed_portable_obs_dir(base_dir: str | Path | None) -> bool:
    if not base_dir:
        return False
    try:
        candidate = Path(base_dir).resolve()
        return candidate == MANAGED_PORTABLE_OBS_DIR
    except Exception:
        return False


def is_legacy_managed_obs_dir(base_dir: str | Path | None) -> bool:
    if not base_dir:
        return False
    try:
        candidate = Path(base_dir).resolve()
        return any(candidate == legacy_dir for legacy_dir in legacy_managed_obs_dirs())
    except Exception:
        return False


def bootstrap_obs_dir(base_dir: str | Path, port: int | None = None, password: str = "") -> dict[str, Any]:
    base_path = Path(base_dir).resolve()
    bootstrapper = SharedOBSBootstrapper(
        base_path,
        process_manager=OBSProcessManager(base_path, logger=LOGGER),
        logger=LOGGER,
    )
    if port is not None:
        password = ensure_obs_password_value(password)[0]
    return bootstrapper.apply(port=port, password=password)


def migrate_legacy_managed_obs_if_needed(port: int | None = None, password: str = "") -> Path | None:
    if is_valid_obs_dir(MANAGED_PORTABLE_OBS_DIR):
        return None

    legacy_dir = next((path for path in legacy_managed_obs_dirs() if is_valid_obs_dir(path)), None)
    if legacy_dir is None:
        return None

    OBSProcessManager(legacy_dir, logger=LOGGER).kill_stale_managed_processes()
    MANAGED_PORTABLE_OBS_DIR.mkdir(parents=True, exist_ok=True)
    copy_obs_tree_contents(legacy_dir, MANAGED_PORTABLE_OBS_DIR)
    bootstrap_obs_dir(MANAGED_PORTABLE_OBS_DIR, port=port, password=password)
    LOGGER.info(
        "旧OBS配置を obs-portable へコピー移行しました: %s -> %s",
        legacy_dir,
        MANAGED_PORTABLE_OBS_DIR,
    )
    return legacy_dir


def repair_legacy_managed_obs_if_present(port: int | None = None, password: str = "") -> Path | None:
    repaired = None
    for legacy_dir in legacy_managed_obs_dirs():
        if not is_valid_obs_dir(legacy_dir):
            continue
        bootstrap_obs_dir(legacy_dir, port=port, password=password)
        repaired = legacy_dir
    return repaired


def get_obs_websocket_config_path(base_dir: str | Path) -> Path:
    return shared_get_obs_websocket_config_path(base_dir)


def get_obs_config_dir(base_dir: str | Path) -> Path:
    return shared_get_obs_config_dir(base_dir)


def get_obs_global_ini_path(base_dir: str | Path) -> Path:
    return shared_get_obs_global_ini_path(base_dir)


def get_obs_user_ini_path(base_dir: str | Path) -> Path:
    return shared_get_obs_user_ini_path(base_dir)


def get_obs_portable_marker_path(base_dir: str | Path) -> Path:
    return shared_get_portable_marker_path(base_dir)


class OBSBootstrapper(SharedOBSBootstrapper):
    """アプリ管理のポータブルOBSを初期化するBootstrapper。"""

    def __init__(self, base_dir: str | Path) -> None:
        if not is_managed_portable_obs_dir(base_dir):
            raise RecorderError(
                f"このアプリは obs-portable に配置されたポータブルOBSのみ対応です。\n利用先: {MANAGED_PORTABLE_OBS_DIR}"
            )
        super().__init__(base_dir, process_manager=OBSProcessManager(base_dir, logger=LOGGER), logger=LOGGER)


def ensure_portable_obs_global_ini(base_dir: str | Path) -> tuple[bool, Path]:
    """
    ポータブルOBSの global.ini にトレイ無効化設定を反映する。
    configparser を使い、キーの大文字小文字を維持して書き込む。
    """
    return OBSBootstrapper(base_dir).ensure_global_ini()


def ensure_portable_obs_user_ini(base_dir: str | Path) -> tuple[bool, Path]:
    """
    ポータブルOBSの user.ini に初回起動・トレイ無効化設定を反映する。
    OBS 32.x は UI 起動設定を global.ini ではなく user.ini から読む。
    """
    return OBSBootstrapper(base_dir).ensure_user_ini()


def ensure_portable_obs_websocket_config(base_dir: str | Path, port: int, password: str) -> tuple[bool, Path]:
    """
    obs-portable に配置されたポータブルOBSのみを対象に、
    WebSocket設定を固定値へ自動補完する。
    """
    return OBSBootstrapper(base_dir).ensure_websocket_config(port, ensure_obs_password_value(password)[0])


def is_tcp_port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((str(host), int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_obs_websocket_port_free(config: AppConfig, timeout: float = 0.5) -> None:
    if not is_tcp_port_open(config.obs.host, config.obs.port, timeout=timeout):
        return
    raise RecorderError(
        "OBS WebSocketポートが既に使用されています。\n"
        f"接続先: {config.obs.host}:{config.obs.port}\n"
        "別のOBS、またはこのアプリ管理外のOBSが起動している可能性があります。\n"
        "既存のOBSを終了してから再実行してください。"
    )


def wait_for_owned_obs_connection(
    config: AppConfig,
    process_manager: OBSProcessManager | None = None,
    timeout_sec: float = 8.0,
    poll_interval: float = 0.5,
) -> bool:
    """このアプリが起動済みのOBSがWebSocket接続可能になるまで待つ。"""
    manager = process_manager or OBSProcessManager(config.obs.obs_dir, logger=LOGGER)
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while True:
        if not manager.has_owned_process():
            return False
        ok, _detail = test_obs_connection(
            config.obs.host,
            config.obs.port,
            config.obs.password,
            timeout=min(1.0, max(0.2, poll_interval)),
        )
        if ok:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.05, poll_interval))


def _ensure_section_dict(root: dict[str, Any], key: str) -> tuple[dict[str, Any], bool]:
    value = root.get(key)
    if isinstance(value, dict):
        return value, False
    root[key] = {}
    return root[key], True


def _safe_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> tuple[int, bool]:
    try:
        parsed = int(value)
    except Exception:
        return default, False
    if minimum is not None and parsed < minimum:
        return default, False
    if maximum is not None and parsed > maximum:
        return default, False
    return parsed, True


def _safe_float(value: Any, default: float, minimum: float | None = None) -> tuple[float, bool]:
    try:
        parsed = float(value)
    except Exception:
        return default, False
    if minimum is not None and parsed < minimum:
        return default, False
    return parsed, True


def _safe_bool(value: Any, default: bool) -> tuple[bool, bool]:
    if isinstance(value, bool):
        return value, True
    if isinstance(value, (int, float)):
        return bool(value), True
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True, True
        if text in {"0", "false", "no", "off"}:
            return False, True
    return default, False


def _ensure_audio_config_defaults(data: dict[str, Any], auto_fix: bool = True) -> dict[str, Any]:
    changed = False
    notes = []
    warnings = []
    errors = []

    audio_cfg, replaced = _ensure_section_dict(data, "audio")
    if replaced:
        changed = True

    for key, spec in MANAGED_AUDIO_INPUTS.items():
        slot_cfg, replaced = _ensure_section_dict(audio_cfg, key)
        if replaced:
            changed = True

        defaults = {
            "input_name": spec["input_name"],
            "device_id": DEFAULT_AUDIO_DEVICE_ID,
            "device_name": DEFAULT_AUDIO_DEVICE_NAME,
            "volume_db": spec["default_volume_db"],
            "mute": spec["default_mute"],
        }
        for field, default_value in defaults.items():
            if slot_cfg.get(field) in (None, ""):
                if auto_fix:
                    slot_cfg[field] = default_value
                    changed = True
                    notes.append(f"audio.{key}.{field} を既定値で補完しました。")
                else:
                    errors.append(f"audio.{key}.{field} が未設定です。")

        input_name = str(slot_cfg.get("input_name") or "").strip()
        if not input_name:
            if auto_fix:
                slot_cfg["input_name"] = spec["input_name"]
                changed = True
                warnings.append(f"audio.{key}.input_name が不正だったため既定値を使用します。")
            else:
                errors.append(f"audio.{key}.input_name が不正です。")

        device_id = str(slot_cfg.get("device_id") or "").strip()
        if not device_id:
            if auto_fix:
                slot_cfg["device_id"] = DEFAULT_AUDIO_DEVICE_ID
                changed = True
                warnings.append(f"audio.{key}.device_id が不正だったため default を使用します。")
            else:
                errors.append(f"audio.{key}.device_id が不正です。")

        device_name = str(slot_cfg.get("device_name") or "").strip()
        if not device_name and auto_fix:
            slot_cfg["device_name"] = DEFAULT_AUDIO_DEVICE_NAME
            changed = True

        volume_db, ok = _safe_float(slot_cfg.get("volume_db"), spec["default_volume_db"])
        if not ok:
            if auto_fix:
                slot_cfg["volume_db"] = volume_db
                changed = True
            warnings.append(f"audio.{key}.volume_db が不正だったため既定値を使用します。")

        mute_value, ok = _safe_bool(slot_cfg.get("mute"), spec["default_mute"])
        if not ok:
            if auto_fix:
                slot_cfg["mute"] = mute_value
                changed = True
            warnings.append(f"audio.{key}.mute が不正だったため既定値を使用します。")

    return {
        "changed": changed,
        "notes": notes,
        "warnings": warnings,
        "errors": errors,
    }


def parse_obs_source_color(value: Any, default: int = DEFAULT_OBS_SOURCE_COLOR) -> tuple[int, bool]:
    if value is None:
        return default, False
    if isinstance(value, int):
        return value & 0xFFFFFFFF, True

    text = str(value).strip()
    if not text:
        return default, False

    # #RRGGBB は ABGR に変換する
    if text.startswith("#") and len(text) == 7:
        try:
            rgb = int(text[1:], 16)
            red = (rgb >> 16) & 0xFF
            green = (rgb >> 8) & 0xFF
            blue = rgb & 0xFF
            color = (0xFF << 24) | (blue << 16) | (green << 8) | red
            return color, True
        except Exception:
            return default, False

    try:
        return int(text, 0) & 0xFFFFFFFF, True
    except Exception:
        return default, False


def obs_color_to_hex(color_value: Any) -> str:
    value, _ = parse_obs_source_color(color_value, default=DEFAULT_OBS_SOURCE_COLOR)
    red = value & 0xFF
    green = (value >> 8) & 0xFF
    blue = (value >> 16) & 0xFF
    return f"#{red:02X}{green:02X}{blue:02X}"


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def normalize_window_capture_source_name(value: Any, legacy_value: Any = None) -> str:
    name = _first_non_empty(value, legacy_value, DEFAULT_OBS_WINDOW_CAPTURE_NAME)
    if name == DEFAULT_OBS_GAME_CAPTURE_NAME:
        return DEFAULT_OBS_WINDOW_CAPTURE_NAME
    return name


def normalize_window_capture_window_selector(value: Any, legacy_value: Any = None) -> str:
    selector = _first_non_empty(value, legacy_value, DEFAULT_OBS_WINDOW_CAPTURE_WINDOW)
    if selector == DEFAULT_OBS_LEGACY_GAME_CAPTURE_WINDOW:
        return DEFAULT_OBS_WINDOW_CAPTURE_WINDOW
    return selector


def normalize_obs_capture_config(obs_cfg: dict[str, Any], auto_fix: bool = True) -> dict[str, Any]:
    report = {
        "changed": False,
        "notes": [],
        "warnings": [],
        "errors": [],
    }
    if not isinstance(obs_cfg, dict):
        report["errors"].append("obs 設定が不正です。")
        return report

    def empty(value: Any) -> bool:
        return value is None or str(value).strip() == ""

    def normalize_text(key: str, legacy_key: str, default: str) -> None:
        current = obs_cfg.get(key)
        legacy = obs_cfg.get(legacy_key)
        if empty(current):
            if not empty(legacy):
                if auto_fix:
                    obs_cfg[key] = str(legacy).strip()
                    report["changed"] = True
                    report["notes"].append(f"{legacy_key} を {key} へ移行しました。")
                else:
                    report["warnings"].append(f"{legacy_key} は旧設定キーです。{key} へ移行してください。")
            elif auto_fix:
                obs_cfg[key] = default
                report["changed"] = True
                report["notes"].append(f"{key} を既定値で補完しました。")
            else:
                report["errors"].append(f"{key} が未設定です。")
            return

        trimmed = str(current).strip()
        if current != trimmed and auto_fix:
            obs_cfg[key] = trimmed
            report["changed"] = True
            report["notes"].append(f"{key} の前後空白を削除しました。")

    normalize_text("window_capture_name", "game_capture_name", DEFAULT_OBS_WINDOW_CAPTURE_NAME)
    normalize_text("window_capture_window", "game_capture_window", DEFAULT_OBS_WINDOW_CAPTURE_WINDOW)

    normalized_name = normalize_window_capture_source_name(obs_cfg.get("window_capture_name"))
    if not empty(obs_cfg.get("window_capture_name")) and obs_cfg.get("window_capture_name") != normalized_name:
        if auto_fix:
            obs_cfg["window_capture_name"] = normalized_name
            report["changed"] = True
            report["notes"].append(f"window_capture_name を {normalized_name} に更新しました。")
        else:
            report["warnings"].append(f"window_capture_name は {normalized_name} への更新が必要です。")

    normalized_window = normalize_window_capture_window_selector(obs_cfg.get("window_capture_window"))
    if not empty(obs_cfg.get("window_capture_window")) and obs_cfg.get("window_capture_window") != normalized_window:
        if auto_fix:
            obs_cfg["window_capture_window"] = normalized_window
            report["changed"] = True
            report["notes"].append("window_capture_window をLoLのRiotWindowClass指定に更新しました。")
        else:
            report["warnings"].append("window_capture_window はLoLのRiotWindowClass指定への更新が必要です。")

    raw_method = obs_cfg.get("window_capture_method")
    method, ok = _safe_int(
        raw_method,
        DEFAULT_OBS_WINDOW_CAPTURE_METHOD,
        minimum=0,
        maximum=2,
    )
    if empty(raw_method):
        if auto_fix:
            obs_cfg["window_capture_method"] = DEFAULT_OBS_WINDOW_CAPTURE_METHOD
            report["changed"] = True
            report["notes"].append("window_capture_method を Windows Graphics Capture で補完しました。")
        else:
            report["errors"].append("window_capture_method が未設定です。")
    elif not ok:
        if auto_fix:
            obs_cfg["window_capture_method"] = method
            report["changed"] = True
        report["warnings"].append(
            f"window_capture_method が不正だったため {DEFAULT_OBS_WINDOW_CAPTURE_METHOD} を使用します。"
        )
    elif auto_fix and raw_method != method:
        obs_cfg["window_capture_method"] = method
        report["changed"] = True

    legacy_removed = False
    if auto_fix:
        for key in ("game_capture_name", "game_capture_window"):
            if key in obs_cfg:
                obs_cfg.pop(key, None)
                legacy_removed = True
        if legacy_removed:
            report["changed"] = True
            report["notes"].append("旧Game Capture設定キーを削除しました。")
    elif any(key in obs_cfg for key in ("game_capture_name", "game_capture_window")):
        report["warnings"].append("旧Game Capture設定キーが残っています。")

    return report


def _has_mpv_dll(bin_path: str | Path | None) -> bool:
    return has_mpv_dll(bin_path, ROOT_DIR)


def run_preflight_checks(cfg: dict[str, Any], auto_fix: bool = True, ensure_dirs: bool = True) -> dict[str, Any]:
    report = {
        "config": cfg if isinstance(cfg, dict) else {},
        "changed": False,
        "notes": [],
        "warnings": [],
        "errors": [],
    }
    data = report["config"]
    if data is not cfg:
        report["changed"] = True
        report["notes"].append("設定形式が不正だったため初期化しました。")

    obs_cfg, replaced = _ensure_section_dict(data, "obs")
    if replaced:
        report["changed"] = True
    paths_cfg, replaced = _ensure_section_dict(data, "paths")
    if replaced:
        report["changed"] = True
    poll_cfg, replaced = _ensure_section_dict(data, "polling")
    if replaced:
        report["changed"] = True
    storage_cfg, replaced = _ensure_section_dict(data, "storage")
    if replaced:
        report["changed"] = True
    audio_fix = _ensure_audio_config_defaults(data, auto_fix=auto_fix)
    if audio_fix["changed"]:
        report["changed"] = True
    report["notes"].extend(audio_fix["notes"])
    report["warnings"].extend(audio_fix["warnings"])
    report["errors"].extend(audio_fix["errors"])

    obs_defaults = {
        "host": DEFAULT_OBS_HOST,
        "port": DEFAULT_OBS_PORT,
        "fps": DEFAULT_OBS_FPS,
        "scene_name": DEFAULT_OBS_SCENE_NAME,
        "source_name": DEFAULT_OBS_SOURCE_NAME,
        "source_color": DEFAULT_OBS_SOURCE_COLOR,
        "dir": DEFAULT_OBS_DIR,
    }
    path_defaults = {
        "bin_dir": DEFAULT_BIN_DIR,
        "recordings_dir": DEFAULT_RECORDINGS_DIR,
        "json_dir": DEFAULT_JSON_DIR,
        "champion_icons_dir": DEFAULT_CHAMPION_ICONS_DIR,
        "champion_aliases_path": "config/champion_aliases.json",
    }
    poll_defaults = {
        "end_error_limit": DEFAULT_END_ERROR_LIMIT,
        "end_missing_grace_sec": DEFAULT_END_MISSING_GRACE_SEC,
        "end_poll_sec": DEFAULT_END_POLL_SEC,
        "event_poll_sec": DEFAULT_EVENT_POLL_SEC,
    }
    storage_defaults = {"max_size_gb": DEFAULT_MAX_STORAGE_GB}

    def apply_defaults(target: dict[str, Any], defaults: dict[str, Any]) -> None:
        for key, value in defaults.items():
            if target.get(key) in (None, ""):
                if auto_fix:
                    target[key] = value
                    report["changed"] = True
                    report["notes"].append(f"{key} を既定値で補完しました。")
                else:
                    report["errors"].append(f"{key} が未設定です。")

    apply_defaults(obs_cfg, obs_defaults)
    apply_defaults(paths_cfg, path_defaults)
    apply_defaults(poll_cfg, poll_defaults)
    apply_defaults(storage_cfg, storage_defaults)

    capture_fix = normalize_obs_capture_config(obs_cfg, auto_fix=auto_fix)
    if capture_fix["changed"]:
        report["changed"] = True
    report["notes"].extend(capture_fix["notes"])
    report["warnings"].extend(capture_fix["warnings"])
    report["errors"].extend(capture_fix["errors"])

    password_value, generated_password = ensure_obs_password_value(obs_cfg.get("password"))
    if generated_password:
        if auto_fix:
            obs_cfg["password"] = password_value
            report["changed"] = True
            report["notes"].append("OBS WebSocketパスワードを自動生成しました。")
        else:
            report["errors"].append("OBS WebSocketパスワードが未設定です。")
    elif obs_cfg.get("password") != password_value:
        if auto_fix:
            obs_cfg["password"] = password_value
            report["changed"] = True
            report["notes"].append("OBS WebSocketパスワードの前後空白を削除しました。")
        else:
            report["warnings"].append("OBS WebSocketパスワードに前後空白があります。")

    port, ok = _safe_int(obs_cfg.get("port"), DEFAULT_OBS_PORT, minimum=1, maximum=65535)
    if not ok:
        if auto_fix:
            obs_cfg["port"] = port
            report["changed"] = True
        report["warnings"].append(f"OBSポートが不正だったため {port} を使用します。")

    fps_value, ok = _safe_int(obs_cfg.get("fps"), DEFAULT_OBS_FPS, minimum=1, maximum=240)
    if not ok:
        if auto_fix:
            obs_cfg["fps"] = fps_value
            report["changed"] = True
        report["warnings"].append(f"OBS FPS が不正だったため {fps_value} を使用します。")

    raw_source_color = obs_cfg.get("source_color")
    source_color, ok = parse_obs_source_color(raw_source_color, default=DEFAULT_OBS_SOURCE_COLOR)
    if not ok:
        if auto_fix:
            obs_cfg["source_color"] = source_color
            report["changed"] = True
        report["warnings"].append("source_color が不正だったため赤 (#FF0000) を使用します。")
    elif source_color == LEGACY_OBS_SOURCE_COLOR_BLUE:
        raw_text = str(raw_source_color).strip().lower() if raw_source_color is not None else ""
        legacy_values = {"", "4294901760", "0xffff0000", "#0000ff"}
        if isinstance(raw_source_color, int):
            is_legacy = raw_source_color == LEGACY_OBS_SOURCE_COLOR_BLUE
        else:
            is_legacy = raw_text in legacy_values
        if is_legacy and auto_fix:
            obs_cfg["source_color"] = DEFAULT_OBS_SOURCE_COLOR
            report["changed"] = True
            report["notes"].append("旧設定の青色ソースを赤色 (#FF0000) に更新しました。")

    end_error_limit, ok = _safe_int(poll_cfg.get("end_error_limit"), DEFAULT_END_ERROR_LIMIT, minimum=1)
    if not ok:
        if auto_fix:
            poll_cfg["end_error_limit"] = end_error_limit
            report["changed"] = True
        report["warnings"].append("end_error_limit が不正だったため既定値を使用します。")

    end_missing_grace_sec, ok = _safe_float(
        poll_cfg.get("end_missing_grace_sec"),
        DEFAULT_END_MISSING_GRACE_SEC,
        minimum=0.0,
    )
    if not ok:
        if auto_fix:
            poll_cfg["end_missing_grace_sec"] = end_missing_grace_sec
            report["changed"] = True
        report["warnings"].append("end_missing_grace_sec が不正だったため既定値を使用します。")

    end_poll_sec, ok = _safe_float(poll_cfg.get("end_poll_sec"), DEFAULT_END_POLL_SEC, minimum=0.1)
    if not ok:
        if auto_fix:
            poll_cfg["end_poll_sec"] = end_poll_sec
            report["changed"] = True
        report["warnings"].append("end_poll_sec が不正だったため既定値を使用します。")

    event_poll_sec, ok = _safe_float(poll_cfg.get("event_poll_sec"), DEFAULT_EVENT_POLL_SEC, minimum=0.1)
    if not ok:
        if auto_fix:
            poll_cfg["event_poll_sec"] = event_poll_sec
            report["changed"] = True
        report["warnings"].append("event_poll_sec が不正だったため既定値を使用します。")

    max_size_gb, ok = _safe_float(storage_cfg.get("max_size_gb"), DEFAULT_MAX_STORAGE_GB, minimum=0.1)
    if not ok:
        if auto_fix:
            storage_cfg["max_size_gb"] = max_size_gb
            report["changed"] = True
        report["warnings"].append("max_size_gb が不正だったため既定値を使用します。")

    recordings_dir = resolve_path(paths_cfg.get("recordings_dir", DEFAULT_RECORDINGS_DIR), DATA_DIR)
    json_dir = resolve_path(paths_cfg.get("json_dir", DEFAULT_JSON_DIR), DATA_DIR)
    bin_dir = resolve_path(paths_cfg.get("bin_dir", DEFAULT_BIN_DIR), DATA_DIR)
    icons_dir = resolve_path(paths_cfg.get("champion_icons_dir", DEFAULT_CHAMPION_ICONS_DIR), DATA_DIR)

    if recordings_dir is None:
        report["errors"].append("recordings_dir の設定が無効です。")
    if json_dir is None and recordings_dir is not None:
        json_dir = recordings_dir / "json"
        if auto_fix:
            paths_cfg["json_dir"] = str(json_dir)
            report["changed"] = True
            report["notes"].append("json_dir が未設定のため recordings/json を設定しました。")

    if ensure_dirs:
        for path_value, label in (
            (recordings_dir, "録画ディレクトリ"),
            (json_dir, "JSONディレクトリ"),
            (bin_dir, "binディレクトリ"),
            (icons_dir, "チャンピオンアイコンディレクトリ"),
        ):
            if path_value is None:
                continue
            try:
                path_value.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                report["errors"].append(f"{label} を作成できません: {path_value} ({e})")

    if bin_dir and not _has_mpv_dll(bin_dir):
        report["warnings"].append("binフォルダに mpv DLL が見つかりません。プレーヤー利用時に配置が必要です。")

    obs_password = str(obs_cfg.get("password") or "")
    if auto_fix:
        try:
            migrated_from = migrate_legacy_managed_obs_if_needed(port=port, password=obs_password)
            if migrated_from is not None:
                report["changed"] = True
                report["notes"].append(
                    f"旧OBS配置を obs-portable へコピー移行しました: {migrated_from}"
                )
        except Exception as e:
            report["warnings"].append(f"旧OBS配置の移行に失敗しました: {e}")

        try:
            repaired_legacy = repair_legacy_managed_obs_if_present(port=port, password=obs_password)
            if repaired_legacy is not None:
                report["changed"] = True
                report["notes"].append(f"旧OBS配置の起動前設定も修復しました: {repaired_legacy}")
        except Exception as e:
            report["warnings"].append(f"旧OBS配置の設定修復に失敗しました: {e}")

    current_obs_dir = resolve_path(obs_cfg.get("dir", DEFAULT_OBS_DIR), DATA_DIR)
    expected_obs_dir = MANAGED_PORTABLE_OBS_DIR

    if not current_obs_dir or not is_managed_portable_obs_dir(current_obs_dir):
        if auto_fix:
            obs_cfg["dir"] = DEFAULT_OBS_DIR
            report["changed"] = True
            report["notes"].append(f"OBSフォルダをアプリ管理用に固定しました: {DEFAULT_OBS_DIR}")
            current_obs_dir = expected_obs_dir
        else:
            report["errors"].append(f"OBSフォルダは obs-portable のポータブルOBSのみ対応です: {expected_obs_dir}")

    if current_obs_dir and is_managed_portable_obs_dir(current_obs_dir):
        try:
            bootstrapper = OBSBootstrapper(current_obs_dir)
            bootstrap_report = bootstrapper.check()
            if bootstrap_report.needs_repair:
                if auto_fix:
                    if bootstrapper.process_manager.has_managed_process():
                        report["warnings"].append(
                            "ポータブルOBSが起動中のため、起動前設定の自動修復を延期しました。"
                            "録画監視を停止してから環境修復を再実行してください。"
                        )
                    else:
                        bootstrap_result = bootstrapper.apply(
                            port=int(obs_cfg.get("port") or DEFAULT_OBS_PORT),
                            password=str(obs_cfg.get("password") or ""),
                        )
                        report["changed"] = True
                        report["notes"].append(
                            f"ポータブルOBS設定を修復しました: {bootstrap_result.get('global_ini_path')}"
                        )
                else:
                    report["warnings"].append("OBS Bootstrapper の修復が必要です。")
        except Exception as e:
            report["warnings"].append(f"OBS Bootstrapper の検査/修復に失敗しました: {e}")

    has_valid_obs = bool(current_obs_dir and is_valid_obs_dir(current_obs_dir))
    if not has_valid_obs:
        report["errors"].append(
            f"ポータブルOBSが見つかりません。\n配置先: {expected_obs_dir}\nobs64.exe が存在する状態で配置してください。"
        )

    return report


def format_preflight_report(report: dict[str, Any]) -> str:
    lines = []
    for note in report.get("notes", []):
        lines.append(f"- {note}")
    for warning in report.get("warnings", []):
        lines.append(f"⚠️ {warning}")
    for error in report.get("errors", []):
        lines.append(f"❌ {error}")
    return "\n".join(lines)


def test_obs_connection(
    host: str | None, port: int | str | None, password: str | None, timeout: float = 2.5
) -> tuple[bool, str]:
    host_text = str(host or "").strip() or DEFAULT_OBS_HOST
    try:
        port_num = int(port)
    except Exception:
        return False, f"OBSポートが不正です: {port}"

    # localhost と 127.0.0.1 の差分で失敗する環境を吸収する
    host_candidates = [host_text]
    if host_text == "localhost":
        host_candidates.append("127.0.0.1")
    elif host_text == "127.0.0.1":
        host_candidates.append("localhost")

    last_error = None
    for candidate in host_candidates:
        client = None
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
        except Exception as e:
            last_error = e
            message = f"{type(e).__name__}: {e}".lower()
            if any(token in message for token in ("auth", "authentication", "password", "identify")):
                return False, "OBSには到達しましたが認証に失敗しました。WebSocketパスワードを確認してください。"
        finally:
            if client:
                try:
                    client.disconnect()
                except Exception:
                    pass

    if last_error:
        return (
            False,
            "OBS WebSocket に接続できません。OBS設定で WebSocket有効化 / ポート番号 を確認してください。\n"
            f"詳細: {type(last_error).__name__}: {last_error}",
        )
    return False, "OBS接続テストに失敗しました。"


def connect_obs_client(
    host: str | None, port: int | str | None, password: str | None, timeout: float = 2.5
) -> tuple[Any, str]:
    host_text = str(host or "").strip() or DEFAULT_OBS_HOST
    port_num, ok = _safe_int(port, DEFAULT_OBS_PORT, minimum=1, maximum=65535)
    if not ok:
        raise RecorderError(f"OBSポートが不正です: {port}")

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
        except Exception as e:
            last_error = e
            message = f"{type(e).__name__}: {e}".lower()
            if any(token in message for token in ("auth", "authentication", "password", "identify")):
                raise RecorderError(
                    "OBSには到達しましたが認証に失敗しました。WebSocketパスワードを確認してください。"
                ) from e

    raise RecorderError(
        f"OBS WebSocket に接続できません。\n接続先: {host_text}:{port_num}\n詳細: {last_error}"
    ) from last_error


def get_audio_config_defaults() -> dict[str, dict[str, Any]]:
    return {
        "desktop": {
            "input_name": DEFAULT_AUDIO_DESKTOP_INPUT_NAME,
            "device_id": DEFAULT_AUDIO_DEVICE_ID,
            "device_name": DEFAULT_AUDIO_DEVICE_NAME,
            "volume_db": DEFAULT_AUDIO_DESKTOP_VOLUME_DB,
            "mute": DEFAULT_AUDIO_DESKTOP_MUTE,
        },
        "mic": {
            "input_name": DEFAULT_AUDIO_MIC_INPUT_NAME,
            "device_id": DEFAULT_AUDIO_DEVICE_ID,
            "device_name": DEFAULT_AUDIO_DEVICE_NAME,
            "volume_db": DEFAULT_AUDIO_MIC_VOLUME_DB,
            "mute": DEFAULT_AUDIO_MIC_MUTE,
        },
    }


def normalize_audio_config(cfg: dict[str, Any], auto_fix: bool = True) -> dict[str, Any]:
    if isinstance(cfg, AppConfig):
        return cfg.audio.to_dict(), {
            "changed": False,
            "notes": [],
            "warnings": [],
            "errors": [],
        }
    container = cfg if isinstance(cfg, dict) else {}
    fix = _ensure_audio_config_defaults(container, auto_fix=auto_fix)
    audio_cfg = container.get("audio", {}) if isinstance(container, dict) else {}
    return audio_cfg, fix


def _get_audio_slot_config(cfg: dict[str, Any], key: str) -> dict[str, Any]:
    if isinstance(cfg, AppConfig):
        slot = cfg.audio.desktop if key == "desktop" else cfg.audio.mic
        return slot.to_dict()

    defaults = get_audio_config_defaults()
    audio_cfg, _ = normalize_audio_config(cfg, auto_fix=True)
    slot = audio_cfg.get(key, {}) if isinstance(audio_cfg, dict) else {}
    merged = dict(defaults[key])
    if isinstance(slot, dict):
        merged.update(slot)

    merged["input_name"] = (
        str(merged.get("input_name") or defaults[key]["input_name"]).strip() or defaults[key]["input_name"]
    )
    merged["device_id"] = (
        str(merged.get("device_id") or defaults[key]["device_id"]).strip() or defaults[key]["device_id"]
    )
    merged["device_name"] = (
        str(merged.get("device_name") or defaults[key]["device_name"]).strip() or defaults[key]["device_name"]
    )
    merged["volume_db"], _ = _safe_float(merged.get("volume_db"), defaults[key]["volume_db"])
    merged["mute"], _ = _safe_bool(merged.get("mute"), defaults[key]["mute"])
    return merged


def _audio_slot_from_config(cfg: dict[str, Any], key: str) -> AudioSlotSettings:
    slot = _get_audio_slot_config(cfg if isinstance(cfg, dict) else {}, key)
    return AudioSlotSettings(
        input_name=slot["input_name"],
        device_id=slot["device_id"],
        device_name=slot["device_name"],
        volume_db=slot["volume_db"],
        mute=slot["mute"],
    )


def _obs_raw(client: Any, request_type: str, payload: dict[str, Any] | None = None) -> Any:
    try:
        return client.send(request_type, payload or {}, raw=True)
    except TypeError:
        return client.send(request_type, payload or {})


def apply_obs_video_settings(client: Any, fps_value: int | str | None = None) -> Any:
    fps_num, _ = _safe_int(fps_value, DEFAULT_OBS_FPS, minimum=1, maximum=240)
    return _obs_raw(
        client,
        "SetVideoSettings",
        {
            "fpsNumerator": int(fps_num),
            "fpsDenominator": 1,
        },
    )


def apply_record_directory_to_obs(client: Any, record_dir: str | Path) -> bool:
    """
    OBSの録画保存先をWebSocket経由で反映する。
    wrapper -> raw request の順で試し、環境差分を吸収する。
    """
    if not record_dir:
        return False

    record_path = str(Path(record_dir))
    errors = []

    try:
        client.set_record_directory(record_path)
        return True
    except Exception as e:
        errors.append(f"wrapper: {type(e).__name__}: {e}")

    try:
        _obs_raw(client, "SetRecordDirectory", {"recordDirectory": record_path})
        return True
    except Exception as e:
        errors.append(f"raw: {type(e).__name__}: {e}")

    raise RecorderError(f"録画保存ディレクトリをOBSに反映できませんでした。\n対象: {record_path}\n" + "\n".join(errors))


def ensure_obs_scene_exists(client: Any, scene_name: str, status_cb: Callable[[str], None] | None = None) -> bool:
    try:
        scene_resp = client.get_scene_list()
        scenes = getattr(scene_resp, "scenes", []) or []
        scene_names = {item.get("sceneName") for item in scenes if isinstance(item, dict)}
    except Exception as e:
        raise RecorderError(f"シーン一覧の取得に失敗しました: {e}") from e

    if scene_name in scene_names:
        return False

    if status_cb:
        try:
            status_cb(f"ℹ️ シーン '{scene_name}' を自動作成します。")
        except Exception:
            pass
    client.create_scene(scene_name)
    return True


def _ensure_single_audio_input(client: Any, scene_name: str, key: str, slot_cfg: dict[str, Any]) -> bool:
    spec = MANAGED_AUDIO_INPUTS[key]
    input_name = str(slot_cfg.get("input_name") or spec["input_name"]).strip() or spec["input_name"]
    input_kind = spec["input_kind"]
    created = False

    input_exists = False
    input_kind_matches = False
    try:
        input_resp = client.get_input_list()
        input_items = getattr(input_resp, "inputs", []) or []
        for item in input_items:
            if not isinstance(item, dict):
                continue
            if item.get("inputName") != input_name:
                continue
            input_exists = True
            input_kind_matches = item.get("inputKind") == input_kind
            break
    except Exception:
        input_exists = False

    if input_exists and not input_kind_matches:
        try:
            client.remove_input(input_name)
            input_exists = False
        except Exception:
            # 種別違いでも削除できない場合は後続の設定更新で失敗させる。
            pass

    if not input_exists:
        settings = {"device_id": str(slot_cfg.get("device_id") or DEFAULT_AUDIO_DEVICE_ID)}
        last_error = None
        for kind_name in (input_kind,):
            try:
                client.create_input(scene_name, input_name, kind_name, settings, True)
                created = True
                input_exists = True
                break
            except Exception as e:
                last_error = e
        if not input_exists:
            raise RecorderError(f"{spec['label']}ソース '{input_name}' の作成に失敗しました: {last_error}")

    # 保存されている device_id を先に適用（default でも可）
    try:
        client.set_input_settings(
            input_name,
            {"device_id": str(slot_cfg.get("device_id") or DEFAULT_AUDIO_DEVICE_ID)},
            overlay=True,
        )
    except Exception:
        pass

    return created


def ensure_managed_audio_inputs(
    client: Any,
    scene_name: str,
    cfg: dict[str, Any] | AppConfig | None = None,
    status_cb: Callable[[str], None] | None = None,
) -> bool:
    ensure_obs_scene_exists(client, scene_name, status_cb=status_cb)
    created_any = False
    for key in ("desktop", "mic"):
        slot_cfg = _get_audio_slot_config(cfg or {}, key)
        created = _ensure_single_audio_input(client, scene_name, key, slot_cfg)
        created_any = created_any or created
        if created and status_cb:
            try:
                status_cb(f"ℹ️ {MANAGED_AUDIO_INPUTS[key]['label']}ソース '{slot_cfg['input_name']}' を作成しました。")
            except Exception:
                pass
    return created_any


def list_audio_devices_for_input(client: Any, input_name: str) -> list[dict[str, str]]:
    try:
        resp = _obs_raw(
            client,
            "GetInputPropertiesListPropertyItems",
            {"inputName": input_name, "propertyName": "device_id"},
        )
    except Exception as e:
        raise RecorderError(f"音声デバイス一覧の取得に失敗しました ({input_name}): {e}") from e

    items = []
    if isinstance(resp, dict):
        items = resp.get("propertyItems") or []

    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        device_id = str(item.get("itemValue") or "").strip()
        device_name = str(item.get("itemName") or device_id or "").strip()
        if not device_id:
            continue
        result.append({"id": device_id, "name": device_name})

    if not result:
        result.append({"id": DEFAULT_AUDIO_DEVICE_ID, "name": DEFAULT_AUDIO_DEVICE_NAME})
    return result


def get_audio_device_catalog(
    client: Any,
    cfg: dict[str, Any] | AppConfig | None = None,
    scene_name: str | None = None,
    status_cb: Callable[[str], None] | None = None,
) -> dict[str, list[dict[str, str]]]:
    scene_name = scene_name or (cfg.obs.scene_name if isinstance(cfg, AppConfig) else DEFAULT_OBS_SCENE_NAME)
    ensure_managed_audio_inputs(client, scene_name, cfg=cfg, status_cb=status_cb)
    desktop_cfg = _get_audio_slot_config(cfg or {}, "desktop")
    mic_cfg = _get_audio_slot_config(cfg or {}, "mic")
    return {
        "desktop": list_audio_devices_for_input(client, desktop_cfg["input_name"]),
        "mic": list_audio_devices_for_input(client, mic_cfg["input_name"]),
    }


def apply_audio_input_settings(
    client: Any,
    input_name: str,
    device_id: str | None = None,
    volume_db: float | int | str | None = None,
    mute: bool | None = None,
) -> None:
    if device_id not in (None, ""):
        client.set_input_settings(input_name, {"device_id": str(device_id)}, overlay=True)
    if volume_db is not None:
        client.set_input_volume(input_name, vol_db=float(volume_db))
    if mute is not None:
        client.set_input_mute(input_name, bool(mute))


def apply_audio_profile_from_config(
    client: Any,
    cfg: AppConfig,
    scene_name: str | None = None,
    status_cb: Callable[[str], None] | None = None,
) -> bool:
    scene_name = scene_name or (cfg.obs.scene_name if isinstance(cfg, AppConfig) else DEFAULT_OBS_SCENE_NAME)
    ensure_managed_audio_inputs(client, scene_name, cfg=cfg, status_cb=status_cb)

    for key in ("desktop", "mic"):
        slot_cfg = _get_audio_slot_config(cfg or {}, key)
        input_name = slot_cfg["input_name"]
        try:
            apply_audio_input_settings(
                client,
                input_name,
                device_id=slot_cfg.get("device_id"),
                volume_db=slot_cfg.get("volume_db"),
                mute=slot_cfg.get("mute"),
            )
        except Exception as e:
            raise RecorderError(f"{MANAGED_AUDIO_INPUTS[key]['label']}設定の適用に失敗しました: {e}") from e

    return True


def setup_obs_sync_elements(
    cfg: dict[str, Any], status_cb: Callable[[str], None] | None = None, auto_launch: bool = True
) -> dict[str, Any]:
    with OBS_OPERATION_LOCK:
        return _setup_obs_sync_elements_locked(cfg, status_cb=status_cb, auto_launch=auto_launch)


def _setup_obs_sync_elements_locked(
    cfg: dict[str, Any], status_cb: Callable[[str], None] | None = None, auto_launch: bool = True
) -> dict[str, Any]:
    config = AppConfig.from_dict(cfg)
    ensure_recording_dirs(config)

    launched_process = None
    recorder = None
    try:
        process_manager = OBSProcessManager(config.obs.obs_dir, logger=LOGGER)
        ok, _ = test_obs_connection(
            config.obs.host,
            config.obs.port,
            config.obs.password,
            timeout=1.5,
        )
        if ok:
            if not process_manager.has_owned_process():
                raise RecorderError(
                    "OBS WebSocketには接続できますが、このアプリが起動した管理対象OBSではありません。\n"
                    f"接続先: {config.obs.host}:{config.obs.port}\n"
                    "既存のOBSを終了してから再実行してください。"
                )
        elif wait_for_owned_obs_connection(config, process_manager=process_manager):
            pass
        elif auto_launch:
            launched_process = launch_obs(config)

        recorder = LoLAutoRecorder(
            config=config,
            obs_process=launched_process,
            status_cb=status_cb,
            auto_setup=True,
        )
        recorder.open()
        # 録画保存先も毎回明示して、環境差分でOBS設定がぶれないようにする。
        try:
            recorder.apply_record_output_settings()
        except Exception:
            pass
        try:
            recorder.apply_audio_profile(cfg)
        except Exception as e:
            if status_cb:
                try:
                    status_cb(f"⚠️ 音声設定の初期適用に失敗しました: {e}")
                except Exception:
                    pass
        return {
            "scene_name": config.obs.scene_name,
            "source_name": config.obs.source_name,
            "source_color": config.obs.source_color,
            "window_capture_name": config.obs.window_capture_name,
            "window_capture_window": config.obs.window_capture_window,
            "window_capture_method": config.obs.window_capture_method,
            "obs_launched": bool(launched_process),
        }
    finally:
        if launched_process:
            if recorder:
                try:
                    recorder.shutdown_obs()
                except Exception:
                    pass
            else:
                try:
                    launched_process.terminate()
                except Exception:
                    pass
        elif recorder:
            try:
                recorder.disconnect_obs()
            except Exception:
                pass


# ▼ 全員分保存する重要なイベント（オブジェクト）
GLOBAL_OBJECTIVES = [
    "DragonKill",  # ドラゴン
    "BaronKill",  # バロン
    "HeraldKill",  # ヘラルド
    "HordeKill",  # ヴォイドグラブ（内部名称）
    "BuildingKill",  # タワー / インヒビターなどの建造物破壊
]

# ▼ 自分が関与しているかチェックするイベント
COMBAT_EVENTS = [
    "ChampionKill"  # キル / デス（自分が関与したものだけ）
]

# SSL警告の無視
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def resolve_path(value: str | Path | None, base_dir: str | Path) -> Path | None:
    if value is None:
        return None
    value = os.path.expandvars(str(value))
    path = Path(value)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def load_settings() -> dict[str, Any]:
    return CONFIG_REPOSITORY.load(create_if_missing=True)


def load_app_config() -> AppConfig:
    return AppConfig.from_dict(load_settings())


def save_settings(cfg: dict[str, Any]) -> None:
    CONFIG_REPOSITORY.save(cfg)


def prepend_env_path(path: str | Path) -> None:
    path_text = str(path)
    if not path_text:
        return
    path_parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    if path_text not in path_parts:
        os.environ["PATH"] = path_text + os.pathsep + os.environ.get("PATH", "")


def configure_mpv_runtime_path(config: AppConfig) -> None:
    """MPVのDLLを読み込めるようにPATHを整える。"""

    bin_dir = config.paths.bin_dir
    if bin_dir:
        prepend_env_path(bin_dir)
        if not has_mpv_dll(bin_dir, ROOT_DIR):
            LOGGER.warning(
                "⚠️ 警告: 'bin' フォルダ内に mpv-1.dll / mpv-2.dll (または libmpv-1.dll / libmpv-2.dll) が見つかりません。"
            )
            LOGGER.warning("探した場所: %s", bin_dir)
            LOGGER.warning("旧配置も確認しました: %s", ROOT_DIR / "bin")
    else:
        LOGGER.warning("⚠️ 警告: bin_dir が未設定です。")


def ensure_recording_dirs(config: AppConfig) -> None:
    config.paths.json_dir.mkdir(parents=True, exist_ok=True)
    config.paths.recordings_dir.mkdir(parents=True, exist_ok=True)


def setup_environment(config: AppConfig) -> None:
    configure_mpv_runtime_path(config)
    ensure_recording_dirs(config)


def parse_max_storage_bytes(storage_cfg: dict[str, Any]) -> int | None:
    max_bytes = storage_cfg.get("max_size_bytes")
    if isinstance(max_bytes, (int, float)) and max_bytes > 0:
        return int(max_bytes)
    max_gb = storage_cfg.get("max_size_gb", DEFAULT_MAX_STORAGE_GB)
    if isinstance(max_gb, (int, float)) and max_gb > 0:
        return int(float(max_gb) * 1024 * 1024 * 1024)
    max_mb = storage_cfg.get("max_size_mb")
    if isinstance(max_mb, (int, float)) and max_mb > 0:
        return int(float(max_mb) * 1024 * 1024)
    return None


def is_within(child: str | Path, parent: str | Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def get_dir_size(path: str | Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except Exception:
                    continue
    except Exception:
        return 0
    return total


def total_storage_size(config: AppConfig | None = None) -> int:
    config = config or load_app_config()
    roots = []
    roots.append(Path(config.paths.recordings_dir))
    json_path = Path(config.paths.json_dir)
    if not roots or not is_within(json_path, roots[0]):
        roots.append(json_path)
    return sum(get_dir_size(root) for root in roots if root.exists())


def parse_saved_at(value: Any) -> float | None:
    if not value:
        return None
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def load_json_metadata(path: str | Path, config: AppConfig | None = None) -> tuple[float | None, Path | None]:
    path = Path(path)
    config = config or load_app_config()
    try:
        data = load_session_payload(path)
        saved_at = parse_saved_at(data.get("saved_at"))
        video_path = data.get("obs_record_path")
        if not video_path:
            return saved_at, None

        raw_path = Path(str(video_path))
        candidates = []
        if raw_path.is_absolute():
            candidates.append(raw_path)
        else:
            candidates.append(path.parent / raw_path)
            candidates.append(Path(config.paths.recordings_dir) / raw_path.name)

        for candidate in candidates:
            if candidate.exists():
                return saved_at, candidate
        if candidates:
            return saved_at, candidates[-1]
        return saved_at, raw_path
    except Exception:
        return None, None


def is_app_owned_video_path(path: str | Path | None, config: AppConfig) -> bool:
    if not path:
        return False
    try:
        video_path = Path(path).resolve()
        recordings_dir = Path(config.paths.recordings_dir).resolve()
    except Exception:
        return False
    if not is_within(video_path, recordings_dir):
        return False
    return video_path.suffix.lower() in {".mp4", ".mkv", ".flv", ".mov", ".avi"}


def enforce_storage_limit(config: AppConfig | None = None, keep_paths: list[str | Path] | None = None) -> None:
    config = config or load_app_config()
    if not config.storage.max_size_bytes:
        return

    keep_paths = {Path(p).resolve() for p in keep_paths or [] if p}
    total = total_storage_size(config)
    if total <= config.storage.max_size_bytes:
        return

    if Path(config.paths.json_dir).exists():
        entries = []
        for json_path in Path(config.paths.json_dir).glob("*.json"):
            saved_at, video_path = load_json_metadata(json_path, config)
            ts = saved_at if saved_at else json_path.stat().st_mtime
            entries.append((ts, json_path, video_path))
        entries.sort(key=lambda item: item[0])

        for _, json_path, video_path in entries:
            if json_path.resolve() in keep_paths:
                continue
            try:
                if (
                    video_path
                    and video_path.exists()
                    and video_path.resolve() not in keep_paths
                    and is_app_owned_video_path(video_path, config)
                ):
                    video_path.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                json_path.unlink(missing_ok=True)
            except Exception:
                pass
            total = total_storage_size(config)
            if total <= config.storage.max_size_bytes:
                return

    if total > config.storage.max_size_bytes:
        LOGGER.warning(
            "Storage limit is still exceeded after deleting app-owned sessions. "
            "Untracked files under recordings_dir were left untouched: %s",
            config.paths.recordings_dir,
        )


def launch_obs(config: AppConfig) -> subprocess.Popen[Any]:
    """OBSをバックグラウンドで起動する"""
    if not config.obs.obs_dir:
        raise RecorderError("OBSのパスが未設定です。設定画面の OBSフォルダ (obs.dir) を指定してください。")

    if is_managed_portable_obs_dir(config.obs.obs_dir):
        try:
            migrate_legacy_managed_obs_if_needed(port=config.obs.port, password=config.obs.password)
            repair_legacy_managed_obs_if_present(port=config.obs.port, password=config.obs.password)
        except Exception as e:
            LOGGER.warning("旧OBS配置の移行/修復に失敗しました: %s", e, exc_info=True)

    obs_dir_abs = os.path.abspath(str(config.obs.obs_dir))
    obs_exe = os.path.abspath(os.path.join(obs_dir_abs, "bin", "64bit", "obs64.exe"))

    if not os.path.exists(obs_exe):
        detected = detect_obs_dir()
        hint = f"\n自動検出候補: {detected}" if detected else ""
        raise RecorderError(f"OBSの実行ファイルが見つかりません。\nパス: {obs_exe}{hint}")

    # OBSは起動時にglobal.iniを読み、終了時に再保存する。
    # 管理対象OBSが既に動いている場合は、設定反映前に必ず止める。
    process_manager = OBSProcessManager(obs_dir_abs, logger=LOGGER)
    process_manager.kill_stale_managed_processes()
    unmanaged_processes = process_manager.unmanaged_processes()
    if unmanaged_processes:
        details = []
        for process in unmanaged_processes[:5]:
            path_text = str(process.executable_path) if process.executable_path else "path unknown"
            details.append(f"pid={process.pid} {path_text}")
        raise RecorderError(
            "管理対象外のOBSが既に起動しています。\n"
            "通常版OBSまたは旧配置のOBSが起動していると、このアプリの起動前設定は反映されません。\n"
            + "\n".join(details)
            + "\n既存のOBSを終了してから再実行してください。"
        )

    bootstrapper = OBSBootstrapper(obs_dir_abs)

    try:
        bootstrap_result = bootstrapper.apply(
            port=config.obs.port,
            password=config.obs.password,
            stop_managed_processes=False,
        )
        global_ini_path = bootstrap_result.get("global_ini_path")
        user_ini_path = bootstrap_result.get("user_ini_path")
        if bootstrap_result.get("global_ini_changed") and global_ini_path:
            LOGGER.info("ℹ️ ポータブルOBSの global.ini を更新しました: %s", global_ini_path)
        if bootstrap_result.get("user_ini_changed") and user_ini_path:
            LOGGER.info("ℹ️ ポータブルOBSの user.ini を更新しました: %s", user_ini_path)
        websocket_result = bootstrap_result.get("websocket")
        if websocket_result:
            changed, ws_cfg_path = websocket_result
        else:
            changed, ws_cfg_path = False, None
        if changed and ws_cfg_path:
            LOGGER.info("ℹ️ ポータブルOBSのWebSocket設定を更新しました: %s", ws_cfg_path)
        LOGGER.info("OBS bootstrap paths: global.ini=%s user.ini=%s", global_ini_path, user_ini_path)
    except Exception as e:
        raise RecorderError(f"ポータブルOBS起動前設定の更新に失敗しました: {e}") from e

    ensure_obs_websocket_port_free(config)

    LOGGER.info("🚀 OBSを起動しています (バックグラウンド/非表示)...")

    try:
        started_at = time.time()
        process = process_manager.start_obs(env=process_manager.isolated_env(), hidden=True)
        hidden_windows = process_manager.hide_main_windows(process, timeout_sec=3.0)
        if hidden_windows:
            LOGGER.info("OBSウィンドウを非表示にしました: pid=%s windows=%s", process.pid, hidden_windows)
        # WebSocketの起動待ち
        time.sleep(2)
        process_manager.hide_main_windows(process, timeout_sec=0.5)
        portable_mode = process_manager.latest_log_portable_mode(since=started_at - 1.0)
        if portable_mode is False:
            process_manager.terminate_process(process)
            raise RecorderError(
                "OBSがポータブルモードではなく通常モードで起動しました。\n"
                f"起動対象: {obs_exe}\n"
                "この状態では obs-portable の global.ini が読まれないため、"
                "自動構成ウィザードやタスクトレイ設定を抑止できません。"
            )
        if portable_mode is None:
            LOGGER.warning("OBSログから Portable mode を確認できませんでした: %s", obs_dir_abs)
        return process
    except RecorderError:
        raise
    except Exception as e:
        raise RecorderError(f"OBS起動エラー: {e}") from e


def ensure_portable_mode_marker(base_dir: str | Path) -> Path:
    if not base_dir:
        raise RecorderError("OBSディレクトリが未設定です。")
    base_path = Path(base_dir)
    base_path.mkdir(parents=True, exist_ok=True)

    primary_marker = base_path / PORTABLE_OBS_MARKER_NAME
    if not primary_marker.exists():
        primary_marker.write_text("", encoding="utf-8")

    # OBS本体のバージョン差異に備え、従来名のマーカーも同時に維持する。
    legacy_marker = base_path / LEGACY_PORTABLE_OBS_MARKER_NAME
    if not legacy_marker.exists():
        legacy_marker.write_text("", encoding="utf-8")

    return primary_marker


def kill_stale_obs_processes() -> None:
    """
    アプリ管理OBSだけを起動直前に終了する。
    通常版OBSやユーザーが別用途で起動したOBSは対象外にする。
    """
    OBSProcessManager(MANAGED_PORTABLE_OBS_DIR, logger=LOGGER).kill_stale_managed_processes()


def normalize_summoner_name(value: Any) -> str | None:
    if not value:
        return None
    name = str(value).strip()
    if "#" in name:
        name = name.split("#", 1)[0]
    return name.strip()


def build_output_path(config: AppConfig) -> Path:
    """重複回避のため、存在しないファイル名を返す"""
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = config.paths.json_dir / f"lol_{timestamp}.json"
    if not candidate.exists():
        return candidate
    for i in range(1, 100):
        candidate = config.paths.json_dir / f"lol_{timestamp}_{i:02d}.json"
        if not candidate.exists():
            return candidate
    time.sleep(1)
    return build_output_path(config)


def save_payload(path: str | Path, payload: dict[str, Any]) -> None:
    save_session_payload(path, payload)


class LiveClientRiotAPIClient(RiotAPIClient):
    """aiohttpでRiot Live Client APIを取得する本番用クライアント。"""

    def __init__(self, session_factory: Callable[..., Any] | None = None) -> None:
        self.session_factory = session_factory or aiohttp.ClientSession

    async def _fetch_result(self, url: str, timeout_sec: float) -> RiotPollResult:
        timeout = aiohttp.ClientTimeout(total=float(timeout_sec))
        try:
            async with self.session_factory(timeout=timeout) as session:
                async with session.get(url, ssl=False) as response:
                    response.raise_for_status()
                    try:
                        data = await response.json(content_type=None)
                    except Exception:
                        text = await response.text()
                        data = text.strip().replace('"', "")
                    if isinstance(data, dict):
                        return RiotPollResult(RiotPollStatus.IN_GAME, payload=data)
                    return RiotPollResult(RiotPollStatus.IN_GAME, payload={"value": data})
        except aiohttp.ClientResponseError as e:
            if e.status in {404, 410}:
                return RiotPollResult(RiotPollStatus.NOT_IN_GAME, error=str(e))
            return RiotPollResult(RiotPollStatus.TEMPORARY_FAILURE, error=str(e))
        except (
            aiohttp.ClientConnectionError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
            OSError,
        ) as e:
            return RiotPollResult(RiotPollStatus.TEMPORARY_FAILURE, error=str(e))

    async def _fetch(self, url: str, timeout_sec: float) -> Any:
        result = await self._fetch_result(url, timeout_sec)
        if result.status != RiotPollStatus.IN_GAME:
            return None
        if result.payload and set(result.payload.keys()) == {"value"}:
            return result.payload["value"]
        return result.payload

    async def get_active_player_name(self) -> str | None:
        return await self._fetch(ACTIVE_PLAYER_URL, timeout_sec=5)

    async def get_event_data(self) -> dict[str, Any] | None:
        data = await self._fetch(EVENT_URL, timeout_sec=5)
        return data if isinstance(data, dict) else None

    async def get_all_game_data(self) -> dict[str, Any] | None:
        data = await self._fetch(ALL_GAME_URL, timeout_sec=1)
        return data if isinstance(data, dict) else None

    async def get_all_game_data_result(self) -> RiotPollResult:
        result = await self._fetch_result(ALL_GAME_URL, timeout_sec=1)
        if result.status != RiotPollStatus.IN_GAME:
            return result
        if isinstance(result.payload, dict) and "gameData" in result.payload:
            return result
        return RiotPollResult(RiotPollStatus.TEMPORARY_FAILURE, error="Unexpected allgamedata payload")


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
        self.config = config or load_app_config()
        self.client = None
        self.obs_process = obs_process
        self.status_cb = status_cb
        self.max_retries = int(max_retries)
        self.retry_delay = float(retry_delay)
        self.logger = logging.getLogger(f"lol_replay.obs.{id(self)}")
        self._status_handler = StatusCallbackLogHandler(status_cb) if status_cb else None
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
                self.client, used_host = connect_obs_client(
                    self.config.obs.host,
                    self.config.obs.port,
                    self.config.obs.password,
                )
                version = self.client.get_version()
                host_note = f" host={used_host}" if used_host != self.config.obs.host else ""
                self.log(f"✅ OBS接続成功 (v{version.obs_version}{host_note})")
                return
            except Exception as e:
                last_error = e
                retry_count += 1
                self.logger.info("Connection retrying... (%s/%s)", retry_count, max_retries)
                if retry_count < max_retries and self.retry_delay > 0:
                    time.sleep(self.retry_delay)

        raise RecorderError(
            "OBS WebSocketへの接続に失敗しました。\n"
            f"接続先: {self.config.obs.host}:{self.config.obs.port}\n"
            f"パスワード設定: {'あり' if self.config.obs.password else 'なし'}\n"
            f"詳細: {last_error}"
        )

    def disconnect(self) -> None:
        if self.client:
            try:
                self.client.disconnect()
            finally:
                self.client = None
        if self._status_handler:
            try:
                self.logger.removeHandler(self._status_handler)
            except Exception:
                pass
            self._status_handler = None

    def setup_record_output(self) -> None:
        if self.config.paths.recordings_dir:
            try:
                Path(self.config.paths.recordings_dir).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

            try:
                apply_record_directory_to_obs(self.client, self.config.paths.recordings_dir)
            except Exception:
                # OBSバージョン差異や権限差分で失敗する場合は継続
                pass

        try:
            apply_obs_video_settings(self.client, self.config.obs.fps)
            self.log(f"🎞️ OBS録画FPSを {self.config.obs.fps} に設定しました。")
        except Exception as e:
            # 録画は続行可能なので警告のみ
            self.log(f"⚠️ OBS録画FPS設定の適用に失敗: {e}")

    def apply_record_output_settings(self) -> bool:
        self.setup_record_output()
        return True

    def apply_audio_profile(self, cfg: AppConfig, scene_name: str | None = None) -> bool:
        return apply_audio_profile_from_config(
            self.client,
            cfg,
            scene_name=scene_name or self.config.obs.scene_name,
            status_cb=self.log,
        )

    def get_audio_device_catalog(self, cfg: AppConfig | None = None, scene_name: str | None = None) -> dict[str, Any]:
        return get_audio_device_catalog(
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
            sync_source_item_id = self._ensure_sync_source_exists()
            self._remove_legacy_game_capture_sources()
            self._apply_scene_item_z_order(window_capture_item_id, sync_source_item_id)
            self._remove_empty_initial_scenes()
        except RecorderError:
            self.logger.exception("OBS同期要素セットアップに失敗しました。")
            raise
        except Exception as e:
            self.logger.exception("OBS同期要素セットアップに失敗しました。")
            raise RecorderError(f"OBS同期要素セットアップに失敗しました: {e}") from e

    def _ensure_scene_exists(self) -> None:
        scene_name = self.config.obs.scene_name
        try:
            ensure_obs_scene_exists(self.client, scene_name, status_cb=self.log)
        except Exception as e:
            raise RecorderError(f"シーン '{scene_name}' の自動作成に失敗しました: {e}") from e

    def _set_current_scene(self) -> None:
        scene_name = self.config.obs.scene_name
        try:
            self.client.set_current_program_scene(scene_name)
        except Exception as e:
            self.log(f"⚠️ 現在シーンの切り替えに失敗: {e}")

    def _remove_empty_initial_scenes(self) -> None:
        target_scene = self.config.obs.scene_name
        try:
            scene_resp = self.client.get_scene_list()
            scenes = getattr(scene_resp, "scenes", []) or []
        except Exception as e:
            self.log(f"⚠️ 初期シーン一覧の取得に失敗: {e}")
            return

        for item in scenes:
            if not isinstance(item, dict):
                continue
            scene_name = str(item.get("sceneName") or "")
            if scene_name == target_scene or scene_name not in DEFAULT_OBS_INITIAL_SCENE_NAMES:
                continue
            try:
                scene_items = getattr(self.client.get_scene_item_list(scene_name), "scene_items", []) or []
            except Exception as e:
                self.log(f"⚠️ 初期シーン '{scene_name}' の中身を確認できません: {e}")
                continue
            if scene_items:
                continue
            try:
                self.client.remove_scene(scene_name)
                self.log(f"ℹ️ OBS初期シーン '{scene_name}' を削除しました。")
            except Exception as e:
                self.log(f"⚠️ OBS初期シーン '{scene_name}' の削除に失敗: {e}")

    def _get_input_kind(self, source_name: str) -> str | None:
        try:
            input_resp = self.client.get_input_list()
            input_items = getattr(input_resp, "inputs", []) or []
            for item in input_items:
                if isinstance(item, dict) and item.get("inputName") == source_name:
                    return str(item.get("inputKind") or "")
        except Exception as e:
            self.log(f"⚠️ OBS入力一覧の取得に失敗: {e}")
        return None

    def _remove_legacy_game_capture_sources(self) -> None:
        if self.config.obs.window_capture_name == DEFAULT_OBS_GAME_CAPTURE_NAME:
            return
        input_kind = self._get_input_kind(DEFAULT_OBS_GAME_CAPTURE_NAME)
        if input_kind != "game_capture":
            return
        try:
            self.client.remove_input(DEFAULT_OBS_GAME_CAPTURE_NAME)
            self.log(f"ℹ️ 旧ゲームキャプチャ '{DEFAULT_OBS_GAME_CAPTURE_NAME}' を削除しました。")
        except Exception as e:
            self.log(f"⚠️ 旧ゲームキャプチャ '{DEFAULT_OBS_GAME_CAPTURE_NAME}' の削除に失敗: {e}")

    def _ensure_window_capture_exists(self) -> int:
        scene_name = self.config.obs.scene_name
        source_name = self.config.obs.window_capture_name
        settings = {
            "window": self.config.obs.window_capture_window,
            "method": self.config.obs.window_capture_method,
            "priority": DEFAULT_OBS_WINDOW_CAPTURE_PRIORITY,
            "cursor": False,
            "client_area": True,
            "force_sdr": False,
        }

        input_exists = False
        input_kind_matches = False
        input_kind = self._get_input_kind(source_name)
        input_exists = input_kind is not None
        input_kind_matches = input_kind == "window_capture"

        if input_exists and not input_kind_matches:
            try:
                self.client.remove_input(source_name)
                input_exists = False
            except Exception as e:
                raise RecorderError(
                    f"ウィンドウキャプチャ名 '{source_name}' は存在しますが、種別が window_capture ではありません: {e}"
                ) from e

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
                fallback_settings = {
                    "window": self.config.obs.window_capture_window,
                    "method": self.config.obs.window_capture_method,
                    "priority": DEFAULT_OBS_WINDOW_CAPTURE_PRIORITY,
                }
                try:
                    self.client.create_input(scene_name, source_name, "window_capture", fallback_settings, True)
                except Exception as fallback_error:
                    raise RecorderError(
                        f"ウィンドウキャプチャ '{source_name}' の自動作成に失敗しました: {fallback_error}"
                    ) from fallback_error
        else:
            try:
                self.client.set_input_settings(source_name, settings, overlay=True)
            except Exception:
                pass

        scene_item_id = self._get_scene_item_id(source_name)
        if scene_item_id is None:
            try:
                self.client.create_scene_item(scene_name, source_name, True)
                scene_item_id = self._get_scene_item_id(source_name)
            except Exception as e:
                raise RecorderError(
                    f"ウィンドウキャプチャ '{source_name}' をシーン '{scene_name}' に配置できませんでした: {e}"
                ) from e

        if scene_item_id is None:
            raise RecorderError(
                f"ウィンドウキャプチャ '{source_name}' は存在しますが、シーン '{scene_name}' で見つかりません。"
            )
        return scene_item_id

    def _ensure_sync_source_exists(self) -> int:
        scene_name = self.config.obs.scene_name
        source_name = self.config.obs.source_name
        input_exists = False
        try:
            input_resp = self.client.get_input_list()
            input_items = getattr(input_resp, "inputs", []) or []
            input_exists = any(isinstance(item, dict) and item.get("inputName") == source_name for item in input_items)
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
                except Exception as e:
                    last_error = e
            if not input_exists:
                raise RecorderError(f"色ソース '{source_name}' の自動作成に失敗しました: {last_error}")
        else:
            try:
                self.client.set_input_settings(source_name, {"color": self.config.obs.source_color}, overlay=True)
            except Exception:
                pass

        scene_item_id = self.get_sync_source_id()
        if scene_item_id is None:
            try:
                self.client.create_scene_item(scene_name, source_name, False)
                scene_item_id = self.get_sync_source_id()
            except Exception as e:
                raise RecorderError(
                    f"色ソース '{source_name}' をシーン '{scene_name}' に配置できませんでした: {e}"
                ) from e

        if scene_item_id is None:
            raise RecorderError(f"色ソース '{source_name}' は存在しますが、シーン '{scene_name}' で見つかりません。")

        try:
            self.client.set_scene_item_transform(
                scene_name, scene_item_id, {"positionX": 0.0, "positionY": 0.0, "alignment": 5}
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
        except Exception as e:
            self.logger.warning("⚠️ シーンアイテム取得エラー: %s", e)
        return None

    def _apply_scene_item_z_order(self, capture_source_item_id: int, sync_source_item_id: int) -> None:
        scene_name = self.config.obs.scene_name
        try:
            # obs-websocketでは sceneItemIndex=0 が最背面。同期マーカーは最前面に置く。
            self.client.set_scene_item_index(scene_name, capture_source_item_id, 0)
            items = getattr(self.client.get_scene_item_list(scene_name), "scene_items", []) or []
            self.client.set_scene_item_index(scene_name, sync_source_item_id, max(0, len(items) - 1))
        except Exception as e:
            self.log(f"⚠️ シーンアイテムの重なり順制御に失敗: {e}")

    def get_sync_source_id(self) -> int | None:
        return self._get_scene_item_id(self.config.obs.source_name)

    def set_sync_marker_enabled(self, enabled: bool, source_id: int | None = None) -> None:
        item_id = source_id if source_id is not None else self.get_sync_source_id()
        if item_id is None:
            raise RecorderError(
                f"同期用ソース '{self.config.obs.source_name}' がシーン '{self.config.obs.scene_name}' に見つかりません。"
            )
        self.client.set_scene_item_enabled(self.config.obs.scene_name, item_id, bool(enabled))

    def start_recording(self) -> None:
        self.client.start_record()

    def stop_recording(self) -> str | None:
        res = self.client.stop_record()
        return getattr(res, "output_path", None)

    def is_recording_active(self) -> bool | None:
        status = self.client.get_record_status()
        return getattr(status, "output_active", None)

    def shutdown(self) -> None:
        if self.obs_process:
            self.log("🧹 OBSを終了しています...")
            OBSProcessManager(self.config.obs.obs_dir, logger=self.logger).terminate_process(self.obs_process)

        self.disconnect()


class LoLAutoRecorder(RecordingSessionManager):
    def __init__(
        self,
        config: AppConfig | None = None,
        obs_process: subprocess.Popen[Any] | None = None,
        status_cb: Callable[[str], None] | None = None,
        auto_setup: bool = True,
        obs_client: OBSClient | None = None,
        riot_api_client: RiotAPIClient | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self.config = config or load_app_config()
        self.my_name = None
        self.status_cb = status_cb
        self.stop_requested = False
        self.stop_event = stop_event
        self.auto_setup = bool(auto_setup)
        self.opened = False
        self.logger = logging.getLogger(f"lol_replay.recorder.{id(self)}")
        self._status_handler = StatusCallbackLogHandler(status_cb) if status_cb else None
        if self._status_handler:
            self.logger.addHandler(self._status_handler)
        self.logger.propagate = True
        self.obs_client = obs_client or ObsWebSocketClient(
            config=self.config,
            obs_process=obs_process,
            status_cb=status_cb,
        )
        self.riot_api_client = riot_api_client or LiveClientRiotAPIClient()
        self.obs_process = getattr(self.obs_client, "obs_process", obs_process)
        self.reset_session()

    def open(self) -> None:
        if self.opened:
            return
        try:
            self.connect_obs()
            self.ensure_record_output_setup()
            if self.auto_setup:
                self.ensure_sync_setup()
            self.opened = True
        except Exception:
            try:
                self.shutdown_obs()
            except Exception:
                pass
            raise

    def log(self, message: str) -> None:
        self.logger.info(message)

    def set_stop_event(self, stop_event: asyncio.Event | None) -> None:
        self.stop_event = stop_event

    def request_stop(self) -> None:
        self.stop_requested = True
        if self.stop_event is not None:
            try:
                self.stop_event.set()
            except Exception:
                pass

    def should_stop(self) -> bool:
        return self.stop_requested or bool(self.stop_event and self.stop_event.is_set())

    async def wait_with_stop_async(self, seconds: float, step: float = 0.5) -> bool:
        if self.should_stop():
            return False
        if self.stop_event is not None:
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=max(0.0, float(seconds)))
                return False
            except asyncio.TimeoutError:
                return not self.should_stop()

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(seconds))
        while loop.time() < deadline:
            if self.should_stop():
                return False
            remaining = deadline - loop.time()
            await asyncio.sleep(min(step, max(0.01, remaining)))
        return not self.should_stop()

    def reset_session(self) -> None:
        self.output_file = None
        self.session_phase = RecordingPhase.IDLE
        self.session_outcome = RecordingOutcome.COMPLETED
        self.failure_reason = None
        self.sync_game_time = 0.0
        self.record_path = None
        self.recording_started = False
        self.session_started = False
        self.saved_events = []
        self.all_events = []
        self.processed_event_keys = set()
        self.all_event_keys = set()
        self.my_name = None
        self.my_name_short = None
        self.last_game_data = None
        self.champion_name = None
        self.player_team = None
        self.enemy_champions = []
        self.game_result = None
        self.winning_team = None
        self.session_finalized = False

    def has_session_data(self) -> bool:
        if self.session_finalized:
            return False
        return (
            self.recording_started
            or self.record_path is not None
            or self.sync_game_time > 0.0
            or bool(self.saved_events)
            or bool(self.all_events)
        )

    def mark_session_failed(self, reason: str | BaseException | None) -> None:
        self.session_phase = RecordingPhase.FAILED
        self.session_outcome = RecordingOutcome.FAILED_PARTIAL
        if reason:
            self.failure_reason = str(reason)

    def mark_session_aborted(self, reason: str | BaseException | None = None) -> None:
        self.session_phase = RecordingPhase.ABORTED
        self.session_outcome = RecordingOutcome.ABORTED
        if reason:
            self.failure_reason = str(reason)

    def connect_obs(self) -> None:
        self.obs_client.connect()

    def apply_record_output_settings(self) -> bool:
        return self.obs_client.apply_record_output_settings()

    def apply_audio_profile(self, cfg: AppConfig) -> bool:
        return self.obs_client.apply_audio_profile(cfg, scene_name=self.config.obs.scene_name)

    def get_audio_device_catalog(self, cfg: AppConfig | None = None) -> dict[str, Any]:
        return self.obs_client.get_audio_device_catalog(
            cfg=cfg or self.config,
            scene_name=self.config.obs.scene_name,
        )

    def get_source_id(self) -> int | None:
        return self.obs_client.get_sync_source_id()

    def ensure_record_output_setup(self) -> None:
        self.obs_client.setup_record_output()

    def ensure_sync_setup(self) -> None:
        self.obs_client.setup_sync_elements()

    def ensure_scene_exists(self) -> None:
        self.obs_client.setup_sync_elements()

    def ensure_sync_source_exists(self) -> None:
        self.obs_client.setup_sync_elements()

    async def poll_all_game_data(self) -> RiotPollResult:
        get_result = getattr(self.riot_api_client, "get_all_game_data_result", None)
        if get_result:
            try:
                result = get_result()
                if hasattr(result, "__await__"):
                    result = await result
                if isinstance(result, RiotPollResult):
                    return result
            except TypeError:
                pass
        data = await self.riot_api_client.get_all_game_data()
        if data:
            return RiotPollResult(RiotPollStatus.IN_GAME, payload=data)
        return RiotPollResult(RiotPollStatus.TEMPORARY_FAILURE)

    async def try_update_player_name_async(self) -> None:
        name = await self.riot_api_client.get_active_player_name()
        if name and name != self.my_name:
            self.my_name = name
            self.my_name_short = normalize_summoner_name(name)
            self.log(f"プレイヤー名を特定: {self.my_name}")

    def update_player_info_from_game_data(self, data: dict[str, Any] | None) -> None:
        if not data or not self.my_name:
            return
        players = data.get("allPlayers", [])
        for player in players:
            summoner = player.get("summonerName") or player.get("summoner_name")
            if summoner == self.my_name or summoner == self.my_name_short:
                self.champion_name = player.get("championName") or player.get("champion_name")
                self.player_team = player.get("team")
                break

        if self.player_team:
            enemy_champions = []
            for player in players:
                if player.get("team") == self.player_team:
                    continue
                champion = player.get("championName") or player.get("champion_name")
                if champion:
                    enemy_champions.append(champion)
            self.enemy_champions = enemy_champions

    def get_player_team_by_name(self, name: str | None) -> str | None:
        if not name or not self.last_game_data:
            return None
        lookup_name = normalize_summoner_name(name)
        for player in self.last_game_data.get("allPlayers", []):
            summoner = player.get("summonerName") or player.get("summoner_name")
            if not summoner:
                continue
            if summoner == name or normalize_summoner_name(summoner) == lookup_name:
                return player.get("team")
        return None

    def enrich_event(self, event: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(event or {})
        killer = enriched.get("KillerName") or enriched.get("killerName")
        killer_team = enriched.get("KillerTeam") or enriched.get("killerTeam") or self.get_player_team_by_name(killer)
        if killer_team:
            enriched["KillerTeam"] = killer_team
            enriched["killer_team"] = killer_team
            if self.player_team:
                enriched["team_relation"] = "own" if killer_team == self.player_team else "enemy"
        return enriched

    def update_result_from_events(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        for event in events:
            if not self.is_game_end_event(event):
                continue
            result_value = (
                event.get("Result") or event.get("result") or event.get("GameResult") or event.get("gameResult")
            )
            winning_team = (
                event.get("WinningTeam") or event.get("winningTeam") or event.get("Team") or event.get("team")
            )
            self.game_result = result_value
            self.winning_team = winning_team
            return

    @staticmethod
    def is_game_end_event(event: dict[str, Any] | None) -> bool:
        return bool(event and event.get("EventName") in {"GameEnd", "EndGame", "GameEnded", "GameComplete"})

    async def wait_for_game_start_async(self) -> bool:
        """LoLの試合開始を監視"""
        self.session_phase = RecordingPhase.WAITING_FOR_GAME
        self.log("⚔️  LoLの試合開始を待機中 (API監視)...")
        while True:
            if self.should_stop():
                self.session_phase = RecordingPhase.CANCELLED
                return False
            result = await self.poll_all_game_data()
            data = result.payload
            if data:
                game_time = data.get("gameData", {}).get("gameTime", 0)
                if game_time > 0:
                    self.log(f"🔥 試合開始検知！ GameTime: {game_time:.2f}s")
                    self.output_file = build_output_path(self.config)
                    await self.try_update_player_name_async()
                    self.session_started = True
                    self.session_phase = RecordingPhase.STARTING
                    return True
            if not await self.wait_with_stop_async(1.0):
                self.session_phase = RecordingPhase.CANCELLED
                return False

    async def start_recording_async(self) -> None:
        """録画開始 -> 同期マーカー"""
        self.session_phase = RecordingPhase.STARTING
        self.log("🎥 録画を開始します...")
        item_id = self.get_source_id()
        if not item_id:
            raise RecorderError(
                f"同期用ソース '{self.config.obs.source_name}' がシーン '{self.config.obs.scene_name}' に見つかりません。\n"
                "設定画面の「OBSにシーン/色ソースを作成」を実行してください。"
            )

        try:
            self.obs_client.start_recording()
            self.recording_started = True
            self.session_phase = RecordingPhase.RECORDING
        except OBSSDKRequestError as e:
            self.log(f"⚠️ 録画開始エラー: {e}")
            raise RecorderError(f"OBS録画開始に失敗しました: {e}") from e
        except Exception as e:
            self.log(f"⚠️ 録画開始エラー: {e}")
            raise RecorderError(f"OBS録画開始に失敗しました: {e}") from e
        if not await self.wait_with_stop_async(2.0):
            return

        event_time = await self.wait_until_game_start_event_async()
        if self.should_stop():
            self.session_phase = RecordingPhase.CANCELLED
            return

        self.log("⚡ 同期シグナル送信 (Marker ON)")
        self.obs_client.set_sync_marker_enabled(True, item_id)

        sync_time = 0.0
        result = await self.poll_all_game_data()
        data = result.payload
        if data:
            sync_time = data.get("gameData", {}).get("gameTime", 0.0)
        if (not sync_time or sync_time <= 0) and event_time is not None:
            sync_time = float(event_time)

        self.sync_game_time = sync_time
        self.log(f"📝 同期ログ記録: {sync_time:.4f}s")

        await self.wait_with_stop_async(0.5)
        self.obs_client.set_sync_marker_enabled(False, item_id)
        self.log("✅ シグナル消灯。録画継続中。")

    async def wait_until_game_start_event_async(self, timeout_sec: float = 180) -> float | None:
        loop = asyncio.get_running_loop()
        start = loop.time()
        while loop.time() - start < timeout_sec:
            if self.should_stop():
                return None
            event_data = await self.riot_api_client.get_event_data()
            if event_data:
                for event in event_data.get("Events", []):
                    if event.get("EventName") == "GameStart":
                        return event.get("EventTime", 0.0)
            if not await self.wait_with_stop_async(0.5):
                return None
        self.log("⚠️ GameStart を検知できませんでした。現在のゲーム時間で同期します。")
        return None

    def process_events(self, events: list[dict[str, Any]]) -> None:
        for raw_event in events:
            event = self.enrich_event(raw_event)
            event_id = event.get("EventID")
            event_name = event.get("EventName")
            event_time = event.get("EventTime", 0.0)

            event_key = event_id if event_id is not None else f"{event_name}_{event_time}"

            if event_key not in self.all_event_keys:
                self.all_events.append(event)
                self.all_event_keys.add(event_key)

            if event_key in self.processed_event_keys:
                continue

            should_save = False
            log_message = ""

            if event_name in GLOBAL_OBJECTIVES:
                should_save = True
                log_message = f"[OBJECTIVE] {event_name}"

            elif event_name in COMBAT_EVENTS and self.my_name:
                killer = event.get("KillerName")
                victim = event.get("VictimName")

                # 自分が関与したキル or デスのみ
                is_involved = (
                    killer == self.my_name
                    or victim == self.my_name
                    or killer == self.my_name_short
                    or victim == self.my_name_short
                )

                if is_involved:
                    should_save = True
                    if killer == self.my_name:
                        role = "KILL"
                    elif victim == self.my_name:
                        role = "DEATH"
                    else:
                        role = "ASSIST"
                    log_message = f"[{role}] {event_name}"

            if should_save:
                try:
                    time_text = f"{float(event_time):.1f}"
                except Exception:
                    time_text = "?"
                self.logger.info("%s (Time: %s)", log_message, time_text)
                self.saved_events.append(event)

            self.processed_event_keys.add(event_key)

    async def record_until_end_async(self) -> RecordingOutcome:
        """試合終了まで待機して録画停止"""
        self.session_phase = RecordingPhase.RECORDING
        self.log("🛡️  試合終了を監視中...")
        loop = asyncio.get_running_loop()
        end_detector = RecordingEndDetector(
            error_limit=self.config.polling.end_error_limit,
            missing_grace_sec=self.config.polling.end_missing_grace_sec,
        )
        while True:
            if self.should_stop():
                self.session_phase = RecordingPhase.CANCELLED
                return RecordingOutcome.CANCELLED
            result = await self.poll_all_game_data()
            data = result.payload
            if not data:
                decision = end_detector.observe_poll_status(result.status, loop.time())
                if decision.should_end:
                    self.log("🏁 試合終了検知。録画を停止します。")
                    self.session_phase = RecordingPhase.FINALIZING
                    return RecordingOutcome.COMPLETED
                if not await self.wait_with_stop_async(self.config.polling.end_poll_sec):
                    self.session_phase = RecordingPhase.CANCELLED
                    return RecordingOutcome.CANCELLED
                continue

            end_detector.observe_poll_status(result.status, loop.time())
            self.last_game_data = data
            if not self.my_name:
                await self.try_update_player_name_async()
            self.update_player_info_from_game_data(data)

            event_data = await self.riot_api_client.get_event_data()
            if event_data:
                events = event_data.get("Events", [])
                self.process_events(events)
                self.update_result_from_events(events)
                if any(self.is_game_end_event(event) for event in events):
                    end_detector.observe_game_end_event()
                    self.log("🏁 GameEndイベントを検知。録画を停止します。")
                    self.session_phase = RecordingPhase.FINALIZING
                    return RecordingOutcome.COMPLETED

            if not await self.wait_with_stop_async(self.config.polling.event_poll_sec):
                self.session_phase = RecordingPhase.CANCELLED
                return RecordingOutcome.CANCELLED
        return RecordingOutcome.COMPLETED

    def stop_recording(self) -> None:
        if not self.obs_client.raw_client or self.record_path is not None:
            return
        if not self.recording_started:
            return

        try:
            is_active = self.obs_client.is_recording_active()
            if is_active is False:
                self.recording_started = False
                return
        except Exception:
            pass

        try:
            self.record_path = self.obs_client.stop_recording()
            if self.record_path:
                self.log(f"💾 保存完了: {self.record_path}")
            self.recording_started = False
        except OBSSDKRequestError as e:
            if e.code == 501:
                self.recording_started = False
                return
            self.log(f"⚠️ 録画停止エラー: {e}")
        except Exception as e:
            self.log(f"⚠️ 録画停止エラー: {e}")

    def shutdown_obs(self) -> None:
        self.obs_client.shutdown()
        self.opened = False
        if self._status_handler:
            try:
                self.logger.removeHandler(self._status_handler)
            except Exception:
                pass
            self._status_handler = None

    def disconnect_obs(self) -> None:
        self.obs_client.disconnect()
        self.opened = False
        if self._status_handler:
            try:
                self.logger.removeHandler(self._status_handler)
            except Exception:
                pass
            self._status_handler = None

    def build_session_payload(self) -> dict[str, Any]:
        if self.output_file is None:
            self.output_file = build_output_path(self.config)

        if self.last_game_data and self.game_result is None:
            game_data = self.last_game_data.get("gameData", {})
            if isinstance(game_data, dict):
                self.game_result = game_data.get("gameResult") or game_data.get("result")
                self.winning_team = self.winning_team or game_data.get("winningTeam") or game_data.get("winning_team")

        record_path_for_json = None
        if self.record_path:
            try:
                record_path_for_json = Path(self.record_path).name
            except Exception:
                record_path_for_json = str(self.record_path)

        return SessionLogV1(
            session_status=self.session_outcome.value,
            session_phase=self.session_phase.value,
            failure_reason=self.failure_reason,
            summoner_name=self.my_name,
            champion_name=self.champion_name,
            enemy_champions=list(self.enemy_champions),
            player_team=self.player_team,
            game_result=self.game_result,
            winning_team=self.winning_team,
            saved_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            sync_game_time=self.sync_game_time,
            obs_record_path=record_path_for_json,
            recordings_dir=str(self.config.paths.recordings_dir),
            json_path=str(self.output_file),
            events=list(self.saved_events),
            events_all=list(self.all_events),
        ).to_payload()

    def save_json(self) -> None:
        if self.session_finalized:
            return
        if self.output_file is None:
            self.output_file = build_output_path(self.config)
        payload = self.build_session_payload()
        save_payload(self.output_file, payload)
        self.session_finalized = True
        self.log(f"ログ保存完了: {self.output_file}")
        enforce_storage_limit(self.config, keep_paths=[self.output_file, self.record_path])

    def write_pending_session_payload(self, error: BaseException) -> Path | None:
        try:
            if self.output_file is None:
                self.output_file = build_output_path(self.config)
            payload = self.build_session_payload()
            payload["finalize_error"] = f"{type(error).__name__}: {error}"
            pending_path = self.output_file.with_name(f"{self.output_file.name}.pending")
            pending_path.parent.mkdir(parents=True, exist_ok=True)
            pending_path.write_text(json.dumps(payload, indent=4, ensure_ascii=False), encoding="utf-8")
            return pending_path
        except Exception as pending_error:
            self.log(f"⚠️ pendingセッションログの保存にも失敗しました: {pending_error}")
            return None

    def finalize_session(
        self,
        outcome: RecordingOutcome | None = None,
        failure_reason: str | BaseException | None = None,
    ) -> FinalizeResult:
        if self.session_finalized:
            return FinalizeResult(success=True, outcome=self.session_outcome, saved=False)
        if outcome is not None:
            self.session_outcome = outcome
        self.session_phase = RecordingPhase.FINALIZING
        if self.session_outcome is RecordingOutcome.FAILED_PARTIAL and failure_reason:
            self.failure_reason = str(failure_reason)
        if self.session_outcome is RecordingOutcome.ABORTED and failure_reason:
            self.failure_reason = str(failure_reason)
        self.stop_recording()
        saved = False
        if self.session_outcome is RecordingOutcome.COMPLETED:
            self.session_phase = RecordingPhase.COMPLETED
        elif self.session_outcome is RecordingOutcome.ABORTED:
            self.session_phase = RecordingPhase.ABORTED
        else:
            self.session_phase = RecordingPhase.FAILED
        try:
            if self.has_session_data() and self.session_outcome.should_save_session:
                self.save_json()
                saved = True
            return FinalizeResult(success=True, outcome=self.session_outcome, saved=saved)
        except Exception as e:
            self.session_phase = RecordingPhase.FAILED
            pending_path = self.write_pending_session_payload(e)
            self.log(f"⚠️ セッションfinalizeに失敗しました: {e}")
            return FinalizeResult(
                success=False,
                outcome=self.session_outcome,
                saved=False,
                error=f"{type(e).__name__}: {e}",
                pending_path=str(pending_path) if pending_path else None,
            )


async def run_cli_recorder() -> None:
    app = None
    try:
        settings = load_settings()
        preflight = run_preflight_checks(settings, auto_fix=True, ensure_dirs=True)
        if preflight.get("changed"):
            save_settings(preflight["config"])
            LOGGER.info("🛠️ 設定を自動補完しました。")
        for warning in preflight.get("warnings", []):
            LOGGER.warning("⚠️ %s", warning)
        if preflight.get("errors"):
            raise RecorderError("\n".join(preflight["errors"]))
        settings = preflight["config"]
        config = AppConfig.from_dict(settings)

        setup_environment(config)
        obs_process = launch_obs(config)

        app = LoLAutoRecorder(config=config, obs_process=obs_process)
        app.open()
        try:
            app.apply_audio_profile(config)
            LOGGER.info("🔊 音声設定をOBSへ適用しました。")
        except Exception as e:
            LOGGER.warning("⚠️ 音声設定の適用に失敗: %s", e)
        while True:
            app.reset_session()
            started = await app.wait_for_game_start_async()
            if not started:
                break
            try:
                await app.start_recording_async()
                outcome = await app.record_until_end_async()
            except Exception as e:
                if app.has_session_data():
                    app.mark_session_failed(e)
                    result = app.finalize_session(outcome=RecordingOutcome.FAILED_PARTIAL, failure_reason=e)
                    if result.success:
                        LOGGER.warning("⚠️ 録画セッションを部分保存しました: %s", e)
                    else:
                        LOGGER.error("❌ 録画セッションの部分保存に失敗しました: %s", result.error)
                raise
            if outcome != RecordingOutcome.COMPLETED:
                if app.has_session_data():
                    app.mark_session_aborted("recording was cancelled")
                    result = app.finalize_session(
                        outcome=RecordingOutcome.ABORTED,
                        failure_reason="recording was cancelled",
                    )
                    if result.success:
                        LOGGER.info("⏹️ 録画セッションを中断ログとして保存しました。")
                    else:
                        LOGGER.error("❌ 中断ログの保存に失敗しました: %s", result.error)
                LOGGER.info("⏹️ 録画セッションを中断しました。")
                break
            result = app.finalize_session(outcome=RecordingOutcome.COMPLETED)
            if not result.success:
                LOGGER.error("❌ セッション保存に失敗しました: %s", result.error)
                break
            LOGGER.info("✅ 試合記録完了。次の試合を待機します。")
    except KeyboardInterrupt:
        LOGGER.info("中断を検知しました。終了処理を行います。")
    except RecorderError as e:
        LOGGER.error("❌ %s", e)
        sys.exit(1)
    finally:
        if app:
            app.stop_recording()
            app.shutdown_obs()
        LOGGER.info("👋 全ての処理が完了しました。")


if __name__ == "__main__":
    asyncio.run(run_cli_recorder())
