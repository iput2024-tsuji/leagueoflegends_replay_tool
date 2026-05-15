from __future__ import annotations

import configparser
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from .obs_process import OBSProcessManager
except ImportError:
    from obs_process import OBSProcessManager


LOGGER = logging.getLogger("lol_replay.obs_bootstrap")
PORTABLE_OBS_MARKER_NAME = "obs_portable_mode.txt"
LEGACY_PORTABLE_OBS_MARKER_NAME = "portable_mode.txt"
TRAY_SETTINGS = {
    "SysTrayEnabled": "false",
    "SysTrayWhenStarted": "false",
    "SysTrayMinimizeToTray": "false",
}
TRAY_SETTINGS_SECTION = "BasicWindow"


@dataclass(frozen=True)
class BootstrapReport:
    obs_dir: Path
    obs_exe: Path
    portable_marker_exists: bool
    legacy_marker_exists: bool
    config_dir_exists: bool
    global_ini_exists: bool
    global_ini_parse_error: str | None = None
    missing_tray_settings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return (
            self.obs_exe.exists()
            and self.portable_marker_exists
            and self.config_dir_exists
            and self.global_ini_exists
            and self.global_ini_parse_error is None
            and not self.missing_tray_settings
        )

    @property
    def needs_repair(self) -> bool:
        return not self.ready


def get_obs_executable_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "bin" / "64bit" / "obs64.exe"


def get_obs_config_dir(base_dir: str | Path) -> Path:
    return Path(base_dir) / "config" / "obs-studio"


def get_obs_global_ini_path(base_dir: str | Path) -> Path:
    return get_obs_config_dir(base_dir) / "global.ini"


def get_obs_websocket_config_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / "config" / "obs-studio" / "plugin_config" / "obs-websocket" / "config.json"


def get_portable_marker_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / PORTABLE_OBS_MARKER_NAME


def get_legacy_marker_path(base_dir: str | Path) -> Path:
    return Path(base_dir) / LEGACY_PORTABLE_OBS_MARKER_NAME


def new_obs_ini_parser() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    return parser


def read_obs_ini_parser(path: Path) -> tuple[configparser.ConfigParser, bool]:
    """BOMなしUTF-8として読み、混入BOMは除去対象として検出する。"""
    parser = new_obs_ini_parser()
    text = path.read_text(encoding="utf-8")
    had_bom = text.startswith("\ufeff")
    if had_bom:
        text = text.lstrip("\ufeff")
    parser.read_string(text)
    return parser, had_bom


