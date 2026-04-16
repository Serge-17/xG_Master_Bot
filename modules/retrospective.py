from __future__ import annotations
from database.crud import get_settled_predictions, get_user_summary

def build_user_retrospective(session, telegram_id: int, limit: int = 30) -> str:
    summary = get_user_summary(session, telegram_id)
    predictions = get_settled_predictions(session, telegram_id=telegram_id, limit=limit)
    
    total = len(predictions)
    wins = sum(1 for p in predictions if p.outcome == "Win")
    
    lines = [
        "📊 <b>Ретро-отчёт за период</b>\n",
        f"Всего ставок: {total}",
        f"Выигрышей: {wins}",
        f"ROI: {summary['roi']}%",
        f"Текущий банк: {summary['bankroll']} руб.",
        "\n<i>ИИ-совет: Продолжайте следовать стратегии флэт для минимизации рисков.</i>"
    ]
    return "\n".join(lines)