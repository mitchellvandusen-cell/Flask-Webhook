# db/__init__.py — Backward-compatible re-export layer
#
# The monolithic db.py (4,749 lines) has been split into focused modules.
# This __init__.py re-exports every public name so that all existing
# `from db import X` statements across the codebase continue to work
# with zero changes.
#
# Module structure:
#   db/pool.py          — Connection pool management
#   db/schema.py        — Alembic migration runner (replaces init_db CREATE TABLE)
#   db/auth.py          — User class, token management, session handling
#   db/subscribers.py   — Subscriber CRUD, bot settings, carrier config
#   db/webhooks.py      — Webhook logging, dedup, recovery
#   db/billing.py       — AI minutes, Stripe integration
#   db/contacts.py      — Contact messages, cache, sync
#   db/api_keys.py      — API key & training token management
#   db/marketplace.py   — GHL marketplace install tracking
#   db/integrations.py  — Discord, Slack, Google Calendar
#   db/agency.py        — Agency management, white-label
#   db/alerts.py        — Persistent dashboard alerts
#
# To add a new function: add it to the appropriate module, then add
# the import here. All external code imports from `db`, never from
# `db.pool` or `db.webhooks` directly.

# ═══════════════════════════════════════════════════════════════
# CONNECTION POOL
# ═══════════════════════════════════════════════════════════════
from db.pool import (
    get_db_connection,
    return_db_connection,
    get_db_connection_with_retry,
    get_pool_stats,
)

# ═══════════════════════════════════════════════════════════════
# SCHEMA / MIGRATIONS
# ═══════════════════════════════════════════════════════════════
from db.schema import init_db

# ═══════════════════════════════════════════════════════════════
# AUTH & USER MODEL
# ═══════════════════════════════════════════════════════════════
from db.auth import (
    User,
    clean_subaccount_contamination,
    backfill_agency_owners_to_subscribers,
    update_subscriber_token,
    update_crm_config_token,
    get_subscribers_needing_token_refresh,
    get_users_needing_reminders,
    mark_reminder_sent,
    mark_email_unsubscribed,
)

# ═══════════════════════════════════════════════════════════════
# SUBSCRIBERS & BOT CONFIG
# ═══════════════════════════════════════════════════════════════
from db.subscribers import (
    get_subscriber_info_sql,
    get_subscriber_info_hybrid,
    get_bot_settings,
    get_bot_settings_by_location,
    save_bot_settings,
    BOT_SETTINGS_DEFAULTS,
    save_contracted_carriers,
    get_contracted_carriers,
)

