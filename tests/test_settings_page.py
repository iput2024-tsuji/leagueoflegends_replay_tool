from src.app import SettingsPage


def test_settings_page_initial_audio_refresh_does_not_auto_launch_obs(qtbot):
    page = SettingsPage(on_back=lambda: None)
    qtbot.addWidget(page)
    calls = []

    def fake_refresh_audio_devices(*, show_message=True, show_error=True, auto_launch=True):
        calls.append(
            {
                "show_message": show_message,
                "show_error": show_error,
                "auto_launch": auto_launch,
            }
        )
        return True

    page.refresh_audio_devices = fake_refresh_audio_devices

    SettingsPage.on_page_shown(page)

    assert page._audio_auto_refreshed_once is True
    assert calls == [{"show_message": False, "show_error": False, "auto_launch": False}]
