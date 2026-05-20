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


def test_missing_config_is_created_from_legacy_config_and_copies_aliases(tmp_path):
    data_dir = tmp_path / "data" / "config"
    install_config_dir = tmp_path / "install" / "config"
    legacy_config = tmp_path / "legacy" / "config" / "setting.json"
    config_path = data_dir / "setting.json"
    sample_path = install_config_dir / "setting.sample.json"
    aliases_path = install_config_dir / "champion_aliases.json"
    legacy_config.parent.mkdir(parents=True)
    install_config_dir.mkdir(parents=True)
    legacy_config.write_text('{"paths": {"recordings_dir": "old-recordings"}}', encoding="utf-8")
    sample_path.write_text('{"paths": {"recordings_dir": "sample-recordings"}}', encoding="utf-8")
    aliases_path.write_text('{"MonkeyKing": "Wukong"}', encoding="utf-8")

    repo = ConfigRepository(
        config_path=config_path,
        sample_path=sample_path,
        legacy_config_paths=(legacy_config,),
    )

    assert repo.load()["paths"]["recordings_dir"] == "old-recordings"
    assert config_path.read_text(encoding="utf-8") == legacy_config.read_text(encoding="utf-8")
    assert (data_dir / "champion_aliases.json").read_text(encoding="utf-8") == aliases_path.read_text(
        encoding="utf-8"
    )
