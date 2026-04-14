from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
import requests

from .config import settings
from .database import SessionLocal, init_db
from .database.crud import (
    create_coupon,
    create_prediction,
    get_recent_predictions,
    get_user_summary,
    record_transaction,
    record_settlement,
    set_bankroll,
    set_bankroll_preferences,
    update_prediction_result,
    update_coupon_settlement,
)
from .modules.ai_analyst import generate_prediction
from .modules.bankroll_manager import recommended_stake
from .modules.data_sources import build_match_context
from .modules.ocr_processor import interpret_result, process_coupon_image
from .modules.retrospective import build_user_retrospective
from .modules.scheduler import register_reporting_jobs, scheduler, start_scheduler
from .templates.messages import HELP_MESSAGE, WELCOME_MESSAGE
from .utils.helpers import format_money, parse_float


logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
download_root = Path(settings.telegram_download_dir)
download_root.mkdir(parents=True, exist_ok=True)


def get_session():
    return SessionLocal()


@dp.message(Command("start"))
async def start_handler(message: Message) -> None:
    await message.answer(WELCOME_MESSAGE)


@dp.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(HELP_MESSAGE)


@dp.message(Command("set_bankroll"))
async def set_bankroll_handler(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Usage: /set_bankroll <amount>")
        return

    try:
        bankroll = parse_float(command.args)
    except ValueError:
        await message.answer("Invalid amount.")
        return

    with get_session() as session:
        user = set_bankroll(session, message.from_user.id, bankroll)
    await message.answer(f"Bankroll updated: {format_money(user.bankroll)}")


@dp.message(Command("set_strategy"))
async def set_strategy_handler(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Usage: /set_strategy <flat|kelly> [flat_percent] [kelly_cap]")
        return

    parts = command.args.split()
    strategy = parts[0].lower()
    if strategy not in {"flat", "kelly"}:
        await message.answer("Strategy must be flat or kelly.")
        return

    flat_percent = None
    kelly_cap = None
    try:
        if len(parts) > 1:
            flat_percent = parse_float(parts[1]) / 100.0
        if len(parts) > 2:
            kelly_cap = parse_float(parts[2]) / 100.0
    except ValueError:
        await message.answer("Invalid percent values.")
        return

    with get_session() as session:
        user = set_bankroll_preferences(
            session,
            message.from_user.id,
            bankroll_strategy=strategy,
            flat_percent=flat_percent,
            kelly_fraction_limit=kelly_cap,
        )
    await message.answer(
        f"Strategy updated: {user.bankroll_strategy}, flat {round(user.flat_percent * 100, 2)}%, "
        f"kelly cap {round(user.kelly_fraction_limit * 100, 2)}%"
    )


@dp.message(Command("my_bankroll"))
async def my_bankroll_handler(message: Message) -> None:
    with get_session() as session:
        summary = get_user_summary(session, message.from_user.id)

    await message.answer(
        "\n".join(
            [
                f"Bankroll: {format_money(summary['bankroll'])}",
                f"Total bets: {summary['total_bets']}",
                f"Strategy: {summary['bankroll_strategy']}",
                f"Flat percent: {summary['flat_percent']}%",
                f"Kelly cap: {summary['kelly_fraction_limit']}%",
                f"ROI: {summary['roi']}%",
                f"Winrate: {summary['winrate']}%",
                f"Total winnings: {format_money(summary['total_winnings'])}",
                f"Total losses: {format_money(summary['total_losses'])}",
            ]
        )
    )


@dp.message(Command("my_predictions"))
async def my_predictions_handler(message: Message) -> None:
    with get_session() as session:
        predictions = get_recent_predictions(session, message.from_user.id, limit=5)

    if not predictions:
        await message.answer("No predictions yet.")
        return

    lines = ["Recent predictions:"]
    for prediction in predictions:
        lines.append(
            f"- {prediction.match_info}: {prediction.ai_prediction} "
            f"({prediction.confidence}/5, stake {format_money(prediction.recommended_amount)}, "
            f"outcome {prediction.outcome})"
        )
    await message.answer("\n".join(lines))


@dp.message(Command("predict"))
async def predict_handler(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Usage: /predict <league> <team1> vs <team2>")
        return

    raw = command.args.strip()
    if " vs " not in raw:
        await message.answer("Please use: /predict <league> <team1> vs <team2>")
        return

    league_and_home, away_team = raw.rsplit(" vs ", 1)
    parts = league_and_home.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Please include the league and both teams.")
        return

    league, home_team = parts[0], parts[1]

    with get_session() as session:
        summary = get_user_summary(session, message.from_user.id)
        bankroll = float(summary["bankroll"])
        strategy = str(summary["bankroll_strategy"])
        flat_percent = float(summary["flat_percent"]) / 100.0
        kelly_cap = float(summary["kelly_fraction_limit"]) / 100.0

    match_context = build_match_context(league, home_team, away_team)
    odds_for_strategy = None
    if match_context.odds:
        odds_for_strategy = match_context.odds.get("home")
    ai_prediction = generate_prediction(match_context, bankroll)
    stake = recommended_stake(
        bankroll=bankroll,
        confidence=float(ai_prediction["confidence"]),
        odds=odds_for_strategy,
        strategy=strategy,
        flat_percent=flat_percent,
        kelly_cap=kelly_cap,
    )

    with get_session() as session:
        created = create_prediction(
            session=session,
            telegram_id=message.from_user.id,
            match_info=f"{league} {home_team} vs {away_team}",
            ai_prediction=str(ai_prediction["prediction"]),
            ai_reasoning=str(ai_prediction["reasoning"]),
            confidence=int(ai_prediction["confidence"]),
            recommended_amount=float(stake),
            placed_amount=float(stake),
        )
        record_transaction(
            session=session,
            telegram_id=message.from_user.id,
            transaction_type="Bet",
            amount=float(stake),
            prediction_id=created.id,
        )

    await message.answer(
        "\n".join(
            [
                f"Prediction: {ai_prediction['prediction']}",
                f"Confidence: {ai_prediction['confidence']}/5",
                f"Stake: {format_money(float(stake))}",
                f"Reasoning: {ai_prediction['reasoning']}",
                f"Sources: {match_context.source_notes}",
            ]
        )
    )


@dp.message(Command("upload_coupon"))
async def upload_coupon_handler(message: Message) -> None:
    await message.answer(
        "Send a coupon screenshot as a photo or document. "
        "I will download it, run OCR, and store the result."
    )


async def _download_telegram_file(bot: Bot, file_id: str, suffix: str) -> Path:
    telegram_file = await bot.get_file(file_id)
    target = download_root / f"{file_id}{suffix}"
    file_url = f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{telegram_file.file_path}"
    response = requests.get(file_url, timeout=60)
    response.raise_for_status()
    target.write_bytes(response.content)
    return target


def _settle_coupon_if_possible(session, telegram_id: int, ocr_result: dict[str, object]) -> None:
    outcome_text = str(ocr_result.get("ocr_recognized_outcome") or "")
    amount = ocr_result.get("ocr_recognized_amount")
    odds = ocr_result.get("ocr_recognized_odds")
    if outcome_text.lower() == "pending" or amount is None:
        return

    outcome = interpret_result(outcome_text)
    if outcome in {"Win", "Refund"}:
        record_settlement(
            session=session,
            telegram_id=telegram_id,
            outcome=outcome,
            stake=float(amount),
            odds=float(odds) if odds is not None else None,
        )
    elif outcome == "Loss":
        record_settlement(
            session=session,
            telegram_id=telegram_id,
            outcome=outcome,
            stake=float(amount),
            odds=float(odds) if odds is not None else None,
        )


@dp.message(F.photo)
async def photo_handler(message: Message, bot: Bot) -> None:
    photo = message.photo[-1]
    local_path = await _download_telegram_file(bot, photo.file_id, ".jpg")
    ocr_result = process_coupon_image(str(local_path))

    with get_session() as session:
        coupon = create_coupon(
            session=session,
            telegram_id=message.from_user.id,
            image_url=str(local_path),
            ocr_status=str(ocr_result["ocr_status"]),
            ocr_recognized_outcome=str(ocr_result["ocr_recognized_outcome"]),
            ocr_recognized_amount=ocr_result["ocr_recognized_amount"],
            ocr_recognized_odds=ocr_result.get("ocr_recognized_odds"),
            local_file_path=str(local_path),
        )
        _settle_coupon_if_possible(session, message.from_user.id, ocr_result)
        update_coupon_settlement(
            session=session,
            coupon_id=coupon.id,
            ocr_status=str(ocr_result["ocr_status"]),
            ocr_recognized_outcome=str(ocr_result["ocr_recognized_outcome"]),
            ocr_recognized_amount=ocr_result["ocr_recognized_amount"],
            ocr_recognized_odds=ocr_result.get("ocr_recognized_odds"),
        )

    outcome = interpret_result(str(ocr_result["ocr_recognized_outcome"]))
    await message.answer(
        "\n".join(
            [
                "Coupon received and processed.",
                f"OCR status: {coupon.ocr_status}",
                f"Recognized outcome: {outcome}",
                f"Recognized amount: {coupon.ocr_recognized_amount if coupon.ocr_recognized_amount is not None else 'N/A'}",
                f"Recognized odds: {coupon.ocr_recognized_odds if coupon.ocr_recognized_odds is not None else 'N/A'}",
                f"Stored file: {coupon.local_file_path}",
            ]
        )
    )


@dp.message(F.document)
async def document_handler(message: Message, bot: Bot) -> None:
    document = message.document
    if document is None:
        return
    local_path = await _download_telegram_file(bot, document.file_id, f"_{document.file_name or 'coupon'}")
    ocr_result = process_coupon_image(str(local_path))
    with get_session() as session:
        coupon = create_coupon(
            session=session,
            telegram_id=message.from_user.id,
            image_url=str(local_path),
            ocr_status=str(ocr_result["ocr_status"]),
            ocr_recognized_outcome=str(ocr_result["ocr_recognized_outcome"]),
            ocr_recognized_amount=ocr_result["ocr_recognized_amount"],
            ocr_recognized_odds=ocr_result.get("ocr_recognized_odds"),
            local_file_path=str(local_path),
        )
        _settle_coupon_if_possible(session, message.from_user.id, ocr_result)
        update_coupon_settlement(
            session=session,
            coupon_id=coupon.id,
            ocr_status=str(ocr_result["ocr_status"]),
            ocr_recognized_outcome=str(ocr_result["ocr_recognized_outcome"]),
            ocr_recognized_amount=ocr_result["ocr_recognized_amount"],
            ocr_recognized_odds=ocr_result.get("ocr_recognized_odds"),
        )
    await message.answer(
        "\n".join(
            [
                "Document received and processed.",
                f"OCR status: {coupon.ocr_status}",
                f"Recognized outcome: {coupon.ocr_recognized_outcome}",
                f"Recognized amount: {coupon.ocr_recognized_amount if coupon.ocr_recognized_amount is not None else 'N/A'}",
            ]
        )
    )


@dp.message(Command("record_result"))
async def record_result_handler(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Usage: /record_result <win|loss|refund> <amount>")
        return
    parts = command.args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: /record_result <win|loss|refund> <amount>")
        return
    outcome, amount_raw = parts
    try:
        amount = parse_float(amount_raw)
    except ValueError:
        await message.answer("Invalid amount.")
        return

    with get_session() as session:
        transaction = record_transaction(
            session=session,
            telegram_id=message.from_user.id,
            transaction_type=outcome,
            amount=amount,
        )

    await message.answer(
        f"Recorded {outcome.title()} for {format_money(amount)}. New bankroll: {format_money(transaction.new_bankroll)}"
    )


@dp.message(Command("close_prediction"))
async def close_prediction_handler(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Usage: /close_prediction <id> <win|loss|refund> <amount>")
        return
    parts = command.args.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: /close_prediction <id> <win|loss|refund> <amount>")
        return

    prediction_id_raw, outcome, amount_raw = parts
    try:
        prediction_id = int(prediction_id_raw)
        amount = parse_float(amount_raw)
    except ValueError:
        await message.answer("Invalid prediction id or amount.")
        return

    with get_session() as session:
        prediction = update_prediction_result(session, prediction_id, outcome)
        if prediction is None:
            await message.answer("Prediction not found.")
            return

        record_settlement(
            session=session,
            telegram_id=message.from_user.id,
            outcome=outcome,
            stake=amount,
            odds=None,
            prediction_id=prediction.id,
        )

    await message.answer(
        f"Prediction #{prediction_id} settled as {outcome.title()} for {format_money(amount)}."
    )


@dp.message(Command("retro_report"))
async def retro_report_handler(message: Message) -> None:
    with get_session() as session:
        report = build_user_retrospective(session, message.from_user.id, limit=30)
    await message.answer(report[:3900])


async def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

    init_db()
    bot = Bot(token=settings.telegram_bot_token)
    start_scheduler()
    register_reporting_jobs(bot)
    await dp.start_polling(bot)

