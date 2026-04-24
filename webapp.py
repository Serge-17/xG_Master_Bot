"""
webapp.py — FastAPI entry point (Render / HuggingFace Space).

Запускает Telegram-бота в режиме polling внутри lifespan FastAPI.
Render: используй тип сервиса "Background Worker" — тогда нет конфликта
двух инстансов при деплое. Если хочешь Web Service — переходи на Webhook.
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
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from telegram import Update

from bot import build_application
from config import config
from db import init_db
from scheduler import start_scheduler


# ── Logging ───────────────────────────────────────────────────────
# FIX: httpx логирует полный URL с токеном в каждом запросе.
# Поднимаем его уровень до WARNING — INFO-строки с токеном исчезают.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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
            log.error("Telegram init всё равно не удался: %s", last_error)
            tg_app = None
        else:
            await tg_app.start()

            # FIX: drop_pending_updates=True в delete_webhook + start_polling
            # гарантирует, что после рестарта не обрабатываются старые апдейты.
            # Это устраняет ситуацию когда после конфликта двух инстансов
            # накапливается очередь необработанных сообщений.
            try:
                await tg_app.bot.delete_webhook(drop_pending_updates=True)
            except Exception as e:
                log.warning("delete_webhook: %s", e)

            # FIX: добавляем timeout между попытками polling при Conflict.
            # Если предыдущий инстанс ещё жив — ждём 5 сек и пробуем снова.
            for poll_attempt in range(1, 4):
                try:
                    await tg_app.updater.start_polling(
                        allowed_updates=Update.ALL_TYPES,
                        drop_pending_updates=True,
                        # При конфликте PTB сам ретраит, но добавим явный
                        # bootstrap_retries чтобы он не паниковал сразу.
                        bootstrap_retries=3,
                    )
                    log.info("🚀 xG Master Bot started (polling, attempt %d)", poll_attempt)
                    break
                except Exception as e:
                    log.warning("start_polling attempt %d/3 failed: %s", poll_attempt, e)
                    if poll_attempt < 3:
                        await asyncio.sleep(5)
                    else:
                        log.error("Не удалось запустить polling: %s", e)
                        tg_app = None

            if tg_app is not None:
                scheduler = start_scheduler(tg_app.bot)

    app.state.tg_app = tg_app
    app.state.scheduler = scheduler

    try:
        yield
    finally:
        log.info("Shutting down...")
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        if tg_app is not None:
            for fn in (tg_app.updater.stop, tg_app.stop, tg_app.shutdown):
                try:
                    await fn()
                except Exception:
                    pass
        log.info("Shutdown complete.")


app = FastAPI(title="xG Master Bot", version="2.1.0", lifespan=lifespan)


@app.get("/")
async def root():
    running = getattr(app.state, "tg_app", None) is not None
    return {"status": "ok", "bot": "xG Master Bot", "version": "2.1.0", "running": running}


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
    }


@app.get("/debug/net")
async def debug_net():
    import socket
    import aiohttp

    result = {}
    try:
        result["dns_v4"] = socket.getaddrinfo(
            "api.telegram.org", 443, family=socket.AF_INET,
        )[0][4][0]
    except Exception as e:
        result["dns_v4_error"] = str(e)

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as s:
            # FIX: не логируем полный URL с токеном — используем getMe без вывода URL
            async with s.get(f"https://api.telegram.org/bot{config.telegram_token}/getMe") as r:
                result["tg_getMe_status"] = r.status
                data = await r.json()
                result["tg_getMe_ok"] = bool(data.get("ok"))
                if data.get("ok"):
                    result["bot_username"] = data["result"].get("username")
    except Exception as e:
        result["tg_getMe_error"] = f"{type(e).__name__}: {e}"

    return result
