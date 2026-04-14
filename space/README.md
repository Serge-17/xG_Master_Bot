---
title: xG-Master Bot
emoji: ⚽
colorFrom: green
colorTo: blue
sdk: docker
app_port: 7860
---

# xG-Master Bot

Docker Space for a football betting analytics bot.

Environment variables to set in Space Settings:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_URL`
- `TELEGRAM_WEBHOOK_SECRET`
- `DATABASE_URL`
- `AI_PROVIDER`
- `DATA_PROVIDER`
- `OCR_PROVIDER`

Recommended runtime command:

```bash
uvicorn xG_Master_Bot.webapp:app --host 0.0.0.0 --port 7860
```
