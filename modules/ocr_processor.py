from __future__ import annotations

import re
from pathlib import Path
import base64
import json

from config import settings


OUTCOME_PATTERNS = {
    "win": ["выигрыш", "won", "win", "выиграл"],
    "loss": ["проигрыш", "lost", "loss", "проиграл"],
    "refund": ["возврат", "refund", "returned"],
}


def _extract_text_with_pytesseract(image_path: str) -> str:
    try:
        from PIL import Image
        import pytesseract

        image = Image.open(image_path)
        return pytesseract.image_to_string(image, lang=settings.ocr_lang)
    except Exception:
        return ""


def _extract_text_with_claude(image_path: str) -> str:
    if not settings.anthropic_api_key:
        return ""

    try:
        import requests

        image_bytes = Path(image_path).read_bytes()
        media_type = "image/jpeg"
        suffix = Path(image_path).suffix.lower()
        if suffix in {".png"}:
            media_type = "image/png"
        elif suffix in {".webp"}:
            media_type = "image/webp"

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": settings.anthropic_model,
                "max_tokens": 1024,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": base64.b64encode(image_bytes).decode("utf-8"),
                                },
                            },
                            {
                                "type": "text",
                                "text": (
                                    "Read this football betting coupon screenshot. "
                                    "Return only plain text containing the outcome "
                                    "(Win, Loss, Refund), and if visible the stake and odds."
                                ),
                            },
                        ],
                    }
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        blocks = data.get("content", [])
        texts = [block.get("text", "") for block in blocks if block.get("type") == "text"]
        return "\n".join(texts)
    except Exception:
        return ""


def parse_coupon_text(text: str) -> dict[str, str | float | None]:
    lowered = text.lower()
    outcome = "pending"
    for key, patterns in OUTCOME_PATTERNS.items():
        if any(pattern in lowered for pattern in patterns):
            outcome = key
            break

    number_matches = [float(match.replace(",", ".")) for match in re.findall(r"\d+[.,]?\d*", text)]
    amount = number_matches[0] if number_matches else None
    odds = number_matches[1] if len(number_matches) > 1 else None
    return {
        "ocr_status": "Processed" if text.strip() else "Empty",
        "ocr_recognized_outcome": outcome.title(),
        "ocr_recognized_amount": amount,
        "ocr_recognized_odds": odds,
    }


def process_coupon_image(image_path: str) -> dict[str, str | float | None]:
    if not Path(image_path).exists():
        raise FileNotFoundError(image_path)

    if not settings.ocr_enabled:
        text = ""
    elif settings.ocr_provider == "claude":
        text = _extract_text_with_claude(image_path) or _extract_text_with_pytesseract(image_path)
    else:
        text = _extract_text_with_pytesseract(image_path)
    return parse_coupon_text(text)


def interpret_result(outcome: str) -> str:
    normalized = (outcome or "").strip().lower()
    if normalized in {"win", "won", "выигрыш", "выиграл"}:
        return "Win"
    if normalized in {"loss", "lost", "проигрыш", "проиграл"}:
        return "Loss"
    if normalized in {"refund", "return", "возврат"}:
        return "Refund"
    return "Pending"


def calculate_settlement_amount(outcome: str, stake: float | None, odds: float | None) -> float:
    normalized = interpret_result(outcome)
    stake_value = max(float(stake or 0.0), 0.0)
    odds_value = float(odds or 2.0)

    if normalized == "Win":
        return round(stake_value * odds_value, 2)
    if normalized == "Refund":
        return round(stake_value, 2)
    if normalized == "Loss":
        return 0.0
    return 0.0
