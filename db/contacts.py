# db/contacts.py — Contact messages, cache, and sync
#
# Handles contact message persistence, local contact cache for the dialer,
# and message sync from CRM webhooks.

from db_legacy import (
    get_message_count,
    sync_messages_to_db,
    get_cached_contacts,
    get_contact_cache_age,
    get_contact_cache_count,
    upsert_contact_cache,
)

__all__ = [
    "get_message_count",
    "sync_messages_to_db",
    "get_cached_contacts",
    "get_contact_cache_age",
    "get_contact_cache_count",
    "upsert_contact_cache",
]
