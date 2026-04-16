import asyncio
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from database import SessionLocal, init_db
from database.crud import get_or_create_user, get_user_summary
from modules.data_sources import TOP_LEAGUES, get_fixtures_by_league, build_match_context
from modules.ai_analyst import ai_analyst
from utils.helpers import format_money

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("xG-Master")

dp = Dispatcher(storage=MemoryStorage())

# ====================== КЛАВИАТУРЫ ======================
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗓 Матчи дня", callback_data="menu_today"),
         InlineKeyboardButton(text="🔍 Найти матч", callback_data="menu_leagues")],
        [InlineKeyboardButton(text="📊 Мой Банк", callback_data="menu_bank"),
         InlineKeyboardButton(text="📈 Статистика", callback_data="menu_stats")]
    ])

def leagues_kb():
    buttons = []
    for name, l_id in TOP_LEAGUES.items():
        buttons.append([InlineKeyboardButton(text=name, callback_data=f"league_{l_id}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="menu_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ====================== ОБРАБОТЧИКИ ======================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    with SessionLocal() as session:
        get_or_create_user(session, message.from_user.id)
    await message.answer("⚽ <b>xG Master Bot</b>\nВыберите раздел:", reply_markup=main_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "menu_main")
async def back_to_main(call: CallbackQuery):
    await call.message.edit_text("⚽ Главное меню:", reply_markup=main_menu_kb())

# 1. ПОКАЗЫВАЕМ СПИСОК ЛИГ
@dp.callback_query(F.data == "menu_leagues")
async def show_leagues(call: CallbackQuery):
    await call.message.edit_text("🏆 Выберите лигу из Топ-10:", reply_markup=leagues_kb())

# 2. ПОКАЗЫВАЕМ МАТЧИ ВЫБРАННОЙ ЛИГИ
@dp.callback_query(F.data.startswith("league_"))
async def show_matches(call: CallbackQuery):
    league_id = int(call.data.split("_")[1])
    await call.answer("⏳ Загружаю матчи...")
    
    fixtures = get_fixtures_by_league(league_id)
    if not fixtures:
        return await call.message.edit_text("❌ На сегодня матчей в этой лиге не найдено.", reply_markup=leagues_kb())
    
    buttons = []
    for f in fixtures[:10]: # Ограничим 10 матчами
        home = f['teams']['home']['name']
        away = f['teams']['away']['name']
        f_id = f['fixture']['id']
        buttons.append([InlineKeyboardButton(text=f"{home} - {away}", callback_data=f"match_{f_id}_{home}_{away}")])
    
    buttons.append([InlineKeyboardButton(text="◀️ К списку лиг", callback_data="menu_leagues")])
    await call.message.edit_text(f"⚽ Матчи на сегодня:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# 3. ПРОВОДИМ АНАЛИЗ МАТЧА
@dp.callback_query(F.data.startswith("match_"))
async def analyze_selected_match(call: CallbackQuery):
    # match_id_HomeName_AwayName
    parts = call.data.split("_")
    home, away = parts[2], parts[3]
    
    await call.answer("🧠 ИИ анализирует xG...")
    
    with SessionLocal() as session:
        user = get_or_create_user(session, call.from_user.id)
        ctx = build_match_context(home, away, "Selected League")
        res = ai_analyst.generate_prediction(ctx, user.bankroll)
    
    text = (
        f"📊 <b>Анализ: {home} vs {away}</b>\n\n"
        f"🤖 Прогноз: <b>{res.best_bet}</b>\n"
        f"📈 Уверенность: <b>{res.confidence}%</b>\n"
        f"💰 Рекомендуемая ставка: <b>{res.recommended_stake} руб.</b>\n\n"
        f"<i>{res.reasoning}</i>"
    )
    
    back_btns = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к матчам", callback_data="menu_leagues")],
        [InlineKeyboardButton(text="🏠 В меню", callback_data="menu_main")]
    ])
    
    await call.message.edit_text(text, reply_markup=back_btns, parse_mode="HTML")

# ОСТАЛЬНЫЕ ОБРАБОТЧИКИ (Банк, Статистика)
@dp.callback_query(F.data == "menu_bank")
async def cb_bank(call: CallbackQuery):
    with SessionLocal() as session:
        summary = get_user_summary(session, call.from_user.id)
    text = f"💰 Банк: {format_money(summary['bankroll'])} руб.\nROI: {summary['roi']}%"
    await call.message.edit_text(text, reply_markup=main_menu_kb())

async def main():
    init_db()
    bot = Bot(token=settings.telegram_bot_token)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())