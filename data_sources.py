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
import unicodedata
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
# TTL динамический: для матчей в течение 90 минут — короткий, иначе длинный.
_odds_cache: dict[str, tuple[float, list]] = {}
_ODDS_CACHE_TTL = 1800        # 30 минут (default — для матчей завтра/позже)
_ODDS_CACHE_TTL_NEAR = 300    # 5 минут (для матчей в ближайшие 90 минут — линия движется)


def _cache_ttl_for_sport(events: list) -> int:
    """Если в выборке есть матч в ближайшие 90 минут — режем TTL."""
    now_utc = datetime.now(timezone.utc)
    near_horizon = now_utc + timedelta(minutes=90)
    for ev in events or []:
        commence = ev.get("commence_time")
        if not commence:
            continue
        try:
            kt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        except Exception:
            continue
        if now_utc - timedelta(minutes=15) <= kt <= near_horizon:
            return _ODDS_CACHE_TTL_NEAR
    return _ODDS_CACHE_TTL

# ── Кэш формы команд и завершённых матчей ───────────────────────
_team_form_cache: dict[str, tuple[float, "TeamForm"]] = {}
_TEAM_FORM_TTL = 6 * 3600
_finished_matches_cache: dict[str, tuple[float, list[dict]]] = {}
_FINISHED_MATCHES_TTL = 6 * 3600


@dataclass
class Match:
    home: str
    away: str
    competition: str
    utc_date: Optional[datetime] = None
    external_id: Optional[str] = None
    sport_key: str = ""           # odds-api sport key — нужен для скоринга

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


@dataclass
class TeamForm:
    form: str = "— — — — —"
    goals_for: int = 0
    goals_against: int = 0
    wins: int = 0
    draws: int = 0
    losses: int = 0

    def summary(self) -> str:
        games = self.wins + self.draws + self.losses
        if games <= 0:
            return "Статистика формы недоступна."
        return (
            f"Последние {games} игр: {self.wins}-{self.draws}-{self.losses}, "
            f"мячи {self.goals_for}:{self.goals_against}."
        )


# ────────────────────────────────────────────────────────────────
# football-data.org — матчи дня
# ────────────────────────────────────────────────────────────────
async def fetch_matches(days_ahead: int = 1) -> list[Match]:
    global _matches_cache, _matches_cache_ts

    now = time.monotonic()
    if _matches_cache and (now - _matches_cache_ts) < config.matches_cache_ttl:
        log.info("fetch_matches: из кэша (%d матчей)", len(_matches_cache))
        return _matches_cache

    odds_matches = await _fetch_matches_from_odds_cache(days_ahead=days_ahead)
    if odds_matches:
        _matches_cache = odds_matches
        _matches_cache_ts = time.monotonic()
        log.info("Найдено матчей через odds-api: %d", len(odds_matches))
        return odds_matches

    try:
        from web_scrapers import scrape_sports_ru_matches

        scraped = await scrape_sports_ru_matches()
        if scraped:
            matches = [
                Match(
                    home=item["home"],
                    away=item["away"],
                    competition=item["competition"],
                    utc_date=item.get("utc_date"),
                    external_id="",
                )
                for item in scraped
            ]
            _matches_cache = matches
            _matches_cache_ts = time.monotonic()
            log.info("Найдено матчей через sports.ru: %d", len(matches))
            return matches
    except Exception as e:
        log.warning("sports.ru matches fallback failed: %s", e)

    if not config.football_api_key:
        if not config.allow_demo_data:
            log.warning(
                "FOOTBALL_API_KEY не задан и sports.ru не отдал матчи — "
                "demo-матчи отключены"
            )
            return []
        log.warning("FOOTBALL_API_KEY не задан и sports.ru не отдал матчи — demo-матчи")
        return [
            Match("Real Madrid", "Barcelona", "La Liga",
                  utc_date=datetime.now(timezone.utc) + timedelta(hours=3)),
            Match("Manchester City", "Arsenal", "Premier League",
                  utc_date=datetime.now(timezone.utc) + timedelta(hours=6)),
            Match("Bayern München", "Borussia Dortmund", "Bundesliga",
                  utc_date=datetime.now(timezone.utc) + timedelta(hours=9)),
        ]

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


