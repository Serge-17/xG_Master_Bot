from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .models import Prediction, Transaction, UploadedCoupon, User


def get_or_create_user(session: Session, telegram_id: int) -> User:
    user = session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user:
        return user

    user = User(telegram_id=telegram_id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def set_bankroll(session: Session, telegram_id: int, bankroll: float) -> User:
    user = get_or_create_user(session, telegram_id)
    user.bankroll = bankroll
    session.commit()
    session.refresh(user)
    return user


def set_bankroll_preferences(
    session: Session,
    telegram_id: int,
    bankroll_strategy: str | None = None,
    flat_percent: float | None = None,
    kelly_fraction_limit: float | None = None,
) -> User:
    user = get_or_create_user(session, telegram_id)
    if bankroll_strategy is not None:
        user.bankroll_strategy = bankroll_strategy.lower()
    if flat_percent is not None:
        user.flat_percent = flat_percent
    if kelly_fraction_limit is not None:
        user.kelly_fraction_limit = kelly_fraction_limit
    session.commit()
    session.refresh(user)
    return user


def create_prediction(
    session: Session,
    telegram_id: int,
    match_info: str,
    ai_prediction: str,
    ai_reasoning: str,
    confidence: int,
    recommended_amount: float,
) -> Prediction:
    user = get_or_create_user(session, telegram_id)
    prediction = Prediction(
        user_id=user.id,
        match_info=match_info,
        ai_prediction=ai_prediction,
        ai_reasoning=ai_reasoning,
        confidence=confidence,
        recommended_amount=recommended_amount,
    )
    session.add(prediction)
    session.commit()
    session.refresh(prediction)
    return prediction


def record_transaction(
    session: Session,
    telegram_id: int,
    transaction_type: str,
    amount: float,
    prediction_id: int | None = None,
) -> Transaction:
    user = get_or_create_user(session, telegram_id)
    new_bankroll = user.bankroll
    normalized_type = transaction_type.lower()
    if normalized_type == "bet":
        new_bankroll -= amount
    elif normalized_type == "win":
        new_bankroll += amount
    elif normalized_type == "loss":
        new_bankroll -= amount
    elif normalized_type == "refund":
        new_bankroll += amount

    user.bankroll = max(new_bankroll, 0.0)
    if normalized_type == "bet":
        user.total_bets += 1
    elif normalized_type == "win":
        user.total_winnings += amount
        user.total_wins += 1
    elif normalized_type == "loss":
        user.total_losses += amount
    elif normalized_type == "refund":
        user.total_refunds += 1

    transaction = Transaction(
        user_id=user.id,
        prediction_id=prediction_id,
        type=transaction_type.title(),
        amount=amount,
        new_bankroll=user.bankroll,
        created_at=datetime.utcnow(),
    )
    session.add(transaction)
    session.commit()
    session.refresh(transaction)
    return transaction


def record_settlement(
    session: Session,
    telegram_id: int,
    outcome: str,
    stake: float,
    odds: float | None = None,
    prediction_id: int | None = None,
) -> Transaction:
    user = get_or_create_user(session, telegram_id)
    normalized = outcome.lower()

    if normalized == "win":
        payout = stake * float(odds or 1.0)
        user.bankroll = round(user.bankroll + payout, 2)
        user.total_winnings += round(payout - stake, 2)
        user.total_wins += 1
        amount = round(payout, 2)
        tx_type = "Win"
    elif normalized == "refund":
        user.bankroll = round(user.bankroll + stake, 2)
        user.total_refunds += 1
        amount = round(stake, 2)
        tx_type = "Refund"
    elif normalized == "loss":
        user.total_losses += round(stake, 2)
        amount = round(stake, 2)
        tx_type = "Loss"
    else:
        amount = round(stake, 2)
        tx_type = outcome.title()

    user.bankroll = max(user.bankroll, 0.0)
    transaction = Transaction(
        user_id=user.id,
        prediction_id=prediction_id,
        type=tx_type,
        amount=amount,
        new_bankroll=user.bankroll,
        created_at=datetime.utcnow(),
    )
    session.add(transaction)
    session.commit()
    session.refresh(transaction)
    return transaction


def create_coupon(
    session: Session,
    telegram_id: int,
    image_url: str,
    prediction_id: int | None = None,
    ocr_status: str = "Pending",
    ocr_recognized_outcome: str = "",
    ocr_recognized_amount: float | None = None,
    ocr_recognized_odds: float | None = None,
    local_file_path: str | None = None,
) -> UploadedCoupon:
    user = get_or_create_user(session, telegram_id)
    coupon = UploadedCoupon(
        user_id=user.id,
        prediction_id=prediction_id,
        image_url=image_url,
        ocr_status=ocr_status,
        ocr_recognized_outcome=ocr_recognized_outcome,
        ocr_recognized_amount=ocr_recognized_amount,
        ocr_recognized_odds=ocr_recognized_odds,
        local_file_path=local_file_path,
    )
    session.add(coupon)
    session.commit()
    session.refresh(coupon)
    return coupon


def update_coupon_settlement(
    session: Session,
    coupon_id: int,
    ocr_status: str,
    ocr_recognized_outcome: str,
    ocr_recognized_amount: float | None,
    ocr_recognized_odds: float | None,
) -> UploadedCoupon | None:
    coupon = session.get(UploadedCoupon, coupon_id)
    if coupon is None:
        return None
    coupon.ocr_status = ocr_status
    coupon.ocr_recognized_outcome = ocr_recognized_outcome
    coupon.ocr_recognized_amount = ocr_recognized_amount
    coupon.ocr_recognized_odds = ocr_recognized_odds
    session.commit()
    session.refresh(coupon)
    return coupon


def get_user_summary(session: Session, telegram_id: int) -> dict[str, float | int]:
    user = get_or_create_user(session, telegram_id)
    total_turnover = user.total_winnings + user.total_losses
    roi = ((user.total_winnings - user.total_losses) / total_turnover * 100) if total_turnover else 0.0
    winrate = (user.total_wins / user.total_bets * 100) if user.total_bets else 0.0
    return {
        "bankroll": round(user.bankroll, 2),
        "total_winnings": round(user.total_winnings, 2),
        "total_losses": round(user.total_losses, 2),
        "total_bets": user.total_bets,
        "total_wins": user.total_wins,
        "total_refunds": user.total_refunds,
        "bankroll_strategy": user.bankroll_strategy,
        "flat_percent": round(user.flat_percent * 100, 2),
        "kelly_fraction_limit": round(user.kelly_fraction_limit * 100, 2),
        "roi": round(roi, 2),
        "winrate": round(winrate, 2),
    }


def get_recent_predictions(session: Session, telegram_id: int, limit: int = 5) -> list[Prediction]:
    user = get_or_create_user(session, telegram_id)
    statement = (
        select(Prediction)
        .where(Prediction.user_id == user.id)
        .order_by(desc(Prediction.created_at))
        .limit(limit)
    )
    return list(session.scalars(statement).all())


def get_settled_predictions(session: Session, telegram_id: int | None = None, limit: int = 50) -> list[Prediction]:
    statement = select(Prediction).where(Prediction.outcome != "Pending")
    if telegram_id is not None:
        user = get_or_create_user(session, telegram_id)
        statement = statement.where(Prediction.user_id == user.id)
    statement = statement.order_by(desc(Prediction.created_at)).limit(limit)
    return list(session.scalars(statement).all())


def update_prediction_result(
    session: Session,
    prediction_id: int,
    outcome: str,
    result_recorded_at: datetime | None = None,
    placed_amount: float | None = None,
) -> Prediction | None:
    prediction = session.get(Prediction, prediction_id)
    if prediction is None:
        return None

    prediction.outcome = outcome.title()
    prediction.result_recorded_at = result_recorded_at or datetime.utcnow()
    if placed_amount is not None:
        prediction.placed_amount = placed_amount
    session.commit()
    session.refresh(prediction)
    return prediction


def list_telegram_ids(session: Session) -> list[int]:
    statement = select(User.telegram_id).order_by(User.telegram_id.asc())
    return [row[0] for row in session.execute(statement).all()]
