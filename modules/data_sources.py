from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import requests

from config import settings


STATSBOMB_XG_EVENT_TYPES = {"Shot"}
DEFAULT_ODDS = {"home": 2.0, "draw": 3.2, "away": 3.8, "over_2_5": 1.9, "btts_yes": 1.8}

LEAGUE_ALIASES: dict[str, list[str]] = {
    "premier league": ["premier league", "england", "epl"],
    "championship": ["championship", "england"],
    "la liga": ["la liga", "spain"],
    "ligue 1": ["ligue 1", "france"],
    "serie a": ["serie a", "italy"],
    "bundesliga": ["bundesliga", "germany"],
    "eredivisie": ["eredivisie", "netherlands"],
    "primeira liga": ["primeira liga", "portugal"],
}


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


def _safe_get(url: str, timeout: int = 20) -> requests.Response | None:
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 xG-Master-Bot/1.0",
                "Accept": "application/json,text/csv,text/html;q=0.9,*/*;q=0.8",
            },
        )
        response.raise_for_status()
        return response
    except requests.RequestException:
        return None


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _contains_any(text: str, candidates: list[str]) -> bool:
    normalized = _normalize(text)
    return any(candidate in normalized for candidate in candidates)


def _load_json(url: str) -> Any | None:
    response = _safe_get(url)
    if response is None:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _load_csv(url: str) -> list[dict[str, str]]:
    response = _safe_get(url)
    if response is None or not response.text.strip():
        return []
    rows = list(csv.DictReader(response.text.splitlines()))
    return rows


def _statsbomb_competitions() -> list[dict[str, Any]]:
    competitions = _load_json(f"{settings.statsbomb_base_url}/competitions.json")
    return competitions if isinstance(competitions, list) else []


def _competition_matches_path(competition_id: int, season_id: int) -> str:
    return f"{settings.statsbomb_base_url}/matches/{competition_id}/{season_id}.json"


def _match_events_path(match_id: int) -> str:
    return f"{settings.statsbomb_base_url}/events/{match_id}.json"


def _find_statsbomb_competition(league: str) -> dict[str, Any] | None:
    league_normalized = _normalize(league)
    aliases = LEAGUE_ALIASES.get(league_normalized, [league_normalized])
    competitions = _statsbomb_competitions()
    candidates: list[dict[str, Any]] = []

    for competition in competitions:
        competition_name = _normalize(str(competition.get("competition_name", "")))
        country_name = _normalize(str(competition.get("country_name", "")))
        if any(alias in competition_name or alias in country_name for alias in aliases):
            candidates.append(competition)

    if not candidates:
        return None

    candidates.sort(key=lambda item: int(item.get("season_id", 0)), reverse=True)
    return candidates[0]


def _recent_team_matches(competition_id: int, season_id: int, team_name: str, limit: int = 5) -> list[dict[str, Any]]:
    matches = _load_json(_competition_matches_path(competition_id, season_id))
    if not isinstance(matches, list):
        return []

    team_normalized = _normalize(team_name)
    filtered = [
        match
        for match in matches
        if _normalize(str(match.get("home_team", {}).get("home_team_name", ""))) == team_normalized
        or _normalize(str(match.get("away_team", {}).get("away_team_name", ""))) == team_normalized
    ]
    filtered.sort(key=lambda item: item.get("match_date", ""), reverse=True)
    return filtered[:limit]


def _compute_match_xg(match_id: int, team_name: str) -> tuple[float | None, float | None, str]:
    events = _load_json(_match_events_path(match_id))
    if not isinstance(events, list):
        return None, None, "events unavailable"

    team_xg = 0.0
    opponent_xg = 0.0
    has_shots = False
    team_normalized = _normalize(team_name)

    for event in events:
        if event.get("type", {}).get("name") not in STATSBOMB_XG_EVENT_TYPES:
            continue
        xg_value = event.get("shot", {}).get("statsbomb_xg")
        if xg_value is None:
            continue
        has_shots = True
        event_team = _normalize(str(event.get("team", {}).get("name", "")))
        if event_team == team_normalized:
            team_xg += float(xg_value)
        else:
            opponent_xg += float(xg_value)

    if not has_shots:
        return None, None, "no shot events"
    return round(team_xg, 3), round(opponent_xg, 3), "statsbomb open-data"


def _team_form_from_matches(matches: list[dict[str, Any]], team_name: str) -> str:
    team_normalized = _normalize(team_name)
    results: list[str] = []
    for match in matches:
        home = _normalize(str(match.get("home_team", {}).get("home_team_name", "")))
        away = _normalize(str(match.get("away_team", {}).get("away_team_name", "")))
        home_score = match.get("home_score")
        away_score = match.get("away_score")
        if home_score is None or away_score is None:
            continue
        if home == team_normalized:
            results.append("W" if home_score > away_score else "D" if home_score == away_score else "L")
        elif away == team_normalized:
            results.append("W" if away_score > home_score else "D" if home_score == away_score else "L")
    return "".join(results[:5]) or "N/A"


def _football_data_column(row: dict[str, str], names: list[str]) -> str | None:
    for name in names:
        for key, value in row.items():
            if _normalize(key) == _normalize(name) and value:
                return value
    return None


