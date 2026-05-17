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
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, Float, Integer, String, Text,
    func, inspect, select, update,
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
    market_probability = Column(Float, default=0.0)     # fair prob рынка после снятия маржи
    confidence = Column(Integer, default=0)             # 0..100
    edge = Column(Float, default=0.0)                   # prob*book_odds - 1
    reasoning = Column(Text)
    risks = Column(Text)
    home_form = Column(String(32))
    away_form = Column(String(32))
    recommended_stake = Column(Float, default=0.0)
    channel_message_id = Column(BigInteger)
    status = Column(String(16), default="pending")      # pending/win/loss/void/needs_review
    created_at = Column(DateTime, default=utcnow)

    # Settlement (Фаза 1: bet results reporting)
    sport_key = Column(String(64))                      # odds-api sport key для скоринга
    result_score = Column(String(16))                   # "2:1" — для UI
    home_goals = Column(Integer)
    away_goals = Column(Integer)
    settled_at = Column(DateTime)
    settle_source = Column(String(32))                  # "odds-api" / "football-data" / "manual"
    pnl_units = Column(Float, default=0.0)              # P&L в долях ставки (1u = размер ставки)
    closing_odds = Column(Float)                        # коэф на старте — для CLV


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


class CachedMatch(Base):
    __tablename__ = "cached_matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_key = Column(String(255), unique=True, nullable=False, index=True)
    external_id = Column(String(64))
    match = Column(String(255), nullable=False)
    home = Column(String(128), nullable=False)
    away = Column(String(128), nullable=False)
    league = Column(String(128))
    kickoff = Column(DateTime)
    source = Column(String(32), default="api")
    source_url = Column(Text)
    facts = Column(Text, default="[]")
    stats = Column(Text, default="[]")
    injuries = Column(Text, default="[]")
    home_form = Column(String(32), default="— — — — —")
    away_form = Column(String(32), default="— — — — —")
    home_summary = Column(Text)
    away_summary = Column(Text)
    raw_payload = Column(Text, default="{}")
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    created_at = Column(DateTime, default=utcnow)


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_key = Column(String(255), nullable=False, index=True)
    match = Column(String(255), nullable=False)
    bookmaker = Column(String(128), default="")
    source = Column(String(32), default="api")
    source_url = Column(Text)
    home = Column(Float, default=0.0)
    draw = Column(Float, default=0.0)
    away = Column(Float, default=0.0)
    over_2_5 = Column(Float, default=0.0)
    under_2_5 = Column(Float, default=0.0)
    btts_yes = Column(Float, default=0.0)
    btts_no = Column(Float, default=0.0)
    fetched_at = Column(DateTime, default=utcnow, index=True)


# ────────────────────────────────────────────────────────────────
# Engine / session
# ────────────────────────────────────────────────────────────────
_engine: Optional[AsyncEngine] = None
_SessionMaker: Optional[async_sessionmaker[AsyncSession]] = None


# Колонки, которые могли отсутствовать в существующей БД (новые фичи).
# create_all только создаёт таблицы, не апдейтит существующие — поэтому
# для добавленных колонок делаем idempotent ALTER TABLE.
_SIGNAL_NEW_COLUMNS: dict[str, str] = {
    "market_probability": "FLOAT DEFAULT 0",
    "sport_key": "VARCHAR(64)",
    "result_score": "VARCHAR(16)",
    "home_goals": "INTEGER",
    "away_goals": "INTEGER",
    "settled_at": "TIMESTAMP",
    "settle_source": "VARCHAR(32)",
    "pnl_units": "FLOAT DEFAULT 0",
    "closing_odds": "FLOAT",
}


