"""
scanner.py — основной пайплайн: матчи → коэффициенты → модель → value-сигналы.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from telegram import Bot

from ai import explain_pick
from analysis import best_value_pick, poisson_probs, xg_from_odds
from channel import publish_signal
from config import config
from data_sources import Match, Odds, fetch_matches, fetch_odds
from db import Signal, get_bank, save_signal


log = logging.getLogger(__name__)

# FIX: Семафор — не больше одного параллельного scan_and_publish.
# Если пользователь нажимает /scan пока уже идёт скан (например, дневной),
# второй вызов ждёт завершения первого вместо дублирования запросов к API.
_scan_lock = asyncio.Lock()


async def scan_and_build_signals(bank: float, limit: int = 6) -> list[tuple[int, Match]]:
    """Сканирует сегодняшние матчи и возвращает список (signal_id, Match)."""

    # FIX: fetch_matches теперь кэшируется в data_sources.py —
    # повторные вызовы в течение TTL не делают HTTP-запросов.
    matches = await fetch_matches()
    if not matches:
        log.info("Матчи не найдены")
        return []

    results: list[tuple[int, Match]] = []

    for match in matches[:limit]:
        if len(results) >= config.max_signals_per_day:
            break

        odds = await fetch_odds(match.home, match.away)
        if odds is None or not odds.has_1x2():
            log.info("Нет коэффициентов для %s", match.title)
            continue

        home_xg, away_xg = xg_from_odds(odds)
        model = poisson_probs(home_xg, away_xg)
        pick = best_value_pick(match.home, match.away, odds, model, bank)
        if pick is None:
            continue

        confidence = int(round(pick.probability * 100))
        if confidence < config.min_confidence:
            log.info("Пропуск %s: confidence %d%% < %d%%",
                     match.title, confidence, config.min_confidence)
            continue

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
            confidence=confidence,
            edge=pick.edge,
            reasoning=meta["reasoning"],
            risks=meta["risks"],
            home_form=meta["home_form"],
            away_form=meta["away_form"],
            recommended_stake=pick.recommended_stake,
        )
        log.info("Signal #%d: %s — %s @ %.2f (edge %+.1f%%)",
                 signal_id, match.title, pick.pick, pick.book_odds, pick.edge * 100)

        results.append((signal_id, match))
        await asyncio.sleep(3)  # пауза между вызовами Gemini

    return results


async def scan_and_publish(bot: Bot, bank: float) -> int:
    """Полный цикл: скан + публикация в канал.

    FIX: обёрнут в _scan_lock — гарантирует что одновременно выполняется
    только один скан. Второй /scan не запускает дублирующие API-запросы,
    а ждёт завершения текущего.
    """
    if _scan_lock.locked():
        log.info("scan_and_publish: уже выполняется, пропускаем")
        return 0

    async with _scan_lock:
        from db import get_signal
        signals = await scan_and_build_signals(bank)
        published = 0
        for signal_id, match in signals:
            sig = await get_signal(signal_id)
            if sig and await publish_signal(bot, sig, match):
                published += 1
        return published
