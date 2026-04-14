from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MatchData:
    league: str
    home_team: str
    away_team: str
    odds: dict[str, float]
    form_notes: str = ""
    injuries_notes: str = ""
    stats_notes: str = ""


def build_match_context(match: MatchData) -> str:
    return (
        f"League: {match.league}\n"
        f"Match: {match.home_team} vs {match.away_team}\n"
        f"Odds: {match.odds}\n"
        f"Form: {match.form_notes}\n"
        f"Injuries: {match.injuries_notes}\n"
        f"Stats: {match.stats_notes}"
    )
