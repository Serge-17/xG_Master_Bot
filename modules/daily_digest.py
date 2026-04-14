from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime

from modules.bankroll_manager import recommended_stake
from modules.data_sources import TeamContext, build_match_context, list_fixtures_for_date
from utils.helpers import format_money


@dataclass(slots=True)
class MatchRecommendation:
    match: TeamContext
    market: str
    market_label: str
    odds: float
    confidence: int
    reasoning: str
    stake: float
    rank_score: float


def _form_score(form: str) -> float:
    score_map = {"W": 1.0, "D": 0.2, "L": -0.8}
    return round(sum(score_map.get(char, 0.0) for char in (form or "")[:5]), 2)


def _safe_metric(value: float | None, fallback: float) -> float:
    return float(value) if value is not None else fallback


def _resolve_market(match: TeamContext) -> tuple[str, str, float, int, str, float]:
    home_xg = _safe_metric(match.home_xg, 1.35)
    away_xg = _safe_metric(match.away_xg, 1.05)
    home_xga = _safe_metric(match.home_xga, 1.15)
    away_xga = _safe_metric(match.away_xga, 1.15)
    total_xg = home_xg + away_xg
    form_delta = _form_score(match.home_form) - _form_score(match.away_form)

    home_edge = (home_xg - away_xga) + form_delta * 0.12
    away_edge = (away_xg - home_xga) - form_delta * 0.12
    over_edge = total_xg - 2.45
    btts_edge = min(home_xg, away_xg) - 0.95

    odds = match.odds or {}
    candidates = [
        ("home", "П1", float(odds.get("home", 2.0)), home_edge, "хозяева выглядят сильнее по xG и форме"),
        ("away", "П2", float(odds.get("away", 2.6)), away_edge, "гости имеют лучший xG-профиль и потенциал по моментам"),
        ("over_2_5", "ТБ 2.5", float(odds.get("over_2_5", 1.9)), over_edge, "ожидается открытый матч с высоким суммарным xG"),
        ("btts_yes", "Обе забьют", float(odds.get("btts_yes", 1.8)), btts_edge, "обе команды стабильно создают моменты"),
    ]
    candidates.sort(key=lambda item: item[3], reverse=True)
    market_key, market_label, market_odds, edge, reason = candidates[0]

    if edge >= 0.85:
        confidence = 5
    elif edge >= 0.45:
        confidence = 4
    elif edge >= 0.15:
        confidence = 3
    else:
        confidence = 2

    reasoning = (
        f"xG {home_xg:.2f}:{away_xg:.2f}, xGA {home_xga:.2f}:{away_xga:.2f}, "
        f"форма {match.home_form or 'N/A'}:{match.away_form or 'N/A'}"
    )
    rank_score = round(edge * 10 + confidence + max(0.0, min(market_odds, 2.6) - 1.5), 3)
    return market_key, market_label, market_odds, confidence, f"{reason}; {reasoning}", rank_score


def build_daily_recommendations(
    target_date: date | None = None,
    limit: int = 12,
    bankroll: float = 0.0,
    strategy: str = "flat",
    flat_percent: float = 0.03,
    kelly_cap: float = 0.25,
) -> list[MatchRecommendation]:
    selected_date = target_date or datetime.now().date()
    fixtures = list_fixtures_for_date(selected_date, limit=max(limit * 3, 30))
    recommendations: list[MatchRecommendation] = []

    for fixture in fixtures:
        match = build_match_context(fixture.league, fixture.home_team, fixture.away_team)
        match.metadata["kickoff"] = fixture.kickoff
        match.metadata["fixture_source"] = fixture.source_notes
        merged_odds = dict(match.odds or {})
        if fixture.odds:
            merged_odds.update(fixture.odds)
        if merged_odds:
            match.odds = merged_odds

        market_key, market_label, market_odds, confidence, reasoning, rank_score = _resolve_market(match)
        stake = recommended_stake(
            bankroll=bankroll,
            confidence=float(confidence),
            odds=market_odds,
            strategy=strategy,
            flat_percent=flat_percent,
            kelly_cap=kelly_cap,
        )
        recommendations.append(
            MatchRecommendation(
                match=match,
                market=market_key,
                market_label=market_label,
                odds=market_odds,
                confidence=confidence,
                reasoning=reasoning,
                stake=stake,
                rank_score=rank_score,
            )
        )

    recommendations.sort(key=lambda item: item.rank_score, reverse=True)
    return recommendations[:limit]


def apply_bankroll_to_recommendations(
    recommendations: list[MatchRecommendation],
    bankroll: float,
    strategy: str = "flat",
    flat_percent: float = 0.03,
    kelly_cap: float = 0.25,
) -> list[MatchRecommendation]:
    personalized: list[MatchRecommendation] = []
    for item in recommendations:
        stake = recommended_stake(
            bankroll=bankroll,
            confidence=float(item.confidence),
            odds=item.odds,
            strategy=strategy,
            flat_percent=flat_percent,
            kelly_cap=kelly_cap,
        )
        personalized.append(replace(item, stake=stake))
    return personalized


def format_channel_digest(recommendations: list[MatchRecommendation], target_date: date | None = None) -> str:
    selected_date = target_date or datetime.now().date()
    updated_at = datetime.now().strftime("%H:%M")
    if not recommendations:
        return (
            f"⚽️ Подборка матчей на {selected_date.strftime('%d.%m.%Y')}\n\n"
            "На сегодня в доступном источнике не нашлось подходящих матчей с линиями."
        )

    lines = [
        f"⚽️ Матчи дня на {selected_date.strftime('%d.%m.%Y')}",
        f"Обновление: {updated_at}",
        "",
    ]
    for index, item in enumerate(recommendations, start=1):
        kickoff = item.match.metadata.get("kickoff") or ""
        kickoff_text = f"{kickoff} " if kickoff else ""
        lines.append(
            f"{index}. {kickoff_text}{item.match.home_team} vs {item.match.away_team}"
        )
        lines.append(
            f"   {item.market_label} @ {item.odds:.2f} | xG {(_safe_metric(item.match.home_xg, 1.35)):.2f}:{(_safe_metric(item.match.away_xg, 1.05)):.2f} | уверенность {item.confidence}/5"
        )
        lines.append(f"   {item.reasoning}")

    return "\n".join(lines)[:3900]


def format_user_digest(
    recommendations: list[MatchRecommendation],
    summary: dict[str, float | int | str],
    target_date: date | None = None,
) -> str:
    selected_date = target_date or datetime.now().date()
    if not recommendations:
        return (
            f"📬 Витрина матчей на {selected_date.strftime('%d.%m.%Y')}\n\n"
            "Сегодня не нашлось матчей с достаточными данными.\n"
            f"Ваш банк: {format_money(float(summary['bankroll']))} руб."
        )

    lines = [
        f"📬 Витрина матчей на {selected_date.strftime('%d.%m.%Y')}",
        f"💰 Банк: {format_money(float(summary['bankroll']))} руб. | ROI: {summary['roi']}% | Winrate: {summary['winrate']}%",
        "",
    ]
    for index, item in enumerate(recommendations, start=1):
        lines.append(
            f"{index}. {item.match.home_team} vs {item.match.away_team} | {item.market_label} @ {item.odds:.2f}"
        )
        lines.append(
            f"   Ставка: {format_money(item.stake)} руб. | Уверенность: {item.confidence}/5"
        )
        lines.append(f"   {item.reasoning}")
    return "\n".join(lines)[:3900]
