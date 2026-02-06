# crm_adapters/hubspot_adapter.py - HubSpot CRM API Adapter
# Uses HubSpot CRM API v3 for contacts, meetings, and communications.
#
# Required crm_config keys:
#   access_token: Private App access token (Bearer token, non-expiring)
#   OR
#   access_token: OAuth access token (short-lived, needs refresh)
#   refresh_token: (optional) OAuth refresh token
#   client_id: (optional) App client ID
#   client_secret: (optional) App client secret
#   owner_id: (optional) HubSpot owner ID for assigning meetings
#
# Optional for SMS:
#   twilio_account_sid, twilio_auth_token, twilio_from_number
#   OR messaging_webhook_url
#
# Authentication: Bearer token (Private App or OAuth)
# Contacts: POST https://api.hubapi.com/crm/v3/objects/contacts
# Meetings: POST https://api.hubapi.com/crm/v3/objects/meetings
# Communications: POST https://api.hubapi.com/crm/v3/objects/communications (log SMS)
# Search: POST https://api.hubapi.com/crm/v3/objects/contacts/search

import logging
import requests
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from crm_adapters.base import CRMAdapter

logger = logging.getLogger(__name__)

HUBSPOT_BASE = "https://api.hubapi.com"
HUBSPOT_TIMEOUT = 20


