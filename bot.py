"""
bot.py — Telegram handlers xG Master Bot v3.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone


# Все матчи отображаем по Москве (UTC+3) — для русскоязычной аудитории
MSK_TZ = timezone(timedelta(hours=3))


def _fmt_msk(dt, with_date: bool = False) -> str:
    """utc datetime → строка в МСК. Принимает naive (трактуем как UTC)
    и aware datetime."""
    if dt is None:
        return "—:—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    msk = dt.astimezone(MSK_TZ)
    return msk.strftime("%d.%m %H:%M МСК") if with_date else msk.strftime("%H:%M МСК")

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
from channel import DISCLAIMER as CAPPER_DISCLAIMER, format_signal_post, publish_signal
from config import config
from data_sources import Match, fetch_matches, fetch_odds, fetch_team_form
from db import (
    add_bet, aggregate_signal_breakdown, aggregate_signal_stats, close_bet,
    ensure_user, find_signal_by_match, get_bank,
    get_cached_match, get_settings, get_signal, list_cached_matches_for_date,
    list_signals, list_todays_signals,
    retro_report, save_signal, set_bank,
    settled_signals_for_msk_date, settled_signals_in_range,
    stats_for_user, update_settings,
)
from scanner import _prepare_match_cache, scan_and_publish, warmup_match_cache
from settlement import manual_settle

log = logging.getLogger(__name__)

DISCLAIMER = CAPPER_DISCLAIMER

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
    "UEFA Champions League": "🏆",
    "Champions League": "🏆",
    "UEFA Europa League": "🥈",
    "Europa League": "🥈",
    "UEFA Europa Conference League": "🥉",
    "Conference League": "🥉",
    "World Cup": "🌎",
    "Euro": "🇪🇺",
}

LEAGUE_TITLES_RU = {
    "Premier League": "Англия. Премьер-лига",
    "EPL": "Англия. Премьер-лига",
    "La Liga": "Испания. Ла Лига",
    "Bundesliga": "Германия. Бундеслига",
    "Serie A": "Италия. Серия А",
    "Ligue 1": "Франция. Лига 1",
    "Eredivisie": "Нидерланды. Эредивизи",
    "Primeira Liga": "Португалия. Примейра",
    "Championship": "Англия. Чемпионшип",
    "UEFA Champions League": "Лига чемпионов УЕФА",
    "Champions League": "Лига чемпионов УЕФА",
    "UEFA Europa League": "Лига Европы УЕФА",
    "Europa League": "Лига Европы УЕФА",
    "UEFA Europa Conference League": "Лига конференций УЕФА",
    "Conference League": "Лига конференций УЕФА",
}

def league_emoji(name: str) -> str:
    for k, v in LEAGUE_EMOJI.items():
        if k.lower() in name.lower():
            return v
    return "⚽"


def league_title_ru(name: str) -> str:
    return LEAGUE_TITLES_RU.get(name, name or "Неизвестная лига")


def form_ru(form: str) -> str:
    mapping = {"W": "В", "D": "Н", "L": "П", "—": "—"}
    parts = [mapping.get(part, part) for part in (form or "").split()]
    return " ".join(parts) if parts else "— — — — —"


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
        "Я считаю value-ставки по топ-лигам. В основе — Пуассон с Dixon-Coles "
        "поправкой, форма команд и медиана букмекерских коэфов. Когда "
        "линия букмекера расходится с моей оценкой — ловлю расхождение "
        "и считаю размер ставки по Kelly.\n\n"
        "Что умею:\n"
        "• 📅 показать матчи дня с коэфами\n"
        "• 🔮 выкатить прогнозы с пояснением «почему беру»\n"
        "• 📤 принять скриншот чека и сам обновить банк\n"
        "• 📊 вести статистику с ROI и винрейтом\n\n"
        f"{DISCLAIMER}\n\n"
        "С чего начнём?",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Что я понимаю:</b>\n"
        "/start — главное меню\n"
        "/setbank 5000 — закрепить банк (без него Kelly считать не от чего)\n"
        "/scan — прогнать матчи прямо сейчас\n"
        "/stats — твоя статистика: винрейт, ROI, прибыль\n"
        "/find Реал — поиск сигнала по команде\n",
        parse_mode=ParseMode.HTML,
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
        f"✅ Банк закреплён: <b>{amount:,.0f} ₽</b>\n"
        f"<i>Размер ставок Kelly теперь считаю от этой суммы.</i>",
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
    await ctx.bot.send_message(chat_id, "🔍 Прогоняю матчи через модель — секунду.")
    try:
        bank = await get_bank(user_id) or 10000.0
        published = await scan_and_publish(ctx.bot, bank)
        if published == 0:
            await _send_personal_signals(ctx, chat_id, user_id)
        else:
            await ctx.bot.send_message(
                chat_id,
                f"✅ В канал ушло прогнозов: <b>{published}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu(),
            )
    except Exception as e:
        log.exception("scan failed")
        await ctx.bot.send_message(chat_id, f"❌ Скан упал: {e}")


async def cmd_settle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Ручной override результата сигнала.
    /settle <signal_id> <win|loss|void> [home_goals:away_goals]
    Доступно только ADMIN_ID.
    """
    user_id = update.effective_user.id
    if not config.admin_id or user_id != config.admin_id:
        await update.message.reply_text("Команда доступна только админу.")
        return
    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Использование: <code>/settle 42 win</code> "
            "или <code>/settle 42 win 2:1</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        signal_id = int(args[0])
    except ValueError:
        await update.message.reply_text("signal_id должен быть числом.")
        return
    verdict = args[1].lower()
    if verdict not in ("win", "loss", "void"):
        await update.message.reply_text("verdict ∈ {win, loss, void}")
        return
    home_goals = away_goals = None
    if len(args) >= 3 and ":" in args[2]:
        try:
            h, a = args[2].split(":", 1)
            home_goals = int(h)
            away_goals = int(a)
        except ValueError:
            await update.message.reply_text("Счёт в формате 2:1")
            return
    sig = await get_signal(signal_id)
    if not sig:
        await update.message.reply_text(f"Сигнал #{signal_id} не найден.")
        return
    await manual_settle(sig, verdict, home_goals=home_goals, away_goals=away_goals)
    score = f" ({home_goals}:{away_goals})" if home_goals is not None else ""
    await update.message.reply_text(
        f"✅ Сигнал #{signal_id} закрыт как <b>{verdict}</b>{score}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = " ".join(ctx.args or []).strip()
    if not query:
        await update.message.reply_text("Использование: <code>/find Реал</code>",
                                        parse_mode=ParseMode.HTML)
        return
    sigs = await find_signal_by_match(query)
    if not sigs:
        await update.message.reply_text(
            f"По «{query}» в моей базе сигналов нет. "
            f"Возможно, матч ещё не разбирал — попробуй позже или /scan."
        )
        return
    await update.message.reply_text(
        _signals_list_text(sigs, title=f"🔍 По запросу «{query}»"),
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
            "⚽ <b>xG Master Bot</b>\n\nС чего продолжаем?",
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
            f"✅ Банк закреплён: <b>{amount:,.0f} ₽</b>\n"
            f"<i>Размер ставок Kelly теперь считаю от этой суммы.</i>",
            parse_mode=ParseMode.HTML, reply_markup=back_button(),
        ); return
    if data == "bank_custom":
        ctx.user_data["awaiting_bank"] = True
        await q.message.edit_text(
            "✏️ Пришли сумму банка числом (например: 7500).",
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
            "📸 Кидай скриншот чека — AI прочитает сумму, коэф и статус, "
            "сам обновлю банк и добавлю ставку в историю."
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

    # ── Ретро-отчёт: главное меню
    if data == "menu_retro":
        await _show_retro_menu(q); return
    if data == "retro_yesterday":
        await _show_retro_yesterday(q); return
    if data == "retro_period_7":
        await _show_retro_period(q, days=7); return
    if data == "retro_period_30":
        await _show_retro_period(q, days=30); return
    if data == "retro_markets":
        await _show_retro_breakdown(q, group_by="market"); return
    if data == "retro_leagues":
        await _show_retro_breakdown(q, group_by="league"); return
    if data == "retro_clv":
        await _show_retro_clv(q); return
    if data == "retro_mybets":
        await _show_retro(q, user.id); return

    # ── Детали сигнала
    if data.startswith("sig_analysis_") or data.startswith("sig_odds_"):
        sig_id = int(data.rsplit("_", 1)[1])
        sig = await get_signal(sig_id)
        if not sig:
            await q.message.reply_text("Этого сигнала уже нет в базе."); return
        if data.startswith("sig_odds_"):
            market_prob = (
                float(sig.market_probability or 0.0)
                if getattr(sig, "market_probability", 0.0)
                else (1 / sig.book_odds if sig.book_odds > 1 else 0.0)
            )
            gap = (sig.probability - market_prob) * 100
            body = (
                f"📊 <b>Коэффициенты — {sig.match}</b>\n\n"
                f"Рынок: {sig.market}\n"
                f"Ставка: <b>{sig.pick}</b>\n"
                f"💰 Букмекер: <b>{sig.book_odds:.2f}</b>  ·  "
                f"🧮 Моя цена: <b>{sig.fair_odds:.2f}</b>\n"
                f"📈 рынок {market_prob*100:.0f}%, моя оценка {sig.probability*100:.0f}% "
                f"({gap:+.1f} п.п.)"
            )
        else:
            cached = None
            if sig.kickoff:
                match_key = f"{sig.kickoff.strftime('%Y%m%d%H%M')}:{sig.match.split(' vs ')[0].lower()}:{sig.match.split(' vs ')[-1].lower()}:"
                cached = await get_cached_match(match_key)
            body = (
                f"🧠 <b>Разбор — {sig.match}</b>\n\n"
                f"📌 <b>Беру:</b> {sig.pick}\n"
                f"💵 <b>Размер ставки:</b> {sig.recommended_stake:.0f} ₽\n\n"
                f"<b>Почему беру:</b>\n{sig.reasoning or '—'}\n\n"
                f"<b>Что смущает:</b> {sig.risks or '—'}\n\n"
                f"🏠 Хозяева: <code>{form_ru(sig.home_form or '— — — — —')}</code>  "
                f"·  ✈️ Гости: <code>{form_ru(sig.away_form or '— — — — —')}</code>\n\n"
                f"🩺 Кадры: {_render_cached_list(getattr(cached, 'injuries', '[]'), 2)}\n"
                f"📌 Факты: {_render_cached_list(getattr(cached, 'facts', '[]'), 3)}\n"
                f"📊 Статистика: {_render_cached_list(getattr(cached, 'stats', '[]'), 3)}"
            )
        await q.message.reply_text(body, parse_mode=ParseMode.HTML); return


# ── Матчи дня (красивое форматирование) ─────────────────────────
async def _show_matches_day(q):
    await q.message.edit_text("⏳ Подтягиваю линию...", reply_markup=back_button())
    matches = await _matches_for_today_view()
    if not matches:
        await q.message.edit_text(
            "📭 На сегодня тишина — расписание пустое или буки ещё не выкатили линию.",
            reply_markup=back_button(),
        )
        return

    by_league: dict[str, list] = {}
    for m in matches:
        league = getattr(m, "league", "") or getattr(m, "competition", "") or "—"
        by_league.setdefault(league, []).append(m)

    lines = ["📅 <b>Что играем сегодня</b>\n"]
    for league, ms in list(by_league.items())[:8]:
        emoji = league_emoji(league)
        lines.append(f"\n{emoji} <b>{league_title_ru(league)}</b>")
        lines.append("─" * 20)
        for m in ms[:5]:
            lines.append(_render_match_card(m))
        if len(ms) > 5:
            lines.append(f"   <i>...и ещё {len(ms)-5} матчей в этой лиге</i>")

    lines.append(f"\n<i>Всего на тарелке: {len(matches)} матчей.</i>")
    lines.append("💡 <i>/scan — прогнать через модель и достать value.</i>")

    await q.message.edit_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_button()
    )


# ── Прогнозы ────────────────────────────────────────────────────
async def _show_signals(q, user_id: int):
    await q.message.edit_text("⏳ Достаю свежие прогнозы...", reply_markup=back_button())

    sigs = await list_todays_signals()
    if not sigs:
        sigs = await list_signals(limit=8)

    if sigs:
        body = _signals_list_text(sigs, title="🔮 Что взял в работу")
        await q.message.edit_text(body, parse_mode=ParseMode.HTML, reply_markup=back_button())
        return

    # Прогнозов нет — запускаем быстрый анализ
    await q.message.edit_text(
        "🔍 Готовых прогнозов нет. Прохожусь по линии прямо сейчас...",
        reply_markup=back_button()
    )
    from scanner import scan_best_guesses
    bank = await get_bank(user_id) or 10000.0
    guesses = await scan_best_guesses(bank, limit=15)

    if not guesses:
        await q.message.edit_text(
            "😐 Сегодня линия плотная — заметного перевеса не вижу. "
            "Бывает, и это нормально: лучше пропустить день, чем ставить на минимальном edge.\n\n"
            "Загляни попозже или запусти /scan.",
            reply_markup=back_button()
        )
        return

    lines = ["🔮 <b>Что беру сегодня</b>"]
    for g in guesses[:3]:
        m = g["match"]
        p = g["pick"]
        model = g.get("model") or {}
        emoji = league_emoji(m.competition)
        ts = _fmt_msk(m.utc_date) if m.utc_date else ""
        market_prob = p.market_probability if p.market_probability > 0 \
            else (1 / p.book_odds if p.book_odds > 1 else 0.0)
        gap = (p.probability - market_prob) * 100
        block = [
            "",
            f"{emoji} <b>{league_title_ru(m.competition)}</b>",
            f"⚽ <b>{m.home} — {m.away}</b>  ·  🕐 {ts}",
            "",
            f"📌 <b>Беру:</b> {p.pick}",
            f"💰 <b>Коэф букмекера:</b> {p.book_odds:.2f}",
            f"🧮 <b>Моя цена:</b> {p.fair_odds:.2f}",
            f"📊 <b>Преимущество:</b> {p.edge*100:+.1f}%  ·  "
            f"📈 рынок {market_prob*100:.0f}% → модель {p.probability*100:.0f}% "
            f"({gap:+.1f} п.п.)",
            f"💵 <b>Размер ставки:</b> {p.recommended_stake:.0f} ₽",
        ]
        if model:
            block += [
                "",
                "📈 <b>Раскладка модели:</b>",
                f"   П1 <b>{model['home']*100:.0f}%</b>  ·  "
                f"Х <b>{model['draw']*100:.0f}%</b>  ·  "
                f"П2 <b>{model['away']*100:.0f}%</b>",
                f"   ТБ 2.5 <b>{model['over_2_5']*100:.0f}%</b>  ·  "
                f"ТМ 2.5 <b>{model['under_2_5']*100:.0f}%</b>",
                f"   BTTS Да <b>{model['btts_yes']*100:.0f}%</b>  ·  "
                f"Нет <b>{model['btts_no']*100:.0f}%</b>",
            ]
        block.append("─" * 25)
        lines += block

    lines.append("")
    lines.append(DISCLAIMER)
    await q.message.edit_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_button()
    )


