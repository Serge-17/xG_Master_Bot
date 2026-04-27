"""
web_scrapers.py — best-effort веб-скрапинг коэффициентов и контекста матча.
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

from data_sources import Odds


# sports.ru показывает время в МСК (UTC+3). Конвертим в UTC.
_MSK_OFFSET_HOURS = 3


log = logging.getLogger(__name__)


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
}


@dataclass
class MatchContext:
    source: str = ""
    source_url: str = ""
    facts: list[str] = field(default_factory=list)
    injuries: list[str] = field(default_factory=list)
    stats: list[str] = field(default_factory=list)
    raw_text: str = ""


def _norm(text: str) -> str:
    text = html.unescape(text or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


async def _fetch_text(url: str, timeout_s: int = 20) -> Optional[str]:
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=DEFAULT_HEADERS) as http:
            async with http.get(url, allow_redirects=True) as r:
                if r.status != 200:
                    log.info("scraper %s -> %s", url, r.status)
                    return None
                return await r.text()
    except Exception as e:
        log.warning("scraper error %s: %s", url, e)
        return None


async def search_sports_ru_match(home: str, away: str) -> Optional[str]:
    queries = [
        f'site:sports.ru/football/match "{home}" "{away}" коэффициенты',
        f'site:sports.ru/football/match "{home}" "{away}"',
    ]
    for query in queries:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        text = await _fetch_text(url)
        if not text:
            continue
        soup = BeautifulSoup(text, "html.parser")
        for a in soup.select("a.result__a, a[data-testid='result-title-a']"):
            href = a.get("href") or ""
            if "sports.ru/football/match/" in href:
                return href if "/odds/" in href else href.rstrip("/") + "/odds/"
    return None


def _parse_schedule_datetime(time_text: str) -> Optional[datetime]:
    """sports.ru печатает время в МСК. Конвертируем в UTC.

    Если получившееся UTC-время оказывается раньше «сейчас минус 6 часов»,
    значит матч уже завтрашний по МСК — добавляем сутки.
    """
    m = re.match(r"^(\d{2}):(\d{2})$", _norm(time_text))
    if not m:
        return None
    now_utc = datetime.now(timezone.utc)
    msk_today = now_utc + timedelta(hours=_MSK_OFFSET_HOURS)
    msk_dt = msk_today.replace(
        hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0
    )
    utc_dt = msk_dt - timedelta(hours=_MSK_OFFSET_HOURS)
    if utc_dt < now_utc - timedelta(hours=6):
        utc_dt += timedelta(days=1)
    return utc_dt


async def scrape_sports_ru_matches() -> list[dict]:
    text = await _fetch_text("https://www.sports.ru/football/match/")
    if not text:
        return []

    soup = BeautifulSoup(text, "html.parser")
    raw_lines = [_norm(line) for line in soup.get_text("\n").splitlines()]
    lines = [line for line in raw_lines if line]
    matches: list[dict] = []
    current_league = ""

    league_re = re.compile(r"^[^\d|]{3,}\d{4}/\d{4}(?:\s*\(\d+\))?$")
    row_re = re.compile(r"^\d{2}:\d{2}\s*\|")

    for line in lines:
        if league_re.match(line):
            current_league = line
            continue
        if not current_league or not row_re.match(line):
            continue

        parts = [_norm(part) for part in line.split("|")]
        if len(parts) < 5:
            continue

        kickoff = _parse_schedule_datetime(parts[0])
        status = parts[1].lower()
        home = parts[2]
        away = parts[4]
        if not home or not away:
            continue
        if any(flag in status for flag in ["заверш", "отмен", "перенес"]):
            continue

        matches.append({
            "home": home,
            "away": away,
            "competition": current_league,
            "utc_date": kickoff,
            "status": parts[1],
        })

    return matches


def _extract_decimal_odds(text: str) -> list[float]:
    values: list[float] = []
    for match in re.finditer(r"(?<!\d)([1-9]\d?(?:[.,]\d{1,2}))(?!\d)", text):
        val = float(match.group(1).replace(",", "."))
        if 1.01 <= val <= 50.0:
            values.append(val)
    return values


def _odds_from_text(page_text: str) -> Optional[Odds]:
    """Парсим только из блока «Коэффициенты»/«1X2», иначе возвращаем None.

    Старая версия брала первые три числа в диапазоне 1.01-50 из всего HTML —
    могла подхватить владение мячом, рейтинги или цены билетов. Теперь требуем
    явные метки П1/X/П2 в окне ±400 символов от ключевого слова.
    """
    text = _norm(page_text)

    # Сначала пытаемся найти блок коэффициентов
    block_keywords = [
        "коэффициент", "котировк", "ставк",
        "линия букмекер", "1x2", "победа", "исход матча",
    ]
    block: Optional[str] = None
    text_lower = text.lower()
    for kw in block_keywords:
        idx = text_lower.find(kw)
        if idx >= 0:
            start = max(0, idx - 100)
            end = min(len(text), idx + 600)
            block = text[start:end]
            break

    haystack = block if block else text[:3000]

    # Требуем явные метки. Ничейный исход обозначается X (или «ничья»).
    p1 = re.search(
        r"(?:П1|^1\b|\b1\b\s*[—–-])\s*[:\s]*([1-9](?:\.\d{1,2}|,\d{1,2}))",
        haystack, flags=re.IGNORECASE,
    )
    px = re.search(
        r"(?:\bX\b|ничья|ничь[ая])\s*[:\s]*([1-9](?:\.\d{1,2}|,\d{1,2}))",
        haystack, flags=re.IGNORECASE,
    )
    p2 = re.search(
        r"(?:П2|\b2\b\s*[—–-])\s*[:\s]*([1-9](?:\.\d{1,2}|,\d{1,2}))",
        haystack, flags=re.IGNORECASE,
    )

    def _val(m) -> float:
        return float(m.group(1).replace(",", ".")) if m else 0.0

    home, draw, away = _val(p1), _val(px), _val(p2)
    if home > 1.0 and draw > 1.0 and away > 1.0:
        return Odds(home=home, draw=draw, away=away, bookmaker="sports.ru")

    # Если меток не нашли — возвращаем None, не угадываем по случайным числам
    return None


async def scrape_sports_ru_odds(match_url: str) -> tuple[Optional[Odds], MatchContext]:
    text = await _fetch_text(match_url)
    ctx = MatchContext(source="sports.ru", source_url=match_url)
    if not text:
        return None, ctx

    soup = BeautifulSoup(text, "html.parser")
    page_text = _norm(soup.get_text(" ", strip=True))
    ctx.raw_text = page_text[:4000]

    odds = _odds_from_text(page_text)

    for label in ("Стадион", "Главный судья", "Турнир", "О матче"):
        m = re.search(
            rf"{label}\s*:?\s*([^:]+?)(?=(Стадион|Главный судья|Матч|Статистика|Коэффициенты|$))",
            page_text,
            re.IGNORECASE,
        )
        if m:
            fact = _norm(f"{label}: {m.group(1)}")
            if fact not in ctx.facts:
                ctx.facts.append(fact)

    stat_patterns = {
        "Победы": r"Победы\s+(\d+)",
        "Голы": r"Голы\s+(\d+[:\-]\d+)",
        "Удары в створ": r"Удары в створ\s+(\d+[:\-]\d+)",
        "Владение": r"Владение\s+(\d+%?[:\-]\d+%?)",
    }
    for label, pattern in stat_patterns.items():
        m = re.search(pattern, page_text, re.IGNORECASE)
        if m:
            ctx.stats.append(f"{label}: {_norm(m.group(1))}")

    return odds, ctx


async def scrape_team_injuries(team_name: str) -> list[str]:
    queries = [
        f'site:sports.ru "{team_name}" травмы футбол',
        f'site:sports.ru "{team_name}" не сыграет футбол',
    ]
    results: list[str] = []
    for query in queries:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        text = await _fetch_text(url, timeout_s=12)
        if not text:
            continue
        soup = BeautifulSoup(text, "html.parser")
        for item in soup.select(".result"):
            title_node = item.select_one(".result__title") or item.select_one("a.result__a")
            snippet_node = item.select_one(".result__snippet")
            title = _norm(title_node.get_text(" ", strip=True) if title_node else "")
            snippet = _norm(snippet_node.get_text(" ", strip=True) if snippet_node else "")
            combined = " — ".join(part for part in [title, snippet] if part)
            if combined and combined not in results:
                results.append(combined[:220])
            if len(results) >= 2:
                return results
    return results


async def fetch_web_odds_and_context(home: str, away: str) -> tuple[Optional[Odds], MatchContext]:
    match_url = await search_sports_ru_match(home, away)
    if not match_url:
        return None, MatchContext()

    odds, ctx = await scrape_sports_ru_odds(match_url)
    ctx.injuries.extend(await scrape_team_injuries(home))
    ctx.injuries.extend(await scrape_team_injuries(away))

    deduped: list[str] = []
    for item in ctx.injuries:
        if item not in deduped:
            deduped.append(item)
    ctx.injuries = deduped[:4]
    return odds, ctx
