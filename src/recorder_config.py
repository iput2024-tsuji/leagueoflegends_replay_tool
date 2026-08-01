from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from . import config_schema
    from .app_paths import get_user_data_root
    from .storage_policy import parse_max_storage_bytes
except ImportError:
    import config_schema
    from app_paths import get_user_data_root
    from storage_policy import parse_max_storage_bytes


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
    fps_numerator: int
    fps_denominator: int
    base_width: int
    base_height: int
    output_width: int
    output_height: int
    scale_type: str
    recording_quality: str
    recording_encoder: str
    obs_dir: Path

    @property
    def fps(self) -> float:
        return self.fps_numerator / self.fps_denominator

    @property
    def game_capture_name(self) -> str:
        return self.window_capture_name

    @property
    def game_capture_window(self) -> str:
        return self.window_capture_window


@dataclass(frozen=True)
class PathsSettings:
    bin_dir: Path
    ffmpeg_executable: Path | None
    recordings_dir: Path
    json_dir: Path
    champion_icons_dir: Path
    champion_aliases_path: Path


@dataclass(frozen=True)
class PollingSettings:
    end_error_limit: int
    end_missing_grace_sec: float
    end_temporary_failure_grace_sec: float
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
    mic: AudioSlotSettings

    def to_dict(self) -> dict[str, Any]:
        return {"mic": self.mic.to_dict()}


def resolve_path(value: str | Path | None, base_dir: str | Path) -> Path | None:
    if value is None:
        return None
    path = Path(os.path.expandvars(str(value)))
    return path if path.is_absolute() else (Path(base_dir) / path).resolve()


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
        normalized = config_schema.normalize_config(source, auto_fix=True).config
        obs = normalized["obs"]
        paths = normalized["paths"]
        polling = normalized["polling"]
        storage = normalized["storage"]
        audio = normalized["audio"]["mic"]
        data_root = get_user_data_root()
        recordings_dir = (
            resolve_path(paths.get("recordings_dir"), data_root)
            or (data_root / config_schema.DEFAULT_RECORDINGS_DIR).resolve()
        )
        json_dir = resolve_path(paths.get("json_dir"), data_root) or (recordings_dir / "json").resolve()
        bin_dir = resolve_path(paths.get("bin_dir"), data_root) or (data_root / config_schema.DEFAULT_BIN_DIR).resolve()
        ffmpeg_value = str(paths.get("ffmpeg_executable") or "").strip()
        ffmpeg_executable = resolve_path(ffmpeg_value, data_root) if ffmpeg_value else None
        icons_dir = (
            resolve_path(paths.get("champion_icons_dir"), data_root)
            or (data_root / config_schema.DEFAULT_CHAMPION_ICONS_DIR).resolve()
        )
        aliases = (
            resolve_path(paths.get("champion_aliases_path"), data_root)
            or (data_root / config_schema.DEFAULT_CHAMPION_ALIASES_PATH).resolve()
        )
        password, _ = config_schema.ensure_obs_password_value(obs.get("password"))
        return cls(
            obs=OBSSettings(
                host=str(obs["host"]),
                port=_int(obs["port"], config_schema.DEFAULT_OBS_PORT),
                password=password,
                scene_name=str(obs["scene_name"]),
                source_name=str(obs["source_name"]),
                source_color=_int(obs["source_color"], config_schema.DEFAULT_OBS_SOURCE_COLOR),
                window_capture_name=str(obs["window_capture_name"]),
                window_capture_window=str(obs["window_capture_window"]),
                window_capture_method=_int(
                    obs["window_capture_method"], config_schema.DEFAULT_OBS_WINDOW_CAPTURE_METHOD
                ),
                fps_numerator=_int(obs["fps_numerator"], config_schema.DEFAULT_OBS_FPS_NUMERATOR),
                fps_denominator=_int(obs["fps_denominator"], config_schema.DEFAULT_OBS_FPS_DENOMINATOR),
                base_width=_int(obs["base_width"], config_schema.DEFAULT_OBS_BASE_WIDTH),
                base_height=_int(obs["base_height"], config_schema.DEFAULT_OBS_BASE_HEIGHT),
                output_width=_int(obs["output_width"], config_schema.DEFAULT_OBS_OUTPUT_WIDTH),
                output_height=_int(obs["output_height"], config_schema.DEFAULT_OBS_OUTPUT_HEIGHT),
                scale_type=str(obs["scale_type"]),
                recording_quality=str(obs["recording_quality"]),
                recording_encoder=str(obs["recording_encoder"]),
                obs_dir=(data_root / config_schema.DEFAULT_OBS_DIR).resolve(),
            ),
            paths=PathsSettings(
                bin_dir,
                ffmpeg_executable,
                recordings_dir,
                json_dir,
                icons_dir,
                aliases,
            ),
            polling=PollingSettings(
                _int(polling["end_error_limit"], config_schema.DEFAULT_END_ERROR_LIMIT),
                _float(polling["end_missing_grace_sec"], config_schema.DEFAULT_END_MISSING_GRACE_SEC),
                _float(
                    polling["end_temporary_failure_grace_sec"], config_schema.DEFAULT_END_TEMPORARY_FAILURE_GRACE_SEC
                ),
                _float(polling["end_poll_sec"], config_schema.DEFAULT_END_POLL_SEC),
                _float(polling["event_poll_sec"], config_schema.DEFAULT_EVENT_POLL_SEC),
            ),
            storage=StorageSettings(
                _float(storage["max_size_gb"], config_schema.DEFAULT_MAX_STORAGE_GB),
                parse_max_storage_bytes(storage, config_schema.DEFAULT_MAX_STORAGE_GB),
            ),
            audio=AudioSettings(
                AudioSlotSettings(
                    str(audio["input_name"]),
                    str(audio["device_id"]),
                    str(audio["device_name"]),
                    _float(audio["volume_db"], 0.0),
                    bool(audio["mute"]),
                )
            ),
        )

    @classmethod
    def load(cls) -> AppConfig:
        try:
            from .config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
        except ImportError:
            from config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
        return cls.from_dict(ConfigRepository(CONFIG_PATH, SAMPLE_CONFIG_PATH).load(create_if_missing=True))

    def audio_to_dict(self) -> dict[str, Any]:
        return {"audio": self.audio.to_dict()}
