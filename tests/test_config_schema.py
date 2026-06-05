from src import config_schema


def test_normalize_config_migrates_capture_settings_and_generates_password():
    cfg = {
        "obs": {
            "password": "",
            "game_capture_name": "legacy_capture",
            "game_capture_window": "Legacy Window:LegacyClass:Legacy.exe",
        }
    }

    result = config_schema.normalize_config(cfg, password_factory=lambda: "secret-password-123456")

    obs = result.config["obs"]
    assert result.changed is True
    assert obs["password"] == "secret-password-123456"
    assert obs["window_capture_name"] == "legacy_capture"
    assert obs["window_capture_window"] == "Legacy Window:LegacyClass:Legacy.exe"
    assert obs["window_capture_method"] == config_schema.DEFAULT_OBS_WINDOW_CAPTURE_METHOD
    assert "game_capture_name" not in obs
    assert "game_capture_window" not in obs


def test_normalize_config_reports_errors_without_auto_fix():
    result = config_schema.normalize_config({"obs": {"password": ""}}, auto_fix=False)

    assert result.errors
    assert any("OBS WebSocketパスワード" in error for error in result.errors)
    assert result.config["obs"]["password"] == ""
