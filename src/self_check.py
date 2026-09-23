from __future__ import annotations

import json
import os
import sys
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

    positive_time, negative_time = _synthetic_sync_probe(cv2, np)
    if not abs(positive_time - 1.0) <= 0.05:
        raise RuntimeError(f"Synthetic sync marker was found at {positive_time:.3f}s, expected 1.000s")
    if negative_time != -1.0:
        raise RuntimeError(f"Synthetic absent sync marker returned {negative_time:.3f}s")

    qt_summary = _qt_runtime_summary(QtCore)
    return (
        f"{qt_summary}; NumPy {np.__version__}; "
        f"pandas {pd.__version__}; scikit-learn {sklearn.__version__}; "
        f"OpenCV {cv2.__version__}; sync marker probe ok (1.000s/-1.000s)"
    )


def _synthetic_sync_probe(cv2: Any, np: Any) -> tuple[float, float]:
    from .player import SyncWorker

    with tempfile.TemporaryDirectory(prefix=".self-check-video-") as directory:
        positive_path = Path(directory) / "positive.avi"
        negative_path = Path(directory) / "negative.avi"
        for path, marker_frame in ((positive_path, 30), (negative_path, None)):
            writer = cv2.VideoWriter(
                str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (320, 240)
            )
            try:
                if not writer.isOpened() or writer.getBackendName() != "FFMPEG":
                    backend = writer.getBackendName() if writer.isOpened() else "unopened"
                    raise RuntimeError(f"OpenCV MJPG writer backend is {backend}, expected FFMPEG")
                for frame_index in range(60):
                    frame = np.zeros((240, 320, 3), dtype=np.uint8)
                    if frame_index == marker_frame:
                        frame[:140, :140] = (0, 0, 255)
                    writer.write(frame)
            finally:
                writer.release()

            capture = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
            try:
                if not capture.isOpened() or capture.getBackendName() != "FFMPEG":
                    backend = capture.getBackendName() if capture.isOpened() else "unopened"
                    raise RuntimeError(f"OpenCV synthetic video backend is {backend}, expected FFMPEG")
                if not capture.grab() or capture.retrieve()[0] is not True:
                    raise RuntimeError(f"OpenCV synthetic video could not be read: {path}")
            finally:
                capture.release()

        def find_marker(path: Path) -> float:
            result: list[float] = []
            worker = SyncWorker(path, max_seconds=3)
            worker.finished.connect(result.append)
            worker.run()
            if not result:
                raise RuntimeError("SyncWorker did not emit a result")
            return result[0]

        return find_marker(positive_path), find_marker(negative_path)


def _qt_runtime_summary(QtCore: Any) -> str:
    application = QtCore.QCoreApplication.instance()
    if application is None:
        application = QtCore.QCoreApplication(["LoLReplayTool-self-check"])

    locale = QtCore.QLocale("ja_JP")
    number_text = locale.toString(1234.5, "f", 1)
    date_text = locale.toString(
        QtCore.QDate(2026, 8, 30),
        QtCore.QLocale.FormatType.LongFormat,
    )
    if not number_text or not date_text:
        raise RuntimeError("Qt locale formatting returned an empty result")

    collator = QtCore.QCollator(locale)
    if collator.compare("あ", "い") >= 0:
        raise RuntimeError("Qt Japanese collation returned an unexpected order")

    boundary_finder = QtCore.QTextBoundaryFinder(
        QtCore.QTextBoundaryFinder.BoundaryType.Word,
        "録画テスト replay",
    )
    if boundary_finder.toNextBoundary() <= 0:
        raise RuntimeError("Qt Unicode boundary detection returned no boundary")

    summary = f"PyQt6/Qt {QtCore.qVersion()} (locale/collation/Unicode ok)"
    if os.name == "nt":
        summary += f"; {_windows_system_icu_summary()}"
    return summary


def _windows_system_icu_summary() -> str:
    build = _windows_build_number()
    if build < 22000:
        raise RuntimeError(
            f"Windows build {build} is below the supported Windows 11 minimum 22000"
        )
    system_directory = _windows_system_directory()
    loaded_path = _loaded_windows_module_path("icuuc.dll")
    expected_path = os.path.join(system_directory, "icuuc.dll")
    actual_canonical = _canonical_path(loaded_path)
    expected_canonical = _canonical_path(expected_path)
    if actual_canonical != expected_canonical:
        raise RuntimeError(
            "Qt loaded icuuc.dll outside the Windows system directory: "
            f"actual={loaded_path}; expected={expected_path}"
        )
    return f"Windows build {build}; icuuc.dll={loaded_path}"


def _windows_build_number() -> int:
    return int(sys.getwindowsversion().build)


def _windows_system_directory() -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_system_directory = kernel32.GetSystemDirectoryW
    get_system_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    get_system_directory.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_system_directory(buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise OSError(
            ctypes.get_last_error(),
            "GetSystemDirectoryW failed or returned a truncated path",
        )
    return buffer.value


def _loaded_windows_module_path(module_name: str) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_module_handle = kernel32.GetModuleHandleW
    get_module_handle.argtypes = [wintypes.LPCWSTR]
    get_module_handle.restype = wintypes.HMODULE
    handle = get_module_handle(module_name)
    if not handle:
        raise OSError(
            ctypes.get_last_error(),
            f"{module_name} is not loaded in the self-check process",
        )

    get_module_filename = kernel32.GetModuleFileNameW
    get_module_filename.argtypes = [
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    get_module_filename.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_module_filename(handle, buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        raise OSError(
            ctypes.get_last_error(),
            f"GetModuleFileNameW failed or truncated {module_name}",
        )
    return buffer.value


def _canonical_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


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
