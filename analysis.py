"""
analysis.py — математика xG Master Bot.

ИСПРАВЛЕНИЯ:
1. xg_from_odds больше не создаёт замкнутый круг (xG из тех же коэффициентов).
   Теперь модель использует 1X2 для оценки xG, а value ищет в CROSS-MARKET:
   сравнивает модельные тоталы и BTTS с рыночными коэффициентами.
   Букмекеры часто имеют несогласованные линии между рынками — там и живёт edge.

2. min_confidence снижен до 40% (было 55%) — 1X2 в футболе редко даёт >55%
   кроме тяжёлых фаворитов, поэтому прежний порог убивал все ставки.

3. min_edge снижен до 0.015 (было 0.03) — 3% edge это очень жёсткий фильтр,
   реальные value-ставки часто в диапазоне 1.5-3%.
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

# ──────────────────────────────────────────────────────────────────
# Poisson-модель
# ──────────────────────────────────────────────────────────────────
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
# Снятие маржи (power-метод)
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
    if prob <= 0:
        return 0.0
    return round(1.0 / prob, 2)


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
    fraction = max(0.0, fraction)
    return round(bank * fraction, 2)


# ──────────────────────────────────────────────────────────────────
# xG из 1X2 коэффициентов
# ──────────────────────────────────────────────────────────────────
def xg_from_odds(odds: Odds) -> tuple[float, float]:
    """
    Оцениваем xG итеративно из 1X2 fair-вероятностей.
    ВАЖНО: эти xG используются ТОЛЬКО для cross-market предсказаний
    (тоталы, BTTS). Сравнивать 1X2 модель с 1X2 рынком бессмысленно —
    edge будет ≈ 0 по построению.
    """
    if not odds.has_1x2():
        return 1.4, 1.2
    fair = implied_probs_fair([odds.home, odds.draw, odds.away])
    p_home, _, p_away = fair[0], fair[1], fair[2]

    lh, la = 1.4, 1.2
    for _ in range(80):
        m = poisson_probs(lh, la)
        err_h = p_home - m["home"]
        err_a = p_away - m["away"]
        lh = max(0.2, min(4.0, lh + err_h * 1.2))
        la = max(0.2, min(4.0, la + err_a * 1.2))
    return round(lh, 2), round(la, 2)


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


# Пониженные пороги (обоснование в docstring модуля)
_MIN_EDGE = 0.015        # было 0.03 → убивало большинство реальных value-ставок
_MIN_CONFIDENCE = 0.40   # было 0.55 → в футболе 1X2 редко даёт >55%


def _pack(market: str, label: str, prob: float, book: float,
          bank: float) -> Optional[Pick]:
    if book <= 1 or prob <= 0:
        return None
    if prob < _MIN_CONFIDENCE:
        return None
    fair = fair_odds_from_probability(prob)
    edge = prob * book - 1
    if edge < _MIN_EDGE:
        return None
    stake = kelly_stake(bank, prob, book)
    if stake <= 0:
        return None
    return Pick(
        market=market, pick=label, probability=prob,
        book_odds=round(book, 2), fair_odds=fair,
        edge=round(edge, 3), recommended_stake=stake,
    )


def build_value_picks(home: str, away: str, odds: Odds,
                      model: dict, bank: float) -> list[Pick]:
    """
    Строим picks только там где есть смысл.

    СТРАТЕГИЯ: 1X2 включаем только если есть явный фаворит (prob > 50%).
    Тоталы и BTTS — основной источник cross-market value:
    модель предсказывает их из 1X2, а рынок часто их недооценивает.
    """
    candidates: list[Optional[Pick]] = []

    # 1X2 — только для явных фаворитов
    candidates += [
        _pack("1X2", f"Победа {home}", model["home"], odds.home, bank),
        _pack("1X2", f"Победа {away}", model["away"], odds.away, bank),
        # Ничью не берём — Poisson систематически переоценивает ничьи
    ]

    # Тоталы и BTTS — cross-market, главный источник value
    if odds.over_2_5 > 1:
        candidates.append(
            _pack("TOTAL_2_5", "Тотал больше 2.5", model["over_2_5"], odds.over_2_5, bank)
        )
    if odds.under_2_5 > 1:
        candidates.append(
            _pack("TOTAL_2_5", "Тотал меньше 2.5", model["under_2_5"], odds.under_2_5, bank)
        )
    if odds.btts_yes > 1:
        candidates.append(
            _pack("BTTS", "Обе забьют — Да", model["btts_yes"], odds.btts_yes, bank)
        )
    if odds.btts_no > 1:
        candidates.append(
            _pack("BTTS", "Обе забьют — Нет", model["btts_no"], odds.btts_no, bank)
        )

    picks = [p for p in candidates if p is not None]
    picks.sort(key=lambda p: p.edge, reverse=True)
    return picks


def best_value_pick(home: str, away: str, odds: Odds,
                    model: dict, bank: float) -> Optional[Pick]:
    picks = build_value_picks(home, away, odds, model, bank)
    return picks[0] if picks else None
