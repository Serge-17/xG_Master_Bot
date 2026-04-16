from __future__ import annotations
import os
import logging
from fastapi import FastAPI, Header, HTTPException, Request
from aiogram import Bot
from aiogram.types import Update

from config import settings
from database import init_db
from main import dp
from modules.scheduler import register_reporting_jobs, start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("WebApp")

app = FastAPI(title="xG-Master Bot API")

@app.on_event("startup")
async def on_startup() -> None:
    log.info("Starting up xG-Master Bot...")
    # Инициализация БД
    init_db()
    
    # Инициализация бота
    bot = Bot(token=settings.telegram_bot_token)
    app.state.bot = bot
    
    # Запуск планировщика (рассылки)
    start_scheduler()
    register_reporting_jobs(bot)

    # Настройка вебхука
    webhook_url = settings.telegram_webhook_url.strip()
    secret_token = settings.telegram_webhook_secret.strip() or None
    if webhook_url:
        log.info(f"Setting webhook to: {webhook_url}")
        await bot.set_webhook(
            url=webhook_url, 
            secret_token=secret_token, 
            drop_pending_updates=True
        )

@app.on_event("shutdown")
async def on_shutdown() -> None:
    stop_scheduler()
    bot = getattr(app.state, "bot", None)
    if bot:
        await bot.session.close()

@app.get("/")
async def index():
    return {"status": "running", "bot": "xG-Master", "docs": "/docs"}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    secret = settings.telegram_webhook_secret.strip() or None
    if secret and x_telegram_bot_api_secret_token != secret:
        raise HTTPException(status_code=403, detail="Invalid secret")

    payload = await request.json()
    bot = app.state.bot
    update = Update.model_validate(payload, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}