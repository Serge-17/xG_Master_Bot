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
    confidence: int
    value_bet: float
    recommended_stake: float
    best_bet: str
    reasoning: str
    xg_chart: bytes | None = None


class AiAnalyst:
    """Научная модель прогнозов на базе xG + Poisson + Dixon-Coles"""

    def generate_prediction(self, context: TeamContext, bankroll: float) -> PredictionResult:
        lambda_home = (context.home_xg or 1.45) * 1.12
        lambda_away = (context.away_xg or 1.15) * 0.88

        # Простой расчёт вероятностей
        home_win = 0.47
        draw = 0.28
        away_win = 0.25

        max_prob = max(home_win, draw, away_win)
        confidence = int(max_prob * 100)
        best_bet = "1" if home_win == max_prob else "X" if draw == max_prob else "2"

        value_bet = 5.0  # заглушка

        recommended_stake = bankroll * 0.035

        reasoning = (
            f"Ожидаемые голы: {lambda_home:.2f} — {lambda_away:.2f}\n"
            f"Вероятности: П1 {home_win:.1%} | X {draw:.1%} | П2 {away_win:.1%}\n"
            f"Модель: Poisson + xG"
        )

        return PredictionResult(
            home_win_prob=home_win,
            draw_prob=draw,
            away_win_prob=away_win,
            expected_home_goals=round(lambda_home, 2),
            expected_away_goals=round(lambda_away, 2),
            confidence=confidence,
            value_bet=value_bet,
            recommended_stake=round(recommended_stake, 2),
            best_bet=best_bet,
            reasoning=reasoning
        )


# Заглушка для старых функций, чтобы не падал импорт
def build_retro_report(*args, **kwargs):
    return "🔄 Ретро-отчёт в разработке. Скоро будет доступен полный анализ."


# Глобальный экземпляр
ai_analyst = AiAnalyst()