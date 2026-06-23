from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class RecordingEndReason(str, Enum):
    STILL_ACTIVE = "still_active"
    GAME_END_EVENT = "game_end_event"
    NOT_IN_GAME_CONFIRMED = "not_in_game_confirmed"
    TEMPORARY_FAILURE_TIMEOUT = "temporary_failure_timeout"
    GAMEFLOW_INACTIVE_CONFIRMED = "gameflow_inactive_confirmed"
    GAME_PROCESS_MISSING_CONFIRMED = "game_process_missing_confirmed"


class RecordingOutcome(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ABORTED = "aborted"
    FAILED_PARTIAL = "failed_partial"

    @property
    def completed(self) -> bool:
        return self is RecordingOutcome.COMPLETED

    @property
    def should_save_session(self) -> bool:
        return self in {RecordingOutcome.COMPLETED, RecordingOutcome.ABORTED, RecordingOutcome.FAILED_PARTIAL}


class RecordingPhase(str, Enum):
    IDLE = "idle"
    WAITING_FOR_GAME = "waiting_for_game"
    STARTING = "starting"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ABORTED = "aborted"
    FAILED = "failed"


@dataclass(frozen=True)
class FinalizeResult:
    success: bool
    outcome: RecordingOutcome
    saved: bool = False
    error: str | None = None
    pending_path: str | None = None


@dataclass(frozen=True)
class RecordingEndDecision:
    should_end: bool
    reason: RecordingEndReason
    detail: str | None = None


class RecordingEndDetector:
    """録画中の複数の試合状態シグナルから終了を判定する状態機械。"""

    def __init__(
        self,
        error_limit: int,
        missing_grace_sec: float,
        temporary_failure_grace_sec: float,
        gameflow_inactive_grace_sec: float = 10.0,
        game_process_missing_grace_sec: float = 10.0,
    ) -> None:
        self.error_limit = max(1, int(error_limit))
        self.missing_grace_sec = max(0.0, float(missing_grace_sec))
        self.temporary_failure_grace_sec = max(0.0, float(temporary_failure_grace_sec))
        self.gameflow_inactive_grace_sec = max(0.0, float(gameflow_inactive_grace_sec))
        self.game_process_missing_grace_sec = max(0.0, float(game_process_missing_grace_sec))
        self.not_in_game_count = 0
        self.not_in_game_started_at: float | None = None
        self.temporary_failure_count = 0
        self.temporary_failure_started_at: float | None = None
        self.gameflow_inactive_count = 0
        self.gameflow_inactive_started_at: float | None = None
        self.game_process_missing_count = 0
        self.game_process_missing_started_at: float | None = None

    def observe_poll_status(self, status: Any, now: float) -> RecordingEndDecision:
        status_value = getattr(status, "value", status)
        if status_value == "in_game":
            self.reset_poll_status()
            return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)

        if status_value == "temporary_failure":
            if self.temporary_failure_started_at is None:
                self.temporary_failure_started_at = now
            self.temporary_failure_count += 1
            failure_duration = now - self.temporary_failure_started_at
            if (
                self.temporary_failure_count >= self.error_limit
                and failure_duration >= self.temporary_failure_grace_sec
            ):
                return RecordingEndDecision(True, RecordingEndReason.TEMPORARY_FAILURE_TIMEOUT)
            return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)

        if status_value == "not_in_game":
            self.reset_temporary_failure()
            if self.not_in_game_started_at is None:
                self.not_in_game_started_at = now
            self.not_in_game_count += 1
            missing_duration = now - self.not_in_game_started_at
            if self.not_in_game_count >= self.error_limit and missing_duration >= self.missing_grace_sec:
                return RecordingEndDecision(True, RecordingEndReason.NOT_IN_GAME_CONFIRMED)

        self.reset_temporary_failure()
        return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)

    def observe_game_end_event(self) -> RecordingEndDecision:
        return RecordingEndDecision(True, RecordingEndReason.GAME_END_EVENT)

    def observe_gameflow_phase(
        self,
        phase: Any,
        now: float,
        *,
        active_phases: set[str] | frozenset[str],
        detail: str | None = None,
    ) -> RecordingEndDecision:
        phase_value = str(phase).strip() if phase not in (None, "") else None
        if phase_value in active_phases:
            self.reset_gameflow_inactive()
            return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)

        if self.gameflow_inactive_started_at is None:
            self.gameflow_inactive_started_at = now
        self.gameflow_inactive_count += 1
        inactive_duration = now - self.gameflow_inactive_started_at
        if (
            self.gameflow_inactive_count >= self.error_limit
            and inactive_duration >= self.gameflow_inactive_grace_sec
        ):
            return RecordingEndDecision(
                True,
                RecordingEndReason.GAMEFLOW_INACTIVE_CONFIRMED,
                detail=detail or phase_value or "None",
            )
        return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)

    def observe_game_process_running(self, is_running: bool | None, now: float) -> RecordingEndDecision:
        if is_running is None:
            return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)
        if is_running:
            self.reset_game_process_missing()
            return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)

        if self.game_process_missing_started_at is None:
            self.game_process_missing_started_at = now
        self.game_process_missing_count += 1
        missing_duration = now - self.game_process_missing_started_at
        if (
            self.game_process_missing_count >= self.error_limit
            and missing_duration >= self.game_process_missing_grace_sec
        ):
            return RecordingEndDecision(True, RecordingEndReason.GAME_PROCESS_MISSING_CONFIRMED)
        return RecordingEndDecision(False, RecordingEndReason.STILL_ACTIVE)

    def reset(self) -> None:
        self.reset_poll_status()
        self.reset_gameflow_inactive()
        self.reset_game_process_missing()

    def reset_poll_status(self) -> None:
        self.not_in_game_count = 0
        self.not_in_game_started_at = None
        self.reset_temporary_failure()

    def reset_temporary_failure(self) -> None:
        self.temporary_failure_count = 0
        self.temporary_failure_started_at = None

    def reset_gameflow_inactive(self) -> None:
        self.gameflow_inactive_count = 0
        self.gameflow_inactive_started_at = None

    def reset_game_process_missing(self) -> None:
        self.game_process_missing_count = 0
        self.game_process_missing_started_at = None
