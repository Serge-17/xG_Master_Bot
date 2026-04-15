from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import requests

from config import settings
from modules.localization import resolve_league_name, resolve_team_name


@dataclass(slots=True)
class TeamContext:
    league: str
    home_team: str
    away_team: str
    home_xg: float | None = None
    away_xg: float | None = None
    home_xga: float | None = None
    away_xga: float | None = None
    home_form: str = ""
    away_form: str = ""
    injuries: str = ""
    odds: dict[str, float] | None = None
    source_notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FixtureRow:
    league: str
    home_team: str
    away_team: str
    match_date: date
    kickoff: str = ""          # "15:30"
    status: str = "NS"         # NS, FT, 1H, HT и т.д.
    home_score: int | None = None
    away_score: int | None = None
    odds: dict[str, float] | None = None
    source_notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class ApiFootballClient:
    BASE_URL = "https://v3.football.api-sports.io"
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "x-apisports-key": settings.API_FOOTBALL_KEY or "",
            "User-Agent": "xG-Master-Bot/2.0"
        })

    def _get(self, endpoint: str, params: dict = None) -> dict:
        if params is None:
            params = {}
        try:
            resp = self.session.get(f"{self.BASE_URL}{endpoint}", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            return data.get("response", []) if isinstance(data, dict) else []
        except Exception as e:
            print(f"API-Football error {endpoint}: {e}")
            return []

    def get_fixtures_by_date(self, fixture_date: date) -> List[FixtureRow]:
        """Основная функция: матчи на выбранную дату"""
        params = {
            "date": fixture_date.strftime("%Y-%m-%d"),
            "timezone": "UTC"
        }
        data = self._get("/fixtures", params)
        
        fixtures = []
        for item in data:
            fixture = item.get("fixture", {})
            teams = item.get("teams", {})
            goals = item.get("goals", {})
            league_data = item.get("league", {})
            
            home = teams.get("home", {}).get("name", "Unknown")
            away = teams.get("away", {}).get("name", "Unknown")
            
            match_date = datetime.fromisoformat(fixture.get("date", "").replace("Z", "+00:00")).date()
            kickoff = datetime.fromisoformat(fixture.get("date", "").replace("Z", "+00:00")).strftime("%H:%M")
            
            row = FixtureRow(
                league=resolve_league_name(league_data.get("name", "")),
                home_team=resolve_team_name(home),
                away_team=resolve_team_name(away),
                match_date=match_date,
                kickoff=kickoff,
                status=fixture.get("status", {}).get("short", "NS"),
                home_score=goals.get("home"),
                away_score=goals.get("away"),
                source_notes="API-Football v3",
                metadata={
                    "fixture_id": fixture.get("id"),
                    "league_id": league_data.get("id"),
                    "season": league_data.get("season")
                }
            )
            fixtures.append(row)
        
        return fixtures

    def get_leagues(self) -> List[dict]:
        """Получить список всех лиг"""
        return self._get("/leagues")

    def get_standings(self, league_id: int, season: int) -> List[dict]:
        """Таблица лиги"""
        params = {"league": league_id, "season": season}
        return self._get("/standings", params)

    def get_fixture_events(self, fixture_id: int) -> List[dict]:
        """События матча (для xG в будущем)"""
        return self._get(f"/fixtures/events?fixture={fixture_id}")


# ====================== ГЛОБАЛЬНЫЕ ФУНКЦИИ ======================

client = ApiFootballClient()


def list_fixtures_for_date(match_date: date) -> List[FixtureRow]:
    """Основная функция, которую использует бот"""
    return client.get_fixtures_by_date(match_date)


def get_league_standings(league_name: str) -> List[dict]:
    """Пример — можно расширить под поиск по имени лиги"""
    # Для начала возвращаем популярные лиги
    popular = {
        "Premier League": (39, 2025),
        "La Liga": (140, 2025),
        "Serie A": (135, 2025),
        "Bundesliga": (78, 2025),
        "Ligue 1": (61, 2025),
    }
    for name, (lid, season) in popular.items():
        if league_name.lower() in name.lower():
            return client.get_standings(lid, season)
    return []


def get_team_form(league_id: int, season: int, team_name: str) -> str:
    """Простая заглушка — можно доработать позже"""
    return "WDLWW"


# Для обратной совместимости со старым кодом
def _fixtures_for_manual_league(league: str) -> List[FixtureRow]:
    """Если нужно выбирать по лиге — можно реализовать позже"""
    today = date.today()
    all_fixtures = list_fixtures_for_date(today)
    return [f for f in all_fixtures if league.lower() in f.league.lower()]


# ====================== НАСТРОЙКИ ======================

def check_api_connection() -> bool:
    """Проверка подключения"""
    try:
        leagues = client.get_leagues()
        return len(leagues) > 0
    except:
        return False