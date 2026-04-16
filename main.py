from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import settings
from database import SessionLocal, init_db
from database.crud import (
    create_coupon, create_prediction, get_recent_predictions,
    get_settled_predictions, get_user_summary, record_settlement,
    record_transaction, set_bankroll, set_bankroll_preferences,
    update_prediction_result, update_coupon_settlement,
)

from modules.ai_analyst import ai_analyst
from modules.bankroll_manager import recommended_stake
from modules.data_sources import build_match_context, list_fixtures_for_date
from modules.daily_digest import build_personal_today_digest
from modules.localization import parse_matchup, resolve_league_name, translate_market, translate_outcome
from modules.news_parser import build_news_summary
from modules.ocr_processor import interpret_result, process_coupon_image
from modules.retrospective import build_user_retrospective
from modules.scheduler import register_reporting_jobs, start_scheduler
from utils.helpers import format_money, parse_float


logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

download_root = Path(settings.telegram_download_dir)
download_root.mkdir(parents=True, exist_ok=True)


# ====================== FSM ======================
class BankrollSetup(StatesGroup):
    waiting_amount = State()


class StrategySetup(StatesGroup):
    waiting_params = State()


# ====================== Keyboards ======================
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


# ====================== Safe methods ======================
async def safe_edit(call: CallbackQuery, text: str, reply_markup=None):
    try:
        await call.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            log.warning(f"Edit error: {e}")


async def safe_answer(call: CallbackQuery, text: str = "✅"):
    try:
        await call.answer(text)
    except TelegramBadRequest:
        pass


# ====================== /start ======================
@dp.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "⚽ <b>xG Master Bot</b> успешно запущен!\n\n"
        "Выберите действие в меню ниже 👇",
        reply_markup=main_menu_kb(),
        parse_mode="HTML"
    )


# ====================== Главное меню ======================
@dp.callback_query(F.data == "menu_main")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, "⚽ <b>xG Master Bot</b> — главное меню", main_menu_kb())
    await safe_answer(call)


# ====================== Точка входа ======================
async def main():
    log.info("xG Master Bot started")
    # Здесь ничего не запускаем — запуск через webhook в webapp.py


if __name__ == "__main__":
    asyncio.run(main())