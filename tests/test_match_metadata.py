import pytest

from src.match_metadata import build_match_metadata, is_tft_match, merge_live_game_metadata


def test_build_match_metadata_uses_queue_catalog_definition():
    payload = {
        "phase": "InProgress",
        "gameData": {
            "queue": {"id": 420},
            "map": {"id": 11, "name": "Summoner's Rift"},
            "gameMode": "CLASSIC",
            "gameType": "MATCHED_GAME",
            "gameId": 12345,
        },
    }
    queue_catalog = [{"id": 420, "name": "Ranked Solo/Duo", "type": "RANKED"}]

    metadata = build_match_metadata(payload, queue_catalog)

    assert metadata == {
        "queue_id": 420,
        "queue_type": "RANKED",
        "display_name": "Ranked Solo/Duo",
        "game_mode": "CLASSIC",
        "game_type": "MATCHED_GAME",
        "map_id": 11,
        "map_name": "Summoner's Rift",
        "game_id": "12345",
        "gameflow_phase": "InProgress",
        "source": "lcu",
    }


def test_build_match_metadata_falls_back_to_builtin_queue_name():
    metadata = build_match_metadata({"queueId": "450"})

    assert metadata["queue_id"] == 450
    assert metadata["display_name"] == "ARAM"
    assert metadata["source"] == "lcu"


def test_build_match_metadata_omits_invalid_empty_values():
    metadata = build_match_metadata({"gameData": {"queueId": "bad", "gameMode": ""}})

    assert metadata == {"source": "lcu"}


def test_merge_live_game_metadata_only_fills_missing_values():
    current = {"game_mode": "CLASSIC", "source": "lcu"}
    payload = {
        "gameData": {
            "gameMode": "ARAM",
            "gameType": "MATCHED_GAME",
            "mapName": "Howling Abyss",
            "gameId": 999,
        }
    }

    metadata = merge_live_game_metadata(current, payload)

    assert metadata == {
        "game_mode": "CLASSIC",
        "game_type": "MATCHED_GAME",
        "map_name": "Howling Abyss",
        "game_id": "999",
        "source": "lcu",
    }


def test_merge_live_game_metadata_marks_live_client_source_when_current_has_no_source():
    metadata = merge_live_game_metadata({}, {"gameData": {"gameMode": "CLASSIC"}})

    assert metadata == {
        "game_mode": "CLASSIC",
        "source": "live_client",
    }


@pytest.mark.parametrize(
    "queue_id", [1090, 1100, 1110, 1111, 1130, 1160, 1170, 1180, 1190, 1210, 1220, 6000, 6100, 6120, 6130]
)
def test_tft_queue_ids_from_official_catalogs(queue_id):
    assert is_tft_match({"queue_id": queue_id})
    assert is_tft_match({"queue_id": str(queue_id)})


@pytest.mark.parametrize(
    "metadata",
    [
        {"game_mode": "TFT"},
        {"game_mode": " tft "},
        {"queue_type": "RANKED_TFT_DOUBLE_UP", "queue_id": 99999},
        {"queue_type": "TFT"},
        {"queue_type": "PVE_PUZZLE_TFT"},
    ],
)
def test_tft_explicit_classification_does_not_require_known_queue_id(metadata):
    assert is_tft_match(metadata)


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"queue_id": None},
        {"queue_id": "invalid"},
        {"queue_id": 1200},
        {"queue_id": 420},
        {"queue_id": 450},
        {"queue_id": 1700},
        {"game_mode": "CLASSIC"},
        {"game_mode": "ARAM"},
        {"queue_type": "NOTTFT"},
        {"display_name": "TFT", "game_type": "MATCHED_GAME"},
    ],
)
def test_other_or_missing_metadata_is_not_assumed_to_be_tft(metadata):
    assert not is_tft_match(metadata)


@pytest.mark.parametrize("use_catalog", [False, True])
def test_queue_game_mode_is_available_to_the_recording_filter(use_catalog):
    queue = {"id": 99999, "gameMode": "TFT"}
    payload = {"gameData": {"queue": {"id": 99999} if use_catalog else queue}}
    metadata = build_match_metadata(payload, [queue] if use_catalog else [])
    assert metadata["game_mode"] == "TFT"
    assert is_tft_match(metadata)
