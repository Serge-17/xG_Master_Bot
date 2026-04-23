"""
config.py — центральный конфиг xG Master Bot
Переменные окружения совпадают с HuggingFace Secrets:
  BOT_TOKEN, TELEGRAM_TOKEN, GEMINI_API_KEY,
  FOOTBALL_DATA_API_KEY, ODDS_API_KEY, DATABASE_URL
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    # ── Telegram ──────────────────────────────────────────────
    # HuggingFace хранит и BOT_TOKEN и TELEGRAM_TOKEN — берём любой
    telegram_token: str = field(default_factory=lambda: (
        os.getenv("BOT_TOKEN") or
        os.getenv("TELEGRAM_TOKEN") or
        os.getenv("TELEGRAM_BOT_TOKEN") or
        ""
    ))

    # ID канала для авто-публикации прогнозов (например: @xg_master_channel)
    channel_id: str = field(default_factory=lambda: os.getenv("CHANNEL_ID", ""))

    # ID администратора бота
    admin_id: int = field(default_factory=lambda: int(os.getenv("ADMIN_ID", "0")))

    # ── Режим запуска ──────────────────────────────────────────
    # "webhook" для HuggingFace/Railway, "polling" для локальной разработки
    bot_mode: str = field(default_factory=lambda: os.getenv("BOT_MODE", "webhook"))

    # Публичный URL Space на HuggingFace (нужен для webhook)
    # Формат: https://serge-17-xg-master-bot.hf.space
    webhook_url: str = field(default_factory=lambda: os.getenv("WEBHOOK_URL", ""))

    webhook_path: str = "/webhook"
    webapp_host: str = "0.0.0.0"
    webapp_port: int = field(default_factory=lambda: int(os.getenv("PORT", "7860")))

    # ── Футбольные данные ──────────────────────────────────────
    # API-Football через api-football.com или RapidAPI
    # В HuggingFace называется: FOOTBALL_DATA_API_KEY
    football_api_key: str = field(default_factory=lambda: (
        os.getenv("FOOTBALL_DATA_API_KEY") or
        os.getenv("FOOTBALL_API_KEY") or
        ""
    ))
    football_api_base: str = "https://v3.football.api-sports.io"

    # Топ-10 лиг (ID для API-Football)
    top_leagues: dict = field(default_factory=lambda: {
        "EPL":        39,   # Англия
        "La Liga":    140,  # Испания
        "Bundesliga": 78,   # Германия
        "Serie A":    135,  # Италия
        "Ligue 1":    61,   # Франция
        "Champions":  2,    # Лига Чемпионов
        "Europa":     3,    # Лига Европы
        "RPL":        235,  # Россия
        "Eredivisie": 88,   # Нидерланды
        "Primeira":   94,   # Португалия
    })

    # ── Коэффициенты букмекеров ────────────────────────────────
    # The Odds API — the-odds-api.com
    # В HuggingFace называется: ODDS_API_KEY
    odds_api_key: str = field(default_factory=lambda: os.getenv("ODDS_API_KEY", ""))
    odds_api_base: str = "https://api.the-odds-api.com/v4"
    odds_regions: str = "eu"          # eu / uk / us / au
    odds_markets: str = "h2h,totals"  # 1X2, тоталы
    odds_format: str = "decimal"

    # ── Gemini AI (OCR + аналитика) ────────────────────────────
    # В HuggingFace называется: GEMINI_API_KEY
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = "gemini-1.5-flash"

    # ── База данных ────────────────────────────────────────────
    # Для HuggingFace рекомендуется PostgreSQL (neon.tech бесплатно)
    # Формат: postgresql://user:pass@host/dbname?sslmode=require
    # Для теста локально: sqlite:///./bot.db
    database_url: str = field(default_factory=lambda: (
        os.getenv("DATABASE_URL") or
        "sqlite:///./bot.db"
    ))

    # ── Риск-менеджмент ────────────────────────────────────────
    default_bank: float = 10000.0        # банк по умолчанию (руб.)
    min_bet_fraction: float = 0.02       # мин. ставка 2% от банка
    max_bet_fraction: float = 0.10       # макс. ставка 10% от банка
    value_bet_threshold: float = 0.05    # порог value: наш_odds < букмекер * (1 - 0.05)
    min_probability: float = 0.55        # мин. вероятность для рекомендации

    # ── Расписание авто-сканирования ───────────────────────────
    scan_hour_utc: int = 8               # сканировать матчи в 08:00 UTC
    publish_hour_utc: int = 9            # публиковать прогнозы в 09:00 UTC
    results_hour_utc: int = 23           # обновлять результаты в 23:00 UTC

    def validate(self) -> list[str]:
        """Проверяет конфигурацию, возвращает список ошибок."""
        errors = []
        if not self.telegram_token:
            errors.append("❌ Нет Telegram токена (BOT_TOKEN или TELEGRAM_TOKEN)")
        if not self.football_api_key:
            errors.append("❌ Нет FOOTBALL_DATA_API_KEY")
        if not self.odds_api_key:
            errors.append("❌ Нет ODDS_API_KEY")
        if self.bot_mode == "webhook" and not self.webhook_url:
            errors.append("⚠️  BOT_MODE=webhook, но WEBHOOK_URL не задан")
        return errors

    def summary(self) -> str:
        """Выводит сводку конфига (без секретов)."""
        return (
            f"Bot mode:     {self.bot_mode}\n"
            f"Webhook URL:  {self.webhook_url or '—'}\n"
            f"Football API: {'✅' if self.football_api_key else '❌'}\n"
            f"Odds API:     {'✅' if self.odds_api_key else '❌'}\n"
            f"Gemini AI:    {'✅' if self.gemini_api_key else '❌'}\n"
            f"Database:     {self.database_url.split('@')[-1] if '@' in self.database_url else self.database_url}\n"
            f"Channel ID:   {self.channel_id or '—'}\n"
            f"Admin ID:     {self.admin_id or '—'}\n"
        )


# Синглтон — импортируй везде как: from config import config
config = Config()