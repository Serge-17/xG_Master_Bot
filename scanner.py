"""
scanner.py — пайплайн: матчи → коэффициенты → модель → сигналы.

ЛОГИКА:
1. scan_and_build_signals() — строгий фильтр value, публикует в канал
2. scan_best_guesses()      — fallback: лучшие picks дня без строгого фильтра,
                              показываются пользователю в личке даже без value
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram import Bot

from ai import explain_pick
from analysis import (
    best_guess_pick, best_value_pick, build_value_picks,
    poisson_probs, xg_from_odds,
)
from channel import publish_signal
from config import config
from data_sources import Match, fetch_matches, fetch_odds
from db import get_bank, get_signal, save_signal


log = logging.getLogger(__name__)

_scan_lock = asyncio.Lock()


async def _process_match(match: Match, bank: float, strict: bool = True):
    """Обрабатывает один матч. Возвращает (pick, odds, model) или None."""
    odds = await fetch_odds(match.home, match.away)
    if odds is None or not odds.has_1x2():
        log.info("[no-odds] %s", match.title)
        return None

    home_xg, away_xg = xg_from_odds(odds, match.competition)
    model = poisson_probs(home_xg, away_xg)

    log.info(
        "[%s] xG=%.2f/%.2f | H%.0f%% D%.0f%% A%.0f%% | O2.5=%.0f%% BTTS=%.0f%%",
        match.title, home_xg, away_xg,
        model["home"]*100, model["draw"]*100, model["away"]*100,
        model["over_2_5"]*100, model["btts_yes"]*100,
    )

    pick = best_value_pick(match.home, match.away, odds, model, bank) if strict \
        else best_guess_pick(match.home, match.away, odds, model, bank)

    return (pick, odds, model) if pick else None


async def scan_and_build_signals(bank: float, limit: int = 25) -> list[tuple[int, Match]]:
    """Строгий скан: публикует в канал только настоящий value."""
    matches = await fetch_matches()
    if not matches:
        log.info("Матчи не найдены")
        return []

    log.info("Строгий скан: %d матчей", len(matches))
    results: list[tuple[int, Match]] = []

    for match in matches[:limit]:
        if len(results) >= config.max_signals_per_day:
            break

        res = await _process_match(match, bank, strict=True)
        if res is None:
            continue
        pick, odds, model = res

        log.info("[VALUE] %s → %s @ %.2f edge=%+.1f%%",
                 match.title, pick.pick, pick.book_odds, pick.edge * 100)

        meta = await explain_pick(
            match.home, match.away, match.competition,
            pick.pick, pick.probability, pick.book_odds, pick.fair_odds,
        )

        signal_id = await save_signal(
            match=match.title,
            league=match.competition,
            kickoff=match.utc_date.replace(tzinfo=None) if match.utc_date else None,
            market=pick.market,
            pick=pick.pick,
            book_odds=pick.book_odds,
            fair_odds=pick.fair_odds,
            probability=pick.probability,
            confidence=int(round(pick.probability * 100)),
            edge=pick.edge,
            reasoning=meta["reasoning"],
            risks=meta["risks"],
            home_form=meta["home_form"],
            away_form=meta["away_form"],
            recommended_stake=pick.recommended_stake,
        )
        results.append((signal_id, match))
        await asyncio.sleep(2)

    log.info("Строгий скан завершён: %d value-ставок из %d матчей",
             len(results), len(matches))
    return results


async def scan_best_guesses(bank: float, limit: int = 20) -> list[dict]:
    """
    Мягкий скан: возвращает лучшие picks даже без строгого value edge.
    Используется для ответа пользователю когда строгих сигналов нет.
    """
    matches = await fetch_matches()
    if not matches:
        return []

    results = []
    for match in matches[:limit]:
        if len(results) >= 5:
            break
        res = await _process_match(match, bank, strict=False)
        if res is None:
            continue
        pick, odds, model = res
        results.append({
            "match": match,
            "pick": pick,
            "model": model,
        })
        await asyncio.sleep(0.5)

    # Сортируем по edge убыванию
    results.sort(key=lambda x: x["pick"].edge, reverse=True)
    return results[:3]


async def scan_and_publish(bot: Bot, bank: float) -> int:
    if _scan_lock.locked():
        log.info("scan уже выполняется")
        return 0

    async with _scan_lock:
        signals = await scan_and_build_signals(bank)
        published = 0
        for signal_id, match in signals:
            sig = await get_signal(signal_id)
            if sig and await publish_signal(bot, sig, match):
                published += 1
        return published
