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
