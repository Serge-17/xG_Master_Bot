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
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./xg_master_bot.db")
    sqlalchemy_echo: bool = os.getenv("SQLALCHEMY_ECHO", "0") == "1"
    default_bet_percent: float = float(os.getenv("DEFAULT_BET_PERCENT", "0.03"))
    ai_provider: str = os.getenv("AI_PROVIDER", "mock").lower()
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    hf_api_token: str = os.getenv("HF_API_TOKEN", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    hf_model: str = os.getenv("HF_MODEL", "mistralai/Mistral-7B-Instruct-v0.3")
    hf_inference_url: str = os.getenv("HF_INFERENCE_URL", "https://api-inference.huggingface.co/models")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
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
    ocr_provider: str = os.getenv("OCR_PROVIDER", "tesseract").lower()
    ocr_enabled: bool = os.getenv("OCR_ENABLED", "1") == "1"
    ocr_lang: str = os.getenv("OCR_LANG", "eng")
    telegram_download_dir: str = os.getenv("TELEGRAM_DOWNLOAD_DIR", "./downloads")


settings = Settings()
