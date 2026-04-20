"""
xG Master Bot v2
────────────────────────────────────────────────────────────
HF Space secrets (уже есть у тебя):
  TELEGRAM_TOKEN       — токен бота от @BotFather
  GEMINI_API_KEY       — ключ Google Gemini
  FOOTBALL_DATA_API_KEY — ключ football-data.org
  ODDS_API_KEY         — ключ the-odds-api.com
  ADMIN_ID             — (опционально) твой Telegram user ID
────────────────────────────────────────────────────────────
"""

import os, re, json, base64, logging, sqlite3, asyncio, threading
from datetime import datetime, timezone
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG  (читаем из HF Secrets)
# ─────────────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
FOOTBALL_API_KEY  = os.environ.get("FOOTBALL_DATA_API_KEY", "")
ODDS_API_KEY      = os.environ.get("ODDS_API_KEY", "")
ADMIN_ID          = int(os.environ.get("ADMIN_ID", 0))

GEMINI_MODEL      = "gemini-2.0-flash"          # для текста
GEMINI_VISION     = "gemini-2.0-flash"          # для картинок (мультимодальный)
GEMINI_URL        = "https://generativelanguage.googleapis.com/v1beta/models"

DB_FILE           = "xg_master.db"
SCAN_HOUR         = 9      # автосканирование в 09:00 UTC
MIN_CONFIDENCE    = 55     # минимальная уверенность для публикации сигнала

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH CHECK  (HF Space keep-alive)
# ─────────────────────────────────────────────────────────────────────────────
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"xG Master Bot v2 OK")
    def log_message(self, *_): pass

def _run_health():
    HTTPServer(("0.0.0.0", 7860), _Health).serve_forever()


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_FILE) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS bank (
                id      INTEGER PRIMARY KEY CHECK (id=1),
                amount  REAL DEFAULT 0,
                updated TEXT DEFAULT CURRENT_TIMESTAMP
            );
            INSERT OR IGNORE INTO bank VALUES (1, 0, CURRENT_TIMESTAMP);

            CREATE TABLE IF NOT EXISTS bets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                match_title TEXT,
                pick        TEXT,
                odds        REAL,
                stake       REAL,
                result      TEXT DEFAULT 'pending',
                profit      REAL DEFAULT 0,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                closed_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS signals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                match_title TEXT,
                pick        TEXT,
                odds        REAL,
                stake       REAL,
                confidence  INTEGER,
                analysis    TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)

def _db(): return sqlite3.connect(DB_FILE)

def get_bank() -> float:
    with _db() as c:
        return c.execute("SELECT amount FROM bank WHERE id=1").fetchone()[0]

def set_bank(v: float):
    with _db() as c:
        c.execute("UPDATE bank SET amount=?,updated=CURRENT_TIMESTAMP WHERE id=1",
                  (round(max(0, v), 2),))

def add_bet(match, pick, odds, stake) -> int:
    with _db() as c:
        return c.execute(
            "INSERT INTO bets (match_title,pick,odds,stake) VALUES (?,?,?,?)",
            (match, pick, round(odds,2), round(stake,2))
        ).lastrowid

def close_bet(bet_id: int, won: bool, stake: float, odds: float) -> float:
    profit = round(stake * odds - stake, 2) if won else round(-stake, 2)
    with _db() as c:
        c.execute("UPDATE bets SET result=?,profit=?,closed_at=CURRENT_TIMESTAMP WHERE id=?",
                  ("win" if won else "loss", profit, bet_id))
    set_bank(get_bank() + (stake * odds if won else 0))
    return profit

def save_signal(match, pick, odds, stake, confidence, analysis) -> int:
    with _db() as c:
        return c.execute(
            "INSERT INTO signals (match_title,pick,odds,stake,confidence,analysis) VALUES (?,?,?,?,?,?)",
            (match, pick, round(odds,2), round(stake,2), confidence, analysis)
        ).lastrowid

