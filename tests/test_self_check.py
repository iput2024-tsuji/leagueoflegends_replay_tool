import io
import json

import pytest

import main as app_entrypoint
from src import self_check


@pytest.fixture
def isolated_self_check(monkeypatch, tmp_path):
    config_path = tmp_path / "config" / "setting.json"
    sample_path = tmp_path / "install" / "config" / "setting.sample.json"
    sample_path.parent.mkdir(parents=True)
    sample_path.write_text(
        json.dumps(
            {
                "obs": {"password": "secret-password", "dir": "obs-portable"},
                "paths": {
                    "bin_dir": str(tmp_path / "bin"),
                    "recordings_dir": str(tmp_path / "recordings"),
                    "json_dir": str(tmp_path / "recordings" / "json"),
                    "champion_icons_dir": str(tmp_path / "champion-icons"),
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(self_check, "CONFIG_PATH", config_path)
    monkeypatch.setattr(self_check, "SAMPLE_CONFIG_PATH", sample_path)
    monkeypatch.setattr(self_check, "get_user_data_root", lambda: tmp_path / "userdata")
    return tmp_path


def test_self_check_passes_with_missing_optional_binaries(isolated_self_check, monkeypatch):
    monkeypatch.setattr(self_check, "has_mpv_dll", lambda _bin_dir, _app_root: False)

    report = self_check.run_self_check()

    assert report["ok"] is True
    statuses = {check["name"]: check["status"] for check in report["checks"]}
    assert statuses["analytics_runtime"] == "ok"
    assert statuses["native_modules"] == "ok"
    assert statuses["config"] == "ok"
    assert statuses["recording_dirs"] == "ok"
    assert statuses["mpv_dll"] == "warning"


def test_native_runtime_summary_exercises_locked_native_modules():
    summary = self_check._native_runtime_summary()

    assert "PyQt6/Qt 6.10.2" in summary
    assert "locale/collation/Unicode ok" in summary
    assert "NumPy 2.4.1" in summary
    assert "pandas 3.0.2" in summary
    assert "scikit-learn 1.8.0" in summary
    assert "OpenCV 4.13.0" in summary
    if self_check.os.name == "nt":
        assert "Windows build" in summary
        assert "icuuc.dll=" in summary


def test_synthetic_sync_probe_finds_marker_and_rejects_absent_marker():
    import cv2
    import numpy as np

    found_time, absent_time = self_check._synthetic_sync_probe(cv2, np)

    assert found_time == pytest.approx(1.0, abs=0.05)
    assert absent_time == -1.0


@pytest.mark.parametrize("factory", ["VideoWriter", "VideoCapture"])
def test_synthetic_sync_probe_rejects_unexpected_backend(monkeypatch, factory):
    import cv2
    import numpy as np

    released = []

    class UnexpectedBackend:
        def isOpened(self):
            return True

        def getBackendName(self):
            return "MSMF"

        def release(self):
            released.append(True)

    monkeypatch.setattr(cv2, factory, lambda *args, **kwargs: UnexpectedBackend())

    with pytest.raises(RuntimeError, match="expected FFMPEG"):
        self_check._synthetic_sync_probe(cv2, np)
    assert released == [True]


@pytest.mark.parametrize("result", [(float("nan"), -1.0), (0.0, -1.0), (1.0, 0.0)])
def test_native_runtime_summary_rejects_invalid_sync_result(monkeypatch, result):
    monkeypatch.setattr(self_check, "_synthetic_sync_probe", lambda *_args: result)
    with pytest.raises(RuntimeError, match="Synthetic"):
        self_check._native_runtime_summary()


def test_native_runtime_failure_is_fatal(isolated_self_check, monkeypatch):
    monkeypatch.setattr(
        self_check,
        "_native_runtime_summary",
        lambda: (_ for _ in ()).throw(RuntimeError("native probe failed")),
    )

    report = self_check.run_self_check()

    native_check = next(check for check in report["checks"] if check["name"] == "native_modules")
    assert report["ok"] is False
    assert native_check["fatal"] is True
    assert native_check["status"] == "error"
    assert "native probe failed" in native_check["message"]


def test_windows_system_icu_summary_accepts_loaded_system_copy(
    monkeypatch, tmp_path
):
    system_directory = tmp_path / "Windows" / "System32"
    system_directory.mkdir(parents=True)
    icu_path = system_directory / "icuuc.dll"
    icu_path.write_bytes(b"system ICU")
    monkeypatch.setattr(self_check, "_windows_build_number", lambda: 22631)
    monkeypatch.setattr(
        self_check, "_windows_system_directory", lambda: str(system_directory)
    )
    monkeypatch.setattr(
        self_check, "_loaded_windows_module_path", lambda _name: str(icu_path)
    )

    summary = self_check._windows_system_icu_summary()

    assert "Windows build 22631" in summary
    assert f"icuuc.dll={icu_path}" in summary


def test_windows_system_icu_summary_rejects_app_local_copy(monkeypatch, tmp_path):
    system_directory = tmp_path / "Windows" / "System32"
    system_directory.mkdir(parents=True)
    app_local = tmp_path / "app" / "icuuc.dll"
    app_local.parent.mkdir()
    app_local.write_bytes(b"app-local ICU")
    monkeypatch.setattr(self_check, "_windows_build_number", lambda: 22631)
    monkeypatch.setattr(
        self_check, "_windows_system_directory", lambda: str(system_directory)
    )
    monkeypatch.setattr(
        self_check, "_loaded_windows_module_path", lambda _name: str(app_local)
    )

    with pytest.raises(RuntimeError, match="outside the Windows system directory"):
        self_check._windows_system_icu_summary()


def test_windows_system_icu_summary_rejects_pre_windows_11(monkeypatch):
    monkeypatch.setattr(self_check, "_windows_build_number", lambda: 19045)

    with pytest.raises(RuntimeError, match="below the supported Windows 11"):
        self_check._windows_system_icu_summary()


def test_self_check_cli_reconfigures_cp1252_streams_to_utf8(monkeypatch):
    report = {"ok": True}
    monkeypatch.setattr(self_check, "run_self_check", lambda: report)
    monkeypatch.setattr(self_check, "format_self_check_report", lambda _report: "セルフチェック正常")
    monkeypatch.setattr(self_check, "self_check_exit_code", lambda _report: 0)
    stdout_buffer = io.BytesIO()
    stderr_buffer = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_buffer, encoding="cp1252", errors="strict")
    stderr = io.TextIOWrapper(stderr_buffer, encoding="cp1252", errors="strict")
    monkeypatch.setattr(app_entrypoint.sys, "stdout", stdout)
    monkeypatch.setattr(app_entrypoint.sys, "stderr", stderr)

    exit_code = app_entrypoint.main(["--self-check"])
    print("標準エラー日本語", file=stderr)
    stdout.flush()
    stderr.flush()

    assert exit_code == 0
    assert stdout.encoding == "utf-8"
    assert stderr.encoding == "utf-8"
    assert stdout.errors == "backslashreplace"
    assert stderr.errors == "backslashreplace"
    assert stdout_buffer.getvalue().decode("utf-8").splitlines() == ["セルフチェック正常"]
    assert stderr_buffer.getvalue().decode("utf-8").splitlines() == ["標準エラー日本語"]
