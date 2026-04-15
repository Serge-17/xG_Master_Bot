from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


_PACKAGE_DIR = Path(__file__).resolve().parent

# Загружаем .env файлы
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
    sqlalchemy_echo: bool = os.getenv("SQLALCHEMY_ECHO", "0") == "1"

    # Betting settings
    default_bet_percent: float = float(os.getenv("DEFAULT_BET_PERCENT", "0.03"))
    reserve_bankroll_fraction: float = float(os.getenv("RESERVE_BANKROLL_FRACTION", "0.25"))
    digest_matches_limit: int = int(os.getenv("DIGEST_MATCHES_LIMIT", "12"))
    digest_post_minute: int = int(os.getenv("DIGEST_POST_MINUTE", "5"))

    # AI Providers
    ai_provider: str = os.getenv("AI_PROVIDER", "mock").lower()
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    hf_api_token: str = os.getenv("HF_API_TOKEN", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    hf_model: str = os.getenv("HF_MODEL", "mistralai/Mistral-7B-Instruct-v0.3")
    hf_inference_url: str = os.getenv(
        "HF_INFERENCE_URL", 
        "https://api-inference.huggingface.co/models"
    )

    # Data Providers - API-Football (главное сейчас)
    api_football_key: str = os.getenv("API_FOOTBALL_KEY", "")
    data_provider: str = os.getenv("DATA_PROVIDER", "api_football").lower()

    # The Odds API (для коэффициентов в будущем)
    odds_api_key: str = os.getenv("ODDS_API_KEY", "")

    # OCR и другие
    ocr_provider: str = os.getenv("OCR_PROVIDER", "tesseract").lower()
    ocr_enabled: bool = os.getenv("OCR_ENABLED", "1") == "1"
    telegram_download_dir: str = os.getenv("TELEGRAM_DOWNLOAD_DIR", "./downloads")

    # News
    news_enabled: bool = os.getenv("NEWS_ENABLED", "1") == "1"


# Глобальный объект настроек
settings = Settings()


# Удобные проверки
def is_api_football_enabled() -> bool:
    return bool(settings.api_football_key)


def check_config() -> None:
    """Быстрая проверка критических настроек"""
    if not settings.telegram_bot_token:
        print("⚠️ WARNING: TELEGRAM_BOT_TOKEN не найден!")
    if not settings.api_football_key:
        print("⚠️ WARNING: API_FOOTBALL_KEY не найден! Бот не сможет получать матчи.")
    else:
        print("✅ API_Football ключ загружен успешно")