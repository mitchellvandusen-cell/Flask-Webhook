# crm_adapters/ghl_adapter.py - GoHighLevel (Lead Connector) Adapter
# Wraps ALL existing GHL code (ghl_calendar, ghl_api, ghl_message) without modification.
# This is the default adapter - existing subscribers use this automatically.

import logging
from typing import Optional
from crm_adapters.base import CRMAdapter

logger = logging.getLogger(__name__)


class GHLAdapter(CRMAdapter):
    """
    GoHighLevel / Lead Connector CRM adapter.
    Delegates to existing ghl_calendar.py, ghl_api.py, ghl_message.py functions.
    """

    CRM_NAME = "LeadConnector"
    SUPPORTS_MESSAGING = True
    SUPPORTS_CALENDAR = True
    SUPPORTS_CONTACTS = True

    def send_message(self, contact_id: str, message: str, **kwargs) -> bool:
        from ghl_api import has_oauth_credentials
        if not has_oauth_credentials() and not self.access_token:
            logger.warning(f"GHL adapter: no OAuth credentials and no token — "
                          f"cannot send SMS to {contact_id}")
            return False
        from ghl_message import send_sms_via_ghl
        sent, _reason, _http_detail = send_sms_via_ghl(
            contact_id=contact_id,
            message=message,
            access_token=self.access_token,
            location_id=self.location_id
        )
        return sent

    def get_free_slots(self) -> str:
        from ghl_calendar import consolidated_calendar_op
        result = consolidated_calendar_op(
            operation="fetch_slots",
            subscriber_data=self.subscriber_data
        )
        return result or "let me look at my calendar"

    def book_appointment(self, contact_id: str, first_name: str, selected_time: str) -> bool:
        from ghl_calendar import consolidated_calendar_op
        result = consolidated_calendar_op(
            operation="book",
            subscriber_data=self.subscriber_data,
            contact_id=contact_id,
            first_name=first_name,
            selected_time=selected_time
        )
        return bool(result)

    def get_contact(self, contact_id: str) -> dict:
        from ghl_api import fetch_contact_data_from_ghl
        return fetch_contact_data_from_ghl(
            contact_id=contact_id,
            location_id=self.location_id,
            access_token=self.access_token
        ) or {}

    def create_contact(self, contact_data: dict) -> Optional[str]:
        # GHL contact creation is handled via their UI/workflows, not typically via API
        # in this application's flow. Return None (not supported in this context).
        logger.info(f"GHL create_contact not implemented - contacts created via GHL workflows")
        return None

    def get_conversation_history(self, contact_id: str, limit: int = 20) -> list:
        from ghl_api import fetch_targeted_ghl_history
        return fetch_targeted_ghl_history(
            contact_id=contact_id,
            location_id=self.location_id,
            access_token=self.access_token,
            limit=limit
        ) or []

    def validate_credentials(self) -> dict:
        from ghl_calendar import detect_token_type
        token_info = detect_token_type(self.access_token)
        return {
            "valid": bool(self.access_token) and self.access_token != "DEMO",
            "message": f"GHL {token_info.get('version', 'unknown')} token configured",
            "details": token_info
        }
