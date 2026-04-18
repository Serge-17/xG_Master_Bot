# ТЗ: xG_Master_Bot v2 — ставочный AI-ассистент

> Версия: 2.0 | Дата: 2026-04-18
> Автор: Сергей Елисеев
> Статус: Draft — к реализации

---

## 1. Архитектура (что и откуда берём)

### Источники данных (все бесплатные)

| Что | Источник | Лимит free | Заметки |
|---|---|---|---|
| Матчи / fixtures | `api.football-data.org` | 10 req/min, 10 топ-лиг | Основной |
| xG / shots / статы | парсинг `understat.com` | без лимита | HTML scraping, JSON в `<script>` |
| Форма, H2H, составы | парсинг `fbref.com` / `sofascore.com` | без лимита | Backup источник |
| Коэффициенты букмекеров | `the-odds-api.com` | 500 req/мес | На 30 матчей/день хватит с кэшем |
| Травмы / дисквалификации | `sofascore.com` | scraping | Опционально |

**Кэш:** Redis (или SQLite-cache) на 1–6 часов — одни и те же fixtures / xG не дёргать повторно.

### AI-слой (бесплатно)

- **Gemini 2.5 Flash** (Google AI Studio) — **1500 запросов/день бесплатно**, умеет vision (OCR купонов) + JSON output + русский отлично
- Backup: **DeepSeek Chat** через OpenRouter — бесплатная модель `deepseek/deepseek-chat-v3:free`

---

## 2. Функциональные модули

### 2.1 Match Scanner (cron раз в 3 часа)

```
1. Получить fixtures на сегодня+завтра из api.football-data.org
2. Для каждого матча:
   - Спарсить xG обеих команд (последние 10 матчей, understat)
   - Спарсить форму (W/D/L последние 5)
   - Спарсить H2H (last 5 meetings)
   - Подтянуть коэффициенты (the-odds-api: 1X2, Total, BTTS)
3. Сохранить в таблицу match_analysis
```

### 2.2 Value Calculator

```python
# Poisson на xG средних
fair_prob_home = poisson_win_prob(xG_home_avg, xG_away_avg)
bookie_prob = 1 / odds_home
edge = fair_prob_home - bookie_prob

# Порог value bet
if edge > 0.05 and odds in [1.5, 3.5]:
    recommend = True
    confidence = edge * 100
```

### 2.3 Stake Sizing (Kelly дробный)

```
stake = bank × fraction × (edge / (odds - 1))
fraction = 0.25  # «четверть-Келли», защита от банкролл-краша
min_stake = max(100₽, bank × 0.01)
max_stake = bank × 0.05  # никогда >5% за ставку
```

### 2.4 Channel Post Generator (Gemini Flash)

Промпт на вход: `match_data + analysis + odds + stake`.

Выход JSON:

```json
{
  "title": "⚽ Манчестер Сити — Арсенал",
  "bet": "П1 (победа Сити)",
  "odds": 1.85,
  "stake_rub": 450,
  "confidence": "72%",
  "reasoning": "3 причины в 1-2 предложения каждая",
  "risk": "низкий/средний/высокий"
}
```

→ форматируется в пост канала с inline-кнопкой **«💰 Принять ставку»**.

### 2.5 Bankroll Bot

Команды:

- `/bank` — показать текущий баланс + история пополнений
- `/deposit 10000` — внести сумму
- `/withdraw 5000` — зафиксировать снятие (уменьшает банк)

### 2.6 Coupon OCR (Gemini Flash Vision)

Пользователь шлёт фото купона → Gemini с structured prompt:

```
Распознай купон букмекера. Верни JSON:
{
  "bet_id": "номер купона",
  "event": "название матча",
  "bet_type": "П1/П2/Тотал/...",
  "odds": 1.85,
  "stake": 450,
  "status": "won/lost/pending/returned",
  "payout": 832.50
}
```

### 2.7 Bet Tracker

- При нажатии **«Принять ставку»** в канале → запись в `bets` со статусом `placed`
- Пользователь шлёт скрин → привязка по `bet_id` или fuzzy-match события → статус `won/lost`
- Автоматический пересчёт банка

### 2.8 Statistics

`/stats` → за 7д / 30д / всё время:

- Ставок, WinRate %, ROI %, Profit ₽
- График кривой банка (matplotlib → PNG)
- Лучшая лига, худшая лига

---

## 3. UX в Telegram

### Главное меню (ReplyKeyboard, постоянное)

```
┌─────────────┬─────────────┐
│ 💰 Банк     │ 📊 Статистика│
├─────────────┼─────────────┤
│ 📸 Загрузить│ ⚙️ Настройки │
│   купон     │              │
└─────────────┴─────────────┘
```

