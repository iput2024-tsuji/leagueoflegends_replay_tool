import inspect
import os
from pathlib import Path

from src import ffmpeg_support


def _write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"ffmpeg")
    return path


def test_resolver_uses_explicit_then_bin_app_and_absolute_path(tmp_path):
    explicit = _write_executable(tmp_path / "explicit" / "ffmpeg.exe")
    bin_candidate = _write_executable(tmp_path / "data" / "bin" / "ffmpeg.exe")
    app_candidate = _write_executable(tmp_path / "app" / "bin" / "ffmpeg.exe")
    path_candidate = _write_executable(tmp_path / "path" / "ffmpeg.exe")

    assert (
        ffmpeg_support.resolve_ffmpeg_executable(
            explicit_path=explicit,
            bin_dir=bin_candidate.parent,
            app_root=app_candidate.parents[1],
            path_value=str(path_candidate.parent),
        )
        == explicit.resolve()
    )

    explicit.unlink()
    assert (
        ffmpeg_support.resolve_ffmpeg_executable(
            explicit_path=explicit,
            bin_dir=bin_candidate.parent,
            app_root=app_candidate.parents[1],
            path_value=str(path_candidate.parent),
        )
        == bin_candidate.resolve()
    )

    bin_candidate.unlink()
    assert (
        ffmpeg_support.resolve_ffmpeg_executable(
            explicit_path=explicit,
            bin_dir=bin_candidate.parent,
            app_root=app_candidate.parents[1],
            path_value=str(path_candidate.parent),
        )
        == app_candidate.resolve()
    )

    app_candidate.unlink()
    app_root_candidate = _write_executable(tmp_path / "app" / "ffmpeg.exe")
    assert (
        ffmpeg_support.resolve_ffmpeg_executable(
            app_root=app_root_candidate.parent,
            path_value=str(path_candidate.parent),
        )
        == app_root_candidate.resolve()
    )

    app_root_candidate.unlink()
    assert (
        ffmpeg_support.resolve_ffmpeg_executable(
            app_root=app_root_candidate.parent,
            path_value=str(path_candidate.parent),
        )
        == path_candidate.resolve()
    )


def test_resolver_ignores_relative_and_empty_path_entries(monkeypatch, tmp_path):
    current = _write_executable(tmp_path / "ffmpeg.exe")
    monkeypatch.chdir(tmp_path)
    path_value = os.pathsep.join(("", ".", "relative"))

    assert (
        ffmpeg_support.resolve_ffmpeg_executable(
            explicit_path="ffmpeg.exe",
            path_value=path_value,
        )
        is None
    )
    assert current.is_file()


def test_manual_setup_message_uses_selected_bin_and_official_page(tmp_path):
    message = ffmpeg_support.manual_setup_message(tmp_path / "custom-bin")

    assert str(tmp_path / "custom-bin" / "ffmpeg.exe") in message
    assert ffmpeg_support.FFMPEG_DOWNLOAD_PAGE in message
    assert "自動取得・同梱されません" in message


def test_runtime_resolver_contains_no_network_client():
    source = inspect.getsource(ffmpeg_support)

    assert "urllib" not in source
    assert "requests" not in source
    assert "urlopen" not in source
