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
