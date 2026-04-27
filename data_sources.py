"""
data_sources.py — клиенты внешних API.
football-data.org  — расписание матчей (free tier: 10 req/min)
the-odds-api.com   — коэффициенты букмекеров
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Optional

import aiohttp

from config import config

log = logging.getLogger(__name__)

_FOOTBALL_DELAY_SEC = 7.0

# ── Кэш матчей ──────────────────────────────────────────────────
_matches_cache: list["Match"] = []
_matches_cache_ts: float = 0.0

# ── Кэш коэффициентов ───────────────────────────────────────────
# Структура: {sport_key: (timestamp, [events])}
_odds_cache: dict[str, tuple[float, list]] = {}
_ODDS_CACHE_TTL = 1800  # 30 минут


@dataclass
class Match:
    home: str
    away: str
    competition: str
    utc_date: Optional[datetime] = None
    external_id: Optional[str] = None

    @property
    def title(self) -> str:
        return f"{self.home} vs {self.away}"


@dataclass
class Odds:
    home: float = 0.0
    draw: float = 0.0
    away: float = 0.0
    over_2_5: float = 0.0
    under_2_5: float = 0.0
    btts_yes: float = 0.0
    btts_no: float = 0.0
    bookmaker: str = ""

    def has_1x2(self) -> bool:
        return self.home > 0 and self.draw > 0 and self.away > 0


# ────────────────────────────────────────────────────────────────
# football-data.org — матчи дня
# ────────────────────────────────────────────────────────────────
async def fetch_matches(days_ahead: int = 1) -> list[Match]:
    global _matches_cache, _matches_cache_ts

    if not config.football_api_key:
        log.warning("FOOTBALL_API_KEY не задан — демо-матчи")
        return [
            Match("Real Madrid", "Barcelona", "La Liga",
                  utc_date=datetime.now(timezone.utc) + timedelta(hours=3)),
            Match("Manchester City", "Arsenal", "Premier League",
                  utc_date=datetime.now(timezone.utc) + timedelta(hours=6)),
            Match("Bayern München", "Borussia Dortmund", "Bundesliga",
                  utc_date=datetime.now(timezone.utc) + timedelta(hours=9)),
        ]

    now = time.monotonic()
    if _matches_cache and (now - _matches_cache_ts) < config.matches_cache_ttl:
        log.info("fetch_matches: из кэша (%d матчей)", len(_matches_cache))
        return _matches_cache

    date_from = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_to = (datetime.now(timezone.utc) + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    headers = {"X-Auth-Token": config.football_api_key}

    matches: list[Match] = []
    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession(timeout=timeout) as http:
        for idx, comp in enumerate(config.football_competitions):
            if idx > 0:
                await asyncio.sleep(_FOOTBALL_DELAY_SEC)

            url = f"{config.football_base}/competitions/{comp}/matches"
            params = {"dateFrom": date_from, "dateTo": date_to, "status": "SCHEDULED,TIMED"}
            try:
                async with http.get(url, headers=headers, params=params) as r:
                    if r.status == 429:
                        log.warning("football-data rate limit на %s, жду 60 сек...", comp)
                        await asyncio.sleep(60)
                        continue
                    if r.status == 403:
                        log.debug("football-data [%s] 403 — нет доступа в плане", comp)
                        continue
                    if r.status != 200:
                        log.warning("football-data [%s] %s", comp, r.status)
                        continue
                    payload = await r.json()
                    comp_name = payload.get("competition", {}).get("name", comp)
                    for m in payload.get("matches", []):
                        utc_date = None
                        if m.get("utcDate"):
                            try:
                                utc_date = datetime.fromisoformat(
                                    m["utcDate"].replace("Z", "+00:00")
                                )
                            except Exception:
                                pass
                        matches.append(Match(
                            home=m["homeTeam"]["name"],
                            away=m["awayTeam"]["name"],
                            competition=comp_name,
                            utc_date=utc_date,
                            external_id=str(m.get("id", "")),
                        ))
            except Exception as e:
                log.error("football-data [%s] error: %s", comp, e)

    log.info("Найдено матчей: %d", len(matches))
    if matches:
        _matches_cache = matches
        _matches_cache_ts = time.monotonic()
    return matches


async def fetch_team_recent_form(team_name: str, limit: int = 5) -> str:
    return "— — — — —"


# ────────────────────────────────────────────────────────────────
# Нормализация названий команд
# ────────────────────────────────────────────────────────────────
def _normalize_team(name: str) -> str:
    """Убираем суффиксы FC/SC/AC и т.д., приводим к нижнему регистру."""
    name = name.lower().strip()
    # Убираем общие суффиксы
    name = re.sub(
        r"\b(fc|cf|sc|ac|as|rc|afc|bfc|cfc|sv|bk|bv|fk|sk|nk|rcd|ss|gil)\b",
        "", name
    )
    # Убираем скобки и лишние пробелы
    name = re.sub(r"\([^)]*\)", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _teams_match(a: str, b: str, threshold: float = 0.65) -> bool:
    """Сравниваем команды с нормализацией."""
    x, y = _normalize_team(a), _normalize_team(b)
    if not x or not y:
        return False
    # Точное вхождение после нормализации
    if x in y or y in x:
        return True
    # Fuzzy match
    ratio = SequenceMatcher(None, x, y).ratio()
    if ratio >= threshold:
        return True
    # Проверяем первые слова (Manchester United → manchester united)
    wx = set(x.split())
    wy = set(y.split())
    common = wx & wy
    if len(common) >= 2:
        return True
    if len(wx) == 1 and len(wy) == 1:
        return ratio >= 0.8
    return False


def _best_odds_from_event(event: dict, home_team: str) -> Odds:
    o = Odds()
    for bk in event.get("bookmakers", []):
        for mk in bk.get("markets", []):
            key = mk.get("key")
            outcomes = mk.get("outcomes", [])
            if key == "h2h":
                for out in outcomes:
                    price = out.get("price", 0) or 0
                    name = out.get("name", "")
                    if _teams_match(name, home_team) and price > o.home:
                        o.home = price
                        o.bookmaker = bk.get("title", o.bookmaker)
                    elif name == "Draw" and price > o.draw:
                        o.draw = price
                    elif not _teams_match(name, home_team) and name != "Draw" and price > o.away:
                        o.away = price
            elif key == "totals":
                for out in outcomes:
                    point = out.get("point")
                    price = out.get("price", 0) or 0
                    name = out.get("name", "")
                    if point == 2.5:
                        if name == "Over" and price > o.over_2_5:
                            o.over_2_5 = price
                        elif name == "Under" and price > o.under_2_5:
                            o.under_2_5 = price
            elif key == "btts":
                for out in outcomes:
                    price = out.get("price", 0) or 0
                    name = out.get("name", "").lower()
                    if name == "yes" and price > o.btts_yes:
                        o.btts_yes = price
                    elif name == "no" and price > o.btts_no:
                        o.btts_no = price
    return o


# ────────────────────────────────────────────────────────────────
# the-odds-api.com — коэффициенты с кэшем
# ────────────────────────────────────────────────────────────────
async def _fetch_sport_odds(http: aiohttp.ClientSession, sport: str) -> list:
    """Загружает все события по одному виду спорта. Использует кэш."""
    global _odds_cache
    now = time.monotonic()

    # Возвращаем из кэша если свежий
    if sport in _odds_cache:
        ts, events = _odds_cache[sport]
        if now - ts < _ODDS_CACHE_TTL:
            return events

    url = f"{config.odds_base}/sports/{sport}/odds/"
    params = {
        "apiKey": config.odds_api_key,
        "regions": "eu",
        "markets": "h2h,totals,btts",
        "oddsFormat": "decimal",
    }
    try:
        async with http.get(url, params=params) as r:
            if r.status == 429:
                log.warning("odds-api rate limit на %s — пауза 30 сек", sport)
                await asyncio.sleep(30)
                return _odds_cache.get(sport, (0, []))[1]  # вернуть старый кэш
            if r.status == 401:
                log.error("odds-api: неверный ключ (401)")
                return []
            if r.status != 200:
                log.warning("odds-api [%s] status %s", sport, r.status)
                return []
            events = await r.json()
            _odds_cache[sport] = (now, events)
            log.info("odds-api [%s]: загружено %d событий", sport, len(events))
            return events
    except Exception as e:
        log.error("odds-api [%s] error: %s", sport, e)
        return []


async def fetch_odds(home: str, away: str) -> Optional[Odds]:
    if not config.odds_api_key:
        return Odds(home=2.10, draw=3.30, away=3.50,
                    over_2_5=1.85, under_2_5=1.95,
                    btts_yes=1.80, btts_no=1.95, bookmaker="demo")

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        for i, sport in enumerate(config.odds_sports):
            # Задержка только если кэш не свежий
            now = time.monotonic()
            cached = _odds_cache.get(sport)
            if cached and (now - cached[0]) < _ODDS_CACHE_TTL:
                events = cached[1]
            else:
                if i > 0:
                    await asyncio.sleep(1.0)  # вежливая пауза между запросами
                events = await _fetch_sport_odds(http, sport)

            for ev in events:
                ev_home = ev.get("home_team", "")
                ev_away = ev.get("away_team", "")
                if _teams_match(home, ev_home) and _teams_match(away, ev_away):
                    odds = _best_odds_from_event(ev, ev_home)
                    if odds.has_1x2():
                        log.info(
                            "Odds найдены [%s]: %s vs %s → H%.2f D%.2f A%.2f",
                            sport, ev_home, ev_away, odds.home, odds.draw, odds.away
                        )
                        return odds

    log.info("Odds не найдены для: %s vs %s", home, away)
    return None


# ────────────────────────────────────────────────────────────────
# Принудительная загрузка всех odds в кэш (вызывать при старте)
# ────────────────────────────────────────────────────────────────
async def warm_odds_cache():
    """Загружает коэффициенты по всем лигам в кэш с паузами."""
    if not config.odds_api_key:
        return
    log.info("Прогрев кэша коэффициентов...")
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        for i, sport in enumerate(config.odds_sports):
            if i > 0:
                await asyncio.sleep(2.0)
            await _fetch_sport_odds(http, sport)
    log.info("Кэш коэффициентов готов")
