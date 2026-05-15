import importlib

import pytest


def reset_player_runtime(player, monkeypatch):
    monkeypatch.setattr(player, "mpv_module", None)
    monkeypatch.setattr(player, "MPV_IMPORT_ERROR", None)
    monkeypatch.setattr(player, "MPV_BOOTSTRAPPED", False)
    monkeypatch.setattr(player, "MPV_DLL_DIRECTORY_HANDLE", None)
    monkeypatch.setattr(player, "MPV_DLL_LOAD_HANDLE", None)


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
    monkeypatch.setattr(player, "load_mpv_dll", lambda path: object())
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


def test_mpv_error_message_explains_missing_python_module(monkeypatch, tmp_path):
    player = importlib.import_module("src.player")
    reset_player_runtime(player, monkeypatch)
    dll_path = tmp_path / "mpv-1.dll"
    dll_path.write_bytes(b"fake")

    monkeypatch.setattr(player, "BIN_DIR", tmp_path)
    monkeypatch.setattr(player, "ROOT_DIR", tmp_path.parent)
    monkeypatch.setattr(
        player,
        "MPV_IMPORT_ERROR",
        ModuleNotFoundError("No module named 'mpv'", name="mpv"),
    )

    message = player.build_mpv_error_message()

    assert "python-mpv モジュール" in message
    assert "pip install python-mpv" in message
    assert "--hidden-import mpv" in message


def test_mpv_runtime_validates_detected_dll_before_import(monkeypatch, tmp_path):
    player = importlib.import_module("src.player")
    reset_player_runtime(player, monkeypatch)
    dll_path = tmp_path / "mpv-1.dll"
    dll_path.write_bytes(b"fake")

    monkeypatch.setattr(player, "BIN_DIR", tmp_path)
    monkeypatch.setattr(player, "ROOT_DIR", tmp_path.parent)
    monkeypatch.setattr(player, "register_mpv_dll_directory", lambda path: None)
    monkeypatch.setattr(player, "load_mpv_dll", lambda path: (_ for _ in ()).throw(OSError("missing dependency")))
    monkeypatch.setattr(
        player.importlib,
        "import_module",
        lambda name: (_ for _ in ()).throw(AssertionError("mpv import should not run after DLL load failure")),
    )

    assert player.bootstrap_mpv_runtime() is None
    assert isinstance(player.MPV_IMPORT_ERROR, OSError)
    assert "missing dependency" in str(player.MPV_IMPORT_ERROR)


def test_mpv_dll_detection_matches_python_mpv_preference(tmp_path):
    support = importlib.import_module("src.mpv_support")
    mpv1 = tmp_path / "mpv-1.dll"
    libmpv2 = tmp_path / "libmpv-2.dll"
    mpv1.write_bytes(b"mpv1")
    libmpv2.write_bytes(b"libmpv2")

    assert support.find_mpv_dll(tmp_path) == libmpv2


def test_player_runtime_raises_without_exiting(monkeypatch):
    player = importlib.import_module("src.player")
    reset_player_runtime(player, monkeypatch)

    monkeypatch.setattr(player, "bootstrap_mpv_runtime", lambda: None)
    monkeypatch.setattr(player, "build_mpv_error_message", lambda: "mpv is missing")

    with pytest.raises(player.PlayerRuntimeError, match="mpv is missing"):
        player.PlayerRuntime().create_player(1)


def test_player_runtime_creates_mpv_with_window_id(monkeypatch):
    player = importlib.import_module("src.player")
    reset_player_runtime(player, monkeypatch)
    created_player = object()

    class FakeMpvRuntime:
        def __init__(self):
            self.calls = []

        def MPV(self, **kwargs):
            self.calls.append(kwargs)
            return created_player

    fake_runtime = FakeMpvRuntime()
    monkeypatch.setattr(player, "bootstrap_mpv_runtime", lambda: fake_runtime)

    result = player.PlayerRuntime().create_player("123")

    assert result is created_player
    assert fake_runtime.calls == [
        {
            "wid": "123",
            "input_default_bindings": False,
            "input_vo_keyboard": False,
            "keepaspect": True,
            "vo": "gpu",
            "gpu_context": "d3d11",
        }
    ]