# ── Найти матч → Показать лиги ────────────────────────────────────
async def _show_leagues(q, ctx):
    await q.message.edit_text("⏳ Подтягиваю лиги...", reply_markup=back_button())
    matches = await fetch_matches()
    if not matches:
        await q.message.edit_text(
            "📭 Расписание пустое — матчей сегодня не вижу.",
            reply_markup=back_button(),
        )
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
        "<i>Тыкни лигу → увидишь матчи дня. Тыкни матч → прогоню через модель "
        "и скажу, есть ли там value.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows)
    )


# ── Выбрана лига → Показать матчи ────────────────────────────────
async def _show_league_matches(q, ctx, league_idx: int):
    leagues = ctx.bot_data.get("search_leagues", [])
    all_matches = ctx.bot_data.get("search_matches", [])

    if not leagues or league_idx >= len(leagues):
        await q.message.edit_text(
            "⚠️ Список лиг устарел — открой «Найти матч» заново.",
            reply_markup=back_button(),
        )
        return

    league = leagues[league_idx]
    matches = [m for m in all_matches if m.competition == league]

    # Сохраняем текущие матчи лиги для выбора
    ctx.bot_data["current_league_matches"] = matches
    ctx.bot_data["current_league_idx"] = league_idx

    emoji = league_emoji(league)
    rows = []
    for i, m in enumerate(matches[:10]):
        ts = _fmt_msk(m.utc_date).split(" ")[0] if m.utc_date else "--:--"
        rows.append([InlineKeyboardButton(
            f"🕐{ts} · {m.home} 🆚 {m.away}",
            callback_data=f"matchpick_{i}"
        )])
    rows.append([InlineKeyboardButton("◀ К лигам", callback_data="menu_search")])
    rows.append([InlineKeyboardButton("◀ В меню", callback_data="back_menu")])

    await q.message.edit_text(
        f"{emoji} <b>{league_title_ru(league)}</b>\n\n"
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
        await q.message.edit_text(
            "⚠️ Список матчей устарел — открой лигу заново.",
            reply_markup=back_button(),
        )
        return

    match = matches[idx]
    ts = _fmt_msk(match.utc_date, with_date=True) if match.utc_date else ""
    emoji = league_emoji(match.competition)

    await q.message.edit_text(
        f"{emoji} <b>{league_title_ru(match.competition)}</b>\n"
        f"⚽ <b>{match.home} — {match.away}</b>\n"
        f"🕐 {ts}\n\n"
        "⏳ Прогоняю через модель...",
        parse_mode=ParseMode.HTML,
        reply_markup=back_button()
    )

    try:
        bank = await get_bank(user_id) or 10000.0
        odds, cached = await _prepare_match_cache(match)

        if not odds or not odds.has_1x2():
            await q.message.edit_text(
                f"{emoji} <b>{match.home} 🆚 {match.away}</b>\n\n"
                "❌ Линию по этому матчу не нашёл.\n"
                "<i>Попробуй другой матч или дождись, когда буки выкатят котировки.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_button("menu_search")
            )
            return

        # Тянем форму обеих команд (кэш на 6ч → почти бесплатно при повторе)
        home_form_obj, away_form_obj = await asyncio.gather(
            fetch_team_form(match.home),
            fetch_team_form(match.away),
        )
        home_xg, away_xg = xg_from_odds(odds, match.competition,
                                        home_form_obj, away_form_obj)
        model = poisson_probs(home_xg, away_xg)

        pick = best_value_pick(match.home, match.away, odds, model, bank) \
               or best_guess_pick(match.home, match.away, odds, model, bank)

        if not pick:
            await q.message.edit_text(
                f"{emoji} <b>{match.home} 🆚 {match.away}</b>\n\n"
                "😐 На этот матч ставку не нашёл — линия и моя оценка слишком близко.\n"
                f"📊 По модели: П1 {model['home']*100:.0f}% · Х {model['draw']*100:.0f}% · "
                f"П2 {model['away']*100:.0f}% · ТБ 2.5 {model['over_2_5']*100:.0f}% · "
                f"BTTS {model['btts_yes']*100:.0f}%\n\n"
                "<i>Букмекер закрыл цену — ждём матч получше.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=back_button("menu_search")
            )
            return

        meta = await explain_pick(
            match.home, match.away, match.competition,
            pick.pick, pick.probability, pick.book_odds, pick.fair_odds,
            extra_context=_cached_match_context(cached),
        )

        market_prob = pick.market_probability if pick.market_probability > 0 \
            else (1 / pick.book_odds if pick.book_odds > 1 else 0.0)
        gap = (pick.probability - market_prob) * 100
        confidence_word = (
            "🔥 высокая" if pick.probability >= 0.65 else
            "✅ уверенная" if pick.probability >= 0.55 else
            "⚖️ средняя"   if pick.probability >= 0.45 else
            "🪙 осторожный заход"
        )

        stake = pick.recommended_stake
        lines = [
            f"{emoji} <b>{league_title_ru(match.competition)}</b>",
            f"⚽ <b>{match.home} — {match.away}</b>",
            f"🕐 {ts}",
            "",
            f"📌 <b>Беру:</b> {pick.pick}",
            f"💰 <b>Коэффициент букмекера:</b> {pick.book_odds:.2f}",
            f"🧮 <b>Моя цена по модели:</b> {pick.fair_odds:.2f}",
            f"📊 <b>Преимущество над линией:</b> {pick.edge*100:+.1f}%",
            f"🎯 <b>Вероятность по модели:</b> {int(round(pick.probability*100))}%",
            f"📈 <b>Рынок vs модель:</b> {market_prob*100:.0f}% → {pick.probability*100:.0f}% "
            f"({gap:+.1f} п.п.)",
            f"💵 <b>Размер ставки:</b> {stake:.0f} ₽  ·  <i>{confidence_word}</i>",
            "",
            "📈 <b>Раскладка модели:</b>",
            f"   Победа хозяев: <b>{model['home']*100:.0f}%</b>",
            f"   Ничья: <b>{model['draw']*100:.0f}%</b>",
            f"   Победа гостей: <b>{model['away']*100:.0f}%</b>",
            f"   Тотал больше 2.5: <b>{model['over_2_5']*100:.0f}%</b>",
            f"   Тотал меньше 2.5: <b>{model['under_2_5']*100:.0f}%</b>",
            f"   Обе забьют — Да: <b>{model['btts_yes']*100:.0f}%</b>",
            f"   Обе забьют — Нет: <b>{model['btts_no']*100:.0f}%</b>",
        ]

        if meta.get("reasoning"):
            lines += ["", "🧠 <b>Почему беру:</b>", meta["reasoning"]]
        if meta.get("risks"):
            lines += ["", f"⚠️ <b>Что смущает:</b> {meta['risks']}"]
        if cached:
            lines += [
                "",
                f"🏠 <b>Форма хозяев:</b> <code>{form_ru(getattr(cached, 'home_form', '— — — — —'))}</code>",
                f"✈️ <b>Форма гостей:</b> <code>{form_ru(getattr(cached, 'away_form', '— — — — —'))}</code>",
                f"🩺 <b>Кадры/травмы:</b> {_render_cached_list(getattr(cached, 'injuries', '[]'), 3)}",
                f"📌 <b>Факты:</b> {_render_cached_list(getattr(cached, 'facts', '[]'), 3)}",
                f"📊 <b>Статистика:</b> {_render_cached_list(getattr(cached, 'stats', '[]'), 3)}",
            ]

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
            f"❌ Что-то пошло не так при анализе: {e}",
            reply_markup=back_button("menu_search")
        )


