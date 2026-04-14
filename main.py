from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
import requests

from config import settings
from database import SessionLocal, init_db
from database.crud import (
    create_coupon,
    create_prediction,
    get_recent_predictions,
    get_settled_predictions,
    get_user_summary,
    record_transaction,
    record_settlement,
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
from modules.data_sources import build_match_context
from modules.daily_digest import (
    apply_bankroll_to_recommendations,
    build_daily_recommendations,
    format_user_digest,
)
from modules.localization import parse_matchup, translate_market, translate_outcome
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
            [
                InlineKeyboardButton(text="📊 Мой Банк", callback_data="menu_bank"),
                InlineKeyboardButton(text="📸 Загрузить чек", callback_data="menu_upload"),
            ],
            [
                InlineKeyboardButton(text="📈 Статистика", callback_data="menu_stats"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu_settings"),
            ],
            [
                InlineKeyboardButton(text="🗓 Матчи дня", callback_data="menu_today"),
                InlineKeyboardButton(text="📋 Прогнозы", callback_data="menu_predictions"),
            ],
            [
                InlineKeyboardButton(text="🔍 Найти матч", callback_data="menu_predict"),
                InlineKeyboardButton(text="📝 Ретро-отчёт", callback_data="menu_retro"),
            ],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")]]
    )


def settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="💰 Установить банк", callback_data="set_bankroll"),
                InlineKeyboardButton(text="📐 Стратегия: Flat", callback_data="set_flat"),
            ],
            [
                InlineKeyboardButton(text="📐 Стратегия: Kelly", callback_data="set_kelly"),
            ],
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")],
        ]
    )


# ── Session helper ──────────────────────────────────────────────────────────

def get_session():
    return SessionLocal()


def build_personal_today_digest(telegram_id: int, limit: int | None = None) -> str:
    target_date = datetime.now().date()
    with get_session() as session:
        summary = get_user_summary(session, telegram_id)

    recommendations = apply_bankroll_to_recommendations(
        build_daily_recommendations(
            target_date=target_date,
            limit=limit or settings.digest_matches_limit,
        ),
        bankroll=float(summary["bankroll"]),
        strategy=str(summary["bankroll_strategy"]),
        flat_percent=float(summary["flat_percent"]) / 100.0,
        kelly_cap=float(summary["kelly_fraction_limit"]) / 100.0,
    )
    return format_user_digest(recommendations, summary, target_date=target_date)


# ── /start ─────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "⚽ <b>xG Master Bot</b> готов к работе!\n\n"
        "Выберите действие в меню ниже 👇",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


@dp.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(
        "<b>Команды:</b>\n"
        "/start — главное меню\n"
        "/set_bankroll &lt;сумма&gt; — установить банк\n"
        "/set_strategy &lt;flat|kelly&gt; [%] [kelly_cap%] — стратегия\n"
        "/my_bankroll — банк и статистика\n"
        "/today — подборка матчей на сегодня\n"
        "/predict &lt;лига&gt; &lt;команда1&gt; vs &lt;команда2&gt; — прогноз\n"
        "/my_predictions — последние прогнозы\n"
        "/close_prediction &lt;id&gt; &lt;win|loss|refund&gt; &lt;сумма&gt; — закрыть\n"
        "/retro_report — ретроспективный отчёт\n"
        "/upload_coupon — загрузить чек",
        parse_mode="HTML",
        reply_markup=back_kb(),
    )


# ── Callback: главное меню ──────────────────────────────────────────────────

@dp.callback_query(F.data == "menu_main")
async def cb_main_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.message.edit_text(
        "⚽ <b>xG Master Bot</b> — главное меню",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )
    await call.answer()


# ── Callback: Мой Банк ──────────────────────────────────────────────────────

