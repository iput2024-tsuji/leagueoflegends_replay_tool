import json

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