# ── Ретро-отчёт ──────────────────────────────────────────────────
_VERDICT_ICON = {"win": "✅", "loss": "❌", "void": "➖"}


def _retro_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Вчера",       callback_data="retro_yesterday")],
        [
            InlineKeyboardButton("📆 7 дней",   callback_data="retro_period_7"),
            InlineKeyboardButton("🗓 30 дней",  callback_data="retro_period_30"),
        ],
        [
            InlineKeyboardButton("🏆 По рынкам", callback_data="retro_markets"),
            InlineKeyboardButton("🌍 По лигам",  callback_data="retro_leagues"),
        ],
        [InlineKeyboardButton("📐 CLV-трекинг",  callback_data="retro_clv")],
        [InlineKeyboardButton("💼 Мои ставки",   callback_data="retro_mybets")],
        [InlineKeyboardButton("◀ В меню",        callback_data="back_menu")],
    ])


def _retro_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀ К отчётам", callback_data="menu_retro")],
        [InlineKeyboardButton("◀ В меню",    callback_data="back_menu")],
    ])


def _format_settled_line(s) -> str:
    icon = _VERDICT_ICON.get(s.status, "•")
    score = s.result_score or "—:—"
    pnl = float(s.pnl_units or 0)
    pnl_txt = f"{pnl:+.2f}u" if s.status != "void" else "возврат"
    return (
        f"{icon} <b>{s.match}</b> · {s.pick} @{s.book_odds:.2f} · "
        f"{score} · {pnl_txt}"
    )