@dp.callback_query(F.data == "menu_bank")
async def cb_bank(call: CallbackQuery) -> None:
    with get_session() as session:
        summary = get_user_summary(session, call.from_user.id)

    text = (
        "📊 <b>Мой банк</b>\n\n"
        f"💰 Баланс: <b>{format_money(summary['bankroll'])} руб.</b>\n"
        f"🎯 Ставок: {summary['total_bets']} | Побед: {summary['total_wins']}\n"
        f"📈 Винрейт: {summary['winrate']}% | ROI: {summary['roi']}%\n"
        f"✅ Выигрыши: {format_money(summary['total_winnings'])} руб.\n"
        f"❌ Проигрыши: {format_money(summary['total_losses'])} руб.\n"
        f"🔄 Возвраты: {summary['total_refunds']}\n\n"
        f"⚙️ Стратегия: {summary['bankroll_strategy'].upper()}\n"
        f"📐 Flat %: {summary['flat_percent']}% | Kelly cap: {summary['kelly_fraction_limit']}%"
    )
    await call.message.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()


# ── Callback: Статистика ────────────────────────────────────────────────────

@dp.callback_query(F.data == "menu_stats")
async def cb_stats(call: CallbackQuery) -> None:
    with get_session() as session:
        predictions = get_settled_predictions(session, telegram_id=call.from_user.id, limit=10)

    if not predictions:
        text = "📈 <b>Статистика</b>\n\nЕщё нет завершённых ставок."
    else:
        lines = ["📈 <b>Последние 10 ставок:</b>\n"]
        for p in predictions:
            icon = {"Win": "✅", "Loss": "❌", "Refund": "🔄"}.get(p.outcome, "⏳")
            lines.append(
                f"{icon} #{p.id} {p.match_info}\n"
                f"   {translate_market(p.ai_prediction)} | {format_money(p.placed_amount)} руб. | {translate_outcome(p.outcome)}"
            )
        text = "\n".join(lines)

    await call.message.edit_text(text[:4000], reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()


# ── Callback: Загрузить чек ─────────────────────────────────────────────────

@dp.callback_query(F.data == "menu_upload")
async def cb_upload(call: CallbackQuery) -> None:
    await call.message.edit_text(
        "📸 <b>Загрузка чека</b>\n\n"
        "Отправьте скриншот вашего купона как <b>фото</b> или <b>документ</b>.\n"
        "Бот автоматически распознает результат и обновит банк.",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await call.answer()


# ── Callback: Прогнозы ──────────────────────────────────────────────────────

@dp.callback_query(F.data == "menu_predictions")
async def cb_predictions(call: CallbackQuery) -> None:
    with get_session() as session:
        predictions = get_recent_predictions(session, call.from_user.id, limit=5)

    if not predictions:
        text = "📋 <b>Прогнозы</b>\n\nПрогнозов пока нет. Откройте 🗓 Матчи дня или используйте 🔍 Найти матч."
    else:
        lines = ["📋 <b>Последние прогнозы:</b>\n"]
        for p in predictions:
            icon = {"Win": "✅", "Loss": "❌", "Refund": "🔄", "Pending": "⏳"}.get(p.outcome, "⏳")
            lines.append(
                f"{icon} <b>#{p.id}</b> {p.match_info}\n"
                f"   🎯 {translate_market(p.ai_prediction)} | ⭐ {p.confidence}/5 | "
                f"💰 {format_money(p.recommended_amount)} руб. | {translate_outcome(p.outcome)}"
            )
        text = "\n".join(lines)

    await call.message.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "menu_today")
async def cb_today(call: CallbackQuery) -> None:
    await call.message.edit_text(build_personal_today_digest(call.from_user.id), reply_markup=back_kb())
    await call.answer()


# ── Callback: Настройки ─────────────────────────────────────────────────────

@dp.callback_query(F.data == "menu_settings")
async def cb_settings(call: CallbackQuery) -> None:
    with get_session() as session:
        summary = get_user_summary(session, call.from_user.id)

    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"Текущая стратегия: <b>{summary['bankroll_strategy'].upper()}</b>\n"
        f"Flat %: {summary['flat_percent']}%\n"
        f"Kelly cap: {summary['kelly_fraction_limit']}%\n\n"
        "Выберите действие:"
    )
    await call.message.edit_text(text, reply_markup=settings_kb(), parse_mode="HTML")
    await call.answer()


