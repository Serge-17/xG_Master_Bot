"""
channel.py — публикация прогнозов в Telegram-канал.

Формат поста и кнопки под ним — согласно ТЗ.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from analysis import Pick
from config import config
from data_sources import Match, Odds
from db import Signal, set_signal_message_id


log = logging.getLogger(__name__)


def _confidence_bar(pct: int) -> str:
    filled = round(max(0, min(100, pct)) / 10)
    return "🟢" * filled + "⬜" * (10 - filled)


def _kickoff_local(match: Match) -> str:
    if match.utc_date is None:
        return "время уточняется"
    dt = match.utc_date.astimezone(timezone.utc)
    return dt.strftime("%d.%m %H:%M UTC")


def format_signal_post(match: Match, pick: Pick, reasoning: str, risks: str,
                       home_form: str, away_form: str) -> str:
    confidence_pct = int(round(pick.probability * 100))
    return (
        f"⚽ <b>{match.home} vs {match.away}</b>\n"
        f"🏆 {match.competition}  |  🕐 {_kickoff_local(match)}\n\n"
        f"📌 <b>Ставка:</b> {pick.pick}\n"
        f"📊 Коэф. букмекера: <b>{pick.book_odds:.2f}</b>\n"
        f"🧮 Наш fair-odds:   <b>{pick.fair_odds:.2f}</b>\n"
        f"📈 Вероятность:   <b>{confidence_pct}%</b>  {_confidence_bar(confidence_pct)}\n"
        f"💎 Value edge:    <b>{pick.edge*100:+.1f}%</b>\n\n"
        f"🏠 Форма хозяев: <code>{home_form}</code>\n"
        f"✈️ Форма гостей: <code>{away_form}</code>\n\n"
        f"🧠 <b>Почему value:</b>\n{reasoning}\n\n"
        f"⚠️ <b>Риски:</b> {risks}\n\n"
        f"💰 <b>Рекомендация:</b> {pick.recommended_stake:.0f} ₽\n\n"
        f"<i>Это рекомендация на основе модели, не гарантия. "
        f"Играйте ответственно — ставки связаны с риском потери денег.</i>"
    )


def channel_post_keyboard(signal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔍 Открыть анализ",
                                 callback_data=f"sig_analysis_{signal_id}"),
            InlineKeyboardButton("📊 Коэффициенты",
                                 callback_data=f"sig_odds_{signal_id}"),
        ],
        [
            InlineKeyboardButton("💰 Мой банк",      callback_data="menu_bank"),
            InlineKeyboardButton("📈 Статистика",    callback_data="menu_stats"),
        ],
        [
            InlineKeyboardButton("📤 Загрузить результат", callback_data="menu_upload"),
        ],
    ])


def format_matches_digest(items: list) -> str:
    lines = ["📅 <b>Матчи сегодня</b>\n"]
    for item in items[:12]:
        kickoff = item.kickoff.strftime("%H:%M") if getattr(item, "kickoff", None) else "—:—"
        league = getattr(item, "league", "") or getattr(item, "competition", "") or "—"
        payload = {}
        raw = getattr(item, "raw_payload", "{}") or "{}"
        if isinstance(raw, str):
            try:
                import json
                payload = json.loads(raw)
            except Exception:
                payload = {}
        odds = payload.get("odds", {})
        odds_line = ""
        if odds.get("home") and odds.get("draw") and odds.get("away"):
            odds_line = (
                f"\nП1 {odds['home']:.2f} | X {odds['draw']:.2f} | П2 {odds['away']:.2f}"
            )
        lines.append(
            f"• <b>{item.home} vs {item.away}</b>\n"
            f"{kickoff} UTC • <i>{league}</i>{odds_line}"
        )
    lines.append(
        "\n<i>Коэффициенты сохранены в базе и будут обновляться несколько раз в день.</i>"
    )
    return "\n".join(lines)


async def publish_signal(bot: Bot, signal: Signal, match: Match) -> bool:
    """Публикует сигнал в CHANNEL_ID. При успехе сохраняет message_id в БД."""
    if not config.channel_id:
        log.info("CHANNEL_ID не задан — публикация в канал пропущена (signal #%d)",
                 signal.id)
        return False

    pick_obj = Pick(
        market=signal.market, pick=signal.pick,
        probability=signal.probability, book_odds=signal.book_odds,
        fair_odds=signal.fair_odds, edge=signal.edge,
        recommended_stake=signal.recommended_stake,
    )
    text = format_signal_post(
        match, pick_obj,
        reasoning=signal.reasoning or "—",
        risks=signal.risks or "—",
        home_form=signal.home_form or "— — — — —",
        away_form=signal.away_form or "— — — — —",
    )

    try:
        msg = await bot.send_message(
            chat_id=config.channel_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=channel_post_keyboard(signal.id),
            disable_web_page_preview=True,
        )
        await set_signal_message_id(signal.id, msg.message_id)
        log.info("Сигнал #%d опубликован в %s (msg=%d)",
                 signal.id, config.channel_id, msg.message_id)
        return True
    except Exception as e:
        log.error("Не удалось опубликовать в %s: %s", config.channel_id, e)
        return False
