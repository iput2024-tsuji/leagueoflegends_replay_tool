from types import SimpleNamespace
from unittest.mock import Mock

from PyQt6.QtWidgets import QSystemTrayIcon

from src import app as app_module, notifications
from src.app import MainWindow
from src.notifications import NotificationEvent, NotificationService, notification_is_enabled


def test_notification_policy_supports_master_and_per_event_switches():
    config = {
        "notifications": {
            "enabled": True,
            "recording_started": True,
            "recording_completed": False,
        }
    }

    assert notification_is_enabled(config, NotificationEvent.RECORDING_STARTED) is True
    assert notification_is_enabled(config, NotificationEvent.RECORDING_COMPLETED) is False

    config["notifications"]["enabled"] = False
    assert notification_is_enabled(config, NotificationEvent.RECORDING_STARTED) is False


def test_notification_service_only_calls_sender_for_enabled_events():
    sent = []
    config = {
        "notifications": {
            "enabled": True,
            "recording_started": True,
            "recording_completed": False,
        }
    }
    service = NotificationService(lambda: config, lambda *args: sent.append(args))

    assert service.notify(NotificationEvent.RECORDING_STARTED, "start", "message") is True
    assert service.notify(NotificationEvent.RECORDING_COMPLETED, "done", "message") is False
    assert sent == [(NotificationEvent.RECORDING_STARTED, "start", "message")]


def test_notification_service_rejects_unknown_event():
    service = NotificationService(lambda: {}, lambda *_args: None)

    assert service.notify("unknown", "title", "message") is False


def test_notification_service_reports_sender_rejection(monkeypatch):
    warning = Mock()
    monkeypatch.setattr(notifications.LOGGER, "warning", warning)
    service = NotificationService(lambda: {}, lambda *_args: False)

    assert service.notify(NotificationEvent.RECORDING_COMPLETED, "done", "message") is False

    warning.assert_called_once_with("Notification sender rejected event=%s", "recording_completed")


def test_completion_notification_is_enabled_by_default():
    assert notification_is_enabled({}, NotificationEvent.RECORDING_COMPLETED) is True


def test_main_window_submits_enabled_tray_notification(monkeypatch):
    tray_icon = Mock()
    tray_icon.isVisible.return_value = True
    monkeypatch.setattr(app_module.QSystemTrayIcon, "supportsMessages", lambda: True)
    window = SimpleNamespace(_tray_icon=tray_icon)

    sent = MainWindow._show_windows_notification(
        window,
        NotificationEvent.RECORDING_COMPLETED,
        "録画が完了しました",
        "保存しました。",
    )

    assert sent is True
    tray_icon.showMessage.assert_called_once_with(
        "録画が完了しました",
        "保存しました。",
        QSystemTrayIcon.MessageIcon.Information,
        4000,
    )


def test_main_window_rejects_notification_without_visible_tray():
    window = SimpleNamespace(_tray_icon=None)

    assert (
        MainWindow._show_windows_notification(
            window,
            NotificationEvent.RECORDING_COMPLETED,
            "録画が完了しました",
            "保存しました。",
        )
        is False
    )
