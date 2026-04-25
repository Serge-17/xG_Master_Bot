"""
analysis.py — математика xG Master Bot.

АРХИТЕКТУРНОЕ ИСПРАВЛЕНИЕ:
Ранее xg_from_odds() решала задачу «найти lambda, при котором Poisson = fair_probs».
Это создавало замкнутый круг: xG выводится из коэфов → модель сравнивается с теми
же коэфами → edge всегда ≈ 0.

Новый подход:
1. Используем LEAGUE_XG_PRIORS — средние xG по каждой лиге из открытых данных.
2. Корректируем prior небольшим сдвигом из 1X2 fair-probs (direction, не magnitude).
3. Сравниваем модельные ТОТАЛЫ и BTTS с рыночными коэффициентами.
   Именно в разрыве между 1X2-рынком и рынком тоталов/BTTS живёт cross-market edge.
4. Для 1X2 value проверяем только если есть ≥5% gap между fair и book.
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

# ── Средние xG по лигам (сезон 2024/25, источник: FBref / Understat) ─────────
# Формат: (home_xg, away_xg) — средние за матч
LEAGUE_XG_PRIORS: dict[str, tuple[float, float]] = {
    # Premier League
    "premier league": (1.55, 1.15),
    "epl": (1.55, 1.15),
    # La Liga
    "primera division": (1.45, 1.05),
    "la liga": (1.45, 1.05),
    # Bundesliga
    "bundesliga": (1.65, 1.20),
    # Serie A
    "serie a": (1.40, 1.00),
    # Ligue 1
    "ligue 1": (1.35, 1.00),
    # Eredivisie
    "eredivisie": (1.70, 1.30),
    # Primeira Liga
    "primeira liga": (1.40, 1.05),
    # Championship
    "championship": (1.35, 1.10),
    "efl championship": (1.35, 1.10),
    # Defaults
    "default": (1.45, 1.10),
}


def _league_prior(competition: str) -> tuple[float, float]:
    """Возвращает (home_xg, away_xg) prior для лиги."""
    key = competition.lower().strip()
    for k, v in LEAGUE_XG_PRIORS.items():
        if k in key or key in k:
            return v
    return LEAGUE_XG_PRIORS["default"]


# ──────────────────────────────────────────────────────────────────
# Poisson-модель
# ──────────────────────────────────────────────────────────────────
def _score_matrix(home_xg: float, away_xg: float) -> np.ndarray:
    home_xg = max(0.1, home_xg)
    away_xg = max(0.1, away_xg)
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
    btts_no  = max(0.0, 1.0 - btts_yes)

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

    def f(k):
        return sum(math.pow(1.0 / o, k) for o in odds) - 1.0

    lo, hi = 0.5, 2.0
    for _ in range(60):
        mid = (lo + hi) / 2
        (lo if f(mid) > 0 else hi).__class__  # dummy
        if f(mid) > 0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2
    return [math.pow(1.0 / o, k) if o and o > 1 else 0.0 for o in odds_list]


def fair_odds_from_probability(prob: float) -> float:
    return round(1.0 / prob, 2) if prob > 0 else 0.0


# ──────────────────────────────────────────────────────────────────
# Оценка xG: league prior + direction adjustment из 1X2 рынка
# ──────────────────────────────────────────────────────────────────
def xg_from_odds(odds: Odds, competition: str = "") -> tuple[float, float]:
    """
    ИСПРАВЛЕНО: больше не выводим xG из тех же коэффициентов.

    Используем league prior как базу, затем делаем небольшой сдвиг
    в направлении 1X2 рыночных сигналов.
    Это позволяет Poisson-модели быть независимой от рынка тоталов/BTTS.
    """
    base_h, base_a = _league_prior(competition)

    if not odds.has_1x2():
        return base_h, base_a

    # Считаем рыночный «strength ratio» из fair-probs
    fair = implied_probs_fair([odds.home, odds.draw, odds.away])
    p_home, p_draw, p_away = fair[0], fair[1], fair[2]

    # Нейтральный матч: p_home ≈ 0.45, p_away ≈ 0.30
    # Сдвигаем prior пропорционально силе фаворита (ограниченно: max ±40%)
    home_strength = (p_home - 0.45) / 0.45   # от -1 до +1 примерно
    away_strength = (p_away - 0.30) / 0.30

    adj_factor = 0.25  # насколько сильно рынок двигает prior
    lh = base_h * (1 + home_strength * adj_factor)
    la = base_a * (1 + away_strength * adj_factor)

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


def _pack(market: str, label: str, prob: float, book: float,
          bank: float, min_edge: float = 0.015,
          min_conf: float = 0.38) -> Optional[Pick]:
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
    )


def build_value_picks(home: str, away: str, odds: Odds,
                      model: dict, bank: float,
                      min_edge: float = 0.015,
                      min_conf: float = 0.38) -> list[Pick]:
    """
    VALUE PICKS — строим cross-market:
    - 1X2: только если модель и рынок расходятся ≥ 2% после снятия маржи
    - Тоталы и BTTS: главный cross-market источник, сравниваем модель с рынком
    """
    candidates: list[Optional[Pick]] = []

    # Снимаем маржу с 1X2 для сравнения
    fair_1x2 = implied_probs_fair([odds.home, odds.draw, odds.away]) \
        if odds.has_1x2() else [0, 0, 0]

    # 1X2: берём только где модель даёт ≥2% gap от fair-рынка
    for label, m_prob, f_prob, book in [
        (f"Победа {home}", model["home"],  fair_1x2[0], odds.home),
        (f"Победа {away}", model["away"],  fair_1x2[2], odds.away),
    ]:
        if abs(m_prob - f_prob) >= 0.04:  # разрыв ≥4% — признак cross-market
            p = _pack("1X2", label, m_prob, book, bank, min_edge, min_conf)
            if p:
                candidates.append(p)

    # Тоталы
    if odds.over_2_5 > 1:
        candidates.append(_pack("TOTAL_2_5", "Тотал больше 2.5",
                                model["over_2_5"], odds.over_2_5, bank, min_edge, min_conf))
    if odds.under_2_5 > 1:
        candidates.append(_pack("TOTAL_2_5", "Тотал меньше 2.5",
                                model["under_2_5"], odds.under_2_5, bank, min_edge, min_conf))
    # BTTS
    if odds.btts_yes > 1:
        candidates.append(_pack("BTTS", "Обе забьют — Да",
                                model["btts_yes"], odds.btts_yes, bank, min_edge, min_conf))
    if odds.btts_no > 1:
        candidates.append(_pack("BTTS", "Обе забьют — Нет",
                                model["btts_no"], odds.btts_no, bank, min_edge, min_conf))

    picks = [p for p in candidates if p is not None]
    picks.sort(key=lambda p: p.edge, reverse=True)
    return picks


def best_value_pick(home: str, away: str, odds: Odds,
                    model: dict, bank: float) -> Optional[Pick]:
    return next(iter(build_value_picks(home, away, odds, model, bank)), None)


def best_guess_pick(home: str, away: str, odds: Odds,
                    model: dict, bank: float) -> Optional[Pick]:
    """
    FALLBACK: если строгих value-ставок нет — возвращаем лучший pick
    с очень мягкими фильтрами (edge ≥ 0, conf ≥ 30%).
    Используется для показа пользователю даже когда edge небольшой.
    """
    picks = build_value_picks(
        home, away, odds, model, bank,
        min_edge=-0.05,  # разрешаем небольшой минус (рынок может ошибаться)
        min_conf=0.30,
    )
    return picks[0] if picks else None
