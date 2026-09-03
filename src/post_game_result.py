from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from .game_events import normalize_summoner_name
except ImportError:
    from game_events import normalize_summoner_name


@dataclass(frozen=True)
class PostGameResult:
    game_result: str | None = None
    winning_team: str | None = None
    player_team: str | None = None
    source: str = "unavailable"

    @property
    def has_result(self) -> bool:
        return any(value not in (None, "") for value in (self.game_result, self.winning_team))

    def to_payload(self) -> dict[str, Any]:
        return {
            "game_result": self.game_result,
            "winning_team": self.winning_team,
            "player_team": self.player_team,
            "source": self.source,
        }


def normalize_lcu_team(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return "ORDER" if value == 100 else "CHAOS" if value == 200 else None
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"100", "1", "order", "blue", "teamone", "team1"}:
        return "ORDER"
    if lowered in {"200", "2", "chaos", "red", "teamtwo", "team2"}:
        return "CHAOS"
    upper = text.upper()
    if upper in {"ORDER", "CHAOS"}:
        return upper
    return None


def normalize_game_result_value(value: Any) -> str | None:
    if isinstance(value, bool):
        return "Win" if value else "Loss"
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"win", "won", "winner", "victory", "true", "1", "success"}:
        return "Win"
    if text in {"loss", "lose", "lost", "defeat", "false", "0", "fail", "failed"}:
        return "Loss"
    return None


def resolve_post_game_result(
    game_result: Any,
    winning_team: Any,
    player_team: Any,
) -> tuple[str | None, str | None, str | None]:
    """Normalize result fields and infer only from a valid, unambiguous pair."""
    normalized_result = normalize_game_result_value(game_result)
    normalized_winning_team = normalize_lcu_team(winning_team)
    normalized_player_team = normalize_lcu_team(player_team)

    result_present = game_result not in (None, "")
    if isinstance(game_result, str):
        result_present = bool(game_result.strip())
    if not result_present and normalized_player_team and normalized_winning_team:
        normalized_result = "Win" if normalized_player_team == normalized_winning_team else "Loss"

    winning_team_present = winning_team not in (None, "")
    if isinstance(winning_team, str):
        winning_team_present = bool(winning_team.strip())
    if not winning_team_present and normalized_winning_team is None and normalized_player_team:
        if normalized_result == "Win":
            normalized_winning_team = normalized_player_team
        elif normalized_result == "Loss":
            normalized_winning_team = opposing_lcu_team(normalized_player_team)

    result = normalized_result
    if result is None and isinstance(game_result, str):
        result = game_result.strip() or None
    return result, normalized_winning_team, normalized_player_team


def build_post_game_result(
    payload: dict[str, Any] | None,
    *,
    player_name: str | None = None,
    player_team: Any | None = None,
    source: str = "lcu_end_of_game",
) -> PostGameResult:
    if not isinstance(payload, dict):
        return PostGameResult(source=source)

    normalized_player_team = _post_game_player_team(payload, player_name, player_team)
    winning_team = _post_game_winning_team(payload)
    game_result = _post_game_local_result(payload, player_name)

    game_result, winning_team, normalized_player_team = resolve_post_game_result(
        game_result, winning_team, normalized_player_team
    )

    return PostGameResult(
        game_result=game_result,
        winning_team=winning_team,
        player_team=normalized_player_team,
        source=source,
    )


def _first_mapping_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _walk_mappings(value: Any) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    if isinstance(value, dict):
        mappings.append(value)
        for child in value.values():
            mappings.extend(_walk_mappings(child))
    elif isinstance(value, list):
        for item in value:
            mappings.extend(_walk_mappings(item))
    return mappings


def _post_game_team_from_mapping(mapping: dict[str, Any]) -> str | None:
    return normalize_lcu_team(
        _first_mapping_value(
            mapping,
            "teamId",
            "teamID",
            "team_id",
            "team",
            "teamNumber",
            "teamIndex",
            "id",
        )
    )


def _post_game_player_name_from_mapping(mapping: dict[str, Any]) -> str | None:
    value = _first_mapping_value(
        mapping,
        "summonerName",
        "summoner_name",
        "riotIdGameName",
        "gameName",
        "displayName",
        "name",
    )
    return str(value).strip() if value not in (None, "") else None


def _post_game_player_team(
    payload: dict[str, Any],
    player_name: str | None,
    fallback_team: Any | None,
) -> str | None:
    for key in ("localPlayer", "localPlayerStats", "localPlayerInfo", "player"):
        value = payload.get(key)
        if isinstance(value, dict):
            team = _post_game_team_from_mapping(value)
            if team:
                return team

    lookup_name = normalize_summoner_name(player_name) if player_name else None
    if lookup_name:
        for mapping in _walk_mappings(payload):
            candidate_name = _post_game_player_name_from_mapping(mapping)
            if not candidate_name:
                continue
            if candidate_name == player_name or normalize_summoner_name(candidate_name) == lookup_name:
                team = _post_game_team_from_mapping(mapping)
                if team:
                    return team

    return normalize_lcu_team(fallback_team)


def _post_game_winning_team(payload: dict[str, Any]) -> Any:
    direct_value = _first_mapping_value(
        payload,
        "winningTeam",
        "winning_team",
        "winningTeamId",
        "winningTeamID",
        "winning_team_id",
    )
    direct = normalize_lcu_team(direct_value)
    if direct:
        return direct
    if direct_value not in (None, ""):
        return direct_value

    for mapping in _walk_mappings(payload):
        team = _post_game_team_from_mapping(mapping)
        if not team:
            continue
        for key in ("isWinningTeam", "winner", "isWinner", "win", "result", "teamStatus"):
            if key in mapping and normalize_game_result_value(mapping.get(key)) == "Win":
                return team
    return None


def _post_game_local_result(payload: dict[str, Any], player_name: str | None) -> str | None:
    local_mappings = []
    for key in ("localPlayer", "localPlayerStats", "localPlayerInfo", "player"):
        value = payload.get(key)
        if isinstance(value, dict):
            local_mappings.append(value)

    lookup_name = normalize_summoner_name(player_name) if player_name else None
    if lookup_name:
        for mapping in _walk_mappings(payload):
            candidate_name = _post_game_player_name_from_mapping(mapping)
            if candidate_name and normalize_summoner_name(candidate_name) == lookup_name:
                local_mappings.append(mapping)

    local_mappings.extend([payload, payload.get("gameData") if isinstance(payload.get("gameData"), dict) else {}])
    for mapping in local_mappings:
        result = normalize_game_result_value(
            _first_mapping_value(
                mapping,
                "gameResult",
                "result",
                "win",
                "isWin",
                "victory",
                "outcome",
            )
        )
        if result:
            return result
    return None


def opposing_lcu_team(team: str | None) -> str | None:
    if team == "ORDER":
        return "CHAOS"
    if team == "CHAOS":
        return "ORDER"
    return None
