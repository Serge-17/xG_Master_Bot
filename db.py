"""
database/db.py — инициализация базы данных
Поддерживает PostgreSQL (продакшен) и SQLite (разработка)
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text, BigInteger
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


# ── Модели таблиц ──────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)        # Telegram user_id
    username = Column(String(64), nullable=True)
    first_name = Column(String(64), nullable=True)
    bank = Column(Float, default=10000.0)            # текущий банк (руб.)
    initial_bank = Column(Float, default=10000.0)    # стартовый банк
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_active = Column(Boolean, default=True)


class Bet(Base):
    __tablename__ = "bets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    match_id = Column(String(64), nullable=True)
    match_name = Column(String(255), nullable=False)
    league = Column(String(64), nullable=True)
    market = Column(String(128), nullable=False)      # тип ставки
    bookmaker_odds = Column(Float, nullable=False)    # коэф. букмекера
    fair_odds = Column(Float, nullable=False)         # наш расчётный коэф.
    probability = Column(Float, nullable=False)       # вероятность (0-1)
    bet_amount = Column(Float, nullable=False)        # сумма ставки (руб.)
    win_amount = Column(Float, nullable=True)         # сумма выигрыша
    status = Column(String(16), default="pending")    # pending/won/lost/void
    screenshot_text = Column(Text, nullable=True)     # распознанный текст OCR
    event_time = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    settled_at = Column(DateTime, nullable=True)


class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(String(64), nullable=True)
    match_name = Column(String(255), nullable=False)
    league = Column(String(64), nullable=True)
    market = Column(String(128), nullable=False)
    bookmaker_odds = Column(Float, nullable=False)
    fair_odds = Column(Float, nullable=False)
    probability = Column(Float, nullable=False)
    recommended_amount = Column(Float, nullable=True)
    reason = Column(Text, nullable=True)
    channel_message_id = Column(BigInteger, nullable=True)
    event_time = Column(DateTime, nullable=True)
    published_at = Column(DateTime, nullable=True)
    result = Column(String(16), nullable=True)        # won/lost/void/pending
    created_at = Column(DateTime, default=datetime.utcnow)


# ── Engine и сессия ────────────────────────────────────────────

_engine = None
_session_factory = None


def _get_db_url(raw_url: str) -> str:
    """Конвертирует sync URL в async-совместимый формат."""
    if raw_url.startswith("postgresql://"):
        return raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if raw_url.startswith("postgres://"):
        # Heroku/Neon иногда дают postgres:// вместо postgresql://
        return raw_url.replace("postgres://", "postgresql+asyncpg://", 1)
    if raw_url.startswith("sqlite:///"):
        return raw_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return raw_url


async def init_db():
    """Инициализирует движок и создаёт таблицы если их нет."""
    global _engine, _session_factory

    from config import config
    db_url = _get_db_url(config.database_url)
    logger.info(f"Подключение к БД: {db_url.split('@')[-1] if '@' in db_url else db_url}")

    _engine = create_async_engine(
        db_url,
        echo=False,
        pool_pre_ping=True,
    )

    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("✅ Таблицы БД созданы/проверены")


def get_session() -> AsyncSession:
    """Возвращает новую сессию БД."""
    if _session_factory is None:
        raise RuntimeError("БД не инициализирована. Вызови init_db() при старте.")
    return _session_factory()