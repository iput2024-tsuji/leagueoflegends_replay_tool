from src import app_paths


def test_user_data_root_uses_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("LOL_REPLAY_TOOL_DATA_DIR", str(tmp_path))

    assert app_paths.get_user_data_root() == tmp_path.resolve()


def test_user_data_root_defaults_to_app_root_in_source_checkout(monkeypatch):
    monkeypatch.delenv("LOL_REPLAY_TOOL_DATA_DIR", raising=False)
    monkeypatch.setattr(app_paths.sys, "frozen", False, raising=False)

    assert app_paths.get_user_data_root() == app_paths.get_app_root()


def test_resource_root_uses_pyinstaller_bundle_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(app_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_paths.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert app_paths.get_resource_root() == tmp_path.resolve()


def test_resource_root_defaults_to_app_root_in_source_checkout(monkeypatch):
    monkeypatch.setattr(app_paths.sys, "frozen", False, raising=False)
    monkeypatch.delattr(app_paths.sys, "_MEIPASS", raising=False)

    assert app_paths.get_resource_root() == app_paths.get_app_root()
