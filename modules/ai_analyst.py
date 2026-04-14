from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Any

import requests
from config import settings
from bankroll_manager import recommended_stake
from data_sources import TeamContext

logger = logging.getLogger(__name__)

JSON_SCHEMA_HINT = {
    "prediction": "P1|X|P2|TБ2.5|BTTS|Other",
    "reasoning": "short explanation",
    "confidence": 1,
    "recommended_stake": 0.0,
}


def _team_context_to_text(match: TeamContext) -> str:
    return (
        f"League: {match.league}\n"
        f"Match: {match.home_team} vs {match.away_team}\n"
        f"Odds: {match.odds}\n"
        f"Home xG: {match.home_xg}\n"
        f"Away xG: {match.away_xg}\n"
        f"Home xGA: {match.home_xga}\n"
        f"Away xGA: {match.away_xga}\n"
        f"Home form: {match.home_form}\n"
        f"Away form: {match.away_form}\n"
        f"Injuries: {match.injuries}\n"
        f"Source notes: {match.source_notes}"
    )


def build_prediction_prompt(match: TeamContext, bankroll: float, confidence: float = 0.6) -> str:
    stake = recommended_stake(bankroll, confidence, strategy="flat")
    return (
        "You are a football betting analyst. Return ONLY valid JSON, no markdown. "
        f"Use this schema hint: {json.dumps(JSON_SCHEMA_HINT, ensure_ascii=False)}.\n"
        f"{_team_context_to_text(match)}\n"
        f"Metadata: {json.dumps(match.metadata, ensure_ascii=False)}\n"
        f"Current bankroll: {bankroll}\n"
        f"Suggested stake: {stake}\n"
        "Prefer safe, conservative recommendations and explicitly mention uncertainty."
    )


