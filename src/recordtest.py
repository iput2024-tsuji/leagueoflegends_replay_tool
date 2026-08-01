from __future__ import annotations

import asyncio
import configparser
import io
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path, PureWindowsPath
from typing import Any

import obsws_python as obs
import urllib3
from obsws_python.error import OBSSDKRequestError

try:
    from . import config_schema, storage_policy as _storage_policy
    from .app_paths import get_app_root, get_user_data_root
    from .champ_select import ChampSelectTracker
    from .config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
    from .game_events import (
        COMBAT_EVENT_NAMES,
        GLOBAL_OBJECTIVE_EVENT_NAMES,
        champion_kill_role,
        normalize_summoner_name,
    )
    from .match_metadata import merge_live_game_metadata
    from .mpv_support import has_mpv_dll
    from .obs_bootstrap import (
        OBSBootstrapper as SharedOBSBootstrapper,
        OBSConfigFileSnapshot,
        OBSMigrationInProgressError,
        OBSMigrationRecoveryRequiredError,
        OBSPathSafetyError,
        ensure_safe_obs_config_directory,
        get_obs_config_dir as shared_get_obs_config_dir,
        get_obs_global_ini_path as shared_get_obs_global_ini_path,
        get_obs_user_ini_path as shared_get_obs_user_ini_path,
        get_obs_websocket_config_path as shared_get_obs_websocket_config_path,
        get_portable_marker_path as shared_get_portable_marker_path,
        is_obs_copy_in_progress,
        lexical_absolute_path,
        list_safe_obs_config_directory,
        migrate_legacy_obs_installation,
        new_obs_ini_parser as shared_new_obs_ini_parser,
        obs_config_mutation_guard,
        preflight_obs_config_directory,
        preflight_obs_config_file,
        revalidate_obs_config_file,
        validate_obs_installation_path,
        write_preflighted_obs_config_file,
    )
    from .obs_process import OBSProcessManager
    from .post_game_result import (
        PostGameResult,
        normalize_game_result_value,
        normalize_lcu_team,
        opposing_lcu_team,
    )
    from .recorder_config import (  # noqa: F401 - compatibility exports
        AppConfig as RecorderAppConfig,
        AudioSettings,
        AudioSlotSettings,
        OBSSettings,
        PathsSettings,
        PollingSettings,
        StorageSettings,
    )
    from .recording_state import (
        FinalizeResult,
        RecordingEndDecision,
        RecordingEndDetector,
        RecordingEndReason,
        RecordingOutcome,
        RecordingPhase,
    )
    from .riot_api import LiveClientRiotAPIClient, RiotAPIClient, RiotPollResult, RiotPollStatus
    from .session_log import SessionLogV1, save_session_payload
except ImportError:
    import config_schema
    import storage_policy as _storage_policy
    from app_paths import get_app_root, get_user_data_root
    from champ_select import ChampSelectTracker
    from config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
    from game_events import (
        COMBAT_EVENT_NAMES,
        GLOBAL_OBJECTIVE_EVENT_NAMES,
        champion_kill_role,
        normalize_summoner_name,
    )
    from match_metadata import merge_live_game_metadata
    from mpv_support import has_mpv_dll
    from obs_bootstrap import (
        OBSBootstrapper as SharedOBSBootstrapper,
        OBSConfigFileSnapshot,
        OBSMigrationInProgressError,
        OBSMigrationRecoveryRequiredError,
        OBSPathSafetyError,
        ensure_safe_obs_config_directory,
        get_obs_config_dir as shared_get_obs_config_dir,
        get_obs_global_ini_path as shared_get_obs_global_ini_path,
        get_obs_user_ini_path as shared_get_obs_user_ini_path,
        get_obs_websocket_config_path as shared_get_obs_websocket_config_path,
        get_portable_marker_path as shared_get_portable_marker_path,
        is_obs_copy_in_progress,
        lexical_absolute_path,
        list_safe_obs_config_directory,
        migrate_legacy_obs_installation,
        new_obs_ini_parser as shared_new_obs_ini_parser,
        obs_config_mutation_guard,
        preflight_obs_config_directory,
        preflight_obs_config_file,
        revalidate_obs_config_file,
        validate_obs_installation_path,
        write_preflighted_obs_config_file,
    )
    from obs_process import OBSProcessManager
    from post_game_result import (
        PostGameResult,
        normalize_game_result_value,
        normalize_lcu_team,
        opposing_lcu_team,
    )
    from recorder_config import (  # noqa: F401 - compatibility exports
        AppConfig as RecorderAppConfig,
        AudioSettings,
        AudioSlotSettings,
        OBSSettings,
        PathsSettings,
        PollingSettings,
        StorageSettings,
    )
    from recording_state import (
        FinalizeResult,
        RecordingEndDecision,
        RecordingEndDetector,
        RecordingEndReason,
        RecordingOutcome,
        RecordingPhase,
    )
    from riot_api import LiveClientRiotAPIClient, RiotAPIClient, RiotPollResult, RiotPollStatus
    from session_log import SessionLogV1, save_session_payload

ROOT_DIR = get_app_root()
DATA_DIR = get_user_data_root()
CONFIG_REPOSITORY = ConfigRepository(CONFIG_PATH, SAMPLE_CONFIG_PATH)

LIVECLIENT_BASE = "https://127.0.0.1:2999/liveclientdata"
ACTIVE_PLAYER_URL = f"{LIVECLIENT_BASE}/activeplayername"
EVENT_URL = f"{LIVECLIENT_BASE}/eventdata"
ALL_GAME_URL = f"{LIVECLIENT_BASE}/allgamedata"
LCU_CHAMP_SELECT_PATH = "/lol-champ-select/v1/session"
LCU_CHAMPION_SUMMARY_PATH = "/lol-game-data/assets/v1/champion-summary.json"
LCU_GAMEFLOW_PHASE_PATH = "/lol-gameflow/v1/gameflow-phase"
LCU_GAMEFLOW_SESSION_PATH = "/lol-gameflow/v1/session"
LCU_GAME_QUEUES_PATH = "/lol-game-queues/v1/queues"
LCU_END_OF_GAME_STATS_PATH = "/lol-end-of-game/v1/eog-stats-block"
LCU_GAMECLIENT_END_OF_GAME_STATS_PATH = "/lol-end-of-game/v1/gameclient-eog-stats-block"

DEFAULT_OBS_PASSWORD_LENGTH = config_schema.DEFAULT_OBS_PASSWORD_LENGTH
DEFAULT_OBS_SCENE_NAME = config_schema.DEFAULT_OBS_SCENE_NAME
DEFAULT_OBS_SOURCE_NAME = config_schema.DEFAULT_OBS_SOURCE_NAME
DEFAULT_OBS_SOURCE_COLOR = config_schema.DEFAULT_OBS_SOURCE_COLOR
LEGACY_OBS_SOURCE_COLOR_BLUE = config_schema.LEGACY_OBS_SOURCE_COLOR_BLUE
DEFAULT_OBS_WINDOW_CAPTURE_NAME = config_schema.DEFAULT_OBS_WINDOW_CAPTURE_NAME
DEFAULT_OBS_WINDOW_CAPTURE_WINDOW = config_schema.DEFAULT_OBS_WINDOW_CAPTURE_WINDOW
DEFAULT_OBS_WINDOW_CAPTURE_METHOD = config_schema.DEFAULT_OBS_WINDOW_CAPTURE_METHOD
DEFAULT_OBS_WINDOW_CAPTURE_PRIORITY = 2  # Match by executable
# OBS process-loopback audio tied to WGC can lose the LoL window during load transitions.
DEFAULT_OBS_WINDOW_CAPTURE_AUDIO = False
DEFAULT_OBS_INITIAL_SCENE_NAMES = frozenset({"Scene", "シーン"})
DEFAULT_OBS_GAME_CAPTURE_NAME = config_schema.DEFAULT_OBS_GAME_CAPTURE_NAME
DEFAULT_OBS_LEGACY_GAME_CAPTURE_WINDOW = config_schema.DEFAULT_OBS_LEGACY_GAME_CAPTURE_WINDOW
DEFAULT_OBS_GAME_CAPTURE_WINDOW = DEFAULT_OBS_LEGACY_GAME_CAPTURE_WINDOW
DEFAULT_OBS_DIR = config_schema.DEFAULT_OBS_DIR
LEGACY_OBS_DIR = "bin/OBS-Studio"
DEFAULT_BIN_DIR = config_schema.DEFAULT_BIN_DIR
DEFAULT_RECORDINGS_DIR = config_schema.DEFAULT_RECORDINGS_DIR
DEFAULT_JSON_DIR = config_schema.DEFAULT_JSON_DIR
DEFAULT_CHAMPION_ICONS_DIR = config_schema.DEFAULT_CHAMPION_ICONS_DIR
DEFAULT_CHAMPION_ALIASES_PATH = config_schema.DEFAULT_CHAMPION_ALIASES_PATH
DEFAULT_OBS_HOST = config_schema.DEFAULT_OBS_HOST
DEFAULT_OBS_PORT = config_schema.DEFAULT_OBS_PORT
DEFAULT_OBS_FPS_NUMERATOR = config_schema.DEFAULT_OBS_FPS_NUMERATOR
DEFAULT_OBS_FPS_DENOMINATOR = config_schema.DEFAULT_OBS_FPS_DENOMINATOR
MAX_OBS_FPS_NUMERATOR = config_schema.MAX_OBS_FPS_NUMERATOR
MAX_OBS_FPS_DENOMINATOR = config_schema.MAX_OBS_FPS_DENOMINATOR
DEFAULT_OBS_BASE_WIDTH = config_schema.DEFAULT_OBS_BASE_WIDTH
DEFAULT_OBS_BASE_HEIGHT = config_schema.DEFAULT_OBS_BASE_HEIGHT
DEFAULT_OBS_OUTPUT_WIDTH = config_schema.DEFAULT_OBS_OUTPUT_WIDTH
DEFAULT_OBS_OUTPUT_HEIGHT = config_schema.DEFAULT_OBS_OUTPUT_HEIGHT
DEFAULT_OBS_SCALE_TYPE = config_schema.DEFAULT_OBS_SCALE_TYPE
DEFAULT_OBS_RECORDING_QUALITY = config_schema.DEFAULT_OBS_RECORDING_QUALITY
DEFAULT_OBS_RECORDING_ENCODER = config_schema.DEFAULT_OBS_RECORDING_ENCODER
VALID_OBS_RECORDING_ENCODERS = config_schema.VALID_OBS_RECORDING_ENCODERS
DEFAULT_OBS_OUTPUT_MODE = "Simple"
DEFAULT_OBS_RECORDING_FORMAT = "mkv"
DEFAULT_OBS_RECORDING_TRACKS = "1"
DEFAULT_OBS_SIMPLE_AUDIO_ENCODER = "aac"
DEFAULT_OBS_ADVANCED_AUDIO_ENCODER = "ffmpeg_aac"
DEFAULT_OBS_SIMPLE_X264_PRESET = "veryfast"
MANAGED_OBS_PROFILE_DIR_NAME = "LoLReplayTool"
MANAGED_OBS_PROFILE_NAME = MANAGED_OBS_PROFILE_DIR_NAME
VALID_OBS_SCALE_TYPES = config_schema.VALID_OBS_SCALE_TYPES
VALID_OBS_RECORDING_QUALITIES = config_schema.VALID_OBS_RECORDING_QUALITIES
DEFAULT_END_ERROR_LIMIT = config_schema.DEFAULT_END_ERROR_LIMIT
DEFAULT_END_MISSING_GRACE_SEC = config_schema.DEFAULT_END_MISSING_GRACE_SEC
DEFAULT_END_TEMPORARY_FAILURE_GRACE_SEC = config_schema.DEFAULT_END_TEMPORARY_FAILURE_GRACE_SEC
DEFAULT_END_POLL_SEC = config_schema.DEFAULT_END_POLL_SEC
DEFAULT_EVENT_POLL_SEC = config_schema.DEFAULT_EVENT_POLL_SEC
DEFAULT_MAX_STORAGE_GB = config_schema.DEFAULT_MAX_STORAGE_GB
DEFAULT_AUDIO_MIC_INPUT_NAME = config_schema.DEFAULT_AUDIO_MIC_INPUT_NAME
DEFAULT_AUDIO_DEVICE_ID = config_schema.DEFAULT_AUDIO_DEVICE_ID
DEFAULT_AUDIO_DEVICE_NAME = config_schema.DEFAULT_AUDIO_DEVICE_NAME
DEFAULT_AUDIO_MIC_VOLUME_DB = config_schema.DEFAULT_AUDIO_MIC_VOLUME_DB
DEFAULT_AUDIO_MIC_MUTE = config_schema.DEFAULT_AUDIO_MIC_MUTE
DEFAULT_RECORDING_START_TIMEOUT_SEC = 15.0
DEFAULT_RECORDING_START_PRIMARY_TIMEOUT_SEC = 5.0
DEFAULT_RECORDING_START_RECOVERY_TIMEOUT_SEC = 12.0
DEFAULT_RECORDING_START_POLL_SEC = 0.25
DEFAULT_RECORDING_START_SETTLE_SEC = 0.75
DEFAULT_OBS_ENCODER_LOG_WAIT_SEC = 4.0
DEFAULT_OBS_ENCODER_LOG_POLL_SEC = 0.25
DEFAULT_GAME_START_EVENT_WAIT_SEC = 3.0
DEFAULT_GAME_START_DIAGNOSTIC_INTERVAL_SEC = 30.0
DEFAULT_LCU_START_LIVE_CLIENT_GRACE_SEC = 20.0
DEFAULT_LCU_START_LIVE_CLIENT_POLL_SEC = 1.0
DEFAULT_GAMEFLOW_INACTIVE_GRACE_SEC = 10.0
DEFAULT_GAME_PROCESS_MISSING_GRACE_SEC = 10.0
DEFAULT_POST_GAME_RESULT_WAIT_SEC = 12.0
DEFAULT_POST_GAME_RESULT_POLL_SEC = 1.0
DEFAULT_SYNC_STALE_GAME_TIME_TOLERANCE_SEC = 1.0
LOL_GAME_PROCESS_NAME = "League of Legends.exe"
MIN_RECORDING_FREE_SPACE_BYTES = 64 * 1024 * 1024
LCU_GAMEFLOW_START_PHASES = frozenset({"gamestart", "inprogress", "reconnect"})
LCU_POST_GAME_RESULT_WAIT_PHASES = frozenset({"preendofgame", "waitingforstats", "endofgame"})
OBS_GLOBAL_AUDIO_DEVICE_PARAMETERS = (
    "DesktopDevice1",
    "DesktopDevice2",
    "AuxDevice1",
    "AuxDevice2",
    "AuxDevice3",
    "AuxDevice4",
)

