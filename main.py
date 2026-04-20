"""
xG Master Bot v2
─────────────────────────────────────────────────────────────────────────────
Env vars (HF Space secrets):
  TELEGRAM_TOKEN   — bot token from @BotFather
  CHANNEL_ID       — channel ID, e.g. -1001234567890 or @mychannel
  ADMIN_ID         — your Telegram user ID (digits only)
  HF_TOKEN         — Hugging Face token
  ODDS_API_KEY     — the-odds-api.com key (free: 500 req/month)
  FOOTBALL_API_KEY — football-data.org key (free tier)
─────────────────────────────────────────────────────────────────────────────
"""

import os, re, json, base64, logging, sqlite3, asyncio, threading
from datetime import datetime, timezone
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, HTTPServer

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from huggingface_hub import InferenceClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
CHANNEL_ID       = os.environ["CHANNEL_ID"]
ADMIN_ID         = int(os.environ.get("ADMIN_ID", 0))
HF_TOKEN         = os.environ.get("HF_TOKEN", "")
ODDS_API_KEY     = os.environ.get("ODDS_API_KEY", "")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "")

MODEL_ANALYSIS = "Qwen/Qwen2.5-72B-Instruct"
MODEL_VISION   = "meta-llama/Llama-3.2-11B-Vision-Instruct"
DB_FILE        = "xg_master.db"
SCAN_HOUR      = 9      # daily auto-scan hour (UTC)
MIN_CONFIDENCE = 55     # skip signals below this %

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

hf = InferenceClient(api_key=HF_TOKEN) if HF_TOKEN else None


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH CHECK SERVER (keeps HF Space alive)
# ─────────────────────────────────────────────────────────────────────────────
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(b"xG Master Bot v2")
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
                amount  REAL    DEFAULT 0,
                updated TEXT    DEFAULT CURRENT_TIMESTAMP
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
                tg_msg_id   INTEGER,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)


def _db(): return sqlite3.connect(DB_FILE)


def get_bank() -> float:
    with _db() as c: return c.execute("SELECT amount FROM bank WHERE id=1").fetchone()[0]

def set_bank(v: float):
    with _db() as c: c.execute("UPDATE bank SET amount=?,updated=CURRENT_TIMESTAMP WHERE id=1", (round(v,2),))

def add_bet(match, pick, odds, stake) -> int:
    with _db() as c:
        return c.execute("INSERT INTO bets (match_title,pick,odds,stake) VALUES (?,?,?,?)",
                         (match, pick, odds, round(stake,2))).lastrowid

def close_bet(bet_id, won, stake, odds):
    profit = round(stake*odds - stake, 2) if won else round(-stake, 2)
    with _db() as c:
        c.execute("UPDATE bets SET result=?,profit=?,closed_at=CURRENT_TIMESTAMP WHERE id=?",
                  ("win" if won else "loss", profit, bet_id))
    set_bank(get_bank() + (stake*odds if won else 0))
    return profit

def save_signal(match, pick, odds, stake, confidence, analysis) -> int:
    with _db() as c:
        return c.execute(
            "INSERT INTO signals (match_title,pick,odds,stake,confidence,analysis) VALUES (?,?,?,?,?,?)",
            (match, pick, round(odds,2), round(stake,2), confidence, analysis)
        ).lastrowid

def get_signals():
    with _db() as c:
        return c.execute(
            "SELECT id,match_title,pick,odds,stake FROM signals ORDER BY created_at DESC LIMIT 10"
        ).fetchall()

