from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class RecordingEndReason(str, Enum):
    STILL_ACTIVE = "still_active"
    GAME_END_EVENT = "game_end_event"
    NOT_IN_GAME_CONFIRMED = "not_in_game_confirmed"


class RecordingOutcome(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED_PARTIAL = "failed_partial"

    @property
    def completed(self) -> bool:
        return self is RecordingOutcome.COMPLETED

    @property
    def should_save_session(self) -> bool:
        return self in {RecordingOutcome.COMPLETED, RecordingOutcome.FAILED_PARTIAL}


class RecordingPhase(str, Enum):
    IDLE = "idle"
    WAITING_FOR_GAME = "waiting_for_game"
    STARTING = "starting"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class RecordingEndDecision:
    should_end: bool
    reason: RecordingEndReason


class RecordingEndDetector:
    """LCUポーリング結果から録画終了を判定する状態機械。"""

    def __init__(self, error_limit: int, missing_grace_sec: float) -> None:
        self.error_limit = max(1, int(error_limit))
        self.missing_grace_sec = max(0.0, float(missing_grace_sec))
        self.not_in_game_count = 0
        self.not_in_game_started_at: float | None = None
        self.temporary_failure_count = 0

    def observe_poll_status(self, status: Any, now: float) -> RecordingEndDecision:
        status_value = getattr(status, "value", status)
        if status_value == "in_game":
            self.reset()
            return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)

        if status_value == "temporary_failure":
            self.temporary_failure_count += 1
            return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)

        if status_value == "not_in_game":
            if self.not_in_game_started_at is None:
                self.not_in_game_started_at = now
            self.not_in_game_count += 1
            missing_duration = now - self.not_in_game_started_at
            if self.not_in_game_count >= self.error_limit and missing_duration >= self.missing_grace_sec:
                return RecordingEndDecision(True, RecordingEndReason.NOT_IN_GAME_CONFIRMED)

        return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)

    def observe_game_end_event(self) -> RecordingEndDecision:
        return RecordingEndDecision(True, RecordingEndReason.GAME_END_EVENT)

    def reset(self) -> None:
        self.not_in_game_count = 0
        self.not_in_game_started_at = None
        self.temporary_failure_count = 0