class HubSpotAdapter(CRMAdapter):
    """
    HubSpot CRM adapter using their v3 API.
    Manages contacts, meeting records, and communications timeline.
    """

    CRM_NAME = "HubSpot"
    SUPPORTS_MESSAGING = True  # Via Twilio + HubSpot Communications log
    SUPPORTS_CALENDAR = True
    SUPPORTS_CONTACTS = True

    def __init__(self, subscriber_data: dict):
        super().__init__(subscriber_data)
        self.hs_token = self.crm_config.get("access_token", "") or self.access_token
        self.refresh_token_hs = self.crm_config.get("refresh_token", "")
        self.client_id = self.crm_config.get("client_id", "")
        self.client_secret = self.crm_config.get("client_secret", "")
        self.owner_id = self.crm_config.get("owner_id", "") or self.crm_user_id
        self.messaging_webhook_url = self.crm_config.get("messaging_webhook_url", "")

    @property
    def _headers(self):
        return {
            "Authorization": f"Bearer {self.hs_token}",
            "Content-Type": "application/json"
        }

    def _refresh_token(self) -> bool:
        if not all([self.refresh_token_hs, self.client_id, self.client_secret]):
            return False
        try:
            resp = requests.post(f"{HUBSPOT_BASE}/oauth/v1/token", data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token_hs,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }, timeout=HUBSPOT_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                self.hs_token = data["access_token"]
                logger.info("HubSpot token refreshed successfully")
                return True
        except Exception as e:
            logger.error(f"HubSpot token refresh error: {e}")
        return False

    def _api_request(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """Make an API request with automatic token refresh on 401."""
        try:
            resp = getattr(requests, method)(url, headers=self._headers, timeout=HUBSPOT_TIMEOUT, **kwargs)
            if resp.status_code == 401 and self._refresh_token():
                resp = getattr(requests, method)(url, headers=self._headers, timeout=HUBSPOT_TIMEOUT, **kwargs)
            return resp
        except Exception as e:
            logger.error(f"HubSpot API error: {e}")
            return None

    def _parse_booking_time(self, selected_time: str) -> tuple:
        import re
        from datetime import time as dt_time
        local_tz = ZoneInfo(self.timezone)
        now_local = datetime.now(local_tz)
        target_date = now_local.date()

        time_str = selected_time.lower().strip()
        if "tomorrow" in time_str:
            target_date = (now_local + timedelta(days=1)).date()

        hour, minute = 14, 0
        match = re.search(r'(\d{1,2}):?(\d{2})?\s*(pm|p\.m\.|am|a\.m\.)', time_str)
        if match:
            h = int(match.group(1))
            m = int(match.group(2) or 0)
            period = match.group(3).lower().replace(".", "")
            if "pm" in period and h != 12:
                h += 12
            elif "am" in period and h == 12:
                h = 0
            hour, minute = h, m
        else:
            bare = re.search(r'(\d{1,2})', time_str)
            if bare:
                h = int(bare.group(1))
                if 1 <= h <= 7:
                    h += 12
                hour = h

        start_dt = datetime.combine(target_date, dt_time(max(9, min(19, hour)), minute), tzinfo=local_tz)
        end_dt = start_dt + timedelta(minutes=30)
        return start_dt, end_dt

    def send_message(self, contact_id: str, message: str, **kwargs) -> bool:
        """Send SMS via Twilio, log as Communication in HubSpot timeline."""
        phone = kwargs.get("phone", "")
        sent = False

        # Try Twilio first
        from crm_adapters.twilio_messaging import has_twilio_config, send_sms_via_twilio, get_twilio_config
        if has_twilio_config(self.crm_config):
            if not phone:
                contact = self.get_contact(contact_id)
                phone = contact.get("phone", "")
            if phone:
                cfg = get_twilio_config(self.crm_config)
                sent = send_sms_via_twilio(phone, message, **cfg)

        # Fallback: webhook relay
        if not sent and self.messaging_webhook_url:
            try:
                resp = requests.post(self.messaging_webhook_url, json={
                    "contact_id": contact_id,
                    "message": message,
                    "phone": phone,
                    "direction": "outbound",
                    "_source": "InsuranceGrokBot",
                    "_event_type": "send_message",
                }, timeout=HUBSPOT_TIMEOUT)
                sent = resp.status_code in [200, 201]
            except Exception as e:
                logger.error(f"HubSpot messaging webhook error: {e}")

        # Log SMS as Communication in HubSpot timeline
        if sent and contact_id:
            self._log_communication(contact_id, message)

        if not sent:
            logger.warning(f"HubSpot: No messaging method available for {contact_id}")
        return sent

    def _log_communication(self, contact_id: str, message: str):
        """Log an outbound SMS as a Communication record linked to the contact."""
        now_ms = str(int(datetime.now().timestamp() * 1000))
        comm_payload = {
            "properties": {
                "hs_communication_channel_type": "SMS",
                "hs_communication_body": message[:65536],
                "hs_communication_logged_from": "CRM",
                "hs_timestamp": now_ms,
            },
            "associations": [{
                "to": {"id": contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 81  # Communication to Contact
                }]
            }]
        }
        url = f"{HUBSPOT_BASE}/crm/v3/objects/communications"
        try:
            resp = self._api_request("post", url, json=comm_payload)
            if resp and resp.status_code in [200, 201]:
                logger.info(f"HubSpot: SMS logged as Communication for contact {contact_id}")
        except Exception as e:
            logger.error(f"HubSpot: Failed to log Communication: {e}")

    def get_free_slots(self) -> str:
        logger.info("HubSpot adapter: No native free-slots API. Using generic response.")
        return "let me check my calendar and get back to you with some times"

    def book_appointment(self, contact_id: str, first_name: str, selected_time: str) -> bool:
        start_dt, end_dt = self._parse_booking_time(selected_time)

        start_ms = str(int(start_dt.timestamp() * 1000))
        end_ms = str(int(end_dt.timestamp() * 1000))

        meeting_payload = {
            "properties": {
                "hs_timestamp": start_ms,
                "hs_meeting_title": f"Life Insurance Review {first_name or 'Lead'}",
                "hs_meeting_start_time": start_ms,
                "hs_meeting_end_time": end_ms,
                "hs_meeting_body": f"Appointment booked via InsuranceGrokBot for {first_name or 'Lead'}",
            }
        }

        if self.owner_id:
            meeting_payload["properties"]["hubspot_owner_id"] = self.owner_id

        if contact_id:
            meeting_payload["associations"] = [{
                "to": {"id": contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 200  # Meeting to Contact
                }]
            }]

        url = f"{HUBSPOT_BASE}/crm/v3/objects/meetings"
        resp = self._api_request("post", url, json=meeting_payload)
        if resp and resp.status_code in [200, 201]:
            result = resp.json()
            logger.info(f"HubSpot meeting created: {result.get('id')} | contact={contact_id}")
            return True

        if resp:
            logger.error(f"HubSpot book_appointment failed: {resp.status_code} | {resp.text[:300]}")
        return False

    def search_contact(self, email: str = None, phone: str = None) -> Optional[dict]:
        """Search HubSpot contacts by email or phone."""
        filters = []
        if email:
            filters.append({"propertyName": "email", "operator": "EQ", "value": email})
        if phone:
            filters.append({"propertyName": "phone", "operator": "EQ", "value": phone})

        if not filters:
            return None

        # HubSpot search: OR logic requires separate filterGroups
        filter_groups = [{"filters": [f]} for f in filters]

        search_payload = {
            "filterGroups": filter_groups,
            "properties": ["firstname", "lastname", "email", "phone"],
            "limit": 1
        }

        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
        resp = self._api_request("post", url, json=search_payload)
        if resp and resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                r = results[0]
                props = r.get("properties", {})
                return {
                    "id": r.get("id", ""),
                    "firstName": props.get("firstname", ""),
                    "lastName": props.get("lastname", ""),
                    "email": props.get("email", ""),
                    "phone": props.get("phone", ""),
                }
        return None

    def update_contact(self, contact_id: str, contact_data: dict) -> bool:
        """Update a HubSpot contact's properties."""
        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
        properties = {}
        if contact_data.get("first_name"):
            properties["firstname"] = contact_data["first_name"]
        if contact_data.get("last_name"):
            properties["lastname"] = contact_data["last_name"]
        if contact_data.get("email"):
            properties["email"] = contact_data["email"]
        if contact_data.get("phone"):
            properties["phone"] = contact_data["phone"]

        if not properties:
            return True

        resp = self._api_request("patch", url, json={"properties": properties})
        if resp and resp.status_code == 200:
            logger.info(f"HubSpot Contact updated: {contact_id}")
            return True
        if resp:
            logger.error(f"HubSpot update_contact failed: {resp.status_code}")
        return False

    def get_contact(self, contact_id: str) -> dict:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
        params = {"properties": "firstname,lastname,email,phone"}
        resp = self._api_request("get", url, params=params)
        if resp and resp.status_code == 200:
            props = resp.json().get("properties", {})
            return {
                "firstName": props.get("firstname", ""),
                "lastName": props.get("lastname", ""),
                "email": props.get("email", ""),
                "phone": props.get("phone", ""),
            }
        return {}

    def create_contact(self, contact_data: dict) -> Optional[str]:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
        payload = {
            "properties": {
                "firstname": contact_data.get("first_name", ""),
                "lastname": contact_data.get("last_name", ""),
                "email": contact_data.get("email", ""),
                "phone": contact_data.get("phone", ""),
            }
        }
        resp = self._api_request("post", url, json=payload)
        if resp and resp.status_code in [200, 201]:
            result = resp.json()
            logger.info(f"HubSpot Contact created: {result.get('id')}")
            return result.get("id")
        # HubSpot returns 409 if contact with same email exists
        if resp and resp.status_code == 409:
            logger.info("HubSpot: Contact already exists, searching...")
            email = contact_data.get("email", "")
            if email:
                existing = self.search_contact(email=email)
                if existing:
                    return existing.get("id")
        if resp:
            logger.error(f"HubSpot create_contact failed: {resp.status_code} | {resp.text[:300]}")
        return None

    def validate_credentials(self) -> dict:
        if not self.hs_token:
            return {"valid": False, "message": "No HubSpot access token configured", "details": {}}
        resp = self._api_request("get", f"{HUBSPOT_BASE}/crm/v3/objects/contacts", params={"limit": 1})
        if resp and resp.status_code == 200:
            return {"valid": True, "message": "HubSpot credentials valid", "details": {}}
        return {"valid": False, "message": "HubSpot validation failed", "details": {}}
