# ⚽ xG Master Bot

> Telegram-бот для анализа футбольной статистики xG, поиска value-ставок и ведения банкролла.

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![python-telegram-bot](https://img.shields.io/badge/python--telegram--bot-20.x-2CA5E0?logo=telegram)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green)

---

## О проекте

**xG Master Bot** — это AI-ассистент для ставочной аналитики, работающий прямо в Telegram. Вместо интуиции — математика: модель Пуассона с поправкой Dixon-Coles считает справедливые вероятности исходов, сравнивает их с котировками букмекеров и находит расхождения (edge). Если перевес есть — Kelly-критерий рассчитывает оптимальный размер ставки.

Бот публикует сигналы в Telegram-канал, принимает скриншоты чеков (AI-OCR), ведёт статистику с ROI и отслеживает CLV (Closing Line Value) как честную меру качества прогнозов.

---

## Возможности

### 📅 Матчи дня
Отображает расписание из топ-лиг с котировками, разбитое по лигам с флагами и временем по МСК.

### 🔮 Прогнозы (Value Scanner)
- Автоматически прогоняет все матчи дня через математическую модель
- Находит value-ставки там, где вероятность модели превышает рыночную
- Выдаёт разбор: беру / не беру / почему / риски

### 🧮 Математическая модель
- **Poisson + Dixon-Coles** — вычисляет вероятности П1/Х/П2, ТБ/ТМ 2.5, BTTS
- **xG из котировок** — извлекает ожидаемые голы прямо из линии букмекера
- **Форма команд** — корректирует вероятности на базе последних 5 матчей

### 💵 Kelly Stake Sizing
```
stake = bank × fraction × edge / (odds − 1)
fraction = 0.25   # четверть-Kelly — защита от просадки банка
max_stake = bank × 0.05   # не более 5% за ставку
```

### 📤 OCR чеков (Gemini Flash Vision)
Отправь скриншот купона → бот прочитает коэффициент, сумму и статус, сам обновит банк и запишет ставку в историю.

### 💰 Банкролл
- Установка банка через `/setbank` или интерактивно
- Три уровня риска: низкий (2%), средний (5%), высокий (8%)
- История транзакций и автоматический пересчёт Kelly

### 📊 Статистика
- Винрейт, ROI, прибыль в единицах и рублях
- Разбивка по рынкам и лигам: где модель работает лучше

### 📖 Ретро-отчёты
- Итоги за вчера / 7 дней / 30 дней
- По рынкам (П1, ТБ, BTTS...) и по лигам
- **CLV-трекинг** — сравнение цены открытия с закрывающим коэффициентом

### 🔍 Поиск матча
Ручной запуск анализа: выбери лигу → матч → бот прогонит через модель и скажет, есть ли там value прямо сейчас.

---

## Архитектура

```
xG_Master_Bot/
├── bot.py          # Telegram-хендлеры, меню, callback-роутер
├── analysis.py     # Poisson-модель, расчёт edge, Kelly
├── ai.py           # Gemini Flash: объяснение pick + OCR чеков
├── scanner.py      # Автоматический скан матчей (cron)
├── channel.py      # Форматирование и публикация в канал
├── data_sources.py # Источники данных (fixtures, odds, форма)
├── db.py           # База данных: сигналы, ставки, банк, кэш
├── scheduler.py    # Планировщик (раз в 3 часа)
├── settlement.py   # Расчёт результатов сигналов
├── config.py       # Конфигурация из env
├── webapp.py       # Webhook-сервер
└── Dockerfile      # Docker-образ для деплоя
```

### Источники данных

| Источник | Что даёт | Лимит free |
|---|---|---|
| `football-data.org` | Матчи, расписание, 10 топ-лиг | 10 req/min |
| `understat.com` | xG, shots, статистика (scraping) | без лимита |
| `fbref.com` | Форма, H2H, составы (scraping) | без лимита |
| `the-odds-api.com` | Коэффициенты букмекеров | 500 req/мес |
| `sofascore.com` | Травмы, дисквалификации | scraping |

**AI-слой:** Gemini 2.5 Flash (1 500 req/день бесплатно) — генерация постов, объяснение пика, OCR купонов.

---

## Быстрый старт

### Требования
- Python 3.11+
- Docker (опционально)
- Аккаунты: Telegram Bot, football-data.org, the-odds-api.com, Google AI Studio

### 1. Клонировать репозиторий
```bash
git clone https://github.com/Serge-17/xG_Master_Bot.git
cd xG_Master_Bot
```

### 2. Установить зависимости
```bash
pip install -r requirements.txt
```

### 3. Настроить переменные окружения

Создай файл `.env` в корне:
```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHANNEL_ID=-100xxxxxxxxx
FOOTBALL_DATA_API_KEY=your_key
ODDS_API_KEY=your_key
GEMINI_API_KEY=your_key
ADMIN_ID=your_telegram_user_id

# Опционально
OPENROUTER_API_KEY=your_key   # backup AI
DATABASE_URL=sqlite:///bot.db  # или PostgreSQL DSN
```

### 4. Запустить

```bash
python bot.py
```

Или через Docker:
```bash
docker build -t xg-master-bot .
docker run --env-file .env xg-master-bot
```

Инструкция по деплою на Railway: [`RAILWAY_DEPLOY.md`](./RAILWAY_DEPLOY.md)

---

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Главное меню |
| `/setbank 10000` | Установить банк (от суммы считается Kelly) |
| `/scan` | Запустить скан матчей прямо сейчас |
| `/stats` | Статистика: винрейт, ROI, прибыль |
| `/find Реал` | Найти сигнал по названию команды |
| `/settle <id> <win\|loss\|void>` | Ручное закрытие сигнала (только админ) |
| `/help` | Список команд |

---

## Пример сигнала в канале

```
⚽ Манчестер Сити — Арсенал
🏆 Англия. Премьер-лига | 19:30 МСК

📌 Беру: П1 (победа хозяев)
💰 Коэф букмекера: 1.85
🧮 Моя цена: 1.61
📊 Преимущество над линией: +7.3%
🎯 Вероятность по модели: 62%
📈 Рынок 54% → модель 62% (+8 п.п.)
💵 Рекомендую: 450 ₽ · ✅ уверенная

📈 Раскладка:
  П1 62% · Х 22% · П2 16%
  ТБ 2.5 58% · BTTS Да 55%

🧠 Почему беру:
Сити дома xG 2.3 за последние 10 матчей, Арсенал
на выезде отдаёт 1.5. Форма хозяев: В В В Н В.
H2H дома: 4-1-0.
⚠️ Что смущает: возможное отсутствие Родри.

[📊 Детали]  [🔍 Другой матч]
```

---

## База данных

Основные таблицы:

```sql
matches       -- матчи с xG, формой, котировками, H2H
signals       -- сгенерированные прогнозы с пиком, эджем, Kelly-ставкой
bets          -- история ставок пользователя (статус, выплата)
bankroll_tx   -- транзакции банка (пополнения, снятия)
users         -- настройки риска, банк, username
```

---

## Деплой

Бот готов к деплою на:
- **Railway** — рекомендуется, есть `Dockerfile` и `RAILWAY_DEPLOY.md`
- **Heroku / Render / Fly.io** — через `Dockerfile`
- **VPS (Ubuntu)** — `python bot.py` + `systemd` или `pm2`

> Для HF Spaces (бесплатный CPU-tier): добавь UptimeRobot ping, иначе Space засыпает через 48 ч.

---

## Стек

- **Python 3.11** — основной язык
- **python-telegram-bot 20.x** — асинхронный фреймворк (aiogram-совместимый подход)
- **SQLite / PostgreSQL** — хранение данных
- **Gemini 2.5 Flash** — AI-слой (JSON output, vision для OCR)
- **Docker** — контейнеризация
- **Railway** — деплой

---

## Необходимые ключи

| Переменная | Где получить |
|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHANNEL_ID` | ID канала (бот должен быть админом) |
| `FOOTBALL_DATA_API_KEY` | [football-data.org](https://football-data.org) |
| `ODDS_API_KEY` | [the-odds-api.com](https://the-odds-api.com) |
| `GEMINI_API_KEY` | [ai.google.dev/aistudio](https://ai.google.dev/aistudio) |
| `OPENROUTER_API_KEY` *(опц.)* | [openrouter.ai](https://openrouter.ai) |

---

## Дисклеймер

> Бот предоставляет **аналитическую информацию** на основе математических моделей.
> Это **не финансовый совет** и не призыв к ставкам.
> Ставки на спорт — это риск. 18+.
> Автор не несёт ответственности за финансовые решения пользователей.

---

## Автор

**Сергей Елисеев** — [GitHub](https://github.com/Serge-17)

---

*README актуален для версии бота v3.0*
