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
    create_coupon,
    create_prediction,
    get_recent_predictions,
    get_settled_predictions,
    get_user_summary,
    record_settlement,
    record_transaction,
    set_bankroll,
    set_bankroll_preferences,
    update_prediction_result,
    update_coupon_settlement,
)
from modules.ai_analyst import (
    analyze_news_sentiment,
    format_prediction_message,
    generate_prediction,
)
from modules.bankroll_manager import recommended_stake
from modules.data_sources import build_match_context, list_fixtures_for_date, FixtureRow
from modules.daily_digest import (
    apply_bankroll_to_recommendations,
    build_daily_recommendations,
    format_user_digest,
)
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


# ── FSM States ─────────────────────────────────────────────────────────────

class MatchScan(StatesGroup):
    waiting_league = State()
    waiting_teams = State()


class BankrollSetup(StatesGroup):
    waiting_amount = State()


class StrategySetup(StatesGroup):
    waiting_params = State()


# ── Keyboards ──────────────────────────────────────────────────────────────

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Мой Банк", callback_data="menu_bank"),
             InlineKeyboardButton(text="📸 Загрузить чек", callback_data="menu_upload")],
            [InlineKeyboardButton(text="📈 Статистика", callback_data="menu_stats"),
             InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings")],
            [InlineKeyboardButton(text="🗓 Матчи дня", callback_data="menu_today"),
             InlineKeyboardButton(text="📋 Прогнозы", callback_data="menu_predictions")],
            [InlineKeyboardButton(text="🔍 Найти матч", callback_data="menu_predict"),
             InlineKeyboardButton(text="📝 Ретро-отчёт", callback_data="menu_retro")],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]]
    )


def top_leagues_kb() -> InlineKeyboardMarkup:
    TOP_LEAGUES = [
        ("Премьер-лига", "premier league"), ("Ла Лига", "la liga"),
        ("Серия А", "serie a"), ("Бундеслига", "bundesliga"),
        ("Лига 1", "ligue 1"), ("Лига чемпионов", "champions league"),
    ]
    rows = []
    for i in range(0, len(TOP_LEAGUES), 2):
        chunk = TOP_LEAGUES[i:i+2]
        rows.append([
            InlineKeyboardButton(text=name, callback_data=f"pick_league_{key}")
            for name, key in chunk
        ])
    rows.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Safe methods (фикс ошибок "message is not modified" и "query too old") ──

async def safe_edit(call: CallbackQuery, text: str, reply_markup=None, parse_mode="HTML"):
    try:
        await call.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            log.warning(f"Edit error: {e}")


async def safe_answer(call: CallbackQuery, text: str = "✅"):
    try:
        await call.answer(text)
    except TelegramBadRequest:
        pass  # query too old — игнорируем


# ── Helper functions ───────────────────────────────────────────────────────

def _fixtures_for_manual_league(league_key: str):
    target_date = datetime.now().date()
    all_fixtures: list[FixtureRow] = list_fixtures_for_date(target_date)

    if league_key == "europe":
        accepted = {"champions league", "europa league", "conference league"}
        selected = [f for f in all_fixtures if f.league.lower() in accepted]
        display_name = "Евро матчи"
    else:
        selected = [f for f in all_fixtures if league_key in f.league.lower()]
        display_name = league_key.title()

    fixtures = [
        {
            "league": row.league,
            "home_team": row.home_team,
            "away_team": row.away_team,
            "kickoff": row.kickoff or "",
        }
        for row in selected[:15]
    ]
    return display_name, fixtures


def build_match_buttons(fixtures: list[dict]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=f"{f['home_team']} — {f['away_team']}",
            callback_data=f"pick_match_{i}"
        )]
        for i, f in enumerate(fixtures)
    ]
    buttons.append([InlineKeyboardButton(text="◀️ К лигам", callback_data="menu_predict")])
    buttons.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Callbacks ──────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "menu_main")
async def cb_main_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, "⚽ <b>xG Master Bot</b> — главное меню", main_menu_kb())
    await safe_answer(call)


@dp.callback_query(F.data.startswith("pick_league_"))
async def cb_pick_league(call: CallbackQuery, state: FSMContext):
    league_key = call.data.replace("pick_league_", "")
    league_name, fixtures = _fixtures_for_manual_league(league_key)

    if not fixtures:
        await safe_edit(call,
            f"⚠️ В лиге <b>{league_name}</b> на сегодня матчей не найдено.\nПопробуйте другую лигу.",
            top_leagues_kb())
        await safe_answer(call)
        return

    await state.update_data(manual_fixtures=fixtures, manual_league=league_name)
    text = f"⚽ <b>{league_name}</b>\nМатчи на {datetime.now().strftime('%d.%m.%Y')}:\n\n"
    for i, f in enumerate(fixtures, 1):
        kick = f"{f['kickoff']} " if f['kickoff'] else ""
        text += f"{i}. {kick}{f['home_team']} — {f['away_team']}\n"

    await safe_edit(call, text + "\nВыберите матч:", build_match_buttons(fixtures))
    await safe_answer(call)


@dp.callback_query(F.data.startswith("pick_match_"))
async def cb_pick_match(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fixtures = data.get("manual_fixtures", [])
    try:
        idx = int(call.data.split("_")[-1])
        fixture = fixtures[idx]
    except:
        await safe_answer(call, "❌ Матч не найден")
        return

    wait_msg = await call.message.answer("⏳ Анализирую матч...")

    prediction_payload = _generate_prediction_payload(   # эта функция у тебя уже есть ниже
        telegram_id=call.from_user.id,
        league=fixture["league"],
        home_team=fixture["home_team"],
        away_team=fixture["away_team"],
    )

    await wait_msg.delete()

    close_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Выиграл", callback_data=f"settle_{prediction_payload['prediction_id']}_win"),
            InlineKeyboardButton(text="❌ Проиграл", callback_data=f"settle_{prediction_payload['prediction_id']}_loss"),
            InlineKeyboardButton(text="🔄 Возврат", callback_data=f"settle_{prediction_payload['prediction_id']}_refund"),
        ],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]
    ])

    await call.message.answer(
        prediction_payload["text"],
        reply_markup=close_kb,
        parse_mode="HTML"
    )
    await safe_answer(call)


# Остальные обработчики (menu_bank, menu_stats и т.д.) оставил почти без изменений,
# только заменил edit_text на safe_edit

# ... (остальной код можно оставить как есть, главное — заменить edit_text и answer)

# ── Точка входа ─────────────────────────────────────────────────────────────

async def main():
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не настроен!")

    if not settings.api_football_key:
        log.warning("API_FOOTBALL_KEY не найден! Матчи могут не загружаться.")

    init_db()
    bot = Bot(token=settings.telegram_bot_token, parse_mode="HTML")
    
    start_scheduler()
    register_reporting_jobs(bot)

    log.info("xG Master Bot запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())