class OBSBootstrapper:
    """ポータブルOBSの検査と修復を分離して扱う。"""

    def __init__(
        self,
        base_dir: str | Path,
        process_manager: OBSProcessManager | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.process_manager = process_manager or OBSProcessManager(self.base_dir)
        self.logger = logger or LOGGER

    @property
    def obs_exe(self) -> Path:
        return get_obs_executable_path(self.base_dir)

    def check(self) -> BootstrapReport:
        global_ini = get_obs_global_ini_path(self.base_dir)
        parse_error = None
        missing = []
        if global_ini.exists():
            try:
                parser, had_bom = read_obs_ini_parser(global_ini)
                if had_bom:
                    missing.append("encoding.BOM")
                if not parser.has_section(TRAY_SETTINGS_SECTION):
                    missing.extend(f"{TRAY_SETTINGS_SECTION}.{key}" for key in TRAY_SETTINGS)
                else:
                    for key, value in TRAY_SETTINGS.items():
                        if parser.get(TRAY_SETTINGS_SECTION, key, fallback=None) != value:
                            missing.append(f"{TRAY_SETTINGS_SECTION}.{key}")
                    for section in parser.sections():
                        for key in parser.options(section):
                            lower_key = key.lower()
                            allowed = section == TRAY_SETTINGS_SECTION and key in TRAY_SETTINGS
                            if ("systray" in lower_key or "hidetray" in lower_key) and not allowed:
                                missing.append(f"{section}.{key}")
            except Exception as e:
                parse_error = f"{type(e).__name__}: {e}"

        return BootstrapReport(
            obs_dir=self.base_dir,
            obs_exe=self.obs_exe,
            portable_marker_exists=get_portable_marker_path(self.base_dir).exists(),
            legacy_marker_exists=get_legacy_marker_path(self.base_dir).exists(),
            config_dir_exists=get_obs_config_dir(self.base_dir).exists(),
            global_ini_exists=global_ini.exists(),
            global_ini_parse_error=parse_error,
            missing_tray_settings=tuple(missing),
        )

    def apply(self, port: int | None = None, password: str = "") -> dict[str, Any]:
        marker = self.ensure_portable_mode_marker()
        config_dir = self.ensure_config_dir()
        changed_ini, global_ini_path = self.ensure_global_ini()
        websocket_result = None
        if port is not None:
            websocket_result = self.ensure_websocket_config(port, password)
        return {
            "marker": marker,
            "config_dir": config_dir,
            "global_ini_changed": changed_ini,
            "global_ini_path": global_ini_path,
            "websocket": websocket_result,
        }

    def bootstrap(self, port: int | None = None, password: str = "") -> dict[str, Any]:
        """Backward-compatible alias for the full repair/setup flow."""
        return self.apply(port=port, password=password)

    def ensure_portable_mode_marker(self) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        primary_marker = get_portable_marker_path(self.base_dir)
        if not primary_marker.exists():
            primary_marker.write_text("", encoding="utf-8")

        legacy_marker = get_legacy_marker_path(self.base_dir)
        if not legacy_marker.exists():
            legacy_marker.write_text("", encoding="utf-8")
        return primary_marker

    def ensure_config_dir(self) -> Path:
        config_dir = get_obs_config_dir(self.base_dir)
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir

    def ensure_global_ini(self) -> tuple[bool, Path]:
        # OBS reads global.ini only at startup and may rewrite it on exit.
        # Stop every process from this managed portable tree before patching.
        self.process_manager.kill_stale_managed_processes()
        ini_path = get_obs_global_ini_path(self.base_dir)
        ini_path.parent.mkdir(parents=True, exist_ok=True)
        parser = new_obs_ini_parser()

        parse_failed = False
        normalized_encoding = False
        if ini_path.exists():
            try:
                parser, normalized_encoding = read_obs_ini_parser(ini_path)
            except Exception as e:
                self.logger.warning("Corrupt OBS global.ini will be regenerated: %s (%s)", ini_path, e)
                parse_failed = True
                ini_path.unlink(missing_ok=True)
                self.regenerate_global_ini_with_obs(ini_path)
                parser = new_obs_ini_parser()
                parser, normalized_encoding = read_obs_ini_parser(ini_path)

        changed = parse_failed or normalized_encoding
        for section in parser.sections():
            for key in list(parser.options(section)):
                lower_key = key.lower()
                allowed = section == TRAY_SETTINGS_SECTION and key in TRAY_SETTINGS
                if ("systray" in lower_key or "hidetray" in lower_key) and not allowed:
                    parser.remove_option(section, key)
                    changed = True

        if not parser.has_section(TRAY_SETTINGS_SECTION):
            parser.add_section(TRAY_SETTINGS_SECTION)
            changed = True

        for key, value in TRAY_SETTINGS.items():
            current = parser.get(TRAY_SETTINGS_SECTION, key, fallback=None)
            if current is None or str(current).strip().lower() != value:
                parser.set(TRAY_SETTINGS_SECTION, key, value)
                changed = True

        if changed or not ini_path.exists():
            with open(ini_path, "w", encoding="utf-8") as f:
                parser.write(f, space_around_delimiters=False)
        return changed, ini_path

    def regenerate_global_ini_with_obs(self, ini_path: Path, timeout_sec: float = 8.0) -> None:
        process = self.process_manager.start_obs(env=self.process_manager.isolated_env(), hidden=True)
        try:
            import time

            deadline = time.monotonic() + timeout_sec
            while time.monotonic() < deadline:
                if ini_path.exists():
                    return
                if process.poll() is not None:
                    break
                time.sleep(0.25)
        finally:
            self.process_manager.terminate_process(process)
            self.process_manager.kill_stale_owned_processes()

        if not ini_path.exists():
            raise RuntimeError(f"OBS did not regenerate global.ini: {ini_path}")

    def ensure_websocket_config(self, port: int, password: str) -> tuple[bool, Path]:
        config_path = get_obs_websocket_config_path(self.base_dir)
        config_path.parent.mkdir(parents=True, exist_ok=True)

        data = {}
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
            except Exception as e:
                self.logger.warning("OBS websocket config was unreadable and will be reset: %s", e, exc_info=True)
                data = {}

        changed = False

        def set_if_diff(key: str, value: Any) -> None:
            nonlocal changed
            if data.get(key) != value:
                data[key] = value
                changed = True

        port_value = max(1, min(65535, int(port)))
        password_text = str(password or "")

        set_if_diff("server_enabled", True)
        set_if_diff("server_port", port_value)
        set_if_diff("auth_required", bool(password_text))
        set_if_diff("server_password", password_text)

        if changed:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        return changed, config_path
