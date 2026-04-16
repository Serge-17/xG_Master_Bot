from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    Message,
    BufferedInputFile
)

# Импорты локальных модулей
from config import settings
from database import SessionLocal, init_db
from database.crud import (
    get_or_create_user,
    get_recent_predictions,
    get_user_summary,
    set_bankroll,
    set_bankroll_preferences,
    create_coupon,
    record_transaction
)

from modules.ai_analyst import ai_analyst
from modules.bankroll_manager import recommended_stake
from modules.data_sources import list_fixtures_for_date, build_match_context
from modules.daily_digest import format_user_digest, build_daily_recommendations
from modules.ocr_processor import process_coupon_image
from modules.retrospective import build_user_retrospective
from utils.helpers import format_money, parse_float

# Настройка логирования в стиле Karpathy (лаконично)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger("xG-Master")

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Создание папки для загрузок, если её нет
download_root = Path(settings.telegram_download_dir)
download_root.mkdir(parents=True, exist_ok=True)

# ====================== FSM (Состояния) ======================
class UserStates(StatesGroup):
    waiting_bankroll = State()
    waiting_coupon = State()
    waiting_predict_search = State()

# ====================== Keyboards (Клавиатуры) ======================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Мой Банк", callback_data="menu_bank"),
         InlineKeyboardButton(text="📸 Загрузить чек", callback_data="menu_upload")],
        [InlineKeyboardButton(text="📈 Статистика", callback_data="menu_stats"),
         InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings")],
        [InlineKeyboardButton(text="🗓 Матчи дня", callback_data="menu_today"),
         InlineKeyboardButton(text="📋 Прогнозы", callback_data="menu_predictions")],
        [InlineKeyboardButton(text="🔍 Найти матч", callback_data="menu_predict"),
         InlineKeyboardButton(text="📝 Ретро-отчёт", callback_data="menu_retro")],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
    ])

# ====================== Helpers ======================
async def safe_edit(call: CallbackQuery, text: str, reply_markup=None):
    try:
        await call.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass

async def safe_answer(call: CallbackQuery, text: str = "✅"):
    try:
        await call.answer(text)
    except TelegramBadRequest:
        pass

# ====================== Command Handlers ======================
@dp.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    with SessionLocal() as session:
        get_or_create_user(session, message.from_user.id)
    
    await message.answer(
        "⚽ <b>xG Master Bot</b> приветствует тебя!\n\n"
        "Я помогу тебе управлять банкроллом и находить валуйные ставки на основе xG-аналитики.\n"
        "Используй меню ниже для навигации 👇",
        reply_markup=main_menu_kb(),
        parse_mode="HTML"
    )

# ====================== Callback Handlers (Menu) ======================

@dp.callback_query(F.data == "menu_main")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, "⚽ <b>xG Master Bot</b> — главное меню", main_menu_kb())
    await safe_answer(call)

@dp.callback_query(F.data == "menu_bank")
async def cb_bank(call: CallbackQuery):
    with SessionLocal() as session:
        summary = get_user_summary(session, call.from_user.id)
    
    text = (
        f"💰 <b>Управление банком</b>\n\n"
        f"Текущий банк: <b>{format_money(summary['bankroll'])} руб.</b>\n"
        f"Стратегия: <code>{summary['bankroll_strategy'].upper()}</code>\n"
        f"Флэт: {summary['flat_percent']}%\n\n"
        f"Используй команду <code>/set_bankroll 10000</code> для изменения суммы."
    )
    await safe_edit(call, text, back_kb())

@dp.callback_query(F.data == "menu_today")
async def cb_today(call: CallbackQuery):
    with SessionLocal() as session:
        summary = get_user_summary(session, call.from_user.id)
    
    # Генерация персонализированного дайджеста
    recs = build_daily_recommendations(limit=8)
    text = format_user_digest(recs, summary)
    
    await safe_edit(call, text, back_kb())
    await safe_answer(call)

@dp.callback_query(F.data == "menu_stats")
async def cb_stats(call: CallbackQuery):
    with SessionLocal() as session:
        summary = get_user_summary(session, call.from_user.id)
    
    text = (
        f"📈 <b>Ваша статистика:</b>\n\n"
        f"Всего ставок: <b>{summary['total_bets']}</b>\n"
        f"Винрейт: <b>{summary['winrate']}%</b>\n"
        f"ROI: <b>{summary['roi']}%</b>\n"
        f"Прибыль/Убыток: <b>{format_money(summary['total_winnings'] - summary['total_losses'])} руб.</b>"
    )
    await safe_edit(call, text, back_kb())

@dp.callback_query(F.data == "menu_upload")
async def cb_upload(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_coupon)
    await safe_edit(call, "📸 <b>Загрузка купона</b>\n\nПришлите скриншот вашей ставки. Я распознаю результат и обновлю ваш банк автоматически.", back_kb())

@dp.callback_query(F.data == "menu_predictions")
async def cb_predictions(call: CallbackQuery):
    with SessionLocal() as session:
        preds = get_recent_predictions(session, call.from_user.id, limit=10)
    
    if not preds:
        text = "📋 <b>У вас пока нет прогнозов.</b>\n\nЗайдите в раздел 'Матчи дня', чтобы получить рекомендации."
    else:
        text = "📋 <b>Последние 10 прогнозов:</b>\n\n"
        for p in preds:
            status_emoji = "⏳" if p.outcome == "Pending" else "✅" if p.outcome == "Win" else "❌"
            text += f"{status_emoji} <code>{p.match_info}</code> — {p.ai_prediction}\n"
    
    await safe_edit(call, text, back_kb())

