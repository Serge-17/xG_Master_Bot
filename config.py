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
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_channel_id: str = os.getenv("TELEGRAM_CHANNEL_ID", "")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./xg_master_bot.db")
    sqlalchemy_echo: bool = os.getenv("SQLALCHEMY_ECHO", "0") == "1"
    default_bet_percent: float = float(os.getenv("DEFAULT_BET_PERCENT", "0.03"))
    reserve_bankroll_fraction: float = float(os.getenv("RESERVE_BANKROLL_FRACTION", "0.25"))
    digest_matches_limit: int = int(os.getenv("DIGEST_MATCHES_LIMIT", "12"))
    digest_post_minute: int = int(os.getenv("DIGEST_POST_MINUTE", "5"))

    # AI providers
    ai_provider: str = os.getenv("AI_PROVIDER", "mock").lower()
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    hf_api_token: str = os.getenv("HF_API_TOKEN", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    hf_model: str = os.getenv("HF_MODEL", "mistralai/Mistral-7B-Instruct-v0.3")
    hf_inference_url: str = os.getenv("HF_INFERENCE_URL", "https://api-inference.huggingface.co/models")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

    # Data providers
    data_provider: str = os.getenv("DATA_PROVIDER", "open").lower()
    statsbomb_base_url: str = os.getenv(
        "STATSBOMB_BASE_URL",
        "https://raw.githubusercontent.com/statsbomb/open-data/master/data",
    )
    football_data_fixtures_url: str = os.getenv(
        "FOOTBALL_DATA_FIXTURES_URL",
        "https://www.football-data.co.uk/matches/resources/fixtures.csv",
    )
    football_data_historical_url: str = os.getenv(
        "FOOTBALL_DATA_HISTORICAL_URL",
        "https://www.football-data.co.uk",
    )

    # API-Football (RapidAPI)
    api_football_key: str = os.getenv("API_FOOTBALL_KEY", "")
    api_football_host: str = os.getenv("API_FOOTBALL_HOST", "v3.football.api-sports.io")

    # The Odds API
    odds_api_key: str = os.getenv("ODDS_API_KEY", "")
    odds_api_regions: str = os.getenv("ODDS_API_REGIONS", "eu")
    odds_api_markets: str = os.getenv("ODDS_API_MARKETS", "h2h,totals")

    # OCR
    ocr_provider: str = os.getenv("OCR_PROVIDER", "tesseract").lower()
    ocr_enabled: bool = os.getenv("OCR_ENABLED", "1") == "1"
    ocr_lang: str = os.getenv("OCR_LANG", "eng+rus")
    telegram_download_dir: str = os.getenv("TELEGRAM_DOWNLOAD_DIR", "./downloads")

    # News
    news_enabled: bool = os.getenv("NEWS_ENABLED", "1") == "1"


settings = Settings()
