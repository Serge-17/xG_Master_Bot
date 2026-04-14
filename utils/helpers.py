from __future__ import annotations

from html import escape


def format_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ")


def safe_html(text: str) -> str:
    return escape(text or "")


def parse_float(value: str) -> float:
    return float(value.replace(",", ".").strip())
