"""Classify which Alpaca paper book a process is looking at.

Staging (laptop, PKPIWG, account PA310V54AWBY as of 2026-09-01) is isolated.
Contest (Frank's EC2, PK2UEW) must never appear on the laptop dashboard.
"""

from __future__ import annotations

CONTEST_KEY_PREFIX = "PK2UEW"
STAGING_KEY_PREFIX = "PKPIWG"


def describe_broker_account(
    env: str,
    key_prefix: str,
    account_number: str,
    *,
    paper: bool = True,
) -> dict:
    prefix = (key_prefix or "")[:6].upper()
    number = (account_number or "").strip() or "--"
    venue = "Paper" if paper else "LIVE"

    if prefix.startswith(CONTEST_KEY_PREFIX):
        return {
            "book": "CONTEST",
            "tone": "danger",
            "headline": f"{venue} trading — CONTEST book",
            "detail": (
                f"{venue} Account {number} · key {prefix or '--'}… "
                "This is Frank's $100k hackathon account. Stop."
            ),
        }
    if env == "staging" or prefix.startswith(STAGING_KEY_PREFIX):
        return {
            "book": "STAGING",
            "tone": "ok",
            "headline": f"{venue} trading — STAGING book",
            "detail": (
                f"{venue} Account {number} · key {prefix or '--'}… "
                "Isolated laptop paper. Contest account is not used."
            ),
        }
    return {
        "book": (env or "unknown").upper(),
        "tone": "warn",
        "headline": f"{venue} trading — {(env or 'unknown').upper()} book",
        "detail": f"{venue} Account {number} · key {prefix or '--'}…",
    }
