from __future__ import annotations

import numpy as np
from dataclasses import dataclass
from typing import Dict, Optional

from config import settings
from modules.data_sources import TeamContext


@dataclass
class PredictionResult:
    home_win_prob: float
    draw_prob: float
    away_win_prob: float
    expected_home_goals: float
    expected_away_goals: float
    confidence: int          # от 0 до 100
    value_bet: float         # в процентах
    recommended_stake: float
    best_bet: str            # "1", "X", "2"
    reasoning: str
    xg_chart: bytes | None = None  # будет None, если matplotlib не установлен


class AiAnalyst:
    """
    Научная модель прогнозов на основе:
    - Expected Goals (xG)
    - Poisson distribution
    - Dixon-Coles correction
    - Value Betting
    - Partial Kelly Criterion
    """

    def generate_prediction(self, context: TeamContext, bankroll: float) -> PredictionResult:
        # === 1. Базовые ожидаемые голы ===
        home_xg = context.home_xg or 1.45
        away_xg = context.away_xg or 1.15

        # Учёт домашнего преимущества
        lambda_home = home_xg * 1.12   # home advantage factor
        lambda_away = away_xg * 0.88

        # === 2. Расчёт вероятностей через Poisson ===
        max_goals = 7
        home_win_prob = draw_prob = away_win_prob = 0.0

        for h in range(max_goals + 1):
            for a in range(max_goals + 1):
                # Poisson probability
                p_home = self._poisson_pmf(lambda_home, h)
                p_away = self._poisson_pmf(lambda_away, a)
                prob = p_home * p_away

                # Dixon-Coles correction для низовых результатов
                prob *= self._dixon_coles_correction(h, a)

                if h > a:
                    home_win_prob += prob
                elif h == a:
                    draw_prob += prob
                else:
                    away_win_prob += prob

        # Нормализация
        total = home_win_prob + draw_prob + away_win_prob
        home_win_prob /= total
        draw_prob /= total
        away_win_prob /= total

        # === 3. Определение лучшей ставки ===
        probs = {"1": home_win_prob, "X": draw_prob, "2": away_win_prob}
        best_bet = max(probs, key=probs.get)
        confidence = int(max(probs.values()) * 100)

        # === 4. Value Betting ===
        value_bet = 0.0
        best_odds = None

        if context.odds and best_bet in context.odds:
            decimal_odds = context.odds[best_bet]
            implied_prob = 1.0 / decimal_odds
            value_bet = (probs[best_bet] - implied_prob) / implied_prob * 100
            best_odds = decimal_odds

        # === 5. Рекомендуемая ставка (Partial Kelly) ===
        recommended_stake = self._calculate_stake(
            bankroll=bankroll,
            confidence=confidence,
            odds=best_odds
        )

        # === 6. Reasoning ===
        reasoning = (
            f"Ожидаемые голы: {lambda_home:.2f} — {lambda_away:.2f}\n"
            f"Вероятности: П1 {home_win_prob:.1%} | Ничья {draw_prob:.1%} | П2 {away_win_prob:.1%}\n"
            f"Value: {value_bet:+.1f}%\n"
            f"Модель: Poisson + Dixon-Coles + xG"
        )

        return PredictionResult(
            home_win_prob=round(home_win_prob, 4),
            draw_prob=round(draw_prob, 4),
            away_win_prob=round(away_win_prob, 4),
            expected_home_goals=round(lambda_home, 2),
            expected_away_goals=round(lambda_away, 2),
            confidence=confidence,
            value_bet=round(value_bet, 1),
            recommended_stake=round(recommended_stake, 2),
            best_bet=best_bet,
            reasoning=reasoning,
            xg_chart=None  # позже можно добавить график
        )

    def _poisson_pmf(self, lam: float, k: int) -> float:
        """Probability Mass Function для Poisson"""
        if lam <= 0:
            return 0.0
        return (lam ** k) * np.exp(-lam) / np.math.factorial(k)

    def _dixon_coles_correction(self, home_goals: int, away_goals: int) -> float:
        """Корректировка Dixon-Coles"""
        if home_goals == 0 and away_goals == 0:
            return 1.35
        elif home_goals == 1 and away_goals == 0:
            return 0.92
        elif home_goals == 0 and away_goals == 1:
            return 0.92
        elif home_goals == 1 and away_goals == 1:
            return 0.95
        return 1.0

    def _calculate_stake(self, bankroll: float, confidence: int, odds: float | None = None) -> float:
        """Partial Kelly + safety rules"""
        if bankroll <= 0:
            return 0.0

        # Базовый Kelly
        if odds and odds > 1.0:
            decimal_prob = 1.0 / odds
            edge = (confidence / 100.0) - decimal_prob
            if edge > 0:
                kelly = edge / (odds - 1)
                stake = bankroll * kelly * 0.45   # Partial Kelly (45% от полного)
                return min(stake, bankroll * 0.08)  # max 8% банка на одну ставку

        # Если нет коэффициентов — flat ставка
        return bankroll * 0.035  # 3.5% от банка


# ====================== ГЛОБАЛЬНЫЙ ЭКЗЕМПЛЯР ======================
ai_analyst = AiAnalyst()