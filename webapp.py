"""
webapp.py — FastAPI веб-сервер для HuggingFace Spaces
Запускает Telegram-бота в режиме webhook + health-check эндпоинт.
HF Spaces требует порт 7860 и публичный HTTPS — webhook идеально подходит.
"""

import asyncio
import logging
import os
import sys

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import uvicorn

# Откладываем импорт бота до старта, чтобы проверить конфиг заранее
logger = logging.getLogger(__name__)

app = FastAPI(title="xG Master Bot", version="1.0.0")


# ── Health check (HuggingFace пингует этот эндпоинт) ──────────
@app.get("/")
async def root():
    return {"status": "ok", "bot": "xG Master Bot", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


# ── Webhook эндпоинт ───────────────────────────────────────────
@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Принимает обновления от Telegram и передаёт боту."""
    try:
        from main import dp, bot
        import json
        from aiogram.types import Update

        data = await request.json()
        update = Update(**data)
        await dp.feed_update(bot=bot, update=update)
        return Response(content="OK", status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        return Response(content="Error", status_code=500)


# ── Эндпоинт для ручной проверки конфига ──────────────────────
@app.get("/config-check")
async def config_check():
    try:
        from config import config
        errors = config.validate()
        return {
            "summary": config.summary(),
            "errors": errors,
            "ok": len(errors) == 0
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Запуск ─────────────────────────────────────────────────────
async def setup_webhook():
    """Регистрирует webhook в Telegram после старта сервера."""
    try:
        from config import config
        from main import bot

        if not config.webhook_url:
            logger.warning("WEBHOOK_URL не задан — webhook не зарегистрирован")
            return

        webhook_full_url = config.webhook_url.rstrip("/") + config.webhook_path
        await bot.set_webhook(url=webhook_full_url)
        logger.info(f"✅ Webhook зарегистрирован: {webhook_full_url}")

        info = await bot.get_webhook_info()
        logger.info(f"Webhook info: {info}")
    except Exception as e:
        logger.error(f"Ошибка регистрации webhook: {e}", exc_info=True)


@app.on_event("startup")
async def on_startup():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logger.info("🚀 xG Master Bot стартует...")

    try:
        from config import config
        errors = config.validate()
        if errors:
            for e in errors:
                logger.warning(e)
        else:
            logger.info("✅ Конфиг валиден")
            logger.info("\n" + config.summary())

        # Запускаем инициализацию БД
        from database.db import init_db
        await init_db()
        logger.info("✅ База данных инициализирована")

        # Регистрируем webhook
        from config import config as cfg
        if cfg.bot_mode == "webhook":
            await setup_webhook()

        # Запускаем планировщик задач
        try:
            from modules.scheduler import start_scheduler
            start_scheduler()
            logger.info("✅ Планировщик запущен")
        except ImportError:
            logger.warning("Модуль scheduler не найден, пропускаем")

    except Exception as e:
        logger.error(f"Ошибка при запуске: {e}", exc_info=True)


@app.on_event("shutdown")
async def on_shutdown():
    try:
        from main import bot
        await bot.delete_webhook()
        await bot.session.close()
        logger.info("Bot shutdown cleanly")
    except Exception as e:
        logger.error(f"Shutdown error: {e}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run(
        "webapp:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        # HuggingFace сам управляет TLS — нам SSL не нужен
    )