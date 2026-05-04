"""
channel.py — публикация прогнозов в Telegram-канал.

Стиль постов переписан под живого каппера: вступления варьируются,
сравнение модели с рынком — словами, без 10 квадратиков-шкалы.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional


# Время матчей показываем по Москве (UTC+3)
MSK_TZ = timezone(timedelta(hours=3))

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from analysis import Pick
from config import config
from data_sources import Match, Odds
from db import (
    Signal, aggregate_signal_stats, set_signal_message_id,
    settled_signals_for_msk_date, settled_signals_in_range,
)


log = logging.getLogger(__name__)


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


# Варьируемые вступления — выбираем по signal_id, чтобы посты не были одинаковыми
INTRO_TEMPLATES = [
    "🎯 <b>Беру в работу</b>",
    "💡 <b>Зацепил value</b>",
    "📍 <b>Линия даёт фору</b>",
    "🔎 <b>Нашёл расхождение</b>",
    "⚡ <b>Свежий заход</b>",
]

CONFIDENCE_LABELS = [
    (0.65, "🔥 высокая"),
    (0.55, "✅ уверенная"),
    (0.45, "⚖️ средняя"),
    (0.0,  "🪙 осторожный заход"),
]

DISCLAIMER = (
    "<i>Не реклама и не гарантия — это математическая модель. "
    "Минус-серия из 3-4 ставок реальна даже на ярком value. "
    "Банкролл-менеджмент важнее любой отдельной ставки.</i>"
)


def _confidence_label(prob: float) -> str:
    for threshold, label in CONFIDENCE_LABELS:
        if prob >= threshold:
            return label
    return CONFIDENCE_LABELS[-1][1]


def _gap_phrase(model_prob: float, market_prob: float) -> str:
    gap = (model_prob - market_prob) * 100
    if market_prob <= 0:
        return f"моя оценка {model_prob*100:.0f}%"
    if gap >= 8:
        return f"рынок {market_prob*100:.0f}%, у меня {model_prob*100:.0f}% — гэп серьёзный, +{gap:.1f} п.п."
    if gap >= 4:
        return f"рынок {market_prob*100:.0f}%, у меня {model_prob*100:.0f}% — перевес +{gap:.1f} п.п."
    if gap >= 0:
        return f"рынок {market_prob*100:.0f}%, у меня {model_prob*100:.0f}% — небольшой перевес +{gap:.1f} п.п."
    return f"рынок {market_prob*100:.0f}%, моя оценка {model_prob*100:.0f}% — иду противоходом"


def _kickoff_local(match: Match) -> str:
    if match.utc_date is None:
        return "время уточняется"
    dt = match.utc_date
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MSK_TZ).strftime("%d.%m %H:%M МСК")


def _league_ru(name: str) -> str:
    return LEAGUE_TITLES_RU.get(name, name or "Неизвестная лига")


def _form_ru(form: str) -> str:
    mapping = {"W": "В", "D": "Н", "L": "П", "—": "—"}
    parts = [mapping.get(part, part) for part in (form or "").split()]
    return " ".join(parts) if parts else "— — — — —"


def format_signal_post(match: Match, pick: Pick, reasoning: str, risks: str,
                       home_form: str, away_form: str,
                       signal_id: int = 0,
                       model: Optional[dict] = None,
                       injuries: Optional[list[str]] = None,
                       facts: Optional[list[str]] = None) -> str:
    intro = INTRO_TEMPLATES[signal_id % len(INTRO_TEMPLATES)]
    confidence = _confidence_label(pick.probability)
    market_prob = pick.market_probability if pick.market_probability > 0 \
        else (1 / pick.book_odds if pick.book_odds > 1 else 0.0)
    gap_text = _gap_phrase(pick.probability, market_prob)

    blocks = [
        f"{intro}",
        f"🏆 <b>{_league_ru(match.competition)}</b>",
        f"⚽ <b>{match.home} — {match.away}</b>",
        f"🕐 {_kickoff_local(match)}",
        "",
        f"📌 <b>Ставка:</b> {pick.pick}",
        f"💰 <b>Коэффициент букмекера:</b> {pick.book_odds:.2f}",
        f"🧮 <b>Моя цена по модели:</b> {pick.fair_odds:.2f}",
        f"📊 <b>Преимущество над линией:</b> {pick.edge*100:+.1f}%",
        f"🎯 <b>Вероятность по модели:</b> {int(round(pick.probability*100))}%  ·  "
        f"<i>{confidence}</i>",
        f"📈 <i>{gap_text}</i>",
        f"💵 <b>Рекомендуемая ставка:</b> {pick.recommended_stake:.0f} ₽",
    ]

    if model:
        blocks += [
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

    blocks += [
        "",
        f"🧠 <b>Почему беру:</b>",
        reasoning,
        "",
        f"⚠️ <b>Что смущает:</b> {risks}",
        "",
        f"🏠 <b>Форма хозяев:</b> <code>{_form_ru(home_form)}</code>",
        f"✈️ <b>Форма гостей:</b> <code>{_form_ru(away_form)}</code>",
    ]

    if injuries:
        blocks.append(f"🩺 <b>Кадры/травмы:</b> {', '.join(injuries[:3])}")
    if facts:
        blocks.append(f"📌 <b>Факты:</b> {', '.join(facts[:3])}")

    blocks += ["", DISCLAIMER]
    return "\n".join(blocks)


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
    lines = ["📅 <b>Что играем сегодня</b>\n"]
    for item in items[:12]:
        ko_dt = getattr(item, "kickoff", None)
        if ko_dt is not None:
            if ko_dt.tzinfo is None:
                ko_dt = ko_dt.replace(tzinfo=timezone.utc)
            kickoff = ko_dt.astimezone(MSK_TZ).strftime("%H:%M")
        else:
            kickoff = "—:—"
        league = _league_ru(getattr(item, "league", "") or getattr(item, "competition", "") or "—")
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
            f"• <b>{item.home} — {item.away}</b>\n"
            f"{kickoff} МСК • <i>{league}</i>{odds_line}"
        )
    lines.append(
        "\n<i>Линию обновляю несколько раз в день. К старту матча цифры могут поехать.</i>"
    )
    return "\n".join(lines)


def _list_from_cached_field(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


async def publish_signal(bot: Bot, signal: Signal, match: Match,
                         model: Optional[dict] = None,
                         cached: object = None) -> bool:
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
    injuries = _list_from_cached_field(getattr(cached, "injuries", None))
    facts = _list_from_cached_field(getattr(cached, "facts", None))

    text = format_signal_post(
        match, pick_obj,
        reasoning=signal.reasoning or "—",
        risks=signal.risks or "—",
        home_form=signal.home_form or "— — — — —",
        away_form=signal.away_form or "— — — — —",
        signal_id=signal.id,
        model=model,
        injuries=injuries,
        facts=facts,
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


# ────────────────────────────────────────────────────────────────
# Reporting: per-signal reply + daily channel report
# ────────────────────────────────────────────────────────────────
_VERDICT_ICON = {"win": "✅", "loss": "❌", "void": "➖"}


def _format_signal_result_line(s: Signal) -> str:
    icon = _VERDICT_ICON.get(s.status, "•")
    score = s.result_score or "—:—"
    pnl = float(s.pnl_units or 0)
    pnl_txt = f"{pnl:+.2f}u" if s.status != "void" else "возврат"
    return (
        f"{icon} <b>{s.match}</b> · {s.pick} @{s.book_odds:.2f} · "
        f"{score} · {pnl_txt}"
    )


def format_daily_report(report_date_msk, signals: list[Signal],
                        rolling_30d: Optional[list[Signal]] = None) -> str:
    """Текст автоморнинг-отчёта в канал."""
    date_str = report_date_msk.strftime("%d.%m.%Y")
    if not signals:
        body = [
            f"📊 <b>Отчёт за {date_str}</b>",
            "",
            "<i>Вчера сигналов в канал не выкатывал — линия не давала value, "
            "лучше пропустить день, чем форсить.</i>",
        ]
        if rolling_30d:
            agg30 = aggregate_signal_stats(rolling_30d)
            voids_chunk = f"-{agg30['voids']}в" if agg30['voids'] else ""
            body += [
                "",
                f"📈 <b>Месяц:</b> {agg30['wins']}-{agg30['losses']}{voids_chunk} · "
                f"ROI {agg30['roi_pct']:+.1f}% · "
                f"банк {agg30['pnl_units']:+.2f}u",
            ]
        return "\n".join(body)

    agg = aggregate_signal_stats(signals)
    lines = [f"📊 <b>Отчёт за {date_str}</b>", ""]
    for s in signals:
        lines.append(_format_signal_result_line(s))

    decided = agg["decided"]
    score_line = f"{agg['wins']}/{decided}" if decided else "—"
    void_chunk = f" · ➖{agg['voids']} возврат" if agg["voids"] else ""
    lines += [
        "",
        f"<b>Итог:</b> {score_line}{void_chunk} · "
        f"ROI {agg['roi_pct']:+.1f}% · "
        f"банк {agg['pnl_units']:+.2f}u",
    ]
    if agg.get("clv_sample"):
        lines.append(f"📐 CLV {agg['clv_pct']:+.2f}% (n={agg['clv_sample']})")

    if rolling_30d:
        agg30 = aggregate_signal_stats(rolling_30d)
        lines += [
            "",
            f"📈 <b>Месяц:</b> {agg30['wins']}-{agg30['losses']}"
            f" · ROI {agg30['roi_pct']:+.1f}% · "
            f"банк {agg30['pnl_units']:+.2f}u",
        ]
        if agg30.get("clv_sample"):
            lines.append(f"📐 CLV месяца: {agg30['clv_pct']:+.2f}% "
                         f"(n={agg30['clv_sample']})")

    lines += [
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)


async def post_daily_report(bot: Bot,
                            report_date_msk=None) -> bool:
    """
    Постит отчёт за вчерашний день по МСК. Возвращает True если опубликовано.
    """
    if not config.channel_id:
        log.info("CHANNEL_ID не задан — daily report пропущен")
        return False

    msk = timezone(timedelta(hours=3))
    if report_date_msk is None:
        report_date_msk = (datetime.now(msk) - timedelta(days=1)).date()

    sigs = await settled_signals_for_msk_date(report_date_msk)
    rolling = await settled_signals_in_range(days=30)

    text = format_daily_report(report_date_msk, sigs, rolling)
    try:
        await bot.send_message(
            chat_id=config.channel_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        log.info("daily report опубликован за %s (n=%d)", report_date_msk, len(sigs))
        return True
    except Exception as e:
        log.error("daily report не опубликован: %s", e)
        return False


async def reply_signal_result(bot: Bot, signal: Signal) -> bool:
    """
    Отвечаем эмодзи-результатом к исходному посту в канале.
    Это соцдоказательство: канал не удаляет проигрышные сигналы.
    """
    if not config.channel_id or not signal.channel_message_id:
        return False
    if signal.status not in ("win", "loss", "void"):
        return False

    icon = _VERDICT_ICON.get(signal.status, "•")
    score = signal.result_score or "—:—"
    pnl = float(signal.pnl_units or 0)
    pnl_txt = f"{pnl:+.2f}u" if signal.status != "void" else "возврат"

    if signal.status == "win":
        head = "Зашло"
    elif signal.status == "loss":
        head = "Не зашло"
    else:
        head = "Возврат"

    text = (
        f"{icon} <b>{head}</b> · {signal.pick} @{signal.book_odds:.2f}\n"
        f"Финал: <b>{score}</b> · {pnl_txt}"
    )

    try:
        await bot.send_message(
            chat_id=config.channel_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=signal.channel_message_id,
            disable_web_page_preview=True,
            allow_sending_without_reply=True,
        )
        return True
    except Exception as e:
        log.warning("reply_signal_result #%d failed: %s", signal.id, e)
        return False
