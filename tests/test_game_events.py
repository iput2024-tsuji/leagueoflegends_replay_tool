from src.game_events import (
    EVENT_CATEGORY_ASSIST,
    EVENT_CATEGORY_BARON,
    EVENT_CATEGORY_BUILDING,
    EVENT_CATEGORY_DEATH,
    EVENT_CATEGORY_DRAGON,
    EVENT_CATEGORY_HERALD_HORDE,
    EVENT_CATEGORY_KILL,
    EVENT_CATEGORY_OTHER,
    champion_kill_role,
    classify_game_event,
    event_assisters,
)


def test_champion_kill_role_distinguishes_kill_death_and_assist():
    player = "Tester#JP1"

    assert (
        champion_kill_role(
            {"EventName": "ChampionKill", "KillerName": "Tester", "VictimName": "Enemy"},
            player,
        )
        == EVENT_CATEGORY_KILL
    )
    assert (
        champion_kill_role(
            {"EventName": "ChampionKill", "KillerName": "Enemy", "VictimName": "Tester#JP1"},
            player,
        )
        == EVENT_CATEGORY_DEATH
    )
    assert (
        champion_kill_role(
            {
                "EventName": "ChampionKill",
                "KillerName": "Ally",
                "VictimName": "Enemy",
                "Assisters": ["Tester", "Support"],
            },
            player,
        )
        == EVENT_CATEGORY_ASSIST
    )


def test_event_assisters_accepts_lowercase_and_mapping_entries():
    assert event_assisters(
        {
            "assisters": [
                {"summonerName": "Tester#JP1"},
                {"name": "Support"},
            ]
        }
    ) == ["Tester#JP1", "Support"]


def test_classify_game_event_covers_replay_filter_categories():
    assert classify_game_event({"EventName": "DragonKill"}, "Tester") == EVENT_CATEGORY_DRAGON
    assert classify_game_event({"EventName": "HeraldKill"}, "Tester") == EVENT_CATEGORY_HERALD_HORDE
    assert classify_game_event({"EventName": "HordeKill"}, "Tester") == EVENT_CATEGORY_HERALD_HORDE
    assert classify_game_event({"EventName": "BaronKill"}, "Tester") == EVENT_CATEGORY_BARON
    assert classify_game_event({"EventName": "BuildingKill"}, "Tester") == EVENT_CATEGORY_BUILDING
    assert classify_game_event({"EventName": "TurretKilled"}, "Tester") == EVENT_CATEGORY_BUILDING
    assert classify_game_event({"EventName": "InhibKilled"}, "Tester") == EVENT_CATEGORY_BUILDING
    assert classify_game_event({"EventName": "MinionsSpawning"}, "Tester") == EVENT_CATEGORY_OTHER
    assert classify_game_event({"EventName": "GameStart"}, "Tester") is None
