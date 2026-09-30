import pytest

from src.replay_timing import make_sync_interval, map_game_time_to_video, normalize_sync_intervals


def test_events_before_and_after_multiple_pauses_keep_their_own_video_clock():
    intervals = [
        make_sync_interval((29, 24), (30, 25)),
        make_sync_interval((75, 184), (76, 185)),
        make_sync_interval((90, 249), (91, 250)),
    ]

    assert map_game_time_to_video(29.5, intervals) == 24.5
    assert map_game_time_to_video(75.78820037841797, intervals) == pytest.approx(184.78820037841797)
    assert map_game_time_to_video(90.5, intervals) == 249.5
    assert map_game_time_to_video(50, intervals) is None
    assert map_game_time_to_video(28, intervals) is None
    assert map_game_time_to_video(92, intervals) is None


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (None, (44, 71)),
        ((44, 71), (44, 72)),
        ((44, 71), (45, 185)),
        ((44, 71), (51, 78)),
        ((44, 71), (43, 72)),
        ((44, 71), (45, 70)),
        ((44, 71), (45, 71)),
        ((44, 71), (45, 73)),
        ((44, 71), (True, 72)),
        ((44, 71), (float("inf"), 72)),
        ((44, 71), (45, float("nan"))),
        ((-1, 0), (0, 1)),
    ],
)
def test_paused_missing_stale_or_invalid_samples_do_not_form_a_mapping(previous, current):
    assert make_sync_interval(previous, current) is None


def test_small_sampling_jitter_is_interpolated_between_observations():
    interval = make_sync_interval((10, 20), (11, 21.1))
    assert map_game_time_to_video(10.5, [interval]) == pytest.approx(20.55)


def test_shared_timestamp_on_opposite_sides_of_a_pause_is_not_guessed():
    intervals = [
        make_sync_interval((43, 38), (44, 39)),
        make_sync_interval((44, 158), (45, 159)),
    ]
    assert map_game_time_to_video(43.5, intervals) == 38.5
    assert map_game_time_to_video(44, intervals) is None
    assert map_game_time_to_video(44.5, intervals) == 158.5


def test_adjacent_intervals_share_an_unambiguous_boundary():
    intervals = [make_sync_interval((10, 20), (11, 21)), make_sync_interval((11, 21), (12, 22))]
    assert map_game_time_to_video(11, intervals) == 21


@pytest.mark.parametrize("value", [None, {}, "bad", [None], [{"game_start": 1}], [True]])
def test_corrupt_timing_payload_has_no_trusted_intervals(value):
    assert normalize_sync_intervals(value) == []


def test_overlapping_or_backwards_intervals_invalidate_the_payload():
    before = make_sync_interval((10, 20), (12, 22))
    overlap = make_sync_interval((11, 23), (13, 25))
    backwards = make_sync_interval((15, 19), (16, 20))
    assert normalize_sync_intervals([before, overlap]) == []
    assert normalize_sync_intervals([before, backwards]) == []
    assert map_game_time_to_video(11, [before, overlap]) is None


@pytest.mark.parametrize("event", [None, True, "11", -1, float("nan"), float("inf"), 10**400])
def test_invalid_event_timestamp_never_maps(event):
    assert map_game_time_to_video(event, [make_sync_interval((10, 20), (12, 22))]) is None
