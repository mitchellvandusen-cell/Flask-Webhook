# crm_adapters/base.py - Abstract Base for All CRM Adapters
# Every CRM adapter must implement these methods.
# The system calls these uniformly regardless of which CRM is active.

import logging
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)


class CRMAdapter(ABC):
    """
    Unified CRM interface. Each CRM adapter implements this contract.

    Methods:
        send_message()         - Send SMS/message to a contact
        get_free_slots()       - Fetch available calendar slots
        book_appointment()     - Create an appointment/event
        get_contact()          - Fetch contact details by ID
        search_contact()       - Search for a contact by email/phone
        create_contact()       - Create a new contact/lead
        update_contact()       - Update an existing contact
        validate_credentials() - Test if credentials are valid
    """

    # Human-readable CRM name (override in subclass)
    CRM_NAME = "Unknown"

    # Capabilities flags - adapters set these based on what they support
    SUPPORTS_MESSAGING = False
    SUPPORTS_CALENDAR = False
    SUPPORTS_CONTACTS = False

    def __init__(self, subscriber_data: dict):
        """
        Initialize with subscriber-specific credentials.

        subscriber_data contains at minimum:
            - access_token: API key / OAuth token / webhook URL
            - location_id: CRM account/location identifier
            - calendar_id: Calendar identifier (if applicable)
            - timezone: Local timezone string
            - crm_user_id: Assigned user/owner ID (if applicable)
            - crm_config: JSON dict with CRM-specific settings
        """
        self.subscriber_data = subscriber_data
        self.access_token = subscriber_data.get("access_token", "")
        self.location_id = subscriber_data.get("location_id", "")
        self.calendar_id = subscriber_data.get("calendar_id", "")
        self.timezone = subscriber_data.get("timezone", "America/Chicago")
        self.crm_user_id = subscriber_data.get("crm_user_id", "")
        self.crm_config = subscriber_data.get("crm_config") or {}
        self.voice_config = subscriber_data.get("voice_config") or {}
        self.sms_send_via = subscriber_data.get("sms_send_via", "")

    @abstractmethod
    def send_message(self, contact_id: str, message: str, **kwargs) -> bool:
        """
        Send a message (SMS, email, etc.) to a contact.
        Returns True if sent successfully.
        """
        pass

    @abstractmethod
    def get_free_slots(self) -> str:
        """
        Fetch available calendar slots.
        Returns a formatted string like "I've got 9:00 AM tomorrow or 2:00 PM Friday"
        or a fallback string if unavailable.
        """
        pass

    @abstractmethod
    def book_appointment(self, contact_id: str, first_name: str, selected_time: str) -> bool:
        """
        Book an appointment for a contact.
        selected_time is a string like "4:00 pm tomorrow".
        Returns True if booked successfully.
        """
        pass

    @abstractmethod
    def get_contact(self, contact_id: str) -> dict:
        """
        Fetch contact details.
        Returns dict with: firstName, lastName, email, phone, etc.
        Returns empty dict if not found or unsupported.
        """
        pass

    @abstractmethod
    def create_contact(self, contact_data: dict) -> Optional[str]:
        """
        Create a new contact/lead in the CRM.
        contact_data: {first_name, last_name, email, phone, ...}
        Returns the new contact ID or None on failure.
        """
        pass

    def search_contact(self, email: str = None, phone: str = None) -> Optional[dict]:
        """
        Search for an existing contact by email or phone.
        Returns dict with {id, firstName, lastName, email, phone} or None if not found.
        Used for deduplication before create_contact().
        Override in subclass for CRM-specific search.
        """
        return None

    def update_contact(self, contact_id: str, contact_data: dict) -> bool:
        """
        Update an existing contact's fields.
        contact_data: {first_name, last_name, email, phone, ...} (only fields to update)
        Returns True if updated successfully.
        Override in subclass for CRM-specific updates.
        """
        return False

    def validate_credentials(self) -> dict:
        """
        Test if the current credentials are valid.
        Returns {"valid": bool, "message": str, "details": dict}
        Override in subclass for CRM-specific validation.
        """
        return {
            "valid": bool(self.access_token),
            "message": "Token present" if self.access_token else "No access token configured",
            "details": {}
        }

    def get_conversation_history(self, contact_id: str, limit: int = 20) -> list:
        """
        Fetch conversation history for a contact.
        Returns list of {"role": "lead"|"assistant", "text": str, "timestamp": str}
        Default: empty list (override if CRM supports it).
        """
        return []

    def get_or_create_contact(self, contact_data: dict) -> Optional[str]:
        """
        Search for an existing contact, create if not found.
        Returns the contact ID or None on failure.
        """
        email = contact_data.get("email", "")
        phone = contact_data.get("phone", "")

        if email or phone:
            existing = self.search_contact(email=email, phone=phone)
            if existing and existing.get("id"):
                logger.info(f"{self.CRM_NAME}: Found existing contact {existing['id']}")
                return existing["id"]

        return self.create_contact(contact_data)

    def __repr__(self):
        return f"<{self.CRM_NAME}Adapter location={self.location_id}>"
