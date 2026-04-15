from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Dict, Tuple

import requests

from config import settings
from modules.data_sources import TeamContext, FixtureRow


@dataclass
class PredictionResult:
    home_win_prob: float
    draw_prob: float
    away_win_prob: float
    predicted_home_goals: float
    predicted_away_goals: float
    confidence: float          # 0-100
    value: float               # Value bet (наша вероятность vs odds)
    recommended_stake: float
    reasoning: str
    xg_chart: bytes | None = None   # изображение графика


class AiAnalyst:
    def __init__(self):
        self.league_attack_strength: Dict[str, float] = {}
        self.league_defense_strength: Dict[str, float] = {}

    def _get_team_strength(self, league: str, team: str, is_home: bool) -> Tuple[float, float]:
        """Возвращает attack и defense strength команды (на основе xG)"""
        # Пока заглушка — в будущем можно кэшировать из API
        base_attack = 1.35 if is_home else 1.15
        base_defense = 0.95 if is_home else 1.05
        return base_attack, base_defense

    def _poisson_prob(self, lam: float, k: int) -> float:
        """Вероятность забить ровно k голов (Poisson)"""
        return (lam ** k) * np.exp(-lam) / np.math.factorial(k)

    def _dixon_coles_correction(self, home_goals: int, away_goals: int) -> float:
        """Корректировка Dixon-Coles для низовых матчей (0-0, 1-0, 0-1, 1-1)"""
        if home_goals == 0 and away_goals == 0:
            return 1.3
        if home_goals == 1 and away_goals == 0:
            return 0.85
        if home_goals == 0 and away_goals == 1:
            return 0.85
        if home_goals == 1 and away_goals == 1:
            return 0.9
        return 1.0

    def generate_prediction(self, context: TeamContext, bankroll: float) -> PredictionResult:
        """Основная научная модель"""
        # 1. Получаем силу команд
        home_attack, home_defense = self._get_team_strength(context.league, context.home_team, True)
        away_attack, away_defense = self._get_team_strength(context.league, context.away_team, False)

        # 2. Рассчитываем ожидаемые голы (xG)
        lambda_home = home_attack * away_defense * 1.35   # home advantage
        lambda_away = away_attack * home_defense * 0.85

        # 3. Симулируем вероятности (Poisson + Dixon-Coles)
        max_goals = 8
        home_win, draw, away_win = 0.0, 0.0, 0.0

        for h in range(max_goals):
            for a in range(max_goals):
                p = (self._poisson_prob(lambda_home, h) *
                     self._poisson_prob(lambda_away, a) *
                     self._dixon_coles_correction(h, a))

                if h > a:
                    home_win += p
                elif h == a:
                    draw += p
                else:
                    away_win += p

        # 4. Нормализация
        total = home_win + draw + away_win
        home_win /= total
        draw /= total
        away_win /= total

        # 5. Confidence и Value (если есть odds)
        confidence = max(home_win, draw, away_win) * 100
        best_prob = max(home_win, draw, away_win)
        best_outcome = "1" if best_prob == home_win else "X" if best_prob == draw else "2"

        value = 0.0
        if context.odds and best_outcome in context.odds:
            decimal_odds = context.odds[best_outcome]
            implied_prob = 1 / decimal_odds
            value = (best_prob - implied_prob) / implied_prob * 100

        # 6. Рекомендуемая ставка (Partial Kelly)
        from modules.bankroll_manager import recommended_stake
        stake = recommended_stake(
            bankroll=bankroll,
            confidence=confidence,
            odds=context.odds.get(best_outcome) if context.odds else None,
            strategy="kelly",
            flat_percent=0.03,
            kelly_cap=0.25
        )

        # 7. График xG
        chart_bytes = self._generate_xg_chart(lambda_home, lambda_away, context)

        reasoning = (
            f"Ожидаемые голы: {lambda_home:.2f} – {lambda_away:.2f}\n"
            f"Вероятности: П1 {home_win:.1%} | X {draw:.1%} | П2 {away_win:.1%}\n"
            f"Value: {value:+.1f}%"
        )

        return PredictionResult(
            home_win_prob=home_win,
            draw_prob=draw,
            away_win_prob=away_win,
            predicted_home_goals=lambda_home,
            predicted_away_goals=lambda_away,
            confidence=round(confidence),
            value=round(value, 1),
            recommended_stake=round(stake, 2),
            reasoning=reasoning,
            xg_chart=chart_bytes
        )

    def _generate_xg_chart(self, home_xg: float, away_xg: float, context: TeamContext) -> bytes:
        """Генерирует красивый график xG"""
        fig, ax = plt.subplots(figsize=(8, 5))
        teams = [context.home_team, context.away_team]
        xg_values = [home_xg, away_xg]

        bars = ax.bar(teams, xg_values, color=['#1f77b4', '#ff7f0e'])
        ax.set_ylabel('Expected Goals (xG)')
        ax.set_title(f'{context.league}\nОжидаемые голы')
        ax.grid(axis='y', alpha=0.3)

        for bar in bars:
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2., height + 0.1,
                    f'{height:.2f}', ha='center', va='bottom', fontsize=12, fontweight='bold')

        buf = BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format='png', dpi=200)
        plt.close(fig)
        buf.seek(0)
        return buf.read()


# Глобальный экземпляр
ai_analyst = AiAnalyst()