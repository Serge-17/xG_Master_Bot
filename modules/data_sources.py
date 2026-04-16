from __future__ import annotations
import requests
from dataclasses import dataclass, field
from datetime import date
from typing import List, Dict, Any
from config import settings

# Список ID популярных лиг (для API-Football)
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
    metadata: Dict[str, Any] = field(default_factory=dict)

class ApiFootballClient:
    def __init__(self):
        self.key = settings.api_football_key
        self.base_url = "https://v3.football.api-sports.io"

    def get_league_fixtures(self, league_id: int):
        headers = {"x-apisports-key": self.key}
        params = {
            "league": league_id, 
            "season": 2023, # Или 2024 в зависимости от текущего сезона лиги
            "date": date.today().strftime("%Y-%m-%d")
        }
        try:
            r = requests.get(f"{self.base_url}/fixtures", headers=headers, params=params, timeout=10)
            return r.json().get("response", [])
        except:
            return []

client = ApiFootballClient()

def get_fixtures_by_league(league_id: int):
    raw = client.get_league_fixtures(league_id)
    return raw

def build_match_context(home: str, away: str, league: str) -> TeamContext:
    # Здесь можно добавить логику получения реальных xG
    return TeamContext(league=league, home_team=home, away_team=away)