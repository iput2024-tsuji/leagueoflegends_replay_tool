from __future__ import annotations

import time
from typing import Any


class ChampSelectTracker:
    """Accumulate completed champion-select actions without hover duplicates."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.session_key: str | None = None
        self.local_player_cell_id: int | None = None
        self.last_phase: str | None = None
        self._actions: dict[str, dict[str, Any]] = {}
        self._teams: dict[str, list[dict[str, Any]]] = {"ally": [], "enemy": []}
        self._confirmed_inactive = False

    @property
    def has_data(self) -> bool:
        return bool(self._actions)

    def observe_inactive(self) -> None:
        if self._actions:
            self._confirmed_inactive = True

    def observe(
        self,
        payload: dict[str, Any],
        champion_names: dict[int, str] | None = None,
        captured_at: str | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []

        names = champion_names or {}
        session_key = _session_key(payload)
        if self._should_reset_for_new_session(session_key):
            self.reset()
        if session_key is not None:
            self.session_key = session_key
        self._confirmed_inactive = False

        self.local_player_cell_id = _optional_int(payload.get("localPlayerCellId"))
        timer = payload.get("timer")
        if isinstance(timer, dict):
            self.last_phase = _optional_text(timer.get("phase"))

        ally_members = _team_members(payload.get("myTeam"), names)
        enemy_members = _team_members(payload.get("theirTeam"), names)
        self._teams = {"ally": ally_members, "enemy": enemy_members}
        team_by_cell = {
            member["cell_id"]: team
            for team, members in self._teams.items()
            for member in members
            if member.get("cell_id") is not None
        }
        position_by_cell = {
            member["cell_id"]: member.get("assigned_position")
            for members in self._teams.values()
            for member in members
            if member.get("cell_id") is not None
        }

        timestamp = captured_at or time.strftime("%Y-%m-%dT%H:%M:%S%z")
        added: list[dict[str, Any]] = []
        actions = payload.get("actions")
        if not isinstance(actions, list):
            actions = []
        for phase_index, action_group in enumerate(actions):
            if not isinstance(action_group, list):
                continue
            for action_index, raw_action in enumerate(action_group):
                if not isinstance(raw_action, dict):
                    continue
                action_type = _optional_text(raw_action.get("type"))
                champion_id = _optional_int(raw_action.get("championId"))
                if action_type not in {"ban", "pick"} or not raw_action.get("completed") or not champion_id:
                    continue

                action_id = _optional_int(raw_action.get("id"))
                actor_cell_id = _optional_int(raw_action.get("actorCellId"))
                key = (
                    f"id:{action_id}"
                    if action_id is not None
                    else f"{action_type}:{phase_index}:{action_index}:{actor_cell_id}"
                )
                team = "ally" if raw_action.get("isAllyAction") is True else team_by_cell.get(actor_cell_id)
                if team not in {"ally", "enemy"}:
                    team = "enemy" if raw_action.get("isAllyAction") is False else "unknown"

                normalized = {
                    "action_id": action_id,
                    "phase_order": phase_index + 1,
                    "action_index": action_index + 1,
                    "pick_turn": _optional_int(raw_action.get("pickTurn")),
                    "type": action_type,
                    "team": team,
                    "actor_cell_id": actor_cell_id,
                    "assigned_position": position_by_cell.get(actor_cell_id),
                    "champion_id": champion_id,
                    "champion_name": names.get(champion_id),
                    "completed": True,
                    "captured_at": self._actions.get(key, {}).get("captured_at") or timestamp,
                }
                is_new = key not in self._actions
                self._actions[key] = normalized
                if is_new:
                    added.append(dict(normalized))

        if names:
            self._apply_champion_names(names)
        return added

    def to_payload(self) -> dict[str, Any]:
        actions = sorted(
            (dict(action) for action in self._actions.values()),
            key=lambda action: (
                action.get("phase_order") or 0,
                action.get("action_index") or 0,
                action.get("action_id") or 0,
            ),
        )
        for order, action in enumerate(actions, start=1):
            action["order"] = order
        return {
            "session_id": self.session_key,
            "local_player_cell_id": self.local_player_cell_id,
            "last_phase": self.last_phase,
            "actions": actions,
            "teams": {
                "ally": [dict(member) for member in self._teams["ally"]],
                "enemy": [dict(member) for member in self._teams["enemy"]],
            },
        }

    def _should_reset_for_new_session(self, session_key: str | None) -> bool:
        if not self._actions:
            return False
        if self._confirmed_inactive:
            return True
        return bool(session_key and self.session_key and session_key != self.session_key)

    def _apply_champion_names(self, names: dict[int, str]) -> None:
        for action in self._actions.values():
            champion_id = action.get("champion_id")
            if champion_id in names:
                action["champion_name"] = names[champion_id]
        for members in self._teams.values():
            for member in members:
                champion_id = member.get("champion_id")
                if champion_id in names:
                    member["champion_name"] = names[champion_id]


def champion_name_catalog(payload: Any) -> dict[int, str]:
    if not isinstance(payload, list):
        return {}
    result: dict[int, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        champion_id = _optional_int(item.get("id"))
        name = _optional_text(item.get("name") or item.get("alias"))
        if champion_id and name:
            result[champion_id] = name
    return result


def _session_key(payload: dict[str, Any]) -> str | None:
    game_id = _optional_int(payload.get("gameId"))
    if game_id:
        return f"game:{game_id}"
    chat_details = payload.get("chatDetails")
    if isinstance(chat_details, dict):
        room_name = _optional_text(chat_details.get("chatRoomName"))
        if room_name:
            return f"chat:{room_name}"
    return None


def _team_members(value: Any, names: dict[int, str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        champion_id = _optional_int(item.get("championId"))
        result.append(
            {
                "cell_id": _optional_int(item.get("cellId")),
                "assigned_position": _optional_text(item.get("assignedPosition")),
                "champion_id": champion_id,
                "champion_name": names.get(champion_id) if champion_id else None,
            }
        )
    return result


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