def _extract_json_payload(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found")
    return json.loads(text[start : end + 1])


def _normalize_prediction_payload(payload: dict[str, Any], bankroll: float) -> dict[str, Any]:
    prediction = str(payload.get("prediction") or payload.get("pick") or "BTTS")
    reasoning = str(payload.get("reasoning") or payload.get("analysis") or "")
    confidence = int(payload.get("confidence") or 3)
    recommended_stake_value = payload.get("recommended_stake")
    if recommended_stake_value is None:
        recommended_stake_value = recommended_stake(bankroll, confidence / 5.0, strategy="flat")
    return {
        "prediction": prediction,
        "reasoning": reasoning,
        "confidence": max(1, min(confidence, 5)),
        "recommended_stake": float(recommended_stake_value),
        "raw": payload,
    }


# ── HuggingFace InferenceClient ────────────────────────────────────────────

def _call_hf_inference_client(prompt: str, max_tokens: int = 512) -> str:
    """Call HuggingFace Inference API using InferenceClient (huggingface_hub)."""
    try:
        from huggingface_hub import InferenceClient  # type: ignore

        model = settings.hf_model or "mistralai/Mistral-7B-Instruct-v0.3"
        token = settings.hf_api_token or None
        client = InferenceClient(model=model, token=token)
        response = client.text_generation(
            prompt,
            max_new_tokens=max_tokens,
            temperature=0.2,
            do_sample=True,
        )
        return str(response)
    except Exception as exc:
        logger.warning("HF InferenceClient error: %s", exc)
        raise


def _call_openai(prompt: str) -> dict[str, Any]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.openai_model,
            "messages": [
                {"role": "system", "content": "You are a concise football betting analyst."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        },
        timeout=45,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return _extract_json_payload(content)


def _call_huggingface(prompt: str) -> dict[str, Any]:
    """Call HuggingFace — tries InferenceClient first, falls back to raw HTTP."""
    if not settings.hf_api_token:
        raise RuntimeError("HF_API_TOKEN is not configured")

    # Try InferenceClient first
    try:
        text = _call_hf_inference_client(prompt)
        return _extract_json_payload(text)
    except Exception:
        pass

    # Fallback: raw HTTP inference endpoint
    response = requests.post(
        f"{settings.hf_inference_url}/{settings.hf_model}",
        headers={
            "Authorization": f"Bearer {settings.hf_api_token}",
            "Content-Type": "application/json",
        },
        json={
            "inputs": prompt,
            "parameters": {
                "max_new_tokens": 512,
                "temperature": 0.2,
                "return_full_text": False,
            },
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list) and data:
        generated = data[0].get("generated_text", "")
    elif isinstance(data, dict):
        generated = data.get("generated_text", "") or data.get("text", "")
    else:
        generated = ""
    return _extract_json_payload(str(generated))


# ── News Analysis ──────────────────────────────────────────────────────────

def analyze_news_sentiment(news_summary: str, team_name: str) -> str:
    """
    Analyse news and return one of: Positive / Negative / Neutral.
    Uses HF InferenceClient if available, else returns Neutral.
    """
    prompt = (
        f"You are a football news analyst. Evaluate the following news for team '{team_name}'.\n"
        f"News:\n{news_summary}\n\n"
        "Reply with ONLY one word: Positive, Negative, or Neutral."
    )
    try:
        if settings.ai_provider in {"hf", "huggingface"} and settings.hf_api_token:
            result = _call_hf_inference_client(prompt, max_tokens=10).strip().split()[0]
            if result.lower() in {"positive", "negative", "neutral"}:
                return result.capitalize()
        elif settings.ai_provider == "openai" and settings.openai_api_key:
            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
                json={
                    "model": settings.openai_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 5,
                    "temperature": 0.0,
                },
                timeout=20,
            )
            response.raise_for_status()
            word = response.json()["choices"][0]["message"]["content"].strip().split()[0]
            if word.lower() in {"positive", "negative", "neutral"}:
                return word.capitalize()
    except Exception as exc:
        logger.warning("Sentiment analysis failed: %s", exc)
    return "Neutral"


def generate_russian_post(
    match: TeamContext,
    prediction: str,
    reasoning: str,
    confidence: int,
    stake: float,
    bankroll: float,
    news_sentiment_home: str = "Neutral",
    news_sentiment_away: str = "Neutral",
) -> str:
    """Generate a Russian-language channel post using HF/OpenAI."""
    odds_home = match.odds.get("home", "?") if match.odds else "?"
    odds_away = match.odds.get("away", "?") if match.odds else "?"
    odds_draw = match.odds.get("draw", "?") if match.odds else "?"

    prompt = (
        "Ты профессиональный каппер. Напиши пост для Telegram-канала о ставке на футбол.\n"
        f"Матч: {match.home_team} vs {match.away_team} ({match.league})\n"
        f"xG хозяев: {match.home_xg}, xG гостей: {match.away_xg}\n"
        f"Форма хозяев: {match.home_form}, Форма гостей: {match.away_form}\n"
        f"Травмы: {match.injuries}\n"
        f"Коэффициенты: П1={odds_home}, X={odds_draw}, П2={odds_away}\n"
        f"Новостной фон хозяев: {news_sentiment_home}, гостей: {news_sentiment_away}\n"
        f"Прогноз AI: {prediction} (уверенность {confidence}/5)\n"
        f"Обоснование: {reasoning}\n"
        f"Рекомендуемая ставка: {stake:.2f} руб. (от банка {bankroll:.2f} руб.)\n\n"
        "Напиши красивый пост с эмодзи, заголовком, анализом и рекомендацией. "
        "Тон: профессиональный каппер. Язык: русский. Длина: 200-300 символов."
    )

    try:
        if settings.ai_provider in {"hf", "huggingface"} and settings.hf_api_token:
            return _call_hf_inference_client(prompt, max_tokens=400).strip()
        elif settings.ai_provider == "openai" and settings.openai_api_key:
            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"},
                json={
                    "model": settings.openai_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 400,
                    "temperature": 0.7,
                },
                timeout=30,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("Russian post generation failed: %s", exc)

    # Fallback: шаблонный пост
    return (
        f"⚽️ {match.home_team} vs {match.away_team}\n"
        f"🏆 {match.league}\n\n"
        f"📊 xG: {match.home_xg} vs {match.away_xg}\n"
        f"📈 Форма: {match.home_form} / {match.away_form}\n\n"
        f"🎯 Прогноз: {prediction}\n"
        f"💡 {reasoning[:100]}\n\n"
        f"💰 Ставка: {stake:.2f} руб. | Кф: {odds_home}\n"
        f"⭐️ Уверенность: {'★' * confidence}{'☆' * (5 - confidence)}"
    )


def mock_predict(match: TeamContext, bankroll: float) -> dict[str, object]:
    prompt = build_prediction_prompt(match, bankroll)
    return {
        "prediction": "BTTS",
        "reasoning": "Mock response generated by the local scaffold. Replace with a real LLM or inference endpoint.",
        "confidence": 3,
        "recommended_stake": recommended_stake(bankroll, 0.6, strategy="flat"),
        "prompt": prompt,
        "match": asdict(match),
    }


def generate_prediction(match: TeamContext, bankroll: float) -> dict[str, object]:
    prompt = build_prediction_prompt(match, bankroll)
    try:
        if settings.ai_provider == "openai":
            payload = _call_openai(prompt)
            normalized = _normalize_prediction_payload(payload, bankroll)
            normalized["prompt"] = prompt
            normalized["match"] = asdict(match)
            return normalized
        if settings.ai_provider in {"hf", "huggingface"}:
            payload = _call_huggingface(prompt)
            normalized = _normalize_prediction_payload(payload, bankroll)
            normalized["prompt"] = prompt
            normalized["match"] = asdict(match)
            return normalized
    except Exception as exc:
        return {
            "prediction": "BTTS",
            "reasoning": f"Provider error, fallback used: {exc}",
            "confidence": 2,
            "recommended_stake": recommended_stake(bankroll, 0.4, strategy="flat"),
            "prompt": prompt,
            "match": asdict(match),
        }

    return mock_predict(match, bankroll)


def build_retro_report(predictions: list[dict[str, object]]) -> str:
    payload = {
        "total_predictions": len(predictions),
        "predictions": predictions,
        "summary": "Mock retrospective report. Replace with LLM analysis for real audits.",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
