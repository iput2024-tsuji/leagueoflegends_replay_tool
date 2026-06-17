from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

try:
    from .notifications import DEFAULT_NOTIFICATION_SETTINGS
except ImportError:
    from notifications import DEFAULT_NOTIFICATION_SETTINGS

DEFAULT_OBS_PASSWORD_LENGTH = 24
DEFAULT_OBS_SCENE_NAME = "lol_seen"
DEFAULT_OBS_SOURCE_NAME = "color"
DEFAULT_OBS_SOURCE_COLOR = 0xFF0000FF
LEGACY_OBS_SOURCE_COLOR_BLUE = 0xFFFF0000
DEFAULT_OBS_WINDOW_CAPTURE_NAME = "lol_window_capture"
DEFAULT_OBS_WINDOW_CAPTURE_WINDOW = "League of Legends (TM) Client:RiotWindowClass:League of Legends.exe"
DEFAULT_OBS_WINDOW_CAPTURE_METHOD = 2
DEFAULT_OBS_GAME_CAPTURE_NAME = "lol_game_capture"
DEFAULT_OBS_LEGACY_GAME_CAPTURE_WINDOW = "League of Legends (TM) Client:League of Legends.exe:League of Legends.exe"
DEFAULT_OBS_DIR = "obs-portable"
DEFAULT_BIN_DIR = "bin"
DEFAULT_RECORDINGS_DIR = "recordings"
DEFAULT_JSON_DIR = "recordings/json"
DEFAULT_CHAMPION_ICONS_DIR = "assets/champions/icons"
DEFAULT_CHAMPION_ALIASES_PATH = "config/champion_aliases.json"
DEFAULT_OBS_HOST = "localhost"
DEFAULT_OBS_PORT = 4455
DEFAULT_OBS_FPS_NUMERATOR = 60
DEFAULT_OBS_FPS_DENOMINATOR = 1
MAX_OBS_FPS_NUMERATOR = 1_000_000
MAX_OBS_FPS_DENOMINATOR = 100_000
DEFAULT_OBS_BASE_WIDTH = 1920
DEFAULT_OBS_BASE_HEIGHT = 1080
DEFAULT_OBS_OUTPUT_WIDTH = 1920
DEFAULT_OBS_OUTPUT_HEIGHT = 1080
DEFAULT_OBS_SCALE_TYPE = "lanczos"
DEFAULT_OBS_RECORDING_QUALITY = "Small"
VALID_OBS_SCALE_TYPES = frozenset({"bilinear", "bicubic", "lanczos", "area"})
VALID_OBS_RECORDING_QUALITIES = frozenset({"Stream", "Small", "HQ", "Lossless"})
DEFAULT_END_ERROR_LIMIT = 3
DEFAULT_END_MISSING_GRACE_SEC = 60.0
DEFAULT_END_TEMPORARY_FAILURE_GRACE_SEC = 180.0
DEFAULT_END_POLL_SEC = 5
DEFAULT_EVENT_POLL_SEC = 1
DEFAULT_MAX_STORAGE_GB = 50
DEFAULT_AUDIO_DEVICE_ID = "default"
DEFAULT_AUDIO_DEVICE_NAME = "Default"
DEFAULT_AUDIO_MIC_INPUT_NAME = "lol_mic_audio"
DEFAULT_AUDIO_MIC_VOLUME_DB = 0.0
DEFAULT_AUDIO_MIC_MUTE = False

MANAGED_AUDIO_INPUTS = {
    "mic": {
        "label": "マイク入力",
        "input_name": DEFAULT_AUDIO_MIC_INPUT_NAME,
        "input_kind": "wasapi_input_capture",
        "default_volume_db": DEFAULT_AUDIO_MIC_VOLUME_DB,
        "default_mute": DEFAULT_AUDIO_MIC_MUTE,
    },
}