def _format_agg_footer(agg: dict) -> list[str]:
    if not agg or agg.get("total", 0) == 0:
        return []
    decided = agg.get("decided", 0)
    score_line = f"{agg['wins']}/{decided}" if decided else "—"
    void_chunk = f" · ➖{agg['voids']}" if agg.get("voids") else ""
    lines = [
        "",
        f"<b>Итого:</b> {score_line}{void_chunk} · "
        f"ROI {agg['roi_pct']:+.1f}% · "
        f"банк {agg['pnl_units']:+.2f}u",
    ]
    if agg.get("clv_sample"):
        lines.append(f"📐 CLV {agg['clv_pct']:+.2f}% (n={agg['clv_sample']})")
    if agg.get("decided"):
        lines.append(f"🎯 Винрейт: {agg['win_rate_pct']:.1f}% · "
                     f"средн. кэф {agg['avg_odds']:.2f}")
    return lines


async def _show_retro_menu(q):
    body = (
        "📖 <b>Ретро-отчёт</b>\n\n"
        "Здесь видно, как сыграли сигналы из канала и твои собственные ставки.\n\n"
        "• <b>Вчера / 7д / 30д</b> — итог по канальным сигналам\n"
        "• <b>По рынкам / По лигам</b> — где модель работает лучше\n"
        "• <b>CLV-трекинг</b> — самое честное мерило эджа\n"
        "• <b>Мои ставки</b> — твоя история закрытых ставок"
    )
    await q.message.edit_text(body, parse_mode=ParseMode.HTML,
                               reply_markup=_retro_menu_kb())