def get_signals(limit=8):
    with _db() as c:
        return c.execute(
            "SELECT id,match_title,pick,odds,stake,confidence FROM signals "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()

def get_stats() -> dict:
    with _db() as c:
        rows = c.execute(
            "SELECT result,COUNT(*),SUM(stake),SUM(profit) FROM bets "
            "WHERE result!='pending' GROUP BY result"
        ).fetchall()
    s = {"win":[0,0.0,0.0], "loss":[0,0.0,0.0]}
    for r, cnt, st, pr in rows:
        s[r] = [cnt, st or 0.0, pr or 0.0]
    total  = s["win"][0] + s["loss"][0]
    staked = s["win"][1] + s["loss"][1]
    return {
        "wins":  s["win"][0],  "losses": s["loss"][0], "total": total,
        "win_rate": round(s["win"][0]/total*100, 1) if total else 0,
        "roi":   round((s["win"][2]+s["loss"][2])/staked*100, 1) if staked else 0,
        "total_profit": round(s["win"][2]+s["loss"][2], 2),
        "bank":  get_bank(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# GEMINI API  (text + vision)
# ─────────────────────────────────────────────────────────────────────────────
async def gemini_text(prompt: str, model: str = GEMINI_MODEL) -> str | None:
    """Отправляет текстовый запрос в Gemini, возвращает строку ответа."""
    if not GEMINI_API_KEY:
        return None
    url = f"{GEMINI_URL}/{model}:generateContent?key={GEMINI_API_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    log.error(f"Gemini text {r.status}: {await r.text()}")
                    return None
                data = await r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        log.error(f"Gemini text error: {e}")
        return None


async def gemini_vision(prompt: str, image_bytes: bytes, mime: str = "image/jpeg") -> str | None:
    """Отправляет изображение + текст в Gemini Vision."""
    if not GEMINI_API_KEY:
        return None
    url = f"{GEMINI_URL}/{GEMINI_VISION}:generateContent?key={GEMINI_API_KEY}"
    b64 = base64.b64encode(image_bytes).decode()
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": mime, "data": b64}},
        {"text": prompt},
    ]}]}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=45)) as r:
                if r.status != 200:
                    log.error(f"Gemini vision {r.status}: {await r.text()}")
                    return None
                data = await r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        log.error(f"Gemini vision error: {e}")
        return None


