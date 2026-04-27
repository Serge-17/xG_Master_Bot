"""
config.py — единый источник env-переменных xG Master Bot.

HuggingFace Secrets (ожидаемые имена):
  TELEGRAM_BOT_TOKEN (или BOT_TOKEN / TELEGRAM_TOKEN)
  GEMINI_API_KEY
  FOOTBALL_API_KEY (или FOOTBALL_DATA_API_KEY)
  ODDS_API_KEY
  DATABASE_URL           — postgresql://...   (Neon)
  CHANNEL_ID             — @channel_username или -100xxxxxxxxxx
  ADMIN_ID               — telegram user id админа (int)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field


log = logging.getLogger(__name__)


def _normalize_channel_id(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("@") or v.startswith("-100"):
        return v
    if v.startswith("-"):
        return v
    if v.isdigit():
        return f"-100{v}"
    return v


def _normalize_db_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    url = url.replace("sslmode=require", "ssl=require")
    url = url.replace("sslmode=req", "ssl=require")
    url = url.replace("ssl=true", "ssl=require")
    return url


@dataclass
class Config:
    # Telegram
    telegram_token: str = field(default_factory=lambda: (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("BOT_TOKEN")
        or os.getenv("TELEGRAM_TOKEN")
        or ""
    ))
    channel_id: str = field(default_factory=lambda: _normalize_channel_id(os.getenv("CHANNEL_ID", "")))
    admin_id: int = field(default_factory=lambda: int(os.getenv("ADMIN_ID", "0") or 0))

    # Внешние API
    football_api_key: str = field(default_factory=lambda: (
        os.getenv("FOOTBALL_API_KEY")
        or os.getenv("FOOTBALL_DATA_API_KEY")
        or ""
    ))
    odds_api_key: str = field(default_factory=lambda: os.getenv("ODDS_API_KEY", ""))
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))

    # БД
    database_url: str = field(default_factory=lambda: _normalize_db_url(
        os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./xg_master.db")
    ))

    # Модели и endpoints
    gemini_model: str = "gemini-2.0-flash"
    gemini_base: str = "https://generativelanguage.googleapis.com/v1beta/models"
    football_base: str = "https://api.football-data.org/v4"
    odds_base: str = "https://api.the-odds-api.com/v4"

    # FIX: Убраны EL (Europa League) и CL (Champions League) — дают 403 на free tier.
    # Free tier football-data.org включает только национальные лиги.
    football_competitions: tuple = (
        "PL",   # Premier League
        "PD",   # La Liga
        "BL1",  # Bundesliga
        "SA",   # Serie A
        "FL1",  # Ligue 1
        "DED",  # Eredivisie
        "PPL",  # Primeira Liga
        "ELC",  # Championship
    )

    odds_sports: tuple = (
        "soccer_epl",
        "soccer_spain_la_liga",
        "soccer_germany_bundesliga",
        "soccer_italy_serie_a",
        "soccer_france_ligue_one",
        "soccer_netherlands_eredivisie",
        "soccer_portugal_primeira_liga",
        "soccer_efl_champ",
    )

    # Риск-менеджмент
    kelly_cap: float = 0.05
    min_confidence: int = 40
    min_edge: float = 0.015

    # Расписание (UTC)
    daily_scan_hour: int = 9
    daily_digest_hour: int = 12
    max_signals_per_day: int = 5

    # HTTP / Space
    webapp_host: str = "0.0.0.0"
    webapp_port: int = field(default_factory=lambda: int(os.getenv("PORT", "7860")))

    # FIX: TTL кэша матчей в секундах (60 мин). Предотвращает повторные
    # запросы к API при нескольких /scan подряд.
    matches_cache_ttl: int = 3600

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.telegram_token:
            problems.append("TELEGRAM_BOT_TOKEN не задан")
        if not self.gemini_api_key:
            problems.append("GEMINI_API_KEY не задан (аналитика и OCR работать не будут)")
        if not self.football_api_key:
            problems.append("FOOTBALL_API_KEY не задан (будут демо-матчи)")
        if not self.odds_api_key:
            problems.append("ODDS_API_KEY не задан (будут демо-коэффициенты)")
        return problems

    def summary(self) -> str:
        host = "—"
        if "@" in self.database_url:
            host = self.database_url.split("@", 1)[1].split("/", 1)[0]
        return (
            f"Telegram:  {'✅' if self.telegram_token else '❌'}\n"
            f"Gemini:    {'✅' if self.gemini_api_key else '❌'}\n"
            f"Football:  {'✅' if self.football_api_key else '❌'}\n"
            f"Odds:      {'✅' if self.odds_api_key else '❌'}\n"
            f"DB host:   {host}\n"
            f"Channel:   {self.channel_id or '—'}\n"
            f"Admin:     {self.admin_id or '—'}\n"
        )


config = Config()
