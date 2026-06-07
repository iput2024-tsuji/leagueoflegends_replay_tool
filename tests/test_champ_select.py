from src.champ_select import ChampSelectTracker, champion_name_catalog


def champ_select_payload(game_id=100):
    return {
        "gameId": game_id,
        "localPlayerCellId": 0,
        "timer": {"phase": "BAN_PICK"},
        "myTeam": [
            {
                "cellId": 0,
                "championId": 103,
                "assignedPosition": "middle",
            }
        ],
        "theirTeam": [
            {
                "cellId": 5,
                "championId": 266,
                "assignedPosition": "top",
            }
        ],
        "actions": [
            [
                {
                    "id": 1,
                    "actorCellId": 0,
                    "championId": 122,
                    "completed": True,
                    "isAllyAction": True,
                    "pickTurn": 1,
                    "type": "ban",
                },
                {
                    "id": 2,
                    "actorCellId": 5,
                    "championId": 99,
                    "completed": True,
                    "isAllyAction": False,
                    "pickTurn": 1,
                    "type": "ban",
                },
            ],
            [
                {
                    "id": 3,
                    "actorCellId": 0,
                    "championId": 103,
                    "completed": True,
                    "isAllyAction": True,
                    "pickTurn": 1,
                    "type": "pick",
                }
            ],
        ],
    }


def test_champ_select_tracker_preserves_action_order_and_champion_names():
    tracker = ChampSelectTracker()
    names = {99: "Lux", 103: "Ahri", 122: "Darius", 266: "Aatrox"}

    added = tracker.observe(champ_select_payload(), names, captured_at="2026-06-07T12:00:00+0900")
    tracker.observe(champ_select_payload(), names, captured_at="2026-06-07T12:00:01+0900")
    payload = tracker.to_payload()

    assert len(added) == 3
    assert [action["order"] for action in payload["actions"]] == [1, 2, 3]
    assert [action["type"] for action in payload["actions"]] == ["ban", "ban", "pick"]
    assert [action["team"] for action in payload["actions"]] == ["ally", "enemy", "ally"]
    assert [action["champion_name"] for action in payload["actions"]] == ["Darius", "Lux", "Ahri"]
    assert payload["actions"][2]["assigned_position"] == "middle"
    assert payload["teams"]["enemy"][0]["champion_name"] == "Aatrox"
    assert payload["session_id"] == "game:100"


def test_champ_select_tracker_ignores_hover_and_resets_after_dodge():
    tracker = ChampSelectTracker()
    first = champ_select_payload(game_id=100)
    first["actions"][1][0]["completed"] = False
    tracker.observe(first, {122: "Darius", 99: "Lux"})

    assert len(tracker.to_payload()["actions"]) == 2

    tracker.observe_inactive()
    second = champ_select_payload(game_id=200)
    second["actions"] = [
        [
            {
                "id": 1,
                "actorCellId": 0,
                "championId": 64,
                "completed": True,
                "isAllyAction": True,
                "pickTurn": 1,
                "type": "pick",
            }
        ]
    ]
    tracker.observe(second, {64: "Lee Sin"})

    payload = tracker.to_payload()
    assert payload["session_id"] == "game:200"
    assert len(payload["actions"]) == 1
    assert payload["actions"][0]["champion_name"] == "Lee Sin"


def test_champion_name_catalog_ignores_invalid_entries():
    catalog = champion_name_catalog(
        [
            {"id": 103, "name": "Ahri"},
            {"id": 0, "name": "None"},
            {"id": "266", "alias": "Aatrox"},
            {"name": "Missing ID"},
        ]
    )

    assert catalog == {103: "Ahri", 266: "Aatrox"}
