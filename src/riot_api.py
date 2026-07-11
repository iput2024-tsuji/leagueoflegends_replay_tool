from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import aiohttp

try:
    from .champ_select import champion_name_catalog
    from .lcu_client import LCUConnectionProvider
    from .match_metadata import build_match_metadata
    from .post_game_result import build_post_game_result
except ImportError:
    from champ_select import champion_name_catalog
    from lcu_client import LCUConnectionProvider
    from match_metadata import build_match_metadata
    from post_game_result import build_post_game_result

LIVECLIENT_BASE = "https://127.0.0.1:2999/liveclientdata"
ACTIVE_PLAYER_URL = f"{LIVECLIENT_BASE}/activeplayername"
EVENT_URL = f"{LIVECLIENT_BASE}/eventdata"
ALL_GAME_URL = f"{LIVECLIENT_BASE}/allgamedata"
LCU_CHAMP_SELECT_PATH = "/lol-champ-select/v1/session"
LCU_CHAMPION_SUMMARY_PATH = "/lol-game-data/assets/v1/champion-summary.json"
LCU_GAMEFLOW_PHASE_PATH = "/lol-gameflow/v1/gameflow-phase"
LCU_GAMEFLOW_SESSION_PATH = "/lol-gameflow/v1/session"
LCU_GAME_QUEUES_PATH = "/lol-game-queues/v1/queues"
LCU_END_OF_GAME_STATS_PATH = "/lol-end-of-game/v1/eog-stats-block"
LCU_GAMECLIENT_END_OF_GAME_STATS_PATH = "/lol-end-of-game/v1/gameclient-eog-stats-block"


class RiotPollStatus(str, Enum):
    IN_GAME = "in_game"
    NOT_IN_GAME = "not_in_game"
    TEMPORARY_FAILURE = "temporary_failure"


@dataclass(frozen=True)
class RiotPollResult:
    status: RiotPollStatus
    payload: dict[str, Any] | None = None
    error: str | None = None


class RiotAPIClient(ABC):
    @abstractmethod
    async def get_active_player_name(self) -> str | None: ...

    @abstractmethod
    async def get_event_data(self) -> dict[str, Any] | None: ...

    @abstractmethod
    async def get_all_game_data(self) -> dict[str, Any] | None: ...

    @abstractmethod
    async def get_all_game_data_result(self) -> RiotPollResult: ...

    @abstractmethod
    async def get_champ_select_session_result(self) -> RiotPollResult: ...

    @abstractmethod
    async def get_champion_catalog(self) -> dict[int, str]: ...

    @abstractmethod
    async def get_gameflow_phase_result(self) -> RiotPollResult: ...

    @abstractmethod
    async def get_match_metadata(self) -> dict[str, Any]: ...

    @abstractmethod
    async def get_post_game_result(
        self, player_name: str | None = None, player_team: Any | None = None
    ) -> RiotPollResult: ...


