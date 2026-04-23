"""
scheduler.py — ежедневный автозапуск сканирования и публикации «Матча дня».
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot

from config import config
from db import get_bank
from scanner import scan_and_publish


log = logging.getLogger(__name__)


async def _daily_job(bot: Bot) -> None:
    log.info("=== Daily scan start ===")
    # Банк для ставок из Kelly: админский, если задан, иначе дефолт.
    bank = 10000.0
    if config.admin_id:
        try:
            b = await get_bank(config.admin_id)
            if b > 0:
                bank = b
        except Exception:
            pass
    try:
        published = await scan_and_publish(bot, bank)
        log.info("Daily scan: опубликовано %d сигналов", published)
    except Exception as e:
        log.exception("Daily scan failed: %s", e)


def start_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _daily_job, trigger=CronTrigger(hour=config.daily_scan_hour, minute=0),
        args=[bot], id="daily_scan", replace_existing=True,
    )
    scheduler.start()
    log.info("Scheduler started: daily scan @ %02d:00 UTC", config.daily_scan_hour)
    return scheduler
