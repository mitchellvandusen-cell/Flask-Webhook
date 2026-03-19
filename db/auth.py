# db/auth.py — User model, authentication, and OAuth token management
#
# Contains the Flask-Login User class and all token persistence logic.
# The User class loads from subscribers, agency_billing, or location_users
# tables depending on the user type.

from db_legacy import (
    User,
    clean_subaccount_contamination,
    update_subscriber_token,
    update_crm_config_token,
    get_subscribers_needing_token_refresh,
    get_users_needing_reminders,
    mark_reminder_sent,
)

__all__ = [
    "User",
    "clean_subaccount_contamination",
    "update_subscriber_token",
    "update_crm_config_token",
    "get_subscribers_needing_token_refresh",
    "get_users_needing_reminders",
    "mark_reminder_sent",
]
