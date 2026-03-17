# crm_providers/hubspot/__init__.py — HubSpot Provider Orchestrator
#
# Wires together all HubSpot integration pieces into a single CRMProvider
# that the core pipeline can call without knowing HubSpot API details.
#
# Delegates to:
#   oauth.py    — Token refresh
#   inbound.py  — Webhook normalization + signature verification
#   sync.py     — Data sync (conversations, deals, contacts)
#   logger.py   — Activity logging (SMS, calls, notes)
#   resolver.py — Contact search/resolution
#   crm_card.py — CRM Card data

import logging
import time

from crm_providers.base import CRMProvider, CRMEvent, SyncResult

logger = logging.getLogger(__name__)


class HubSpotProvider(CRMProvider):
    """
    Full HubSpot CRM integration provider.

    Capabilities:
        - OAuth2 with 6-hour token expiry + auto-refresh
        - Inbound webhooks (batched, HMAC-SHA256 v3 signature)
        - Incremental data sync (conversations, deals, contacts)
        - Activity logging (SMS, calls, notes back to HubSpot timeline)
        - Contact resolution via CRM v3 Search API
        - CRM Card for HubSpot sidebar (AI intelligence, zero cost)
    """

    CRM_NAME = "HubSpot"
    CRM_TYPE = "hubspot"

    HAS_INBOUND_WEBHOOKS = True
    HAS_OAUTH = True
    HAS_DATA_SYNC = True
    HAS_ACTIVITY_LOGGING = True
    HAS_MARKETPLACE = True
    HAS_EMBEDDABLE_UI = True

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ INBOUND EVENTS ═════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def normalize_webhook(self, request_data, headers=None):
        """
        Convert raw HubSpot webhook payload → canonical CRMEvent.

        HubSpot sends batched arrays. This handles a single event dict
        (caller iterates the batch).
        """
        from crm_providers.hubspot.inbound import (
            normalize_hubspot_event, HUBSPOT_EVENT_MAP,
        )

        if not isinstance(request_data, dict):
            return None

        subscription_type = request_data.get("subscriptionType", "")
        if subscription_type not in HUBSPOT_EVENT_MAP:
            return None

        # Caller must provide subscriber in request_data["_subscriber"]
        subscriber = request_data.get("_subscriber", {})
        if not subscriber:
            return None

        payload = normalize_hubspot_event(request_data, subscriber)
        if not payload:
            return None

        return CRMEvent(
            event_type=payload.get("event_type", ""),
            crm_source="hubspot",
            contact_id=payload.get("contact_id", ""),
            location_id=payload.get("location_id", ""),
            contact={
                "id": payload.get("contact_id", ""),
                "firstName": payload.get("first_name", ""),
                "lastName": payload.get("last_name", ""),
                "email": payload.get("email", ""),
                "phone": payload.get("phone", ""),
            },
            message={
                "body": payload.get("body", ""),
                "direction": "inbound",
            },
            subscriber=subscriber,
            raw_payload=payload,
        )

    def verify_webhook_signature(self, request_data, headers, signature=None):
        """Verify HubSpot v3 HMAC-SHA256 signature."""
        from crm_providers.hubspot.inbound import verify_hubspot_signature_v3

        sig = signature or (headers or {}).get("X-HubSpot-Signature-v3", "")
        timestamp = (headers or {}).get("X-HubSpot-Request-Timestamp", "")

        body = request_data if isinstance(request_data, bytes) else b""
        return verify_hubspot_signature_v3(body, sig, timestamp)

    def get_webhook_event_type_map(self):
        """
        Map IGB trigger names → HubSpot event type strings.

        Used by the workflow engine to match triggers to HubSpot events.
        """
        return {
            "contact_created": "ContactCreate",
            "sms_received": "InboundMessage",
            "field_updated": "ContactUpdate",
            "stage_changed": "DealUpdate",
            "tag_added": "ContactUpdate",       # HubSpot uses property changes
            "tag_removed": "ContactUpdate",
        }

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ TOKEN LIFECYCLE ════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def get_valid_token(self, subscriber):
        """
        Get a valid HubSpot API token, auto-refreshing if expired.

        HubSpot OAuth tokens expire every 6 hours. We check token_expires_at
        and refresh proactively with a 10-minute buffer.

        Returns: (token, was_refreshed, error)
        """
        crm_config = subscriber.get("crm_config") or {}
        token = crm_config.get("access_token", "")
        expires_at = crm_config.get("token_expires_at", 0)

        if not token:
            return (None, False, "no_token")

        # Check expiry with 10-minute buffer
        now = int(time.time())
        if expires_at and now < (expires_at - 600):
            return (token, False, None)

        # Token expired or expiring soon — refresh
        new_token = self.refresh_token(subscriber)
        if new_token:
            return (new_token, True, None)

        # Refresh failed but token might still work for a bit
        if token:
            return (token, False, "refresh_failed")

        return (None, False, "token_expired")

    def refresh_token(self, subscriber):
        """
        Force-refresh HubSpot OAuth token.

        Returns new access token or None.
        """
        from crm_providers.hubspot.oauth import refresh_hubspot_token

        result = refresh_hubspot_token(subscriber)
        if result:
            return result.get("access_token")
        return None

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ DATA SYNC ══════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def sync_conversations(self, location_id, token, since=None):
        """Sync HubSpot communications → crm_conversations table."""
        from crm_providers.hubspot.sync import sync_hubspot_conversations
        return sync_hubspot_conversations(location_id, token, since=since)

    def sync_deals(self, location_id, token, since=None):
        """Sync HubSpot deals → crm_deals table."""
        from crm_providers.hubspot.sync import sync_hubspot_deals
        return sync_hubspot_deals(location_id, token, since=since)

    def sync_contacts(self, location_id, token, since=None):
        """Sync HubSpot contacts → contacts table."""
        from crm_providers.hubspot.sync import sync_hubspot_contacts
        return sync_hubspot_contacts(location_id, token, since=since)

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ ACTIVITY LOGGING ═════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def log_outbound_sms(self, contact_id, message, token, **kwargs):
        """Log outbound SMS as HubSpot Communication."""
        from crm_providers.hubspot.logger import log_outbound_sms
        return log_outbound_sms(contact_id, message, token, **kwargs)

    def log_call(self, contact_id, direction, duration, token, **kwargs):
        """Log call as HubSpot Call engagement."""
        from crm_providers.hubspot.logger import log_call
        return log_call(contact_id, direction, duration, token, **kwargs)

    def log_note(self, contact_id, note, token, **kwargs):
        """Log note on HubSpot contact."""
        from crm_providers.hubspot.logger import log_note
        return log_note(contact_id, note, token, **kwargs)

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ CONTACT RESOLUTION ═══════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def resolve_contact(self, phone=None, name=None, email=None, token=None):
        """Search HubSpot for contact by phone/name/email."""
        from crm_providers.hubspot.resolver import resolve_contact
        return resolve_contact(phone=phone, name=name, email=email,
                               access_token=token)

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ EMBEDDABLE UI ════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def get_crm_card_data(self, contact_id, subscriber):
        """
        Return AI intelligence for CRM Card display.
        Reads from cache only — zero AI cost.
        """
        from crm_providers.hubspot.crm_card import _get_intelligence

        location_id = subscriber.get("location_id", "")
        return _get_intelligence(contact_id, location_id)
