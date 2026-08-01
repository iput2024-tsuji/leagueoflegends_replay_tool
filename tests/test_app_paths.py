import os
import subprocess

from src import app_paths


def _create_directory_link(link, target) -> None:
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr or completed.stdout or "mklink /J failed")
        return
    os.symlink(target, link, target_is_directory=True)


def test_user_data_root_uses_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LOL_REPLAY_TOOL_DATA_DIR", str(tmp_path))

    assert app_paths.get_user_data_root() == tmp_path.resolve()


def test_user_data_root_defaults_to_app_root_in_source_checkout(monkeypatch):
    monkeypatch.delenv("LOL_REPLAY_TOOL_DATA_DIR", raising=False)
    monkeypatch.setattr(app_paths.sys, "frozen", False, raising=False)

    assert app_paths.get_user_data_root() == app_paths.get_app_root()


def test_user_data_root_preserves_lexical_junction_path(monkeypatch, tmp_path):
    external = tmp_path / "external"
    override = tmp_path / "data-link"
    external.mkdir()
    _create_directory_link(override, external)
    monkeypatch.setenv("LOL_REPLAY_TOOL_DATA_DIR", str(override))

    result = app_paths.get_user_data_root()

    assert result == override.absolute()
    assert result != external.resolve()


def test_resource_root_uses_pyinstaller_bundle_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(app_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_paths.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert app_paths.get_resource_root() == tmp_path.resolve()


def test_resource_root_defaults_to_app_root_in_source_checkout(monkeypatch):
    monkeypatch.setattr(app_paths.sys, "frozen", False, raising=False)
    monkeypatch.delattr(app_paths.sys, "_MEIPASS", raising=False)

    assert app_paths.get_resource_root() == app_paths.get_app_root()