def _extract_odds_from_row(row: dict[str, str]) -> dict[str, float] | None:
    def _parse(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return None

    home = _parse(_football_data_column(row, ["B365H", "HomeOdds", "Home"]))
    draw = _parse(_football_data_column(row, ["B365D", "DrawOdds", "Draw"]))
    away = _parse(_football_data_column(row, ["B365A", "AwayOdds", "Away"]))
    over_25 = _parse(_football_data_column(row, ["B365>2.5", "Over2.5", "Over 2.5"]))
    btts_yes = _parse(_football_data_column(row, ["B365BTTSYes", "BTTSYes", "BTTS Yes"]))

    if home is None or draw is None or away is None:
        return None

    odds = {
        "home": home,
        "draw": draw,
        "away": away,
    }
    if over_25 is not None:
        odds["over_2_5"] = over_25
    if btts_yes is not None:
        odds["btts_yes"] = btts_yes
    return odds


@lru_cache(maxsize=1)
def _football_data_fixtures() -> list[dict[str, str]]:
    return _load_csv(settings.football_data_fixtures_url)


def _find_fixtures_odds(league: str, home_team: str, away_team: str) -> tuple[dict[str, float] | None, str]:
    league_normalized = _normalize(league)
    home_normalized = _normalize(home_team)
    away_normalized = _normalize(away_team)
    for row in _football_data_fixtures():
        row_league = _normalize(
            row.get("League")
            or row.get("Competition")
            or row.get("Country")
            or row.get("Div")
            or row.get("Division")
            or ""
        )
        row_home = _normalize(row.get("Home") or row.get("HomeTeam") or row.get("Home Team") or "")
        row_away = _normalize(row.get("Away") or row.get("AwayTeam") or row.get("Away Team") or "")
        if league_normalized and league_normalized not in row_league and row_league not in league_normalized:
            continue
        if home_normalized and away_normalized and home_normalized in row_home and away_normalized in row_away:
            odds = _extract_odds_from_row(row)
            if odds is not None:
                return odds, "football-data.co.uk fixtures.csv"

    return None, "football-data.co.uk fallback unavailable"


def fetch_open_xg_context(league: str, home_team: str, away_team: str) -> TeamContext:
    competition = _find_statsbomb_competition(league)
    if competition is None:
        return TeamContext(
            league=league,
            home_team=home_team,
            away_team=away_team,
            home_form="N/A",
            away_form="N/A",
            injuries="Manual or external feed required",
            source_notes="StatsBomb Open Data competition not found for this league",
            metadata={"provider": "statsbomb-open-data", "available": False},
        )

    competition_id = int(competition["competition_id"])
    season_id = int(competition["season_id"])
    home_matches = _recent_team_matches(competition_id, season_id, home_team)
    away_matches = _recent_team_matches(competition_id, season_id, away_team)

    def _aggregate(team_name: str, matches: list[dict[str, Any]]) -> tuple[float | None, float | None, str]:
        if not matches:
            return None, None, "no matches found"

        xgs: list[float] = []
        xgas: list[float] = []
        for match in matches:
            match_id = int(match.get("match_id"))
            team_xg, opponent_xg, note = _compute_match_xg(match_id, team_name)
            if team_xg is not None:
                xgs.append(team_xg)
            if opponent_xg is not None:
                xgas.append(opponent_xg)
        home_xg = round(sum(xgs) / len(xgs), 3) if xgs else None
        home_xga = round(sum(xgas) / len(xgas), 3) if xgas else None
        return home_xg, home_xga, f"{len(matches)} recent matches from StatsBomb"

    home_xg, home_xga, home_note = _aggregate(home_team, home_matches)
    away_xg, away_xga, away_note = _aggregate(away_team, away_matches)

    odds, odds_note = _find_fixtures_odds(league, home_team, away_team)

    home_form = _team_form_from_matches(home_matches, home_team)
    away_form = _team_form_from_matches(away_matches, away_team)

    source_notes = " | ".join(
        [
            f"StatsBomb competition: {competition.get('competition_name')} {competition.get('season_name')}",
            home_note,
            away_note,
            odds_note,
        ]
    )

    return TeamContext(
        league=league,
        home_team=home_team,
        away_team=away_team,
        home_xg=home_xg,
        away_xg=away_xg,
        home_xga=home_xga,
        away_xga=away_xga,
        home_form=home_form,
        away_form=away_form,
        injuries="Manual or external feed required",
        odds=odds or DEFAULT_ODDS,
        source_notes=source_notes,
        metadata={
            "provider": "statsbomb-open-data + football-data.co.uk",
            "competition_id": competition_id,
            "season_id": season_id,
            "odds_source": odds_note,
        },
    )


def build_match_context(league: str, home_team: str, away_team: str) -> TeamContext:
    if settings.data_provider in {"open", "statsbomb", "football-data"}:
        try:
            return fetch_open_xg_context(league, home_team, away_team)
        except Exception as exc:
            return TeamContext(
                league=league,
                home_team=home_team,
                away_team=away_team,
                home_form="N/A",
                away_form="N/A",
                injuries="Manual or external feed required",
                odds=DEFAULT_ODDS,
                source_notes=f"Open-data provider fallback used: {exc}",
                metadata={"provider": "fallback", "available": False},
            )

    return TeamContext(
        league=league,
        home_team=home_team,
        away_team=away_team,
        home_form="N/A",
        away_form="N/A",
        injuries="Manual or external feed required",
        odds=DEFAULT_ODDS,
        source_notes="Mock data provider selected",
        metadata={"provider": "mock", "available": False},
    )
