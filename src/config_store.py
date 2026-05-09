from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

try:
    from .app_paths import get_app_root
except ImportError:
    from app_paths import get_app_root


ROOT_DIR = get_app_root()
CONFIG_PATH = ROOT_DIR / "config" / "setting.json"
SAMPLE_CONFIG_PATH = ROOT_DIR / "config" / "setting.sample.json"
_CONFIG_LOCK = threading.RLock()


class ConfigRepository:
    """setting.json の読み書きを集約する。"""

    def __init__(self, config_path: Path = CONFIG_PATH, sample_path: Path = SAMPLE_CONFIG_PATH) -> None:
        self.config_path = config_path
        self.sample_path = sample_path

    def load(self, create_if_missing: bool = True) -> dict[str, Any]:
        with _CONFIG_LOCK:
            if not self.config_path.exists():
                if not create_if_missing:
                    raise FileNotFoundError(f"setting.json was not found: {self.config_path}")
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                if self.sample_path.exists():
                    self._write_text_atomic(self.sample_path.read_text(encoding="utf-8"))
                else:
                    self._write_text_atomic(json.dumps({}, indent=4))

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
