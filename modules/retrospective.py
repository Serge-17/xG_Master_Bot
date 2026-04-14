from __future__ import annotations

from dataclasses import asdict
from statistics import mean

from ..database.crud import get_settled_predictions, get_user_summary
from .ai_analyst import build_retro_report


def _prediction_to_payload(prediction) -> dict[str, object]:
    return {
        "id": prediction.id,
        "match_info": prediction.match_info,
        "ai_prediction": prediction.ai_prediction,
        "confidence": prediction.confidence,
        "recommended_amount": prediction.recommended_amount,
        "placed_amount": prediction.placed_amount,
        "outcome": prediction.outcome,
        "created_at": prediction.created_at.isoformat() if prediction.created_at else None,
        "result_recorded_at": prediction.result_recorded_at.isoformat() if prediction.result_recorded_at else None,
    }


def build_user_retrospective(session, telegram_id: int, limit: int = 30) -> str:
    summary = get_user_summary(session, telegram_id)
    predictions = get_settled_predictions(session, telegram_id=telegram_id, limit=limit)
    payloads = [_prediction_to_payload(prediction) for prediction in predictions]

    total = len(payloads)
    wins = sum(1 for item in payloads if item["outcome"] == "Win")
    losses = sum(1 for item in payloads if item["outcome"] == "Loss")
    refunds = sum(1 for item in payloads if item["outcome"] == "Refund")
    avg_confidence = round(mean([float(item["confidence"]) for item in payloads]), 2) if payloads else 0.0

    report_json = build_retro_report(payloads)
    trend_notes = []
    if payloads:
        high_conf = [item for item in payloads if int(item["confidence"]) >= 4]
        low_conf = [item for item in payloads if int(item["confidence"]) <= 2]
        if high_conf:
            trend_notes.append(f"High confidence sample: {len(high_conf)} bets")
        if low_conf:
            trend_notes.append(f"Low confidence sample: {len(low_conf)} bets")
        trend_notes.append("Check whether xG and market odds were aligned before placing stakes.")
    lines = [
        "Retrospective report",
        f"Settled predictions: {total}",
        f"Wins: {wins}",
        f"Losses: {losses}",
        f"Refunds: {refunds}",
        f"Average confidence: {avg_confidence}/5",
        f"Bankroll: {summary['bankroll']}",
        f"ROI: {summary['roi']}%",
        f"Winrate: {summary['winrate']}%",
        "",
        "Coach notes:",
        *trend_notes,
        "",
        "Raw analysis payload:",
        report_json,
    ]
    return "\n".join(lines)
