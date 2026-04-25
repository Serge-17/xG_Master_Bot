# Деплой xG Master Bot на Railway

## Шаг 1 — Создай аккаунт
Зайди на https://railway.app → Sign Up with GitHub

## Шаг 2 — Новый проект
New Project → Deploy from GitHub repo → выбери `xG_Master_Bot`

Railway сам найдёт Dockerfile и задеплоит.

## Шаг 3 — Переменные окружения
В Railway Dashboard → твой проект → Variables → добавь:

| Переменная | Значение |
|---|---|
| TELEGRAM_BOT_TOKEN | токен от @BotFather |
| GEMINI_API_KEY | ключ от Google AI Studio |
| FOOTBALL_API_KEY | ключ от football-data.org |
| ODDS_API_KEY | ключ от the-odds-api.com |
| DATABASE_URL | строка Neon PostgreSQL |
| CHANNEL_ID | ID твоего канала (-100...) |
| ADMIN_ID | твой Telegram ID |

**WEBHOOK_URL пока не добавляй** — сначала задеплой без него.

## Шаг 4 — Первый деплой
Railway автоматически задеплоит после добавления переменных.
Подожди 2-3 минуты, проверь логи — бот запустится в polling-режиме.

## Шаг 5 — Добавь Webhook (для стабильности)
После деплоя:
1. Railway Dashboard → Settings → Domains → Generate Domain
2. Скопируй URL (типа `https://xg-master-bot-production.up.railway.app`)
3. Variables → добавь `WEBHOOK_URL` = скопированный URL
4. Railway автоматически передеплоит → бот переключится на webhook

## Готово!
Бот работает 24/7. Логи: Railway Dashboard → твой сервис → Deployments → View Logs

## Локальный запуск (для разработки)
```bash
cp .env.example .env
# заполни .env своими токенами (WEBHOOK_URL оставь пустым)
pip install -r requirements.txt
python -m uvicorn webapp:app --host 0.0.0.0 --port 8000
```
Бот запустится в polling-режиме автоматически.