MANAGED_PORTABLE_OBS_DIR = lexical_absolute_path(DATA_DIR / DEFAULT_OBS_DIR)
LEGACY_MANAGED_OBS_DIR = lexical_absolute_path(ROOT_DIR / LEGACY_OBS_DIR)
LEGACY_ROOT_OBS_DIR = lexical_absolute_path(ROOT_DIR / DEFAULT_OBS_DIR)
LEGACY_DATA_BIN_OBS_DIR = lexical_absolute_path(DATA_DIR / LEGACY_OBS_DIR)
PORTABLE_OBS_MARKER_NAME = "obs_portable_mode.txt"
LEGACY_PORTABLE_OBS_MARKER_NAME = "portable_mode.txt"
MANAGED_AUDIO_INPUTS = config_schema.MANAGED_AUDIO_INPUTS


class AppConfig(RecorderAppConfig):
    """Compatibility facade that honors the patchable managed OBS path."""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AppConfig:
        config = RecorderAppConfig.from_dict(data)
        return cls(
            obs=replace(config.obs, obs_dir=MANAGED_PORTABLE_OBS_DIR),
            paths=config.paths,
            polling=config.polling,
            storage=config.storage,
            audio=config.audio,
        )

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


def is_lol_game_process_running() -> bool | None:
    """Return whether the LoL game process is currently alive on Windows."""

    if os.name != "nt":
        return None

    command = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        f"Get-CimInstance Win32_Process -Filter \"Name='{LOL_GAME_PROCESS_NAME}'\" "
        "| Select-Object -First 1 -ExpandProperty ProcessId"
    )
    kwargs: dict[str, object] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 3,
        "check": False,
    }
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        kwargs["creationflags"] = create_no_window
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                command,
            ],
            **kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return bool(str(completed.stdout or "").strip())


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
    def get_record_status_details(self) -> dict[str, Any]:
        pass

    @abstractmethod
    def shutdown(self) -> None:
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
    if base_dir is None:
        return False
    try:
        if not validate_obs_installation_path(base_dir):
            return False
        SharedOBSBootstrapper(base_dir, logger=LOGGER).validate_layout()
        return not is_obs_copy_in_progress(base_dir)
    except Exception:
        return False


def legacy_managed_obs_dirs() -> tuple[Path, ...]:
    seen = set()
    result = []
    for path in (LEGACY_ROOT_OBS_DIR, LEGACY_MANAGED_OBS_DIR, LEGACY_DATA_BIN_OBS_DIR):
        normalized = lexical_absolute_path(path)
        key = str(normalized).casefold()
        if key == str(MANAGED_PORTABLE_OBS_DIR).casefold() or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
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
        candidate = lexical_absolute_path(base_dir)
        return candidate == MANAGED_PORTABLE_OBS_DIR
    except Exception:
        return False


def is_legacy_managed_obs_dir(base_dir: str | Path | None) -> bool:
    if not base_dir:
        return False
    try:
        candidate = lexical_absolute_path(base_dir)
        return any(candidate == legacy_dir for legacy_dir in legacy_managed_obs_dirs())
    except Exception:
        return False


def bootstrap_obs_dir(base_dir: str | Path, port: int | None = None, password: str = "") -> dict[str, Any]:
    base_path = lexical_absolute_path(base_dir)
    bootstrapper = SharedOBSBootstrapper(
        base_path,
        process_manager=OBSProcessManager(base_path, logger=LOGGER),
        logger=LOGGER,
    )
    if port is not None:
        password = ensure_obs_password_value(password)[0]
    return bootstrapper.apply(port=port, password=password)


def migrate_legacy_managed_obs_if_needed(port: int | None = None, password: str = "") -> Path | None:
    def prepare_source(legacy_dir: Path) -> None:
        OBSProcessManager(legacy_dir, logger=LOGGER).kill_stale_managed_processes()

    def finalize_destination(destination: Path) -> None:
        bootstrap_obs_dir(destination, port=port, password=password)

    legacy_dir = migrate_legacy_obs_installation(
        MANAGED_PORTABLE_OBS_DIR,
        legacy_managed_obs_dirs(),
        prepare_source=prepare_source,
        finalize_destination=finalize_destination,
    )
    if legacy_dir is None:
        return None
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


@dataclass(frozen=True)
class _OBSRecordingProfileLayout:
    base_dir: Path
    profiles_root: Path
    profile_directories: tuple[Path, ...]
    profile_files: tuple[OBSConfigFileSnapshot, ...]
    user_file: OBSConfigFileSnapshot


@dataclass(frozen=True)
class _OBSRecordingProfileFileUpdate:
    snapshot: OBSConfigFileSnapshot
    payload: bytes | None

    @property
    def changed(self) -> bool:
        return self.payload is not None


@dataclass(frozen=True)
class _OBSRecordingProfileUpdatePlan:
    layout: _OBSRecordingProfileLayout
    updates: tuple[_OBSRecordingProfileFileUpdate, ...]


_WINDOWS_RESERVED_PROFILE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def _validate_obs_profile_dir_name(value: object) -> str:
    name = str(value or "")
    windows_path = PureWindowsPath(name)
    reserved_stem = name.split(".", 1)[0].upper()
    if (
        not name
        or name in {".", ".."}
        or name != name.rstrip(" .")
        or any(character in name for character in ('/', '\\', ':', '\0', '<', '>', '"', '|', '?', '*'))
        or any(ord(character) < 32 for character in name)
        or windows_path.drive
        or windows_path.is_absolute()
        or reserved_stem in _WINDOWS_RESERVED_PROFILE_NAMES
    ):
        raise OBSPathSafetyError(f"OBS profile directory名が安全な単一componentではありません: {name!r}")
    return name


def _obs_ini_parser_from_snapshot(snapshot: OBSConfigFileSnapshot) -> tuple[Any, bool]:
    parser = shared_new_obs_ini_parser()
    if snapshot.payload is None:
        return parser, True
    try:
        text = snapshot.payload.decode("utf-8")
        had_bom = text.startswith("\ufeff")
        if had_bom:
            text = text.lstrip("\ufeff")
        parser.read_string(text)
        return parser, had_bom
    except (UnicodeError, configparser.Error):
        return shared_new_obs_ini_parser(), True


def _render_obs_ini(parser: Any) -> bytes:
    buffer = io.StringIO()
    parser.write(buffer, space_around_delimiters=False)
    return buffer.getvalue().encode("utf-8")


def _raw_obs_profile_selection_values(snapshot: OBSConfigFileSnapshot) -> dict[str, str]:
    if snapshot.payload is None:
        return {}
    try:
        text = snapshot.payload.decode("utf-8").lstrip("\ufeff")
    except UnicodeError:
        return {}

    section = ""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if section != "Basic" or not stripped or stripped.startswith(("#", ";")):
            continue
        delimiter_indexes = [index for delimiter in ("=", ":") if (index := raw_line.find(delimiter)) >= 0]
        if not delimiter_indexes:
            continue
        delimiter_index = min(delimiter_indexes)
        key = raw_line[:delimiter_index].strip().casefold()
        if key in {"profiledir", "profile"}:
            # Discard formatting whitespace after the delimiter, but retain
            # trailing whitespace because Windows normalizes it in path names.
            values[key] = raw_line[delimiter_index + 1 :].lstrip(" \t")
    return values


def _set_obs_ini_value(parser: Any, section: str, key: str, value: str) -> bool:
    value_text = str(value)
    if not parser.has_section(section):
        parser.add_section(section)
        parser.set(section, key, value_text)
        return True
    current = parser.get(section, key, fallback=None)
    if current != value_text:
        parser.set(section, key, value_text)
        return True
    return False


def _obs_current_profile_dir_name(user_file: OBSConfigFileSnapshot) -> str | None:
    parser, _normalized = _obs_ini_parser_from_snapshot(user_file)
    if not parser.has_section("Basic"):
        return None
    raw_values = _raw_obs_profile_selection_values(user_file)
    for key in ("ProfileDir", "Profile"):
        value = parser.get("Basic", key, fallback="")
        value_text = str(value or "")
        if value_text.strip():
            raw_value = raw_values.get(key.casefold())
            if raw_value is not None:
                _validate_obs_profile_dir_name(raw_value)
            return _validate_obs_profile_dir_name(value_text)
    return None


