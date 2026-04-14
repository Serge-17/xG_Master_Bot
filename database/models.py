from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from . import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    bankroll: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_winnings: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_losses: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_bets: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_refunds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    bankroll_strategy: Mapped[str] = mapped_column(String(20), default="flat", nullable=False)
    flat_percent: Mapped[float] = mapped_column(Float, default=0.03, nullable=False)
    kelly_fraction_limit: Mapped[float] = mapped_column(Float, default=0.25, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    predictions: Mapped[list["Prediction"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    coupons: Mapped[list["UploadedCoupon"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    match_info: Mapped[str] = mapped_column(String(255), nullable=False)
    ai_prediction: Mapped[str] = mapped_column(String(80), nullable=False)
    ai_reasoning: Mapped[str] = mapped_column(Text, default="", nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    recommended_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    placed_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), default="Pending", nullable=False)
    result_recorded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    user: Mapped["User"] = relationship(back_populates="predictions")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    prediction_id: Mapped[int | None] = mapped_column(ForeignKey("predictions.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    new_bankroll: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    user: Mapped["User"] = relationship(back_populates="transactions")


class UploadedCoupon(Base):
    __tablename__ = "uploaded_coupons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    prediction_id: Mapped[int | None] = mapped_column(ForeignKey("predictions.id"), nullable=True)
    image_url: Mapped[str] = mapped_column(Text, nullable=False)
    ocr_status: Mapped[str] = mapped_column(String(30), default="Pending", nullable=False)
    ocr_recognized_outcome: Mapped[str] = mapped_column(String(20), default="", nullable=False)
    ocr_recognized_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    ocr_recognized_odds: Mapped[float | None] = mapped_column(Float, nullable=True)
    local_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    user: Mapped["User"] = relationship(back_populates="coupons")
