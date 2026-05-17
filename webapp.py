"""
webapp.py — FastAPI entry point.

АРХИТЕКТУРНОЕ ИСПРАВЛЕНИЕ:
Все тяжёлые операции (DB, Telegram init, scheduler) запускаются
в фоновом asyncio.Task ПОСЛЕ того как uvicorn поднялся и /health
начал отвечать 200 OK. Railway Healthcheck проходит мгновенно.
"""

from __future__ import annotations

import socket as _socket
_orig = _socket.getaddrinfo
def _v4(h, p, family=0, *a, **kw):
    if family in (0, _socket.AF_UNSPEC):
        family = _socket.AF_INET
    return _orig(h, p, family, *a, **kw)
_socket.getaddrinfo = _v4

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from telegram import Update

from config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("xg_master")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
USE_WEBHOOK = bool(WEBHOOK_URL)
_BOT_ID = (config.telegram_token or "0").split(":")[0]
WEBHOOK_PATH = f"/webhook/{_BOT_ID}"

# Глобальные объекты — заполняются в фоне
_tg_app = None
_scheduler = None
_ready = False   # True когда бот полностью инициализирован
_init_errors: list[str] = []


async def _init_bot():
    """Запускается в фоне — не блокирует uvicorn и healthcheck."""
    global _tg_app, _scheduler, _ready

    log.info("Фоновая инициализация бота...")
    _init_errors.clear()

    # Маленькая пауза чтобы uvicorn успел поднять сервер
    await asyncio.sleep(2)

    for p in config.validate():
        log.warning("Config: %s", p)
    log.info("Config:\n%s", config.summary())

    # БД
    try:
        from db import init_db
        await init_db()
        log.info("DB готова")
    except Exception as e:
        log.error("init_db: %s", e)
        _init_errors.append(f"db: {e}")
        return

    # Прогрев кэша коэффициентов
    try:
        from data_sources import warm_odds_cache
        from scanner import warmup_match_cache
        await warm_odds_cache()
        await warmup_match_cache(limit=24)
    except Exception as e:
        log.warning("warm caches: %s", e)

    if not config.telegram_token:
        log.error("TELEGRAM_BOT_TOKEN не задан")
        _init_errors.append("telegram token missing")
        return

    # Telegram
    from bot import build_application
    proxy = os.getenv("TG_PROXY", "")
    tg_app = build_application(config.telegram_token, proxy=proxy)

    for attempt in range(1, 6):
        try:
            await tg_app.initialize()
            break
        except Exception as e:
            log.warning("initialize attempt %d/5: %s", attempt, e)
            await asyncio.sleep(min(5 * attempt, 20))
    else:
        log.error("Telegram init не удался")
        _init_errors.append("telegram initialize failed")
        return

    await tg_app.start()

    telegram_ready = False
    if USE_WEBHOOK:
        full_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
        try:
            await tg_app.bot.set_webhook(
                url=full_url,
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )
            log.info("✅ Webhook: %s", full_url)
            telegram_ready = True
        except Exception as e:
            log.error("set_webhook: %s", e)
            _init_errors.append(f"set_webhook: {e}")
    else:
        try:
            await tg_app.bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        for attempt in range(1, 4):
            try:
                await tg_app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                    bootstrap_retries=3,
                )
                log.info("✅ Polling запущен")
                telegram_ready = True
                break
            except Exception as e:
                log.warning("polling attempt %d: %s", attempt, e)
                await asyncio.sleep(5)
        if not telegram_ready:
            _init_errors.append("polling failed")

    if not telegram_ready:
        try:
            await tg_app.stop()
            await tg_app.shutdown()
        except Exception:
            pass
        return

    from scheduler import start_scheduler
    scheduler = start_scheduler(tg_app.bot)

    _tg_app = tg_app
    _scheduler = scheduler
    _ready = True
    log.info("🚀 xG Master Bot полностью запущен (%s)",
             "webhook" if USE_WEBHOOK else "polling")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Запускаем инициализацию в фоне — НЕ ждём её завершения
    task = asyncio.create_task(_init_bot())
    app.state.init_task = task

    yield  # ← uvicorn сразу начинает принимать запросы, /health = 200

    # Shutdown
    log.info("Завершаю работу...")
    task.cancel()
    if _scheduler:
        _scheduler.shutdown(wait=False)
    if _tg_app:
        if USE_WEBHOOK:
            try:
                await _tg_app.bot.delete_webhook()
            except Exception:
                pass
        else:
            try:
                await _tg_app.updater.stop()
            except Exception:
                pass
        for fn in (_tg_app.stop, _tg_app.shutdown):
            try:
                await fn()
            except Exception:
                pass


app = FastAPI(title="xG Master Bot", version="2.3.0", lifespan=lifespan)


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    if not _tg_app:
        return Response(status_code=503)
    try:
        update = Update.de_json(await request.json(), _tg_app.bot)
        await _tg_app.process_update(update)
    except Exception as e:
        log.exception("webhook error: %s", e)
        return Response(status_code=500)
    return Response(status_code=200)


@app.get("/")
@app.head("/")
async def root():
    return {
        "status": "ok",
        "bot_ready": _ready,
        "mode": "webhook" if USE_WEBHOOK else "polling",
        "init_errors": _init_errors[-5:],
    }


@app.get("/health")
async def health():
    # Всегда 200 — Railway Healthcheck проходит сразу
    return {"status": "healthy", "bot_ready": _ready, "init_errors": _init_errors[-5:]}
