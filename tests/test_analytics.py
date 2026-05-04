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
        ],
    )
    write_match(
        tmp_path / "loss.json",
        "Loss",
        "Ahri",
        [{"EventID": 3, "EventName": "FirstBlood", "EventTime": 120.0}],
    )

    analyzer = GameDataAnalyzer(json_dir=tmp_path)
    df = analyzer.load_dataframe()
    result = analyzer.horde_kill_15min_winrate_correlation(df)

    assert set(df["champion_name"]) == {"Malphite", "Ahri"}
    assert "DragonKill" in set(df["event_name"])
    assert result["sample_size"] == 2
    assert result["winrate_by_event_count"][1] == 1.0
    assert result["winrate_by_event_count"][0] == 0.0
