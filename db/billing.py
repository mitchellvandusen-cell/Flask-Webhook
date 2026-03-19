# db/billing.py — AI minutes billing (credit, deduct, audit)
#
# Usage-based billing for AI voice processing minutes.
# Supports overdraft protection, purchase history, and full audit trail.

from db_legacy import (
    get_ai_minute_balance,
    credit_ai_minutes,
    deduct_ai_minutes,
    get_ai_minute_purchases,
    get_ai_minute_usage,
    audit_ai_minutes,
)

__all__ = [
    "get_ai_minute_balance",
    "credit_ai_minutes",
    "deduct_ai_minutes",
    "get_ai_minute_purchases",
    "get_ai_minute_usage",
    "audit_ai_minutes",
]
