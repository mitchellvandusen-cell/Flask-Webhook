# crm_adapters/hubspot_adapter.py - HubSpot CRM API Adapter
# Uses HubSpot CRM API v3 for contacts and meetings.
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
# Authentication: Bearer token (Private App or OAuth)
# Contacts: POST https://api.hubapi.com/crm/v3/objects/contacts
# Meetings: POST https://api.hubapi.com/crm/v3/objects/meetings

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
    Manages contacts and meeting records.
    """

    CRM_NAME = "HubSpot"
    SUPPORTS_MESSAGING = False  # HubSpot doesn't have native SMS API
    SUPPORTS_CALENDAR = True
    SUPPORTS_CONTACTS = True

    def __init__(self, subscriber_data: dict):
        super().__init__(subscriber_data)
        self.hs_token = self.crm_config.get("access_token", "") or self.access_token
        self.refresh_token_hs = self.crm_config.get("refresh_token", "")
        self.client_id = self.crm_config.get("client_id", "")
        self.client_secret = self.crm_config.get("client_secret", "")
        self.owner_id = self.crm_config.get("owner_id", "") or self.crm_user_id

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

    def _parse_booking_time(self, selected_time: str) -> tuple:
        """Parse selected_time string into (start_dt, end_dt) datetimes."""
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
        logger.info("HubSpot adapter: Native SMS not supported. Use Zapier or SMS integration.")
        return False

    def get_free_slots(self) -> str:
        logger.info("HubSpot adapter: No native free-slots API. Using generic response.")
        return "let me check my calendar and get back to you with some times"

    def book_appointment(self, contact_id: str, first_name: str, selected_time: str) -> bool:
        """Create a HubSpot meeting engagement linked to a contact."""
        start_dt, end_dt = self._parse_booking_time(selected_time)

        # HubSpot uses Unix timestamps in milliseconds for meeting times
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        meeting_payload = {
            "properties": {
                "hs_timestamp": str(start_ms),
                "hs_meeting_title": f"Life Insurance Review {first_name or 'Lead'}",
                "hs_meeting_start_time": str(start_ms),
                "hs_meeting_end_time": str(end_ms),
                "hs_meeting_body": f"Appointment booked via InsuranceGrokBot for {first_name or 'Lead'}",
            }
        }

        if self.owner_id:
            meeting_payload["properties"]["hubspot_owner_id"] = self.owner_id

        # Associate with contact if we have a contact_id
        if contact_id:
            meeting_payload["associations"] = [{
                "to": {"id": contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 200  # Meeting to Contact
                }]
            }]

        url = f"{HUBSPOT_BASE}/crm/v3/objects/meetings"
        try:
            resp = requests.post(url, json=meeting_payload, headers=self._headers, timeout=HUBSPOT_TIMEOUT)
            if resp.status_code in [200, 201]:
                result = resp.json()
                logger.info(f"HubSpot meeting created: {result.get('id')} | contact={contact_id}")
                return True
            elif resp.status_code == 401 and self._refresh_token():
                resp = requests.post(url, json=meeting_payload, headers=self._headers, timeout=HUBSPOT_TIMEOUT)
                if resp.status_code in [200, 201]:
                    return True
            logger.error(f"HubSpot book_appointment failed: {resp.status_code} | {resp.text[:300]}")
        except Exception as e:
            logger.error(f"HubSpot book_appointment error: {e}")
        return False

    def get_contact(self, contact_id: str) -> dict:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
        params = {"properties": "firstname,lastname,email,phone"}
        try:
            resp = requests.get(url, headers=self._headers, params=params, timeout=HUBSPOT_TIMEOUT)
            if resp.status_code == 200:
                props = resp.json().get("properties", {})
                return {
                    "firstName": props.get("firstname", ""),
                    "lastName": props.get("lastname", ""),
                    "email": props.get("email", ""),
                    "phone": props.get("phone", ""),
                }
            elif resp.status_code == 401 and self._refresh_token():
                resp = requests.get(url, headers=self._headers, params=params, timeout=HUBSPOT_TIMEOUT)
                if resp.status_code == 200:
                    props = resp.json().get("properties", {})
                    return {
                        "firstName": props.get("firstname", ""),
                        "lastName": props.get("lastname", ""),
                        "email": props.get("email", ""),
                        "phone": props.get("phone", ""),
                    }
        except Exception as e:
            logger.error(f"HubSpot get_contact error: {e}")
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
        try:
            resp = requests.post(url, json=payload, headers=self._headers, timeout=HUBSPOT_TIMEOUT)
            if resp.status_code in [200, 201]:
                result = resp.json()
                logger.info(f"HubSpot Contact created: {result.get('id')}")
                return result.get("id")
            elif resp.status_code == 401 and self._refresh_token():
                resp = requests.post(url, json=payload, headers=self._headers, timeout=HUBSPOT_TIMEOUT)
                if resp.status_code in [200, 201]:
                    return resp.json().get("id")
            logger.error(f"HubSpot create_contact failed: {resp.status_code} | {resp.text[:300]}")
        except Exception as e:
            logger.error(f"HubSpot create_contact error: {e}")
        return None

    def validate_credentials(self) -> dict:
        if not self.hs_token:
            return {"valid": False, "message": "No HubSpot access token configured", "details": {}}
        try:
            resp = requests.get(
                f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
                headers=self._headers, params={"limit": 1}, timeout=HUBSPOT_TIMEOUT
            )
            if resp.status_code == 200:
                return {"valid": True, "message": "HubSpot credentials valid", "details": {}}
            elif resp.status_code == 401:
                if self._refresh_token():
                    return {"valid": True, "message": "HubSpot token refreshed and valid", "details": {}}
                return {"valid": False, "message": "HubSpot token expired", "details": {}}
            return {"valid": False, "message": f"HubSpot returned {resp.status_code}", "details": {}}
        except Exception as e:
            return {"valid": False, "message": f"HubSpot connection error: {e}", "details": {}}
