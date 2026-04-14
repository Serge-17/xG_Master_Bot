"""initial schema

Revision ID: 0001_initial
Revises: None
Create Date: 2026-04-13 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("telegram_id", sa.Integer(), nullable=False),
        sa.Column("bankroll", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("total_winnings", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("total_losses", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("total_bets", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_wins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_refunds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bankroll_strategy", sa.String(length=20), nullable=False, server_default="flat"),
        sa.Column("flat_percent", sa.Float(), nullable=False, server_default="0.03"),
        sa.Column("kelly_fraction_limit", sa.Float(), nullable=False, server_default="0.25"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
    )
    op.create_index(op.f("ix_users_id"), "users", ["id"], unique=False)
    op.create_index(op.f("ix_users_telegram_id"), "users", ["telegram_id"], unique=False)

    op.create_table(
        "predictions",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("match_info", sa.String(length=255), nullable=False),
        sa.Column("ai_prediction", sa.String(length=80), nullable=False),
        sa.Column("ai_reasoning", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("recommended_amount", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("placed_amount", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("outcome", sa.String(length=20), nullable=False, server_default="Pending"),
        sa.Column("result_recorded_at", sa.DateTime(), nullable=True),
        sa.Column("telegram_message_id", sa.Integer(), nullable=True),
        sa.Column("result_source", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_predictions_id"), "predictions", ["id"], unique=False)

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("prediction_id", sa.Integer(), sa.ForeignKey("predictions.id"), nullable=True),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("new_bankroll", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_transactions_id"), "transactions", ["id"], unique=False)

    op.create_table(
        "uploaded_coupons",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("prediction_id", sa.Integer(), sa.ForeignKey("predictions.id"), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=False),
        sa.Column("ocr_status", sa.String(length=30), nullable=False, server_default="Pending"),
        sa.Column("ocr_recognized_outcome", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("ocr_recognized_amount", sa.Float(), nullable=True),
        sa.Column("ocr_recognized_odds", sa.Float(), nullable=True),
        sa.Column("local_file_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_uploaded_coupons_id"), "uploaded_coupons", ["id"], unique=False)


def downgrade() -> None:
    op.drop_table("uploaded_coupons")
    op.drop_table("transactions")
    op.drop_table("predictions")
    op.drop_table("users")
