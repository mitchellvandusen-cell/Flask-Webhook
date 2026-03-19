# db/subscribers.py — Subscriber CRUD, bot settings, carrier configuration
#
# Core subscriber lookup and configuration persistence.
# Bot settings control tone, behavior, personality of the AI agent.

from db_legacy import (
    get_subscriber_info_sql,
    get_subscriber_info_hybrid,
    get_bot_settings,
    get_bot_settings_by_location,
    save_bot_settings,
    BOT_SETTINGS_DEFAULTS,
    save_contracted_carriers,
    get_contracted_carriers,
)

__all__ = [
    "get_subscriber_info_sql",
    "get_subscriber_info_hybrid",
    "get_bot_settings",
    "get_bot_settings_by_location",
    "save_bot_settings",
    "BOT_SETTINGS_DEFAULTS",
    "save_contracted_carriers",
    "get_contracted_carriers",
]
