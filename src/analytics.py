import json
from pathlib import Path

import pandas as pd

try:
    from . import recordtest
except ImportError:
    import recordtest


class GameDataAnalyzer:
    """蓄積された録画JSONをpandas DataFrameへ変換し、簡易分析する。"""

    def __init__(self, json_dir=None, config=None):
        self.config = config or recordtest.load_app_config()
        self.json_dir = Path(json_dir) if json_dir else Path(self.config.paths.json_dir)

    def iter_json_files(self):
        if not self.json_dir.exists():
            return []
        return sorted(self.json_dir.glob("*.json"))

    def load_dataframe(self):
        rows = []
        for match_index, json_path in enumerate(self.iter_json_files()):
            payload = self._read_payload(json_path)
            if not payload:
                continue

            match_id = json_path.stem
            base = self._match_base_row(payload, match_id, json_path, match_index)
            events = payload.get("events_all") or payload.get("events") or []

            if not events:
                rows.append({**base, **self._empty_event_row()})
                continue

            for event in events:
                if not isinstance(event, dict):
                    continue
                rows.append({**base, **self._event_row(event)})

        return pd.DataFrame(rows)

    def correlate_event_with_winrate(self, df, event_name, within_seconds=None):
        if df is None or df.empty:
            return {
                "event_name": event_name,
                "within_seconds": within_seconds,
                "sample_size": 0,
                "correlation": None,
                "winrate_by_event_count": {},
            }

        matches = (
            df.drop_duplicates("match_id")
            .set_index("match_id")[["is_win", "champion_name", "game_result"]]
            .copy()
        )
        event_rows = df[df["event_name"] == event_name].copy()
        if within_seconds is not None:
            event_rows = event_rows[event_rows["event_time"].fillna(float("inf")) <= float(within_seconds)]

        counts = event_rows.groupby("match_id").size()
        matches["event_count"] = counts.reindex(matches.index, fill_value=0).astype(int)
        matches = matches.dropna(subset=["is_win"])

        if matches.empty:
            correlation = None
        elif matches["event_count"].nunique() < 2 or matches["is_win"].nunique() < 2:
            correlation = 0.0
        else:
            correlation = float(matches["event_count"].corr(matches["is_win"].astype(float)))

        winrate_by_count = (
            matches.groupby("event_count")["is_win"]
            .mean()
            .sort_index()
            .to_dict()
        )

        return {
            "event_name": event_name,
            "within_seconds": within_seconds,
            "sample_size": int(len(matches)),
            "correlation": correlation,
            "winrate_by_event_count": {int(k): float(v) for k, v in winrate_by_count.items()},
        }

    def horde_kill_15min_winrate_correlation(self, df=None):
        source = df if df is not None else self.load_dataframe()
        return self.correlate_event_with_winrate(source, "HordeKill", within_seconds=15 * 60)

    def build_feature_matrix(self, df=None):
        source = df if df is not None else self.load_dataframe()
        feature_names = [
            "horde_kill_15m",
            "own_building_kill_15m",
            "first_blood",
        ]
        if source is None or source.empty:
            return pd.DataFrame(columns=feature_names), pd.Series(dtype="int64", name="is_win")

        matches = (
            source.drop_duplicates("match_id")
            .set_index("match_id")[["is_win", "player_team"]]
            .dropna(subset=["is_win"])
            .copy()
        )
        if matches.empty:
            return pd.DataFrame(columns=feature_names), pd.Series(dtype="int64", name="is_win")

        timed = source[source["event_time"].fillna(float("inf")) <= 15 * 60].copy()

        horde_counts = (
            timed[timed["event_name"] == "HordeKill"]
            .groupby("match_id")
            .size()
        )

        building_rows = timed[timed["event_name"] == "BuildingKill"].copy()
        own_building_counts = (
            building_rows[self._own_team_event_mask(building_rows)]
            .groupby("match_id")
            .size()
        )

        first_blood_rows = source[source["is_first_blood"].fillna(False)].copy()
        first_blood_owned = (
            first_blood_rows[self._own_team_event_mask(first_blood_rows)]
            .groupby("match_id")
            .size()
        )

        x = pd.DataFrame(index=matches.index)
        x["horde_kill_15m"] = horde_counts.reindex(matches.index, fill_value=0).astype(int)
        x["own_building_kill_15m"] = own_building_counts.reindex(matches.index, fill_value=0).astype(int)
        x["first_blood"] = (first_blood_owned.reindex(matches.index, fill_value=0) > 0).astype(int)
        y = matches["is_win"].astype(int).rename("is_win")
        return x, y

    def _read_payload(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _match_base_row(self, payload, match_id, json_path, match_index):
        game_result = payload.get("game_result")
        player_team = payload.get("player_team")
        winning_team = payload.get("winning_team")
        return {
            "match_id": match_id,
            "match_index": match_index,
            "json_path": str(json_path),
            "saved_at": payload.get("saved_at"),
            "summoner_name": payload.get("summoner_name"),
            "champion_name": payload.get("champion_name"),
            "enemy_champions": payload.get("enemy_champions") or [],
            "player_team": player_team,
            "game_result": game_result,
            "winning_team": winning_team,
            "is_win": self._is_win(game_result, player_team, winning_team),
            "sync_game_time": payload.get("sync_game_time"),
            "obs_record_path": payload.get("obs_record_path"),
        }

    def _event_row(self, event):
        event_name = event.get("EventName")
        event_time = self._to_float(event.get("EventTime"))
        return {
            "event_id": event.get("EventID"),
            "event_name": event_name,
            "event_time": event_time,
            "killer_name": event.get("KillerName"),
            "victim_name": event.get("VictimName"),
            "assisters": event.get("Assisters"),
            "dragon_type": event.get("DragonType") or event.get("dragonType"),
            "building_type": event.get("BuildingType") or event.get("buildingType"),
            "killer_team": event.get("KillerTeam") or event.get("killer_team"),
            "team_relation": event.get("team_relation"),
            "is_dragon": event_name == "DragonKill",
            "is_baron": event_name == "BaronKill",
            "is_horde": event_name == "HordeKill",
            "is_building": event_name == "BuildingKill",
            "is_first_blood": event_name == "FirstBlood" or bool(event.get("FirstBlood")),
            "raw_event": event,
        }

    def _empty_event_row(self):
        return {
            "event_id": None,
            "event_name": None,
            "event_time": None,
            "killer_name": None,
            "victim_name": None,
            "assisters": None,
            "dragon_type": None,
            "building_type": None,
            "killer_team": None,
            "team_relation": None,
            "is_dragon": False,
            "is_baron": False,
            "is_horde": False,
            "is_building": False,
            "is_first_blood": False,
            "raw_event": None,
        }

    def _own_team_event_mask(self, df):
        if df.empty:
            return pd.Series(False, index=df.index)
        relation = df.get("team_relation")
        if relation is not None:
            own_by_relation = relation.fillna("").astype(str).str.lower().eq("own")
        else:
            own_by_relation = pd.Series(False, index=df.index)

        killer_team = df.get("killer_team")
        player_team = df.get("player_team")
        if killer_team is not None and player_team is not None:
            own_by_team = (
                killer_team.fillna("").astype(str).str.lower()
                == player_team.fillna("").astype(str).str.lower()
            )
        else:
            own_by_team = pd.Series(False, index=df.index)
        return own_by_relation | own_by_team

    def _is_win(self, game_result, player_team, winning_team):
        if isinstance(game_result, bool):
            return game_result
        text = str(game_result or "").strip().lower()
        if text in {"win", "won", "victory", "true", "1"}:
            return True
        if text in {"loss", "lose", "lost", "defeat", "false", "0"}:
            return False
        if player_team and winning_team:
            return str(player_team).strip().lower() == str(winning_team).strip().lower()
        return None

    def _to_float(self, value):
        try:
            return float(value)
        except Exception:
            return None
