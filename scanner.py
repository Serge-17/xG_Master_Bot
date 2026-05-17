"""
scanner.py — пайплайн: матчи → коэффициенты → контекст → модель → сигналы.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from telegram import Bot

from ai import explain_pick
from analysis import best_guess_pick, best_value_pick, poisson_probs, xg_from_odds
from channel import publish_signal
from config import config
from data_sources import Match, Odds, TeamForm, fetch_matches, fetch_odds, fetch_team_form
from db import (
    find_recent_signal,
    get_cached_match,
    get_latest_odds_snapshot,
    get_signal,
    save_odds_snapshot,
    save_signal,
    upsert_cached_match,
)
from web_scrapers import fetch_web_odds_and_context


log = logging.getLogger(__name__)

_scan_lock = asyncio.Lock()


def _match_key(match: Match) -> str:
    kickoff = match.utc_date.strftime("%Y%m%d%H%M") if match.utc_date else "na"
    ext = match.external_id or ""
    return f"{kickoff}:{match.home.lower()}:{match.away.lower()}:{ext}"


def _context_to_text(cached) -> str:
    if not cached:
        return ""
    parts: list[str] = []
    try:
        facts = json.loads(cached.facts or "[]")
        stats = json.loads(cached.stats or "[]")
        injuries = json.loads(cached.injuries or "[]")
    except Exception:
        facts, stats, injuries = [], [], []
    if facts:
        parts.append("Факты: " + "; ".join(facts[:4]))
    if stats:
        parts.append("Статистика: " + "; ".join(stats[:4]))
    if injuries:
        parts.append("Травмы/кадровые новости: " + "; ".join(injuries[:3]))
    if cached.home_summary:
        parts.append(f"{cached.home}: {cached.home_summary}")
    if cached.away_summary:
        parts.append(f"{cached.away}: {cached.away_summary}")
    return "\n".join(parts)


def _form_games(form: Optional[TeamForm]) -> int:
    if not form:
        return 0
    return int(form.wins + form.draws + form.losses)


def _minutes_to_kickoff(match: Match) -> Optional[float]:
    if match.utc_date is None:
        return None
    dt = match.utc_date
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - datetime.now(timezone.utc)).total_seconds() / 60


def _public_signal_reject_reason(
    match: Match,
    pick,
    home_form: Optional[TeamForm],
    away_form: Optional[TeamForm],
) -> str:
    minutes = _minutes_to_kickoff(match)
    if minutes is not None and minutes < config.min_minutes_before_kickoff:
        return f"до старта {minutes:.0f} мин < {config.min_minutes_before_kickoff}"

    if pick.market.upper() not in {m.upper() for m in config.allowed_signal_markets}:
        return f"рынок {pick.market} отключён quality gate"

    if pick.book_odds < config.min_signal_odds or pick.book_odds > config.max_signal_odds:
        return (
            f"коэффициент {pick.book_odds:.2f} вне диапазона "
            f"{config.min_signal_odds:.2f}-{config.max_signal_odds:.2f}"
        )

    min_games = config.min_form_games_for_signal
    home_games = _form_games(home_form)
    away_games = _form_games(away_form)
    if min_games and (home_games < min_games or away_games < min_games):
        return f"недостаточно формы: {home_games}/{away_games} игр < {min_games}"

    return ""


async def _prepare_match_cache(match: Match, include_forms: bool = True) -> tuple[Optional[Odds], object]:
    match_key = _match_key(match)
    cached_match = await get_cached_match(match_key)
    snapshot = await get_latest_odds_snapshot(match_key, max_age_hours=8)

    live_odds = None
    source = "cache"
    source_url = getattr(cached_match, "source_url", "")
    facts: list[str] = []
    stats: list[str] = []
    injuries: list[str] = []

    api_odds = await fetch_odds(match.home, match.away)
    if api_odds and api_odds.has_1x2():
        live_odds = api_odds
        source = "odds-api"
    elif snapshot:
        live_odds = Odds(
            home=snapshot.home,
            draw=snapshot.draw,
            away=snapshot.away,
            over_2_5=snapshot.over_2_5,
            under_2_5=snapshot.under_2_5,
            btts_yes=snapshot.btts_yes,
            btts_no=snapshot.btts_no,
            bookmaker=snapshot.bookmaker,
        )
        source = snapshot.source
        source_url = snapshot.source_url or source_url

    web_odds, web_ctx = await fetch_web_odds_and_context(match.home, match.away)
    if web_ctx.source_url:
        source_url = web_ctx.source_url
        facts = web_ctx.facts
        stats = web_ctx.stats
        injuries = web_ctx.injuries
    if (
        live_odds is None
        and config.allow_web_odds_fallback
        and web_odds
        and web_odds.has_1x2()
    ):
        live_odds = web_odds
        source = "sports.ru"

    if include_forms:
        home_form, away_form = await asyncio.gather(
            fetch_team_form(match.home),
            fetch_team_form(match.away),
        )
    else:
        home_form = None
        away_form = None

    if live_odds:
        await save_odds_snapshot(
            match_key=match_key,
            match=match.title,
            bookmaker=live_odds.bookmaker,
            source=source,
            source_url=source_url,
            home=live_odds.home,
            draw=live_odds.draw,
            away=live_odds.away,
            over_2_5=live_odds.over_2_5,
            under_2_5=live_odds.under_2_5,
            btts_yes=live_odds.btts_yes,
            btts_no=live_odds.btts_no,
        )

    await upsert_cached_match(
        match_key=match_key,
        external_id=match.external_id or "",
        match=match.title,
        home=match.home,
        away=match.away,
        league=match.competition,
        kickoff=match.utc_date.replace(tzinfo=None) if match.utc_date else None,
        source=source,
        source_url=source_url,
        facts=facts,
        stats=stats,
        injuries=injuries,
        home_form=home_form.form if home_form else getattr(cached_match, "home_form", "— — — — —"),
        away_form=away_form.form if away_form else getattr(cached_match, "away_form", "— — — — —"),
        home_summary=home_form.summary() if home_form else getattr(cached_match, "home_summary", ""),
        away_summary=away_form.summary() if away_form else getattr(cached_match, "away_summary", ""),
        raw_payload={
            "bookmaker": live_odds.bookmaker if live_odds else "",
            "odds": {
                "home": live_odds.home if live_odds else 0.0,
                "draw": live_odds.draw if live_odds else 0.0,
                "away": live_odds.away if live_odds else 0.0,
            },
        },
    )

    cached_match = await get_cached_match(match_key)
    return live_odds, cached_match


async def warmup_match_cache(limit: int = 24) -> int:
    matches = await fetch_matches(days_ahead=1)
    count = 0
    for match in matches[:limit]:
        try:
            odds, _ = await _prepare_match_cache(match, include_forms=False)
            if odds and odds.has_1x2():
                count += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            log.warning("cache warmup failed for %s: %s", match.title, e)
    return count


async def _process_match(match: Match, bank: float, strict: bool = True):
    odds, cached = await _prepare_match_cache(match)
    if odds is None or not odds.has_1x2():
        log.info("[no-odds] %s", match.title)
        return None

    home_form, away_form = await asyncio.gather(
        fetch_team_form(match.home),
        fetch_team_form(match.away),
    )
    home_xg, away_xg = xg_from_odds(odds, match.competition,
                                    home_form, away_form)
    model = poisson_probs(home_xg, away_xg)

    pick = best_value_pick(match.home, match.away, odds, model, bank) if strict \
        else best_guess_pick(match.home, match.away, odds, model, bank)

    return (pick, odds, model, cached, home_form, away_form) if pick else None


async def scan_and_build_signals(bank: float, limit: int = 25
                                 ) -> list[tuple[int, Match, dict, object]]:
    matches = await fetch_matches()
    if not matches:
        log.info("Матчи не найдены")
        return []

    results: list[tuple[int, Match, dict, object]] = []
    for match in matches[:limit]:
        if len(results) >= config.max_signals_per_day:
            break

        res = await _process_match(match, bank, strict=True)
        if res is None:
            continue
        pick, odds, model, cached, home_form, away_form = res

        reject_reason = _public_signal_reject_reason(match, pick, home_form, away_form)
        if reject_reason:
            log.info("[quality-skip] %s / %s: %s", match.title, pick.pick, reject_reason)
            continue

        duplicate = await find_recent_signal(match.title, pick.market, pick.pick)
        if duplicate:
            log.info(
                "[duplicate-skip] %s / %s already signal #%d",
                match.title,
                pick.pick,
                duplicate.id,
            )
            continue

        meta = await explain_pick(
            match.home, match.away, match.competition,
            pick.pick, pick.probability, pick.book_odds, pick.fair_odds,
            extra_context=_context_to_text(cached),
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
            market_probability=pick.market_probability,
            confidence=int(round(pick.probability * 100)),
            edge=pick.edge,
            reasoning=meta["reasoning"],
            risks=meta["risks"],
            home_form=getattr(cached, "home_form", meta["home_form"]) or meta["home_form"],
            away_form=getattr(cached, "away_form", meta["away_form"]) or meta["away_form"],
            recommended_stake=pick.recommended_stake,
            sport_key=match.sport_key or "",
        )
        results.append((signal_id, match, model, cached))
        await asyncio.sleep(2)

    return results


async def scan_best_guesses(bank: float, limit: int = 20) -> list[dict]:
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
        pick, odds, model, cached, _, _ = res
        results.append({
            "match": match,
            "pick": pick,
            "model": model,
            "cached": cached,
        })
        await asyncio.sleep(0.5)

    results.sort(key=lambda x: x["pick"].edge, reverse=True)
    return results[:3]


async def scan_and_publish(bot: Bot, bank: float) -> int:
    if _scan_lock.locked():
        log.info("scan уже выполняется")
        return 0

    async with _scan_lock:
        signals = await scan_and_build_signals(bank)
        published = 0
        for signal_id, match, model, cached in signals:
            sig = await get_signal(signal_id)
            if sig and await publish_signal(bot, sig, match,
                                            model=model, cached=cached):
                published += 1
        return published