async def _fetch_matches_from_odds_cache(days_ahead: int = 1) -> list[Match]:
    if not config.odds_api_key:
        return []

    timeout = aiohttp.ClientTimeout(total=20)
    now_utc = datetime.now(timezone.utc)
    limit_dt = now_utc + timedelta(days=days_ahead)
    results: list[Match] = []
    seen: set[str] = set()

    async with aiohttp.ClientSession(timeout=timeout) as http:
        for i, sport in enumerate(config.odds_sports):
            cached = _odds_cache.get(sport)
            ttl = _cache_ttl_for_sport(cached[1]) if cached else _ODDS_CACHE_TTL
            if cached and (time.monotonic() - cached[0]) < ttl:
                events = cached[1]
            else:
                if i > 0:
                    await asyncio.sleep(1.0)
                events = await _fetch_sport_odds(http, sport)

            for ev in events:
                home = ev.get("home_team", "")
                away = ev.get("away_team", "")
                if not home or not away:
                    continue
                commence_raw = ev.get("commence_time")
                kickoff = None
                if commence_raw:
                    try:
                        kickoff = datetime.fromisoformat(commence_raw.replace("Z", "+00:00"))
                    except Exception:
                        kickoff = None
                if kickoff and not (now_utc - timedelta(hours=4) <= kickoff <= limit_dt):
                    continue
                match_key = ev.get("id") or f"{sport}:{home}:{away}:{commence_raw}"
                if match_key in seen:
                    continue
                seen.add(match_key)
                results.append(Match(
                    home=home,
                    away=away,
                    competition=ev.get("sport_title") or sport,
                    utc_date=kickoff,
                    external_id=str(ev.get("id", "")),
                    sport_key=sport,
                ))

    results.sort(key=lambda m: m.utc_date or now_utc)
    return results


async def fetch_team_form(team_name: str, limit: int = 5) -> TeamForm:
    if not config.football_api_key:
        return TeamForm()

    cache_key = _normalize_team(team_name)
    now = time.monotonic()
    cached_form = _team_form_cache.get(cache_key)
    if cached_form and (now - cached_form[0]) < _TEAM_FORM_TTL:
        return cached_form[1]

    headers = {"X-Auth-Token": config.football_api_key}
    timeout = aiohttp.ClientTimeout(total=15)
    all_rows: list[dict] = []

    async with aiohttp.ClientSession(timeout=timeout) as http:
        for comp in config.football_competitions:
            payload_matches = await _fetch_finished_matches(http, headers, comp)
            if not payload_matches:
                continue

            for match in payload_matches:
                home = match.get("homeTeam", {}).get("name", "")
                away = match.get("awayTeam", {}).get("name", "")
                if _teams_match(team_name, home) or _teams_match(team_name, away):
                    all_rows.append(match)

    all_rows.sort(key=lambda m: m.get("utcDate", ""), reverse=True)
    all_rows = all_rows[:limit]
    if not all_rows:
        form_obj = TeamForm()
        _team_form_cache[cache_key] = (time.monotonic(), form_obj)
        return form_obj

    form: list[str] = []
    gf = ga = wins = draws = losses = 0
    for match in all_rows:
        home = match.get("homeTeam", {}).get("name", "")
        score = match.get("score", {}).get("fullTime", {})
        home_goals = int(score.get("home") or 0)
        away_goals = int(score.get("away") or 0)
        is_home = _teams_match(team_name, home)

        if is_home:
            gf += home_goals
            ga += away_goals
            if home_goals > away_goals:
                wins += 1
                form.append("W")
            elif home_goals == away_goals:
                draws += 1
                form.append("D")
            else:
                losses += 1
                form.append("L")
        else:
            gf += away_goals
            ga += home_goals
            if away_goals > home_goals:
                wins += 1
                form.append("W")
            elif away_goals == home_goals:
                draws += 1
                form.append("D")
            else:
                losses += 1
                form.append("L")

    form_obj = TeamForm(
        form=" ".join(form) if form else "— — — — —",
        goals_for=gf,
        goals_against=ga,
        wins=wins,
        draws=draws,
        losses=losses,
    )
    _team_form_cache[cache_key] = (time.monotonic(), form_obj)
    return form_obj


