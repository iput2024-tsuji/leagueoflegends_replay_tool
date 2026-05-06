from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
from pandas import DataFrame, Series
from sklearn.preprocessing import MultiLabelBinarizer
from sklearn.tree import DecisionTreeClassifier, export_text

try:
    from . import recordtest
except ImportError:
    import recordtest


class GameDataAnalyzer:
    """蓄積された録画JSONをpandas DataFrameへ変換し、簡易分析する。"""

    def __init__(self, json_dir: str | Path | None = None, config: Any | None = None) -> None:
        self.config = config or recordtest.load_app_config()
        self.json_dir = Path(json_dir) if json_dir else Path(self.config.paths.json_dir)

    def iter_json_files(self) -> list[Path]:
        if not self.json_dir.exists():
            return []
        return sorted(self.json_dir.glob("*.json"))

    def load_dataframe(self) -> DataFrame:
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

    def correlate_event_with_winrate(
        self,
        df: DataFrame | None,
        event_name: str,
        within_seconds: float | None = None,
    ) -> dict[str, Any]:
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

    def horde_kill_15min_winrate_correlation(self, df: DataFrame | None = None) -> dict[str, Any]:
        source = df if df is not None else self.load_dataframe()
        return self.correlate_event_with_winrate(source, "HordeKill", within_seconds=15 * 60)

    def build_feature_matrix(self, df: DataFrame | None = None) -> tuple[DataFrame, Series]:
        source = df if df is not None else self.load_dataframe()
        feature_names = [
            "horde_kill_15m",
            "own_building_kill_15m",
            "first_blood",
        ]
        if source is None or source.empty:
            return pd.DataFrame(columns=feature_names), pd.Series(dtype="int64", name="is_win")

        match_columns = ["is_win", "player_team"]
        if "enemy_champions" in source.columns:
            match_columns.append("enemy_champions")
        matches = (
            source.drop_duplicates("match_id")
            .set_index("match_id")[match_columns]
            .dropna(subset=["is_win"])
            .copy()
        )
        if "enemy_champions" not in matches.columns:
            matches["enemy_champions"] = [[] for _ in range(len(matches))]
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

        enemy_lists = matches["enemy_champions"].apply(self._normalize_champion_list).tolist()
        enemy_binarizer = MultiLabelBinarizer()
        enemy_matrix = enemy_binarizer.fit_transform(enemy_lists)
        if len(enemy_binarizer.classes_) > 0:
            used_names = set(x.columns)
            enemy_feature_labels = {}
            enemy_columns = []
            for champion in enemy_binarizer.classes_:
                feature_name = self._enemy_feature_name(champion, used_names)
                enemy_columns.append(feature_name)
                enemy_feature_labels[feature_name] = str(champion)
            enemy_df = pd.DataFrame(enemy_matrix, index=matches.index, columns=enemy_columns).astype(int)
            x = pd.concat([x, enemy_df], axis=1)
            x.attrs["enemy_feature_labels"] = enemy_feature_labels

        y = matches["is_win"].astype(int).rename("is_win")
        return x, y

    def extract_tactical_insights(self) -> dict[str, Any]:
        x, y = self.build_feature_matrix()
        feature_labels = self._feature_labels(x)
        if x.empty or y.empty:
            return {
                "sample_size": 0,
                "best_rule": None,
                "worst_rule": None,
                "tree_text": "",
                "reason": "分析可能な試合データがありません。",
            }
        if len(y) < 3 or y.nunique() < 2:
            return {
                "sample_size": int(len(y)),
                "best_rule": None,
                "worst_rule": None,
                "tree_text": "",
                "reason": "決定木分析には勝敗両方を含む3試合以上のデータが必要です。",
            }

        model = DecisionTreeClassifier(
            max_depth=5,
            min_samples_leaf=2,
            min_samples_split=4,
            random_state=42,
        )
        model.fit(x, y)

        leaves = self._decision_tree_leaf_rules(model, x.columns.tolist(), feature_labels)
        if not leaves:
            return {
                "sample_size": int(len(y)),
                "best_rule": None,
                "worst_rule": None,
                "tree_text": export_text(model, feature_names=[feature_labels[name] for name in x.columns]),
                "reason": "決定木からルールを抽出できませんでした。",
            }

        best = max(leaves, key=lambda item: (item["win_rate"], item["samples"]))
        worst = min(leaves, key=lambda item: (item["win_rate"], -item["samples"]))
        return {
            "sample_size": int(len(y)),
            "best_rule": self._format_leaf_rule(best),
            "worst_rule": self._format_leaf_rule(worst),
            "tree_text": export_text(model, feature_names=[feature_labels[name] for name in x.columns]),
            "reason": None,
        }

    def _read_payload(self, path: Path) -> dict[str, Any] | None:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _match_base_row(
        self,
        payload: dict[str, Any],
        match_id: str,
        json_path: Path,
        match_index: int,
    ) -> dict[str, Any]:
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

    def _event_row(self, event: dict[str, Any]) -> dict[str, Any]:
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

    def _empty_event_row(self) -> dict[str, Any]:
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

    def _own_team_event_mask(self, df: DataFrame) -> Series:
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

    def _normalize_champion_list(self, champions: Any) -> list[str]:
        if champions is None:
            return []
        if isinstance(champions, str):
            source = champions.split(",")
        elif isinstance(champions, (list, tuple, set)):
            source = champions
        else:
            return []

        normalized = []
        for champion in source:
            name = str(champion or "").strip()
            if name:
                normalized.append(name)
        return sorted(set(normalized))

    def _enemy_feature_name(self, champion: str, used_names: set[str]) -> str:
        safe_name = "".join(char if char.isalnum() else "_" for char in str(champion).strip())
        while "__" in safe_name:
            safe_name = safe_name.replace("__", "_")
        safe_name = safe_name.strip("_") or "Unknown"

        base_name = f"enemy_{safe_name}"
        feature_name = base_name
        suffix = 2
        while feature_name in used_names:
            feature_name = f"{base_name}_{suffix}"
            suffix += 1
        used_names.add(feature_name)
        return feature_name

    def _feature_labels(self, x: DataFrame) -> dict[str, str]:
        labels = {
            "horde_kill_15m": "15分以内HordeKill",
            "own_building_kill_15m": "15分以内タワー破壊",
            "first_blood": "ファーストブラッド取得",
        }
        enemy_feature_labels = x.attrs.get("enemy_feature_labels", {})
        for feature_name, champion in enemy_feature_labels.items():
            labels[feature_name] = f"敵に{champion}"
        for feature_name in x.columns:
            if feature_name.startswith("enemy_") and feature_name not in labels:
                labels[feature_name] = f"敵に{feature_name.removeprefix('enemy_')}"
        return labels

    def _decision_tree_leaf_rules(
        self,
        model: DecisionTreeClassifier,
        feature_names: list[str],
        feature_labels: dict[str, str],
    ) -> list[dict[str, Any]]:
        tree = model.tree_
        class_index = {int(label): index for index, label in enumerate(model.classes_)}
        win_index = class_index.get(1)
        leaves = []

        def walk(node_id: int, conditions: list[str]) -> None:
            left = tree.children_left[node_id]
            right = tree.children_right[node_id]
            if left == right:
                values = tree.value[node_id][0]
                samples = int(tree.n_node_samples[node_id])
                if win_index is None or samples <= 0:
                    win_rate = 0.0
                elif abs(float(values.sum()) - 1.0) < 1e-9:
                    win_rate = float(values[win_index])
                else:
                    win_rate = float(values[win_index]) / float(values.sum())
                leaves.append(
                    {
                        "conditions": conditions or ["全試合"],
                        "samples": samples,
                        "win_rate": win_rate,
                    }
                )
                return

            feature_name = feature_names[tree.feature[node_id]]
            label = feature_labels.get(feature_name, feature_name)
            threshold = float(tree.threshold[node_id])
            walk(left, conditions + [self._format_tree_condition(label, feature_name, threshold, "left")])
            walk(right, conditions + [self._format_tree_condition(label, feature_name, threshold, "right")])

        walk(0, [])
        return leaves

    def _format_tree_condition(self, label: str, feature_name: str, threshold: float, side: str) -> str:
        if feature_name == "first_blood":
            return f"{label}なし" if side == "left" else f"{label}あり"
        if feature_name.startswith("enemy_"):
            return f"{label}がいない" if side == "left" else f"{label}がいる"

        if side == "left":
            value = int(threshold // 1)
            return f"{label} <= {value}"

        value = int(threshold // 1) + 1
        return f"{label} >= {value}"

    def _format_leaf_rule(self, leaf: dict[str, Any]) -> str:
        rule_text = " AND ".join(leaf["conditions"])
        win_rate = round(leaf["win_rate"] * 100)
        return f"{rule_text} -> WinRate {win_rate}% (n={leaf['samples']})"

    def _is_win(self, game_result: Any, player_team: Any, winning_team: Any) -> bool | None:
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

    def _to_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None