def get_stats() -> dict:
    with _db() as c:
        rows = c.execute(
            "SELECT result,COUNT(*),SUM(stake),SUM(profit) FROM bets WHERE result!='pending' GROUP BY result"
        ).fetchall()
    s = {"win":[0,0,0],"loss":[0,0,0]}
    for r,cnt,st,pr in rows: s[r]=[cnt, st or 0, pr or 0]
    total = s["win"][0]+s["loss"][0]
    staked = s["win"][1]+s["loss"][1]
    return {
        "wins": s["win"][0], "losses": s["loss"][0], "total": total,
        "win_rate": round(s["win"][0]/total*100,1) if total else 0,
        "roi": round((s["win"][2]+s["loss"][2])/staked*100,1) if staked else 0,
        "total_profit": round(s["win"][2]+s["loss"][2],2),
        "bank": get_bank(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# FOOTBALL DATA  (football-data.org free tier)
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_todays_matches() -> list[dict]:
    if not FOOTBALL_API_KEY:
        log.warning("No FOOTBALL_API_KEY — using demo matches")
        return [
            {"home":"Real Madrid","away":"Barcelona","competition":"La Liga","id":"demo1"},
            {"home":"Man City","away":"Arsenal","competition":"Premier League","id":"demo2"},
        ]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    comps = ["PL","PD","BL1","SA","FL1","CL","EL"]
    matches = []
    headers = {"X-Auth-Token": FOOTBALL_API_KEY}

    async with aiohttp.ClientSession() as session:
        for comp in comps:
            url = f"https://api.football-data.org/v4/competitions/{comp}/matches"
            params = {"dateFrom": today, "dateTo": today, "status": "SCHEDULED,TIMED"}
            try:
                async with session.get(url, headers=headers, params=params,
                                       timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status != 200: continue
                    data = await r.json()
                    comp_name = data.get("competition",{}).get("name", comp)
                    for m in data.get("matches",[]):
                        matches.append({
                            "home": m["homeTeam"]["name"],
                            "away": m["awayTeam"]["name"],
                            "competition": comp_name,
                            "id": str(m["id"]),
                        })
            except Exception as e:
                log.error(f"football-data [{comp}]: {e}")

    return matches[:20]


# ─────────────────────────────────────────────────────────────────────────────
# ODDS  (the-odds-api.com)
# ─────────────────────────────────────────────────────────────────────────────
def _sim(a, b): return SequenceMatcher(None, a.lower(), b.lower()).ratio()

def _team_match(search, candidate):
    s = re.sub(r'\b(fc|cf|sc|ac|as|rc)\b','', search.lower()).strip()
    c = re.sub(r'\b(fc|cf|sc|ac|as|rc)\b','', candidate.lower()).strip()
    return s in c or c in s or _sim(s,c) > 0.7

async def fetch_odds(home, away) -> dict | None:
    if not ODDS_API_KEY:
        return {"home":2.10,"draw":3.20,"away":3.50,"bookmaker":"demo"}

    sports = ["soccer_epl","soccer_spain_la_liga","soccer_germany_bundesliga",
              "soccer_italy_serie_a","soccer_france_ligue_one","soccer_uefa_champs_league"]
    async with aiohttp.ClientSession() as session:
        for sport in sports:
            try:
                async with session.get(
                    f"https://api.the-odds-api.com/v4/sports/{sport}/odds/",
                    params={"apiKey":ODDS_API_KEY,"regions":"eu","markets":"h2h","oddsFormat":"decimal"},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status != 200: continue
                    for ev in await r.json():
                        if _team_match(home, ev["home_team"]) and _team_match(away, ev["away_team"]):
                            return _best_odds(ev, ev["home_team"])
            except Exception as e:
                log.error(f"Odds API [{sport}]: {e}")
    return None

def _best_odds(event, home_team) -> dict:
    b = {"home":0.0,"draw":0.0,"away":0.0,"bookmaker":""}
    for bk in event.get("bookmakers",[]):
        for mk in bk.get("markets",[]):
            if mk["key"]!="h2h": continue
            for o in mk["outcomes"]:
                p = o["price"]
                if o["name"]==home_team and p>b["home"]:   b["home"]=p; b["bookmaker"]=bk["title"]
                elif o["name"]=="Draw"  and p>b["draw"]:   b["draw"]=p
                elif                       p>b["away"]:    b["away"]=p
    return b


# ─────────────────────────────────────────────────────────────────────────────
# AI ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
def _kelly(bank, prob, odds) -> float:
    edge = prob * odds - 1
    if edge <= 0: return 0
    fraction = min(edge / (odds - 1), 0.10)   # cap at 10% of bank
    return round(bank * fraction, 2)

async def analyze_match(home, away, competition, odds: dict | None) -> dict | None:
    bank = get_bank()
    if not hf:
        prob, raw_odds = 0.60, (odds["home"] if odds else 2.10)
        return {"pick":f"Победа {home}","odds":raw_odds,"confidence":60,
                "reasoning":f"{home} в хорошей форме на своём поле.",
                "risks":"Возможна ничья.","home_form":"W W D W L","away_form":"L D W L D",
                "stake":_kelly(bank,prob,raw_odds),"bank":bank}

    odds_str = ""
    if odds:
        odds_str = (f"Коэффициенты:\n"
                    f"  {home}: {odds['home']}\n  Ничья: {odds['draw']}\n"
                    f"  {away}: {odds['away']}\n  Букмекер: {odds.get('bookmaker','')}\n")

    prompt = (
        f"Ты профессиональный аналитик ставок. Матч: {home} vs {away}, {competition}.\n"
        f"{odds_str}\n"
        "Проанализируй форму, h2h, положение в таблице, травмы, мотивацию, xG.\n"
        "Ответь ТОЛЬКО JSON:\n"
        '{"pick":"Победа X или Ничья или Тотал б2.5 или Обе забьют",'
        '"odds":2.15,"confidence":72,'
        '"reasoning":"3-4 предложения","risks":"1-2 риска",'
        '"home_form":"W W D L W","away_form":"L W W D W"}'
    )
    try:
        resp = hf.chat_completion(
            model=MODEL_ANALYSIS,
            messages=[{"role":"user","content":prompt}],
            max_tokens=700,
        )
        text = resp.choices[0].message.content
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            data = json.loads(m.group())
            prob = data.get("confidence",60)/100
            raw_odds = float(data.get("odds", odds["home"] if odds else 2.0))
            data["stake"] = _kelly(bank, prob, raw_odds)
            data["bank"] = bank
            return data
    except Exception as e:
        log.error(f"AI analysis: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SCREENSHOT OCR
# ─────────────────────────────────────────────────────────────────────────────
async def read_screenshot(image_bytes: bytes) -> dict | None:
    if not hf: return None
    b64 = base64.b64encode(image_bytes).decode()
    prompt = (
        "Это скриншот ставки в букмекерской конторе. "
        "Извлеки данные. Ответь ТОЛЬКО JSON:\n"
        '{"won":true,"stake":1000,"odds":2.1,"payout":2100,"match":"Команда А vs Команда Б"}'
    )
    try:
        resp = hf.chat_completion(
            model=MODEL_VISION,
            messages=[{"role":"user","content":[
                {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}},
                {"type":"text","text":prompt},
            ]}],
            max_tokens=300,
        )
        text = resp.choices[0].message.content
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m: return json.loads(m.group())
    except Exception as e:
        log.error(f"Screenshot OCR: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CHANNEL POST
# ─────────────────────────────────────────────────────────────────────────────
def _bar(pct): return "🟢"*round(pct/10) + "⬜"*(10-round(pct/10))

def build_post(home, away, competition, a: dict) -> tuple[str, InlineKeyboardMarkup]:
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    text = (
        f"⚽ <b>{home} vs {away}</b>\n"
        f"🏆 {competition}  |  🕐 {now}\n\n"
        f"📌 <b>Ставка:</b> {a.get('pick','—')}\n"
        f"📊 Коэффициент: <b>{a.get('odds',0)}</b>\n"
        f"💡 Уверенность: {_bar(a.get('confidence',0))} {a.get('confidence',0)}%\n\n"
        f"📈 {home}: <code>{a.get('home_form','—')}</code>\n"
        f"📈 {away}: <code>{a.get('away_form','—')}</code>\n\n"
        f"🧠 <b>Анализ:</b>\n{a.get('reasoning','')}\n\n"
        f"⚠️ <b>Риски:</b> {a.get('risks','')}\n\n"
        f"💰 Рекомендуемая ставка: <b>{a.get('stake',0)} ₽</b>  (банк {a.get('bank',0)} ₽)"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Мой банк",       callback_data="menu_bank"),
         InlineKeyboardButton("📊 Статистика",     callback_data="menu_stats")],
        [InlineKeyboardButton("📤 Загрузить чек",  callback_data="menu_upload"),
         InlineKeyboardButton("📋 Все сигналы",    callback_data="menu_signals")],
    ])
    return text, kb

async def post_to_channel(app, home, away, competition, analysis) -> int | None:
    text, kb = build_post(home, away, competition, analysis)
    try:
        msg = await app.bot.send_message(CHANNEL_ID, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return msg.message_id
    except Exception as e:
        log.error(f"Channel post: {e}"); return None


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULER JOB
# ─────────────────────────────────────────────────────────────────────────────
async def daily_scan(app):
    log.info("=== Daily scan started ===")
    matches = await fetch_todays_matches()
    posted = 0
    for m in matches[:6]:
        home, away, comp = m["home"], m["away"], m["competition"]
        log.info(f"Analyzing: {home} vs {away}")
        odds     = await fetch_odds(home, away)
        analysis = await analyze_match(home, away, comp, odds)
        if not analysis: continue
        if analysis.get("confidence",0) < MIN_CONFIDENCE:
            log.info(f"Low confidence ({analysis['confidence']}%), skip"); continue

        msg_id = await post_to_channel(app, home, away, comp, analysis)
        sig_id = save_signal(f"{home} vs {away}", analysis["pick"], analysis["odds"],
                             analysis["stake"], analysis["confidence"], analysis["reasoning"])
        if msg_id:
            with _db() as c: c.execute("UPDATE signals SET tg_msg_id=? WHERE id=?", (msg_id, sig_id))

        posted += 1
        if posted >= 3: break
        await asyncio.sleep(8)

    log.info(f"=== Scan done. Posted {posted} signals ===")


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────────────────────────────────────────────────────
MAIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("💰 Мой банк",      callback_data="menu_bank"),
     InlineKeyboardButton("📊 Статистика",    callback_data="menu_stats")],
    [InlineKeyboardButton("📤 Загрузить чек", callback_data="menu_upload"),
     InlineKeyboardButton("📋 Сигналы",       callback_data="menu_signals")],
    [InlineKeyboardButton("🔍 Сканировать матчи", callback_data="menu_scan")],
])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚽ <b>xG Master Bot</b>\n\nАнализирую матчи, слежу за банком, считаю ROI.",
        parse_mode=ParseMode.HTML, reply_markup=MAIN_KB,
    )


