from src.post_game_result import (
    build_post_game_result,
    normalize_game_result_value,
    normalize_lcu_team,
    opposing_lcu_team,
)


def test_build_post_game_result_uses_local_player_and_winning_team():
    payload = {
        "winningTeam": 100,
        "localPlayer": {
            "summonerName": "Tester#JP1",
            "teamId": 100,
        },
    }

    result = build_post_game_result(payload, player_name="Tester#JP1", source="lcu_end_of_game")

    assert result.game_result == "Win"
    assert result.winning_team == "ORDER"
    assert result.player_team == "ORDER"
    assert result.source == "lcu_end_of_game"


def test_build_post_game_result_finds_named_player_in_nested_payload():
    payload = {
        "teams": [
            {"teamId": 100, "isWinningTeam": False},
            {"teamId": 200, "isWinningTeam": True},
        ],
        "participants": [
            {"riotIdGameName": "Ally", "teamId": 100},
            {"riotIdGameName": "Tester", "teamId": 200},
        ],
    }

    result = build_post_game_result(payload, player_name="Tester#JP1")

    assert result.game_result == "Win"
    assert result.winning_team == "CHAOS"
    assert result.player_team == "CHAOS"


def test_build_post_game_result_derives_losing_winning_team_from_player_team():
    result = build_post_game_result(
        {"localPlayerStats": {"team": "blue", "gameResult": "defeat"}},
        player_name="Tester#JP1",
    )

    assert result.game_result == "Loss"
    assert result.player_team == "ORDER"
    assert result.winning_team == "CHAOS"


def test_post_game_normalizers_accept_known_result_shapes():
    assert normalize_lcu_team("teamTwo") == "CHAOS"
    assert normalize_lcu_team(100) == "ORDER"
    assert normalize_lcu_team(True) is None
    assert normalize_game_result_value("victory") == "Win"
    assert normalize_game_result_value(False) == "Loss"
    assert opposing_lcu_team("ORDER") == "CHAOS"
