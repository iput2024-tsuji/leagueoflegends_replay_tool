import importlib


def reset_player_runtime(player, monkeypatch):
    monkeypatch.setattr(player, "mpv_module", None)
    monkeypatch.setattr(player, "MPV_IMPORT_ERROR", None)
    monkeypatch.setattr(player, "MPV_BOOTSTRAPPED", False)
    monkeypatch.setattr(player, "MPV_DLL_DIRECTORY_HANDLE", None)


def test_mpv_runtime_bootstrap_is_lazy_until_playback_starts():
    player = importlib.import_module("src.player")

    assert player.MPV_BOOTSTRAPPED is False
    assert player.mpv_module is None


def test_mpv_runtime_bootstrap_uses_existing_bin_dll(monkeypatch, tmp_path):
    player = importlib.import_module("src.player")
    reset_player_runtime(player, monkeypatch)
    runtime = object()
    registered = []
    (tmp_path / "mpv-1.dll").write_bytes(b"fake")

    monkeypatch.setattr(player, "BIN_DIR", tmp_path)
    monkeypatch.setattr(player, "ROOT_DIR", tmp_path.parent)
    monkeypatch.setattr(player, "register_mpv_dll_directory", lambda path: registered.append(path))
    monkeypatch.setattr(player.importlib, "import_module", lambda name: runtime)

    assert player.bootstrap_mpv_runtime() is runtime
    assert player.MPV_IMPORT_ERROR is None
    assert registered == [tmp_path]


def test_mpv_error_message_distinguishes_import_failure_from_missing_dll(monkeypatch, tmp_path):
    player = importlib.import_module("src.player")
    reset_player_runtime(player, monkeypatch)
    dll_path = tmp_path / "mpv-1.dll"
    dll_path.write_bytes(b"fake")

    monkeypatch.setattr(player, "BIN_DIR", tmp_path)
    monkeypatch.setattr(player, "ROOT_DIR", tmp_path.parent)
    monkeypatch.setattr(player, "MPV_IMPORT_ERROR", OSError("cannot load mpv dependency"))

    message = player.build_mpv_error_message()

    assert "MPV DLL は見つかりました" in message
    assert str(dll_path) in message
    assert "OSError: cannot load mpv dependency" in message
