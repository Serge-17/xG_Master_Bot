"""
bot.py — Telegram handlers xG Master Bot.

Главное меню (8 кнопок):
  💰 Мой банк   | 📤 Загрузить чек
  📊 Статистика | ⚙️ Настройки
  📅 Матчи дня  | 🔮 Прогнозы
  🔍 Найти матч | 📖 Ретро-отчёт
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai import parse_receipt
from analysis import best_value_pick, poisson_probs, xg_from_odds
from channel import format_signal_post, publish_signal
from config import config
from data_sources import Match, fetch_matches, fetch_odds
from db import (
    add_bet, close_bet, ensure_user, find_signal_by_match, get_bank,
    get_settings, get_signal, list_signals, list_todays_signals,
    retro_report, save_signal, set_bank, stats_for_user, update_settings,
)
from scanner import scan_and_publish


log = logging.getLogger(__name__)


DISCLAIMER = (
    "⚠️ <i>Ставки связаны с риском потери денег. "
    "Бот даёт вероятностные рекомендации, а не гарантии.</i>"
)


# ────────────────────────────────────────────────────────────────
# Главное меню (8 кнопок)
# ────────────────────────────────────────────────────────────────
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Мой банк",     callback_data="menu_bank"),
            InlineKeyboardButton("📤 Загрузить чек", callback_data="menu_upload"),
        ],
        [
            InlineKeyboardButton("📊 Статистика",   callback_data="menu_stats"),
            InlineKeyboardButton("⚙️ Настройки",    callback_data="menu_settings"),
        ],
        [
            InlineKeyboardButton("📅 Матчи дня",    callback_data="menu_matches"),
            InlineKeyboardButton("🔮 Прогнозы",     callback_data="menu_signals"),
        ],
        [
            InlineKeyboardButton("🔍 Найти матч",   callback_data="menu_search"),
            InlineKeyboardButton("📖 Ретро-отчёт",  callback_data="menu_retro"),
        ],
    ])


def back_button(target: str = "back_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀ В меню", callback_data=target)]])


# ────────────────────────────────────────────────────────────────
# Команды
# ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user.id, user.username or "", user.first_name or "")
    await update.message.reply_text(
        "⚽ <b>xG Master Bot</b>\n\n"
        "Ищу value-ставки по топ-лигам. Собираю коэффициенты, считаю Poisson "
        "и Kelly, комментарий — через Gemini.\n\n"
        f"{DISCLAIMER}\n\n"
        "Выбери действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — главное меню\n"
        "/setbank 5000 — установить банк\n"
        "/scan — сканировать матчи прямо сейчас\n"
        "/stats — быстрая статистика\n"
        "/find <команда> — поиск сигнала по названию команды\n",
    )


async def cmd_setbank(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user.id, user.username or "", user.first_name or "")
    try:
        amount = float(ctx.args[0])
        assert amount >= 0
    except Exception:
        await update.message.reply_text("Использование: /setbank 5000")
        return
    await set_bank(user.id, amount)
    await update.message.reply_text(
        f"✅ Банк установлен: <b>{amount:,.0f} ₽</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = await stats_for_user(update.effective_user.id)
    await update.message.reply_text(_stats_text(s), parse_mode=ParseMode.HTML)


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await ensure_user(user_id, update.effective_user.username or "",
                      update.effective_user.first_name or "")
    await ctx.bot.send_message(chat_id, "🔍 Сканирую матчи, ищу value-ставки...")
    try:
        bank = await get_bank(user_id) or 10000.0
        published = await scan_and_publish(ctx.bot, bank)
        if published == 0:
            # Пользователь всё равно получит матчи в личку: дёрнем короткий поиск
            await _send_personal_signals(ctx, chat_id, user_id)
        else:
            await ctx.bot.send_message(
                chat_id, f"✅ Опубликовано сигналов в канал: {published}",
                reply_markup=main_menu(),
            )
    except Exception as e:
        log.exception("scan failed")
        await ctx.bot.send_message(chat_id, f"❌ Ошибка сканирования: {e}")


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args or []).strip()
    if not query:
        await update.message.reply_text("Использование: /find Real Madrid")
        return
    sigs = await find_signal_by_match(query)
    if not sigs:
        await update.message.reply_text("Ничего не нашёл.")
        return
    await update.message.reply_text(
        _signals_list_text(sigs, title=f"🔍 Найдено по «{query}»"),
        parse_mode=ParseMode.HTML,
    )


# ────────────────────────────────────────────────────────────────
# Callback router
# ────────────────────────────────────────────────────────────────
async def callback_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id
    user = update.effective_user
    await ensure_user(user.id, user.username or "", user.first_name or "")

    data = q.data or ""

    if data == "back_menu":
        await q.message.edit_text(
            "⚽ <b>xG Master Bot</b>\n\nВыбери действие:",
            parse_mode=ParseMode.HTML, reply_markup=main_menu(),
        )
        return

    if data == "menu_bank":
        await _show_bank(q, user.id)
        return

    if data.startswith("setbank_"):
        amount = float(data.split("_", 1)[1])
        await set_bank(user.id, amount)
        await q.message.edit_text(
            f"✅ Банк установлен: <b>{amount:,.0f} ₽</b>",
            parse_mode=ParseMode.HTML, reply_markup=back_button(),
        )
        return

    if data == "bank_custom":
        ctx.user_data["awaiting_bank"] = True
        await q.message.edit_text(
            "✏️ Пришли сумму банка числом (например: 7500)",
            reply_markup=back_button(),
        )
        return

    if data == "menu_stats":
        s = await stats_for_user(user.id)
        await q.message.edit_text(
            _stats_text(s), parse_mode=ParseMode.HTML, reply_markup=back_button(),
        )
        return

    if data == "menu_upload":
        ctx.user_data["awaiting_receipt"] = True
        await q.message.reply_text(
            "📸 Пришли скриншот чека ставки — AI прочитает результат и "
            "обновит банк автоматически."
        )
        return

    if data == "menu_settings":
        await _show_settings(q, user.id)
        return

    if data.startswith("risk_"):
        level = data.split("_", 1)[1]
        cap_map = {"low": 0.02, "mid": 0.05, "high": 0.08}
        await update_settings(user.id, {"risk": level, "kelly_cap": cap_map.get(level, 0.05)})
        await _show_settings(q, user.id)
        return

    if data == "menu_matches":
        await q.message.edit_text("📅 Загружаю сегодняшние матчи...", reply_markup=back_button())
        matches = await fetch_matches()
        if not matches:
            await q.message.edit_text("📭 На сегодня матчей не найдено.",
                                      reply_markup=back_button())
            return
        lines = ["📅 <b>Матчи сегодня</b>\n"]
        for m in matches[:20]:
            ts = m.utc_date.strftime("%H:%M") if m.utc_date else "--:--"
            lines.append(f"• {ts} UTC — <b>{m.home}</b> vs <b>{m.away}</b>  <i>[{m.competition}]</i>")
        await q.message.edit_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                  reply_markup=back_button())
        return

    if data == "menu_signals":
        sigs = await list_todays_signals()
        if not sigs:
            sigs = await list_signals(limit=8)
        body = _signals_list_text(sigs, title="🔮 Прогнозы") if sigs else \
            "🔮 Пока нет прогнозов. Нажми /scan — запущу анализ."
        await q.message.edit_text(body, parse_mode=ParseMode.HTML, reply_markup=back_button())
        return

    if data == "menu_search":
        ctx.user_data["awaiting_search"] = True
        await q.message.edit_text(
            "🔍 Напиши название команды или матч (например: <i>Real Madrid</i>)",
            parse_mode=ParseMode.HTML, reply_markup=back_button(),
        )
        return

    if data == "menu_retro":
        bets = await retro_report(user.id, limit=15)
        if not bets:
            body = "📖 История пуста. После закрытия ставок здесь появится отчёт."
        else:
            lines = ["📖 <b>Ретро-отчёт — последние закрытые ставки:</b>\n"]
            for b in bets:
                icon = {"win": "✅", "loss": "❌", "void": "↩️"}.get(b.status, "•")
                profit = f"{b.profit:+.0f} ₽" if b.profit else "0 ₽"
                lines.append(
                    f"{icon} <b>{b.match or '—'}</b>  @ {b.odds:.2f}  |  "
                    f"ставка {b.stake:.0f} ₽  →  {profit}"
                )
            body = "\n".join(lines)
        await q.message.edit_text(body, parse_mode=ParseMode.HTML, reply_markup=back_button())
        return

    if data.startswith("sig_analysis_") or data.startswith("sig_odds_"):
        sig_id = int(data.rsplit("_", 1)[1])
        sig = await get_signal(sig_id)
        if not sig:
            await q.message.reply_text("Сигнал не найден.")
            return
        if data.startswith("sig_odds_"):
            body = (
                f"📊 <b>Коэффициенты — {sig.match}</b>\n\n"
                f"Рынок: {sig.market}\n"
                f"Ставка: {sig.pick}\n"
                f"Коэф. букмекера: <b>{sig.book_odds:.2f}</b>\n"
                f"Fair-odds модели: <b>{sig.fair_odds:.2f}</b>\n"
                f"Вероятность: <b>{sig.probability*100:.1f}%</b>\n"
                f"Edge: <b>{sig.edge*100:+.1f}%</b>"
            )
        else:
            body = (
                f"🧠 <b>Анализ — {sig.match}</b>\n\n"
                f"Ставка: <b>{sig.pick}</b>\n"
                f"Рекомендация: {sig.recommended_stake:.0f} ₽\n\n"
                f"<b>Почему value:</b>\n{sig.reasoning or '—'}\n\n"
                f"<b>Риски:</b> {sig.risks or '—'}\n\n"
                f"🏠 Форма: <code>{sig.home_form or '— — — — —'}</code>\n"
                f"✈️ Форма: <code>{sig.away_form or '— — — — —'}</code>"
            )
        await q.message.reply_text(body, parse_mode=ParseMode.HTML)
        return


# ────────────────────────────────────────────────────────────────
# Текстовые сообщения (ожидаемые ответы)
# ────────────────────────────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    if ctx.user_data.pop("awaiting_bank", False):
        try:
            amount = float(text.replace(" ", "").replace(",", "."))
            assert amount >= 0
        except Exception:
            await update.message.reply_text("Не понял число. Попробуй ещё раз через кнопку 💰 Мой банк.")
            return
        await set_bank(user.id, amount)
        await update.message.reply_text(
            f"✅ Банк установлен: <b>{amount:,.0f} ₽</b>",
            parse_mode=ParseMode.HTML, reply_markup=main_menu(),
        )
        return

    if ctx.user_data.pop("awaiting_search", False):
        sigs = await find_signal_by_match(text)
        if not sigs:
            await update.message.reply_text(
                f"Прогнозов по «{text}» ещё не было. Нажми /scan для нового анализа.",
                reply_markup=main_menu(),
            )
            return
        await update.message.reply_text(
            _signals_list_text(sigs, title=f"🔍 Найдено по «{text}»"),
            parse_mode=ParseMode.HTML, reply_markup=main_menu(),
        )
        return

    # Фолбэк — показать меню
    await update.message.reply_text(
        "Не понял. Открой меню:",
        reply_markup=main_menu(),
    )


# ────────────────────────────────────────────────────────────────
# Фото — OCR чека
# ────────────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user.id, user.username or "", user.first_name or "")

    if not ctx.user_data.pop("awaiting_receipt", False):
        await update.message.reply_text(
            "📸 Это чек? Нажми 📤 <b>Загрузить чек</b> в меню, потом пришли фото.",
            parse_mode=ParseMode.HTML, reply_markup=main_menu(),
        )
        return

    if not update.message.photo:
        await update.message.reply_text("Не вижу фото. Пришли именно скриншот чека.")
        return

    await update.message.reply_text("🔍 Читаю скриншот через AI...")

    photo = update.message.photo[-1]
    tg_file = await ctx.bot.get_file(photo.file_id)
    image_bytes = bytes(await tg_file.download_as_bytearray())

    result = await parse_receipt(image_bytes)
    if not result or result["stake"] <= 0 or result["odds"] <= 1:
        await update.message.reply_text(
            "❌ Не смог распознать чек.\n"
            "Попробуй более чёткое фото: видны сумма, коэффициент, статус.",
            reply_markup=main_menu(),
        )
        return

    status = result["status"]
    stake = result["stake"]
    odds = result["odds"]
    match_name = result["match"] or "Ставка по скриншоту"

    bet_id = await add_bet(
        user_id=user.id,
        match=match_name,
        pick="—",
        odds=odds,
        stake=stake,
        source="screenshot",
        note=f"OCR payout={result.get('payout', 0)}",
    )
    bet = await close_bet(bet_id, status)
    if bet is None:
        await update.message.reply_text("❌ Не смог сохранить ставку.")
        return

    icon = {"win": "✅ Победа!", "loss": "❌ Проигрыш", "void": "↩️ Возврат"}[status]
    profit_str = f"{bet.profit:+.0f} ₽" if bet.profit else "0 ₽"
    await update.message.reply_text(
        f"{icon}\n\n"
        f"🏆 {match_name}\n"
        f"📊 Коэф: {odds:.2f}  |  Ставка: {stake:.0f} ₽\n"
        f"💵 Результат: <b>{profit_str}</b>\n"
        f"💰 Банк: {bet.bank_before:.0f} ₽ → <b>{bet.bank_after:.0f} ₽</b>",
        parse_mode=ParseMode.HTML, reply_markup=main_menu(),
    )


# ────────────────────────────────────────────────────────────────
# Помощники рендеринга
# ────────────────────────────────────────────────────────────────
def _stats_text(s: dict) -> str:
    sign = "📈" if s["roi"] >= 0 else "📉"
    return (
        "📊 <b>Статистика ставок</b>\n\n"
        f"Всего: <b>{s['total']}</b>  "
        f"(✅ {s['wins']} / ❌ {s['losses']} / ↩️ {s['voids']})\n"
        f"🏆 Винрейт: <b>{s['win_rate']}%</b>\n"
        f"{sign} ROI: <b>{s['roi']}%</b>\n"
        f"💵 Прибыль: <b>{s['profit']:+.2f} ₽</b>\n"
        f"📊 Средний коэф: <b>{s['avg_odds']:.2f}</b>\n"
        f"💸 Средняя ставка: <b>{s['avg_stake']:.0f} ₽</b>\n"
        f"💰 Банк: <b>{s['bank']:,.0f} ₽</b>"
    )


def _signals_list_text(sigs: list, title: str = "🔮 Прогнозы") -> str:
    lines = [f"<b>{title}</b>\n"]
    for s in sigs:
        icon = {"win": "✅", "loss": "❌", "void": "↩️", "pending": "🔵"}.get(s.status, "🔵")
        ko = s.kickoff.strftime("%d.%m %H:%M") if s.kickoff else "—"
        lines.append(
            f"{icon} <b>{s.match}</b>  <i>({s.league or '—'})</i>\n"
            f"   {s.pick}  @ {s.book_odds:.2f}  "
            f"| fair {s.fair_odds:.2f} | {int(s.probability*100)}%  | {ko}"
        )
    return "\n".join(lines)


async def _show_bank(q, user_id: int):
    bank = await get_bank(user_id)
    presets = [500, 1000, 2000, 5000, 10000, 25000]
    rows = [
        [InlineKeyboardButton(f"{p:,} ₽".replace(",", " "), callback_data=f"setbank_{p}")
         for p in presets[:3]],
        [InlineKeyboardButton(f"{p:,} ₽".replace(",", " "), callback_data=f"setbank_{p}")
         for p in presets[3:]],
        [InlineKeyboardButton("✏️ Своя сумма", callback_data="bank_custom")],
        [InlineKeyboardButton("◀ В меню", callback_data="back_menu")],
    ]
    await q.message.edit_text(
        f"💰 <b>Мой банк: {bank:,.0f} ₽</b>\n\n"
        "Выбери пресет или /setbank &lt;сумма&gt;",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _show_settings(q, user_id: int):
    st = await get_settings(user_id)
    risk = st.get("risk", "mid")
    kelly = st.get("kelly_cap", config.kelly_cap)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(("✅ " if risk == "low" else "") + "Консервативно (2%)",
                                 callback_data="risk_low"),
        ],
        [
            InlineKeyboardButton(("✅ " if risk == "mid" else "") + "Средне (5%)",
                                 callback_data="risk_mid"),
        ],
        [
            InlineKeyboardButton(("✅ " if risk == "high" else "") + "Агрессивно (8%)",
                                 callback_data="risk_high"),
        ],
        [InlineKeyboardButton("◀ В меню", callback_data="back_menu")],
    ])
    await q.message.edit_text(
        "⚙️ <b>Настройки риск-менеджмента</b>\n\n"
        f"Текущий профиль: <b>{risk}</b>\n"
        f"Лимит ставки: до <b>{kelly*100:.0f}%</b> банка\n\n"
        f"{DISCLAIMER}",
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )


async def _send_personal_signals(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    """Если не удалось опубликовать в канал (нет CHANNEL_ID) — шлём в личку."""
    sigs = await list_todays_signals()
    if not sigs:
        await ctx.bot.send_message(
            chat_id,
            "😐 Value-ставок сегодня не нашёл. Попробуй позже или смягчи настройки (⚙️).",
            reply_markup=main_menu(),
        )
        return
    for s in sigs[:3]:
        m = Match(
            home=s.match.split(" vs ")[0],
            away=s.match.split(" vs ")[-1],
            competition=s.league or "—",
            utc_date=s.kickoff.replace(tzinfo=timezone.utc) if s.kickoff else None,
        )
        from analysis import Pick
        pick = Pick(
            market=s.market, pick=s.pick, probability=s.probability,
            book_odds=s.book_odds, fair_odds=s.fair_odds, edge=s.edge,
            recommended_stake=s.recommended_stake,
        )
        text = format_signal_post(
            m, pick, s.reasoning or "—", s.risks or "—",
            s.home_form or "— — — — —", s.away_form or "— — — — —",
        )
        await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                   reply_markup=main_menu())


# ────────────────────────────────────────────────────────────────
# Регистрация хендлеров
# ────────────────────────────────────────────────────────────────
def build_application(token: str) -> Application:
    # Щадящие таймауты на случай медленного cold start HF Space.
    # IPv4-only форс живёт в webapp.py через monkey-patch socket.getaddrinfo.
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(35.0)
        .build()
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("menu",    cmd_start))
    app.add_handler(CommandHandler("setbank", cmd_setbank))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("scan",    cmd_scan))
    app.add_handler(CommandHandler("find",    cmd_find))

    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app
