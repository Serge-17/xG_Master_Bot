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
    home_xg: float | None = 1.5
    away_xg: float | None = 1.2
    home_form: str = ""
    away_form: str = ""
    home_xga: float = 1.1 # Ожидаемые пропущенные голы
    away_xga: float = 1.1
    odds: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

class ApiFootballClient:
    def __init__(self):
        self.key = settings.api_football_key
        self.base_url = "https://v3.football.api-sports.io"

    def get_fixtures(self, match_date: date):
        headers = {"x-apisports-key": self.key}
        params = {"date": match_date.strftime("%Y-%m-%d")}
        try:
            r = requests.get(f"{self.base_url}/fixtures", headers=headers, params=params, timeout=10)
            return r.json().get("response", [])
        except:
            return []

client = ApiFootballClient()

def list_fixtures_for_date(match_date: date, limit: int = 50):
    from dataclasses import dataclass
    @dataclass
    class Fixture:
        league: str; home_team: str; away_team: str; kickoff: str; status: str; source_notes: str; odds: dict
    
    raw = client.get_fixtures(match_date)
    res = []
    for item in raw[:limit]:
        res.append(Fixture(
            league=item['league']['name'],
            home_team=item['teams']['home']['name'],
            away_team=item['teams']['away']['name'],
            kickoff=item['fixture']['date'][11:16],
            status=item['fixture']['status']['short'],
            source_notes="API-Football",
            odds={"home": 2.0, "draw": 3.2, "away": 3.5} # В идеале тянуть из /odds
        ))
    return res

def build_match_context(league: str, home_team: str, away_team: str) -> TeamContext:
    # Здесь можно добавить логику запроса статистики команд
    return TeamContext(league=league, home_team=home_team, away_team=away_team)