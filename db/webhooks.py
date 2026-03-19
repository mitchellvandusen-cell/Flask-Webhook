# db/webhooks.py — Webhook logging, deduplication, and recovery
#
# Tracks webhook processing for audit, handles failed webhook recovery,
# and provides the activity log for the dashboard.

from db_legacy import (
    log_webhook_event,
    get_auth_failed_messages,
    mark_webhook_log_retried,
    mark_webhook_log_backfill_retried,
    get_webhook_logs,
    get_token_failed_webhook_logs,
    save_failed_webhook_payload,
    get_unretried_failed_webhooks,
    mark_failed_webhook_retried,
)

__all__ = [
    "log_webhook_event",
    "get_auth_failed_messages",
    "mark_webhook_log_retried",
    "mark_webhook_log_backfill_retried",
    "get_webhook_logs",
    "get_token_failed_webhook_logs",
    "save_failed_webhook_payload",
    "get_unretried_failed_webhooks",
    "mark_failed_webhook_retried",
]
