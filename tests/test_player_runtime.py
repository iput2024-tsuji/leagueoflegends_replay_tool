import importlib


def test_mpv_runtime_bootstrap_is_lazy_until_playback_starts():
    player = importlib.import_module("src.player")

    assert player.MPV_BOOTSTRAPPED is False
    assert player.mpv_module is None