def _parse_json(text: str) -> dict | None:
    """Вытаскивает первый JSON-объект из строки."""
    try:
        m = re.search(r'\{.*\}', text, re.DOTALL)
        return json.loads(m.group()) if m else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# FOOTBALL DATA  (football-data.org free tier)
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_todays_matches() -> list[dict]:
    if not FOOTBALL_API_KEY:
        log.warning("FOOTBALL_DATA_API_KEY не задан — демо-матчи")
        return [
            {"home": "Real Madrid",     "away": "Barcelona", "competition": "La Liga"},
            {"home": "Manchester City", "away": "Arsenal",   "competition": "Premier League"},
        ]

    today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    comps   = ["PL", "PD", "BL1", "SA", "FL1", "CL", "EL"]
    matches = []

    async with aiohttp.ClientSession() as session:
        for comp in comps:
            try:
                url    = f"https://api.football-data.org/v4/competitions/{comp}/matches"
                params = {"dateFrom": today, "dateTo": today, "status": "SCHEDULED,TIMED"}
                async with session.get(url, headers=headers, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200:
                        continue
                    data      = await r.json()
                    comp_name = data.get("competition", {}).get("name", comp)
                    for m in data.get("matches", []):
                        matches.append({
                            "home":        m["homeTeam"]["name"],
                            "away":        m["awayTeam"]["name"],
                            "competition": comp_name,
                        })
            except Exception as e:
                log.error(f"football-data [{comp}]: {e}")

    log.info(f"Найдено матчей сегодня: {len(matches)}")
    return matches[:20]


# ─────────────────────────────────────────────────────────────────────────────
# ODDS  (the-odds-api.com)
# ─────────────────────────────────────────────────────────────────────────────
def _sim(a, b) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def _team_match(search: str, candidate: str) -> bool:
    s = re.sub(r'\b(fc|cf|sc|ac|as|rc|afc|bfc)\b', '', search.lower()).strip()
    c = re.sub(r'\b(fc|cf|sc|ac|as|rc|afc|bfc)\b', '', candidate.lower()).strip()
    return s in c or c in s or _sim(s, c) > 0.70

async def fetch_odds(home: str, away: str) -> dict | None:
    if not ODDS_API_KEY:
        return {"home": 2.10, "draw": 3.20, "away": 3.50, "bookmaker": "demo"}

    sports = [
        "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
        "soccer_italy_serie_a", "soccer_france_ligue_one",
        "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    ]
    async with aiohttp.ClientSession() as session:
        for sport in sports:
            try:
                params = {
                    "apiKey": ODDS_API_KEY, "regions": "eu",
                    "markets": "h2h", "oddsFormat": "decimal",
                }
                async with session.get(
                    f"https://api.the-odds-api.com/v4/sports/{sport}/odds/",
                    params=params, timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status != 200:
                        continue
                    for ev in await r.json():
                        if _team_match(home, ev["home_team"]) and _team_match(away, ev["away_team"]):
                            return _best_odds(ev, ev["home_team"])
            except Exception as e:
                log.error(f"Odds [{sport}]: {e}")
    return None

def _best_odds(event: dict, home_team: str) -> dict:
    b = {"home": 0.0, "draw": 0.0, "away": 0.0, "bookmaker": ""}
    for bk in event.get("bookmakers", []):
        for mk in bk.get("markets", []):
            if mk["key"] != "h2h":
                continue
            for o in mk["outcomes"]:
                p = o["price"]
                if o["name"] == home_team and p > b["home"]:
                    b["home"] = p; b["bookmaker"] = bk["title"]
                elif o["name"] == "Draw" and p > b["draw"]:
                    b["draw"] = p
                elif o["name"] != home_team and o["name"] != "Draw" and p > b["away"]:
                    b["away"] = p
    return b


# ─────────────────────────────────────────────────────────────────────────────
# AI АНАЛИЗ  (Gemini)
# ─────────────────────────────────────────────────────────────────────────────
def _kelly(bank: float, prob: float, odds: float) -> float:
    """Критерий Келли, ограничен 10% банка."""
    edge = prob * odds - 1
    if edge <= 0 or bank <= 0:
        return 0.0
    fraction = min(edge / (odds - 1), 0.10)
    return round(bank * fraction, 2)

async def analyze_match(home: str, away: str, competition: str, odds: dict | None) -> dict | None:
    bank = get_bank()

    odds_str = ""
    if odds:
        odds_str = (
            f"Лучшие коэффициенты (EU букмекеры):\n"
            f"  {home}: {odds['home']}\n"
            f"  Ничья: {odds['draw']}\n"
            f"  {away}: {odds['away']}\n"
            f"  Источник: {odds.get('bookmaker','')}\n\n"
        )

    prompt = f"""Ты профессиональный аналитик футбольных ставок с 15-летним опытом.

МАТЧ: {home} vs {away}
ТУРНИР: {competition}
{odds_str}
Проанализируй:
1. Текущую форму команд (последние 5 матчей, реальные данные)
2. h2h — личные встречи
3. Положение в таблице и турнирная мотивация
4. Травмы и дисквалификации ключевых игроков
5. Статистику xG и реализацию моментов
6. Домашнее/выездное преимущество

Выбери ОДНУ ставку с наилучшим value и ответь СТРОГО в JSON (без пояснений вне JSON):
{{
  "pick": "Победа {home}" | "Победа {away}" | "Ничья" | "Тотал больше 2.5" | "Обе забьют",
  "odds": <лучший коэф для этой ставки, число>,
  "confidence": <твоя уверенность 0-100>,
  "reasoning": "<3-4 предложения — конкретные факты почему эта ставка>",
  "risks": "<1-2 главных риска>",
  "home_form": "<последние 5: W/D/L через пробел>",
  "away_form": "<последние 5: W/D/L через пробел>"
}}"""

    text = await gemini_text(prompt)
    if not text:
        # Fallback без AI
        raw_odds = odds["home"] if odds else 2.10
        return {
            "pick": f"Победа {home}", "odds": raw_odds, "confidence": 50,
            "reasoning": "AI недоступен — базовый сигнал по коэффициентам.",
            "risks": "Нет AI-анализа.", "home_form": "? ? ? ? ?", "away_form": "? ? ? ? ?",
            "stake": _kelly(bank, 0.50, raw_odds), "bank": bank,
        }

    data = _parse_json(text)
    if not data:
        log.warning(f"Не удалось распарсить JSON от Gemini:\n{text[:300]}")
        return None

    prob     = data.get("confidence", 60) / 100
    raw_odds = float(data.get("odds", odds["home"] if odds else 2.0))
    data["stake"] = _kelly(bank, prob, raw_odds)
    data["bank"]  = bank
    return data


# ─────────────────────────────────────────────────────────────────────────────
# ЧТЕНИЕ СКРИНШОТА  (Gemini Vision)
# ─────────────────────────────────────────────────────────────────────────────
async def read_screenshot(image_bytes: bytes) -> dict | None:
    prompt = """Это скриншот ставки из букмекерской конторы (1xBet, Фонбет, Мелбет, Леон и т.д.).
Внимательно прочитай все цифры и текст на изображении.
Ответь ТОЛЬКО JSON, без пояснений:
{
  "won": true или false,
  "stake": <сумма ставки в рублях, число>,
  "odds": <коэффициент, число>,
  "payout": <выплата в рублях, 0 если проигрыш>,
  "match": "<название матча или пустая строка>"
}"""
    text = await gemini_vision(prompt, image_bytes)
    if not text:
        return None
    return _parse_json(text)


# ─────────────────────────────────────────────────────────────────────────────
# СКАНИРОВАНИЕ — отправляет сигналы напрямую пользователю
# ─────────────────────────────────────────────────────────────────────────────
def _bar(pct: int) -> str:
    filled = round(pct / 10)
    return "🟢" * filled + "⬜" * (10 - filled)

def _signal_text(home, away, competition, a: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%d.%m %H:%M UTC")
    return (
        f"⚽ <b>{home} vs {away}</b>\n"
        f"🏆 {competition}  |  🕐 {now}\n\n"
        f"📌 <b>Ставка:</b>  {a.get('pick','—')}\n"
        f"📊 Коэффициент:  <b>{a.get('odds', 0)}</b>\n"
        f"💡 Уверенность: {_bar(a.get('confidence',0))} {a.get('confidence',0)}%\n\n"
        f"📈 {home}: <code>{a.get('home_form','—')}</code>\n"
        f"📈 {away}: <code>{a.get('away_form','—')}</code>\n\n"
        f"🧠 <b>Анализ:</b>\n{a.get('reasoning','')}\n\n"
        f"⚠️ <b>Риски:</b> {a.get('risks','')}\n\n"
        f"💰 Рекомендуемая ставка: <b>{a.get('stake', 0)} ₽</b>  (банк: {a.get('bank', 0)} ₽)"
    )

def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Мой банк",      callback_data="menu_bank"),
         InlineKeyboardButton("📊 Статистика",    callback_data="menu_stats")],
        [InlineKeyboardButton("📤 Загрузить чек", callback_data="menu_upload"),
         InlineKeyboardButton("📋 Сигналы",       callback_data="menu_signals")],
        [InlineKeyboardButton("🔍 Сканировать матчи", callback_data="menu_scan")],
    ])

async def run_scan(app, chat_id: int):
    """Сканирует матчи и отправляет сигналы в указанный чат."""
    log.info(f"=== Сканирование матчей для chat_id={chat_id} ===")

    try:
        await app.bot.send_message(chat_id, "🔍 Ищу матчи и анализирую...")
    except Exception: pass

    matches = await fetch_todays_matches()
    if not matches:
        await app.bot.send_message(chat_id, "📭 Сегодня подходящих матчей не найдено.")
        return

    posted = 0
    for m in matches[:6]:
        home, away, comp = m["home"], m["away"], m["competition"]
        log.info(f"Анализирую: {home} vs {away}")

        odds     = await fetch_odds(home, away)
        analysis = await analyze_match(home, away, comp, odds)

        if not analysis:
            continue
        if analysis.get("confidence", 0) < MIN_CONFIDENCE:
            log.info(f"Пропускаю (уверенность {analysis['confidence']}%)")
            continue

        text    = _signal_text(home, away, comp, analysis)
        back_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💰 Банк",   callback_data="menu_bank"),
            InlineKeyboardButton("📊 Стат",   callback_data="menu_stats"),
            InlineKeyboardButton("📤 Чек",    callback_data="menu_upload"),
        ]])

        try:
            await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=back_kb)
        except Exception as e:
            log.error(f"Отправка сигнала: {e}")
            continue

        save_signal(f"{home} vs {away}", analysis["pick"],
                    analysis["odds"], analysis["stake"],
                    analysis["confidence"], analysis["reasoning"])

        posted += 1
        if posted >= 3:
            break
        await asyncio.sleep(5)

    if posted == 0:
        await app.bot.send_message(chat_id, "😐 Хороших ставок сегодня не нашёл (уверенность < 55%).")

    log.info(f"=== Сканирование завершено. Сигналов: {posted} ===")


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ <b>xG Master Bot</b>\n\n"
        "Анализирую матчи с помощью AI, слежу за твоим банком и считаю ROI.\n\n"
        "Выбери действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=_main_keyboard(),
    )