async def _show_retro_yesterday(q):
    msk = MSK_TZ
    yday = (datetime.now(msk) - timedelta(days=1)).date()
    sigs = await settled_signals_for_msk_date(yday)
    date_str = yday.strftime("%d.%m.%Y")
    if not sigs:
        body = (
            f"📅 <b>Отчёт за {date_str}</b>\n\n"
            "<i>Сигналов в этот день не было — модель не нашла value, "
            "или они ещё в процессе расчёта.</i>"
        )
    else:
        lines = [f"📅 <b>Отчёт за {date_str}</b>", ""]
        for s in sigs:
            lines.append(_format_settled_line(s))
        lines += _format_agg_footer(aggregate_signal_stats(sigs))
        body = "\n".join(lines)
    await q.message.edit_text(body, parse_mode=ParseMode.HTML,
                               reply_markup=_retro_back_kb())


async def _show_retro_period(q, days: int):
    sigs = await settled_signals_in_range(days)
    if not sigs:
        body = (
            f"🗓 <b>Последние {days} дней</b>\n\n"
            "<i>Закрытых сигналов нет — либо стартовала фича только что, "
            "либо модель пропустила период.</i>"
        )
    else:
        agg = aggregate_signal_stats(sigs)
        lines = [
            f"🗓 <b>Последние {days} дней</b>",
            "",
            f"📊 Сигналов: <b>{agg['total']}</b>"
            + (f" (➖{agg['voids']} возврат)" if agg['voids'] else ""),
            f"✅ Зашло: <b>{agg['wins']}</b>"
            f"   ❌ Не зашло: <b>{agg['losses']}</b>",
            f"📈 Винрейт: <b>{agg['win_rate_pct']:.1f}%</b>"
            f"   ROI: <b>{agg['roi_pct']:+.1f}%</b>",
            f"💼 Банк: <b>{agg['pnl_units']:+.2f}u</b>"
            f"   средний кэф: <b>{agg['avg_odds']:.2f}</b>",
        ]
        if agg["clv_sample"]:
            lines.append(
                f"📐 CLV: <b>{agg['clv_pct']:+.2f}%</b> (n={agg['clv_sample']})"
            )
        # Последние 8 сигналов как сэмпл
        lines += ["", "<b>Последние сигналы:</b>"]
        for s in sigs[-8:][::-1]:
            lines.append(_format_settled_line(s))
        body = "\n".join(lines)
    await q.message.edit_text(body, parse_mode=ParseMode.HTML,
                               reply_markup=_retro_back_kb())