async def _migrate_signal_schema() -> None:
    """Добавляем недостающие колонки в signals для существующих БД."""
    if _engine is None:
        return
    async with _engine.begin() as conn:
        def _existing_columns(sync_conn):
            insp = inspect(sync_conn)
            try:
                return {col["name"] for col in insp.get_columns("signals")}
            except Exception:
                return set()
        existing = await conn.run_sync(_existing_columns)
        for name, ddl in _SIGNAL_NEW_COLUMNS.items():
            if name in existing:
                continue
            try:
                await conn.exec_driver_sql(f"ALTER TABLE signals ADD COLUMN {name} {ddl}")
                log.info("Migration: signals.%s added", name)
            except Exception as e:
                log.warning("Migration: skip signals.%s: %s", name, e)


async def init_db() -> None:
    global _engine, _SessionMaker
    url = config.database_url
    safe_url = url.split("@", 1)[1] if "@" in url else url
    log.info("DB connect → %s", safe_url)

    _engine = create_async_engine(url, pool_pre_ping=True, pool_recycle=300)
    _SessionMaker = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _migrate_signal_schema()
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


async def find_recent_signal(
    match: str,
    market: str,
    pick: str,
    max_age_hours: int = 96,
) -> Optional[Signal]:
    """Ищем свежий дубликат сигнала, чтобы канал не усиливал одну экспозицию."""
    cutoff_dt = datetime.fromtimestamp(utcnow().timestamp() - max_age_hours * 3600)
    async with session() as s:
        res = await s.execute(
            select(Signal)
            .where(
                func.lower(Signal.match) == match.lower(),
                func.lower(Signal.market) == market.lower(),
                func.lower(Signal.pick) == pick.lower(),
                Signal.created_at >= cutoff_dt,
            )
            .order_by(Signal.created_at.desc())
            .limit(1)
        )
        return res.scalar_one_or_none()


async def upsert_cached_match(
    *,
    match_key: str,
    match: str,
    home: str,
    away: str,
    league: str = "",
    kickoff: Optional[datetime] = None,
    external_id: str = "",
    source: str = "api",
    source_url: str = "",
    facts: Optional[list[str]] = None,
    stats: Optional[list[str]] = None,
    injuries: Optional[list[str]] = None,
    home_form: str = "— — — — —",
    away_form: str = "— — — — —",
    home_summary: str = "",
    away_summary: str = "",
    raw_payload: Optional[dict] = None,
) -> int:
    async with session() as s:
        res = await s.execute(select(CachedMatch).where(CachedMatch.match_key == match_key))
        row = res.scalar_one_or_none()
        values = dict(
            external_id=external_id,
            match=match,
            home=home,
            away=away,
            league=league,
            kickoff=kickoff,
            source=source,
            source_url=source_url,
            facts=json.dumps(facts or [], ensure_ascii=False),
            stats=json.dumps(stats or [], ensure_ascii=False),
            injuries=json.dumps(injuries or [], ensure_ascii=False),
            home_form=home_form,
            away_form=away_form,
            home_summary=home_summary,
            away_summary=away_summary,
            raw_payload=json.dumps(raw_payload or {}, ensure_ascii=False),
            updated_at=utcnow(),
        )
        if row is None:
            row = CachedMatch(match_key=match_key, **values)
            s.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await s.commit()
        await s.refresh(row)
        return row.id


async def get_cached_match(match_key: str) -> Optional[CachedMatch]:
    async with session() as s:
        res = await s.execute(select(CachedMatch).where(CachedMatch.match_key == match_key))
        return res.scalar_one_or_none()


async def list_cached_matches_for_date(day: date) -> list[CachedMatch]:
    async with session() as s:
        res = await s.execute(
            select(CachedMatch)
            .where(func.date(CachedMatch.kickoff) == day)
            .order_by(CachedMatch.kickoff.asc().nulls_last())
        )
        return list(res.scalars().all())


