from __future__ import annotations

import os

from fastapi import FastAPI, Header, HTTPException, Request
from aiogram import Bot
from aiogram.types import Update

from .config import settings
from .database import init_db
from .main import dp
from .modules.scheduler import register_reporting_jobs, start_scheduler, stop_scheduler


app = FastAPI(title="xG-Master Bot API")


@app.on_event("startup")
async def on_startup() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    init_db()
    app.state.bot = Bot(token=settings.telegram_bot_token)
    start_scheduler()
    register_reporting_jobs(app.state.bot)

    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    secret_token = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip() or None
    if webhook_url:
        await app.state.bot.set_webhook(url=webhook_url, secret_token=secret_token, drop_pending_updates=True)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    stop_scheduler()
    bot = getattr(app.state, "bot", None)
    if bot is not None:
        await bot.session.close()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    expected_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip() or None
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    payload = await request.json()
    bot = request.app.state.bot
    update = Update.model_validate(payload, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webapp:app", host="0.0.0.0", port=int(os.getenv("PORT", "7860")), reload=False)
