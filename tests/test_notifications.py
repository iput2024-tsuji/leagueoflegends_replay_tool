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
