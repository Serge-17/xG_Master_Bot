"""
webapp.py — FastAPI entry point.

Режим определяется автоматически:
  WEBHOOK_URL задан  → Webhook (Railway / VPS / любой HTTPS-хост)
  WEBHOOK_URL пуст   → Polling  (локальная разработка, ngrok)

Railway: после первого деплоя Railway даст URL вида
  https://xg-master-bot-production.up.railway.app
Добавь его как переменную WEBHOOK_URL в Railway Variables.
"""

from __future__ import annotations

import socket as _socket
_orig_gai = _socket.getaddrinfo
def _ipv4_only(host, port, family=0, *a, **kw):
    if family in (0, _socket.AF_UNSPEC):
        family = _socket.AF_INET
    return _orig_gai(host, port, family, *a, **kw)
_socket.getaddrinfo = _ipv4_only

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from telegram import Update
from telegram.ext import Application

from bot import build_application
from config import config
from db import init_db
from scheduler import start_scheduler

# ── Логирование ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
# Скрываем токен из httpx-логов
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("xg_master")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
USE_WEBHOOK = bool(WEBHOOK_URL)

# Путь для webhook — ID бота вместо полного токена (безопаснее)
_BOT_ID = config.telegram_token.split(":")[0] if config.telegram_token else "bot"
WEBHOOK_PATH = f"/webhook/{_BOT_ID}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Конфиг ────────────────────────────────────────────────────
    for p in config.validate():
        log.warning("Config: %s", p)
    log.info("Config:\n%s", config.summary())
    log.info("Mode: %s", "WEBHOOK → " + WEBHOOK_URL if USE_WEBHOOK else "POLLING (local)")

    # ── БД ────────────────────────────────────────────────────────
    try:
        await init_db()
    except Exception as e:
        log.exception("init_db failed: %s", e)

    tg_app: Application | None = None
    scheduler = None

    if not config.telegram_token:
        log.error("TELEGRAM_BOT_TOKEN не задан — бот не запущен")
    else:
        tg_app = build_application(config.telegram_token)

        # Retry initialize (Railway cold start может быть медленным)
        for attempt in range(1, 6):
            try:
                await tg_app.initialize()
                break
            except Exception as e:
                log.warning("initialize attempt %d/5: %s", attempt, e)
                await asyncio.sleep(min(5 * attempt, 20))
        else:
            log.error("Telegram init не удался — бот не запущен")
            tg_app = None

        if tg_app:
            await tg_app.start()

            if USE_WEBHOOK:
                # ── WEBHOOK режим ─────────────────────────────────
                full_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
                try:
                    await tg_app.bot.set_webhook(
                        url=full_url,
                        drop_pending_updates=True,
                        allowed_updates=Update.ALL_TYPES,
                    )
                    log.info("✅ Webhook: %s", full_url)
                except Exception as e:
                    log.error("set_webhook failed: %s", e)
            else:
                # ── POLLING режим (локально) ──────────────────────
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
                        log.info("✅ Polling запущен (attempt %d)", attempt)
                        break
                    except Exception as e:
                        log.warning("start_polling attempt %d/3: %s", attempt, e)
                        if attempt < 3:
                            await asyncio.sleep(5)

            scheduler = start_scheduler(tg_app.bot)
            log.info("🚀 xG Master Bot запущен (%s)",
                     "webhook" if USE_WEBHOOK else "polling")

    app.state.tg_app = tg_app
    app.state.scheduler = scheduler

    try:
        yield
    finally:
        log.info("Завершаю работу...")
        if scheduler:
            scheduler.shutdown(wait=False)
        if tg_app:
            if USE_WEBHOOK:
                try:
                    await tg_app.bot.delete_webhook()
                except Exception:
                    pass
            else:
                try:
                    await tg_app.updater.stop()
                except Exception:
                    pass
            for fn in (tg_app.stop, tg_app.shutdown):
                try:
                    await fn()
                except Exception:
                    pass
        log.info("Остановлен.")


app = FastAPI(title="xG Master Bot", version="2.2.0", lifespan=lifespan)


# ── Webhook endpoint (только в webhook-режиме) ────────────────────
@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    tg_app = getattr(app.state, "tg_app", None)
    if not tg_app:
        return Response(status_code=503)
    try:
        update = Update.de_json(await request.json(), tg_app.bot)
        await tg_app.process_update(update)
    except Exception as e:
        log.exception("webhook error: %s", e)
        return Response(status_code=500)
    return Response(status_code=200)


# ── Health / info ────────────────────────────────────────────────
@app.get("/")
@app.head("/")
async def root():
    return {
        "status": "ok",
        "bot": "xG Master Bot",
        "version": "2.2.0",
        "mode": "webhook" if USE_WEBHOOK else "polling",
        "running": getattr(app.state, "tg_app", None) is not None,
    }


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/config-check")
async def config_check():
    return {"problems": config.validate(), "mode": "webhook" if USE_WEBHOOK else "polling"}
