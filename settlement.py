"""
settlement.py — расчёт результата опубликованных сигналов.

Логика:
1. Берём pending-сигналы, у которых kickoff ≥ 2ч назад.
2. Тянем финальный счёт (the-odds-api scores → football-data fallback).
3. Парсим Signal.market + Signal.pick → канонический (рынок, сторона).
4. Определяем verdict: win/loss/void и P&L в долях ставки (1u = размер ставки).
5. Идемпотентно обновляем Signal в БД.

Edge cases:
- Кубковые матчи: используем счёт основного времени (FT) — это стандарт линий.
- Перенос/абандон: если счёт не пришёл за 7 дней → void через void_orphan_signals().
- Парсинг pick fail → пропускаем (нужно ручное закрытие через /settle).
- При несовпадении источников можно расширить до needs_review (пока берём любой
  завершённый источник — odds-api первый, football-data только fallback).
"""

from __future__ import annotations

import logging
from typing import Optional

from data_sources import MatchScore, fetch_current_odds_decimal, fetch_match_score
from db import (
    Signal,
    list_orphan_pending_signals,
    list_signals_kicking_soon,
    list_signals_to_settle,
    set_signal_closing_odds,
    update_signal_settlement,
)

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────
# Парсинг pick → канонический ярлык (рынок, сторона)
# ────────────────────────────────────────────────────────────────
def _parse_match_teams(match: str) -> tuple[str, str]:
    parts = match.split(" vs ")
    if len(parts) != 2:
        return ("", "")
    return parts[0].strip(), parts[1].strip()


def parse_pick(market: str, pick: str, match: str) -> Optional[tuple[str, str]]:
    """('1X2','home'|'draw'|'away') | ('TOTAL_2_5','over'|'under') | ('BTTS','yes'|'no')."""
    m = (market or "").upper()
    p = (pick or "").lower().strip()
    if not m or not p:
        return None

    if m == "1X2":
        if "ничь" in p or "draw" in p:
            return ("1X2", "draw")
        home, away = _parse_match_teams(match)
        h = home.lower()
        a = away.lower()
        if "побед" in p or "win" in p or "п1" in p or "п2" in p:
            if "п1" in p:
                return ("1X2", "home")
            if "п2" in p:
                return ("1X2", "away")
            after = p.split("побед", 1)[1] if "побед" in p else p
            if h and h in after:
                return ("1X2", "home")
            if a and a in after:
                return ("1X2", "away")
            if h and h in p:
                return ("1X2", "home")
            if a and a in p:
                return ("1X2", "away")
        return None

    if m in ("TOTAL_2_5", "TOTAL", "ТОТАЛ", "TOTALS"):
        if "больше" in p or "over" in p or "тб " in p or p.startswith("тб"):
            return ("TOTAL_2_5", "over")
        if "меньше" in p or "under" in p or "тм " in p or p.startswith("тм"):
            return ("TOTAL_2_5", "under")
        return None

    if m == "BTTS":
        if "да" in p or "yes" in p:
            return ("BTTS", "yes")
        if "нет" in p or "no" in p:
            return ("BTTS", "no")
        return None

    return None


# ────────────────────────────────────────────────────────────────
# Verdict per market
# ────────────────────────────────────────────────────────────────
def verdict_for(canon: tuple[str, str], home_goals: int, away_goals: int) -> str:
    """Возвращает 'win' / 'loss' / 'void'."""
    market, side = canon
    if market == "1X2":
        if side == "home":
            return "win" if home_goals > away_goals else "loss"
        if side == "away":
            return "win" if away_goals > home_goals else "loss"
        if side == "draw":
            return "win" if home_goals == away_goals else "loss"
    if market == "TOTAL_2_5":
        total = home_goals + away_goals
        if side == "over":
            return "win" if total > 2.5 else "loss"
        if side == "under":
            return "win" if total < 2.5 else "loss"
    if market == "BTTS":
        both = home_goals >= 1 and away_goals >= 1
        if side == "yes":
            return "win" if both else "loss"
        if side == "no":
            return "win" if not both else "loss"
    return "void"


def pnl_units_for(verdict: str, book_odds: float) -> float:
    """P&L в долях ставки (стандарт капперов: 1u = размер ставки)."""
    if verdict == "win":
        return float(book_odds) - 1.0
    if verdict == "loss":
        return -1.0
    return 0.0


