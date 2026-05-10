from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SESSION_LOG_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SessionLogV1:
    summoner_name: str | None = None
    champion_name: str | None = None
    enemy_champions: list[str] = field(default_factory=list)
    player_team: str | None = None
    game_result: str | bool | None = None
    winning_team: str | int | None = None
    saved_at: str | None = None
    sync_game_time: float = 0.0
    obs_record_path: str | None = None
    recordings_dir: str | None = None
    json_path: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    events_all: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SessionLogV1:
        if not isinstance(payload, dict):
            raise ValueError("session log payload must be a JSON object")
        schema_version = int(payload.get("schema_version") or SESSION_LOG_SCHEMA_VERSION)
        if schema_version != SESSION_LOG_SCHEMA_VERSION:
            raise ValueError(f"unsupported session log schema_version: {schema_version}")

        paths = payload.get("paths") if isinstance(payload.get("paths"), dict) else {}
        return cls(
            summoner_name=_optional_str(payload.get("summoner_name")),
            champion_name=_optional_str(payload.get("champion_name")),
            enemy_champions=_string_list(payload.get("enemy_champions")),
            player_team=_optional_str(payload.get("player_team")),
            game_result=payload.get("game_result"),
            winning_team=payload.get("winning_team"),
            saved_at=_optional_str(payload.get("saved_at")),
            sync_game_time=_float_value(payload.get("sync_game_time"), 0.0),
            obs_record_path=_optional_str(payload.get("obs_record_path")),
            recordings_dir=_optional_str(paths.get("recordings_dir")),
            json_path=_optional_str(paths.get("json_path")),
            events=_event_list(payload.get("events")),
            events_all=_event_list(payload.get("events_all")),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SESSION_LOG_SCHEMA_VERSION,
            "summoner_name": self.summoner_name,
            "champion_name": self.champion_name,
            "enemy_champions": list(self.enemy_champions),
            "player_team": self.player_team,
            "game_result": self.game_result,
            "winning_team": self.winning_team,
            "saved_at": self.saved_at,
            "sync_game_time": self.sync_game_time,
            "obs_record_path": self.obs_record_path,
            "paths": {
                "recordings_dir": self.recordings_dir,
                "json_path": self.json_path,
            },
            "events": list(self.events),
            "events_all": list(self.events_all),
            "counts": {
                "filtered": len(self.events),
                "all": len(self.events_all),
            },
        }


def load_session_payload(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return SessionLogV1.from_payload(payload).to_payload()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        source = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        source = value
    else:
        return []
    return [text for item in source if (text := str(item or "").strip())]


def _event_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]
