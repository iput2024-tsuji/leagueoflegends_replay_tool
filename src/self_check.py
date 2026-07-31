from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

try:
    from . import config_schema, recordtest
    from .app_paths import get_app_root, get_user_data_root
    from .config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
    from .mpv_support import has_mpv_dll
except ImportError:
    import config_schema
    import recordtest
    from app_paths import get_app_root, get_user_data_root
    from config_store import CONFIG_PATH, SAMPLE_CONFIG_PATH, ConfigRepository
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
        _add_check(
            checks,
            "ffmpeg",
            (app_config.paths.bin_dir / "ffmpeg.exe").exists(),
            f"ffmpeg={app_config.paths.bin_dir / 'ffmpeg.exe'}",
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
            f"obs_dir={setup_env.OBS_PORTABLE_DIR}",
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
