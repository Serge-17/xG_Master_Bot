from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..database import SessionLocal
from ..database.crud import list_telegram_ids
from .retrospective import build_user_retrospective


scheduler = AsyncIOScheduler()


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def register_reporting_jobs(bot) -> None:
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
