"""
analysis.py — математика xG Master Bot.

Включает:
  - Poisson-модель для 1X2, тоталов, BTTS.
  - Снятие маржи букмекера (power-метод) для fair-odds.
  - Kelly criterion с верхней планкой.
  - Сборку value-picks из модельных вероятностей и коэффициентов.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.stats import poisson

from config import config
from data_sources import Odds


MAX_GOALS = 10


# ────────────────────────────────────────────────────────────────
# Poisson-модель (1X2, тоталы, BTTS)
# ────────────────────────────────────────────────────────────────
def _score_matrix(home_xg: float, away_xg: float) -> np.ndarray:
    home_xg = max(0.05, home_xg)
    away_xg = max(0.05, away_xg)
    h = np.array([poisson.pmf(i, home_xg) for i in range(MAX_GOALS)])
    a = np.array([poisson.pmf(i, away_xg) for i in range(MAX_GOALS)])
    return np.outer(h, a)


def poisson_probs(home_xg: float, away_xg: float) -> dict:
    m = _score_matrix(home_xg, away_xg)
    p_home = float(np.sum(np.tril(m, -1)))
    p_draw = float(np.sum(np.diag(m)))
    p_away = float(np.sum(np.triu(m, 1)))

    # Тотал 2.5: суммируем клетки, где i+j >= 3
    over_2_5 = 0.0
    for i in range(MAX_GOALS):
        for j in range(MAX_GOALS):
            if i + j >= 3:
                over_2_5 += m[i, j]
    over_2_5 = float(over_2_5)
    under_2_5 = max(0.0, 1.0 - over_2_5)

    # BTTS = 1 - P(home=0) - P(away=0) + P(both=0)
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


# ────────────────────────────────────────────────────────────────
# Fair odds из рынка (power-метод снятия маржи)
# ────────────────────────────────────────────────────────────────
def implied_probs_fair(odds_list: list[float]) -> list[float]:
    """Power-метод: находит k, такое что sum(1/o)^k = 1."""
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
    if prob <= 0:
        return 0.0
    return round(1.0 / prob, 2)


# ────────────────────────────────────────────────────────────────
# Kelly-критерий
# ────────────────────────────────────────────────────────────────
def kelly_stake(bank: float, prob: float, book_odds: float, cap: Optional[float] = None) -> float:
    """Дробный Kelly, ограниченный cap (по умолчанию — config.kelly_cap)."""
    if cap is None:
        cap = config.kelly_cap
    if bank <= 0 or book_odds <= 1 or prob <= 0:
        return 0.0
    edge = prob * book_odds - 1
    if edge <= 0:
        return 0.0
    fraction = min(edge / (book_odds - 1), cap)
    fraction = max(0.0, fraction)
    return round(bank * fraction, 2)


# ────────────────────────────────────────────────────────────────
# Сборка value-picks
# ────────────────────────────────────────────────────────────────
@dataclass
class Pick:
    market: str           # "1X2" / "TOTAL_2_5" / "BTTS"
    pick: str             # человекочитаемый лейбл
    probability: float    # модельная вероятность (0..1)
    book_odds: float      # коэф букмекера
    fair_odds: float      # 1 / probability
    edge: float           # probability * book_odds - 1
    recommended_stake: float


def _pack(market: str, label: str, prob: float, book: float, bank: float) -> Optional[Pick]:
    if book <= 1 or prob <= 0:
        return None
    fair = fair_odds_from_probability(prob)
    edge = prob * book - 1
    if edge < config.min_edge:
        return None
    stake = kelly_stake(bank, prob, book)
    if stake <= 0:
        return None
    return Pick(
        market=market, pick=label, probability=prob,
        book_odds=round(book, 2), fair_odds=fair,
        edge=round(edge, 3), recommended_stake=stake,
    )


def build_value_picks(home: str, away: str, odds: Odds, model: dict, bank: float) -> list[Pick]:
    """Возвращает все value-ставки, отсортированные по edge (убывание)."""
    candidates: list[Optional[Pick]] = [
        _pack("1X2", f"Победа {home}", model["home"], odds.home, bank),
        _pack("1X2", "Ничья", model["draw"], odds.draw, bank),
        _pack("1X2", f"Победа {away}", model["away"], odds.away, bank),
        _pack("TOTAL_2_5", "Тотал больше 2.5", model["over_2_5"], odds.over_2_5, bank),
        _pack("TOTAL_2_5", "Тотал меньше 2.5", model["under_2_5"], odds.under_2_5, bank),
        _pack("BTTS", "Обе забьют — Да", model["btts_yes"], odds.btts_yes, bank),
        _pack("BTTS", "Обе забьют — Нет", model["btts_no"], odds.btts_no, bank),
    ]
    picks = [p for p in candidates if p is not None]
    picks.sort(key=lambda p: p.edge, reverse=True)
    return picks


def best_value_pick(home: str, away: str, odds: Odds, model: dict, bank: float) -> Optional[Pick]:
    picks = build_value_picks(home, away, odds, model, bank)
    return picks[0] if picks else None


# ────────────────────────────────────────────────────────────────
# Оценка xG из коэффициентов (грубая аппроксимация)
# ────────────────────────────────────────────────────────────────
def xg_from_odds(odds: Odds) -> tuple[float, float]:
    """Если у нас нет xG-данных, оценим их из рыночных 1X2.
    Используем простой итеративный подбор lambda_home, lambda_away,
    чтобы poisson_probs матчило рыночным fair-probs."""
    if not odds.has_1x2():
        return 1.4, 1.2
    fair = implied_probs_fair([odds.home, odds.draw, odds.away])
    p_home, _, p_away = fair[0], fair[1], fair[2]

    # Начальная оценка через медианные xG в топ-лигах (~1.4 home / 1.2 away)
    lh, la = 1.4, 1.2
    for _ in range(80):
        m = poisson_probs(lh, la)
        err_h = p_home - m["home"]
        err_a = p_away - m["away"]
        lh = max(0.2, min(4.0, lh + err_h * 1.2))
        la = max(0.2, min(4.0, la + err_a * 1.2))
    return round(lh, 2), round(la, 2)
