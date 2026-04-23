"""
db.py — PostgreSQL (Neon) / SQLite async схема xG Master Bot.

Таблицы:
  users      — банк + настройки пользователя (id = Telegram user_id)
  signals    — прогнозы модели (публикуются в канал)
  bets       — сделанные пользователем ставки (из скриншотов)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, Integer, String, Text,
    func, select, update,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from config import config


log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)           # telegram user_id
    username = Column(String(64))
    first_name = Column(String(128))
    bank = Column(Float, default=0.0)
    initial_bank = Column(Float, default=0.0)
    settings = Column(Text, default="{}")               # JSON
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class Signal(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match = Column(String(255), nullable=False)
    league = Column(String(64))
    kickoff = Column(DateTime)
    market = Column(String(64), default="1X2")          # 1X2 / totals / btts
    pick = Column(String(128), nullable=False)          # "Победа Реал" / "ТБ 2.5" ...
    book_odds = Column(Float, nullable=False)
    fair_odds = Column(Float, nullable=False)
    probability = Column(Float, nullable=False)         # 0..1
    confidence = Column(Integer, default=0)             # 0..100
    edge = Column(Float, default=0.0)                   # prob*book_odds - 1
    reasoning = Column(Text)
    risks = Column(Text)
    home_form = Column(String(32))
    away_form = Column(String(32))
    recommended_stake = Column(Float, default=0.0)
    channel_message_id = Column(BigInteger)
    status = Column(String(16), default="pending")      # pending/win/loss/void
    created_at = Column(DateTime, default=utcnow)


class Bet(Base):
    __tablename__ = "bets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    signal_id = Column(Integer)                         # опциональная связь с сигналом
    match = Column(String(255))
    pick = Column(String(128))
    league = Column(String(64))
    odds = Column(Float, nullable=False)
    stake = Column(Float, nullable=False)
    status = Column(String(16), default="pending")      # pending/win/loss/void
    profit = Column(Float, default=0.0)
    bank_before = Column(Float)
    bank_after = Column(Float)
    source = Column(String(32), default="manual")       # manual / screenshot
    note = Column(Text)
    created_at = Column(DateTime, default=utcnow)
    closed_at = Column(DateTime)


# ────────────────────────────────────────────────────────────────
# Engine / session
# ────────────────────────────────────────────────────────────────
_engine: Optional[AsyncEngine] = None
_SessionMaker: Optional[async_sessionmaker[AsyncSession]] = None


async def init_db() -> None:
    global _engine, _SessionMaker
    url = config.database_url
    safe_url = url.split("@", 1)[1] if "@" in url else url
    log.info("DB connect → %s", safe_url)

    _engine = create_async_engine(url, pool_pre_ping=True, pool_recycle=300)
    _SessionMaker = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("DB schema ready")


def session() -> AsyncSession:
    if _SessionMaker is None:
        raise RuntimeError("init_db() не был вызван")
    return _SessionMaker()


# ────────────────────────────────────────────────────────────────
# CRUD: users
# ────────────────────────────────────────────────────────────────
async def ensure_user(user_id: int, username: str = "", first_name: str = "") -> User:
    async with session() as s:
        u = await s.get(User, user_id)
        if u is None:
            u = User(id=user_id, username=username, first_name=first_name, bank=0.0)
            s.add(u)
            await s.commit()
            await s.refresh(u)
        return u


async def get_bank(user_id: int) -> float:
    async with session() as s:
        u = await s.get(User, user_id)
        return float(u.bank) if u else 0.0


async def set_bank(user_id: int, amount: float) -> None:
    amount = round(max(0.0, amount), 2)
    async with session() as s:
        u = await s.get(User, user_id)
        if u is None:
            u = User(id=user_id, bank=amount, initial_bank=amount)
            s.add(u)
        else:
            if not u.initial_bank:
                u.initial_bank = amount
            u.bank = amount
            u.updated_at = utcnow()
        await s.commit()


async def adjust_bank(user_id: int, delta: float) -> float:
    async with session() as s:
        u = await s.get(User, user_id)
        if u is None:
            u = User(id=user_id, bank=max(0.0, delta))
            s.add(u)
            await s.commit()
            return u.bank
        u.bank = round(max(0.0, float(u.bank) + delta), 2)
        u.updated_at = utcnow()
        await s.commit()
        return float(u.bank)


async def get_settings(user_id: int) -> dict:
    async with session() as s:
        u = await s.get(User, user_id)
        if not u or not u.settings:
            return {}
        try:
            return json.loads(u.settings)
        except Exception:
            return {}


async def update_settings(user_id: int, patch: dict) -> dict:
    async with session() as s:
        u = await s.get(User, user_id)
        if u is None:
            u = User(id=user_id, settings=json.dumps(patch))
            s.add(u)
            await s.commit()
            return patch
        data = {}
        try:
            data = json.loads(u.settings or "{}")
        except Exception:
            data = {}
        data.update(patch)
        u.settings = json.dumps(data)
        u.updated_at = utcnow()
        await s.commit()
        return data


# ────────────────────────────────────────────────────────────────
# CRUD: signals
# ────────────────────────────────────────────────────────────────
async def save_signal(**kwargs) -> int:
    async with session() as s:
        sig = Signal(**kwargs)
        s.add(sig)
        await s.commit()
        await s.refresh(sig)
        return sig.id


async def get_signal(signal_id: int) -> Optional[Signal]:
    async with session() as s:
        return await s.get(Signal, signal_id)


async def set_signal_message_id(signal_id: int, message_id: int) -> None:
    async with session() as s:
        await s.execute(
            update(Signal).where(Signal.id == signal_id).values(channel_message_id=message_id)
        )
        await s.commit()


async def list_signals(limit: int = 10) -> list[Signal]:
    async with session() as s:
        res = await s.execute(
            select(Signal).order_by(Signal.created_at.desc()).limit(limit)
        )
        return list(res.scalars().all())


async def list_todays_signals() -> list[Signal]:
    async with session() as s:
        today = utcnow().date()
        res = await s.execute(
            select(Signal).where(func.date(Signal.created_at) == today)
            .order_by(Signal.created_at.desc())
        )
        return list(res.scalars().all())


async def find_signal_by_match(query: str, limit: int = 10) -> list[Signal]:
    q = f"%{query.lower()}%"
    async with session() as s:
        res = await s.execute(
            select(Signal).where(func.lower(Signal.match).like(q))
            .order_by(Signal.created_at.desc()).limit(limit)
        )
        return list(res.scalars().all())


# ────────────────────────────────────────────────────────────────
# CRUD: bets
# ────────────────────────────────────────────────────────────────
async def add_bet(
    user_id: int, match: str, pick: str, odds: float, stake: float,
    league: str = "", signal_id: Optional[int] = None, source: str = "manual",
    note: str = "",
) -> int:
    """Создаёт ставку и СРАЗУ списывает stake из банка (деньги «в игре»)."""
    stake = round(stake, 2)
    odds = round(odds, 3)
    async with session() as s:
        user = await s.get(User, user_id)
        if user is None:
            user = User(id=user_id, bank=0.0)
            s.add(user)
            await s.flush()
        bank_before = float(user.bank)
        user.bank = round(max(0.0, bank_before - stake), 2)
        user.updated_at = utcnow()
        b = Bet(
            user_id=user_id, signal_id=signal_id, match=match, pick=pick,
            league=league, odds=odds, stake=stake,
            status="pending", source=source, note=note,
            bank_before=bank_before, bank_after=user.bank,
        )
        s.add(b)
        await s.commit()
        await s.refresh(b)
        return b.id


async def close_bet(bet_id: int, result: str) -> Optional[Bet]:
    """result ∈ {'win','loss','void'}. Возвращает обновлённый Bet."""
    assert result in {"win", "loss", "void"}
    async with session() as s:
        b = await s.get(Bet, bet_id)
        if b is None or b.status != "pending":
            return b

        user = await s.get(User, b.user_id)
        if user is None:
            user = User(id=b.user_id, bank=0.0)
            s.add(user)

        # stake уже списан при add_bet. Нужно вернуть на банк:
        #   win  → stake * odds (вся выплата)
        #   loss → 0
        #   void → stake (возврат)
        if result == "win":
            payout = round(b.stake * b.odds, 2)
            profit = round(payout - b.stake, 2)
        elif result == "void":
            payout = round(b.stake, 2)
            profit = 0.0
        else:
            payout = 0.0
            profit = round(-b.stake, 2)

        user.bank = round(float(user.bank) + payout, 2)
        user.updated_at = utcnow()

        b.status = result
        b.profit = profit
        b.closed_at = utcnow()
        b.bank_after = user.bank
        await s.commit()
        await s.refresh(b)
        return b


async def list_bets(user_id: int, limit: int = 20) -> list[Bet]:
    async with session() as s:
        res = await s.execute(
            select(Bet).where(Bet.user_id == user_id)
            .order_by(Bet.created_at.desc()).limit(limit)
        )
        return list(res.scalars().all())


async def stats_for_user(user_id: int) -> dict:
    async with session() as s:
        user = await s.get(User, user_id)
        bank = float(user.bank) if user else 0.0
        rows = (await s.execute(
            select(Bet.status, func.count(), func.sum(Bet.stake), func.sum(Bet.profit), func.avg(Bet.odds))
            .where(Bet.user_id == user_id, Bet.status != "pending")
            .group_by(Bet.status)
        )).all()

    tally = {"win": [0, 0.0, 0.0, 0.0], "loss": [0, 0.0, 0.0, 0.0], "void": [0, 0.0, 0.0, 0.0]}
    for status, cnt, staked, profit, avg_odds in rows:
        tally[status] = [cnt or 0, staked or 0.0, profit or 0.0, avg_odds or 0.0]

    total = tally["win"][0] + tally["loss"][0] + tally["void"][0]
    staked = tally["win"][1] + tally["loss"][1] + tally["void"][1]
    profit = tally["win"][2] + tally["loss"][2]     # void не влияет
    decided = tally["win"][0] + tally["loss"][0]
    avg_odds = (
        (tally["win"][0] * tally["win"][3] + tally["loss"][0] * tally["loss"][3]) / decided
    ) if decided else 0.0
    return {
        "bank": round(bank, 2),
        "total": total,
        "wins": tally["win"][0],
        "losses": tally["loss"][0],
        "voids": tally["void"][0],
        "win_rate": round(tally["win"][0] / decided * 100, 1) if decided else 0.0,
        "roi": round(profit / staked * 100, 1) if staked else 0.0,
        "profit": round(profit, 2),
        "avg_odds": round(avg_odds, 2),
        "avg_stake": round(staked / total, 2) if total else 0.0,
    }


async def retro_report(user_id: int, limit: int = 20) -> list[Bet]:
    """Последние закрытые ставки для ретро-отчёта."""
    async with session() as s:
        res = await s.execute(
            select(Bet).where(Bet.user_id == user_id, Bet.status != "pending")
            .order_by(Bet.closed_at.desc().nullslast()).limit(limit)
        )
        return list(res.scalars().all())
