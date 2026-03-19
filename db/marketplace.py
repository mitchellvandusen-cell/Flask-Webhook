# db/marketplace.py — GHL marketplace install tracking
#
# Tracks app installs from the GoHighLevel marketplace, handles uninstall
# cleanup, and manages setup email delivery status.

from db_legacy import (
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

__all__ = [
    "save_marketplace_install",
    "mark_install_oauth_complete",
    "get_incomplete_installs",
    "get_all_marketplace_installs",
    "mark_setup_email_sent",
    "find_marketplace_email",
    "save_uninstall_record",
    "delete_subscriber_data",
    "save_uninstall_feedback",
    "get_uninstall_feedback",
]
