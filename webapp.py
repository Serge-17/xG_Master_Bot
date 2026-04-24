"""
webapp.py — FastAPI entry point (Render Web Service).

Режим: WEBHOOK (не polling).
Telegram сам пушит апдейты на наш HTTPS URL — никакого getUpdates,
никакого Conflict при деплое двух инстансов.

Обязательная env-переменная:
  WEBHOOK_URL — публичный HTTPS-URL сервиса, например:
                https://xg-master-bot.onrender.com
"""

from __future__ import annotations

# ── IPv4-only egress ──────────────────────────────────────────────
import socket as _socket

_original_getaddrinfo = _socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, *args, **kwargs):
    if family in (0, _socket.AF_UNSPEC):
        family = _socket.AF_INET
    return _original_getaddrinfo(host, port, family, *args, **kwargs)


_socket.getaddrinfo = _ipv4_only_getaddrinfo


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


# ── Logging ───────────────────────────────────────────────────────
# httpx логирует полный URL с токеном → поднимаем до WARNING.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("xg_master")

# ── Webhook path — секретный суффикс защищает от посторонних запросов ──
# Telegram будет слать POST на /webhook/<TOKEN_HASH>
_WEBHOOK_PATH = f"/webhook/{config.telegram_token.split(':')[0]}"


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

    tg_app: Application | None = None
    scheduler = None

    if not config.telegram_token:
        log.error("TELEGRAM_BOT_TOKEN пуст — бот не запущен.")
    else:
        webhook_url = os.getenv("WEBHOOK_URL", "").rstrip("/")
        if not webhook_url:
            log.error(
                "WEBHOOK_URL не задан! Добавь env-переменную WEBHOOK_URL=https://<твой-домен.onrender.com>"
            )

        tg_app = build_application(config.telegram_token)

        # Ретраим initialize при холодном старте
        last_error = None
        for attempt in range(1, 6):
            try:
                await tg_app.initialize()
                last_error = None
                break
            except Exception as e:
                last_error = e
                log.warning("tg_app.initialize() attempt %d/5 failed: %s", attempt, e)
                await asyncio.sleep(min(5 * attempt, 20))

        if last_error is not None:
            log.error("Telegram init не удался: %s", last_error)
            tg_app = None
        else:
            await tg_app.start()

            if webhook_url:
                full_webhook = f"{webhook_url}{_WEBHOOK_PATH}"
                try:
                    # setWebhook регистрирует наш URL в Telegram.
                    # drop_pending_updates=True сбрасывает накопленную очередь.
                    await tg_app.bot.set_webhook(
                        url=full_webhook,
                        drop_pending_updates=True,
                        allowed_updates=Update.ALL_TYPES,
                    )
                    log.info("✅ Webhook установлен: %s", full_webhook)
                except Exception as e:
                    log.error("set_webhook failed: %s", e)
            else:
                log.warning("Webhook не установлен — WEBHOOK_URL не задан")

            scheduler = start_scheduler(tg_app.bot)
            log.info("🚀 xG Master Bot started (webhook mode)")

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
                await tg_app.bot.delete_webhook()
            except Exception:
                pass
            for fn in (tg_app.stop, tg_app.shutdown):
                try:
                    await fn()
                except Exception:
                    pass
        log.info("Shutdown complete.")


app = FastAPI(title="xG Master Bot", version="2.1.0", lifespan=lifespan)


# ── Telegram webhook endpoint ─────────────────────────────────────
@app.post(_WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    """Принимает апдейты от Telegram и передаёт в PTB."""
    tg_app: Application | None = getattr(app.state, "tg_app", None)
    if tg_app is None:
        return Response(status_code=503, content="Bot not ready")
    try:
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)
    except Exception as e:
        log.exception("webhook processing error: %s", e)
        return Response(status_code=500)
    return Response(status_code=200)


# ── Health / info endpoints ───────────────────────────────────────
@app.get("/")
async def root():
    running = getattr(app.state, "tg_app", None) is not None
    return {"status": "ok", "bot": "xG Master Bot", "version": "2.1.0",
            "running": running, "mode": "webhook"}


@app.head("/")
async def root_head():
    return


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/config-check")
async def config_check():
    return {
        "summary": config.summary(),
        "problems": config.validate(),
        "webhook_path": _WEBHOOK_PATH,
    }