async def callback_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id

    match q.data:

        # ── Главное меню ───────────────────────────────────────────────────
        case "back_menu":
            await q.message.edit_text(
                "⚽ <b>xG Master Bot</b>\n\nВыбери действие:",
                parse_mode=ParseMode.HTML, reply_markup=_main_keyboard(),
            )

        # ── Банк ───────────────────────────────────────────────────────────
        case "menu_bank":
            bank    = get_bank()
            presets = [500, 1000, 2000, 5000, 10000]
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{p} ₽", callback_data=f"setbank_{p}") for p in presets[:3]],
                [InlineKeyboardButton(f"{p} ₽", callback_data=f"setbank_{p}") for p in presets[3:]],
                [InlineKeyboardButton("◀ Назад", callback_data="back_menu")],
            ])
            await q.message.edit_text(
                f"💰 <b>Мой банк: {bank} ₽</b>\n\n"
                "Выбери новую сумму банка или введи /setbank <сумма>",
                parse_mode=ParseMode.HTML, reply_markup=kb,
            )

        case _ if q.data.startswith("setbank_"):
            amount = float(q.data.split("_")[1])
            set_bank(amount)
            await q.message.edit_text(
                f"✅ Банк установлен: <b>{amount} ₽</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀ Назад", callback_data="back_menu")
                ]]),
            )

        # ── Статистика ─────────────────────────────────────────────────────
        case "menu_stats":
            s = get_stats()
            sign  = "📈" if s["roi"] >= 0 else "📉"
            await q.message.edit_text(
                f"📊 <b>Статистика ставок</b>\n\n"
                f"Ставок всего:  <b>{s['total']}</b>\n"
                f"✅ Выиграно:   <b>{s['wins']}</b>\n"
                f"❌ Проиграно:  <b>{s['losses']}</b>\n"
                f"🏆 Винрейт:    <b>{s['win_rate']}%</b>\n"
                f"{sign} ROI:       <b>{s['roi']}%</b>\n"
                f"💵 Прибыль:    <b>{s['total_profit']} ₽</b>\n"
                f"💰 Банк сейчас: <b>{s['bank']} ₽</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀ Назад", callback_data="back_menu")
                ]]),
            )

        # ── Загрузить чек ──────────────────────────────────────────────────
        case "menu_upload":
            ctx.user_data["awaiting_receipt"] = True
            await q.message.reply_text(
                "📸 Пришли скриншот чека о ставке — "
                "AI прочитает результат и обновит банк автоматически."
            )

        # ── Последние сигналы ──────────────────────────────────────────────
        case "menu_signals":
            sigs = get_signals()
            if not sigs:
                body = "📋 Сигналов пока нет.\nНажми 🔍 Сканировать матчи."
            else:
                lines = ["📋 <b>Последние сигналы:</b>\n"]
                for _, match, pick, odds, stake, conf in sigs:
                    lines.append(
                        f"• <b>{match}</b>\n"
                        f"  {pick} @ {odds}  |  {stake} ₽  |  {conf}%\n"
                    )
                body = "\n".join(lines)
            await q.message.edit_text(
                body, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀ Назад", callback_data="back_menu")
                ]]),
            )

        # ── Сканировать матчи ──────────────────────────────────────────────
        case "menu_scan":
            asyncio.create_task(run_scan(ctx.application, chat_id))


