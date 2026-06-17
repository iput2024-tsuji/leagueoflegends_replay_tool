from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SESSION_LOG_SCHEMA_VERSION = 1
SessionLogMigration = Callable[[dict[str, Any]], dict[str, Any]]
SESSION_LOG_MIGRATIONS: dict[int, SessionLogMigration] = {}


@dataclass(frozen=True)
class SessionLogLoadResult:
    path: Path
    payload: dict[str, Any] | None = None
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.payload is not None and not self.errors


@dataclass(frozen=True)
class SessionLogV1:
    session_status: str = "completed"
    session_phase: str | None = None
    failure_reason: str | None = None
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
    match: dict[str, Any] = field(default_factory=dict)
    ban_pick: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    events_all: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SessionLogV1:
        if not isinstance(payload, dict):
            raise ValueError("session log payload must be a JSON object")
        payload = migrate_session_payload(payload)

        paths = payload.get("paths") if isinstance(payload.get("paths"), dict) else {}
        return cls(
            session_status=_optional_str(payload.get("session_status")) or "completed",
            session_phase=_optional_str(payload.get("session_phase")),
            failure_reason=_optional_str(payload.get("failure_reason")),
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
            match=_dict_value(payload.get("match")),
            ban_pick=_dict_value(payload.get("ban_pick")),
            events=_event_list(payload.get("events")),
            events_all=_event_list(payload.get("events_all")),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SESSION_LOG_SCHEMA_VERSION,
            "session_status": self.session_status,
            "session_phase": self.session_phase,
            "failure_reason": self.failure_reason,
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
            "match": dict(self.match),
            "ban_pick": dict(self.ban_pick),
            "events": list(self.events),
            "events_all": list(self.events_all),
            "counts": {
                "filtered": len(self.events),
                "all": len(self.events_all),
            },
        }


def migrate_session_payload(
    payload: dict[str, Any],
    *,
    target_version: int = SESSION_LOG_SCHEMA_VERSION,
    migrations: dict[int, SessionLogMigration] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("session log payload must be a JSON object")

    migrations = SESSION_LOG_MIGRATIONS if migrations is None else migrations
    schema_version = _schema_version(payload)
    if schema_version > target_version:
        raise ValueError(f"unsupported session log schema_version: {schema_version}")

    migrated = dict(payload)
    while schema_version < target_version:
        migration = migrations.get(schema_version)
        if migration is None:
            raise ValueError(f"missing session log migration: v{schema_version} -> v{schema_version + 1}")
        migrated = migration(dict(migrated))
        if not isinstance(migrated, dict):
            raise ValueError(f"session log migration v{schema_version} did not return a JSON object")
        schema_version += 1
        migrated["schema_version"] = schema_version
    return migrated


def load_session_payload(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return SessionLogV1.from_payload(payload).to_payload()


class SessionLogRepository:
    """Session JSON を途中破損しないよう atomic に保存する。"""

    def save_payload(self, path: str | Path, payload: dict[str, Any]) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(f"{target.name}.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)


def save_session_payload(path: str | Path, payload: dict[str, Any]) -> None:
    SessionLogRepository().save_payload(path, payload)


def load_session_payload_result(path: str | Path) -> SessionLogLoadResult:
    source_path = Path(path)
    try:
        return SessionLogLoadResult(path=source_path, payload=load_session_payload(source_path))
    except Exception as e:
        return SessionLogLoadResult(path=source_path, errors=(f"{type(e).__name__}: {e}",))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _schema_version(payload: dict[str, Any]) -> int:
    try:
        return int(payload.get("schema_version") or SESSION_LOG_SCHEMA_VERSION)
    except Exception as e:
        raise ValueError(f"invalid session log schema_version: {payload.get('schema_version')}") from e


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


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
