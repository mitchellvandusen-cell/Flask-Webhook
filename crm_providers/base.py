# crm_providers/base.py — CRM Provider Abstract Base Class
#
# Full CRM integration provider that subsumes the outbound-only CRMAdapter
# pattern with inbound webhooks, data sync, activity logging, token lifecycle,
# contact resolution, and embeddable UI support.
#
# Each CRM (GHL, HubSpot, Salesforce, etc.) implements this as a self-contained
# package in crm_providers/<crm_name>/.

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CRMEvent:
    """Canonical event schema — normalized from any CRM's webhook payload."""
    event_type: str                     # e.g. "message.inbound", "contact.created", "deal.updated"
    crm_source: str                     # e.g. "ghl", "hubspot", "salesforce", "standalone"
    contact_id: str = ""                # CRM-native contact ID
    location_id: str = ""               # IGB subscriber location ID
    contact: dict = field(default_factory=dict)   # {id, phone, firstName, lastName, email, ...}
    message: dict = field(default_factory=dict)    # {body, direction, type, ...}
    subscriber: dict = field(default_factory=dict) # Full subscriber row
    raw_payload: dict = field(default_factory=dict) # Original CRM payload for debugging


@dataclass
class SyncResult:
    """Result of a data sync operation."""
    synced: int = 0
    updated: int = 0
    errors: int = 0
    next_cursor: Optional[str] = None
    error_message: Optional[str] = None


class CRMProvider(ABC):
    """
    Full CRM integration provider.

    Each CRM implements this interface as a package:
        crm_providers/ghl/       — GoHighLevel (wraps existing code)
        crm_providers/hubspot/   — HubSpot (new)
        crm_providers/salesforce/ — Salesforce (future)
        crm_providers/standalone/ — IGB as CRM (future)

    The provider orchestrates:
        - Inbound event handling (webhooks/polling)
        - Token lifecycle (OAuth, refresh)
        - Data sync (conversations, deals, contacts)
        - Activity logging back to CRM
        - Contact resolution (search/validate)
        - Embeddable UI data (CRM cards)
        - Outbound operations (delegates to existing CRMAdapter)
    """

    # Override in subclass
    CRM_NAME: str = "Unknown"
    CRM_TYPE: str = "unknown"  # Registry key — matches subscriber.crm_type

    # Capability flags — subclasses set these
    HAS_INBOUND_WEBHOOKS: bool = False
    HAS_OAUTH: bool = False
    HAS_DATA_SYNC: bool = False
    HAS_ACTIVITY_LOGGING: bool = False
    HAS_MARKETPLACE: bool = False
    HAS_EMBEDDABLE_UI: bool = False

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ INBOUND EVENTS ═════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def normalize_webhook(self, request_data: dict, headers: dict = None) -> Optional[CRMEvent]:
        """
        Convert raw CRM webhook payload → canonical CRMEvent.
        Returns None if the event should be ignored.
        Override in subclass for CRM-specific normalization.
        """
        return None

    def verify_webhook_signature(self, request_data: bytes, headers: dict,
                                  signature: str = None) -> bool:
        """
        Verify webhook authenticity using CRM-specific signature scheme.
        Default: no verification (override for production CRMs).
        """
        return True

    def get_webhook_event_type_map(self) -> dict:
        """
        Map IGB trigger names → CRM-specific event type strings.
        Used by the workflow engine to match triggers.
        Returns: {"contact_created": "CRMEventName", ...}
        """
        return {}

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ TOKEN LIFECYCLE ════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def get_valid_token(self, subscriber: dict) -> tuple:
        """
        Get a valid API token for the subscriber.

        Returns: (token: str|None, was_refreshed: bool, error: str|None)

        Default implementation reads from crm_config.access_token.
        Override for CRM-specific OAuth refresh logic.
        """
        crm_config = subscriber.get("crm_config") or {}
        token = crm_config.get("access_token", "")
        if token:
            return (token, False, None)
        return (None, False, "no_token")

    def refresh_token(self, subscriber: dict) -> Optional[str]:
        """
        Force-refresh OAuth token.
        Returns new token or None on failure.
        Override for CRM-specific refresh logic.
        """
        return None

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ DATA SYNC ══════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def sync_conversations(self, location_id: str, token: str,
                           since: str = None) -> SyncResult:
        """
        Incremental conversation/message sync → crm_conversations table.
        Override for CRM-specific sync logic.
        """
        return SyncResult()

    def sync_deals(self, location_id: str, token: str,
                   since: str = None) -> SyncResult:
        """
        Incremental deal/opportunity sync → crm_deals table.
        Override for CRM-specific sync logic.
        """
        return SyncResult()

    def sync_contacts(self, location_id: str, token: str,
                      since: str = None) -> SyncResult:
        """
        Incremental contact sync → contacts table.
        Override for CRM-specific sync logic.
        """
        return SyncResult()

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ ACTIVITY LOGGING (back to CRM) ═════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def log_outbound_sms(self, contact_id: str, message: str, token: str,
                          **kwargs) -> bool:
        """
        Log an outbound SMS in the CRM's timeline/activity feed.
        Default: no-op. Override for CRM-specific logging.
        """
        return False

    def log_call(self, contact_id: str, direction: str, duration: int,
                  token: str, **kwargs) -> bool:
        """
        Log a call in the CRM's timeline.
        Default: no-op. Override for CRM-specific logging.
        """
        return False

    def log_note(self, contact_id: str, note: str, token: str,
                  **kwargs) -> bool:
        """
        Log a note in the CRM's timeline.
        Default: no-op. Override for CRM-specific logging.
        """
        return False

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ CONTACT RESOLUTION ═════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def resolve_contact(self, phone: str = None, name: str = None,
                        email: str = None, token: str = None) -> Optional[dict]:
        """
        Search CRM for contact by phone/name/email.
        Returns: {id, firstName, lastName, email, phone, ...} or None.
        """
        return None

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ OUTBOUND OPERATIONS (delegates to CRMAdapter) ══════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def get_adapter(self, subscriber_data: dict):
        """
        Return the existing CRMAdapter for outbound operations.
        This bridges the old adapter system with the new provider system.
        """
        from crm_adapters.factory import get_crm_adapter
        return get_crm_adapter(self.CRM_TYPE, subscriber_data)

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ EMBEDDABLE UI ══════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def get_crm_card_data(self, contact_id: str, subscriber: dict) -> Optional[dict]:
        """
        Return data for CRM sidebar card (HubSpot CRM Card, etc.).
        None if not supported. Override in subclass.
        """
        return None

    # ═══════════════════════════════════════════════════════════════════════
    # ═══ UTILITIES ══════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════

    def is_ghl(self) -> bool:
        """Check if this is the GHL provider."""
        return self.CRM_TYPE.lower() in ("ghl", "gohighlevel")

    def __repr__(self):
        return f"<{self.CRM_NAME}Provider>"
