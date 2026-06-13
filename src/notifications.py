from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from enum import Enum
from typing import Any

LOGGER = logging.getLogger("lol_replay.notifications")


class NotificationEvent(str, Enum):
    RECORDING_STARTED = "recording_started"
    RECORDING_COMPLETED = "recording_completed"
    RECORDING_FAILED = "recording_failed"
    MINIMIZED_TO_TRAY = "minimized_to_tray"


DEFAULT_NOTIFICATION_SETTINGS = {
    "enabled": True,
    NotificationEvent.RECORDING_STARTED.value: True,
    NotificationEvent.RECORDING_COMPLETED.value: True,
    NotificationEvent.RECORDING_FAILED.value: True,
    NotificationEvent.MINIMIZED_TO_TRAY.value: True,
}


def notification_is_enabled(config: Mapping[str, Any] | None, event: NotificationEvent | str) -> bool:
    settings = config.get("notifications", {}) if isinstance(config, Mapping) else {}
    if not isinstance(settings, Mapping):
        settings = {}
    event_key = event.value if isinstance(event, NotificationEvent) else str(event)
    return bool(
        settings.get("enabled", DEFAULT_NOTIFICATION_SETTINGS["enabled"])
        and settings.get(event_key, DEFAULT_NOTIFICATION_SETTINGS.get(event_key, False))
    )


class NotificationService:
    """Apply persisted notification policy before invoking a platform sender."""

    def __init__(
        self,
        config_loader: Callable[[], Mapping[str, Any]],
        sender: Callable[[NotificationEvent, str, str], bool | None],
    ) -> None:
        self.config_loader = config_loader
        self.sender = sender

    def notify(self, event: NotificationEvent | str, title: str, message: str) -> bool:
        try:
            notification_event = event if isinstance(event, NotificationEvent) else NotificationEvent(str(event))
            config = self.config_loader()
            if not notification_is_enabled(config, notification_event):
                LOGGER.info("Notification suppressed by settings: event=%s", notification_event.value)
                return False
            result = self.sender(notification_event, str(title), str(message))
            if result is False:
                LOGGER.warning("Notification sender rejected event=%s", notification_event.value)
                return False
            LOGGER.info("Notification dispatched: event=%s", notification_event.value)
            return True
        except Exception:
            LOGGER.warning("Notification dispatch failed: event=%s", event, exc_info=True)
            return False
