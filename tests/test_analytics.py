import json
import shutil
from pathlib import Path

from src.analytics import GameDataAnalyzer


def runtime_dir(name):
    path = Path("tests") / "_tmp" / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_match(path, result, champion, events):
    path.write_text(
        json.dumps(
            {
                "summoner_name": "Tester",
                "champion_name": champion,
                "enemy_champions": ["Darius", "Lux"],
                "player_team": "ORDER",
                "game_result": result,
                "saved_at": "2026-01-01 00:00:00",
                "events_all": events,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_analyzer_flattens_events_and_correlates_horde_kill():
    tmp_path = runtime_dir("analytics")
    write_match(
        tmp_path / "win.json",
        "Win",
        "Malphite",
        [
            {"EventID": 1, "EventName": "HordeKill", "EventTime": 600.0},
            {"EventID": 2, "EventName": "DragonKill", "EventTime": 900.0, "DragonType": "Fire"},
            {"EventID": 4, "EventName": "BuildingKill", "EventTime": 700.0, "KillerTeam": "ORDER"},
            {"EventID": 5, "EventName": "FirstBlood", "EventTime": 120.0, "KillerTeam": "ORDER"},
        ],
    )
    write_match(
        tmp_path / "loss.json",
        "Loss",
        "Ahri",
        [
            {"EventID": 3, "EventName": "FirstBlood", "EventTime": 120.0, "KillerTeam": "CHAOS"},
            {"EventID": 6, "EventName": "BuildingKill", "EventTime": 800.0, "KillerTeam": "CHAOS"},
        ],
    )

    analyzer = GameDataAnalyzer(json_dir=tmp_path)
    df = analyzer.load_dataframe()
    result = analyzer.horde_kill_15min_winrate_correlation(df)

    assert set(df["champion_name"]) == {"Malphite", "Ahri"}
    assert "DragonKill" in set(df["event_name"])
    assert result["sample_size"] == 2
    assert result["winrate_by_event_count"][1] == 1.0
    assert result["winrate_by_event_count"][0] == 0.0

    x, y = analyzer.build_feature_matrix(df)
    assert list(x.columns) == ["horde_kill_15m", "own_building_kill_15m", "first_blood"]
    assert x.loc["win", "horde_kill_15m"] == 1
    assert x.loc["win", "own_building_kill_15m"] == 1
    assert x.loc["win", "first_blood"] == 1
    assert x.loc["loss", "own_building_kill_15m"] == 0
    assert x.loc["loss", "first_blood"] == 0
    assert y.loc["win"] == 1
    assert y.loc["loss"] == 0


def test_extract_tactical_insights_returns_best_and_worst_rules():
    tmp_path = runtime_dir("insights")
    for index in range(4):
        write_match(
            tmp_path / f"win_{index}.json",
            "Win",
            "Malphite",
            [
                {"EventID": 100 + index, "EventName": "HordeKill", "EventTime": 500.0},
                {"EventID": 200 + index, "EventName": "HordeKill", "EventTime": 650.0},
                {"EventID": 300 + index, "EventName": "BuildingKill", "EventTime": 700.0, "KillerTeam": "ORDER"},
                {"EventID": 400 + index, "EventName": "FirstBlood", "EventTime": 120.0, "KillerTeam": "ORDER"},
            ],
        )

    for index in range(4):
        write_match(
            tmp_path / f"loss_{index}.json",
            "Loss",
            "Ahri",
            [
                {"EventID": 500 + index, "EventName": "BuildingKill", "EventTime": 700.0, "KillerTeam": "CHAOS"},
                {"EventID": 600 + index, "EventName": "FirstBlood", "EventTime": 120.0, "KillerTeam": "CHAOS"},
            ],
        )

    insights = GameDataAnalyzer(json_dir=tmp_path).extract_tactical_insights()

    assert insights["sample_size"] == 8
    assert insights["reason"] is None
    assert "WinRate 100%" in insights["best_rule"]
    assert "WinRate 0%" in insights["worst_rule"]
    assert "n=4" in insights["best_rule"]
    assert "15分以内" in insights["best_rule"]
    assert "15分以内" in insights["tree_text"]
