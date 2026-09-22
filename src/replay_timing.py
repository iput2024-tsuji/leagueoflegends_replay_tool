"""Map game events only across nearby, observed recording-clock samples."""

from __future__ import annotations

import math
from typing import Any

# Polling normally happens every second. Do not bridge long stalls or pauses.
MAX_SYNC_SAMPLE_GAP_SEC = 5.0
MAX_SYNC_CLOCK_DIFFERENCE_SEC = 0.5


def _time_value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def make_sync_interval(
    previous: tuple[float, float] | None,
    current: tuple[float, float],
) -> dict[str, float] | None:
    if previous is None:
        return None
    values = [_time_value(value) for value in (*previous, *current)]
    if any(value is None for value in values):
        return None
    game_start, video_start, game_end, video_end = values
    game_delta = game_end - game_start
    video_delta = video_end - video_start
    if not (game_delta > 0 and 0 < video_delta <= MAX_SYNC_SAMPLE_GAP_SEC):
        return None
    if abs(game_delta - video_delta) > MAX_SYNC_CLOCK_DIFFERENCE_SEC:
        return None
    return {
        "game_start": game_start,
        "game_end": game_end,
        "video_start": video_start,
        "video_end": video_end,
    }


def normalize_sync_intervals(value: Any) -> list[dict[str, float]]:
    """Malformed new timing data must not fall back to a legacy global offset."""
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            return []
        interval = make_sync_interval(
            (item.get("game_start"), item.get("video_start")),
            (item.get("game_end"), item.get("video_end")),
        )
        if interval is None:
            return []
        if result and (
            interval["game_start"] < result[-1]["game_end"]
            or interval["video_start"] < result[-1]["video_end"]
        ):
            return []
        result.append(interval)
    return result


def map_game_time_to_video(event_time: Any, intervals: list[dict[str, float]]) -> float | None:
    event = _time_value(event_time)
    if event is None:
        return None
    found = None
    for interval in normalize_sync_intervals(intervals):
        start, end = interval["game_start"], interval["game_end"]
        if not start <= event <= end:
            continue
        fraction = (event - start) / (end - start)
        video = interval["video_start"] + fraction * (interval["video_end"] - interval["video_start"])
        # A timestamp shared by both sides of a pause is ambiguous.
        if found is not None and not math.isclose(found, video, rel_tol=0, abs_tol=1e-6):
            return None
        found = video
    return found