async def callback_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()

    match q.data:
        case "menu_bank":
            bank = get_bank()
            presets = [500, 1000, 2000, 5000, 10000]
            rows = [
                [InlineKeyboardButton(f"{p} ₽", callback_data=f"setbank_{p}") for p in presets[:3]],
                [InlineKeyboardButton(f"{p} ₽", callback_data=f"setbank_{p}") for p in presets[3:]],
                [InlineKeyboardButton("◀ Назад", callback_data="back_menu")],
            ]
            await q.message.edit_text(
                f"💰 <b>Банк: {bank} ₽</b>\n\nВыбери сумму или введи /setbank &lt;сумма&gt;",
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows),
            )

        case "menu_stats":
            s = get_stats()
            emoji = "📈" if s["roi"] >= 0 else "📉"
            await q.message.edit_text(
                f"📊 <b>Статистика</b>\n\n"
                f"Ставок: <b>{s['total']}</b>  ✅{s['wins']} / ❌{s['losses']}\n"
                f"Винрейт: <b>{s['win_rate']}%</b>\n"
                f"{emoji} ROI: <b>{s['roi']}%</b>\n"
                f"Прибыль: <b>{s['total_profit']} ₽</b>\n"
                f"💰 Банк: <b>{s['bank']} ₽</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="back_menu")]]),
            )

        case "menu_upload":
            ctx.user_data["awaiting_receipt"] = True
            await q.message.reply_text("📸 Пришли скриншот чека — прочитаю результат и обновлю банк.")

        case "menu_signals":
            sigs = get_signals()
            if not sigs:
                text = "📋 Нет сигналов. Запусти сканирование."
            else:
                lines = ["📋 <b>Последние сигналы:</b>\n"]
                for _, match, pick, odds, stake in sigs:
                    lines.append(f"• <b>{match}</b>\n  {pick} @ {odds} | {stake} ₽\n")
                text = "\n".join(lines)
            await q.message.edit_text(
                text, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀ Назад", callback_data="back_menu")]]),
            )

        case "menu_scan":
            if update.effective_user.id == ADMIN_ID:
                await q.message.reply_text("🔍 Сканирование запущено...")
                asyncio.create_task(daily_scan(ctx.application))
            else:
                await q.message.reply_text("⛔ Только для администратора.")

        case "back_menu":
            await q.message.edit_text(
                "⚽ <b>xG Master Bot</b>\n\nВыбери действие:",
                parse_mode=ParseMode.HTML, reply_markup=MAIN_KB,
            )

        case _ if q.data.startswith("setbank_"):
            amount = float(q.data.split("_")[1])
            set_bank(amount)
            await q.message.edit_text(f"✅ Банк установлен: <b>{amount} ₽</b>", parse_mode=ParseMode.HTML)


