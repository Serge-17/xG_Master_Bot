from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


_PACKAGE_DIR = Path(__file__).resolve().parent
load_dotenv(_PACKAGE_DIR / ".env", override=False)
load_dotenv(Path.cwd() / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    # Telegram
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_webhook_secret: str = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    telegram_webhook_url: str = os.getenv("TELEGRAM_WEBHOOK_URL", "")
    telegram_channel_id: int = int(os.getenv("CHANNEL_ID", "0") or "0")

    # Database
    database_url: str = os.getenv(
        "DATABASE_URL", "sqlite+aiosqlite:///xg_master_bot.db"
    )

    # Betting
    default_bet_percent: float = float(os.getenv("DEFAULT_BET_PERCENT", "0.03"))
    reserve_bankroll_fraction: float = float(os.getenv("RESERVE_BANKROLL_FRACTION", "0.25"))
    digest_matches_limit: int = int(os.getenv("DIGEST_MATCHES_LIMIT", "12"))

    # AI
    ai_provider: str = os.getenv("AI_PROVIDER", "mock").lower()
    hf_api_token: str = os.getenv("HF_API_TOKEN", "")
    hf_model: str = os.getenv("HF_MODEL", "")

    # === Главное сейчас ===
    api_football_key: str = os.getenv("API_FOOTBALL_KEY", "")   # ←←← Это обязательно!

    # Дополнительно
    data_provider: str = os.getenv("DATA_PROVIDER", "api_football").lower()
    odds_api_key: str = os.getenv("ODDS_API_KEY", "")


settings = Settings()


# Быстрая проверка
def check_config():
    if not settings.telegram_bot_token:
        print("❌ TELEGRAM_BOT_TOKEN не найден!")
    if not settings.api_football_key:
        print("❌ API_FOOTBALL_KEY не найден! Матчи не будут загружаться.")
    else:
        print(f"✅ API_Football ключ загружен ({len(settings.api_football_key)} символов)")