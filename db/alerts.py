# db/alerts.py — Persistent dashboard alerts
#
# Database-backed alert banners that survive page reloads.
# Used for critical notifications (token expiry, A2P status, etc.)

from db_legacy import (
    save_persistent_alert,
    get_persistent_alerts,
    dismiss_persistent_alert,
)

__all__ = [
    "save_persistent_alert",
    "get_persistent_alerts",
    "dismiss_persistent_alert",
]