def preflight_obs_recording_profile_ini(base_dir: str | Path) -> _OBSRecordingProfileLayout:
    """Inspect every profile target lexically without creating or writing anything."""

    base_path = lexical_absolute_path(base_dir)
    profiles_root = get_obs_config_dir(base_path) / "basic" / "profiles"
    user_file = preflight_obs_config_file(
        get_obs_user_ini_path(base_path),
        label="user.ini",
    )
    current_name = _obs_current_profile_dir_name(user_file)

    names: list[str] = []
    seen_names: set[str] = set()

    def add_profile_name(value: object) -> None:
        name = _validate_obs_profile_dir_name(value)
        name_key = name.casefold()
        if name_key not in seen_names:
            seen_names.add(name_key)
            names.append(name)

    add_profile_name(MANAGED_OBS_PROFILE_DIR_NAME)
    if current_name is not None:
        add_profile_name(current_name)

    discovered_files: dict[str, OBSConfigFileSnapshot] = {}
    discovered_names: dict[str, str] = {}
    discovered_directories: list[Path] = []
    for entry in list_safe_obs_config_directory(profiles_root):
        if entry.kind != "directory":
            continue
        name = _validate_obs_profile_dir_name(entry.name)
        name_key = name.casefold()
        previous_name = discovered_names.get(name_key)
        if previous_name is not None and previous_name != name:
            raise OBSPathSafetyError(
                "OBS profile directory名がcase-insensitiveに衝突しています: "
                f"{previous_name!r}, {name!r}"
            )
        discovered_names[name_key] = name
        profile_dir = profiles_root / name
        if not preflight_obs_config_directory(profile_dir):
            raise OBSPathSafetyError(f"OBS profile directoryが列挙後に消失しました: {profile_dir}")
        discovered_directories.append(lexical_absolute_path(profile_dir))
        snapshot = preflight_obs_config_file(profile_dir / "basic.ini", label="basic.ini")
        if snapshot.exists:
            discovered_files[name_key] = snapshot
            add_profile_name(name)

    profile_directories: list[Path] = []
    profile_files: list[OBSConfigFileSnapshot] = []
    for name in names:
        profile_dir = lexical_absolute_path(profiles_root / name)
        if profile_dir.parent != lexical_absolute_path(profiles_root):
            raise OBSPathSafetyError(f"OBS profile pathがprofiles root外です: {profile_dir}")
        preflight_obs_config_directory(profile_dir)
        profile_directories.append(profile_dir)
        profile_files.append(
            discovered_files.get(name.casefold())
            or preflight_obs_config_file(profile_dir / "basic.ini", label="basic.ini")
        )

    all_directories = {str(path).casefold(): path for path in discovered_directories}
    for profile_dir in profile_directories:
        all_directories.setdefault(str(profile_dir).casefold(), profile_dir)
    return _OBSRecordingProfileLayout(
        base_dir=base_path,
        profiles_root=lexical_absolute_path(profiles_root),
        profile_directories=tuple(all_directories.values()),
        profile_files=tuple(profile_files),
        user_file=user_file,
    )


def _prepare_obs_user_profile_selection(
    user_file: OBSConfigFileSnapshot,
    profile_dir_name: str,
) -> _OBSRecordingProfileFileUpdate:
    parser, changed = _obs_ini_parser_from_snapshot(user_file)
    changed = _set_obs_ini_value(parser, "Basic", "Profile", MANAGED_OBS_PROFILE_NAME) or changed
    changed = _set_obs_ini_value(parser, "Basic", "ProfileDir", profile_dir_name) or changed
    return _OBSRecordingProfileFileUpdate(
        snapshot=user_file,
        payload=_render_obs_ini(parser) if changed else None,
    )


def _select_requested_obs_recording_encoder(
    recording_encoder: str = DEFAULT_OBS_RECORDING_ENCODER,
    *,
    obs_dir: str | Path | None = None,
) -> OBSRecordingEncoderSelection:
    requested_encoder = str(recording_encoder or DEFAULT_OBS_RECORDING_ENCODER).strip().lower()
    if requested_encoder == "auto":
        return detect_obs_recording_encoder(obs_dir)
    if requested_encoder not in VALID_OBS_RECORDING_ENCODERS:
        requested_encoder = "x264"
    return OBSRecordingEncoderSelection(
        requested_encoder,
        requested_encoder,
        requested_encoder,
        requested_encoder != "x264",
    )


def _prepare_obs_recording_profile_ini(
    snapshot: OBSConfigFileSnapshot,
    *,
    record_dir: str | Path,
    scale_type: str = DEFAULT_OBS_SCALE_TYPE,
    recording_quality: str = DEFAULT_OBS_RECORDING_QUALITY,
    recording_encoder: str = DEFAULT_OBS_RECORDING_ENCODER,
    obs_dir: str | Path | None = None,
    selected_encoder: OBSRecordingEncoderSelection | None = None,
) -> _OBSRecordingProfileFileUpdate:
    parser, changed = _obs_ini_parser_from_snapshot(snapshot)
    if not parser.has_section("General"):
        parser.add_section("General")
        changed = True
    current_name = parser.get("General", "Name", fallback="")
    profile_dir_name = snapshot.path.parent.name
    is_managed_profile = profile_dir_name.casefold() == MANAGED_OBS_PROFILE_DIR_NAME.casefold()
    expected_name = MANAGED_OBS_PROFILE_NAME if is_managed_profile else profile_dir_name
    if current_name == "" or (is_managed_profile and current_name != expected_name):
        parser.set("General", "Name", expected_name)
        changed = True

    encoder_selection = selected_encoder or _select_requested_obs_recording_encoder(recording_encoder, obs_dir=obs_dir)
    for section, key, value in _obs_recording_profile_parameter_updates(
        scale_type=scale_type,
        recording_quality=recording_quality,
        selected_encoder=encoder_selection,
        record_dir=record_dir,
    ):
        changed = _set_obs_ini_value(parser, section, key, value) or changed

    return _OBSRecordingProfileFileUpdate(
        snapshot=snapshot,
        payload=_render_obs_ini(parser) if changed else None,
    )


def _prepare_obs_recording_profile_update(
    layout: _OBSRecordingProfileLayout,
    *,
    record_dir: str | Path,
    scale_type: str,
    recording_quality: str,
    recording_encoder: str,
    selected_encoder: OBSRecordingEncoderSelection | None,
) -> _OBSRecordingProfileUpdatePlan:
    updates = [
        _prepare_obs_recording_profile_ini(
            snapshot,
            record_dir=record_dir,
            scale_type=scale_type,
            recording_quality=recording_quality,
            recording_encoder=recording_encoder,
            obs_dir=layout.base_dir,
            selected_encoder=selected_encoder,
        )
        for snapshot in layout.profile_files
    ]
    updates.append(
        _prepare_obs_user_profile_selection(
            layout.user_file,
            MANAGED_OBS_PROFILE_DIR_NAME,
        )
    )
    return _OBSRecordingProfileUpdatePlan(layout=layout, updates=tuple(updates))


def _revalidate_obs_recording_profile_layout(layout: _OBSRecordingProfileLayout) -> None:
    # Re-enumeration catches a reparse/special entry introduced after the plan.
    entries = list_safe_obs_config_directory(layout.profiles_root)
    for entry in entries:
        if entry.kind != "directory":
            continue
        name = _validate_obs_profile_dir_name(entry.name)
        profile_dir = layout.profiles_root / name
        if not preflight_obs_config_directory(profile_dir):
            raise OBSPathSafetyError(f"OBS profile directoryが再検査中に消失しました: {profile_dir}")
        preflight_obs_config_file(profile_dir / "basic.ini", label="basic.ini")
    for profile_dir in layout.profile_directories:
        preflight_obs_config_directory(profile_dir)


def _apply_obs_recording_profile_update(plan: _OBSRecordingProfileUpdatePlan) -> tuple[Path, ...]:
    with obs_config_mutation_guard(plan.layout.base_dir):
        _revalidate_obs_recording_profile_layout(plan.layout)
        for update in plan.updates:
            revalidate_obs_config_file(update.snapshot)

        changed_updates = tuple(update for update in plan.updates if update.changed)
        for directory in {update.snapshot.path.parent for update in changed_updates}:
            ensure_safe_obs_config_directory(directory)

        _revalidate_obs_recording_profile_layout(plan.layout)
        for update in plan.updates:
            revalidate_obs_config_file(update.snapshot)
        for update in changed_updates:
            if update.payload is None:
                continue
            write_preflighted_obs_config_file(update.snapshot, update.payload)

    return tuple(lexical_absolute_path(update.snapshot.path) for update in changed_updates)


def ensure_obs_recording_profile_ini(
    base_dir: str | Path,
    *,
    record_dir: str | Path,
    scale_type: str = DEFAULT_OBS_SCALE_TYPE,
    recording_quality: str = DEFAULT_OBS_RECORDING_QUALITY,
    recording_encoder: str = DEFAULT_OBS_RECORDING_ENCODER,
    selected_encoder: OBSRecordingEncoderSelection | None = None,
) -> tuple[Path, ...]:
    layout = preflight_obs_recording_profile_ini(base_dir)
    plan = _prepare_obs_recording_profile_update(
        layout,
        record_dir=record_dir,
        scale_type=scale_type,
        recording_quality=recording_quality,
        recording_encoder=recording_encoder,
        selected_encoder=selected_encoder,
    )
    return _apply_obs_recording_profile_update(plan)


def _obs_encoder_log_label(selected_encoder: OBSRecordingEncoderSelection) -> str:
    return f"{selected_encoder.display_name} ({selected_encoder.encoder_kind})"


def _wait_for_obs_startup_encoder_selection(
    process_manager: OBSProcessManager,
    *,
    since: float,
    timeout_sec: float = DEFAULT_OBS_ENCODER_LOG_WAIT_SEC,
) -> OBSRecordingEncoderSelection | None:
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while True:
        try:
            encoder_kinds = process_manager.latest_log_encoder_kinds(since=since)
        except Exception as e:
            LOGGER.warning("OBS起動後ログから録画エンコーダを検出できませんでした: %s", e)
            return None
        if encoder_kinds:
            return select_obs_recording_encoder(encoder_kinds)
        if time.monotonic() >= deadline:
            return None
        time.sleep(DEFAULT_OBS_ENCODER_LOG_POLL_SEC)


