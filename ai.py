"""
ai.py — Gemini (text + vision) обёртки.

Две функции высокого уровня:
  explain_pick() — словесный анализ value-ставки (форма, мотивация, риски).
  parse_receipt() — OCR скриншота чека: выигрыш/проигрыш/сумма.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Optional

import aiohttp

from config import config


log = logging.getLogger(__name__)


async def _gemini_call(parts: list[dict], timeout_s: int = 45) -> Optional[str]:
    if not config.gemini_api_key:
        return None
    url = (
        f"{config.gemini_base}/{config.gemini_model}:generateContent"
        f"?key={config.gemini_api_key}"
    )
    body = {"contents": [{"parts": parts}]}
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(url, json=body) as r:
                if r.status != 200:
                    text = await r.text()
                    log.error("Gemini %s: %s", r.status, text[:300])
                    return None
                data = await r.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        log.error("Gemini call error: %s", e)
        return None


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    # Чистим markdown-ограждения ```json ... ```
    text = re.sub(r"```(?:json)?", "", text)
    text = text.replace("```", "")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────
# Комментарий к value-пику
# ────────────────────────────────────────────────────────────────
async def explain_pick(home: str, away: str, competition: str,
                       pick_label: str, probability: float,
                       book_odds: float, fair_odds: float,
                       extra_context: str = "") -> dict:
    """Возвращает dict с ключами: reasoning, risks, home_form, away_form."""
    context_suffix = f"\nДоп. контекст:\n{extra_context}\n" if extra_context else ""
    fallback = {
        "reasoning": (
            f"Модель оценивает вероятность {probability:.0%} против "
            f"рыночных ~{1/book_odds:.0%}. Это value. "
            f"{extra_context[:220]}".strip()
        ),
        "risks": "Данные о составах и травмах могли не учесть событий последних часов.",
        "home_form": "— — — — —",
        "away_form": "— — — — —",
    }
    if not config.gemini_api_key:
        return fallback

    prompt = (
        f"Ты футбольный аналитик. Матч: {home} vs {away}, турнир: {competition}.\n"
        f"Модель выбрала ставку: «{pick_label}».\n"
        f"Модельная вероятность: {probability:.1%}. "
        f"Коэффициент букмекера: {book_odds}. Наш fair-odds: {fair_odds}.\n\n"
        f"{context_suffix}"
        "Вкратце объясни почему это может быть value-ставка: форма, xG, "
        "мотивация, домашний/выездной фактор. Укажи 1–2 главных риска.\n\n"
        "Ответь СТРОГО в JSON без пояснений вне JSON:\n"
        "{\n"
        '  "reasoning": "<3-4 предложения — конкретные факты>",\n'
        '  "risks": "<1-2 риска>",\n'
        '  "home_form": "<форма хозяев: W/D/L через пробел, 5 игр>",\n'
        '  "away_form": "<форма гостей: W/D/L через пробел, 5 игр>"\n'
        "}"
    )
    raw = await _gemini_call([{"text": prompt}], timeout_s=30)
    parsed = _extract_json(raw or "")
    if not parsed:
        return fallback
    return {
        "reasoning": parsed.get("reasoning") or fallback["reasoning"],
        "risks": parsed.get("risks") or fallback["risks"],
        "home_form": parsed.get("home_form") or fallback["home_form"],
        "away_form": parsed.get("away_form") or fallback["away_form"],
    }


# ────────────────────────────────────────────────────────────────
# OCR скриншота ставки
# ────────────────────────────────────────────────────────────────
async def parse_receipt(image_bytes: bytes, mime: str = "image/jpeg") -> Optional[dict]:
    """Возвращает: {'status': 'win'|'loss'|'void', 'stake': float, 'odds': float,
                     'payout': float, 'match': str} или None."""
    if not config.gemini_api_key:
        return None

    prompt = (
        "Это скриншот ставки из букмекерской конторы (1xBet, Фонбет, Мелбет, "
        "Леон, Winline и т.д.). Внимательно прочитай цифры и текст.\n\n"
        "Ответь СТРОГО в JSON без пояснений вне JSON. Статус: "
        "'win' если ставка сыграла/зачислена; 'loss' если проиграла; "
        "'void' если возврат/отмена. Если не уверен — 'void'.\n\n"
        "{\n"
        '  "status": "win" | "loss" | "void",\n'
        '  "stake": <сумма ставки в рублях, число>,\n'
        '  "odds": <итоговый коэффициент, число>,\n'
        '  "payout": <выплата в рублях или 0>,\n'
        '  "match": "<название матча или пустая строка>"\n'
        "}"
    )
    parts = [
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode()}},
        {"text": prompt},
    ]
    raw = await _gemini_call(parts, timeout_s=45)
    parsed = _extract_json(raw or "")
    if not parsed:
        return None

    status = str(parsed.get("status", "")).lower().strip()
    # Принимаем старый формат с won:bool тоже
    if status not in {"win", "loss", "void"}:
        won = parsed.get("won")
        if won is True:
            status = "win"
        elif won is False:
            status = "loss"
        else:
            status = "void"

    try:
        stake = float(parsed.get("stake", 0) or 0)
        odds = float(parsed.get("odds", 0) or 0)
        payout = float(parsed.get("payout", 0) or 0)
    except (TypeError, ValueError):
        return None

    return {
        "status": status,
        "stake": round(stake, 2),
        "odds": round(odds, 2),
        "payout": round(payout, 2),
        "match": str(parsed.get("match", "") or ""),
    }
