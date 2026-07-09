from __future__ import annotations

from typing import Any

QUEUE_DISPLAY_NAMES = {
    0: "カスタム",
    400: "ノーマル ドラフト",
    420: "ランク ソロ/デュオ",
    430: "ノーマル ブラインド",
    440: "ランク フレックス",
    450: "ARAM",
    490: "クイックプレイ",
    700: "Clash",
    830: "Co-op vs. AI 入門",
    840: "Co-op vs. AI 初級",
    850: "Co-op vs. AI 中級",
    900: "URF",
    1020: "One for All",
    1300: "Nexus Blitz",
    1700: "Arena",
}


def build_match_metadata(
    gameflow_payload: dict[str, Any] | None,
    queue_catalog: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = gameflow_payload if isinstance(gameflow_payload, dict) else {}
    game_data = payload.get("gameData") if isinstance(payload.get("gameData"), dict) else {}
    queue = game_data.get("queue") if isinstance(game_data.get("queue"), dict) else {}
    map_data = game_data.get("map") if isinstance(game_data.get("map"), dict) else {}

    queue_id = _optional_int(
        _first_present(
            _first_mapping_value(queue, "id", "queueId"),
            _first_mapping_value(game_data, "queueId", "queue_id"),
            _first_mapping_value(payload, "queueId", "queue_id"),
        )
    )
    queue_definition = {}
    for item in queue_catalog or []:
        if not isinstance(item, dict):
            continue
        item_id = _optional_int(_first_mapping_value(item, "id", "queueId"))
        if queue_id is not None and item_id == queue_id:
            queue_definition = item
            break

    queue_type = _first_mapping_value(
        queue,
        "type",
        "queueType",
        "gameTypeConfigId",
    ) or _first_mapping_value(queue_definition, "type", "queueType", "name")
    display_name = _first_mapping_value(
        queue_definition,
        "name",
        "shortName",
        "description",
    ) or _first_mapping_value(queue, "name", "shortName", "description")
    if not display_name and queue_id is not None:
        display_name = QUEUE_DISPLAY_NAMES.get(queue_id, f"Unknown ({queue_id})")

    game_mode = _first_mapping_value(game_data, "gameMode", "game_mode") or _first_mapping_value(
        payload, "gameMode", "game_mode"
    )
    game_type = _first_mapping_value(game_data, "gameType", "game_type") or _first_mapping_value(
        payload, "gameType", "game_type"
    )
    map_id = _optional_int(
        _first_present(
            _first_mapping_value(map_data, "id", "mapId"),
            _first_mapping_value(game_data, "mapId", "map_id"),
        )
    )
    map_name = _first_mapping_value(map_data, "name", "mapString") or _first_mapping_value(
        game_data, "mapName", "map_name"
    )
    game_id = _first_mapping_value(game_data, "gameId", "game_id") or _first_mapping_value(
        payload, "gameId", "game_id"
    )
    gameflow_phase = _first_mapping_value(payload, "phase", "gameflowPhase", "gameflow_phase")

    metadata = {
        "queue_id": queue_id,
        "queue_type": str(queue_type) if queue_type not in (None, "") else None,
        "display_name": str(display_name) if display_name not in (None, "") else None,
        "game_mode": str(game_mode) if game_mode not in (None, "") else None,
        "game_type": str(game_type) if game_type not in (None, "") else None,
        "map_id": map_id,
        "map_name": str(map_name) if map_name not in (None, "") else None,
        "game_id": str(game_id) if game_id not in (None, "") else None,
        "gameflow_phase": str(gameflow_phase) if gameflow_phase not in (None, "") else None,
        "source": "lcu",
    }
    return {key: value for key, value in metadata.items() if value is not None}


def merge_live_game_metadata(current: dict[str, Any], payload: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(current)
    if not isinstance(payload, dict):
        return result
    game_data = payload.get("gameData")
    if not isinstance(game_data, dict):
        return result

    fallback = {
        "game_mode": _first_mapping_value(game_data, "gameMode", "game_mode"),
        "game_type": _first_mapping_value(game_data, "gameType", "game_type"),
        "map_name": _first_mapping_value(game_data, "mapName", "map_name"),
        "game_id": _first_mapping_value(game_data, "gameId", "game_id"),
    }
    for key, value in fallback.items():
        if key not in result and value not in (None, ""):
            result[key] = str(value)
    if result and "source" not in result:
        result["source"] = "live_client"
    return result


def _first_mapping_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None
