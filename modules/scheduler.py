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
from config import settings

scheduler = AsyncIOScheduler()

def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()

def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)

def register_reporting_jobs(bot) -> None:
    #Hourly digest
    scheduler.add_job(
        send_hourly_match_digest,
        trigger="cron",
        minute=settings.digest_post_minute,
        args=[bot, settings.digest_matches_limit],
        id="hourly_match_digest",
        replace_existing=True,
    )

async def send_hourly_match_digest(bot, limit: int = 12) -> None:
    target_date = datetime.now().date()
    base_recommendations = build_daily_recommendations(target_date=target_date, limit=limit)
    
    # Отправка в канал
    channel_id = str(settings.telegram_channel_id)
    if channel_id and channel_id != "0":
        try:
            await bot.send_message(chat_id=channel_id, text=format_channel_digest(base_recommendations))
        except:
            pass