### Пост в канал (пример)

```
⚽ Манчестер Сити — Арсенал
🏆 Premier League | 19:30 МСК

📈 Ставка: П1 @ 1.85
💵 Рекомендую: 450₽ (3% банка)
🎯 Уверенность: 72%

Почему:
• xG Сити дома за 10 матчей: 2.3 vs 1.1 у Арсенала на выезде
• Форма Сити: ВВВНВ (4 победы из 5)
• H2H дома: 4-1-0 в пользу Сити
⚠️ Риск: травма Родри

[💰 Принять ставку] [📊 Детали]
```

---

## 4. Схема БД (дополнения к существующей)

```sql
matches (id, league, home, away, kick_off, xg_home, xg_away,
         form_home, form_away, h2h_json, odds_json, analyzed_at)

predictions (id, match_id, bet_type, fair_prob, bookie_prob,
             edge, recommended_stake, confidence, channel_msg_id)

bets (id, user_id, prediction_id, stake, odds, status,
      payout, placed_at, settled_at, coupon_photo_file_id)

bankroll_tx (id, user_id, type, amount, balance_after, note, created_at)
```

---

## 5. План реализации (5 этапов, ~30 ч)

| # | Этап | Часы | Критерий готовности |
|---|---|---|---|
| 1 | Парсеры данных (understat, fbref, the-odds-api) + кэш | 8 | Команда `/scan` выдаёт матч с реальными xG и odds |
| 2 | Value calculator + Kelly + DB-схема | 4 | `analyze_match(id)` возвращает рекомендацию |
| 3 | Gemini-генератор поста + публикация в канал + inline-кнопки | 6 | Пост в тестовом канале с кнопкой «Принять» |
| 4 | Bankroll + OCR купонов (Gemini Vision) + привязка ставок | 8 | Сценарий deposit → bet → coupon → auto-settle работает |
| 5 | Statistics (`/stats`, график), scheduler, deploy на HF Space | 4 | Бот крутится 24/7, digest раз в 3 часа |

---

## 6. Бесплатный AI для написания кода

**Рекомендую Gemini Code Assist** (Google):

- **180 000 completions / мес бесплатно** — самый щедрый tier на рынке
- Плагин в VSCode + JetBrains
- Ссылка: [codeassist.google](https://codeassist.google)

Альтернативы:

| Инструмент | Free лимит | Когда брать |
|---|---|---|
| **Gemini Code Assist** | 180k completions/мес | Основной выбор |
| **GitHub Copilot Free** | 2k completions + 50 chat/мес | Если уже в GitHub |
| **Cursor Free** | 2k completions + limited chat | Если любишь IDE |
| **Aider + Gemini Flash** | ≈бесконечно (1500 req/день API) | CLI-кодинг из терминала |
| **Google Jules** | бесплатный AI-agent | Автономные задачи типа «сделай фичу X» |

**Для runtime-AI внутри бота** (OCR + генерация постов): **Gemini 2.5 Flash через AI Studio** — бесплатно, 1500 req/день, vision + JSON mode.

---

## 7. Риски / ограничения

- **Парсинг understat / fbref** может сломаться при редизайне → закладываем fallback на sofascore
- **The Odds API 500/мес** = ~16 запросов/день, достаточно только с агрессивным кэшем (6 ч TTL)
- **Gemini free tier** — 1500 req/день: на digest (30 матчей) + OCR (10 купонов) хватает, но если вырастет — нужен платный
- **Ставки = регулируемая тема в РФ** → формулировка «аналитика», а не «советы по ставкам»; дисклеймер 18+
- **HF Space CPU free tier** засыпает через 48 ч — нужен UptimeRobot ping на webhook

---

## 8. Необходимые ключи / доступы перед стартом

- [ ] `TELEGRAM_BOT_TOKEN` — создать через @BotFather
- [ ] `TELEGRAM_CHANNEL_ID` — ID канала для публикаций (бот должен быть админом)
- [ ] `FOOTBALL_DATA_API_KEY` — регистрация на football-data.org
- [ ] `ODDS_API_KEY` — регистрация на the-odds-api.com
- [ ] `GEMINI_API_KEY` — получить на ai.google.dev/aistudio
- [ ] `OPENROUTER_API_KEY` (backup) — openrouter.ai

---

*Документ живой — обновляется по мере реализации. Изменения через PR + отметка в changelog ниже.*

## Changelog

- **v2.0 (2026-04-18)** — первая версия ТЗ, подготовлена к старту Этапа 1
