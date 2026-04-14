from __future__ import annotations

from dataclasses import dataclass

from ..config import settings


@dataclass(slots=True)
class StakePlan:
    strategy: str
    stake: float
    fraction: float


def kelly_fraction(probability: float, odds: float) -> float:
    probability = max(0.0, min(probability, 1.0))
    if odds <= 1.0:
        return 0.0
    b = odds - 1.0
    q = 1.0 - probability
    fraction = (b * probability - q) / b
    return max(0.0, fraction)


def confidence_to_probability(confidence: float) -> float:
    confidence = max(1.0, min(confidence, 5.0))
    return round(0.5 + ((confidence - 3.0) * 0.1), 3)


def recommended_stake(
    bankroll: float,
    confidence: float,
    odds: float | None = None,
    strategy: str = "flat",
    flat_percent: float | None = None,
    kelly_cap: float | None = None,
) -> float:
    bankroll = max(0.0, bankroll)
    if strategy == "kelly" and odds is not None:
        probability = confidence_to_probability(confidence)
        fraction = kelly_fraction(probability, odds)
        cap = kelly_cap if kelly_cap is not None else settings.default_bet_percent * 8
        fraction = min(fraction, cap)
        return round(bankroll * fraction, 2)

    percent = flat_percent if flat_percent is not None else settings.default_bet_percent
    return round(bankroll * percent, 2)


def apply_result(bankroll: float, outcome: str, stake: float, odds: float | None = None) -> float:
    outcome = outcome.lower()
    if outcome == "win":
        if odds is None:
            return round(bankroll + stake, 2)
        return round(bankroll + stake * max(odds - 1.0, 0.0), 2)
    if outcome == "refund":
        return round(bankroll + stake, 2)
    if outcome == "loss":
        return round(max(bankroll - stake, 0.0), 2)
    return round(bankroll, 2)