# ────────────────────────────────────────────────────────────────
# Settle one signal
# ────────────────────────────────────────────────────────────────
async def settle_signal(signal: Signal,
                        score: Optional[MatchScore] = None) -> Optional[str]:
    """
    Settle одного сигнала. Возвращает verdict ('win'/'loss'/'void') или None
    если settle отложен (счёт не найден, pick не распарсился).
    """
    if signal.status != "pending":
        return None

    canon = parse_pick(signal.market or "", signal.pick or "", signal.match or "")
    if canon is None:
        log.warning("[settle] не парсится pick #%d: %r / %r",
                    signal.id, signal.market, signal.pick)
        return None

    if score is None:
        home, away = _parse_match_teams(signal.match or "")
        score = await fetch_match_score(
            home or "", away or "",
            sport_key=signal.sport_key or "",
            external_id="",
        )

    if score is None or not score.completed:
        return None

    verdict = verdict_for(canon, score.home_goals, score.away_goals)
    pnl = pnl_units_for(verdict, signal.book_odds or 0.0)

    await update_signal_settlement(
        signal.id,
        status=verdict,
        home_goals=score.home_goals,
        away_goals=score.away_goals,
        pnl_units=pnl,
        settle_source=score.source,
    )
    log.info("[settle] #%d %s: %s @ %.2f → %s (%+.2fu)",
             signal.id, signal.match, signal.pick, signal.book_odds or 0,
             verdict, pnl)
    return verdict


# ────────────────────────────────────────────────────────────────
# Bulk settle
# ────────────────────────────────────────────────────────────────
async def settle_pending_signals() -> dict:
    """Проходит по всем готовым к settle сигналам, возвращает счётчики + список settled."""
    pending = await list_signals_to_settle(grace_hours=2, max_age_days=14)
    counts = {"win": 0, "loss": 0, "void": 0, "skipped": 0}
    settled: list[Signal] = []

    for sig in pending:
        try:
            verdict = await settle_signal(sig)
        except Exception as e:
            log.exception("[settle] упал на #%d: %s", sig.id, e)
            verdict = None
        if verdict:
            counts[verdict] = counts.get(verdict, 0) + 1
            settled.append(sig)
        else:
            counts["skipped"] += 1

    log.info("settle pass: pending=%d settled=%d skipped=%d",
             len(pending), len(settled), counts["skipped"])
    return {"counts": counts, "signals": settled, "total": len(pending)}


async def void_orphan_signals(stale_days: int = 7) -> int:
    """Старше N дней без счёта → void. Иначе они вечно висят pending."""
    orphans = await list_orphan_pending_signals(stale_days=stale_days)
    n = 0
    for sig in orphans:
        try:
            await update_signal_settlement(
                sig.id,
                status="void",
                home_goals=None,
                away_goals=None,
                pnl_units=0.0,
                settle_source="orphan",
            )
            n += 1
        except Exception as e:
            log.warning("orphan-void #%d failed: %s", sig.id, e)
    if n:
        log.info("orphan-void: помечено void %d сигналов старше %d дней", n, stale_days)
    return n


# ────────────────────────────────────────────────────────────────
# Manual settlement (admin override)
# ────────────────────────────────────────────────────────────────
async def snapshot_closing_odds() -> int:
    """
    Для всех сигналов со стартом в ближайшие 30 минут и без closing_odds —
    тянем текущий коэф и сохраняем как closing line. Это база для CLV.
    """
    sigs = await list_signals_kicking_soon(window_min=30)
    n = 0
    for sig in sigs:
        canon = parse_pick(sig.market or "", sig.pick or "", sig.match or "")
        if canon is None:
            continue
        market_pick = f"{canon[0]}:{canon[1]}"
        home, away = _parse_match_teams(sig.match or "")
        if not home or not away:
            continue
        try:
            cur = await fetch_current_odds_decimal(home, away, market_pick)
        except Exception as e:
            log.debug("CLV fetch failed for #%d: %s", sig.id, e)
            cur = None
        if cur and cur > 1.01:
            await set_signal_closing_odds(sig.id, cur)
            n += 1
            log.info("[clv] #%d closing %.2f (был %.2f)",
                     sig.id, cur, sig.book_odds or 0)
    return n


async def manual_settle(signal: Signal, verdict: str,
                        home_goals: Optional[int] = None,
                        away_goals: Optional[int] = None) -> bool:
    """Ручной override через /settle. Можно перебить даже уже settled."""
    if verdict not in ("win", "loss", "void"):
        raise ValueError(f"unknown verdict: {verdict}")
    pnl = pnl_units_for(verdict, signal.book_odds or 0.0)
    await update_signal_settlement(
        signal.id,
        status=verdict,
        home_goals=home_goals,
        away_goals=away_goals,
        pnl_units=pnl,
        settle_source="manual",
    )
    log.info("[manual-settle] #%d → %s", signal.id, verdict)
    return True
