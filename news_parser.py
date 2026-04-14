from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

RSS_FEEDS: dict[str, str] = {
    "sky_sports": "https://www.skysports.com/rss/12040",
    "bbc_sport": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "sports_ru": "https://www.sports.ru/rss/football.xml",
    "goal_com": "https://www.goal.com/feeds/en/news",
}


@dataclass(slots=True)
class NewsItem:
    title: str
    summary: str
    source: str
    link: str = ""
    published: str = ""


def _fetch_feed(url: str, source: str, limit: int = 10) -> list[NewsItem]:
    try:
        import feedparser  # type: ignore

        feed = feedparser.parse(url)
        items: list[NewsItem] = []
        for entry in feed.entries[:limit]:
            items.append(
                NewsItem(
                    title=entry.get("title", ""),
                    summary=entry.get("summary", entry.get("description", ""))[:300],
                    source=source,
                    link=entry.get("link", ""),
                    published=entry.get("published", ""),
                )
            )
        return items
    except Exception as exc:
        logger.warning("Feed %s failed: %s", url, exc)
        return []


def fetch_news(team_name: str | None = None, limit_per_feed: int = 5) -> list[NewsItem]:
    """Fetch news from all RSS feeds, optionally filtered by team name."""
    all_items: list[NewsItem] = []
    for source, url in RSS_FEEDS.items():
        all_items.extend(_fetch_feed(url, source, limit=limit_per_feed))

    if team_name:
        normalized = team_name.lower()
        filtered = [item for item in all_items if normalized in item.title.lower() or normalized in item.summary.lower()]
        return filtered if filtered else all_items[:limit_per_feed]

    return all_items


def build_news_summary(team_name: str | None = None, max_items: int = 5) -> str:
    items = fetch_news(team_name=team_name, limit_per_feed=max_items)
    if not items:
        return "Новости не найдены."
    lines = []
    for item in items[:max_items]:
        lines.append(f"[{item.source}] {item.title}: {item.summary[:120]}...")
    return "\n".join(lines)