async def _show_retro_breakdown(q, group_by: str):
    rows = await aggregate_signal_breakdown(days=30, group_by=group_by)
    title = "🏆 По рынкам (30 дней)" if group_by == "market" else "🌍 По лигам (30 дней)"
    if not rows:
        body = f"<b>{title}</b>\n\n<i>Закрытых сигналов за 30 дней нет.</i>"
    else:
        lines = [f"<b>{title}</b>", ""]
        for row in rows[:10]:
            label = league_title_ru(row["key"]) if group_by == "league" else row["key"]
            decided = row["decided"]
            score = f"{row['wins']}/{decided}" if decided else "—"
            tail = ""
            if row.get("clv_sample"):
                tail = f" · CLV {row['clv_pct']:+.2f}%"
            lines.append(
                f"• <b>{label}</b>: {row['total']} сигн · {score} · "
                f"ROI {row['roi_pct']:+.1f}% · "
                f"банк {row['pnl_units']:+.2f}u{tail}"
            )
        body = "\n".join(lines)
    await q.message.edit_text(body, parse_mode=ParseMode.HTML,
                               reply_markup=_retro_back_kb())


async def _show_retro_clv(q):
    sigs = await settled_signals_in_range(days=30)
    pairs = [
        s for s in sigs
        if s.book_odds and s.closing_odds and s.closing_odds > 1.01
    ]
    if not pairs:
        body = (
            "📐 <b>CLV-трекинг</b>\n\n"
            "<i>Пока нет ни одного сигнала с закрывающим коэф. — "
            "снэпшоты делаются за 30 минут до старта матчей.</i>\n\n"
            "CLV (Closing Line Value) — самое честное мерило эджа на длинной "
            "дистанции. Винрейт лжёт на коротких выборках, CLV — нет."
        )
    else:
        diffs = [(s.book_odds / s.closing_odds - 1) * 100 for s in pairs]
        avg = sum(diffs) / len(diffs)
        wins = sum(1 for d in diffs if d > 0)
        losses = sum(1 for d in diffs if d < 0)
        lines = [
            "📐 <b>CLV-трекинг (30 дней)</b>",
            "",
            f"📊 Сэмпл: <b>{len(pairs)}</b> сигналов",
            f"📈 Средний CLV: <b>{avg:+.2f}%</b>",
            f"✅ Линию обыграл: <b>{wins}</b>   ❌ проиграл: <b>{losses}</b>",
            "",
            "<i>Положительный CLV = модель ловила цену лучше рынка к старту. "
            "Это и есть истинный edge.</i>",
        ]
        body = "\n".join(lines)
    await q.message.edit_text(body, parse_mode=ParseMode.HTML,
                               reply_markup=_retro_back_kb())


