# crm_providers/ghl/__init__.py — GoHighLevel Provider
#
# Wraps all existing GHL-specific code into the CRMProvider interface.
# ZERO changes to existing GHL modules — pure delegation.

import logging
from typing import Optional

from crm_providers.base import CRMProvider, CRMEvent, SyncResult

logger = logging.getLogger(__name__)


class GHLProvider(CRMProvider):
    """
    GoHighLevel CRM provider.

    Delegates to existing modules:
        - ghl_api.py          → token lifecycle
        - ghl_sync.py         → data sync
        - ghl_logger.py       → activity logging
        - contact_validator.py → contact resolution
        - payload_utils.py     → webhook normalization
    """

    CRM_NAME = "GoHighLevel"
    CRM_TYPE = "ghl"

    HAS_INBOUND_WEBHOOKS = True
    HAS_OAUTH = True
    HAS_DATA_SYNC = True
    HAS_ACTIVITY_LOGGING = True
    HAS_MARKETPLACE = True
    HAS_EMBEDDABLE_UI = False

    # ═══ Inbound Events ═══════════════════════════════════════════════

    def normalize_webhook(self, request_data: dict, headers: dict = None) -> Optional[CRMEvent]:
        """Normalize GHL webhook payload using existing payload_utils."""
        from payload_utils import normalize_payload_universal

        normalized = normalize_payload_universal(request_data)
        if not normalized:
            return None

        contact_id = normalized.get("contactId") or normalized.get("contact_id", "")
        location_id = normalized.get("locationId") or normalized.get("location_id", "")

        return CRMEvent(
            event_type=request_data.get("event_type", "unknown"),
            crm_source="ghl",
            contact_id=contact_id,
            location_id=location_id,
            contact={
                "id": contact_id,
                "firstName": normalized.get("first_name", ""),
                "lastName": normalized.get("last_name", ""),
                "phone": normalized.get("phone", ""),
                "email": normalized.get("email", ""),
            },
            message={
                "body": normalized.get("message", ""),
                "direction": "inbound",
            },
            raw_payload=request_data,
        )

    def verify_webhook_signature(self, request_data: bytes, headers: dict,
                                  signature: str = None) -> bool:
        """GHL webhook signature verification — delegated to webhooks blueprint."""
        import hmac
        import hashlib
        import os

        secret = os.getenv("MARKETPLACE_WEBHOOK_SECRET", "")
        if not secret or not signature:
            return True  # No verification configured

        expected = hmac.new(
            secret.encode(), request_data, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def get_webhook_event_type_map(self) -> dict:
        """GHL event type mapping for workflow triggers."""
        return {
            "contact_created": "ContactCreate",
            "sms_received": "InboundMessage",
            "inbound_call": "InboundCall",
            "missed_call": "MissedCall",
            "voicemail_received": "VoicemailReceived",
            "tag_added": "TagAdded",
            "tag_removed": "TagRemoved",
            "stage_changed": "OpportunityStageUpdate",
            "appointment_booked": "AppointmentCreate",
            "appointment_noshow": "AppointmentNoShow",
            "contact_dnd": "ContactDndUpdate",
            "field_updated": "ContactUpdate",
        }

    # ═══ Token Lifecycle ═══════════════════════════════════════════════

    def get_valid_token(self, subscriber: dict) -> tuple:
        """Delegate to existing GHL token management."""
        from ghl_api import get_valid_token_with_status

        location_id = subscriber.get("location_id", "")
        token, was_refreshed, error = get_valid_token_with_status(
            location_id, subscriber=subscriber
        )
        return (token, was_refreshed, error)

    def refresh_token(self, subscriber: dict) -> Optional[str]:
        """Force-refresh GHL OAuth token."""
        from ghl_api import get_valid_token_with_status

        location_id = subscriber.get("location_id", "")
        token, _, _ = get_valid_token_with_status(
            location_id, subscriber=subscriber, force_refresh=True
        )
        return token

    # ═══ Data Sync ═════════════════════════════════════════════════════

    def sync_conversations(self, location_id: str, token: str,
                           since: str = None) -> SyncResult:
        """Delegate to existing GHL sync engine."""
        try:
            from ghl_sync import sync_messages_for_location
            result = sync_messages_for_location(location_id, token)
            return SyncResult(
                synced=result.get("synced", 0) if isinstance(result, dict) else 0,
                errors=result.get("errors", 0) if isinstance(result, dict) else 0,
            )
        except Exception as e:
            logger.error(f"GHL conversation sync failed: {e}")
            return SyncResult(errors=1, error_message=str(e))

    def sync_deals(self, location_id: str, token: str,
                   since: str = None) -> SyncResult:
        """Delegate to existing GHL sync engine."""
        try:
            from ghl_sync import sync_opportunities_for_location
            result = sync_opportunities_for_location(location_id, token)
            return SyncResult(
                synced=result.get("synced", 0) if isinstance(result, dict) else 0,
                errors=result.get("errors", 0) if isinstance(result, dict) else 0,
            )
        except Exception as e:
            logger.error(f"GHL deal sync failed: {e}")
            return SyncResult(errors=1, error_message=str(e))

    # ═══ Activity Logging ═════════════════════════════════════════════

    def log_outbound_sms(self, contact_id: str, message: str, token: str,
                          **kwargs) -> bool:
        """Delegate to existing GHL logger."""
        try:
            from ghl_logger import log_outbound_sms_to_ghl

            location_id = kwargs.get("location_id", "")
            return log_outbound_sms_to_ghl(
                contact_id=contact_id,
                message=message,
                access_token=token,
                location_id=location_id,
            )
        except Exception as e:
            logger.error(f"GHL SMS logging failed: {e}")
            return False

    def log_call(self, contact_id: str, direction: str, duration: int,
                  token: str, **kwargs) -> bool:
        """Delegate to existing GHL call logger."""
        try:
            from ghl_logger import log_call_to_ghl

            return log_call_to_ghl(
                contact_id=contact_id,
                direction=direction,
                duration=duration,
                access_token=token,
                **kwargs,
            )
        except Exception as e:
            logger.error(f"GHL call logging failed: {e}")
            return False

    # ═══ Contact Resolution ════════════════════════════════════════════

    def resolve_contact(self, phone: str = None, name: str = None,
                        email: str = None, token: str = None,
                        location_id: str = None) -> Optional[dict]:
        """Delegate to existing GHL contact validator."""
        try:
            from contact_validator import search_contact_by_phone

            if phone and location_id:
                result = search_contact_by_phone(location_id, phone, name or "")
                return result
        except Exception as e:
            logger.error(f"GHL contact resolution failed: {e}")
        return None
