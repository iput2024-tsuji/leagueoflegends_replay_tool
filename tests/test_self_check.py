import io
import json

import main as app_entrypoint
from src import self_check


def test_self_check_passes_with_missing_optional_binaries(monkeypatch, tmp_path):
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
    monkeypatch.setattr(self_check, "has_mpv_dll", lambda _bin_dir, _app_root: False)

    report = self_check.run_self_check()

    assert report["ok"] is True
    statuses = {check["name"]: check["status"] for check in report["checks"]}
    assert statuses["analytics_runtime"] == "ok"
    assert statuses["config"] == "ok"
    assert statuses["recording_dirs"] == "ok"
    assert statuses["mpv_dll"] == "warning"


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