async def _show_retro(q, user_id: int):
    bets = await retro_report(user_id, limit=15)
    if not bets:
        body = (
            "💼 <b>Мои ставки — пусто</b>\n\n"
            "Когда закроешь первую ставку (через 📤 «Загрузить чек» или вручную) — "
            "здесь появится разбор: что сыграло, что нет, какой ROI."
        )
    else:
        lines = ["💼 <b>Мои последние закрытые ставки</b>\n"]
        total_profit = 0.0
        for b in bets:
            icon = {"win": "✅", "loss": "❌", "void": "↩️"}.get(b.status, "•")
            profit = f"{b.profit:+.0f} ₽" if b.profit else "0 ₽"
            total_profit += float(b.profit or 0)
            lines.append(
                f"{icon} <b>{b.match or '—'}</b>  @ {b.odds:.2f}  ·  "
                f"ставка {b.stake:.0f} ₽  →  {profit}"
            )
        sign = "📈" if total_profit >= 0 else "📉"
        lines.append(f"\n{sign} <b>Итого по этим ставкам: {total_profit:+.0f} ₽</b>")
        body = "\n".join(lines)
    await q.message.edit_text(body, parse_mode=ParseMode.HTML,
                               reply_markup=_retro_back_kb())


# ── Текстовые сообщения ──────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    if ctx.user_data.pop("awaiting_bank", False):
        try:
            amount = float(text.replace(" ", "").replace(",", "."))
            assert amount >= 0
        except Exception:
            await update.message.reply_text(
                "Не разобрал сумму. Зайди ещё раз через 💰 Мой банк и пришли цифру (например 7500).",
            )
            return
        await set_bank(user.id, amount)
        await update.message.reply_text(
            f"✅ Банк закреплён: <b>{amount:,.0f} ₽</b>",
            parse_mode=ParseMode.HTML, reply_markup=main_menu(),
        )
        return

    await update.message.reply_text(
        "Жми кнопки в меню — там всё, что я умею.",
        reply_markup=main_menu(),
    )


