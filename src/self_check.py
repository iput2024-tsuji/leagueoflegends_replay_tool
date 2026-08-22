from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

try:
    from . import config_schema, recordtest
    from .app_paths import get_app_root, get_user_data_root
    from .config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
    from .ffmpeg_support import manual_setup_message, resolve_ffmpeg_executable
    from .mpv_support import has_mpv_dll
except ImportError:
    import config_schema
    import recordtest
    from app_paths import get_app_root, get_user_data_root
    from config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
    from ffmpeg_support import manual_setup_message, resolve_ffmpeg_executable
    from mpv_support import has_mpv_dll


def run_self_check() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    app_root = get_app_root()
    data_root = get_user_data_root()
    _add_check(checks, "app_root", app_root.exists(), f"app_root={app_root}")
    _add_check(checks, "data_root", _is_writable_directory(data_root), f"data_root={data_root}")

    try:
        from . import analytics

        _add_check(
            checks,
            "analytics_runtime",
            callable(analytics.DecisionTreeClassifier),
            "scikit-learn native runtime loaded",
        )
    except Exception as e:
        _add_check(
            checks,
            "analytics_runtime",
            False,
            f"{type(e).__name__}: {e}",
            fatal=True,
        )

    try:
        _add_check(checks, "native_modules", True, _native_runtime_summary())
    except Exception as e:
        _add_check(
            checks,
            "native_modules",
            False,
            f"{type(e).__name__}: {e}",
            fatal=True,
        )

    config_data: dict[str, Any] = {}
    app_config = None
    try:
        repository = ConfigRepository(CONFIG_PATH, SAMPLE_CONFIG_PATH)
        config_data = repository.load(create_if_missing=True)
        normalized = config_schema.normalize_config(
            config_data,
            auto_fix=True,
            password_factory=recordtest.generate_obs_password,
        )
        if normalized.changed:
            repository.save(normalized.config)
            config_data = normalized.config
        app_config = recordtest.AppConfig.from_dict(config_data)
        _add_check(checks, "config", True, f"config_path={CONFIG_PATH}")
    except Exception as e:
        _add_check(checks, "config", False, f"{type(e).__name__}: {e}", fatal=True)

    if app_config is not None:
        _add_check(
            checks,
            "recording_dirs",
            _is_writable_directory(app_config.paths.recordings_dir)
            and _is_writable_directory(app_config.paths.json_dir),
            f"recordings={app_config.paths.recordings_dir}; json={app_config.paths.json_dir}",
        )
        _add_check(
            checks,
            "mpv_dll",
            has_mpv_dll(app_config.paths.bin_dir, app_root),
            f"bin_dir={app_config.paths.bin_dir}",
            fatal=False,
            level="warning",
        )
        ffmpeg_path = resolve_ffmpeg_executable(
            explicit_path=app_config.paths.ffmpeg_executable,
            bin_dir=app_config.paths.bin_dir,
            app_root=app_root,
        )
        ffmpeg_message = (
            f"ffmpeg={ffmpeg_path}"
            if ffmpeg_path
            else manual_setup_message(app_config.paths.bin_dir).replace("\n", " ")
        )
        _add_check(
            checks,
            "ffmpeg",
            ffmpeg_path is not None,
            ffmpeg_message,
            fatal=False,
            level="warning",
        )

    try:
        from scripts import setup_env

        obs_ready = setup_env.is_environment_ready()
        _add_check(
            checks,
            "obs_portable",
            obs_ready,
            (
                f"obs_dir={setup_env.OBS_PORTABLE_DIR}"
                if obs_ready
                else setup_env.obs_manual_setup_message().replace("\n", " ")
            ),
            fatal=False,
            level="warning",
        )
    except Exception as e:
        _add_check(checks, "obs_portable", False, f"{type(e).__name__}: {e}", fatal=False, level="warning")

    fatal_errors = [check for check in checks if check["fatal"] and check["status"] != "ok"]
    return {
        "ok": not fatal_errors,
        "app_root": str(app_root),
        "data_root": str(data_root),
        "checks": checks,
    }


def format_self_check_report(report: dict[str, Any]) -> str:
    lines = ["LoLReplayTool self-check", f"status: {'ok' if report.get('ok') else 'failed'}"]
    for check in report.get("checks", []):
        status = check.get("status")
        name = check.get("name")
        message = check.get("message")
        lines.append(f"- {status}: {name}: {message}")
    return "\n".join(lines)


def self_check_exit_code(report: dict[str, Any]) -> int:
    return 0 if report.get("ok") else 1


def report_as_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False)


def _add_check(
    checks: list[dict[str, Any]],
    name: str,
    ok: bool,
    message: str,
    *,
    fatal: bool = True,
    level: str = "error",
) -> None:
    checks.append(
        {
            "name": name,
            "status": "ok" if ok else level,
            "fatal": bool(fatal),
            "message": message,
        }
    )


def _native_runtime_summary() -> str:
    import cv2
    import numpy as np
    import pandas as pd
    import sklearn
    from PyQt6 import QtCore
    from sklearn.tree import DecisionTreeClassifier

    fft_real = tuple(float(value.real) for value in np.fft.fft([1.0, 2.0, 3.0, 4.0]))
    if fft_real != (10.0, -2.0, -2.0, -2.0):
        raise RuntimeError(f"NumPy FFT returned an unexpected result: {fft_real}")

    rolling = pd.Series([1.0, 2.0, 3.0]).rolling(2).mean().tolist()
    if rolling[1:] != [1.5, 2.5]:
        raise RuntimeError(f"pandas rolling returned an unexpected result: {rolling}")

    model = DecisionTreeClassifier(random_state=0).fit([[0.0], [1.0]], [0, 1])
    prediction = int(model.predict([[1.0]])[0])
    if prediction != 1:
        raise RuntimeError(
            f"scikit-learn prediction returned an unexpected result: {prediction}"
        )

    gray = cv2.cvtColor(np.zeros((2, 2, 3), dtype=np.uint8), cv2.COLOR_BGR2GRAY)
    if gray.shape != (2, 2):
        raise RuntimeError(f"OpenCV conversion returned an unexpected shape: {gray.shape}")

    return (
        f"PyQt6/Qt {QtCore.qVersion()}; NumPy {np.__version__}; "
        f"pandas {pd.__version__}; scikit-learn {sklearn.__version__}; "
        f"OpenCV {cv2.__version__}"
    )


def _is_writable_directory(path: str | Path) -> bool:
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".self-check-", dir=target, delete=False) as tmp:
            tmp.write(b"ok")
            tmp_path = Path(tmp.name)
        tmp_path.unlink(missing_ok=True)
        return True
    except Exception:
        return False
