from __future__ import annotations

from collections.abc import Iterable
from typing import Any

EVENT_CATEGORY_KILL = "kill"
EVENT_CATEGORY_DEATH = "death"
EVENT_CATEGORY_ASSIST = "assist"
EVENT_CATEGORY_DRAGON = "dragon"
EVENT_CATEGORY_HERALD_HORDE = "herald_horde"
EVENT_CATEGORY_BARON = "baron"
EVENT_CATEGORY_BUILDING = "building"
EVENT_CATEGORY_OTHER = "other"

COMBAT_EVENT_NAMES = frozenset({"ChampionKill"})
DRAGON_EVENT_NAMES = frozenset({"DragonKill"})
HERALD_HORDE_EVENT_NAMES = frozenset({"HeraldKill", "HordeKill"})
BARON_EVENT_NAMES = frozenset({"BaronKill"})
BUILDING_EVENT_NAMES = frozenset({"BuildingKill", "TurretKilled", "InhibKilled"})
GLOBAL_OBJECTIVE_EVENT_NAMES = frozenset(
    DRAGON_EVENT_NAMES | HERALD_HORDE_EVENT_NAMES | BARON_EVENT_NAMES | BUILDING_EVENT_NAMES
)


def normalize_summoner_name(value: Any) -> str | None:
    if not value:
        return None
    name = str(value).strip()
    if "#" in name:
        name = name.split("#", 1)[0]
    return name.strip() or None


def summoner_names_match(left: Any, right: Any) -> bool:
    left_name = normalize_summoner_name(left)
    right_name = normalize_summoner_name(right)
    return bool(left_name and right_name and left_name.casefold() == right_name.casefold())


def event_assisters(event: dict[str, Any]) -> list[str]:
    value = event.get("Assisters")
    if value is None:
        value = event.get("assisters")
    if value is None:
        return []
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        return []

    result = []
    for item in values:
        if isinstance(item, dict):
            item = (
                item.get("SummonerName")
                or item.get("summonerName")
                or item.get("Name")
                or item.get("name")
            )
        name = str(item or "").strip()
        if name:
            result.append(name)
    return result


def champion_kill_role(event: dict[str, Any], player_name: Any) -> str | None:
    if event.get("EventName") not in COMBAT_EVENT_NAMES or not normalize_summoner_name(player_name):
        return None
    if summoner_names_match(event.get("KillerName") or event.get("killerName"), player_name):
        return EVENT_CATEGORY_KILL
    if summoner_names_match(event.get("VictimName") or event.get("victimName"), player_name):
        return EVENT_CATEGORY_DEATH
    if any(summoner_names_match(assister, player_name) for assister in event_assisters(event)):
        return EVENT_CATEGORY_ASSIST
    return None


def classify_game_event(event: dict[str, Any], player_name: Any) -> str | None:
    event_name = str(event.get("EventName") or "").strip()
    if event_name in COMBAT_EVENT_NAMES:
        return champion_kill_role(event, player_name)
    if event_name in DRAGON_EVENT_NAMES:
        return EVENT_CATEGORY_DRAGON
    if event_name in HERALD_HORDE_EVENT_NAMES:
        return EVENT_CATEGORY_HERALD_HORDE
    if event_name in BARON_EVENT_NAMES:
        return EVENT_CATEGORY_BARON
    if event_name in BUILDING_EVENT_NAMES:
        return EVENT_CATEGORY_BUILDING
    if event_name == "GameStart":
        return None
    return EVENT_CATEGORY_OTHER
