"""
scheduler.py — ежедневное сканирование + keep-alive пинг.

Render Free tier засыпает через 15 мин без HTTP-запросов.
_keepalive_job пингует собственный /health каждые 10 минут —
сервис не засыпает и всегда готов принять webhook от Telegram.
"""

from __future__ import annotations

import logging
import os

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Bot

from config import config
from db import get_bank
from scanner import scan_and_publish


log = logging.getLogger(__name__)


async def _daily_job(bot: Bot) -> None:
    log.info("=== Daily scan start ===")
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


async def _keepalive_job() -> None:
    """
    Пингует собственный /health чтобы Render не усыпил сервис.
    Без этого Free tier засыпает через 15 мин → первый webhook
    после сна обрабатывается с задержкой 20-50 сек или теряется.
    """
    webhook_url = os.getenv("WEBHOOK_URL", "").rstrip("/")
    if not webhook_url:
        return
    url = f"{webhook_url}/health"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                if r.status == 200:
                    log.debug("keep-alive: %s OK", url)
                else:
                    log.warning("keep-alive: %s → %s", url, r.status)
    except Exception as e:
        log.warning("keep-alive ping failed: %s", e)


def start_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")

    # Ежедневный скан матчей
    scheduler.add_job(
        _daily_job,
        trigger=CronTrigger(hour=config.daily_scan_hour, minute=0),
        args=[bot],
        id="daily_scan",
        replace_existing=True,
    )

    # Keep-alive каждые 10 минут (Render засыпает через 15)
    scheduler.add_job(
        _keepalive_job,
        trigger=IntervalTrigger(minutes=10),
        id="keepalive",
        replace_existing=True,
    )

    scheduler.start()
    log.info(
        "Scheduler started: daily scan @ %02d:00 UTC | keep-alive every 10 min",
        config.daily_scan_hour,
    )
    return scheduler