@dp.callback_query(F.data == "menu_retro")
async def cb_retro(call: CallbackQuery):
    await safe_answer(call, "⏳ Генерирую отчет...")
    with SessionLocal() as session:
        report = build_user_retrospective(session, call.from_user.id)
    
    # Если отчет слишком длинный, Телеграм может его обрезать
    await safe_edit(call, report[:4000], back_kb())

@dp.callback_query(F.data == "menu_predict")
async def cb_predict_search(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_predict_search)
    await safe_edit(call, "🔍 <b>Поиск матча</b>\n\nВведите название команды или лиги для получения xG-анализа:", back_kb())

# ====================== Message Handlers ======================

@dp.message(Command("set_bankroll"))
async def cmd_set_bankroll(message: Message, command: CommandObject):
    if not command.args:
        return await message.answer("Пример: <code>/set_bankroll 5000</code>", parse_mode="HTML")
    
    try:
        amount = parse_float(command.args)
        with SessionLocal() as session:
            set_bankroll(session, message.from_user.id, amount)
        await message.answer(f"✅ Банк успешно установлен: <b>{format_money(amount)} руб.</b>", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Ошибка: введите число.")

@dp.message(UserStates.waiting_predict_search)
async def process_predict_search(message: Message, state: FSMContext):
    query = message.text.strip().lower()
    await message.answer(f"🔎 Ищу матчи по запросу: <i>{query}</i>...", parse_mode="HTML")
    
    # Поиск матчей в сегодняшней линии
    fixtures = list_fixtures_for_date(datetime.now().date())
    found = [f for f in fixtures if query in f.home_team.lower() or query in f.away_team.lower() or query in f.league.lower()]
    
    if not found:
        return await message.answer("❌ Ничего не найдено в ближайшей линии. Попробуйте другое название.", reply_markup=back_kb())
    
    f = found[0] # Берем первый найденный
    with SessionLocal() as session:
        user = get_or_create_user(session, message.from_user.id)
        ctx = build_match_context(f.league, f.home_team, f.away_team)
        analysis = ai_analyst.generate_prediction(ctx, user.bankroll)
        
    res_text = (
        f"📊 <b>Анализ: {f.home_team} vs {f.away_team}</b>\n\n"
        f"{analysis.reasoning}\n\n"
        f"🎯 Рекомендуемая ставка: <b>{analysis.best_bet}</b>\n"
        f"💰 Ставка: <b>{format_money(analysis.recommended_stake)} руб.</b>\n"
        f"📈 Уверенность: <b>{analysis.confidence}/100</b>"
    )
    await message.answer(res_text, parse_mode="HTML", reply_markup=back_kb())
    await state.clear()

@dp.message(F.photo, UserStates.waiting_coupon)
async def handle_coupon_upload(message: Message, state: FSMContext, bot: Bot):
    await message.answer("⏳ <b>Обработка изображения...</b>\nЯ использую OCR для распознавания текста чека.", parse_mode="HTML")
    
    # Скачивание фото
    photo = message.photo[-1]
    file_path = download_root / f"coupon_{message.from_user.id}_{datetime.now().timestamp()}.jpg"
    await bot.download(photo, destination=str(file_path))
    
    try:
        # Распознавание
        result = process_coupon_image(str(file_path))
        
        with SessionLocal() as session:
            # Создаем запись в БД
            create_coupon(
                session, 
                message.from_user.id, 
                image_url=str(file_path),
                ocr_status=result["ocr_status"],
                ocr_recognized_outcome=result["ocr_recognized_outcome"],
                ocr_recognized_amount=result["ocr_recognized_amount"],
                ocr_recognized_odds=result["ocr_recognized_odds"]
            )
            
            # Если результат понятен — обновляем банк (транзакция)
            if result["ocr_recognized_outcome"] in ["Win", "Loss", "Refund"]:
                record_transaction(
                    session, 
                    message.from_user.id, 
                    transaction_type=result["ocr_recognized_outcome"].lower(),
                    amount=result["ocr_recognized_amount"] or 0.0
                )

        response = (
            f"✅ <b>Купон обработан!</b>\n\n"
            f"Статус: {result['ocr_recognized_outcome']}\n"
            f"Сумма: {result['ocr_recognized_amount']} руб.\n"
            f"Коэффициент: {result['ocr_recognized_odds']}\n\n"
            f"<i>Ваш баланс обновлен в соответствии с результатом.</i>"
        )
        await message.answer(response, parse_mode="HTML", reply_markup=back_kb())
        
    except Exception as e:
        log.error(f"OCR Error: {e}")
        await message.answer("❌ Не удалось четко распознать купон. Попробуйте другое фото или введите результат вручную.")
    
    await state.clear()

# ====================== Entry Point ======================
async def main():
    log.info("Инициализация базы данных...")
    init_db()
    
    log.info("Запуск бота...")
    # Если бот запущен не через webapp.py (webhook), а напрямую (polling)
    bot = Bot(token=settings.telegram_bot_token)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен")