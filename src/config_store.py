from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from .app_paths import get_app_root
except ImportError:
    from app_paths import get_app_root


ROOT_DIR = get_app_root()
CONFIG_PATH = ROOT_DIR / "config" / "setting.json"
SAMPLE_CONFIG_PATH = ROOT_DIR / "config" / "setting.sample.json"


class ConfigRepository:
    """setting.json の読み書きを集約する。"""

    def __init__(self, config_path: Path = CONFIG_PATH, sample_path: Path = SAMPLE_CONFIG_PATH) -> None:
        self.config_path = config_path
        self.sample_path = sample_path

    def load(self, create_if_missing: bool = True) -> dict[str, Any]:
        if not self.config_path.exists():
            if not create_if_missing:
                raise FileNotFoundError(f"setting.json was not found: {self.config_path}")
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            if self.sample_path.exists():
                self.config_path.write_text(self.sample_path.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                self.config_path.write_text(json.dumps({}, indent=4), encoding="utf-8")

        try:
            with open(self.config_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, data: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
