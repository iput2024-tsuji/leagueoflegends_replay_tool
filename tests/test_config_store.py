from src.config_store import ConfigRepository


def test_invalid_config_is_backed_up_before_defaults_are_returned(tmp_path):
    config_path = tmp_path / "setting.json"
    sample_path = tmp_path / "setting.sample.json"
    config_path.write_text("{broken", encoding="utf-8")
    sample_path.write_text('{"ok": true}', encoding="utf-8")

    repo = ConfigRepository(config_path=config_path, sample_path=sample_path)

    assert repo.load(create_if_missing=True) == {}
    assert not config_path.exists()
    backups = list(tmp_path.glob("setting.json.*.invalid"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{broken"


def test_config_save_replaces_file_atomically(tmp_path):
    config_path = tmp_path / "setting.json"
    sample_path = tmp_path / "setting.sample.json"
    repo = ConfigRepository(config_path=config_path, sample_path=sample_path)

    repo.save({"obs": {"port": 4455}})

    assert config_path.read_text(encoding="utf-8")
    assert not (tmp_path / "setting.json.tmp").exists()
    assert repo.load(create_if_missing=False)["obs"]["port"] == 4455