async def save_odds_snapshot(
    *,
    match_key: str,
    match: str,
    bookmaker: str = "",
    source: str = "api",
    source_url: str = "",
    home: float = 0.0,
    draw: float = 0.0,
    away: float = 0.0,
    over_2_5: float = 0.0,
    under_2_5: float = 0.0,
    btts_yes: float = 0.0,
    btts_no: float = 0.0,
) -> int:
    async with session() as s:
        snap = OddsSnapshot(
            match_key=match_key,
            match=match,
            bookmaker=bookmaker,
            source=source,
            source_url=source_url,
            home=home,
            draw=draw,
            away=away,
            over_2_5=over_2_5,
            under_2_5=under_2_5,
            btts_yes=btts_yes,
            btts_no=btts_no,
        )
        s.add(snap)
        await s.commit()
        await s.refresh(snap)
        return snap.id


async def get_latest_odds_snapshot(match_key: str, max_age_hours: int = 8) -> Optional[OddsSnapshot]:
    cutoff_dt = datetime.fromtimestamp(utcnow().timestamp() - max_age_hours * 3600)
    async with session() as s:
        res = await s.execute(
            select(OddsSnapshot)
            .where(OddsSnapshot.match_key == match_key, OddsSnapshot.fetched_at >= cutoff_dt)
            .order_by(OddsSnapshot.fetched_at.desc())
            .limit(1)
        )
        return res.scalar_one_or_none()


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


# ────────────────────────────────────────────────────────────────
# CRUD: settlement & reporting
# ────────────────────────────────────────────────────────────────
async def list_signals_to_settle(grace_hours: int = 2,
                                  max_age_days: int = 14) -> list[Signal]:
    """
    Pending-сигналы готовы к settle когда:
      - kickoff < now - grace_hours (минимум 2ч после старта = матч закончился)
      - kickoff > now - max_age_days (отсекаем legacy без kickoff)
    """
    now = utcnow()
    earliest = now - timedelta(days=max_age_days)
    latest = now - timedelta(hours=grace_hours)
    async with session() as s:
        res = await s.execute(
            select(Signal)
            .where(
                Signal.status == "pending",
                Signal.kickoff.isnot(None),
                Signal.kickoff >= earliest,
                Signal.kickoff <= latest,
            )
            .order_by(Signal.kickoff.asc())
        )
        return list(res.scalars().all())


async def list_signals_kicking_soon(window_min: int = 30) -> list[Signal]:
    """Сигналы чей старт через ≤window_min минут — для CLV-снапшотов."""
    now = utcnow()
    horizon = now + timedelta(minutes=window_min)
    async with session() as s:
        res = await s.execute(
            select(Signal)
            .where(
                Signal.status == "pending",
                Signal.closing_odds.is_(None),
                Signal.kickoff.isnot(None),
                Signal.kickoff > now,
                Signal.kickoff <= horizon,
            )
        )
        return list(res.scalars().all())


async def list_orphan_pending_signals(stale_days: int = 7) -> list[Signal]:
    """Pending старше N дней без счёта — кандидаты на void."""
    cutoff = utcnow() - timedelta(days=stale_days)
    async with session() as s:
        res = await s.execute(
            select(Signal).where(
                Signal.status == "pending",
                Signal.kickoff.isnot(None),
                Signal.kickoff <= cutoff,
            )
        )
        return list(res.scalars().all())


async def update_signal_settlement(
    signal_id: int,
    *,
    status: str,
    home_goals: Optional[int],
    away_goals: Optional[int],
    pnl_units: float,
    settle_source: str,
) -> None:
    score = (
        f"{home_goals}:{away_goals}"
        if home_goals is not None and away_goals is not None else None
    )
    async with session() as s:
        await s.execute(
            update(Signal)
            .where(Signal.id == signal_id)
            .values(
                status=status,
                home_goals=home_goals,
                away_goals=away_goals,
                result_score=score,
                pnl_units=round(pnl_units, 4),
                settle_source=settle_source,
                settled_at=utcnow(),
            )
        )
        await s.commit()


async def set_signal_closing_odds(signal_id: int, closing_odds: float) -> None:
    async with session() as s:
        await s.execute(
            update(Signal)
            .where(Signal.id == signal_id, Signal.closing_odds.is_(None))
            .values(closing_odds=round(closing_odds, 3))
        )
        await s.commit()