def _first_mapping_value(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


class LiveClientRiotAPIClient(RiotAPIClient):
    def __init__(
        self,
        session_factory: Callable[..., Any] | None = None,
        lcu_connection_provider: LCUConnectionProvider | None = None,
    ) -> None:
        self.session_factory = session_factory or aiohttp.ClientSession
        self.lcu_connection_provider = lcu_connection_provider or LCUConnectionProvider()
        self._champion_catalog: dict[int, str] = {}
        self._queue_catalog: list[dict[str, Any]] = []

    async def _fetch_result(self, url: str, timeout_sec: float) -> RiotPollResult:
        timeout = aiohttp.ClientTimeout(total=float(timeout_sec))
        try:
            async with self.session_factory(timeout=timeout) as session:
                async with session.get(url, ssl=False) as response:
                    response.raise_for_status()
                    try:
                        data = await response.json(content_type=None)
                    except Exception:
                        data = (await response.text()).strip().replace('"', "")
                    payload = data if isinstance(data, dict) else {"value": data}
                    return RiotPollResult(RiotPollStatus.IN_GAME, payload=payload)
        except aiohttp.ClientResponseError as e:
            status = RiotPollStatus.NOT_IN_GAME if e.status in {404, 410} else RiotPollStatus.TEMPORARY_FAILURE
            return RiotPollResult(status, error=str(e))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            return RiotPollResult(RiotPollStatus.TEMPORARY_FAILURE, error=str(e))

    async def _fetch(self, url: str, timeout_sec: float) -> Any:
        result = await self._fetch_result(url, timeout_sec)
        if result.status != RiotPollStatus.IN_GAME:
            return None
        if result.payload and set(result.payload) == {"value"}:
            return result.payload["value"]
        return result.payload

    async def get_active_player_name(self) -> str | None:
        return await self._fetch(ACTIVE_PLAYER_URL, 5)

    async def get_event_data(self) -> dict[str, Any] | None:
        data = await self._fetch(EVENT_URL, 5)
        return data if isinstance(data, dict) else None

    async def get_all_game_data(self) -> dict[str, Any] | None:
        data = await self._fetch(ALL_GAME_URL, 1)
        return data if isinstance(data, dict) else None

    async def get_all_game_data_result(self) -> RiotPollResult:
        result = await self._fetch_result(ALL_GAME_URL, 1)
        if result.status != RiotPollStatus.IN_GAME or (
            isinstance(result.payload, dict) and "gameData" in result.payload
        ):
            return result
        return RiotPollResult(RiotPollStatus.TEMPORARY_FAILURE, error="Unexpected allgamedata payload")

    async def _fetch_lcu_result(self, path: str, timeout_sec: float = 1.0) -> RiotPollResult:
        connection = await asyncio.to_thread(self.lcu_connection_provider.get_connection_info)
        if connection is None:
            return RiotPollResult(RiotPollStatus.NOT_IN_GAME)
        try:
            timeout = aiohttp.ClientTimeout(total=float(timeout_sec))
            async with self.session_factory(timeout=timeout) as session:
                async with session.get(
                    f"{connection.base_url}{path}", auth=aiohttp.BasicAuth("riot", connection.password), ssl=False
                ) as response:
                    response.raise_for_status()
                    data = await response.json(content_type=None)
                    return RiotPollResult(
                        RiotPollStatus.IN_GAME, payload=data if isinstance(data, dict) else {"value": data}
                    )
        except aiohttp.ClientResponseError as e:
            if e.status in {401, 403}:
                self.lcu_connection_provider.invalidate()
            status = RiotPollStatus.NOT_IN_GAME if e.status in {404, 410} else RiotPollStatus.TEMPORARY_FAILURE
            return RiotPollResult(status, error=str(e))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            self.lcu_connection_provider.invalidate()
            return RiotPollResult(RiotPollStatus.TEMPORARY_FAILURE, error=str(e))

    async def get_champ_select_session_result(self) -> RiotPollResult:
        result = await self._fetch_lcu_result(LCU_CHAMP_SELECT_PATH)
        if result.status != RiotPollStatus.IN_GAME or (
            isinstance(result.payload, dict) and "actions" in result.payload
        ):
            return result
        return RiotPollResult(RiotPollStatus.TEMPORARY_FAILURE, error="Unexpected champ-select payload")

    async def get_champion_catalog(self) -> dict[int, str]:
        if self._champion_catalog:
            return dict(self._champion_catalog)
        result = await self._fetch_lcu_result(LCU_CHAMPION_SUMMARY_PATH, 2)
        if result.status != RiotPollStatus.IN_GAME or not isinstance(result.payload, dict):
            return {}
        catalog = champion_name_catalog(result.payload.get("value"))
        if catalog:
            self._champion_catalog = catalog
        return dict(catalog)

    async def get_gameflow_phase_result(self) -> RiotPollResult:
        result = await self._fetch_lcu_result(LCU_GAMEFLOW_PHASE_PATH)
        if result.status != RiotPollStatus.IN_GAME or not isinstance(result.payload, dict):
            return result
        phase = _first_mapping_value(result.payload, "phase", "value")
        if phase == "":
            return RiotPollResult(RiotPollStatus.TEMPORARY_FAILURE, error="Unexpected gameflow phase payload")
        return RiotPollResult(RiotPollStatus.IN_GAME, payload={"phase": str(phase) if phase is not None else "None"})

    async def get_match_metadata(self) -> dict[str, Any]:
        result = await self._fetch_lcu_result(LCU_GAMEFLOW_SESSION_PATH, 1.5)
        if result.status != RiotPollStatus.IN_GAME or not isinstance(result.payload, dict):
            return {}
        if not self._queue_catalog:
            queues = await self._fetch_lcu_result(LCU_GAME_QUEUES_PATH, 2)
            value = queues.payload.get("value") if isinstance(queues.payload, dict) else None
            if isinstance(value, list):
                self._queue_catalog = [dict(item) for item in value if isinstance(item, dict)]
        return build_match_metadata(result.payload, self._queue_catalog)

    async def get_post_game_result(
        self, player_name: str | None = None, player_team: Any | None = None
    ) -> RiotPollResult:
        status = RiotPollStatus.NOT_IN_GAME
        errors = []
        for path, source in (
            (LCU_END_OF_GAME_STATS_PATH, "lcu_end_of_game"),
            (LCU_GAMECLIENT_END_OF_GAME_STATS_PATH, "lcu_gameclient_end_of_game"),
            (LCU_GAMEFLOW_SESSION_PATH, "lcu_gameflow_session"),
        ):
            result = await self._fetch_lcu_result(path, 1.5)
            if result.status == RiotPollStatus.TEMPORARY_FAILURE:
                status = result.status
            if result.error:
                errors.append(result.error)
            if result.status == RiotPollStatus.IN_GAME and isinstance(result.payload, dict):
                post_game = build_post_game_result(
                    result.payload, player_name=player_name, player_team=player_team, source=source
                )
                if post_game.has_result:
                    return RiotPollResult(RiotPollStatus.IN_GAME, payload=post_game.to_payload())
        return RiotPollResult(status, error="; ".join(errors) if errors else "Post-game result unavailable")