async def fetch_team_recent_form(team_name: str, limit: int = 5) -> str:
    return (await fetch_team_form(team_name, limit=limit)).form


# ────────────────────────────────────────────────────────────────
# Нормализация названий команд
# ────────────────────────────────────────────────────────────────
def _normalize_team(name: str) -> str:
    """Убираем суффиксы FC/SC/AC и т.д., унакцент, приводим к нижнему регистру.

    Unicode-normalize нужен чтобы 'Bayern München' и 'Bayern Munich' давали
    одинаковую базу 'bayern munchen'.
    """
    name = name.lower().strip()
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    name = re.sub(
        r"\b(fc|cf|sc|ac|as|rc|afc|bfc|cfc|sv|bk|bv|fk|sk|nk|rcd|ss|gil)\b",
        "", name,
    )
    name = re.sub(r"\([^)]*\)", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


# Префиксы городов / общие слова, которые сами по себе НЕ идентифицируют клуб
# (Real Madrid vs Real Sociedad, Manchester United vs Manchester City и т.п.)
_TEAM_STOPWORDS = {
    "de", "del", "la", "el", "los", "las",
    "club", "team", "real", "athletic", "atletico", "deportivo",
    "manchester", "madrid", "milan", "borussia", "bayern",
}


def _teams_match(a: str, b: str, threshold: float = 0.72) -> bool:
    """Строгое сравнение команд.

    1. Точное совпадение нормализованных строк.
    2. Подстрока (для длинных названий).
    3. SequenceMatcher ratio:
       - ≥ 0.88 — абсолютное доверие (Bayern Munchen/Munich).
       - 0.72-0.88 — borderline: верифицируем токенами. Если
         дифференцирующие токены явно разные (united vs city) → False.
    4. Token check: общие токены при отсутствии конфликтов.
    """
    x, y = _normalize_team(a), _normalize_team(b)
    if not x or not y:
        return False
    if x == y:
        return True
    if min(len(x), len(y)) >= 6 and (x in y or y in x):
        return True

    ratio = SequenceMatcher(None, x, y).ratio()
    wx = {w for w in x.split() if len(w) > 3 and w not in _TEAM_STOPWORDS}
    wy = {w for w in y.split() if len(w) > 3 and w not in _TEAM_STOPWORDS}

    if ratio >= 0.88:
        return True

    if ratio >= threshold:
        # Borderline: токены должны хотя бы пересекаться или быть похожими
        if not wx or not wy:
            return True  # одна сторона состоит только из стоп-слов — доверяем ratio
        if wx & wy or wx.issubset(wy) or wy.issubset(wx):
            return True
        for tw in wx:
            for ty in wy:
                if SequenceMatcher(None, tw, ty).ratio() >= 0.75:
                    return True
        return False  # дифференцирующие токены явно разные

    if not wx or not wy:
        return False
    common = wx & wy
    if len(common) >= 2:
        return True
    if common and (wx.issubset(wy) or wy.issubset(wx)):
        return True
    return False


def _median(values: list[float]) -> float:
    vals = sorted(v for v in values if v and v > 1.0)
    if not vals:
        return 0.0
    n = len(vals)
    return vals[n // 2] if n % 2 else round((vals[n // 2 - 1] + vals[n // 2]) / 2, 3)


def _best_odds_from_event(event: dict, home_team: str) -> Odds:
    """Берёт МЕДИАНУ по букмекерам, а не максимум.

    Старая логика «максимум по каждой стороне» создавала синтетическую
    линию с минусовой маржой → fair_probs искусственно завышены → fake edge.
    Медиана даёт стабильную оценку реальной справедливой цены.
    """
    h2h_home: list[float] = []
    h2h_draw: list[float] = []
    h2h_away: list[float] = []
    over: list[float] = []
    under: list[float] = []
    btts_y: list[float] = []
    btts_n: list[float] = []
    bookmakers: list[str] = []

    for bk in event.get("bookmakers", []):
        title = bk.get("title", "")
        if title:
            bookmakers.append(title)
        for mk in bk.get("markets", []):
            key = mk.get("key")
            for out in mk.get("outcomes", []):
                price = out.get("price", 0) or 0
                if not price or price <= 1:
                    continue
                name = out.get("name", "")
                if key == "h2h":
                    if _teams_match(name, home_team):
                        h2h_home.append(price)
                    elif name == "Draw":
                        h2h_draw.append(price)
                    elif name and name != "Draw":
                        h2h_away.append(price)
                elif key == "totals" and out.get("point") == 2.5:
                    if name == "Over":
                        over.append(price)
                    elif name == "Under":
                        under.append(price)
                elif key == "btts":
                    if name.lower() == "yes":
                        btts_y.append(price)
                    elif name.lower() == "no":
                        btts_n.append(price)

    return Odds(
        home=_median(h2h_home),
        draw=_median(h2h_draw),
        away=_median(h2h_away),
        over_2_5=_median(over),
        under_2_5=_median(under),
        btts_yes=_median(btts_y),
        btts_no=_median(btts_n),
        bookmaker=", ".join(bookmakers[:3]) if bookmakers else "",
    )


# ────────────────────────────────────────────────────────────────
# the-odds-api.com — коэффициенты с кэшем
# ────────────────────────────────────────────────────────────────
async def _fetch_sport_odds(http: aiohttp.ClientSession, sport: str) -> list:
    """Загружает все события по одному виду спорта. Использует кэш с
    динамическим TTL: 5 минут если есть матч в ближайшие 90 минут, иначе 30."""
    global _odds_cache
    now = time.monotonic()

    if sport in _odds_cache:
        ts, events = _odds_cache[sport]
        ttl = _cache_ttl_for_sport(events)
        if now - ts < ttl:
            return events

    url = f"{config.odds_base}/sports/{sport}/odds/"
    params = {
        "apiKey": config.odds_api_key,
        "regions": "eu",
        "markets": "h2h,totals",
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
                body = await r.text()
                log.warning("odds-api [%s] status %s: %s", sport, r.status, body[:300])
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
        if not config.allow_demo_data:
            log.warning("ODDS_API_KEY не задан — коэффициенты для %s vs %s недоступны", home, away)
            return None
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
                    await _enrich_event_odds(http, ev, odds)
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


async def _fetch_finished_matches(
    http: aiohttp.ClientSession,
    headers: dict[str, str],
    comp: str,
) -> list[dict]:
    now = time.monotonic()
    cached = _finished_matches_cache.get(comp)
    if cached and (now - cached[0]) < _FINISHED_MATCHES_TTL:
        return cached[1]

    url = f"{config.football_base}/competitions/{comp}/matches"
    params = {
        "status": "FINISHED",
        "dateFrom": (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d"),
        "dateTo": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    try:
        async with http.get(url, headers=headers, params=params) as r:
            if r.status == 429:
                log.warning("football-data rate limit на finished/%s", comp)
                return cached[1] if cached else []
            if r.status != 200:
                return cached[1] if cached else []
            payload = await r.json()
    except Exception:
        return cached[1] if cached else []

    matches = payload.get("matches", [])
    _finished_matches_cache[comp] = (time.monotonic(), matches)
    return matches


async def _enrich_event_odds(http: aiohttp.ClientSession, event: dict, odds: Odds) -> None:
    """Подтягивает дополнительные рынки для конкретного события.

    По документации The Odds API endpoint `/sports/{sport}/odds` поддерживает
    только featured markets. Для `btts` используем event odds endpoint.
    """
    sport = event.get("sport_key")
    event_id = event.get("id")
    if not sport or not event_id:
        return

    if odds.btts_yes > 1 and odds.btts_no > 1:
        return

    url = f"{config.odds_base}/sports/{sport}/events/{event_id}/odds"
    params = {
        "apiKey": config.odds_api_key,
        "regions": "eu",
        "markets": "btts",
        "oddsFormat": "decimal",
    }
    try:
        async with http.get(url, params=params) as r:
            if r.status != 200:
                return
            payload = await r.json()
    except Exception as e:
        log.debug("event-odds btts failed for %s: %s", event_id, e)
        return

    enriched = _best_odds_from_event(payload, payload.get("home_team", ""))
    if enriched.btts_yes > odds.btts_yes:
        odds.btts_yes = enriched.btts_yes
    if enriched.btts_no > odds.btts_no:
        odds.btts_no = enriched.btts_no
    if enriched.bookmaker and not odds.bookmaker:
        odds.bookmaker = enriched.bookmaker


# ────────────────────────────────────────────────────────────────
# Финальные счета — для settlement движка
# ────────────────────────────────────────────────────────────────
# Кэш скоринга: {sport_key: (timestamp, [score_event...])}
_scores_cache: dict[str, tuple[float, list[dict]]] = {}
_SCORES_CACHE_TTL = 1200  # 20 минут


@dataclass
class MatchScore:
    home_goals: int
    away_goals: int
    completed: bool
    source: str
    last_update: Optional[datetime] = None


async def _fetch_sport_scores(http: aiohttp.ClientSession, sport: str,
                              days_from: int = 3) -> list[dict]:
    """
    GET /v4/sports/{sport}/scores/?daysFrom=N
    Возвращает завершённые и идущие матчи за последние N дней.
    Стоит 2 квоты за вызов на free tier — поэтому кэш на 20 мин.
    """
    if not config.odds_api_key:
        return []
    now = time.monotonic()
    cached = _scores_cache.get(sport)
    if cached and (now - cached[0]) < _SCORES_CACHE_TTL:
        return cached[1]

    url = f"{config.odds_base}/sports/{sport}/scores/"
    params = {
        "apiKey": config.odds_api_key,
        "daysFrom": days_from,
        "dateFormat": "iso",
    }
    try:
        async with http.get(url, params=params) as r:
            if r.status == 429:
                log.warning("odds-api scores rate limit на %s", sport)
                return cached[1] if cached else []
            if r.status == 401:
                log.error("odds-api scores: неверный ключ (401)")
                return []
            if r.status != 200:
                body = await r.text()
                log.warning("odds-api scores [%s] %s: %s", sport, r.status, body[:200])
                return cached[1] if cached else []
            data = await r.json()
    except Exception as e:
        log.warning("odds-api scores [%s] error: %s", sport, e)
        return cached[1] if cached else []

    _scores_cache[sport] = (now, data)
    log.info("odds-api scores [%s]: %d матчей", sport, len(data))
    return data


def _parse_score_event(ev: dict, home_team: str, away_team: str
                       ) -> Optional[MatchScore]:
    """Достаём счёт из одного score-события odds-api."""
    if not ev.get("completed"):
        return None
    scores_arr = ev.get("scores") or []
    if len(scores_arr) < 2:
        return None
    by_team = {}
    for s in scores_arr:
        name = (s.get("name") or "").strip()
        try:
            by_team[name] = int(s.get("score") or 0)
        except (TypeError, ValueError):
            return None
    # API возвращает имена ровно как home_team/away_team в том же событии,
    # но на всякий случай fallback по позициям.
    h = by_team.get(home_team)
    a = by_team.get(away_team)
    if h is None or a is None:
        # позиционный fallback
        h = int(scores_arr[0].get("score") or 0) if h is None else h
        a = int(scores_arr[1].get("score") or 0) if a is None else a
    last = None
    if ev.get("last_update"):
        try:
            last = datetime.fromisoformat(ev["last_update"].replace("Z", "+00:00"))
        except Exception:
            last = None
    return MatchScore(home_goals=h, away_goals=a, completed=True,
                      source="odds-api", last_update=last)


async def fetch_match_score(home: str, away: str,
                            sport_key: str = "",
                            external_id: str = "") -> Optional[MatchScore]:
    """
    Возвращает финальный счёт (90 минут / FT) или None.

    Источники в порядке убывания приоритета:
    1) the-odds-api scores (если знаем sport_key — один вызов; иначе — перебор)
    2) football-data.org finished — fallback (рейтинг лимит, нет UEFA на free)

    Не возвращает счёт по AET/penalties — odds-api отдаёт основное время.
    """
    timeout = aiohttp.ClientTimeout(total=20)
    sports_to_try = [sport_key] if sport_key else list(config.odds_sports)

    async with aiohttp.ClientSession(timeout=timeout) as http:
        for sport in sports_to_try:
            if not sport:
                continue
            events = await _fetch_sport_scores(http, sport)
            for ev in events:
                if external_id and str(ev.get("id", "")) == str(external_id):
                    score = _parse_score_event(ev, ev.get("home_team", ""),
                                               ev.get("away_team", ""))
                    if score:
                        return score
                ev_home = ev.get("home_team", "")
                ev_away = ev.get("away_team", "")
                if (_teams_match(home, ev_home) and _teams_match(away, ev_away)):
                    score = _parse_score_event(ev, ev_home, ev_away)
                    if score:
                        return score

        # Fallback: football-data.org
        if config.football_api_key:
            headers = {"X-Auth-Token": config.football_api_key}
            for comp in config.football_competitions:
                matches = await _fetch_finished_matches(http, headers, comp)
                for m in matches:
                    fd_home = (m.get("homeTeam") or {}).get("name", "")
                    fd_away = (m.get("awayTeam") or {}).get("name", "")
                    if _teams_match(home, fd_home) and _teams_match(away, fd_away):
                        ft = (m.get("score") or {}).get("fullTime") or {}
                        h = ft.get("home")
                        a = ft.get("away")
                        if h is None or a is None:
                            continue
                        return MatchScore(
                            home_goals=int(h), away_goals=int(a),
                            completed=True, source="football-data",
                        )
    return None


async def fetch_current_odds_decimal(home: str, away: str, market_pick: str
                                     ) -> Optional[float]:
    """
    Возвращает текущий decimal-коэф для конкретной ставки (для CLV-снепшота).
    market_pick формат: "1X2:home", "1X2:draw", "1X2:away",
                        "TOTAL_2_5:over", "TOTAL_2_5:under",
                        "BTTS:yes", "BTTS:no".
    """
    odds = await fetch_odds(home, away)
    if not odds:
        return None
    market, _, side = market_pick.partition(":")
    market = market.upper()
    side = side.lower()
    mapping = {
        ("1X2", "home"): odds.home,
        ("1X2", "draw"): odds.draw,
        ("1X2", "away"): odds.away,
        ("TOTAL_2_5", "over"): odds.over_2_5,
        ("TOTAL_2_5", "under"): odds.under_2_5,
        ("BTTS", "yes"): odds.btts_yes,
        ("BTTS", "no"): odds.btts_no,
    }
    val = mapping.get((market, side))
    return val if val and val > 1.01 else None
