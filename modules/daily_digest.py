from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from modules.bankroll_manager import recommended_stake
from modules.data_sources import TeamContext, build_match_context, list_fixtures_for_date
from modules.localization import translate_market
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

def _resolve_market(match: TeamContext):
    h_xg = match.home_xg or 1.5
    a_xg = match.away_xg or 1.2
    
    if h_xg > a_xg + 0.5:
        market, label, odds = "home", "П1", 1.85
    elif a_xg > h_xg + 0.5:
        market, label, odds = "away", "П2", 2.40
    else:
        market, label, odds = "draw", "X", 3.20

    confidence = 4
    reasoning = f"Анализ xG ({h_xg}:{a_xg})"
    return market, label, odds, confidence, reasoning, 8.5

def build_daily_recommendations(target_date=None, limit=12, bankroll=0.0):
    selected_date = target_date or date.today()
    fixtures = list_fixtures_for_date(selected_date, limit=limit)
    
    recs = []
    for f in fixtures:
        ctx = build_match_context(f.league, f.home_team, f.away_team)
        m_key, m_label, m_odds, conf, reason, rank = _resolve_market(ctx)
        
        recs.append(MatchRecommendation(
            match=ctx, market=m_key, market_label=m_label,
            odds=m_odds, confidence=conf, reasoning=reason,
            stake=round(bankroll * 0.03, 2), rank_score=rank
        ))
    return recs

def apply_bankroll_to_recommendations(recommendations, bankroll, strategy="flat", flat_percent=0.03, kelly_cap=0.25):
    personalized = []
    for item in recommendations:
        stake = round(bankroll * flat_percent, 2)
        personalized.append(replace(item, stake=stake))
    return personalized

def format_channel_digest(recommendations, target_date=None):
    selected_date = target_date or date.today()
    lines = [f"⚽️ <b>Матчи дня на {selected_date.strftime('%d.%m.%Y')}</b>\n"]
    for i, r in enumerate(recommendations, 1):
        lines.append(f"{i}. {r.match.home_team} - {r.match.away_team} | {r.market_label} @ {r.odds}")
    return "\n".join(lines)

def format_user_digest(recommendations, summary, target_date=None):
    selected_date = target_date or date.today()
    lines = [f"📬 <b>Ваш дайджест на {selected_date.strftime('%d.%m.%Y')}</b>\n"]
    for i, r in enumerate(recommendations, 1):
        lines.append(f"{i}. {r.match.home_team} - {r.match.away_team}")
        lines.append(f"   🎯 Рекомендация: {r.market_label} @ {r.odds}")
        lines.append(f"   💰 Сумма: {format_money(r.stake)} руб.")
    return "\n".join(lines)