from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

try:
    from .app_paths import get_app_root, get_resource_root, get_user_data_root
except ImportError:
    from app_paths import get_app_root, get_resource_root, get_user_data_root


ROOT_DIR = get_app_root()
RESOURCE_DIR = get_resource_root()
DATA_DIR = get_user_data_root()
CONFIG_PATH = DATA_DIR / "config" / "setting.json"
SAMPLE_CONFIG_PATH = RESOURCE_DIR / "config" / "setting.sample.json"
CHAMPION_ALIASES_PATH = DATA_DIR / "config" / "champion_aliases.json"
SAMPLE_CHAMPION_ALIASES_PATH = RESOURCE_DIR / "config" / "champion_aliases.json"
_CONFIG_LOCK = threading.RLock()


def _default_legacy_config_paths(config_path: Path) -> tuple[Path, ...]:
    try:
        if config_path.resolve() != CONFIG_PATH.resolve():
            return ()
    except Exception:
        return ()

    legacy_path = ROOT_DIR / "config" / "setting.json"
    try:
        if legacy_path.resolve() == config_path.resolve():
            return ()
    except Exception:
        pass
    return (legacy_path,)


class ConfigRepository:
    """setting.json の読み書きを集約する。"""

    def __init__(
        self,
        config_path: Path = CONFIG_PATH,
        sample_path: Path = SAMPLE_CONFIG_PATH,
        legacy_config_paths: tuple[Path, ...] | None = None,
    ) -> None:
        self.config_path = config_path
        self.sample_path = sample_path
        self.legacy_config_paths = (
            legacy_config_paths if legacy_config_paths is not None else _default_legacy_config_paths(config_path)
        )

    def load(self, create_if_missing: bool = True) -> dict[str, Any]:
        with _CONFIG_LOCK:
            if not self.config_path.exists():
                if not create_if_missing:
                    raise FileNotFoundError(f"setting.json was not found: {self.config_path}")
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                legacy_config = self._find_legacy_config()
                if legacy_config is not None:
                    self._write_text_atomic(legacy_config.read_text(encoding="utf-8"))
                elif self.sample_path.exists():
                    self._write_text_atomic(self.sample_path.read_text(encoding="utf-8"))
                else:
                    self._write_text_atomic(json.dumps({}, indent=4))
                self._copy_auxiliary_config_files()

            try:
                with open(self.config_path, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
            except Exception:
                self._backup_invalid_config()
                return {}

    def save(self, data: dict[str, Any]) -> None:
        text = json.dumps(data, indent=4, ensure_ascii=False)
        with _CONFIG_LOCK:
            self._write_text_atomic(text)
            self._copy_auxiliary_config_files()

    def _find_legacy_config(self) -> Path | None:
        for path in self.legacy_config_paths:
            try:
                if path.exists() and path.resolve() != self.config_path.resolve():
                    return path
            except Exception:
                continue
        return None

    def _copy_auxiliary_config_files(self) -> None:
        source_path = self.sample_path.parent / "champion_aliases.json"
        target_path = self.config_path.parent / "champion_aliases.json"
        if not source_path.exists() or target_path.exists():
            return
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            text = source_path.read_text(encoding="utf-8")
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    def _write_text_atomic(self, text: str) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config_path.with_name(f"{self.config_path.name}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.config_path)

    def _backup_invalid_config(self) -> None:
        if not self.config_path.exists():
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        backup_path = self.config_path.with_name(f"{self.config_path.name}.{timestamp}.invalid")
        try:
            os.replace(self.config_path, backup_path)
        except Exception:
            pass
