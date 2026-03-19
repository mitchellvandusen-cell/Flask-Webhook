# db/agency.py — Agency management and white-label branding
#
# Handles multi-tenant agency operations: member lookup, company linking,
# and white-label config (custom branding for agency dashboards).

from db_legacy import (
    get_agency_by_company_id,
    get_agency_members_by_company_id,
    save_whitelabel_config,
    get_whitelabel_config,
    get_whitelabel_for_user,
    update_agency_company_metadata,
    link_subscriber_to_agency,
)

__all__ = [
    "get_agency_by_company_id",
    "get_agency_members_by_company_id",
    "save_whitelabel_config",
    "get_whitelabel_config",
    "get_whitelabel_for_user",
    "update_agency_company_metadata",
    "link_subscriber_to_agency",
]
