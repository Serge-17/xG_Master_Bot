from __future__ import annotations
import requests
from dataclasses import dataclass, field
from datetime import date
from typing import List, Dict, Any
from config import settings

# Список ID популярных лиг для быстрого поиска
TOP_LEAGUES = {
    "🏴󠁧󠁢󠁥󠁮󠁧󠁿 АПЛ": 39,
    "🇪🇸 Ла Лига": 140,
    "🇩🇪 Бундеслига": 78,
    "🇮🇹 Серия А": 135,
    "🇫🇷 Лига 1": 61,
    "🇷🇺 РПЛ": 235,
    "🇪🇺 Лига Чемпионов": 2,
    "🇪🇺 Лига Европы": 3,
    "🇳🇱 Эредивизи": 88,
    "🇵🇹 Примейра": 94
}

@dataclass
class TeamContext:
    league: str
    home_team: str
    away_team: str
    home_xg: float = 1.5
    away_xg: float = 1.2
    home_xga: float = 1.1
    away_xga: float = 1.1
    home_form: str = "WWDLW"
    away_form: str = "LLDWW"
    odds: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class FixtureRow:
    league: str
    home_team: str
    away_team: str
    match_date: date
    kickoff: str = ""
    status: str = "NS"
    source_notes: str = "API-Football"
    odds: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

class ApiFootballClient:
    def __init__(self):
        self.key = settings.api_football_key
        self.base_url = "https://v3.football.api-sports.io"

    def _get(self, endpoint: str, params: dict):
        headers = {"x-apisports-key": self.key}
        try:
            r = requests.get(f"{self.base_url}{endpoint}", headers=headers, params=params, timeout=10)
            return r.json().get("response", [])
        except Exception as e:
            print(f"API Error: {e}")
            return []

    def get_fixtures_by_date(self, match_date: date):
        params = {"date": match_date.strftime("%Y-%m-%d")}
        return self._get("/fixtures", params)

    def get_fixtures_by_league(self, league_id: int):
        params = {"league": league_id, "season": 2023, "date": date.today().strftime("%Y-%m-%d")}
        return self._get("/fixtures", params)

client = ApiFootballClient()

# ЭТА ФУНКЦИЯ БЫЛА ПРОПУЩЕНА И ВЫЗЫВАЛА ОШИБКУ
def list_fixtures_for_date(match_date: date, limit: int = 50) -> List[FixtureRow]:
    raw = client.get_fixtures_by_date(match_date)
    res = []
    for item in raw[:limit]:
        res.append(FixtureRow(
            league=item['league']['name'],
            home_team=item['teams']['home']['name'],
            away_team=item['teams']['away']['name'],
            match_date=match_date,
            kickoff=item['fixture']['date'][11:16],
            status=item['fixture']['status']['short'],
            metadata={"fixture_id": item['fixture']['id']}
        ))
    return res

def get_fixtures_by_league(league_id: int):
    return client.get_fixtures_by_league(league_id)

def build_match_context(league: str, home_team: str, away_team: str) -> TeamContext:
    return TeamContext(league=league, home_team=home_team, away_team=away_team)