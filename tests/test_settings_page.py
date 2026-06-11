from src import app
from src.app import SettingsPage


class FakeSettingsPage:
    def __init__(self) -> None:
        self._audio_auto_refreshed_once = False
        self.calls = []

    def refresh_audio_devices(self, *, show_message=True, show_error=True, auto_launch=True):
        self.calls.append(
            {
                "show_message": show_message,
                "show_error": show_error,
                "auto_launch": auto_launch,
            }
        )
        return True

    def apply_audio_settings_to_obs(self, *, show_success=True, show_error=True, auto_launch=True):
        self.calls.append(
            {
                "show_success": show_success,
                "show_error": show_error,
                "auto_launch": auto_launch,
            }
        )
        return True


def test_settings_page_initial_audio_refresh_auto_launches_managed_obs():
    page = FakeSettingsPage()
    SettingsPage.on_page_shown(page)

    assert page._audio_auto_refreshed_once is True
    assert page.calls == [{"show_message": False, "show_error": False, "auto_launch": True}]


def test_settings_page_audio_auto_apply_auto_launches_managed_obs():
    page = FakeSettingsPage()

    SettingsPage._apply_audio_settings_auto(page)

    assert page.calls == [{"show_success": False, "show_error": False, "auto_launch": True}]


def test_settings_page_supports_fractional_high_fps(qtbot, monkeypatch):
    config = {
        "obs": {
            "password": "secret",
            "fps_numerator": 240000,
            "fps_denominator": 1001,
        },
        "paths": {},
        "storage": {},
        "polling": {},
        "audio": {},
        "app": {},
        "notifications": {
            "enabled": True,
            "recording_started": True,
            "recording_completed": False,
            "recording_failed": True,
            "minimized_to_tray": False,
        },
    }
    monkeypatch.setattr(app, "load_config", lambda: config)

    page = SettingsPage(lambda: None)
    qtbot.addWidget(page)

    assert page.obs_fps_numerator.maximum() > 120
    assert page.obs_fps_numerator.value() == 240000
    assert page.obs_fps_denominator.value() == 1001

    page.obs_fps_numerator.setValue(300000)
    page.obs_fps_denominator.setValue(1001)
    page._write_settings_ui_to_config(config)

    assert config["obs"]["fps_numerator"] == 300000
    assert config["obs"]["fps_denominator"] == 1001


def test_settings_page_saves_independent_notification_preferences(qtbot, monkeypatch):
    config = {
        "obs": {"password": "secret"},
        "paths": {},
        "storage": {},
        "polling": {},
        "audio": {},
        "app": {},
        "notifications": {},
    }
    monkeypatch.setattr(app, "load_config", lambda: config)

    page = SettingsPage(lambda: None)
    qtbot.addWidget(page)

    page.notifications_enabled_check.setChecked(True)
    page.notification_recording_started_check.setChecked(False)
    page.notification_recording_completed_check.setChecked(True)
    page.notification_recording_failed_check.setChecked(False)
    page.notification_minimized_to_tray_check.setChecked(True)
    page._write_settings_ui_to_config(config)

    assert config["notifications"] == {
        "enabled": True,
        "recording_started": False,
        "recording_completed": True,
        "recording_failed": False,
        "minimized_to_tray": True,
    }

    page.notifications_enabled_check.setChecked(False)
    assert page.notification_recording_started_check.isEnabled() is False
    assert page.notification_recording_completed_check.isEnabled() is False
