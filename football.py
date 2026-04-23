"""
modules/football.py — сбор данных о матчах
Использует API-Football (api-football.com) с ключом FOOTBALL_DATA_API_KEY
"""

import asyncio
import logging
from datetime import date, datetime
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class FootballAPI:
    """Клиент для API-Football v3."""

    def __init__(self, api_key: str, base_url: str = "https://v3.football.api-sports.io"):
        if not api_key:
            raise ValueError("FOOTBALL_DATA_API_KEY не задан!")
        self.api_key = api_key
        self.base_url = base_url
        self.headers = {
            "x-apisports-key": self.api_key,
            "Accept": "application/json",
        }
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self.headers)
        return self._session

    async def _get(self, endpoint: str, params: dict = None) -> dict:
        session = await self._get_session()
        url = f"{self.base_url}/{endpoint}"
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429:
                    logger.warning("API лимит исчерпан (429). Ожидаем...")
                    await asyncio.sleep(60)
                    return {}
                else:
                    logger.error(f"API ошибка {resp.status}: {url}")
                    return {}
        except Exception as e:
            logger.error(f"Ошибка запроса к {url}: {e}")
            return {}

    async def get_fixtures(self, league_id: int, season: int = None,
                           match_date: date = None) -> list[dict]:
        """Получает список матчей для лиги."""
        if season is None:
            season = datetime.now().year
        params = {"league": league_id, "season": season}
        if match_date:
            params["date"] = match_date.strftime("%Y-%m-%d")

        data = await self._get("fixtures", params)
        return data.get("response", [])

    async def get_today_fixtures(self, league_ids: list[int]) -> list[dict]:
        """Получает все матчи сегодня по списку лиг."""
        today = date.today()
        all_fixtures = []
        for league_id in league_ids:
            fixtures = await self._get_fixtures_by_date(today, league_id)
            all_fixtures.extend(fixtures)
            await asyncio.sleep(0.3)  # rate limiting
        return all_fixtures

    async def _get_fixtures_by_date(self, match_date: date,
                                     league_id: int = None) -> list[dict]:
        params = {"date": match_date.strftime("%Y-%m-%d")}
        if league_id:
            params["league"] = league_id
        data = await self._get("fixtures", params)
        return data.get("response", [])

    async def get_fixture_stats(self, fixture_id: int) -> dict:
        """Статистика матча (xG, удары, владение мячом)."""
        data = await self._get("fixtures/statistics", {"fixture": fixture_id})
        return data.get("response", [])

    async def get_team_form(self, team_id: int, league_id: int,
                             season: int = None, last: int = 5) -> dict:
        """Форма команды — последние N матчей."""
        if season is None:
            season = datetime.now().year
        data = await self._get("teams/statistics", {
            "team": team_id,
            "league": league_id,
            "season": season,
        })
        return data.get("response", {})

    async def get_head_to_head(self, team1_id: int, team2_id: int,
                                last: int = 10) -> list[dict]:
        """Очные встречи двух команд."""
        data = await self._get("fixtures/headtohead", {
            "h2h": f"{team1_id}-{team2_id}",
            "last": last,
        })
        return data.get("response", [])

    async def get_injuries(self, fixture_id: int) -> list[dict]:
        """Травмы и дисквалификации к матчу."""
        data = await self._get("injuries", {"fixture": fixture_id})
        return data.get("response", [])

    async def get_standings(self, league_id: int, season: int = None) -> list:
        """Таблица лиги."""
        if season is None:
            season = datetime.now().year
        data = await self._get("standings", {
            "league": league_id,
            "season": season,
        })
        try:
            return data["response"][0]["league"]["standings"][0]
        except (KeyError, IndexError):
            return []

    async def get_lineups(self, fixture_id: int) -> list[dict]:
        """Составы команд на матч."""
        data = await self._get("fixtures/lineups", {"fixture": fixture_id})
        return data.get("response", [])

    async def check_status(self) -> dict:
        """Проверяет статус API ключа и оставшиеся запросы."""
        data = await self._get("status")
        return data.get("response", {})

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# Синглтон
_football_api: Optional[FootballAPI] = None


def get_football_api() -> FootballAPI:
    global _football_api
    if _football_api is None:
        from config import config
        _football_api = FootballAPI(config.football_api_key, config.football_api_base)
    return _football_api