# ── Фото — OCR чека ──────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await ensure_user(user.id, user.username or "", user.first_name or "")

    if not ctx.user_data.pop("awaiting_receipt", False):
        await update.message.reply_text(
            "📸 Если это чек — сначала жми 📤 <b>Загрузить чек</b> в меню, "
            "потом пришли фото. Так я пойму, что от меня ждут.",
            parse_mode=ParseMode.HTML, reply_markup=main_menu(),
        )
        return

    if not update.message.photo:
        await update.message.reply_text("Не вижу фото — пришли именно скриншот чека.")
        return

    await update.message.reply_text("🔍 Читаю чек...")

    photo = update.message.photo[-1]
    tg_file = await ctx.bot.get_file(photo.file_id)
    image_bytes = bytes(await tg_file.download_as_bytearray())

    result = await parse_receipt(image_bytes)
    if not result or result["stake"] <= 0 or result["odds"] <= 1:
        await update.message.reply_text(
            "❌ Не разобрал чек. Нужно чтобы на фото были видны сумма ставки, "
            "коэффициент и статус (выигрыш/проигрыш). Пришли ещё раз почётче.",
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
        await update.message.reply_text("❌ Не смог сохранить ставку — попробуй ещё раз.")
        return

    icon = {"win": "✅ Зашло!", "loss": "❌ Не зашло", "void": "↩️ Возврат"}[status]
    profit_str = f"{bet.profit:+.0f} ₽" if bet.profit else "0 ₽"
    await update.message.reply_text(
        f"{icon}\n\n"
        f"🏆 {match_name}\n"
        f"📊 Коэф {odds:.2f}  ·  ставка {stake:.0f} ₽\n"
        f"💵 Результат: <b>{profit_str}</b>\n"
        f"💰 Банк: {bet.bank_before:.0f} ₽ → <b>{bet.bank_after:.0f} ₽</b>",
        parse_mode=ParseMode.HTML, reply_markup=main_menu(),
    )


# ── Helpers ──────────────────────────────────────────────────────
def _stats_text(s: dict) -> str:
    sign = "📈" if s["roi"] >= 0 else "📉"
    if s["total"] == 0:
        return (
            "📊 <b>Статистика пустая</b>\n\n"
            "Сделай первую ставку или загрузи скриншот чека — посчитаю "
            "винрейт, ROI и прибыль на длинной."
        )
    return (
        "📊 <b>Как идут дела</b>\n\n"
        f"Сыграно: <b>{s['total']}</b>  "
        f"(✅ {s['wins']} · ❌ {s['losses']} · ↩️ {s['voids']})\n"
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
        ko = _fmt_msk(s.kickoff, with_date=True) if s.kickoff else "—"
        emoji = league_emoji(s.league or "")
        lines.append(
            f"{icon} {emoji} <b>{s.match}</b>\n"
            f"   <i>{league_title_ru(s.league or '')}</i>\n"
            f"   📌 {s.pick}  @  <b>{s.book_odds:.2f}</b>\n"
            f"   📈 {int(s.probability*100)}%  ·  🕐 {ko}\n"
        )
    return "\n".join(lines)


def _render_cached_list(raw: str, limit: int = 2) -> str:
    try:
        items = json.loads(raw or "[]")
    except Exception:
        items = []
    return ", ".join(items[:limit]) if items else "—"


def _cached_match_context(cached) -> str:
    if not cached:
        return ""
    parts = []
    for label, raw in [("Факты", cached.facts), ("Статистика", cached.stats), ("Травмы", cached.injuries)]:
        text = _render_cached_list(raw, 3)
        if text != "—":
            parts.append(f"{label}: {text}")
    if getattr(cached, "home_summary", ""):
        parts.append(f"{cached.home}: {cached.home_summary}")
    if getattr(cached, "away_summary", ""):
        parts.append(f"{cached.away}: {cached.away_summary}")
    return "\n".join(parts)


def _render_match_card(item) -> str:
    kickoff_dt = getattr(item, "kickoff", None) or getattr(item, "utc_date", None)
    kickoff = _fmt_msk(kickoff_dt) if kickoff_dt else "--:--"
    lines = [
        f"🕐 <b>{kickoff}</b>",
        f"   {item.home} — {item.away}",
    ]
    raw = getattr(item, "raw_payload", "{}") or "{}"
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        payload = {}
    odds = payload.get("odds", {})
    if odds.get("home") and odds.get("draw") and odds.get("away"):
        lines.append(
            f"   П1 <b>{odds['home']:.2f}</b> | X <b>{odds['draw']:.2f}</b> | П2 <b>{odds['away']:.2f}</b>"
        )
    return "\n".join(lines)


async def _matches_for_today_view():
    today = datetime.now(timezone.utc).date()
    cached = await list_cached_matches_for_date(today)
    if cached:
        return cached
    await warmup_match_cache(limit=18)
    cached = await list_cached_matches_for_date(today)
    if cached:
        return cached
    return await fetch_matches()


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
    bank_line = (
        f"💰 <b>Банк сейчас: {bank:,.0f} ₽</b>"
        if bank > 0 else
        "💰 <b>Банк не задан</b>"
    )
    await q.message.edit_text(
        f"{bank_line}\n\n"
        "От этой суммы я считаю размер каждой ставки по Kelly. "
        "Выбери пресет или укажи свою сумму — а лучше реальный игровой банк.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _show_settings(q, user_id: int):
    st = await get_settings(user_id)
    risk = st.get("risk", "mid")
    kelly = st.get("kelly_cap", config.kelly_cap)
    desc = {
        "low":  "До 2% банка на ставку. Растёшь медленно, но и просадка короткая.",
        "mid":  "До 5% банка. Стандартный Kelly — золотая середина.",
        "high": "До 8% банка. Агрессивно: больше профита на длинной — и больнее на минус-серии.",
    }
    risk_label = {"low": "консервативный", "mid": "средний", "high": "агрессивный"}[risk]
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
        "⚙️ <b>Риск-профиль</b>\n\n"
        f"Сейчас: <b>{risk_label}</b>  ·  потолок ставки <b>{kelly*100:.0f}%</b> от банка\n\n"
        f"<i>{desc[risk]}</i>\n\n"
        "Чем выше потолок — тем сильнее раскачка. Если просадки тяжело "
        "переносить психологически, лучше консервативный профиль.\n\n"
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
            "😐 Сегодня линия плотная — value не вижу. Это нормально: "
            "лучше пропустить день, чем ставить на минимальном edge.",
            reply_markup=main_menu(),
        )
        return

    for g in guesses[:3]:
        m = g["match"]
        p = g["pick"]
        model = g.get("model") or {}
        emoji = league_emoji(m.competition)
        ts = _fmt_msk(m.utc_date) if m.utc_date else ""
        market_prob = p.market_probability if p.market_probability > 0 \
            else (1 / p.book_odds if p.book_odds > 1 else 0.0)
        gap = (p.probability - market_prob) * 100
        lines = [
            f"{emoji} <b>{league_title_ru(m.competition)}</b>",
            f"⚽ <b>{m.home} — {m.away}</b>  ·  🕐 {ts}",
            "",
            f"📌 <b>Беру:</b> {p.pick}",
            f"💰 <b>Коэф букмекера:</b> {p.book_odds:.2f}",
            f"🧮 <b>Моя цена:</b> {p.fair_odds:.2f}",
            f"📊 <b>Преимущество над линией:</b> {p.edge*100:+.1f}%",
            f"📈 рынок {market_prob*100:.0f}% → модель {p.probability*100:.0f}% "
            f"({gap:+.1f} п.п.)",
            f"💵 <b>Размер ставки:</b> {p.recommended_stake:.0f} ₽",
        ]
        if model:
            lines += [
                "",
                "📈 <b>Раскладка модели:</b>",
                f"   П1 <b>{model['home']*100:.0f}%</b>  ·  "
                f"Х <b>{model['draw']*100:.0f}%</b>  ·  "
                f"П2 <b>{model['away']*100:.0f}%</b>",
                f"   ТБ 2.5 <b>{model['over_2_5']*100:.0f}%</b>  ·  "
                f"BTTS Да <b>{model['btts_yes']*100:.0f}%</b>",
            ]
        lines += ["", DISCLAIMER]
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
    app.add_handler(CommandHandler("settle",  cmd_settle))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app
