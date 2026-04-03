# crm_adapters/zapier_adapter.py - Zapier Webhook Adapter
# Sends data to Zapier Catch Hook URLs, which then route to any downstream CRM/tool.
# This is the universal connector - if a CRM isn't directly supported, Zapier bridges it.
#
# Required crm_config keys:
#   webhook_url: The Zapier Catch Hook URL (e.g., https://hooks.zapier.com/hooks/catch/123/abc/)
#   message_webhook_url: (optional) Separate hook for messages
#   booking_webhook_url: (optional) Separate hook for bookings
#   contact_webhook_url: (optional) Separate hook for contact creation

import logging
import requests
from typing import Optional
from crm_adapters.base import CRMAdapter

logger = logging.getLogger(__name__)

ZAPIER_TIMEOUT = 15  # seconds


class ZapierAdapter(CRMAdapter):
    """
    Zapier webhook adapter. Sends structured JSON to Catch Hook URLs.
    Zapier then routes data to any connected app (CRM, calendar, email, etc.).

    This adapter supports ALL operations by sending webhooks - the actual
    CRM actions happen inside Zapier's workflow (Zap).
    """

    CRM_NAME = "Zapier"
    SUPPORTS_MESSAGING = True
    SUPPORTS_CALENDAR = True
    SUPPORTS_CONTACTS = True

    def __init__(self, subscriber_data: dict):
        super().__init__(subscriber_data)
        self.webhook_url = self.crm_config.get("webhook_url", "")
        self.message_webhook_url = self.crm_config.get("message_webhook_url", "") or self.webhook_url
        self.booking_webhook_url = self.crm_config.get("booking_webhook_url", "") or self.webhook_url
        self.contact_webhook_url = self.crm_config.get("contact_webhook_url", "") or self.webhook_url

    def _send_webhook(self, url: str, payload: dict, event_type: str) -> bool:
        """Send a webhook to Zapier with retry."""
        if not url:
            logger.error(f"Zapier {event_type}: No webhook URL configured")
            return False

        payload["_event_type"] = event_type
        payload["_source"] = "Omnisconn"
        payload["_location_id"] = self.location_id

        for attempt in range(1, 4):
            try:
                resp = requests.post(url, json=payload, timeout=ZAPIER_TIMEOUT)
                if resp.status_code in [200, 201]:
                    logger.info(f"Zapier {event_type} webhook sent successfully | status={resp.status_code}")
                    return True
                else:
                    logger.warning(f"Zapier {event_type} webhook failed | status={resp.status_code} | attempt={attempt}")
            except requests.RequestException as e:
                logger.warning(f"Zapier {event_type} webhook error | attempt={attempt} | error={e}")

            if attempt < 3:
                import time
                time.sleep(2 ** attempt)

        logger.error(f"Zapier {event_type} webhook failed after 3 attempts")
        return False

    def send_message(self, contact_id: str, message: str, **kwargs) -> bool:
        return self._send_webhook(self.message_webhook_url, {
            "contact_id": contact_id,
            "message": message,
            "message_type": "sms",
            "direction": "outbound",
        }, "send_message")

    def get_free_slots(self) -> str:
        # Zapier is async - can't fetch slots in real-time via webhook
        # Return a generic response; the booking will be handled via webhook
        logger.info("Zapier adapter: Calendar slots not available via webhook - using generic response")
        return "let me check my calendar and get back to you with some times"

    def book_appointment(self, contact_id: str, first_name: str, selected_time: str) -> bool:
        return self._send_webhook(self.booking_webhook_url, {
            "contact_id": contact_id,
            "first_name": first_name,
            "selected_time": selected_time,
            "appointment_title": f"Life Insurance Review {first_name or 'Lead'}",
            "timezone": self.timezone,
        }, "book_appointment")

    def get_contact(self, contact_id: str) -> dict:
        # Zapier webhooks are fire-and-forget; can't fetch data synchronously
        logger.info(f"Zapier adapter: get_contact not available via webhook for {contact_id}")
        return {}

    def create_contact(self, contact_data: dict) -> Optional[str]:
        success = self._send_webhook(self.contact_webhook_url, {
            "first_name": contact_data.get("first_name", ""),
            "last_name": contact_data.get("last_name", ""),
            "email": contact_data.get("email", ""),
            "phone": contact_data.get("phone", ""),
        }, "create_contact")
        # Zapier doesn't return an ID synchronously
        return "zapier_pending" if success else None

    def validate_credentials(self) -> dict:
        if not self.webhook_url:
            return {"valid": False, "message": "No Zapier webhook URL configured", "details": {}}
        return {
            "valid": True,
            "message": "Zapier webhook URL configured",
            "details": {
                "webhook_url": self.webhook_url[:40] + "..." if len(self.webhook_url) > 40 else self.webhook_url,
                "has_separate_hooks": bool(self.crm_config.get("message_webhook_url") or self.crm_config.get("booking_webhook_url")),
            }
        }