@dp.callback_query(F.data == "set_bankroll")
async def cb_set_bankroll(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BankrollSetup.waiting_amount)
    await call.message.edit_text(
        "💰 Введите размер вашего банка (в рублях):\nНапример: <code>10000</code>",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@dp.message(BankrollSetup.waiting_amount)
async def process_bankroll_amount(message: Message, state: FSMContext) -> None:
    try:
        amount = parse_float(message.text or "")
    except ValueError:
        await message.answer("❌ Неверный формат суммы. Введите число, например: 10000")
        return
    with get_session() as session:
        user = set_bankroll(session, message.from_user.id, amount)
    await state.clear()
    await message.answer(
        f"✅ Банк установлен: <b>{format_money(user.bankroll)} руб.</b>",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "set_flat")
async def cb_set_flat(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(StrategySetup.waiting_params)
    await state.update_data(strategy="flat")
    await call.message.edit_text(
        "📐 <b>Стратегия: Flat</b>\n\n"
        "Введите процент от банка для каждой ставки:\n"
        "Например: <code>3</code> (= 3% от банка)",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@dp.callback_query(F.data == "set_kelly")
async def cb_set_kelly(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(StrategySetup.waiting_params)
    await state.update_data(strategy="kelly")
    await call.message.edit_text(
        "📐 <b>Стратегия: Kelly</b>\n\n"
        "Введите максимальный % от банка (Kelly cap):\n"
        "Например: <code>25</code> (= cap 25%)",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@dp.message(StrategySetup.waiting_params)
async def process_strategy_params(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    strategy = data.get("strategy", "flat")
    try:
        pct = parse_float(message.text or "")
    except ValueError:
        await message.answer("❌ Введите число, например: 3")
        return

    with get_session() as session:
        if strategy == "flat":
            user = set_bankroll_preferences(session, message.from_user.id, bankroll_strategy="flat", flat_percent=pct / 100)
        else:
            user = set_bankroll_preferences(session, message.from_user.id, bankroll_strategy="kelly", kelly_fraction_limit=pct / 100)

    await state.clear()
    await message.answer(
        f"✅ Стратегия <b>{strategy.upper()}</b> установлена!\n"
        f"Flat %: {round(user.flat_percent * 100, 2)}% | Kelly cap: {round(user.kelly_fraction_limit * 100, 2)}%",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


# ── Callback: Найти матч (FSM) ──────────────────────────────────────────────

@dp.callback_query(F.data == "menu_predict")
async def cb_predict_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(MatchScan.waiting_league)
    await call.message.edit_text(
        "🔍 <b>Поиск матча</b>\n\n"
        "Введите название лиги:\n"
        "Например: <code>Премьер-лига</code>, <code>Ла Лига</code>, <code>Серия А</code>, <code>Лига чемпионов</code>",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await call.answer()


@dp.message(MatchScan.waiting_league)
async def process_league(message: Message, state: FSMContext) -> None:
    await state.update_data(league=message.text or "")
    await state.set_state(MatchScan.waiting_teams)
    await message.answer(
        "⚽ Введите команды в формате:\n<code>Команда1 - Команда2</code>\n\n"
        "Можно писать и так: <code>Арсенал vs Челси</code>\n"
        "Например: <code>Барселона - Атлетико</code>",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )


@dp.message(MatchScan.waiting_teams)
async def process_teams(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    parsed_match = parse_matchup(raw)
    if parsed_match is None:
        await message.answer(
            "❌ Формат не распознан.\nИспользуйте, например: <code>Арсенал - Бавария</code>",
            parse_mode="HTML",
        )
        return

    home_team, away_team = parsed_match
    data = await state.get_data()
    league = data.get("league", "Неизвестная лига")
    await state.clear()

    wait_msg = await message.answer("⏳ Анализирую матч, собираю данные...")

    # Данные матча
    with get_session() as session:
        summary = get_user_summary(session, message.from_user.id)

    bankroll = float(summary["bankroll"])
    strategy = str(summary["bankroll_strategy"])
    flat_percent = float(summary["flat_percent"]) / 100.0
    kelly_cap = float(summary["kelly_fraction_limit"]) / 100.0

    match_context = build_match_context(league, home_team.strip(), away_team.strip())

    # Новости
    news_home = build_news_summary(team_name=home_team.strip(), max_items=3)
    news_away = build_news_summary(team_name=away_team.strip(), max_items=3)
    sentiment_home = analyze_news_sentiment(news_home, home_team.strip())
    sentiment_away = analyze_news_sentiment(news_away, away_team.strip())

    # AI прогноз
    odds_for_strategy = match_context.odds.get("home") if match_context.odds else None
    ai_result = generate_prediction(match_context, bankroll)
    stake = recommended_stake(
        bankroll=bankroll,
        confidence=float(ai_result["confidence"]),
        odds=odds_for_strategy,
        strategy=strategy,
        flat_percent=flat_percent,
        kelly_cap=kelly_cap,
    )

    # Сохранение
    with get_session() as session:
        created = create_prediction(
            session=session,
            telegram_id=message.from_user.id,
            match_info=f"{league} {home_team.strip()} - {away_team.strip()}",
            ai_prediction=str(ai_result["prediction"]),
            ai_reasoning=str(ai_result["reasoning"]),
            confidence=int(ai_result["confidence"]),
            recommended_amount=float(stake),
        )
        record_transaction(
            session=session,
            telegram_id=message.from_user.id,
            transaction_type="Bet",
            amount=float(stake),
            prediction_id=created.id,
        )
        created_id = int(created.id)

    await wait_msg.delete()

    close_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Выиграл", callback_data=f"settle_{created_id}_win"),
                InlineKeyboardButton(text="❌ Проиграл", callback_data=f"settle_{created_id}_loss"),
                InlineKeyboardButton(text="🔄 Возврат", callback_data=f"settle_{created_id}_refund"),
            ],
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data="menu_main")],
        ]
    )
    prediction_text = format_prediction_message(
        match=match_context,
        prediction=str(ai_result["prediction"]),
        reasoning=str(ai_result["reasoning"]),
        confidence=int(ai_result["confidence"]),
        stake=float(stake),
        bankroll=bankroll,
        news_sentiment_home=sentiment_home,
        news_sentiment_away=sentiment_away,
        prediction_id=created_id,
    )

    await message.answer(
        f"{prediction_text}\n\nУкажите результат, когда матч завершится:",
        reply_markup=close_kb,
        parse_mode="HTML",
    )


# ── Callback: Закрытие ставки ───────────────────────────────────────────────

@dp.callback_query(F.data.startswith("settle_"))
async def cb_settle(call: CallbackQuery) -> None:
    _, pred_id_str, outcome = call.data.split("_", 2)
    prediction_id = int(pred_id_str)

    with get_session() as session:
        prediction = update_prediction_result(session, prediction_id, outcome)
        if prediction is None:
            await call.answer("Прогноз не найден.", show_alert=True)
            return
        tx = record_settlement(
            session=session,
            telegram_id=call.from_user.id,
            outcome=outcome,
            stake=float(prediction.placed_amount or prediction.recommended_amount),
            prediction_id=prediction.id,
        )

    icons = {"win": "✅ Победа!", "loss": "❌ Проигрыш", "refund": "🔄 Возврат"}
    await call.message.edit_text(
        f"{icons.get(outcome, '📋')} Прогноз #{prediction_id} закрыт.\n"
        f"💰 Новый банк: <b>{format_money(tx.new_bankroll)} руб.</b>",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await call.answer()


# ── Callback: Ретро-отчёт ───────────────────────────────────────────────────

@dp.callback_query(F.data == "menu_retro")
async def cb_retro(call: CallbackQuery) -> None:
    await call.answer("Генерирую отчёт...")
    with get_session() as session:
        report = build_user_retrospective(session, call.from_user.id, limit=30)
    await call.message.edit_text(
        report[:4000],
        reply_markup=back_kb(),
    )


# ── Команды (text-based) ────────────────────────────────────────────────────

@dp.message(Command("set_bankroll"))
async def set_bankroll_handler(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Использование: /set_bankroll <сумма>")
        return
    try:
        bankroll = parse_float(command.args)
    except ValueError:
        await message.answer("❌ Неверный формат суммы.")
        return
    with get_session() as session:
        user = set_bankroll(session, message.from_user.id, bankroll)
    await message.answer(
        f"✅ Банк обновлён: <b>{format_money(user.bankroll)} руб.</b>",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


@dp.message(Command("set_strategy"))
async def set_strategy_handler(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Использование: /set_strategy <flat|kelly> [flat_%] [kelly_cap_%]")
        return
    parts = command.args.split()
    strategy = parts[0].lower()
    if strategy not in {"flat", "kelly"}:
        await message.answer("Стратегия должна быть flat или kelly.")
        return
    flat_percent = None
    kelly_cap = None
    try:
        if len(parts) > 1:
            flat_percent = parse_float(parts[1]) / 100.0
        if len(parts) > 2:
            kelly_cap = parse_float(parts[2]) / 100.0
    except ValueError:
        await message.answer("❌ Неверные значения процентов.")
        return
    with get_session() as session:
        user = set_bankroll_preferences(
            session, message.from_user.id,
            bankroll_strategy=strategy, flat_percent=flat_percent, kelly_fraction_limit=kelly_cap,
        )
    await message.answer(
        f"✅ Стратегия: {user.bankroll_strategy.upper()}, "
        f"flat {round(user.flat_percent * 100, 2)}%, kelly cap {round(user.kelly_fraction_limit * 100, 2)}%"
    )


@dp.message(Command("my_bankroll"))
async def my_bankroll_handler(message: Message) -> None:
    with get_session() as session:
        summary = get_user_summary(session, message.from_user.id)
    await message.answer(
        f"📊 <b>Банк:</b> {format_money(summary['bankroll'])} руб.\n"
        f"🎯 Ставок: {summary['total_bets']} | Побед: {summary['total_wins']}\n"
        f"📈 Винрейт: {summary['winrate']}% | ROI: {summary['roi']}%\n"
        f"✅ Выигрыши: {format_money(summary['total_winnings'])} руб.\n"
        f"❌ Проигрыши: {format_money(summary['total_losses'])} руб.",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


@dp.message(Command("today"))
async def today_handler(message: Message) -> None:
    await message.answer(build_personal_today_digest(message.from_user.id), reply_markup=main_menu_kb())


@dp.message(Command("my_predictions"))
async def my_predictions_handler(message: Message) -> None:
    with get_session() as session:
        predictions = get_recent_predictions(session, message.from_user.id, limit=5)
    if not predictions:
        await message.answer("Прогнозов пока нет.", reply_markup=main_menu_kb())
        return
    lines = ["📋 <b>Последние прогнозы:</b>\n"]
    for p in predictions:
        icon = {"Win": "✅", "Loss": "❌", "Refund": "🔄", "Pending": "⏳"}.get(p.outcome, "⏳")
        lines.append(
            f"{icon} #{p.id} {p.match_info}: {translate_market(p.ai_prediction)} "
            f"({p.confidence}/5, {format_money(p.recommended_amount)} руб., {translate_outcome(p.outcome)})"
        )
    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_menu_kb())


@dp.message(Command("predict"))
async def predict_handler(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Использование: /predict <лига> ; <команда1> - <команда2>")
        return

    if ";" not in command.args:
        await message.answer("Например: <code>/predict Ла Лига ; Барселона - Атлетико</code>", parse_mode="HTML")
        return

    league_raw, teams_raw = [part.strip() for part in command.args.split(";", 1)]
    parsed_match = parse_matchup(teams_raw)
    if parsed_match is None:
        await message.answer("Команды не распознаны. Пример: <code>Барселона - Атлетико</code>", parse_mode="HTML")
        return
    league = league_raw
    home_team, away_team = parsed_match

    with get_session() as session:
        summary = get_user_summary(session, message.from_user.id)
    bankroll = float(summary["bankroll"])
    strategy = str(summary["bankroll_strategy"])
    flat_percent = float(summary["flat_percent"]) / 100.0
    kelly_cap = float(summary["kelly_fraction_limit"]) / 100.0

    match_context = build_match_context(league, home_team, away_team)
    odds_for_strategy = match_context.odds.get("home") if match_context.odds else None
    ai_result = generate_prediction(match_context, bankroll)
    stake = recommended_stake(bankroll, float(ai_result["confidence"]), odds=odds_for_strategy,
                              strategy=strategy, flat_percent=flat_percent, kelly_cap=kelly_cap)

    with get_session() as session:
        created = create_prediction(
            session=session, telegram_id=message.from_user.id,
            match_info=f"{league} {home_team} - {away_team}",
            ai_prediction=str(ai_result["prediction"]),
            ai_reasoning=str(ai_result["reasoning"]),
            confidence=int(ai_result["confidence"]),
            recommended_amount=float(stake),
        )
        record_transaction(session=session, telegram_id=message.from_user.id,
                           transaction_type="Bet", amount=float(stake), prediction_id=created.id)
        created_id = int(created.id)

    await message.answer(
        format_prediction_message(
            match=match_context,
            prediction=str(ai_result["prediction"]),
            reasoning=str(ai_result["reasoning"]),
            confidence=int(ai_result["confidence"]),
            stake=float(stake),
            bankroll=bankroll,
            prediction_id=created_id,
        ),
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


@dp.message(Command("close_prediction"))
async def close_prediction_handler(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Использование: /close_prediction <id> <win|loss|refund> <сумма>")
        return
    parts = command.args.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Нужно: /close_prediction <id> <win|loss|refund> <сумма>")
        return
    prediction_id_raw, outcome, amount_raw = parts
    try:
        prediction_id = int(prediction_id_raw)
        amount = parse_float(amount_raw)
    except ValueError:
        await message.answer("❌ Неверный ID или сумма.")
        return
    with get_session() as session:
        prediction = update_prediction_result(session, prediction_id, outcome)
        if prediction is None:
            await message.answer("Прогноз не найден.")
            return
        tx = record_settlement(session=session, telegram_id=message.from_user.id,
                               outcome=outcome, stake=amount, prediction_id=prediction.id)
    await message.answer(
        f"✅ Прогноз #{prediction_id} закрыт как {translate_outcome(outcome.title())} на {format_money(amount)} руб.\n"
        f"💰 Новый банк: {format_money(tx.new_bankroll)} руб.",
        reply_markup=main_menu_kb(),
    )


@dp.message(Command("retro_report"))
async def retro_report_handler(message: Message) -> None:
    with get_session() as session:
        report = build_user_retrospective(session, message.from_user.id, limit=30)
    await message.answer(report[:3900], reply_markup=main_menu_kb())


@dp.message(Command("upload_coupon"))
async def upload_coupon_handler(message: Message) -> None:
    await message.answer(
        "📸 Отправьте скриншот купона как фото или документ.\n"
        "Бот распознает результат и обновит банк.",
        reply_markup=back_kb(),
    )


# ── OCR: фото и документы ───────────────────────────────────────────────────

async def _download_telegram_file(bot: Bot, file_id: str, suffix: str) -> Path:
    telegram_file = await bot.get_file(file_id)
    target = download_root / f"{file_id}{suffix}"
    file_url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{telegram_file.file_path}"
    response = requests.get(file_url, timeout=60)
    response.raise_for_status()
    target.write_bytes(response.content)
    return target


def _settle_coupon_if_possible(session, telegram_id: int, ocr_result: dict) -> None:
    outcome_text = str(ocr_result.get("ocr_recognized_outcome") or "")
    amount = ocr_result.get("ocr_recognized_amount")
    odds = ocr_result.get("ocr_recognized_odds")
    if outcome_text.lower() == "pending" or amount is None:
        return
    outcome = interpret_result(outcome_text)
    if outcome in {"Win", "Refund", "Loss"}:
        record_settlement(session=session, telegram_id=telegram_id, outcome=outcome,
                          stake=float(amount), odds=float(odds) if odds else None)


@dp.message(F.photo)
async def photo_handler(message: Message, bot: Bot) -> None:
    photo = message.photo[-1]
    local_path = await _download_telegram_file(bot, photo.file_id, ".jpg")
    ocr_result = process_coupon_image(str(local_path))

    with get_session() as session:
        coupon = create_coupon(
            session=session, telegram_id=message.from_user.id,
            image_url=str(local_path),
            ocr_status=str(ocr_result["ocr_status"]),
            ocr_recognized_outcome=str(ocr_result["ocr_recognized_outcome"]),
            ocr_recognized_amount=ocr_result["ocr_recognized_amount"],
            ocr_recognized_odds=ocr_result.get("ocr_recognized_odds"),
            local_file_path=str(local_path),
        )
        _settle_coupon_if_possible(session, message.from_user.id, ocr_result)
        update_coupon_settlement(session=session, coupon_id=coupon.id,
                                 ocr_status=str(ocr_result["ocr_status"]),
                                 ocr_recognized_outcome=str(ocr_result["ocr_recognized_outcome"]),
                                 ocr_recognized_amount=ocr_result["ocr_recognized_amount"],
                                 ocr_recognized_odds=ocr_result.get("ocr_recognized_odds"))

    outcome = interpret_result(str(ocr_result["ocr_recognized_outcome"]))
    icon = {"Win": "✅", "Loss": "❌", "Refund": "🔄"}.get(outcome, "⏳")
    await message.answer(
        f"📸 <b>Чек обработан</b>\n\n"
        f"{icon} Результат: <b>{outcome}</b>\n"
        f"💰 Сумма: {format_money(coupon.ocr_recognized_amount) if coupon.ocr_recognized_amount else 'N/A'} руб.\n"
        f"📊 Кф: {coupon.ocr_recognized_odds or 'N/A'}\n"
        f"🔄 OCR статус: {coupon.ocr_status}",
        parse_mode="HTML",
        reply_markup=main_menu_kb(),
    )


@dp.message(F.document)
async def document_handler(message: Message, bot: Bot) -> None:
    document = message.document
    if document is None:
        return
    local_path = await _download_telegram_file(bot, document.file_id, f"_{document.file_name or 'coupon'}")
    ocr_result = process_coupon_image(str(local_path))
    with get_session() as session:
        coupon = create_coupon(
            session=session, telegram_id=message.from_user.id,
            image_url=str(local_path),
            ocr_status=str(ocr_result["ocr_status"]),
            ocr_recognized_outcome=str(ocr_result["ocr_recognized_outcome"]),
            ocr_recognized_amount=ocr_result["ocr_recognized_amount"],
            ocr_recognized_odds=ocr_result.get("ocr_recognized_odds"),
            local_file_path=str(local_path),
        )
        _settle_coupon_if_possible(session, message.from_user.id, ocr_result)
        update_coupon_settlement(session=session, coupon_id=coupon.id,
                                 ocr_status=str(ocr_result["ocr_status"]),
                                 ocr_recognized_outcome=str(ocr_result["ocr_recognized_outcome"]),
                                 ocr_recognized_amount=ocr_result["ocr_recognized_amount"],
                                 ocr_recognized_odds=ocr_result.get("ocr_recognized_odds"))
    await message.answer(
        f"📄 Документ обработан\nOCR: {coupon.ocr_status} | {coupon.ocr_recognized_outcome}",
        reply_markup=main_menu_kb(),
    )


@dp.message(Command("record_result"))
async def record_result_handler(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Использование: /record_result <win|loss|refund> <сумма>")
        return
    parts = command.args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Нужно: /record_result <win|loss|refund> <сумма>")
        return
    outcome, amount_raw = parts
    try:
        amount = parse_float(amount_raw)
    except ValueError:
        await message.answer("❌ Неверная сумма.")
        return
    with get_session() as session:
        tx = record_transaction(session=session, telegram_id=message.from_user.id,
                                transaction_type=outcome, amount=amount)
    await message.answer(
        f"✅ {outcome.title()} на {format_money(amount)} руб. записан.\n"
        f"💰 Новый банк: {format_money(tx.new_bankroll)} руб.",
        reply_markup=main_menu_kb(),
    )


# ── Точка входа ─────────────────────────────────────────────────────────────

async def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не настроен")

    init_db()
    bot = Bot(token=settings.telegram_bot_token)
    start_scheduler()
    register_reporting_jobs(bot)
    await dp.start_polling(bot)
