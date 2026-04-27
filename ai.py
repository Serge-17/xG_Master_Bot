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
def _parse_capper_text(text: str) -> dict:
    """Парсим free-form ответ Gemini по секциям ПОЧЕМУ/РИСК/ФОРМА_Х/ФОРМА_Г."""
    out = {"reasoning": "", "risks": "", "home_form": "", "away_form": ""}
    if not text:
        return out

    sections = {
        "reasoning": re.search(r"(?:ПОЧЕМУ|БЕРЁМ|АНАЛИЗ)[:\-—]\s*(.+?)(?=(?:РИСК|ФОРМА_Х|ФОРМА_Г|$))",
                               text, flags=re.IGNORECASE | re.DOTALL),
        "risks":     re.search(r"РИСК[А-ЯA-ZЁ]*[:\-—]\s*(.+?)(?=(?:ФОРМА_Х|ФОРМА_Г|$))",
                               text, flags=re.IGNORECASE | re.DOTALL),
        "home_form": re.search(r"ФОРМА_Х[:\-—]\s*([WDLВНП\s—\-]+)",
                               text, flags=re.IGNORECASE),
        "away_form": re.search(r"ФОРМА_Г[:\-—]\s*([WDLВНП\s—\-]+)",
                               text, flags=re.IGNORECASE),
    }
    for key, m in sections.items():
        if m:
            out[key] = m.group(1).strip().strip(".,;:") or ""
    return out


async def explain_pick(home: str, away: str, competition: str,
                       pick_label: str, probability: float,
                       book_odds: float, fair_odds: float,
                       extra_context: str = "") -> dict:
    """Возвращает dict с ключами: reasoning, risks, home_form, away_form.

    Промпт переписан в стиле живого каппера — свободный текст по секциям,
    а не сухой JSON. Это даёт более живой комментарий (см. формат поста).
    """
    market_prob = 1 / book_odds if book_odds > 1 else 0.0
    gap = (probability - market_prob) * 100
    context_suffix = f"\nКонтекст матча:\n{extra_context}\n" if extra_context else ""

    fallback_reasoning = (
        f"Беру {pick_label}. По модели вероятность {probability:.0%}, "
        f"бук закладывает ~{market_prob:.0%} — гэп {gap:+.1f} п.п. в нашу пользу. "
        f"Справедливая цена ~{fair_odds:.2f}, в линии {book_odds:.2f} — это и есть value."
    )
    fallback = {
        "reasoning": fallback_reasoning,
        "risks": "Состав и кадровые новости последних часов могли не дойти до модели — проверьте линейку перед стартом.",
        "home_form": "— — — — —",
        "away_form": "— — — — —",
    }
    if not config.gemini_api_key:
        return fallback

    prompt = (
        f"Ты опытный футбольный каппер. Пишешь как для подписчиков канала — живо, "
        f"конкретно, без воды, от первого лица.\n\n"
        f"Матч: {home} — {away}, турнир: {competition}.\n"
        f"Ставка модели: «{pick_label}».\n"
        f"Вероятность по модели: {probability:.0%}. Букмекер закладывает ~{market_prob:.0%} "
        f"(коэф {book_odds:.2f}, наш fair {fair_odds:.2f}, гэп {gap:+.1f} п.п.).\n"
        f"{context_suffix}\n"
        "Дай ответ ровно в таком формате (без markdown, заголовки секций обязательны):\n\n"
        "ПОЧЕМУ:\n"
        "<2-4 предложения от первого лица — почему берём ставку. Опирайся на форму, xG, "
        "мотивацию, фактор поля, кадры. Используй жаргон каппера: «бук занижает», "
        "«противоход», «осторожный заход», но не переборщи. Цифры — конкретные.>\n\n"
        "РИСКИ:\n"
        "<1-2 предложения о реальных угрозах ставке — травмы, ротация, история встреч.>\n\n"
        "ФОРМА_Х: <последние 5 матчей хозяев в формате W D L через пробел>\n"
        "ФОРМА_Г: <последние 5 матчей гостей в формате W D L через пробел>\n"
    )
    raw = await _gemini_call([{"text": prompt}], timeout_s=30)
    if not raw:
        return fallback
    parsed = _parse_capper_text(raw)
    return {
        "reasoning": parsed["reasoning"] or fallback["reasoning"],
        "risks":     parsed["risks"]     or fallback["risks"],
        "home_form": parsed["home_form"] or fallback["home_form"],
        "away_form": parsed["away_form"] or fallback["away_form"],
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