def _start_hidden_obs_and_verify_portable(
    process_manager: OBSProcessManager,
    *,
    obs_dir_abs: str,
    obs_exe: str,
) -> tuple[subprocess.Popen[Any], float]:
    LOGGER.info("🚀 OBSを起動しています (バックグラウンド/非表示)...")
    started_at = time.time()
    process = process_manager.start_obs(env=process_manager.isolated_env(), hidden=True)
    hidden_windows = process_manager.hide_main_windows(process, timeout_sec=3.0)
    if hidden_windows:
        LOGGER.info("OBSウィンドウを非表示にしました: pid=%s windows=%s", process.pid, hidden_windows)
    # WebSocketとOBS起動ログの出力待ち。
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
    return process, started_at


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
    if "desktop" in audio_cfg:
        audio_cfg.pop("desktop")
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

    normalization = config_schema.normalize_config(
        data,
        auto_fix=auto_fix,
        password_factory=generate_obs_password,
    )
    if normalization.changed:
        report["changed"] = True
    report["notes"].extend(normalization.notes)
    report["warnings"].extend(normalization.warnings)
    report["errors"].extend(normalization.errors)

    obs_cfg = data["obs"]
    paths_cfg = data["paths"]
    port, _ = _safe_int(obs_cfg.get("port"), DEFAULT_OBS_PORT, minimum=1, maximum=65535)

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
    migration_failed = False
    if auto_fix:
        try:
            migrated_from = migrate_legacy_managed_obs_if_needed(port=port, password=obs_password)
            if migrated_from is not None:
                report["changed"] = True
                report["notes"].append(f"旧OBS配置を obs-portable へコピー移行しました: {migrated_from}")
        except Exception as e:
            migration_failed = True
            report["warnings"].append(f"旧OBS配置の移行に失敗しました: {e}")

        if not migration_failed:
            try:
                repaired_legacy = repair_legacy_managed_obs_if_present(port=port, password=obs_password)
                if repaired_legacy is not None:
                    report["changed"] = True
                    report["notes"].append(f"旧OBS配置の起動前設定も修復しました: {repaired_legacy}")
            except Exception as e:
                report["warnings"].append(f"旧OBS配置の設定修復に失敗しました: {e}")

    current_obs_dir = resolve_obs_path(obs_cfg.get("dir", DEFAULT_OBS_DIR), DATA_DIR)
    expected_obs_dir = MANAGED_PORTABLE_OBS_DIR

    if not current_obs_dir or not is_managed_portable_obs_dir(current_obs_dir):
        if auto_fix:
            obs_cfg["dir"] = DEFAULT_OBS_DIR
            report["changed"] = True
            report["notes"].append(f"OBSフォルダをアプリ管理用に固定しました: {DEFAULT_OBS_DIR}")
            current_obs_dir = expected_obs_dir
        else:
            report["errors"].append(f"OBSフォルダは obs-portable のポータブルOBSのみ対応です: {expected_obs_dir}")

    bootstrap_layout_safe = False
    if current_obs_dir and is_managed_portable_obs_dir(current_obs_dir):
        bootstrap_blocked_reason = None
        if migration_failed:
            bootstrap_blocked_reason = "OBSコピー移行が失敗したため、コピー元とコピー先の設定修復を延期しました。"
        elif os.path.lexists(current_obs_dir):
            try:
                if is_obs_copy_in_progress(current_obs_dir):
                    bootstrap_blocked_reason = (
                        "OBSコピー移行が進行中または再開待ちのため、コピー先の設定修復を延期しました。"
                    )
            except Exception as e:
                bootstrap_blocked_reason = f"OBSコピー移行状態を安全に確認できないため、設定修復を延期しました: {e}"

        if bootstrap_blocked_reason is not None:
            report["warnings"].append(bootstrap_blocked_reason)
        else:
            try:
                bootstrapper = OBSBootstrapper(current_obs_dir)
                bootstrap_report = bootstrapper.check()
                # Nested profile targets belong to the same setup transaction.
                # Validate them before bootstrap.apply can change global/user.ini.
                preflight_obs_recording_profile_ini(current_obs_dir)
                bootstrap_layout_safe = True
                if not bootstrap_report.obs_exe_exists:
                    report["warnings"].append("OBS本体が未配置のため、起動前設定の自動生成を延期しました。")
                elif bootstrap_report.needs_repair:
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
                bootstrap_layout_safe = False
                report["warnings"].append(f"OBS Bootstrapper の検査/修復に失敗しました: {e}")

            if (
                auto_fix
                and bootstrap_layout_safe
                and recordings_dir is not None
                and is_valid_obs_dir(current_obs_dir)
            ):
                try:
                    process_manager = OBSProcessManager(current_obs_dir, logger=LOGGER)
                    if process_manager.has_managed_process():
                        report["warnings"].append(
                            "ポータブルOBSが起動中のため、録画プロファイル修復を延期しました。"
                            "録画監視を停止してから環境修復を再実行してください。"
                        )
                    else:
                        changed_profiles = ensure_obs_recording_profile_ini(
                            current_obs_dir,
                            record_dir=recordings_dir,
                            scale_type=str(obs_cfg.get("scale_type") or DEFAULT_OBS_SCALE_TYPE),
                            recording_quality=str(obs_cfg.get("recording_quality") or DEFAULT_OBS_RECORDING_QUALITY),
                            recording_encoder=str(obs_cfg.get("recording_encoder") or DEFAULT_OBS_RECORDING_ENCODER),
                        )
                        if changed_profiles:
                            report["changed"] = True
                            report["notes"].append(
                                "OBS録画プロファイルをSimple/H.264/mkv設定へ修復しました: "
                                + ", ".join(str(path) for path in changed_profiles)
                            )
                except Exception as e:
                    report["warnings"].append(f"OBS録画プロファイルの検査/修復に失敗しました: {e}")

    has_valid_obs = bool(current_obs_dir and bootstrap_layout_safe and is_valid_obs_dir(current_obs_dir))
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
    if key != "mic":
        raise KeyError(key)
    if isinstance(cfg, AppConfig):
        return cfg.audio.mic.to_dict()

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


def disable_obs_global_audio_devices(client: Any) -> None:
    for parameter_name in OBS_GLOBAL_AUDIO_DEVICE_PARAMETERS:
        try:
            _obs_raw(
                client,
                "SetProfileParameter",
                {
                    "parameterCategory": "Audio",
                    "parameterName": parameter_name,
                    "parameterValue": "disabled",
                },
            )
        except Exception as e:
            raise RecorderError(f"OBSグローバル音声デバイス '{parameter_name}' の無効化に失敗しました: {e}") from e

    try:
        special_inputs = _obs_raw(client, "GetSpecialInputs")
    except Exception as e:
        raise RecorderError(f"OBSグローバル音声入力の確認に失敗しました: {e}") from e

    if not isinstance(special_inputs, dict):
        return

    input_names = {
        str(value).strip()
        for value in special_inputs.values()
        if isinstance(value, str) and str(value).strip()
    }
    for input_name in input_names:
        muted = False
        disabled = False
        try:
            client.set_input_mute(input_name, True)
            muted = True
        except Exception:
            pass
        try:
            client.set_input_settings(input_name, {"device_id": "disabled"}, overlay=True)
            disabled = True
        except Exception:
            pass
        if not muted and not disabled:
            raise RecorderError(f"OBSグローバル音声入力 '{input_name}' を停止できませんでした。")


def apply_obs_video_settings(
    client: Any,
    fps_numerator: int | str | None = None,
    *,
    fps_denominator: int | str | None = None,
    base_width: int | str | None = None,
    base_height: int | str | None = None,
    output_width: int | str | None = None,
    output_height: int | str | None = None,
) -> Any:
    fps_num, _ = _safe_int(
        fps_numerator,
        DEFAULT_OBS_FPS_NUMERATOR,
        minimum=1,
        maximum=MAX_OBS_FPS_NUMERATOR,
    )
    fps_den, _ = _safe_int(
        fps_denominator,
        DEFAULT_OBS_FPS_DENOMINATOR,
        minimum=1,
        maximum=MAX_OBS_FPS_DENOMINATOR,
    )
    base_width_value, _ = _safe_int(base_width, DEFAULT_OBS_BASE_WIDTH, minimum=64, maximum=4096)
    base_height_value, _ = _safe_int(base_height, DEFAULT_OBS_BASE_HEIGHT, minimum=64, maximum=4096)
    output_width_value, _ = _safe_int(output_width, DEFAULT_OBS_OUTPUT_WIDTH, minimum=64, maximum=4096)
    output_height_value, _ = _safe_int(output_height, DEFAULT_OBS_OUTPUT_HEIGHT, minimum=64, maximum=4096)
    return _obs_raw(
        client,
        "SetVideoSettings",
        {
            "fpsNumerator": int(fps_num),
            "fpsDenominator": int(fps_den),
            "baseWidth": int(base_width_value),
            "baseHeight": int(base_height_value),
            "outputWidth": int(output_width_value),
            "outputHeight": int(output_height_value),
        },
    )


def _is_h264_encoder_kind(kind: str) -> bool:
    normalized = str(kind).strip().lower()
    return not any(codec in normalized for codec in ("hevc", "av1", "h265"))


def select_obs_recording_encoder(encoder_kinds: list[str] | tuple[str, ...]) -> OBSRecordingEncoderSelection:
    normalized = [(str(kind).strip(), str(kind).strip().lower()) for kind in encoder_kinds if str(kind).strip()]
    candidates = (
        ("nvenc", "NVIDIA NVENC H.264", True, lambda value: "nvenc" in value),
        ("qsv", "Intel Quick Sync H.264", True, lambda value: "qsv" in value or "quicksync" in value),
        ("amd", "AMD AMF H.264", True, lambda value: "amf" in value or "amd" in value),
        ("x264", "x264", False, lambda value: "x264" in value),
    )
    for profile_value, display_name, hardware, matches in candidates:
        for original, value in normalized:
            if _is_h264_encoder_kind(value) and matches(value):
                return OBSRecordingEncoderSelection(profile_value, original, display_name, hardware)
    return OBSRecordingEncoderSelection("x264", "obs_x264", "x264 (fallback)", False)


def detect_obs_recording_encoder(obs_dir: str | Path | None) -> OBSRecordingEncoderSelection:
    if obs_dir:
        try:
            process_manager = OBSProcessManager(obs_dir, logger=LOGGER)
            latest_log_encoder_kinds = getattr(process_manager, "latest_log_encoder_kinds", None)
            encoder_kinds = latest_log_encoder_kinds() if callable(latest_log_encoder_kinds) else []
            if encoder_kinds:
                return select_obs_recording_encoder(encoder_kinds)
        except Exception as e:
            LOGGER.warning("OBSログから録画エンコーダを検出できないためx264を使用します: %s", e)
    return OBSRecordingEncoderSelection("x264", "obs_x264", "x264 (fallback)", False)


def _normalized_obs_scale_type(scale_type: str | None) -> str:
    scale_value = str(scale_type or DEFAULT_OBS_SCALE_TYPE).strip().lower()
    if scale_value not in VALID_OBS_SCALE_TYPES:
        return DEFAULT_OBS_SCALE_TYPE
    return scale_value


def _normalized_obs_recording_quality(recording_quality: str | None) -> str:
    quality_lookup = {value.lower(): value for value in VALID_OBS_RECORDING_QUALITIES}
    return quality_lookup.get(
        str(recording_quality or DEFAULT_OBS_RECORDING_QUALITY).strip().lower(),
        DEFAULT_OBS_RECORDING_QUALITY,
    )


def _obs_advanced_encoder_value(selected_encoder: OBSRecordingEncoderSelection) -> str:
    encoder_kind = str(selected_encoder.encoder_kind or "").strip()
    normalized_kind = encoder_kind.lower()
    if normalized_kind.startswith(("obs_", "ffmpeg_")):
        return encoder_kind
    if selected_encoder.profile_value == "x264":
        return "obs_x264"
    if selected_encoder.profile_value == "nvenc":
        return "obs_nvenc_h264_tex"
    if selected_encoder.profile_value == "qsv":
        return "obs_qsv11_v2"
    if selected_encoder.profile_value == "amd":
        return "h264_texture_amf"
    return str(selected_encoder.profile_value or "obs_x264")


def _obs_recording_profile_parameter_updates(
    *,
    scale_type: str = DEFAULT_OBS_SCALE_TYPE,
    recording_quality: str = DEFAULT_OBS_RECORDING_QUALITY,
    selected_encoder: OBSRecordingEncoderSelection,
    record_dir: str | Path | None = None,
) -> list[tuple[str, str, str]]:
    scale_value = _normalized_obs_scale_type(scale_type)
    quality_value = _normalized_obs_recording_quality(recording_quality)
    advanced_encoder = _obs_advanced_encoder_value(selected_encoder)
    updates: list[tuple[str, str, str]] = [
        ("Output", "Mode", DEFAULT_OBS_OUTPUT_MODE),
        ("Video", "ScaleType", scale_value),
        ("AdvOut", "RecType", "Standard"),
        ("AdvOut", "RecFormat2", DEFAULT_OBS_RECORDING_FORMAT),
        ("AdvOut", "RecUseRescale", "false"),
        ("AdvOut", "RecTracks", DEFAULT_OBS_RECORDING_TRACKS),
        ("AdvOut", "RecAudioEncoder", DEFAULT_OBS_ADVANCED_AUDIO_ENCODER),
        ("AdvOut", "Encoder", advanced_encoder),
        ("AdvOut", "RecEncoder", advanced_encoder),
        ("SimpleOutput", "RecFormat2", DEFAULT_OBS_RECORDING_FORMAT),
        ("SimpleOutput", "UseAdvanced", "false"),
        ("SimpleOutput", "RecTracks", DEFAULT_OBS_RECORDING_TRACKS),
        ("SimpleOutput", "RecRB", "false"),
        ("SimpleOutput", "StreamEncoder", "x264"),
        ("SimpleOutput", "StreamAudioEncoder", DEFAULT_OBS_SIMPLE_AUDIO_ENCODER),
        ("SimpleOutput", "RecAudioEncoder", DEFAULT_OBS_SIMPLE_AUDIO_ENCODER),
        ("SimpleOutput", "Preset", DEFAULT_OBS_SIMPLE_X264_PRESET),
        ("SimpleOutput", "RecQuality", quality_value),
    ]
    if record_dir:
        record_path = str(Path(record_dir))
        updates.extend(
            [
                ("SimpleOutput", "FilePath", record_path),
                ("AdvOut", "RecFilePath", record_path),
                ("AdvOut", "FFFilePath", record_path),
            ]
        )
    updates.append(("SimpleOutput", "RecEncoder", selected_encoder.profile_value))
    return updates