def _msk_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Границы суток МСК в naive UTC (для SQL-сравнения с Signal.kickoff)."""
    msk = timezone(timedelta(hours=3))
    start_msk = datetime.combine(day, datetime.min.time(), tzinfo=msk)
    end_msk = start_msk + timedelta(days=1)
    return (
        start_msk.astimezone(timezone.utc).replace(tzinfo=None),
        end_msk.astimezone(timezone.utc).replace(tzinfo=None),
    )


async def settled_signals_for_msk_date(day: date) -> list[Signal]:
    start, end = _msk_day_bounds(day)
    async with session() as s:
        res = await s.execute(
            select(Signal)
            .where(
                Signal.kickoff >= start,
                Signal.kickoff < end,
                Signal.status.in_(["win", "loss", "void"]),
            )
            .order_by(Signal.kickoff.asc())
        )
        return list(res.scalars().all())


async def settled_signals_in_range(days: int) -> list[Signal]:
    """Settled signals за последние N дней по МСК (include today's settled)."""
    msk = timezone(timedelta(hours=3))
    today_msk = datetime.now(msk).date()
    start_day = today_msk - timedelta(days=days - 1)
    start, _ = _msk_day_bounds(start_day)
    async with session() as s:
        res = await s.execute(
            select(Signal)
            .where(
                Signal.kickoff >= start,
                Signal.status.in_(["win", "loss", "void"]),
            )
            .order_by(Signal.kickoff.asc())
        )
        return list(res.scalars().all())


def aggregate_signal_stats(signals: list["Signal"]) -> dict:
    """Метрики по списку settled сигналов."""
    total = len(signals)
    wins = sum(1 for s in signals if s.status == "win")
    losses = sum(1 for s in signals if s.status == "loss")
    voids = sum(1 for s in signals if s.status == "void")
    decided = wins + losses
    pnl = sum(float(s.pnl_units or 0) for s in signals)
    staked_units = decided  # каждая ставка = 1u номинала, void не считаем
    roi = (pnl / staked_units * 100) if staked_units else 0.0
    win_rate = (wins / decided * 100) if decided else 0.0
    avg_odds = (
        sum(float(s.book_odds or 0) for s in signals if s.status in ("win", "loss"))
        / decided
    ) if decided else 0.0

    # CLV: среднее (book_odds / closing_odds - 1) * 100% по сигналам с closing_odds
    clv_pairs = [
        (float(s.book_odds), float(s.closing_odds))
        for s in signals
        if s.book_odds and s.closing_odds and s.closing_odds > 1.01
    ]
    if clv_pairs:
        clv_pct = sum((b / c - 1) * 100 for b, c in clv_pairs) / len(clv_pairs)
    else:
        clv_pct = 0.0

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "decided": decided,
        "pnl_units": round(pnl, 3),
        "roi_pct": round(roi, 2),
        "win_rate_pct": round(win_rate, 1),
        "avg_odds": round(avg_odds, 2),
        "clv_pct": round(clv_pct, 2),
        "clv_sample": len(clv_pairs),
    }


async def aggregate_signal_breakdown(days: int, group_by: str) -> list[dict]:
    """
    group_by ∈ {"market","league"}: возвращает список словарей с метриками.
    Используется для разделов «По рынкам» / «По лигам» в ретро-отчёте.
    """
    sigs = await settled_signals_in_range(days)
    if group_by not in ("market", "league"):
        raise ValueError(f"unknown group_by: {group_by}")

    buckets: dict[str, list[Signal]] = {}
    for s in sigs:
        key = (s.market if group_by == "market" else (s.league or "—")) or "—"
        buckets.setdefault(key, []).append(s)

    rows = []
    for key, items in buckets.items():
        agg = aggregate_signal_stats(items)
        rows.append({"key": key, **agg})
    rows.sort(key=lambda r: (r["pnl_units"], r["total"]), reverse=True)
    return rows
