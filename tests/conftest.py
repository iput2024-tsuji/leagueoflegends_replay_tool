import copy
from unittest.mock import patch

import pytest

TEST_SETTINGS = {
    "obs": {
        "host": "localhost",
        "port": 4455,
        "fps": 60,
        "password": "",
        "scene_name": "lol_seen_test",
        "source_name": "sync_marker_test",
        "source_color": "#FF0000",
        "dir": "tests/_tmp/obs-portable",
    },
    "paths": {
        "bin_dir": "tests/_tmp/bin",
        "recordings_dir": "tests/_tmp/recordings",
        "json_dir": "tests/_tmp/recordings/json",
        "champion_icons_dir": "tests/_tmp/assets/champions/icons",
        "champion_aliases_path": "tests/_tmp/config/champion_aliases.json",
    },
    "polling": {
        "end_error_limit": 3,
        "end_poll_sec": 0.1,
        "event_poll_sec": 0.1,
    },
    "storage": {
        "max_size_gb": 1,
    },
    "audio": {
        "mic": {
            "input_name": "lol_mic_audio",
            "device_id": "default",
            "device_name": "Default",
            "volume_db": 0.0,
            "mute": False,
        },
    },
    "app": {
        "setup_completed": True,
        "minimize_to_tray": False,
    },
}


@pytest.fixture(autouse=True)
def mock_recordtest_settings():
    """Keep tests independent from local config/setting.json."""

    def load_test_settings():
        return copy.deepcopy(TEST_SETTINGS)

    with patch("src.recordtest.load_settings", side_effect=load_test_settings):
        yield
