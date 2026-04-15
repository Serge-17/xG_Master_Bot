from __future__ import annotations

import requests
from dataclasses import dataclass, field
from datetime import date
from typing import List, Dict, Any

from config import settings


@dataclass
class TeamContext:
    league: str
    home_team: str
    away_team: str
    home_xg: float | None = None
    away_xg: float | None = None
    home_form: str = ""
    away_form: str = ""
    odds: Dict[str, float] | None = None
    source_notes: str = ""


@dataclass
class FixtureRow:
    league: str
    home_team: str
    away_team: str
    match_date: date
    kickoff: str = ""
    status: str = "NS"
    home_score: int | None = None
    away_score: int | None = None
    odds: Dict[str, float] | None = None
    source_notes: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


class ApiFootballClient:
    BASE_URL = "https://v3.football.api-sports.io"

    def __init__(self):
        self.session = requests.Session()
        self.key = getattr(settings, 'api_football_key', '') or getattr(settings, 'API_FOOTBALL_KEY', '')
        self.session.headers.update({
            "x-apisports-key": self.key,
            "User-Agent": "xG-Master-Bot/2.0"
        })

    def _get(self, endpoint: str, params: dict = None):
        if params is None:
            params = {}
        try:
            r = self.session.get(f"{self.BASE_URL}{endpoint}", params=params, timeout=12)
            r.raise_for_status()
            return r.json().get("response", [])
        except Exception as e:
            print(f"[API-Football] Error {endpoint}: {e}")
            return []

    def get_fixtures_by_date(self, match_date: date) -> List[FixtureRow]:
        params = {"date": match_date.strftime("%Y-%m-%d"), "timezone": "UTC"}
        data = self._get("/fixtures", params)

        fixtures = []
        for item in data:
            fix = item.get("fixture", {})
            teams = item.get("teams", {})
            goals = item.get("goals", {})
            league_data = item.get("league", {})

            fixtures.append(FixtureRow(
                league=league_data.get("name", "Unknown League"),
                home_team=teams.get("home", {}).get("name", "Unknown"),
                away_team=teams.get("away", {}).get("name", "Unknown"),
                match_date=match_date,
                kickoff=fix.get("date", "")[11:16] if fix.get("date") else "",
                status=fix.get("status", {}).get("short", "NS"),
                home_score=goals.get("home"),
                away_score=goals.get("away"),
                source_notes="API-Football",
                metadata={"fixture_id": fix.get("id")}
            ))
        return fixtures


# Глобальный клиент
client = ApiFootballClient()


def list_fixtures_for_date(match_date: date) -> List[FixtureRow]:
    return client.get_fixtures_by_date(match_date)


# Для совместимости
def _fixtures_for_manual_league(league_key: str):
    today = date.today()
    all_f = list_fixtures_for_date(today)
    filtered = [f for f in all_f if league_key.lower() in f.league.lower()]
    return league_key.title(), filtered