# ═══════════════════════════════════════════════════════════════
# WEBHOOKS
# ═══════════════════════════════════════════════════════════════
from db.webhooks import (
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

# ═══════════════════════════════════════════════════════════════
# BILLING (AI Minutes)
# ═══════════════════════════════════════════════════════════════
from db.billing import (
    get_ai_minute_balance,
    credit_ai_minutes,
    deduct_ai_minutes,
    get_ai_minute_purchases,
    get_ai_minute_usage,
    audit_ai_minutes,
)

# ═══════════════════════════════════════════════════════════════
# CONTACTS & MESSAGES
# ═══════════════════════════════════════════════════════════════
from db.contacts import (
    get_message_count,
    sync_messages_to_db,
    get_cached_contacts,
    get_contact_cache_age,
    get_contact_cache_count,
    upsert_contact_cache,
)

# ═══════════════════════════════════════════════════════════════
# API KEYS & TRAINING TOKENS
# ═══════════════════════════════════════════════════════════════
from db.api_keys import (
    generate_api_key,
    generate_webhook_secret,
    api_key_prefix,
    create_api_key_for_user,
    revoke_api_key,
    generate_training_token,
    create_training_token_for_user,
    revoke_training_token,
    get_subscriber_by_training_token,
    save_outbound_webhook_url,
    get_subscriber_by_api_key,
    log_api_usage,
    get_api_request_count,
)

# ═══════════════════════════════════════════════════════════════
# MARKETPLACE
# ═══════════════════════════════════════════════════════════════
from db.marketplace import (
    save_marketplace_install,
    mark_install_oauth_complete,
    get_incomplete_installs,
    get_all_marketplace_installs,
    mark_setup_email_sent,
    find_marketplace_email,
    save_uninstall_record,
    delete_subscriber_data,
    save_uninstall_feedback,
    get_uninstall_feedback,
)

# ═══════════════════════════════════════════════════════════════
# INTEGRATIONS (Discord, Slack, Google Calendar)
# ═══════════════════════════════════════════════════════════════
from db.integrations import (
    save_discord_connection,
    get_discord_connection,
    delete_discord_connection,
    save_discord_servers,
    get_discord_servers,
    save_discord_webhook_channel,
    get_discord_webhook_channels,
    delete_discord_webhook_channel,
    save_slack_connection,
    get_slack_connection,
    delete_slack_connection,
    save_slack_workspaces,
    get_slack_workspaces,
    save_google_calendar_config,
    get_google_calendar_config,
    delete_google_calendar_config,
)

# ═══════════════════════════════════════════════════════════════
# AGENCY & WHITE-LABEL
# ═══════════════════════════════════════════════════════════════
from db.agency import (
    get_agency_by_company_id,
    get_agency_members_by_company_id,
    save_whitelabel_config,
    get_whitelabel_config,
    get_whitelabel_for_user,
    update_agency_company_metadata,
    link_subscriber_to_agency,
)

# ═══════════════════════════════════════════════════════════════
# ALERTS
# ═══════════════════════════════════════════════════════════════
from db.alerts import (
    save_persistent_alert,
    get_persistent_alerts,
    dismiss_persistent_alert,
)

# ═══════════════════════════════════════════════════════════════
# ANALYTICS (Conversion Events)
# ═══════════════════════════════════════════════════════════════
from db.analytics import (
    log_conversion_event,
    get_conversion_stats,
    get_conversion_events,
    get_conversion_stats_multi,
)

# ═══════════════════════════════════════════════════════════════
# PUBLIC API — every name importable from `db`
# ═══════════════════════════════════════════════════════════════
__all__ = [
    # Pool
    "get_db_connection", "return_db_connection", "get_db_connection_with_retry", "get_pool_stats",
    # Schema
    "init_db",
    # Auth
    "User", "clean_subaccount_contamination", "backfill_agency_owners_to_subscribers",
    "update_subscriber_token",
    "update_crm_config_token", "get_subscribers_needing_token_refresh",
    "get_users_needing_reminders", "mark_reminder_sent", "mark_email_unsubscribed",
    # Subscribers
    "get_subscriber_info_sql", "get_subscriber_info_hybrid",
    "get_bot_settings", "get_bot_settings_by_location", "save_bot_settings",
    "BOT_SETTINGS_DEFAULTS", "save_contracted_carriers", "get_contracted_carriers",
    # Webhooks
    "log_webhook_event", "get_auth_failed_messages", "mark_webhook_log_retried",
    "mark_webhook_log_backfill_retried", "get_webhook_logs",
    "get_token_failed_webhook_logs", "save_failed_webhook_payload",
    "get_unretried_failed_webhooks", "mark_failed_webhook_retried",
    # Billing
    "get_ai_minute_balance", "credit_ai_minutes", "deduct_ai_minutes",
    "get_ai_minute_purchases", "get_ai_minute_usage", "audit_ai_minutes",
    # Contacts
    "get_message_count", "sync_messages_to_db",
    "get_cached_contacts", "get_contact_cache_age", "get_contact_cache_count",
    "upsert_contact_cache",
    # API Keys
    "generate_api_key", "generate_webhook_secret", "api_key_prefix",
    "create_api_key_for_user", "revoke_api_key",
    "generate_training_token", "create_training_token_for_user", "revoke_training_token",
    "get_subscriber_by_training_token", "save_outbound_webhook_url",
    "get_subscriber_by_api_key", "log_api_usage", "get_api_request_count",
    # Marketplace
    "save_marketplace_install", "mark_install_oauth_complete",
    "get_incomplete_installs", "get_all_marketplace_installs",
    "mark_setup_email_sent", "find_marketplace_email",
    "save_uninstall_record", "delete_subscriber_data",
    "save_uninstall_feedback", "get_uninstall_feedback",
    # Integrations
    "save_discord_connection", "get_discord_connection", "delete_discord_connection",
    "save_discord_servers", "get_discord_servers",
    "save_discord_webhook_channel", "get_discord_webhook_channels",
    "delete_discord_webhook_channel",
    "save_slack_connection", "get_slack_connection", "delete_slack_connection",
    "save_slack_workspaces", "get_slack_workspaces",
    "save_google_calendar_config", "get_google_calendar_config",
    "delete_google_calendar_config",
    # Agency
    "get_agency_by_company_id", "get_agency_members_by_company_id",
    "save_whitelabel_config", "get_whitelabel_config", "get_whitelabel_for_user",
    "update_agency_company_metadata", "link_subscriber_to_agency",
    # Alerts
    "save_persistent_alert", "get_persistent_alerts", "dismiss_persistent_alert",
    # Analytics
    "log_conversion_event", "get_conversion_stats", "get_conversion_events",
    "get_conversion_stats_multi",
]