def _set_obs_profile_parameter(client: Any, category: str, name: str, value: str) -> None:
    _obs_raw(
        client,
        "SetProfileParameter",
        {
            "parameterCategory": category,
            "parameterName": name,
            "parameterValue": value,
        },
    )


def get_obs_profile_parameter_value(client: Any, category: str, name: str) -> str | None:
    try:
        response = _obs_raw(
            client,
            "GetProfileParameter",
            {
                "parameterCategory": category,
                "parameterName": name,
            },
        )
    except Exception:
        return None
    if isinstance(response, dict):
        for key in ("parameterValue", "parameter_value", "value"):
            value = response.get(key)
            if value is not None:
                return str(value)
    for attr in ("parameter_value", "parameterValue", "value"):
        value = getattr(response, attr, None)
        if value is not None:
            return str(value)
    return None


def _raise_for_obs_request_status(response: Any, request_type: str) -> None:
    if not isinstance(response, dict):
        return
    status = response.get("requestStatus")
    if not isinstance(status, dict) or status.get("result") is not False:
        return
    code = status.get("code", "?")
    comment = status.get("comment") or status.get("message") or response
    raise RecorderError(f"OBS request failed: request={request_type}, code={code}, detail={comment}")


def apply_obs_recording_quality_settings(
    client: Any,
    *,
    scale_type: str = DEFAULT_OBS_SCALE_TYPE,
    recording_quality: str = DEFAULT_OBS_RECORDING_QUALITY,
    recording_encoder: str = DEFAULT_OBS_RECORDING_ENCODER,
    obs_dir: str | Path | None = None,
    record_dir: str | Path | None = None,
) -> OBSRecordingEncoderSelection:
    selected_encoder = _select_requested_obs_recording_encoder(recording_encoder, obs_dir=obs_dir)

    for category, name, value in _obs_recording_profile_parameter_updates(
        scale_type=scale_type,
        recording_quality=recording_quality,
        selected_encoder=selected_encoder,
        record_dir=record_dir,
    ):
        _set_obs_profile_parameter(client, category, name, value)
    return selected_encoder


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


def validate_recording_directory(record_dir: str | Path) -> Path:
    target = Path(record_dir).resolve()
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / f".lol-replay-write-{secrets.token_hex(6)}.tmp"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
    except Exception as e:
        raise RecorderError(f"録画保存ディレクトリへ書き込めません。\n対象: {target}\n詳細: {e}") from e

    try:
        free_bytes = shutil.disk_usage(target).free
    except Exception:
        free_bytes = None
    if free_bytes is not None and free_bytes < MIN_RECORDING_FREE_SPACE_BYTES:
        free_mb = free_bytes / (1024 * 1024)
        raise RecorderError(
            "録画保存先の空き容量が不足しています。\n"
            f"対象: {target}\n"
            f"空き容量: {free_mb:.1f} MB"
        )
    return target


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
    for key in MANAGED_AUDIO_INPUTS:
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
    mic_cfg = _get_audio_slot_config(cfg or {}, "mic")
    return {"mic": list_audio_devices_for_input(client, mic_cfg["input_name"])}


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

    for key in MANAGED_AUDIO_INPUTS:
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
GLOBAL_OBJECTIVES = GLOBAL_OBJECTIVE_EVENT_NAMES

# ▼ 自分が関与しているかチェックするイベント
COMBAT_EVENTS = COMBAT_EVENT_NAMES

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


def resolve_obs_path(value: str | Path | None, base_dir: str | Path) -> Path | None:
    if value is None:
        return None
    expanded = Path(os.path.expandvars(str(value)))
    if not expanded.is_absolute():
        expanded = Path(base_dir) / expanded
    return lexical_absolute_path(expanded)


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
    return _storage_policy.parse_max_storage_bytes(storage_cfg, DEFAULT_MAX_STORAGE_GB)


is_within = _storage_policy.is_within
get_dir_size = _storage_policy.get_dir_size
parse_saved_at = _storage_policy.parse_saved_at


def total_storage_size(config: AppConfig | None = None) -> int:
    return _storage_policy.total_storage_size(config or load_app_config())


def load_json_metadata(path: str | Path, config: AppConfig | None = None) -> tuple[float | None, Path | None]:
    return _storage_policy.load_json_metadata(path, config or load_app_config())


def is_app_owned_video_path(path: str | Path | None, config: AppConfig) -> bool:
    return _storage_policy.is_app_owned_video_path(path, config)


def find_app_owned_clip_paths(video_path: str | Path | None, config: AppConfig) -> tuple[Path, ...]:
    return _storage_policy.find_app_owned_clip_paths(video_path, config)


def enforce_storage_limit(config: AppConfig | None = None, keep_paths: list[str | Path] | None = None) -> None:
    _storage_policy.enforce_storage_limit(config or load_app_config(), keep_paths)


def launch_obs(config: AppConfig) -> subprocess.Popen[Any]:
    """OBSをバックグラウンドで起動する"""
    if not config.obs.obs_dir:
        raise RecorderError("OBSのパスが未設定です。設定画面の OBSフォルダ (obs.dir) を指定してください。")

    if is_managed_portable_obs_dir(config.obs.obs_dir):
        try:
            migrate_legacy_managed_obs_if_needed(port=config.obs.port, password=config.obs.password)
            repair_legacy_managed_obs_if_present(port=config.obs.port, password=config.obs.password)
        except (OBSMigrationInProgressError, OBSMigrationRecoveryRequiredError) as e:
            raise RecorderError(str(e)) from e
        except Exception as e:
            LOGGER.warning("旧OBS配置の移行/修復に失敗しました: %s", e, exc_info=True)

    obs_dir_abs = os.path.abspath(str(config.obs.obs_dir))
    obs_exe = os.path.abspath(os.path.join(obs_dir_abs, "bin", "64bit", "obs64.exe"))

    if is_obs_copy_in_progress(obs_dir_abs):
        raise RecorderError(
            "OBSのコピー移行が完了していません。旧配置を保持したまま再検査するか、"
            "現在のobs-portableを別の場所へ退避して空にしてから、"
            "公式ReleaseのWindows x64 ZIPを専用obs-portableへ再展開してください。"
        )

    if not is_valid_obs_dir(obs_dir_abs):
        detected = detect_obs_dir()
        hint = f"\n自動検出候補: {detected}" if detected else ""
        raise RecorderError(f"OBSの実行ファイルが見つかりません。\nパス: {obs_exe}{hint}")

    process_manager = OBSProcessManager(obs_dir_abs, logger=LOGGER)
    bootstrapper = OBSBootstrapper(obs_dir_abs)

    # Every bootstrap/profile target must be safe before a running OBS is stopped.
    try:
        bootstrapper.preflight_apply(port=config.obs.port)
        preflight_obs_recording_profile_ini(obs_dir_abs)
    except Exception as e:
        raise RecorderError(f"ポータブルOBS起動前設定の安全性検査に失敗しました: {e}") from e

    # OBSは起動時にglobal.iniを読み、終了時に再保存する。
    # 管理対象OBSが既に動いている場合は、設定反映前に必ず止める。
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

    try:
        record_dir = validate_recording_directory(config.paths.recordings_dir)
        startup_encoder = _select_requested_obs_recording_encoder(config.obs.recording_encoder, obs_dir=obs_dir_abs)
        changed_profiles = ensure_obs_recording_profile_ini(
            obs_dir_abs,
            record_dir=record_dir,
            scale_type=config.obs.scale_type,
            recording_quality=config.obs.recording_quality,
            recording_encoder=config.obs.recording_encoder,
            selected_encoder=startup_encoder,
        )
        if changed_profiles:
            LOGGER.info("ℹ️ OBS録画プロファイルを修復しました: %s", ", ".join(str(path) for path in changed_profiles))
        LOGGER.info("OBS起動時録画エンコーダ: %s", _obs_encoder_log_label(startup_encoder))
    except Exception as e:
        raise RecorderError(f"OBS録画プロファイルの起動前修復に失敗しました: {e}") from e

    ensure_obs_websocket_port_free(config)

    try:
        process, started_at = _start_hidden_obs_and_verify_portable(
            process_manager,
            obs_dir_abs=obs_dir_abs,
            obs_exe=obs_exe,
        )
        requested_encoder = str(config.obs.recording_encoder or DEFAULT_OBS_RECORDING_ENCODER).strip().lower()
        if requested_encoder == "auto" and not startup_encoder.hardware:
            detected_encoder = _wait_for_obs_startup_encoder_selection(
                process_manager,
                since=started_at - 1.0,
            )
            if detected_encoder is None:
                LOGGER.warning("OBS起動後ログからGPUエンコーダを確認できないためx264で録画します。")
            elif detected_encoder.hardware:
                LOGGER.info(
                    "OBS起動後にGPUエンコーダを検出したため、録画設定反映のためOBSを再起動します。"
                )
                try:
                    preflight_obs_recording_profile_ini(obs_dir_abs)
                except Exception as e:
                    raise RecorderError(
                        f"OBS録画プロファイルの再起動前安全性検査に失敗しました: {e}"
                    ) from e
                process_manager.terminate_process(process)
                changed_profiles = ensure_obs_recording_profile_ini(
                    obs_dir_abs,
                    record_dir=record_dir,
                    scale_type=config.obs.scale_type,
                    recording_quality=config.obs.recording_quality,
                    recording_encoder=config.obs.recording_encoder,
                    selected_encoder=detected_encoder,
                )
                if changed_profiles:
                    LOGGER.info(
                        "ℹ️ OBS録画プロファイルをGPUエンコーダへ更新しました: %s",
                        ", ".join(str(path) for path in changed_profiles),
                    )
                LOGGER.info("OBS起動時録画エンコーダ: %s", _obs_encoder_log_label(detected_encoder))
                process, _started_at = _start_hidden_obs_and_verify_portable(
                    process_manager,
                    obs_dir_abs=obs_dir_abs,
                    obs_exe=obs_exe,
                )
            else:
                LOGGER.info("OBS起動後にGPUエンコーダが見つからないためx264で録画します。")
        return process
    except RecorderError:
        raise
    except Exception as e:
        raise RecorderError(f"OBS起動エラー: {e}") from e


def ensure_portable_mode_marker(base_dir: str | Path) -> Path:
    if not base_dir:
        raise RecorderError("OBSディレクトリが未設定です。")
    return SharedOBSBootstrapper(base_dir, logger=LOGGER).ensure_portable_mode_marker()


def kill_stale_obs_processes() -> None:
    """
    アプリ管理OBSだけを起動直前に終了する。
    通常版OBSやユーザーが別用途で起動したOBSは対象外にする。
    """
    OBSProcessManager(MANAGED_PORTABLE_OBS_DIR, logger=LOGGER).kill_stale_managed_processes()


