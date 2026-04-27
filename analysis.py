"""
analysis.py — математика xG Master Bot.

Стек:
- LEAGUE_XG_PRIORS: средние xG по лиге (FBref / Understat 2024/25).
- Форма команды (gf/ga за 5 матчей) подмешивается в λ — модель
  становится независимой от 1X2-линии букмекера.
- Dixon-Coles correction (rho = -0.10) для tau(0,0)/(0,1)/(1,0)/(1,1):
  поднимает вероятность 0:0/1:0/0:1/1:1 на низких xG, что даёт
  корректные оценки BTTS-Нет и Под 2.5.
- Маржа букмекера снимается power-методом.

Edge-фильтр поднят до 3% (config.min_edge): на стандартной EU-маржe
6-8% значения < 3% — это шум модели, не сигнал.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.stats import poisson

from config import config
from data_sources import Odds, TeamForm


MAX_GOALS = 10
DC_RHO = -0.10  # Dixon-Coles low-score correction strength

# ── Средние xG по лигам (сезон 2024/25, источник: FBref / Understat) ─────────
# Формат: (home_xg, away_xg) — средние за матч
LEAGUE_XG_PRIORS: dict[str, tuple[float, float]] = {
    "premier league": (1.55, 1.15),
    "epl": (1.55, 1.15),
    "primera division": (1.45, 1.05),
    "la liga": (1.45, 1.05),
    "bundesliga": (1.65, 1.20),
    "serie a": (1.40, 1.00),
    "ligue 1": (1.35, 1.00),
    "eredivisie": (1.70, 1.30),
    "primeira liga": (1.40, 1.05),
    "championship": (1.35, 1.10),
    "efl championship": (1.35, 1.10),
    "default": (1.45, 1.10),
}


def _league_prior(competition: str) -> tuple[float, float]:
    key = competition.lower().strip()
    for k, v in LEAGUE_XG_PRIORS.items():
        if k in key or key in k:
            return v
    return LEAGUE_XG_PRIORS["default"]


# ──────────────────────────────────────────────────────────────────
# Dixon-Coles low-score correction
# ──────────────────────────────────────────────────────────────────
def _dc_tau(i: int, j: int, lh: float, la: float, rho: float = DC_RHO) -> float:
    if i == 0 and j == 0:
        return 1.0 - lh * la * rho
    if i == 0 and j == 1:
        return 1.0 + lh * rho
    if i == 1 and j == 0:
        return 1.0 + la * rho
    if i == 1 and j == 1:
        return 1.0 - rho
    return 1.0


def _score_matrix(home_xg: float, away_xg: float) -> np.ndarray:
    home_xg = max(0.1, home_xg)
    away_xg = max(0.1, away_xg)
    h = np.array([poisson.pmf(i, home_xg) for i in range(MAX_GOALS)])
    a = np.array([poisson.pmf(i, away_xg) for i in range(MAX_GOALS)])
    m = np.outer(h, a)
    # Dixon-Coles correction для четырёх low-score ячеек
    for i, j in [(0, 0), (0, 1), (1, 0), (1, 1)]:
        m[i, j] *= _dc_tau(i, j, home_xg, away_xg)
    total = m.sum()
    if total > 0:
        m /= total
    return m


def poisson_probs(home_xg: float, away_xg: float) -> dict:
    m = _score_matrix(home_xg, away_xg)
    p_home = float(np.sum(np.tril(m, -1)))
    p_draw = float(np.sum(np.diag(m)))
    p_away = float(np.sum(np.triu(m, 1)))

    over_2_5 = float(sum(
        m[i, j] for i in range(MAX_GOALS) for j in range(MAX_GOALS) if i + j >= 3
    ))
    under_2_5 = max(0.0, 1.0 - over_2_5)

    p_h0 = float(np.sum(m[0, :]))
    p_a0 = float(np.sum(m[:, 0]))
    p_00 = float(m[0, 0])
    btts_yes = max(0.0, 1.0 - p_h0 - p_a0 + p_00)
    btts_no = max(0.0, 1.0 - btts_yes)

    return {
        "home": p_home, "draw": p_draw, "away": p_away,
        "over_2_5": over_2_5, "under_2_5": under_2_5,
        "btts_yes": btts_yes, "btts_no": btts_no,
    }


# ──────────────────────────────────────────────────────────────────
# Снятие маржи букмекера (power-метод)
# ──────────────────────────────────────────────────────────────────
def implied_probs_fair(odds_list: list[float]) -> list[float]:
    odds = [o for o in odds_list if o and o > 1]
    if len(odds) < 2:
        return [1.0 / o if o > 1 else 0.0 for o in odds_list]

    def f(k: float) -> float:
        return sum(math.pow(1.0 / o, k) for o in odds) - 1.0

    lo, hi = 0.5, 2.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2
    return [math.pow(1.0 / o, k) if o and o > 1 else 0.0 for o in odds_list]


def fair_odds_from_probability(prob: float) -> float:
    return round(1.0 / prob, 2) if prob > 0 else 0.0


# ──────────────────────────────────────────────────────────────────
# Оценка xG: league prior + форма команд (БЕЗ 1X2)
# ──────────────────────────────────────────────────────────────────
def _form_lambda(form: Optional[TeamForm], league_avg: float) -> float:
    """Берём gf/ga за прошедшие матчи и комбинируем с league average."""
    if not form:
        return league_avg
    games = form.wins + form.draws + form.losses
    if games < 2:
        return league_avg
    team_gf = form.goals_for / games
    team_ga = form.goals_against / games
    # Сила атаки относительно league avg, оборона соперника подмешается через base_a
    return max(0.3, min(3.5, 0.5 * league_avg + 0.5 * team_gf))


def xg_from_odds(
    odds: Odds,
    competition: str = "",
    home_form: Optional[TeamForm] = None,
    away_form: Optional[TeamForm] = None,
) -> tuple[float, float]:
    """
    ИСПРАВЛЕНО: xG больше не выводится из 1X2-коэффициентов.

    λ_home = blend(league_prior_home, team_gf_home_per_game) с поправкой
             на оборону гостей (team_ga_away_per_game vs league_avg).
    λ_away — симметрично.

    Это делает Poisson-модель полностью независимой от рынка букмекера —
    edge на тоталах/BTTS теперь real cross-market, а не круговая зависимость.
    """
    base_h, base_a = _league_prior(competition)

    lh = _form_lambda(home_form, base_h)
    la = _form_lambda(away_form, base_a)

    # Defence adjustment: если у соперника пропуски выше league avg → λ растёт
    if home_form:
        games = home_form.wins + home_form.draws + home_form.losses
        if games >= 2:
            home_ga = home_form.goals_against / games
            la *= max(0.7, min(1.4, home_ga / max(0.5, base_a)))
    if away_form:
        games = away_form.wins + away_form.draws + away_form.losses
        if games >= 2:
            away_ga = away_form.goals_against / games
            lh *= max(0.7, min(1.4, away_ga / max(0.5, base_h)))

    lh = round(max(0.3, min(3.5, lh)), 2)
    la = round(max(0.3, min(3.5, la)), 2)
    return lh, la


# ──────────────────────────────────────────────────────────────────
# Kelly-критерий
# ──────────────────────────────────────────────────────────────────
def kelly_stake(bank: float, prob: float, book_odds: float,
                cap: Optional[float] = None) -> float:
    if cap is None:
        cap = config.kelly_cap
    if bank <= 0 or book_odds <= 1 or prob <= 0:
        return 0.0
    edge = prob * book_odds - 1
    if edge <= 0:
        return 0.0
    fraction = min(edge / (book_odds - 1), cap)
    return round(bank * max(0.0, fraction), 2)


# ──────────────────────────────────────────────────────────────────
# Picks
# ──────────────────────────────────────────────────────────────────
@dataclass
class Pick:
    market: str
    pick: str
    probability: float
    book_odds: float
    fair_odds: float
    edge: float
    recommended_stake: float
    market_probability: float = 0.0  # fair_prob букмекера для словесного сравнения


def _pack(market: str, label: str, prob: float, book: float, market_prob: float,
          bank: float, min_edge: float,
          min_conf: float) -> Optional[Pick]:
    if book <= 1 or prob <= 0:
        return None
    if prob < min_conf:
        return None
    fair = fair_odds_from_probability(prob)
    edge = prob * book - 1
    if edge < min_edge:
        return None
    stake = kelly_stake(bank, prob, book)
    if stake <= 0:
        return None
    return Pick(
        market=market, pick=label, probability=prob,
        book_odds=round(book, 2), fair_odds=fair,
        edge=round(edge, 3), recommended_stake=stake,
        market_probability=round(market_prob, 4),
    )


def build_value_picks(home: str, away: str, odds: Odds,
                      model: dict, bank: float,
                      min_edge: Optional[float] = None,
                      min_conf: float = 0.40) -> list[Pick]:
    """
    Строим cross-market value-сигналы:
    - 1X2: только если модель и рынок расходятся ≥ 4% (cross-market check).
    - Тоталы и BTTS: основная зона value, сравниваем модельные p против fair-prob рынка.
    """
    if min_edge is None:
        min_edge = config.min_edge

    candidates: list[Optional[Pick]] = []
    fair_1x2 = implied_probs_fair([odds.home, odds.draw, odds.away]) \
        if odds.has_1x2() else [0.0, 0.0, 0.0]

    # 1X2: cross-market gate ≥ 4%
    for label, m_prob, f_prob, book in [
        (f"Победа {home}", model["home"], fair_1x2[0], odds.home),
        (f"Победа {away}", model["away"], fair_1x2[2], odds.away),
    ]:
        if abs(m_prob - f_prob) >= 0.04:
            p = _pack("1X2", label, m_prob, book, f_prob, bank, min_edge, min_conf)
            if p:
                candidates.append(p)

    # Тоталы 2.5
    if odds.over_2_5 > 1 and odds.under_2_5 > 1:
        fair_tot = implied_probs_fair([odds.over_2_5, odds.under_2_5])
        candidates.append(_pack("TOTAL_2_5", "Тотал больше 2.5",
                                model["over_2_5"], odds.over_2_5, fair_tot[0],
                                bank, min_edge, min_conf))
        candidates.append(_pack("TOTAL_2_5", "Тотал меньше 2.5",
                                model["under_2_5"], odds.under_2_5, fair_tot[1],
                                bank, min_edge, min_conf))

    # BTTS
    if odds.btts_yes > 1 and odds.btts_no > 1:
        fair_btts = implied_probs_fair([odds.btts_yes, odds.btts_no])
        candidates.append(_pack("BTTS", "Обе забьют — Да",
                                model["btts_yes"], odds.btts_yes, fair_btts[0],
                                bank, min_edge, min_conf))
        candidates.append(_pack("BTTS", "Обе забьют — Нет",
                                model["btts_no"], odds.btts_no, fair_btts[1],
                                bank, min_edge, min_conf))

    picks = [p for p in candidates if p is not None]
    picks.sort(key=lambda p: p.edge, reverse=True)
    return picks


def best_value_pick(home: str, away: str, odds: Odds,
                    model: dict, bank: float) -> Optional[Pick]:
    return next(iter(build_value_picks(home, away, odds, model, bank)), None)


def best_guess_pick(home: str, away: str, odds: Odds,
                    model: dict, bank: float) -> Optional[Pick]:
    """
    FALLBACK: если строгих value-ставок нет — мягкие фильтры
    (edge ≥ -3%, conf ≥ 32%). Используется для показа пользователю.
    """
    picks = build_value_picks(
        home, away, odds, model, bank,
        min_edge=-0.03,
        min_conf=0.32,
    )
    return picks[0] if picks else None
