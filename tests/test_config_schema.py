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
    assert obs["base_width"] == 1920
    assert obs["base_height"] == 1080
    assert obs["output_width"] == 1920
    assert obs["output_height"] == 1080
    assert obs["scale_type"] == "lanczos"
    assert obs["recording_quality"] == "Small"
    assert "game_capture_name" not in obs
    assert "game_capture_window" not in obs


def test_normalize_config_reports_errors_without_auto_fix():
    result = config_schema.normalize_config({"obs": {"password": ""}}, auto_fix=False)

    assert result.errors
    assert any("OBS WebSocketパスワード" in error for error in result.errors)
    assert result.config["obs"]["password"] == ""


def test_normalize_config_repairs_invalid_video_quality_settings():
    cfg = {
        "obs": {
            "password": "secret",
            "base_width": 1919,
            "base_height": 20,
            "output_width": 99999,
            "output_height": "invalid",
            "scale_type": "nearest",
            "recording_quality": "unknown",
        }
    }

    result = config_schema.normalize_config(cfg)

    obs = result.config["obs"]
    assert obs["base_width"] == config_schema.DEFAULT_OBS_BASE_WIDTH
    assert obs["base_height"] == config_schema.DEFAULT_OBS_BASE_HEIGHT
    assert obs["output_width"] == config_schema.DEFAULT_OBS_OUTPUT_WIDTH
    assert obs["output_height"] == config_schema.DEFAULT_OBS_OUTPUT_HEIGHT
    assert obs["scale_type"] == config_schema.DEFAULT_OBS_SCALE_TYPE
    assert obs["recording_quality"] == config_schema.DEFAULT_OBS_RECORDING_QUALITY