@dataclass
class ConfigNormalizationResult:
    config: dict[str, Any]
    changed: bool = False
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_report(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "changed": self.changed,
            "notes": list(self.notes),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def generate_obs_password(length: int = DEFAULT_OBS_PASSWORD_LENGTH) -> str:
    token = secrets.token_urlsafe(max(18, int(length)))
    return token[: max(12, int(length))]


def is_missing_obs_password(value: Any) -> bool:
    return str(value or "").strip() in {"", "your_password_here"}


def ensure_obs_password_value(
    value: Any,
    password_factory: Callable[[], str] | None = None,
) -> tuple[str, bool]:
    if is_missing_obs_password(value):
        factory = password_factory or generate_obs_password
        return str(factory()), True
    return str(value).strip(), False


def normalize_config(
    data: dict[str, Any] | None,
    *,
    auto_fix: bool = True,
    password_factory: Callable[[], str] | None = None,
) -> ConfigNormalizationResult:
    result = ConfigNormalizationResult(config=data if isinstance(data, dict) else {})
    if result.config is not data:
        result.changed = True
        result.notes.append("設定形式が不正だったため初期化しました。")

    obs_cfg = _ensure_section(result, "obs")
    paths_cfg = _ensure_section(result, "paths")
    poll_cfg = _ensure_section(result, "polling")
    storage_cfg = _ensure_section(result, "storage")
    app_cfg = _ensure_section(result, "app")
    notifications_cfg = _ensure_section(result, "notifications")
    _ensure_section(result, "audio")
    _migrate_obs_fps_config(result, obs_cfg, auto_fix=auto_fix)

    _apply_defaults(
        result,
        obs_cfg,
        {
            "host": DEFAULT_OBS_HOST,
            "port": DEFAULT_OBS_PORT,
            "fps_numerator": DEFAULT_OBS_FPS_NUMERATOR,
            "fps_denominator": DEFAULT_OBS_FPS_DENOMINATOR,
            "base_width": DEFAULT_OBS_BASE_WIDTH,
            "base_height": DEFAULT_OBS_BASE_HEIGHT,
            "output_width": DEFAULT_OBS_OUTPUT_WIDTH,
            "output_height": DEFAULT_OBS_OUTPUT_HEIGHT,
            "scale_type": DEFAULT_OBS_SCALE_TYPE,
            "recording_quality": DEFAULT_OBS_RECORDING_QUALITY,
            "scene_name": DEFAULT_OBS_SCENE_NAME,
            "source_name": DEFAULT_OBS_SOURCE_NAME,
            "source_color": DEFAULT_OBS_SOURCE_COLOR,
            "dir": DEFAULT_OBS_DIR,
        },
        auto_fix=auto_fix,
    )
    _apply_defaults(
        result,
        paths_cfg,
        {
            "bin_dir": DEFAULT_BIN_DIR,
            "recordings_dir": DEFAULT_RECORDINGS_DIR,
            "json_dir": DEFAULT_JSON_DIR,
            "champion_icons_dir": DEFAULT_CHAMPION_ICONS_DIR,
            "champion_aliases_path": DEFAULT_CHAMPION_ALIASES_PATH,
        },
        auto_fix=auto_fix,
    )
    _apply_defaults(
        result,
        poll_cfg,
        {
            "end_error_limit": DEFAULT_END_ERROR_LIMIT,
            "end_missing_grace_sec": DEFAULT_END_MISSING_GRACE_SEC,
            "end_temporary_failure_grace_sec": DEFAULT_END_TEMPORARY_FAILURE_GRACE_SEC,
            "end_poll_sec": DEFAULT_END_POLL_SEC,
            "event_poll_sec": DEFAULT_EVENT_POLL_SEC,
        },
        auto_fix=auto_fix,
    )
    _apply_defaults(result, storage_cfg, {"max_size_gb": DEFAULT_MAX_STORAGE_GB}, auto_fix=auto_fix)
    _apply_defaults(result, notifications_cfg, DEFAULT_NOTIFICATION_SETTINGS, auto_fix=auto_fix)

    _ensure_audio_config_defaults(result, auto_fix=auto_fix)
    _normalize_obs_capture_config(result, obs_cfg, auto_fix=auto_fix)
    _normalize_password(result, obs_cfg, auto_fix=auto_fix, password_factory=password_factory)
    _normalize_numeric_values(result, obs_cfg, poll_cfg, storage_cfg, auto_fix=auto_fix)

    if app_cfg.get("minimize_to_tray") is None and auto_fix:
        app_cfg["minimize_to_tray"] = True
        result.changed = True

    return result


def _migrate_obs_fps_config(
    result: ConfigNormalizationResult,
    obs_cfg: dict[str, Any],
    *,
    auto_fix: bool,
) -> None:
    if "fps" not in obs_cfg:
        return

    if auto_fix:
        if obs_cfg.get("fps_numerator") in (None, ""):
            obs_cfg["fps_numerator"] = obs_cfg.get("fps")
        if obs_cfg.get("fps_denominator") in (None, ""):
            obs_cfg["fps_denominator"] = DEFAULT_OBS_FPS_DENOMINATOR
        obs_cfg.pop("fps", None)
        result.changed = True
        result.notes.append("旧OBS FPS設定を分子・分母形式へ更新しました。")
    else:
        result.warnings.append("旧OBS FPS設定が残っています。")


def parse_obs_source_color(value: Any, default: int = DEFAULT_OBS_SOURCE_COLOR) -> tuple[int, bool]:
    if value is None:
        return default, False
    if isinstance(value, int):
        return value & 0xFFFFFFFF, True

    text = str(value).strip()
    if not text:
        return default, False

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


def _ensure_section(result: ConfigNormalizationResult, key: str) -> dict[str, Any]:
    value = result.config.get(key)
    if isinstance(value, dict):
        return value
    result.config[key] = {}
    result.changed = True
    return result.config[key]


def _apply_defaults(
    result: ConfigNormalizationResult,
    target: dict[str, Any],
    defaults: dict[str, Any],
    *,
    auto_fix: bool,
) -> None:
    for key, value in defaults.items():
        if target.get(key) in (None, ""):
            if auto_fix:
                target[key] = value
                result.changed = True
                result.notes.append(f"{key} を既定値で補完しました。")
            else:
                result.errors.append(f"{key} が未設定です。")


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


def _ensure_audio_config_defaults(result: ConfigNormalizationResult, *, auto_fix: bool) -> None:
    audio_cfg = _ensure_section(result, "audio")
    if "desktop" in audio_cfg:
        audio_cfg.pop("desktop")
        result.changed = True

    for key, spec in MANAGED_AUDIO_INPUTS.items():
        slot = audio_cfg.get(key)
        if not isinstance(slot, dict):
            slot = {}
            audio_cfg[key] = slot
            result.changed = True

        defaults = {
            "input_name": spec["input_name"],
            "device_id": DEFAULT_AUDIO_DEVICE_ID,
            "device_name": DEFAULT_AUDIO_DEVICE_NAME,
            "volume_db": spec["default_volume_db"],
            "mute": spec["default_mute"],
        }
        _apply_defaults(result, slot, defaults, auto_fix=auto_fix)

        input_name = str(slot.get("input_name") or "").strip()
        if not input_name:
            if auto_fix:
                slot["input_name"] = spec["input_name"]
                result.changed = True
                result.warnings.append(f"audio.{key}.input_name が不正だったため既定値を使用します。")
            else:
                result.errors.append(f"audio.{key}.input_name が不正です。")

        device_id = str(slot.get("device_id") or "").strip()
        if not device_id:
            if auto_fix:
                slot["device_id"] = DEFAULT_AUDIO_DEVICE_ID
                result.changed = True
                result.warnings.append(f"audio.{key}.device_id が不正だったため default を使用します。")
            else:
                result.errors.append(f"audio.{key}.device_id が不正です。")

        device_name = str(slot.get("device_name") or "").strip()
        if not device_name and auto_fix:
            slot["device_name"] = DEFAULT_AUDIO_DEVICE_NAME
            result.changed = True

        volume_db, ok = _safe_float(slot.get("volume_db"), spec["default_volume_db"])
        if not ok:
            if auto_fix:
                slot["volume_db"] = volume_db
                result.changed = True
            result.warnings.append(f"audio.{key}.volume_db が不正だったため既定値を使用します。")

        mute_value, ok = _safe_bool(slot.get("mute"), spec["default_mute"])
        if not ok:
            if auto_fix:
                slot["mute"] = mute_value
                result.changed = True
            result.warnings.append(f"audio.{key}.mute が不正だったため既定値を使用します。")


def _normalize_obs_capture_config(
    result: ConfigNormalizationResult,
    obs_cfg: dict[str, Any],
    *,
    auto_fix: bool,
) -> None:
    def empty(value: Any) -> bool:
        return value is None or str(value).strip() == ""

    def normalize_text(key: str, legacy_key: str, default: str) -> None:
        current = obs_cfg.get(key)
        legacy = obs_cfg.get(legacy_key)
        if empty(current):
            if not empty(legacy):
                if auto_fix:
                    obs_cfg[key] = str(legacy).strip()
                    result.changed = True
                    result.notes.append(f"{legacy_key} を {key} へ移行しました。")
                else:
                    result.warnings.append(f"{legacy_key} は旧設定キーです。{key} へ移行してください。")
            elif auto_fix:
                obs_cfg[key] = default
                result.changed = True
                result.notes.append(f"{key} を既定値で補完しました。")
            else:
                result.errors.append(f"{key} が未設定です。")
            return

        trimmed = str(current).strip()
        if current != trimmed and auto_fix:
            obs_cfg[key] = trimmed
            result.changed = True
            result.notes.append(f"{key} の前後空白を削除しました。")

    normalize_text("window_capture_name", "game_capture_name", DEFAULT_OBS_WINDOW_CAPTURE_NAME)
    normalize_text("window_capture_window", "game_capture_window", DEFAULT_OBS_WINDOW_CAPTURE_WINDOW)

    normalized_name = normalize_window_capture_source_name(obs_cfg.get("window_capture_name"))
    if not empty(obs_cfg.get("window_capture_name")) and obs_cfg.get("window_capture_name") != normalized_name:
        if auto_fix:
            obs_cfg["window_capture_name"] = normalized_name
            result.changed = True
            result.notes.append(f"window_capture_name を {normalized_name} に更新しました。")
        else:
            result.warnings.append(f"window_capture_name は {normalized_name} への更新が必要です。")

    normalized_window = normalize_window_capture_window_selector(obs_cfg.get("window_capture_window"))
    if not empty(obs_cfg.get("window_capture_window")) and obs_cfg.get("window_capture_window") != normalized_window:
        if auto_fix:
            obs_cfg["window_capture_window"] = normalized_window
            result.changed = True
            result.notes.append("window_capture_window をLoLのRiotWindowClass指定に更新しました。")
        else:
            result.warnings.append("window_capture_window はLoLのRiotWindowClass指定への更新が必要です。")

    raw_method = obs_cfg.get("window_capture_method")
    method, ok = _safe_int(raw_method, DEFAULT_OBS_WINDOW_CAPTURE_METHOD, minimum=0, maximum=2)
    if empty(raw_method):
        if auto_fix:
            obs_cfg["window_capture_method"] = DEFAULT_OBS_WINDOW_CAPTURE_METHOD
            result.changed = True
            result.notes.append("window_capture_method を Windows Graphics Capture で補完しました。")
        else:
            result.errors.append("window_capture_method が未設定です。")
    elif not ok:
        if auto_fix:
            obs_cfg["window_capture_method"] = method
            result.changed = True
        result.warnings.append(
            f"window_capture_method が不正だったため {DEFAULT_OBS_WINDOW_CAPTURE_METHOD} を使用します。"
        )
    elif auto_fix and raw_method != method:
        obs_cfg["window_capture_method"] = method
        result.changed = True

    legacy_removed = False
    if auto_fix:
        for key in ("game_capture_name", "game_capture_window"):
            if key in obs_cfg:
                obs_cfg.pop(key, None)
                legacy_removed = True
        if legacy_removed:
            result.changed = True
            result.notes.append("旧Game Capture設定キーを削除しました。")
    elif any(key in obs_cfg for key in ("game_capture_name", "game_capture_window")):
        result.warnings.append("旧Game Capture設定キーが残っています。")


def _normalize_password(
    result: ConfigNormalizationResult,
    obs_cfg: dict[str, Any],
    *,
    auto_fix: bool,
    password_factory: Callable[[], str] | None,
) -> None:
    password_value, generated_password = ensure_obs_password_value(obs_cfg.get("password"), password_factory)
    if generated_password:
        if auto_fix:
            obs_cfg["password"] = password_value
            result.changed = True
            result.notes.append("OBS WebSocketパスワードを自動生成しました。")
        else:
            result.errors.append("OBS WebSocketパスワードが未設定です。")
    elif obs_cfg.get("password") != password_value:
        if auto_fix:
            obs_cfg["password"] = password_value
            result.changed = True
            result.notes.append("OBS WebSocketパスワードの前後空白を削除しました。")
        else:
            result.warnings.append("OBS WebSocketパスワードに前後空白があります。")


def _normalize_numeric_values(
    result: ConfigNormalizationResult,
    obs_cfg: dict[str, Any],
    poll_cfg: dict[str, Any],
    storage_cfg: dict[str, Any],
    *,
    auto_fix: bool,
) -> None:
    port, ok = _safe_int(obs_cfg.get("port"), DEFAULT_OBS_PORT, minimum=1, maximum=65535)
    if not ok:
        if auto_fix:
            obs_cfg["port"] = port
            result.changed = True
        result.warnings.append(f"OBSポートが不正だったため {port} を使用します。")

    for key, default, maximum, label in (
        (
            "fps_numerator",
            DEFAULT_OBS_FPS_NUMERATOR,
            MAX_OBS_FPS_NUMERATOR,
            "OBS FPS分子",
        ),
        (
            "fps_denominator",
            DEFAULT_OBS_FPS_DENOMINATOR,
            MAX_OBS_FPS_DENOMINATOR,
            "OBS FPS分母",
        ),
    ):
        value, value_ok = _safe_int(obs_cfg.get(key), default, minimum=1, maximum=maximum)
        if not value_ok:
            if auto_fix:
                obs_cfg[key] = value
                result.changed = True
            result.warnings.append(f"{label}が不正だったため {value} を使用します。")

    for key, default in (
        ("base_width", DEFAULT_OBS_BASE_WIDTH),
        ("base_height", DEFAULT_OBS_BASE_HEIGHT),
        ("output_width", DEFAULT_OBS_OUTPUT_WIDTH),
        ("output_height", DEFAULT_OBS_OUTPUT_HEIGHT),
    ):
        value, value_ok = _safe_int(obs_cfg.get(key), default, minimum=64, maximum=4096)
        if value % 2:
            value_ok = False
            value = default
        if not value_ok:
            if auto_fix:
                obs_cfg[key] = value
                result.changed = True
            result.warnings.append(f"OBS {key} が不正だったため {value} を使用します。")

    scale_type = str(obs_cfg.get("scale_type") or "").strip().lower()
    if scale_type not in VALID_OBS_SCALE_TYPES:
        if auto_fix:
            obs_cfg["scale_type"] = DEFAULT_OBS_SCALE_TYPE
            result.changed = True
        result.warnings.append(f"OBS scale_type が不正だったため {DEFAULT_OBS_SCALE_TYPE} を使用します。")
    elif auto_fix and obs_cfg.get("scale_type") != scale_type:
        obs_cfg["scale_type"] = scale_type
        result.changed = True

    recording_quality = str(obs_cfg.get("recording_quality") or "").strip()
    quality_lookup = {value.lower(): value for value in VALID_OBS_RECORDING_QUALITIES}
    normalized_quality = quality_lookup.get(recording_quality.lower())
    if normalized_quality is None:
        if auto_fix:
            obs_cfg["recording_quality"] = DEFAULT_OBS_RECORDING_QUALITY
            result.changed = True
        result.warnings.append(f"OBS recording_quality が不正だったため {DEFAULT_OBS_RECORDING_QUALITY} を使用します。")
    elif auto_fix and obs_cfg.get("recording_quality") != normalized_quality:
        obs_cfg["recording_quality"] = normalized_quality
        result.changed = True

    raw_source_color = obs_cfg.get("source_color")
    source_color, ok = parse_obs_source_color(raw_source_color, default=DEFAULT_OBS_SOURCE_COLOR)
    if not ok:
        if auto_fix:
            obs_cfg["source_color"] = source_color
            result.changed = True
        result.warnings.append("source_color が不正だったため赤 (#FF0000) を使用します。")
    elif source_color == LEGACY_OBS_SOURCE_COLOR_BLUE:
        raw_text = str(raw_source_color).strip().lower() if raw_source_color is not None else ""
        legacy_values = {"", "4294901760", "0xffff0000", "#0000ff"}
        is_legacy = (
            raw_source_color == LEGACY_OBS_SOURCE_COLOR_BLUE
            if isinstance(raw_source_color, int)
            else raw_text in legacy_values
        )
        if is_legacy and auto_fix:
            obs_cfg["source_color"] = DEFAULT_OBS_SOURCE_COLOR
            result.changed = True
            result.notes.append("旧設定の青色ソースを赤色 (#FF0000) に更新しました。")

    _normalize_int_field(
        result,
        poll_cfg,
        "end_error_limit",
        DEFAULT_END_ERROR_LIMIT,
        "end_error_limit が不正だったため既定値を使用します。",
        auto_fix=auto_fix,
        minimum=1,
    )
    _normalize_float_field(
        result,
        poll_cfg,
        "end_missing_grace_sec",
        DEFAULT_END_MISSING_GRACE_SEC,
        "end_missing_grace_sec が不正だったため既定値を使用します。",
        auto_fix=auto_fix,
        minimum=0.0,
    )
    _normalize_float_field(
        result,
        poll_cfg,
        "end_temporary_failure_grace_sec",
        DEFAULT_END_TEMPORARY_FAILURE_GRACE_SEC,
        "end_temporary_failure_grace_sec が不正だったため既定値を使用します。",
        auto_fix=auto_fix,
        minimum=0.0,
    )
    _normalize_float_field(
        result,
        poll_cfg,
        "end_poll_sec",
        DEFAULT_END_POLL_SEC,
        "end_poll_sec が不正だったため既定値を使用します。",
        auto_fix=auto_fix,
        minimum=0.1,
    )
    _normalize_float_field(
        result,
        poll_cfg,
        "event_poll_sec",
        DEFAULT_EVENT_POLL_SEC,
        "event_poll_sec が不正だったため既定値を使用します。",
        auto_fix=auto_fix,
        minimum=0.1,
    )
    _normalize_float_field(
        result,
        storage_cfg,
        "max_size_gb",
        DEFAULT_MAX_STORAGE_GB,
        "max_size_gb が不正だったため既定値を使用します。",
        auto_fix=auto_fix,
        minimum=0.1,
    )


def _normalize_int_field(
    result: ConfigNormalizationResult,
    section: dict[str, Any],
    key: str,
    default: int,
    warning: str,
    *,
    auto_fix: bool,
    minimum: int | None = None,
    maximum: int | None = None,
) -> None:
    value, ok = _safe_int(section.get(key), default, minimum=minimum, maximum=maximum)
    if not ok:
        if auto_fix:
            section[key] = value
            result.changed = True
        result.warnings.append(warning)


def _normalize_float_field(
    result: ConfigNormalizationResult,
    section: dict[str, Any],
    key: str,
    default: float,
    warning: str,
    *,
    auto_fix: bool,
    minimum: float | None = None,
) -> None:
    value, ok = _safe_float(section.get(key), default, minimum=minimum)
    if not ok:
        if auto_fix:
            section[key] = value
            result.changed = True
        result.warnings.append(warning)


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""