# ─────────────────────────────────────────────────────────────────────────────
# КОМАНДЫ
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_setbank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(ctx.args[0])
        assert amount > 0
        set_bank(amount)
        await update.message.reply_text(
            f"✅ Банк установлен: <b>{amount} ₽</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        await update.message.reply_text("Использование: /setbank 5000")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s    = get_stats()
    sign = "📈" if s["roi"] >= 0 else "📉"
    await update.message.reply_text(
        f"📊 Всего: {s['total']}  ✅{s['wins']} / ❌{s['losses']}\n"
        f"Винрейт: {s['win_rate']}%  |  {sign} ROI: {s['roi']}%\n"
        f"Прибыль: {s['total_profit']} ₽  |  💰 Банк: {s['bank']} ₽",
        parse_mode=ParseMode.HTML,
    )


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    asyncio.create_task(run_scan(ctx.application, update.effective_chat.id))


# ─────────────────────────────────────────────────────────────────────────────
# ОБРАБОТЧИК ФОТО  (чек ставки)
# ─────────────────────────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_receipt"):
        # Если фото прислали не в режиме ожидания — спрашиваем
        await update.message.reply_text(
            "📸 Это чек ставки? Нажми кнопку 📤 Загрузить чек в меню, "
            "потом пришли фото."
        )
        return

    await update.message.reply_text("🔍 Читаю скриншот через AI...")

    photo = update.message.photo[-1]
    tg_file    = await ctx.bot.get_file(photo.file_id)
    image_bytes = bytes(await tg_file.download_as_bytearray())

    result = await read_screenshot(image_bytes)

    if not result:
        await update.message.reply_text(
            "❌ Не смог прочитать скрин.\n"
            "Убедись что фото чёткое и хорошо освещено."
        )
        return

    won   = bool(result.get("won", False))
    stake = float(result.get("stake", 0))
    odds  = float(result.get("odds", 1.0))
    match = result.get("match", "Ставка")

    if stake <= 0:
        await update.message.reply_text(
            "⚠️ Не удалось определить сумму ставки.\n"
            "Попробуй прислать более чёткое фото."
        )
        return

    bank_before = get_bank()
    bet_id      = add_bet(match, "—", odds, stake)
    profit      = close_bet(bet_id, won, stake, odds)
    bank_now    = get_bank()

    ctx.user_data["awaiting_receipt"] = False

    sign  = "+" if profit >= 0 else ""
    emoji = "✅ Победа!" if won else "❌ Проигрыш"

    await update.message.reply_text(
        f"{emoji}\n\n"
        f"🏆 {match}\n"
        f"📊 Коэф: {odds}  |  Ставка: {stake} ₽\n"
        f"💵 Результат: {sign}{profit} ₽\n"
        f"💰 Банк: {bank_before} ₽ → <b>{bank_now} ₽</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=_main_keyboard(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN (или BOT_TOKEN) не задан!")

    init_db()
    threading.Thread(target=_run_health, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("setbank", cmd_setbank))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("scan",    cmd_scan))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # Ежедневный авто-скан в SCAN_HOUR:00 UTC
    # Скан идёт в ADMIN_ID если задан, иначе тихо
    if ADMIN_ID:
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            run_scan, "cron", hour=SCAN_HOUR, minute=0,
            args=[app, ADMIN_ID], id="daily_scan",
        )
        scheduler.start()
        log.info(f"Авто-скан в {SCAN_HOUR}:00 UTC → chat {ADMIN_ID}")
    else:
        log.info("ADMIN_ID не задан — авто-скан отключён. Используй /scan или кнопку.")

    log.info("🚀 xG Master Bot v2 запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
