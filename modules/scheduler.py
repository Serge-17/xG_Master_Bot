from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import SessionLocal
from database.crud import get_user_summary, list_telegram_ids
from modules.daily_digest import (
    apply_bankroll_to_recommendations,
    build_daily_recommendations,
    format_channel_digest,
    format_user_digest,
)
from modules.retrospective import build_user_retrospective
from config import settings



scheduler = AsyncIOScheduler()


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def register_reporting_jobs(bot) -> None:
    scheduler.add_job(
        send_hourly_match_digest,
        trigger="cron",
        minute=settings.digest_post_minute,
        args=[bot, settings.digest_matches_limit],
        id="hourly_match_digest",
        replace_existing=True,
    )
    scheduler.add_job(
        send_scheduled_retrospectives,
        trigger="cron",
        day_of_week="mon",
        hour=9,
        minute=0,
        args=[bot],
        id="weekly_retro_reports",
        replace_existing=True,
    )
    scheduler.add_job(
        send_scheduled_retrospectives,
        trigger="cron",
        day="1",
        hour=9,
        minute=30,
        args=[bot],
        id="monthly_retro_reports",
        replace_existing=True,
    )


async def send_hourly_match_digest(bot, limit: int = 12) -> None:
    target_date = datetime.now().date()
    base_recommendations = build_daily_recommendations(target_date=target_date, limit=limit)

    channel_id = (settings.telegram_channel_id or "").strip()
    if channel_id:
        try:
            await bot.send_message(
                chat_id=channel_id,
                text=format_channel_digest(base_recommendations, target_date=target_date),
            )
        except Exception:
            pass

    with SessionLocal() as session:
        telegram_ids = list_telegram_ids(session)

    for telegram_id in telegram_ids:
        with SessionLocal() as session:
            summary = get_user_summary(session, telegram_id)
        personalized = apply_bankroll_to_recommendations(
            base_recommendations,
            bankroll=float(summary["bankroll"]),
            strategy=str(summary["bankroll_strategy"]),
            flat_percent=float(summary["flat_percent"]) / 100.0,
            kelly_cap=float(summary["kelly_fraction_limit"]) / 100.0,
        )
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=format_user_digest(personalized, summary, target_date=target_date),
            )
        except Exception:
            continue


async def send_scheduled_retrospectives(bot, limit: int = 30) -> None:
    with SessionLocal() as session:
        telegram_ids = list_telegram_ids(session)

    for telegram_id in telegram_ids:
        with SessionLocal() as session:
            report = build_user_retrospective(session, telegram_id, limit=limit)
        try:
            await bot.send_message(chat_id=telegram_id, text=report[:3900])
        except Exception:
            continue
