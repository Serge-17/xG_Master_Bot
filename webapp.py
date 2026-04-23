"""
webapp.py — FastAPI entry point для HuggingFace Space (port 7860).

HF Space требует слушать :7860. Мы запускаем uvicorn, а внутри lifespan:
 - инициализируем БД (Neon PostgreSQL),
 - стартуем Telegram-бота в режиме polling,
 - включаем планировщик ежедневного скана.

Webhook не используем — polling проще и не требует внешнего URL.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from telegram import Update

from bot import build_application
from config import config
from db import init_db
from scheduler import start_scheduler


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("xg_master")


@asynccontextmanager
async def lifespan(app: FastAPI):
    problems = config.validate()
    for p in problems:
        log.warning(p)
    log.info("Config:\n%s", config.summary())

    try:
        await init_db()
    except Exception as e:
        log.exception("init_db failed: %s", e)

    tg_app = None
    scheduler = None
    if not config.telegram_token:
        log.error("TELEGRAM_BOT_TOKEN пуст — бот не запущен.")
    else:
        tg_app = build_application(config.telegram_token)

        # Ретраим initialize() — при холодном старте HF Space сеть может
        # быть не готова, api.telegram.org по первому запросу таймаутит.
        last_error = None
        for attempt in range(1, 6):
            try:
                await tg_app.initialize()
                last_error = None
                break
            except Exception as e:
                last_error = e
                log.warning("tg_app.initialize() attempt %d/5 failed: %s",
                            attempt, e)
                await asyncio.sleep(min(5 * attempt, 20))
        if last_error is not None:
            log.error("Telegram init всё равно не удался: %s", last_error)
            tg_app = None
        else:
            await tg_app.start()
            try:
                await tg_app.bot.delete_webhook(drop_pending_updates=False)
            except Exception as e:
                log.warning("delete_webhook: %s", e)
            await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            scheduler = start_scheduler(tg_app.bot)
            log.info("🚀 xG Master Bot started (polling)")

    app.state.tg_app = tg_app
    app.state.scheduler = scheduler

    try:
        yield
    finally:
        log.info("Shutting down...")
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if tg_app is not None:
            try:
                await tg_app.updater.stop()
            except Exception:
                pass
            try:
                await tg_app.stop()
            except Exception:
                pass
            try:
                await tg_app.shutdown()
            except Exception:
                pass
        log.info("Shutdown complete.")


app = FastAPI(title="xG Master Bot", version="2.0.0", lifespan=lifespan)


@app.get("/")
async def root():
    running = app.state.tg_app is not None if hasattr(app.state, "tg_app") else False
    return {"status": "ok", "bot": "xG Master Bot", "version": "2.0.0", "running": running}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/config-check")
async def config_check():
    return {
        "summary": config.summary(),
        "problems": config.validate(),
    }


@app.get("/debug/net")
async def debug_net():
    """Диагностика: добиваемся ли мы до api.telegram.org разными способами."""
    import socket
    import aiohttp

    result = {}

    # 1. DNS
    try:
        result["dns_v4"] = socket.getaddrinfo(
            "api.telegram.org", 443, family=socket.AF_INET,
        )[0][4][0]
    except Exception as e:
        result["dns_v4_error"] = str(e)
    try:
        result["dns_v6"] = socket.getaddrinfo(
            "api.telegram.org", 443, family=socket.AF_INET6,
        )[0][4][0]
    except Exception as e:
        result["dns_v6_error"] = str(e)

    # 2. Raw HTTPS через aiohttp (IPv4)
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as s:
            async with s.get(f"https://api.telegram.org/bot{config.telegram_token}/getMe") as r:
                result["tg_getMe_status"] = r.status
                data = await r.json()
                result["tg_getMe_ok"] = bool(data.get("ok"))
                if data.get("ok"):
                    result["bot_username"] = data["result"].get("username")
    except Exception as e:
        result["tg_getMe_error"] = f"{type(e).__name__}: {e}"

    return result
