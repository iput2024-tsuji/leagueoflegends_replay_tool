from src import recordtest
from src.recorder_config import AppConfig as RecorderAppConfig
from src.riot_api import LiveClientRiotAPIClient, RiotAPIClient, RiotPollResult, RiotPollStatus
from src.storage_policy import enforce_storage_limit


def test_recordtest_reexports_split_recording_core() -> None:
    assert issubclass(recordtest.AppConfig, RecorderAppConfig)
    assert recordtest.LiveClientRiotAPIClient is LiveClientRiotAPIClient
    assert recordtest.RiotAPIClient is RiotAPIClient
    assert recordtest.RiotPollResult is RiotPollResult
    assert recordtest.RiotPollStatus is RiotPollStatus
    assert recordtest.enforce_storage_limit is not enforce_storage_limit


def test_recordtest_storage_facade_keeps_optional_config(monkeypatch) -> None:
    config = recordtest.AppConfig.from_dict({})
    calls = []
    monkeypatch.setattr(recordtest, "load_app_config", lambda: config)
    monkeypatch.setattr(
        recordtest._storage_policy, "enforce_storage_limit", lambda cfg, paths: calls.append((cfg, paths))
    )

    recordtest.enforce_storage_limit(keep_paths=["keep.mkv"])

    assert calls == [(config, ["keep.mkv"])]