def _first_mapping_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


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
        self._apply_record_output_basics()

        try:
            apply_obs_video_settings(
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
        except Exception as e:
            # 録画は続行可能なので警告のみ
            self.log(f"⚠️ OBS映像設定の適用に失敗: {e}")

        self._apply_recording_quality_settings()

    def _apply_record_output_basics(self) -> None:
        disable_obs_global_audio_devices(self.client)
        self.log("🔇 OBSのデスクトップ音声とグローバル音声入力を無効化しました。")

        if self.config.paths.recordings_dir:
            record_dir = validate_recording_directory(self.config.paths.recordings_dir)
            apply_record_directory_to_obs(self.client, record_dir)

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
                record_dir = validate_recording_directory(self.config.paths.recordings_dir)
            selected_encoder = apply_obs_recording_quality_settings(
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
                f"format={DEFAULT_OBS_RECORDING_FORMAT}, mode={DEFAULT_OBS_OUTPUT_MODE}"
            )
            return selected_encoder
        except Exception as e:
            self.log(f"⚠️ OBS録画品質設定の適用に失敗: {e}")
            if raise_on_error:
                raise
            return None

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
            self._fit_window_capture_to_canvas(window_capture_item_id)
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
            "capture_audio": DEFAULT_OBS_WINDOW_CAPTURE_AUDIO,
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
                    "capture_audio": DEFAULT_OBS_WINDOW_CAPTURE_AUDIO,
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
                fallback_settings = {
                    "window": self.config.obs.window_capture_window,
                    "method": self.config.obs.window_capture_method,
                    "priority": DEFAULT_OBS_WINDOW_CAPTURE_PRIORITY,
                    "capture_audio": DEFAULT_OBS_WINDOW_CAPTURE_AUDIO,
                }
                try:
                    self.client.set_input_settings(source_name, fallback_settings, overlay=True)
                except Exception as fallback_error:
                    raise RecorderError(
                        f"ウィンドウキャプチャ '{source_name}' の設定更新に失敗しました: {fallback_error}"
                    ) from fallback_error

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

    def _fit_window_capture_to_canvas(self, scene_item_id: int) -> None:
        scene_name = self.config.obs.scene_name
        width = float(self.config.obs.base_width)
        height = float(self.config.obs.base_height)
        transform = {
            "positionX": 0.0,
            "positionY": 0.0,
            "alignment": 5,
            "boundsType": "OBS_BOUNDS_SCALE_INNER",
            "boundsAlignment": 0,
            "boundsWidth": width,
            "boundsHeight": height,
        }
        try:
            self.client.set_scene_item_transform(scene_name, scene_item_id, transform)
        except Exception as e:
            self.log(f"⚠️ ウィンドウキャプチャをキャンバスへフィットできませんでした: {e}")

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
            raise RecorderError("OBS録画エンコーダの切り替えに失敗しました。")
        self.log(
            "🎞️ OBS録画エンコーダを切り替えました: "
            f"encoder={selected_encoder.display_name} ({selected_encoder.encoder_kind})"
        )
        return selected_encoder

    def stop_recording(self) -> str | None:
        res = self.client.stop_record()
        return getattr(res, "output_path", None)

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
            value = get_obs_profile_parameter_value(self.client, category, name)
            if value is not None:
                details[f"{category}.{name}"] = value

        try:
            profile_list = _obs_raw(self.client, "GetProfileList")
            details["OBS.current_profile"] = _obs_response_value(
                profile_list,
                "currentProfileName",
                "current_profile_name",
            )
        except Exception as e:
            details["OBS.current_profile_error"] = f"{type(e).__name__}: {e}"

        try:
            scene_collections = _obs_raw(self.client, "GetSceneCollectionList")
            details["OBS.current_scene_collection"] = _obs_response_value(
                scene_collections,
                "currentSceneCollectionName",
                "current_scene_collection_name",
            )
        except Exception as e:
            details["OBS.current_scene_collection_error"] = f"{type(e).__name__}: {e}"

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
        except Exception as e:
            details["OBS.outputs_error"] = f"{type(e).__name__}: {e}"

        try:
            simple_status = _obs_raw(self.client, "GetOutputStatus", {"outputName": "simple_file_output"})
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
        except Exception as e:
            details["simple_file_output.status_error"] = f"{type(e).__name__}: {e}"

        try:
            simple_settings = _obs_raw(self.client, "GetOutputSettings", {"outputName": "simple_file_output"})
            output_settings = _obs_response_value(simple_settings, "outputSettings", "output_settings")
            if isinstance(output_settings, dict):
                for key in ("path", "muxer_settings"):
                    if output_settings.get(key) not in (None, ""):
                        details[f"simple_file_output.{key}"] = output_settings.get(key)
        except Exception as e:
            details["simple_file_output.settings_error"] = f"{type(e).__name__}: {e}"
        return details

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
        game_process_checker: Callable[[], bool | None] | None = None,
        gameflow_inactive_grace_sec: float = DEFAULT_GAMEFLOW_INACTIVE_GRACE_SEC,
        game_process_missing_grace_sec: float = DEFAULT_GAME_PROCESS_MISSING_GRACE_SEC,
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
        self.game_process_checker = game_process_checker or is_lol_game_process_running
        self.gameflow_inactive_grace_sec = max(0.0, float(gameflow_inactive_grace_sec))
        self.game_process_missing_grace_sec = max(0.0, float(game_process_missing_grace_sec))
        self.champion_catalog: dict[int, str] = {}
        self._require_game_clear = False
        self._last_completed_game_id: str | None = None
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
        self.match_metadata: dict[str, Any] = {}
        self.game_start_detection_source: str | None = None
        self.game_start_anchor_game_time: float | None = None
        self.game_start_anchor_monotonic: float | None = None
        self.sync_time_source: str | None = None
        self.champ_select_tracker = ChampSelectTracker()
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
            or self.champ_select_tracker.has_data
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

    async def capture_champ_select_async(self) -> None:
        get_result = getattr(self.riot_api_client, "get_champ_select_session_result", None)
        if not callable(get_result):
            return
        try:
            result = get_result()
            if hasattr(result, "__await__"):
                result = await result
        except Exception as e:
            self.logger.debug("Champion select poll failed: %s", e)
            return
        if not isinstance(result, RiotPollResult):
            return
        if result.status == RiotPollStatus.NOT_IN_GAME:
            self.champ_select_tracker.observe_inactive()
            return
        if result.status != RiotPollStatus.IN_GAME or not isinstance(result.payload, dict):
            return

        if not self.champion_catalog:
            get_catalog = getattr(self.riot_api_client, "get_champion_catalog", None)
            if callable(get_catalog):
                try:
                    catalog = get_catalog()
                    if hasattr(catalog, "__await__"):
                        catalog = await catalog
                    if isinstance(catalog, dict):
                        self.champion_catalog = {
                            int(champion_id): str(name)
                            for champion_id, name in catalog.items()
                            if name
                        }
                except Exception as e:
                    self.logger.debug("Champion catalog fetch failed: %s", e)

        added = self.champ_select_tracker.observe(result.payload, self.champion_catalog)
        for action in added:
            champion = action.get("champion_name") or f"ID {action.get('champion_id')}"
            self.log(
                "Ban/Pick記録: "
                f"{action.get('team')} {action.get('type')} {champion} "
                f"(phase {action.get('phase_order')})"
            )

    async def capture_match_metadata_async(self) -> None:
        get_metadata = getattr(self.riot_api_client, "get_match_metadata", None)
        if not callable(get_metadata):
            return
        try:
            metadata = get_metadata()
            if hasattr(metadata, "__await__"):
                metadata = await metadata
        except Exception as e:
            self.logger.debug("Match metadata poll failed: %s", e)
            return
        if not isinstance(metadata, dict) or not metadata:
            return

        previous_name = self.match_metadata.get("display_name")
        self.match_metadata.update(metadata)
        current_name = self.match_metadata.get("display_name")
        if current_name and current_name != previous_name:
            self.log(f"マッチ種類を検出: {current_name}")

    @staticmethod
    def _normalize_gameflow_phase(value: Any) -> str | None:
        if value in (None, ""):
            return None
        normalized = re.sub(r"[\s_-]+", "", str(value)).casefold()
        return normalized or None

    async def poll_gameflow_phase(self) -> RiotPollResult:
        get_phase = getattr(self.riot_api_client, "get_gameflow_phase_result", None)
        if not callable(get_phase):
            return RiotPollResult(RiotPollStatus.NOT_IN_GAME)
        try:
            result = get_phase()
            if hasattr(result, "__await__"):
                result = await result
        except Exception as e:
            return RiotPollResult(RiotPollStatus.TEMPORARY_FAILURE, error=str(e))
        if not isinstance(result, RiotPollResult):
            return RiotPollResult(
                RiotPollStatus.TEMPORARY_FAILURE,
                error="Unexpected gameflow phase result",
            )
        if result.status == RiotPollStatus.IN_GAME and isinstance(result.payload, dict):
            phase = _first_mapping_value(result.payload, "phase", "value")
            if phase not in (None, ""):
                self.match_metadata["gameflow_phase"] = str(phase)
        return result

    async def is_lol_game_process_running_async(self) -> bool | None:
        try:
            result = await asyncio.to_thread(self.game_process_checker)
            if hasattr(result, "__await__"):
                result = await result
        except Exception as e:
            self.logger.debug("LoL game process check failed: %s", e)
            return None
        if result is None:
            return None
        return bool(result)

    def _gameflow_phase_from_poll_result(self, result: RiotPollResult) -> tuple[str | None, bool]:
        if result.status != RiotPollStatus.IN_GAME or not isinstance(result.payload, dict):
            return None, False
        phase = _first_mapping_value(result.payload, "phase", "value")
        if phase in (None, ""):
            return None, True
        return str(phase), True

    async def _observe_recording_context_end_async(
        self,
        end_detector: RecordingEndDetector,
        live_result: RiotPollResult,
        now: float,
    ) -> RecordingEndDecision:
        gameflow_result = await self.poll_gameflow_phase()
        gameflow_phase, has_gameflow_phase = self._gameflow_phase_from_poll_result(gameflow_result)
        if has_gameflow_phase:
            normalized_phase = self._normalize_gameflow_phase(gameflow_phase)
            gameflow_decision = end_detector.observe_gameflow_phase(
                normalized_phase,
                now,
                active_phases=LCU_GAMEFLOW_START_PHASES,
                detail=gameflow_phase or "None",
            )
            if gameflow_decision.should_end:
                return gameflow_decision

        if live_result.status == RiotPollStatus.IN_GAME:
            return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)

        process_running = await self.is_lol_game_process_running_async()
        process_decision = end_detector.observe_game_process_running(process_running, now)
        if process_decision.should_end:
            return process_decision
        return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)

    def _recording_end_message(self, decision: RecordingEndDecision) -> str:
        if decision.reason == RecordingEndReason.GAME_END_EVENT:
            return "🏁 GameEndイベントを検知。録画を停止します。"
        if decision.reason == RecordingEndReason.NOT_IN_GAME_CONFIRMED:
            return "🏁 Live Clientが試合外になったため録画を停止します。"
        if decision.reason == RecordingEndReason.TEMPORARY_FAILURE_TIMEOUT:
            return "🏁 Live Clientの応答が長時間復旧しないため録画を停止します。"
        if decision.reason == RecordingEndReason.GAMEFLOW_INACTIVE_CONFIRMED:
            phase = decision.detail or "None"
            return f"🏁 LCU Gameflowが{phase}になったため録画を停止します。"
        if decision.reason == RecordingEndReason.GAME_PROCESS_MISSING_CONFIRMED:
            return "🏁 LoLゲームプロセスが終了したため録画を停止します。"
        return "🏁 試合終了検知。録画を停止します。"

    def _complete_recording_end(self, decision: RecordingEndDecision) -> RecordingOutcome:
        if decision.reason == RecordingEndReason.GAME_END_EVENT:
            self.remember_completed_game_id()
            self._require_game_clear = True
        else:
            self._require_game_clear = False
        self.match_metadata["recording_end_reason"] = decision.reason.value
        if decision.detail:
            self.match_metadata["recording_end_detail"] = decision.detail
        self.log(self._recording_end_message(decision))
        self.session_phase = RecordingPhase.FINALIZING
        return RecordingOutcome.COMPLETED

    async def _complete_recording_end_async(self, decision: RecordingEndDecision) -> RecordingOutcome:
        await self.ensure_post_game_result_async(decision)
        return self._complete_recording_end(decision)

    async def wait_for_previous_game_clear_async(self) -> bool:
        if not self._require_game_clear:
            return True

        self.log("前の試合データが終了するまで待機します...")
        temporary_failure_count = 0
        while not self.should_stop():
            result = await self.poll_all_game_data()
            if result.status == RiotPollStatus.NOT_IN_GAME:
                self._require_game_clear = False
                self.log("前の試合データの終了を確認しました。次の試合を監視します。")
                return True
            if result.status == RiotPollStatus.TEMPORARY_FAILURE:
                temporary_failure_count += 1
                if temporary_failure_count >= self.config.polling.end_error_limit:
                    self._require_game_clear = False
                    self.log("LoL試合プロセスの終了を確認しました。次の試合を監視します。")
                    return True
            else:
                temporary_failure_count = 0
                current_game_id = self._game_id_from_live_payload(result.payload)
                if (
                    current_game_id
                    and self._last_completed_game_id
                    and current_game_id != self._last_completed_game_id
                ):
                    self._require_game_clear = False
                    self.log("新しい試合IDを検出しました。次の試合として監視を開始します。")
                    return True
            if not await self.wait_with_stop_async(1.0):
                return False
        return False

    @staticmethod
    def _game_id_from_live_payload(payload: dict[str, Any] | None) -> str | None:
        if not isinstance(payload, dict):
            return None
        game_data = payload.get("gameData")
        if not isinstance(game_data, dict):
            return None
        value = _first_mapping_value(game_data, "gameId", "game_id")
        return str(value) if value not in (None, "") else None

    def remember_completed_game_id(self) -> None:
        game_id = self.match_metadata.get("game_id") or self._game_id_from_live_payload(self.last_game_data)
        if game_id not in (None, ""):
            self._last_completed_game_id = str(game_id)

    def defer_current_game_until_clear(self) -> None:
        game_id = self.match_metadata.get("game_id") or self._game_id_from_live_payload(self.last_game_data)
        if game_id not in (None, ""):
            self._last_completed_game_id = str(game_id)
        self._require_game_clear = True

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
            self.game_result = normalize_game_result_value(result_value) or result_value
            self.winning_team = normalize_lcu_team(winning_team) or winning_team
            if self.game_result or self.winning_team:
                self.match_metadata["result_source"] = "live_client_game_end"
            return

    @staticmethod
    def is_game_end_event(event: dict[str, Any] | None) -> bool:
        return bool(event and event.get("EventName") in {"GameEnd", "EndGame", "GameEnded", "GameComplete"})

    def _apply_post_game_result_payload(self, payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        game_result = normalize_game_result_value(payload.get("game_result") or payload.get("result"))
        winning_team = normalize_lcu_team(payload.get("winning_team") or payload.get("winningTeam"))
        player_team = normalize_lcu_team(payload.get("player_team") or payload.get("playerTeam"))

        if game_result is None and player_team and winning_team:
            game_result = "Win" if player_team == winning_team else "Loss"
        if winning_team is None and player_team and game_result == "Win":
            winning_team = player_team
        if winning_team is None and player_team and game_result == "Loss":
            winning_team = opposing_lcu_team(player_team)

        changed = False
        if not self.game_result and game_result:
            self.game_result = game_result
            changed = True
        if not self.winning_team and winning_team:
            self.winning_team = winning_team
            changed = True
        if not self.player_team and player_team:
            self.player_team = player_team
            changed = True

        if changed:
            source = str(payload.get("source") or "lcu_end_of_game")
            self.match_metadata["result_source"] = source
            self.log(
                "🏁 勝敗を取得しました: "
                f"result={self.game_result or 'Unknown'}, winning_team={self.winning_team or 'Unknown'}"
            )
        return changed

    async def _poll_post_game_result_once_async(self) -> bool:
        get_result = getattr(self.riot_api_client, "get_post_game_result", None)
        if not callable(get_result):
            return False
        try:
            result = get_result(player_name=self.my_name, player_team=self.player_team)
        except TypeError:
            result = get_result(self.my_name, self.player_team)
        if hasattr(result, "__await__"):
            result = await result

        if isinstance(result, PostGameResult):
            return self._apply_post_game_result_payload(result.to_payload())
        if isinstance(result, RiotPollResult):
            if result.status == RiotPollStatus.IN_GAME:
                return self._apply_post_game_result_payload(result.payload)
            return False
        if isinstance(result, dict):
            return self._apply_post_game_result_payload(result)
        return False

    def _post_game_result_wait_seconds(self, decision: RecordingEndDecision) -> float:
        phase = self._normalize_gameflow_phase(decision.detail)
        if phase in LCU_POST_GAME_RESULT_WAIT_PHASES:
            return DEFAULT_POST_GAME_RESULT_WAIT_SEC
        return 0.0

    async def ensure_post_game_result_async(self, decision: RecordingEndDecision) -> None:
        if self.game_result and self.winning_team:
            return
        if await self._poll_post_game_result_once_async():
            return

        wait_seconds = self._post_game_result_wait_seconds(decision)
        if wait_seconds > 0:
            self.log("🏁 試合後の勝敗情報を確認しています...")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + wait_seconds
            while loop.time() < deadline:
                if self.should_stop():
                    return
                if not await self.wait_with_stop_async(DEFAULT_POST_GAME_RESULT_POLL_SEC):
                    return
                if await self._poll_post_game_result_once_async():
                    return

        self.match_metadata.setdefault("result_source", "unavailable")
        self.log("⚠️ 勝敗を取得できませんでした。録画保存を優先します。")

    @staticmethod
    def _live_game_time(payload: dict[str, Any] | None) -> float | None:
        if not isinstance(payload, dict):
            return None
        game_data = payload.get("gameData")
        if not isinstance(game_data, dict):
            return None
        raw_value = _first_mapping_value(game_data, "gameTime", "game_time")
        try:
            game_time = float(raw_value)
        except (TypeError, ValueError):
            return None
        return game_time if game_time >= 0 else None

    async def _mark_game_started(
        self,
        *,
        source: str,
        live_data: dict[str, Any] | None = None,
        game_time: float | None = None,
    ) -> bool:
        loop = asyncio.get_running_loop()
        self.game_start_detection_source = source
        self.match_metadata["game_start_detection_source"] = source
        if source == "live_client":
            anchor_game_time = float(game_time or 0.0)
            self.game_start_anchor_game_time = anchor_game_time
            self.game_start_anchor_monotonic = loop.time()
            self.match_metadata["game_start_game_time"] = anchor_game_time
            self.log(f"🔥 試合開始検知！ Live Client GameTime: {anchor_game_time:.2f}s")
        else:
            phase = self.match_metadata.get("gameflow_phase") or "unknown"
            self.log(f"🔥 試合開始検知！ LCU Gameflow Phase: {phase}")
        self.output_file = build_output_path(self.config)
        await self.try_update_player_name_async()
        if live_data:
            self.match_metadata = merge_live_game_metadata(self.match_metadata, live_data)
        self.session_started = True
        self.session_phase = RecordingPhase.STARTING
        return True

    @staticmethod
    def _game_start_poll_summary(
        live_result: RiotPollResult,
        gameflow_phase: str | None,
    ) -> str:
        live_status = live_result.status.value
        if live_result.error:
            error_text = " ".join(str(live_result.error).split())
            if len(error_text) > 120:
                error_text = f"{error_text[:117]}..."
            live_status = f"{live_status} ({error_text})"
        return f"Live Client={live_status}, LCU phase={gameflow_phase or 'unknown'}"

    async def wait_for_game_start_async(self) -> bool:
        """LoLの試合開始を監視"""
        self.session_phase = RecordingPhase.WAITING_FOR_GAME
        if not await self.wait_for_previous_game_clear_async():
            self.session_phase = RecordingPhase.CANCELLED
            return False
        self.log("⚔️  LoLの試合開始を待機中 (API監視)...")
        loop = asyncio.get_running_loop()
        next_diagnostic_at = loop.time() + DEFAULT_GAME_START_DIAGNOSTIC_INTERVAL_SEC
        while True:
            if self.should_stop():
                self.session_phase = RecordingPhase.CANCELLED
                return False
            await self.capture_champ_select_async()
            await self.poll_gameflow_phase()
            await self.capture_match_metadata_async()
            result = await self.poll_all_game_data()
            data = result.payload
            game_time = self._live_game_time(data)
            if result.status == RiotPollStatus.IN_GAME and game_time is not None:
                return await self._mark_game_started(
                    source="live_client",
                    live_data=data,
                    game_time=game_time,
                )

            gameflow_phase = self.match_metadata.get("gameflow_phase")
            if self._normalize_gameflow_phase(gameflow_phase) in LCU_GAMEFLOW_START_PHASES:
                live_result = await self.wait_for_live_client_after_lcu_start_async(initial_result=result)
                if live_result is None:
                    if self.should_stop():
                        self.session_phase = RecordingPhase.CANCELLED
                        return False
                    self.log("⚠️ Live Clientを確認できないため、LCU Gameflow Phaseで録画開始します。")
                    return await self._mark_game_started(source="lcu")
                live_data, live_game_time = live_result
                return await self._mark_game_started(
                    source="live_client",
                    live_data=live_data,
                    game_time=live_game_time,
                )

            if loop.time() >= next_diagnostic_at:
                self.log(f"🔎 試合開始監視中: {self._game_start_poll_summary(result, gameflow_phase)}")
                next_diagnostic_at = loop.time() + DEFAULT_GAME_START_DIAGNOSTIC_INTERVAL_SEC
            if not await self.wait_with_stop_async(1.0):
                self.session_phase = RecordingPhase.CANCELLED
                return False

    async def wait_for_live_client_after_lcu_start_async(
        self,
        initial_result: RiotPollResult | None = None,
        timeout_sec: float = DEFAULT_LCU_START_LIVE_CLIENT_GRACE_SEC,
        poll_sec: float = DEFAULT_LCU_START_LIVE_CLIENT_POLL_SEC,
    ) -> tuple[dict[str, Any], float] | None:
        """LCUの開始検知が早すぎる場合に、LoL本体のLive Client起動を短時間待つ。"""
        attempts = max(1, int(max(0.1, float(timeout_sec)) / max(0.1, float(poll_sec))))
        results: list[RiotPollResult] = []
        if initial_result is not None:
            results.append(initial_result)
        self.log("LCUで試合開始を検知しました。LoL本体のLive Client接続を待機します...")

        for attempt in range(attempts):
            if self.should_stop():
                return None
            result = results.pop(0) if results else await self.poll_all_game_data()
            data = result.payload
            game_time = self._live_game_time(data)
            if result.status == RiotPollStatus.IN_GAME and data is not None and game_time is not None:
                self.log(f"Live Client接続を確認しました。GameTime: {float(game_time):.2f}s")
                return data, float(game_time)
            if attempt < attempts - 1 and not await self.wait_with_stop_async(poll_sec):
                return None
        return None

    def _has_live_client_start_anchor(self) -> bool:
        return (
            self.game_start_detection_source == "live_client"
            and self.game_start_anchor_game_time is not None
            and self.game_start_anchor_monotonic is not None
        )

    def _estimated_game_time_from_start_anchor(self, marker_monotonic: float) -> float | None:
        if self.game_start_anchor_game_time is None or self.game_start_anchor_monotonic is None:
            return None
        elapsed = max(0.0, float(marker_monotonic) - float(self.game_start_anchor_monotonic))
        return max(0.0, float(self.game_start_anchor_game_time) + elapsed)

    async def resolve_sync_game_time_async(
        self,
        *,
        event_time: float | None,
        marker_monotonic: float,
    ) -> tuple[float, str]:
        if self.game_start_detection_source == "lcu" and event_time is not None:
            return float(event_time), "game_start_event"

        result = await self.poll_all_game_data()
        live_game_time = self._live_game_time(result.payload)
        estimated_game_time = self._estimated_game_time_from_start_anchor(marker_monotonic)

        if live_game_time is not None:
            if (
                estimated_game_time is not None
                and live_game_time + DEFAULT_SYNC_STALE_GAME_TIME_TOLERANCE_SEC < estimated_game_time
            ):
                return estimated_game_time, "estimated_from_start_anchor"
            return float(live_game_time), "live_client"

        if estimated_game_time is not None:
            return estimated_game_time, "estimated_from_start_anchor"
        if event_time is not None:
            return float(event_time), "game_start_event"
        return 0.0, "unavailable"

    async def start_recording_async(self) -> None:
        """録画開始 -> 同期マーカー"""
        self.session_phase = RecordingPhase.STARTING
        self.log("🎥 録画を開始します...")
        validate_recording_directory(self.config.paths.recordings_dir)
        item_id = self.get_source_id()
        if not item_id:
            raise RecorderError(
                f"同期用ソース '{self.config.obs.source_name}' がシーン '{self.config.obs.scene_name}' に見つかりません。\n"
                "設定画面の「OBSにシーン/色ソースを作成」を実行してください。"
            )

        errors = []
        self._prepare_recording_output_for_start()
        if not await self.wait_with_stop_async(DEFAULT_RECORDING_START_SETTLE_SEC, step=0.1):
            return
        request_started_at = time.time()
        try:
            self.obs_client.start_recording()
        except Exception as e:
            if isinstance(e, OBSSDKRequestError):
                try:
                    if self.obs_client.is_recording_active() is True:
                        self.log("OBSは既に録画中だったため、その録画を継続します。")
                        self.recording_started = True
                        self.session_phase = RecordingPhase.RECORDING
                except Exception:
                    pass
            if not self.recording_started:
                errors.append(self._format_obs_start_error(e, 1))
        else:
            if await self.wait_for_recording_active_async(DEFAULT_RECORDING_START_PRIMARY_TIMEOUT_SEC):
                self.recording_started = True
                self.session_phase = RecordingPhase.RECORDING
            else:
                errors.append("試行1: OBSは開始要求を受理しましたが、録画状態へ移行しませんでした。")

        if not self.recording_started:
            await self._recover_recording_start_async(errors)

        if not self.recording_started:
            details = self._record_status_summary()
            message = "OBS録画開始に失敗しました。\n" + "\n".join(errors)
            if details:
                message += f"\nOBS状態: {details}"
            diagnostics = self._obs_recording_diagnostics(request_started_at)
            if diagnostics:
                message += f"\n{diagnostics}"
            self.log(f"⚠️ {message}")
            raise RecorderError(message)

        event_time = None
        if not self._has_live_client_start_anchor():
            event_time = await self.wait_until_game_start_event_async()
            if self.should_stop():
                self.session_phase = RecordingPhase.CANCELLED
                return

        marker_monotonic = asyncio.get_running_loop().time()
        self.log("⚡ 同期シグナル送信 (Marker ON)")
        self.obs_client.set_sync_marker_enabled(True, item_id)

        sync_time, sync_source = await self.resolve_sync_game_time_async(
            event_time=event_time,
            marker_monotonic=marker_monotonic,
        )

        self.sync_game_time = sync_time
        self.sync_time_source = sync_source
        self.match_metadata["sync_time_source"] = sync_source
        self.log(f"📝 同期ログ記録: {sync_time:.4f}s ({sync_source})")

        await self.wait_with_stop_async(0.5)
        self.obs_client.set_sync_marker_enabled(False, item_id)
        self.log("✅ シグナル消灯。録画継続中。")

    def _prepare_recording_output_for_start(self) -> None:
        preparer = getattr(self.obs_client, "prepare_recording_start", None)
        if not callable(preparer):
            return
        try:
            preparer()
        except Exception as e:
            self.log(f"⚠️ 録画開始前のOBS出力設定再適用に失敗: {e}")

    def _selected_recording_encoder_is_hardware(self) -> bool:
        selected_encoder = getattr(self.obs_client, "last_recording_encoder_selection", None)
        return bool(getattr(selected_encoder, "hardware", False))

    async def _recover_recording_start_async(self, errors: list[str]) -> None:
        if self.should_stop():
            return
        self.log("⚠️ OBS録画が開始状態へ移行しないため、出力設定を再適用して再試行します。")
        self._prepare_recording_output_for_start()
        set_encoder = getattr(self.obs_client, "set_recording_encoder", None)
        if callable(set_encoder):
            try:
                if self._selected_recording_encoder_is_hardware():
                    self.log("⚠️ GPUエンコーダ失敗のためx264へ切り替えて再試行します。")
                set_encoder("x264")
            except Exception as e:
                errors.append(f"復旧準備: x264への切り替えに失敗しました ({type(e).__name__}: {e})")
        if not await self.wait_with_stop_async(DEFAULT_RECORDING_START_SETTLE_SEC, step=0.1):
            return

        retry_started_at = time.time()
        try:
            toggler = getattr(self.obs_client, "toggle_recording", None)
            if callable(toggler):
                toggler()
            else:
                self.obs_client.start_recording()
        except Exception as e:
            errors.append(f"復旧試行: {type(e).__name__}: {e}")
            return

        if await self.wait_for_recording_active_async(DEFAULT_RECORDING_START_RECOVERY_TIMEOUT_SEC):
            self.recording_started = True
            self.session_phase = RecordingPhase.RECORDING
            self.log("✅ OBS録画を復旧試行で開始しました。")
            return

        errors.append("復旧試行: OBSは再試行要求後も録画状態へ移行しませんでした。")
        diagnostics = self._obs_recording_diagnostics(retry_started_at)
        if diagnostics:
            errors.append(diagnostics)

    async def wait_for_recording_active_async(
        self,
        timeout_sec: float = DEFAULT_RECORDING_START_TIMEOUT_SEC,
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.1, float(timeout_sec))
        while loop.time() < deadline:
            if self.should_stop():
                return False
            try:
                if self.obs_client.is_recording_active() is True:
                    return True
            except Exception as e:
                self.logger.debug("OBS recording status poll failed: %s", e)
            await asyncio.sleep(DEFAULT_RECORDING_START_POLL_SEC)
        return False

    def _record_status_summary(self) -> str:
        get_details = getattr(self.obs_client, "get_record_status_details", None)
        if not callable(get_details):
            return ""
        try:
            details = get_details()
        except Exception as e:
            return f"取得失敗 ({type(e).__name__}: {e})"
        if not isinstance(details, dict):
            return ""
        return ", ".join(f"{key}={value}" for key, value in details.items() if value is not None)

    def _obs_recording_diagnostics(self, since: float) -> str:
        try:
            lines = OBSProcessManager(self.config.obs.obs_dir, logger=self.logger).latest_log_recording_diagnostics(
                since=since
            )
        except Exception as e:
            self.logger.debug("OBS recording diagnostics failed: %s", e)
            return ""
        if lines:
            return "OBSログ:\n" + "\n".join(f"- {line}" for line in lines)
        return "OBSログ: 録画開始要求後にOBS側の録画出力ログが見つかりませんでした。"

    @staticmethod
    def _format_obs_start_error(error: BaseException, attempt: int) -> str:
        if isinstance(error, OBSSDKRequestError):
            return (
                f"試行{attempt}: request={getattr(error, 'req_name', 'StartRecord')}, "
                f"code={getattr(error, 'code', '?')}, detail={error}"
            )
        return f"試行{attempt}: {type(error).__name__}: {error}"

    async def wait_until_game_start_event_async(self, timeout_sec: float = DEFAULT_GAME_START_EVENT_WAIT_SEC) -> float | None:
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
                role = champion_kill_role(event, self.my_name)
                if role:
                    should_save = True
                    log_message = f"[{role.upper()}] {event_name}"

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
            temporary_failure_grace_sec=self.config.polling.end_temporary_failure_grace_sec,
            gameflow_inactive_grace_sec=self.gameflow_inactive_grace_sec,
            game_process_missing_grace_sec=self.game_process_missing_grace_sec,
        )
        while True:
            if self.should_stop():
                self.session_phase = RecordingPhase.CANCELLED
                return RecordingOutcome.CANCELLED
            result = await self.poll_all_game_data()
            now = loop.time()
            data = result.payload
            if not data:
                decision = end_detector.observe_poll_status(result.status, now)
                if decision.should_end:
                    return await self._complete_recording_end_async(decision)
                decision = await self._observe_recording_context_end_async(end_detector, result, now)
                if decision.should_end:
                    return await self._complete_recording_end_async(decision)
                if not await self.wait_with_stop_async(self.config.polling.end_poll_sec):
                    self.session_phase = RecordingPhase.CANCELLED
                    return RecordingOutcome.CANCELLED
                continue

            end_detector.observe_poll_status(result.status, now)
            decision = await self._observe_recording_context_end_async(end_detector, result, now)
            if decision.should_end:
                return await self._complete_recording_end_async(decision)
            self.last_game_data = data
            self.match_metadata = merge_live_game_metadata(self.match_metadata, data)
            if not self.my_name:
                await self.try_update_player_name_async()
            self.update_player_info_from_game_data(data)

            event_data = await self.riot_api_client.get_event_data()
            if event_data:
                events = event_data.get("Events", [])
                self.process_events(events)
                self.update_result_from_events(events)
                if any(self.is_game_end_event(event) for event in events):
                    return await self._complete_recording_end_async(end_detector.observe_game_end_event())

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
                if self.record_path is None:
                    self.session_outcome = RecordingOutcome.FAILED_PARTIAL
                    self.failure_reason = "OBS録画が完了処理前に停止しており、動画ファイルを確認できませんでした。"
                return
        except Exception:
            pass

        try:
            self.record_path = self.obs_client.stop_recording()
            if self.record_path:
                self.log(f"💾 保存完了: {self.record_path}")
            else:
                self.session_outcome = RecordingOutcome.FAILED_PARTIAL
                self.failure_reason = "OBSから録画ファイルの保存先が返されませんでした。"
            self.recording_started = False
        except OBSSDKRequestError as e:
            if e.code == 501:
                self.recording_started = False
                self.session_outcome = RecordingOutcome.FAILED_PARTIAL
                self.failure_reason = f"OBS録画は既に停止していました: {e}"
                return
            self.log(f"⚠️ 録画停止エラー: {e}")
            self.session_outcome = RecordingOutcome.FAILED_PARTIAL
            self.failure_reason = f"OBS録画停止に失敗しました: {e}"
        except Exception as e:
            self.log(f"⚠️ 録画停止エラー: {e}")
            self.session_outcome = RecordingOutcome.FAILED_PARTIAL
            self.failure_reason = f"OBS録画停止に失敗しました: {e}"

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

        if self.last_game_data and (self.game_result is None or self.winning_team is None):
            game_data = self.last_game_data.get("gameData", {})
            if isinstance(game_data, dict):
                result_value = game_data.get("gameResult") or game_data.get("result")
                winning_team = game_data.get("winningTeam") or game_data.get("winning_team")
                if self.game_result is None:
                    self.game_result = normalize_game_result_value(result_value) or result_value
                if self.winning_team is None:
                    self.winning_team = normalize_lcu_team(winning_team) or winning_team
                if self.game_result or self.winning_team:
                    self.match_metadata["result_source"] = "live_client_game_data"

        if self.game_start_detection_source:
            self.match_metadata["game_start_detection_source"] = self.game_start_detection_source
        if self.sync_time_source:
            self.match_metadata["sync_time_source"] = self.sync_time_source

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
            match=dict(self.match_metadata),
            ban_pick=self.champ_select_tracker.to_payload() if self.champ_select_tracker.has_data else {},
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
