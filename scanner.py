"""
scanner.py — пайплайн: матчи → коэффициенты → модель → value-сигналы.

ИСПРАВЛЕНИЯ:
1. limit увеличен с 6 до 25 — сканируем все доступные матчи дня.
2. Добавлено подробное логирование почему матч пропущен.
3. asyncio.Lock — не более одного параллельного скана.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from telegram import Bot

from ai import explain_pick
from analysis import best_value_pick, build_value_picks, poisson_probs, xg_from_odds
from channel import publish_signal
from config import config
from data_sources import Match, Odds, fetch_matches, fetch_odds
from db import Signal, get_bank, save_signal, get_signal


log = logging.getLogger(__name__)

_scan_lock = asyncio.Lock()


async def scan_and_build_signals(bank: float, limit: int = 25) -> list[tuple[int, Match]]:
    matches = await fetch_matches()
    if not matches:
        log.info("Матчи не найдены")
        return []

    log.info("Сканируем %d матчей (лимит %d)", len(matches), limit)
    results: list[tuple[int, Match]] = []

    for match in matches[:limit]:
        if len(results) >= config.max_signals_per_day:
            break

        odds = await fetch_odds(match.home, match.away)
        if odds is None or not odds.has_1x2():
            log.info("[skip] %s — нет коэффициентов 1X2", match.title)
            continue

        home_xg, away_xg = xg_from_odds(odds)
        model = poisson_probs(home_xg, away_xg)

        # Логируем все кандидаты для диагностики
        all_picks = build_value_picks(match.home, match.away, odds, model, bank)
        log.info(
            "[%s] xG=%.2f/%.2f | model=H%.0f%%D%.0f%%A%.0f%% O2.5=%.0f%% | picks=%d",
            match.title, home_xg, away_xg,
            model["home"]*100, model["draw"]*100, model["away"]*100,
            model["over_2_5"]*100, len(all_picks),
        )

        pick = all_picks[0] if all_picks else None
        if pick is None:
            log.info("[skip] %s — нет value-ставок (edge/confidence)", match.title)
            continue

        log.info(
            "[value!] %s → %s @ %.2f | edge=%+.1f%% prob=%.0f%%",
            match.title, pick.pick, pick.book_odds, pick.edge * 100, pick.probability * 100,
        )

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
        log.info("Signal #%d сохранён: %s", signal_id, match.title)
        results.append((signal_id, match))
        await asyncio.sleep(3)

    log.info("Скан завершён: найдено %d value-ставок из %d матчей", len(results), len(matches))
    return results


async def scan_and_publish(bot: Bot, bank: float) -> int:
    if _scan_lock.locked():
        log.info("scan_and_publish: уже выполняется, пропускаем")
        return 0

    async with _scan_lock:
        signals = await scan_and_build_signals(bank)
        published = 0
        for signal_id, match in signals:
            sig = await get_signal(signal_id)
            if sig and await publish_signal(bot, sig, match):
                published += 1
        return published
