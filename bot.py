"""
bot.py — Telegram handlers xG Master Bot v3.0
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

from ai import explain_pick, parse_receipt
from analysis import best_guess_pick, best_value_pick, poisson_probs, xg_from_odds
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

# Эмодзи для лиг
LEAGUE_EMOJI = {
    "Premier League": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "La Liga": "🇪🇸",
    "Bundesliga": "🇩🇪",
    "Serie A": "🇮🇹",
    "Ligue 1": "🇫🇷",
    "Eredivisie": "🇳🇱",
    "Championship": "🏴󠁧󠁢󠁥󠁮󠁧󠁿",
    "Primeira Liga": "🇵🇹",
    "Champions League": "🏆",
    "Europa League": "🌍",
    "World Cup": "🌎",
    "Euro": "🇪🇺",
}

def league_emoji(name: str) -> str:
    for k, v in LEAGUE_EMOJI.items():
        if k.lower() in name.lower():
            return v
    return "⚽"


# ── Клавиатуры ───────────────────────────────────────────────────
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


# ── Команды ──────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user.id, user.username or "", user.first_name or "")
    await update.message.reply_text(
        "⚽ <b>xG Master Bot</b>\n\n"
        "Ищу value-ставки по топ-лигам. Считаю Poisson + Kelly, "
        "AI-комментарий через Gemini.\n\n"
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


# ── Callback router ───────────────────────────────────────────────
async def callback_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user = update.effective_user
    await ensure_user(user.id, user.username or "", user.first_name or "")
    data = q.data or ""

    # ── Назад в меню
    if data == "back_menu":
        await q.message.edit_text(
            "⚽ <b>xG Master Bot</b>\n\nВыбери действие:",
            parse_mode=ParseMode.HTML, reply_markup=main_menu(),
        )
        return

    # ── Банк
    if data == "menu_bank":
        await _show_bank(q, user.id); return
    if data.startswith("setbank_"):
        amount = float(data.split("_", 1)[1])
        await set_bank(user.id, amount)
        await q.message.edit_text(
            f"✅ Банк установлен: <b>{amount:,.0f} ₽</b>",
            parse_mode=ParseMode.HTML, reply_markup=back_button(),
        ); return
    if data == "bank_custom":
        ctx.user_data["awaiting_bank"] = True
        await q.message.edit_text(
            "✏️ Пришли сумму банка числом (например: 7500)",
            reply_markup=back_button(),
        ); return

    # ── Статистика
    if data == "menu_stats":
        s = await stats_for_user(user.id)
        await q.message.edit_text(
            _stats_text(s), parse_mode=ParseMode.HTML, reply_markup=back_button(),
        ); return

    # ── Загрузить чек
    if data == "menu_upload":
        ctx.user_data["awaiting_receipt"] = True
        await q.message.reply_text(
            "📸 Пришли скриншот чека ставки — AI прочитает результат и "
            "обновит банк автоматически."
        ); return

    # ── Настройки
    if data == "menu_settings":
        await _show_settings(q, user.id); return
    if data.startswith("risk_"):
        level = data.split("_", 1)[1]
        cap_map = {"low": 0.02, "mid": 0.05, "high": 0.08}
        await update_settings(user.id, {"risk": level, "kelly_cap": cap_map.get(level, 0.05)})
        await _show_settings(q, user.id); return

    # ── Матчи дня
    if data == "menu_matches":
        await _show_matches_day(q); return

    # ── Прогнозы
    if data == "menu_signals":
        await _show_signals(q, user.id); return

    # ── Найти матч → показать лиги
    if data == "menu_search":
        await _show_leagues(q, ctx); return

    # ── Выбрана лига → показать матчи
    if data.startswith("league_"):
        idx = int(data.split("_", 1)[1])
        await _show_league_matches(q, ctx, idx); return

    # ── Выбран матч → запустить анализ
    if data.startswith("matchpick_"):
        idx = int(data.split("_", 1)[1])
        await _analyze_match(q, ctx, user.id); return

    # ── Ретро-отчёт
    if data == "menu_retro":
        await _show_retro(q, user.id); return

    # ── Детали сигнала
    if data.startswith("sig_analysis_") or data.startswith("sig_odds_"):
        sig_id = int(data.rsplit("_", 1)[1])
        sig = await get_signal(sig_id)
        if not sig:
            await q.message.reply_text("Сигнал не найден."); return
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
        await q.message.reply_text(body, parse_mode=ParseMode.HTML); return


# ── Матчи дня (красивое форматирование) ─────────────────────────
async def _show_matches_day(q):
    await q.message.edit_text("⏳ Загружаю расписание...", reply_markup=back_button())
    matches = await fetch_matches()
    if not matches:
        await q.message.edit_text("📭 На сегодня матчей не найдено.", reply_markup=back_button())
        return

    # Группируем по лиге
    by_league: dict[str, list[Match]] = {}
    for m in matches:
        by_league.setdefault(m.competition, []).append(m)

    lines = ["📅 <b>Матчи сегодня</b>\n"]
    for league, ms in list(by_league.items())[:8]:
        emoji = league_emoji(league)
        lines.append(f"\n{emoji} <b>{league}</b>")
        lines.append("─" * 20)
        for m in ms[:5]:
            ts = m.utc_date.strftime("%H:%M") if m.utc_date else "--:--"
            lines.append(f"🕐 <b>{ts} UTC</b>")
            lines.append(f"   {m.home} 🆚 {m.away}")
        if len(ms) > 5:
            lines.append(f"   <i>...ещё {len(ms)-5} матчей</i>")

    lines.append(f"\n<i>Всего матчей: {len(matches)}</i>")
    lines.append("\n💡 <i>Нажми /scan для анализа value-ставок</i>")

    await q.message.edit_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_button()
    )


# ── Прогнозы ────────────────────────────────────────────────────
async def _show_signals(q, user_id: int):
    await q.message.edit_text("⏳ Ищу прогнозы...", reply_markup=back_button())

    sigs = await list_todays_signals()
    if not sigs:
        sigs = await list_signals(limit=8)

    if sigs:
        body = _signals_list_text(sigs, title="🔮 Прогнозы")
        await q.message.edit_text(body, parse_mode=ParseMode.HTML, reply_markup=back_button())
        return

    # Прогнозов нет — запускаем быстрый анализ
    await q.message.edit_text(
        "🔍 Прогнозов ещё нет. Запускаю быстрый анализ матчей...",
        reply_markup=back_button()
    )
    from scanner import scan_best_guesses
    bank = await get_bank(user_id) or 10000.0
    guesses = await scan_best_guesses(bank, limit=15)

    if not guesses:
        await q.message.edit_text(
            "😐 Сегодня нет матчей с явным value.\n"
            "Попробуй позже или запусти /scan вручную.",
            reply_markup=back_button()
        )
        return

    lines = ["🔮 <b>Лучшие прогнозы на сегодня</b>\n"]
    for g in guesses:
        m = g["match"]
        p = g["pick"]
        emoji = league_emoji(m.competition)
        ts = m.utc_date.strftime("%H:%M UTC") if m.utc_date else ""
        lines += [
            f"\n{emoji} <b>{m.competition}</b>",
            f"⚽ <b>{m.home} 🆚 {m.away}</b>  {ts}",
            f"📌 Ставка: <b>{p.pick}</b> @ <b>{p.book_odds:.2f}</b>",
            f"📊 Вероятность: <b>{int(p.probability*100)}%</b>  "
            f"| Fair: {p.fair_odds:.2f}  | Edge: {p.edge*100:+.1f}%",
            "─" * 25,
        ]

    lines.append(f"\n{DISCLAIMER}")
    await q.message.edit_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_button()
    )


# ── Найти матч → Показать лиги ────────────────────────────────────
async def _show_leagues(q, ctx):
    await q.message.edit_text("⏳ Загружаю лиги...", reply_markup=back_button())
    matches = await fetch_matches()
    if not matches:
        await q.message.edit_text("📭 Матчей не найдено.", reply_markup=back_button())
        return

    # Группируем по лиге
    by_league: dict[str, list[Match]] = {}
    for m in matches:
        by_league.setdefault(m.competition, []).append(m)

    # Сохраняем матчи в контексте
    ctx.bot_data["search_leagues"] = list(by_league.keys())
    ctx.bot_data["search_matches"] = matches

    rows = []
    for i, (league, ms) in enumerate(by_league.items()):
        emoji = league_emoji(league)
        rows.append([InlineKeyboardButton(
            f"{emoji} {league} ({len(ms)})",
            callback_data=f"league_{i}"
        )])
    rows.append([InlineKeyboardButton("◀ В меню", callback_data="back_menu")])

    await q.message.edit_text(
        "🔍 <b>Выбери лигу:</b>\n\n"
        "<i>Нажми на лигу — увидишь матчи дня.\n"
        "Нажми на матч — получишь прогноз ставки.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows)
    )


# ── Выбрана лига → Показать матчи ────────────────────────────────
async def _show_league_matches(q, ctx, league_idx: int):
    leagues = ctx.bot_data.get("search_leagues", [])
    all_matches = ctx.bot_data.get("search_matches", [])

    if not leagues or league_idx >= len(leagues):
        await q.message.edit_text("⚠️ Данные устарели. Попробуй снова.", reply_markup=back_button())
        return

    league = leagues[league_idx]
    matches = [m for m in all_matches if m.competition == league]

    # Сохраняем текущие матчи лиги для выбора
    ctx.bot_data["current_league_matches"] = matches
    ctx.bot_data["current_league_idx"] = league_idx

    emoji = league_emoji(league)
    rows = []
    for i, m in enumerate(matches[:10]):
        ts = m.utc_date.strftime("%H:%M") if m.utc_date else "--:--"
        rows.append([InlineKeyboardButton(
            f"🕐{ts} · {m.home} 🆚 {m.away}",
            callback_data=f"matchpick_{i}"
        )])
    rows.append([InlineKeyboardButton("◀ К лигам", callback_data="menu_search")])
    rows.append([InlineKeyboardButton("◀ В меню", callback_data="back_menu")])

    await q.message.edit_text(
        f"{emoji} <b>{league}</b>\n\n"
        "Выбери матч для анализа ставки:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows)
    )


# ── Выбран матч → Анализ ─────────────────────────────────────────
async def _analyze_match(q, ctx, user_id: int):
    # Получаем индекс матча
    data = q.data or ""
    idx = int(data.split("_", 1)[1])

    matches = ctx.bot_data.get("current_league_matches", [])
    if not matches or idx >= len(matches):
        await q.message.edit_text("⚠️ Данные устарели. Попробуй снова.", reply_markup=back_button())
        return

    match = matches[idx]
    ts = match.utc_date.strftime("%d.%m %H:%M UTC") if match.utc_date else ""
    emoji = league_emoji(match.competition)

    await q.message.edit_text(
        f"{emoji} <b>{match.competition}</b>\n"
        f"⚽ <b>{match.home} 🆚 {match.away}</b>  {ts}\n\n"
        "⏳ Анализирую матч...",
        parse_mode=ParseMode.HTML,
        reply_markup=back_button()
    )

    try:
        bank = await get_bank(user_id) or 10000.0
        odds = await fetch_odds(match.home, match.away)

        if not odds or not odds.has_1x2():
            await q.message.edit_text(
                f"{emoji} <b>{match.home} 🆚 {match.away}</b>\n\n"
                "❌ Коэффициенты на этот матч не найдены.\n"
                "<i>Попробуй другой матч или дождись публикации линии.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_button("menu_search")
            )
            return

        home_xg, away_xg = xg_from_odds(odds, match.competition)
        model = poisson_probs(home_xg, away_xg)

        # Пробуем строгий pick, потом мягкий
        pick = best_value_pick(match.home, match.away, odds, model, bank) \
               or best_guess_pick(match.home, match.away, odds, model, bank)

        if not pick:
            await q.message.edit_text(
                f"{emoji} <b>{match.home} 🆚 {match.away}</b>\n\n"
                "😐 Value-ставки на этот матч не найдено.\n"
                f"📊 Модель: Д {model['home']*100:.0f}% | Н {model['draw']*100:.0f}% | "
                f"А {model['away']*100:.0f}%\n\n"
                "<i>Коэффициенты букмекеров не дают преимущества.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_button("menu_search")
            )
            return

        # Получаем AI-комментарий
        meta = await explain_pick(
            match.home, match.away, match.competition,
            pick.pick, pick.probability, pick.book_odds, pick.fair_odds,
        )

        stake = pick.recommended_stake
        lines = [
            f"{emoji} <b>{match.competition}</b>",
            f"⚽ <b>{match.home} 🆚 {match.away}</b>  {ts}",
            "",
            f"📌 <b>Ставка: {pick.pick}</b>",
            f"💰 Коэффициент: <b>{pick.book_odds:.2f}</b>",
            f"📊 Fair odds: {pick.fair_odds:.2f}  |  Edge: <b>{pick.edge*100:+.1f}%</b>",
            f"🎯 Вероятность: <b>{int(pick.probability*100)}%</b>",
            f"💵 Рекомендуемая ставка: <b>{stake:.0f} ₽</b>",
            "",
            "📈 <b>Модель Пуассона:</b>",
            f"   🏠 П1: {model['home']*100:.0f}%  |  Х: {model['draw']*100:.0f}%  |  П2: {model['away']*100:.0f}%",
            f"   Тотал >2.5: {model['over_2_5']*100:.0f}%  |  BTTS: {model['btts_yes']*100:.0f}%",
        ]

        if meta.get("reasoning"):
            lines += ["", "🧠 <b>Анализ:</b>", meta["reasoning"]]
        if meta.get("risks"):
            lines += ["", f"⚠️ <b>Риски:</b> {meta['risks']}"]

        lines += ["", DISCLAIMER]

        await q.message.edit_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Другой матч", callback_data="menu_search")],
                [InlineKeyboardButton("◀ В меню", callback_data="back_menu")],
            ])
        )

    except Exception as e:
        log.exception("analyze_match failed")
        await q.message.edit_text(
            f"❌ Ошибка анализа: {e}",
            reply_markup=back_button("menu_search")
        )


# ── Ретро-отчёт ──────────────────────────────────────────────────
async def _show_retro(q, user_id: int):
    bets = await retro_report(user_id, limit=15)
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


# ── Текстовые сообщения ──────────────────────────────────────────
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

    await update.message.reply_text("Открой меню:", reply_markup=main_menu())


# ── Фото — OCR чека ──────────────────────────────────────────────
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
        user_id=user.id, match=match_name, pick="—", odds=odds, stake=stake,
        source="screenshot", note=f"OCR payout={result.get('payout', 0)}",
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


# ── Helpers ──────────────────────────────────────────────────────
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
        emoji = league_emoji(s.league or "")
        lines.append(
            f"{icon} {emoji} <b>{s.match}</b>\n"
            f"   📌 {s.pick}  @ <b>{s.book_odds:.2f}</b>  "
            f"| {int(s.probability*100)}%  | ⏰ {ko}\n"
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
    desc = {
        "low":  "Консервативно — ставишь до 2% банка. Медленный рост, минимальный риск.",
        "mid":  "Средне — до 5% банка. Баланс между ростом и риском.",
        "high": "Агрессивно — до 8% банка. Высокий потенциал и высокий риск.",
    }
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            ("✅ " if risk == "low" else "") + "Консервативно (2%)", callback_data="risk_low")],
        [InlineKeyboardButton(
            ("✅ " if risk == "mid" else "") + "Средне (5%)", callback_data="risk_mid")],
        [InlineKeyboardButton(
            ("✅ " if risk == "high" else "") + "Агрессивно (8%)", callback_data="risk_high")],
        [InlineKeyboardButton("◀ В меню", callback_data="back_menu")],
    ])
    await q.message.edit_text(
        "⚙️ <b>Настройки риск-менеджмента</b>\n\n"
        f"Текущий профиль: <b>{risk}</b>\n"
        f"Лимит ставки: до <b>{kelly*100:.0f}%</b> от банка\n\n"
        f"<i>{desc[risk]}</i>\n\n"
        "Выбери профиль риска:\n"
        "• <b>Консервативно</b> — для новичков и осторожных\n"
        "• <b>Средне</b> — стандартный Kelly\n"
        "• <b>Агрессивно</b> — опытные игроки\n\n"
        f"{DISCLAIMER}",
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )


async def _send_personal_signals(ctx, chat_id: int, user_id: int):
    from scanner import scan_best_guesses
    bank = await get_bank(user_id) or 10000.0
    guesses = await scan_best_guesses(bank, limit=15)

    if not guesses:
        await ctx.bot.send_message(
            chat_id,
            "😐 Value-ставок сегодня не нашёл. Попробуй позже.",
            reply_markup=main_menu(),
        )
        return

    for g in guesses[:3]:
        m = g["match"]
        p = g["pick"]
        emoji = league_emoji(m.competition)
        ts = m.utc_date.strftime("%H:%M UTC") if m.utc_date else ""
        lines = [
            f"{emoji} <b>{m.competition}</b>",
            f"⚽ <b>{m.home} 🆚 {m.away}</b>  {ts}",
            f"📌 Ставка: <b>{p.pick}</b> @ <b>{p.book_odds:.2f}</b>",
            f"🎯 Вероятность: <b>{int(p.probability*100)}%</b>  | Edge: {p.edge*100:+.1f}%",
            f"💵 Рекомендация: <b>{p.recommended_stake:.0f} ₽</b>",
            "",
            DISCLAIMER,
        ]
        await ctx.bot.send_message(
            chat_id, "\n".join(lines),
            parse_mode=ParseMode.HTML, reply_markup=main_menu()
        )


# ── Сборка приложения ────────────────────────────────────────────
def build_application(token: str, proxy: str = "") -> Application:
    builder = Application.builder().token(token)

    if proxy:
        base_url = proxy.rstrip("/") + "/bot"
        base_file_url = proxy.rstrip("/") + "/file/bot"
        builder = builder.base_url(base_url).base_file_url(base_file_url)

    builder = (builder
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(35.0))

    app = builder.build()
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