from src.match_metadata import build_match_metadata, merge_live_game_metadata


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