async def cmd_setbank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(ctx.args[0])
        assert amount > 0
        set_bank(amount)
        await update.message.reply_text(f"✅ Банк установлен: <b>{amount} ₽</b>", parse_mode=ParseMode.HTML)
    except Exception:
        await update.message.reply_text("Использование: /setbank 5000")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_stats()
    emoji = "📈" if s["roi"] >= 0 else "📉"
    await update.message.reply_text(
        f"📊 <b>Статистика</b>\n\n"
        f"Всего: {s['total']} | ✅{s['wins']} / ❌{s['losses']}\n"
        f"Винрейт: {s['win_rate']}%  {emoji} ROI: {s['roi']}%\n"
        f"Прибыль: {s['total_profit']} ₽  |  💰 Банк: {s['bank']} ₽",
        parse_mode=ParseMode.HTML,
    )


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Только для администратора."); return
    await update.message.reply_text("🔍 Сканирование запущено...")
    asyncio.create_task(daily_scan(ctx.application))


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_receipt"): return
    await update.message.reply_text("🔍 Читаю скрин ставки...")

    photo = update.message.photo[-1]
    file  = await ctx.bot.get_file(photo.file_id)
    image = bytes(await file.download_as_bytearray())

    result = await read_screenshot(image)
    if not result:
        await update.message.reply_text("❌ Не смог прочитать скрин. Пришли более чёткое фото."); return

    won   = result.get("won", False)
    stake = float(result.get("stake", 0))
    odds  = float(result.get("odds", 1.0))
    match = result.get("match", "Ставка")

    bank_before = get_bank()
    bet_id = add_bet(match, "—", odds, stake)
    profit = close_bet(bet_id, won, stake, odds)
    bank_now = get_bank()
    ctx.user_data["awaiting_receipt"] = False

    sign = "+" if profit >= 0 else ""
    emoji = "✅ Победа!" if won else "❌ Проигрыш"
    await update.message.reply_text(
        f"{emoji}\n\n"
        f"🏆 {match}\n"
        f"📊 Коэф: {odds} | Ставка: {stake} ₽\n"
        f"💵 Результат: {sign}{profit} ₽\n"
        f"💰 Банк: {bank_before} ₽ → <b>{bank_now} ₽</b>",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    init_db()
    threading.Thread(target=_run_health, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("setbank", cmd_setbank))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("scan",    cmd_scan))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(daily_scan, "cron", hour=SCAN_HOUR, minute=0, args=[app], id="scan")
    scheduler.start()

    log.info("🚀 xG Master Bot v2 started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
