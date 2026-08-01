import os
import sys
from pathlib import Path

APP_NAME = "LoLReplayTool"


def _lexical_absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def get_app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_resource_root() -> Path:
    """Return the root containing read-only files bundled with the app."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and bundle_root:
        return Path(bundle_root).resolve()
    return get_app_root()


def get_user_data_root() -> Path:
    override = os.environ.get("LOL_REPLAY_TOOL_DATA_DIR")
    if override:
        return _lexical_absolute(override)

    if getattr(sys, "frozen", False) and os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return _lexical_absolute(Path(local_appdata) / APP_NAME)
        return _lexical_absolute(Path.home() / "AppData" / "Local" / APP_NAME)

    return get_app_root()
