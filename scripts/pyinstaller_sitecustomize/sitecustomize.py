"""Keep PyInstaller dependency discovery on the build script's fixed PATH."""

import os

_GUARDED_PATH = os.environ.get("LOL_REPLAY_PYINSTALLER_PATH")
if _GUARDED_PATH:
    os.environ["PATH"] = _GUARDED_PATH
