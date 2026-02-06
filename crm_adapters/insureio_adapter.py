# crm_adapters/insureio_adapter.py - Insureio CRM Adapter
# Uses Insureio's consumer/lead API for creating leads.
# Insureio is insurance-industry specific - primarily for lead management and quoting.
#
# Required crm_config keys:
#   api_key: Insureio API key or agent key
#   brand_id: Insureio Brand/Profile ID (required for lead creation)
#   subdomain: Organization subdomain (e.g., "myagency" for myagency.insureio.com)
#   agent_id: (optional) Agent ID for quoting
#
# Authentication: API key via Basic Auth or request body
# Leads: POST https://{subdomain}.insureio.com/consumers.json
# Appointments: Not natively supported - use webhook/Calendly integration

import logging
import requests
from typing import Optional
from crm_adapters.base import CRMAdapter

logger = logging.getLogger(__name__)

INSUREIO_TIMEOUT = 20


class InsureioAdapter(CRMAdapter):
    """
    Insureio CRM adapter for insurance-specific lead management.
    Supports lead creation and webhook-based appointment notifications.
    Insureio does not have a native appointment booking API - appointments
    are handled via Calendly or webhook integrations.
    """

    CRM_NAME = "Insureio"
    SUPPORTS_MESSAGING = False
    SUPPORTS_CALENDAR = False  # No native calendar API
    SUPPORTS_CONTACTS = True

    def __init__(self, subscriber_data: dict):
        super().__init__(subscriber_data)
        self.api_key = self.crm_config.get("api_key", "")
        self.brand_id = self.crm_config.get("brand_id", "")
        self.subdomain = self.crm_config.get("subdomain", "")
        self.agent_id = self.crm_config.get("agent_id", "")
        # Optional webhook URL for sending booking notifications
        self.booking_webhook_url = self.crm_config.get("booking_webhook_url", "")

    @property
    def _base_url(self):
        if self.subdomain:
            return f"https://{self.subdomain}.insureio.com"
        return "https://app.insureio.com"

    @property
    def _auth(self):
        """Basic auth tuple for requests."""
        if self.api_key:
            return (self.api_key, "")
        return None

    def send_message(self, contact_id: str, message: str, **kwargs) -> bool:
        logger.info("Insureio adapter: No native messaging API. Use Zapier or external SMS service.")
        return False

    def get_free_slots(self) -> str:
        logger.info("Insureio adapter: No native calendar API. Using generic response.")
        return "let me check my calendar and get back to you with some times"

    def book_appointment(self, contact_id: str, first_name: str, selected_time: str) -> bool:
        """
        Insureio doesn't have a native appointment API.
        If a booking_webhook_url is configured, send the booking data there.
        Otherwise, log the booking intent for manual follow-up.
        """
        if self.booking_webhook_url:
            payload = {
                "contact_id": contact_id,
                "first_name": first_name,
                "selected_time": selected_time,
                "appointment_title": f"Life Insurance Review {first_name or 'Lead'}",
                "timezone": self.timezone,
                "_source": "InsuranceGrokBot",
                "_event_type": "book_appointment",
            }
            try:
                resp = requests.post(self.booking_webhook_url, json=payload, timeout=INSUREIO_TIMEOUT)
                if resp.status_code in [200, 201]:
                    logger.info(f"Insureio booking webhook sent for {contact_id}")
                    return True
                logger.warning(f"Insureio booking webhook failed: {resp.status_code}")
            except Exception as e:
                logger.error(f"Insureio booking webhook error: {e}")
            return False

        logger.info(f"Insureio: No booking API or webhook. Booking intent logged for {contact_id} at {selected_time}")
        return False

    def get_contact(self, contact_id: str) -> dict:
        # Insureio consumer fetch endpoint
        url = f"{self._base_url}/consumers/{contact_id}.json"
        try:
            resp = requests.get(url, auth=self._auth, timeout=INSUREIO_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                consumer = data.get("consumer", data)
                return {
                    "firstName": consumer.get("first_name", ""),
                    "lastName": consumer.get("last_name", ""),
                    "email": consumer.get("email", ""),
                    "phone": consumer.get("phone", ""),
                }
        except Exception as e:
            logger.error(f"Insureio get_contact error: {e}")
        return {}

    def create_contact(self, contact_data: dict) -> Optional[str]:
        """Create a consumer/lead in Insureio."""
        if not self.brand_id:
            logger.error("Insureio: brand_id required for lead creation")
            return None

        url = f"{self._base_url}/consumers.json"
        payload = {
            "consumer": {
                "brand_id": self.brand_id,
                "first_name": contact_data.get("first_name", ""),
                "last_name": contact_data.get("last_name", ""),
                "email": contact_data.get("email", ""),
                "phone": contact_data.get("phone", ""),
            }
        }

        # Add optional fields if provided
        if contact_data.get("date_of_birth"):
            payload["consumer"]["date_of_birth"] = contact_data["date_of_birth"]
        if contact_data.get("state"):
            payload["consumer"]["state"] = contact_data["state"]
        if contact_data.get("gender"):
            payload["consumer"]["gender"] = contact_data["gender"]

        try:
            resp = requests.post(url, json=payload, auth=self._auth, timeout=INSUREIO_TIMEOUT)
            if resp.status_code in [200, 201]:
                result = resp.json()
                consumer_id = result.get("consumer", {}).get("id") or result.get("id")
                logger.info(f"Insureio consumer created: {consumer_id}")
                return str(consumer_id) if consumer_id else None
            logger.error(f"Insureio create_contact failed: {resp.status_code} | {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Insureio create_contact error: {e}")
        return None

    def validate_credentials(self) -> dict:
        if not self.api_key:
            return {"valid": False, "message": "No Insureio API key configured", "details": {}}
        # Insureio doesn't have a simple "ping" endpoint; validate by checking if we can reach the API
        url = f"{self._base_url}/consumers.json"
        try:
            resp = requests.get(url, auth=self._auth, params={"limit": 1}, timeout=INSUREIO_TIMEOUT)
            if resp.status_code in [200, 401, 403]:
                # 200 = valid, 401/403 = endpoint exists but key may be wrong
                is_valid = resp.status_code == 200
                return {
                    "valid": is_valid,
                    "message": "Insureio API key valid" if is_valid else "Insureio API key may be invalid",
                    "details": {"subdomain": self.subdomain, "brand_id": self.brand_id}
                }
            return {"valid": False, "message": f"Insureio returned {resp.status_code}", "details": {}}
        except Exception as e:
            return {"valid": False, "message": f"Insureio connection error: {e}", "details": {}}
