# crm_adapters/pipedrive_adapter.py - Pipedrive CRM API Adapter
# Uses Pipedrive REST API v1 for persons (contacts) and activities (appointments).
#
# Required crm_config keys:
#   api_token: Pipedrive API token (from Settings > Personal Preferences > API)
#   company_domain: Pipedrive company domain (e.g., "mycompany" for mycompany.pipedrive.com)
#   OR
#   access_token: OAuth Bearer token
#   refresh_token: (optional) OAuth refresh token
#   client_id / client_secret: (optional) For OAuth refresh
#   owner_id: (optional) Pipedrive user ID for assigning activities
#
# Contacts (Persons): POST https://{domain}.pipedrive.com/api/v1/persons
# Activities: POST https://{domain}.pipedrive.com/api/v1/activities

import logging
import requests
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from crm_adapters.base import CRMAdapter

logger = logging.getLogger(__name__)

PIPEDRIVE_TIMEOUT = 20


class PipedriveAdapter(CRMAdapter):
    """
    Pipedrive CRM adapter using their REST API v1.
    Manages persons (contacts) and activities (appointments/meetings).
    """

    CRM_NAME = "Pipedrive"
    SUPPORTS_MESSAGING = False
    SUPPORTS_CALENDAR = True
    SUPPORTS_CONTACTS = True

    def __init__(self, subscriber_data: dict):
        super().__init__(subscriber_data)
        self.api_token = self.crm_config.get("api_token", "")
        self.company_domain = self.crm_config.get("company_domain", "")
        self.pd_oauth_token = self.crm_config.get("access_token", "") or self.access_token
        self.refresh_token_pd = self.crm_config.get("refresh_token", "")
        self.client_id = self.crm_config.get("client_id", "")
        self.client_secret = self.crm_config.get("client_secret", "")
        self.owner_id = self.crm_config.get("owner_id", "") or self.crm_user_id

    @property
    def _base_url(self):
        if self.company_domain:
            return f"https://{self.company_domain}.pipedrive.com/api/v1"
        return "https://api.pipedrive.com/api/v1"

    @property
    def _auth_params(self):
        """Return auth as query params (API token) or empty dict (OAuth uses headers)."""
        if self.api_token:
            return {"api_token": self.api_token}
        return {}

    @property
    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if not self.api_token and self.pd_oauth_token:
            headers["Authorization"] = f"Bearer {self.pd_oauth_token}"
        return headers

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
        logger.info("Pipedrive adapter: No native messaging API. Use Zapier integration for SMS.")
        return False

    def get_free_slots(self) -> str:
        logger.info("Pipedrive adapter: No native free-slots API. Using generic response.")
        return "let me check my calendar and get back to you with some times"

    def book_appointment(self, contact_id: str, first_name: str, selected_time: str) -> bool:
        """Create a Pipedrive Activity (meeting type) linked to a person."""
        start_dt, end_dt = self._parse_booking_time(selected_time)
        duration = end_dt - start_dt
        duration_str = f"{duration.seconds // 3600:02d}:{(duration.seconds % 3600) // 60:02d}"

        activity_payload = {
            "subject": f"Life Insurance Review {first_name or 'Lead'}",
            "type": "meeting",
            "due_date": start_dt.strftime("%Y-%m-%d"),
            "due_time": start_dt.strftime("%H:%M"),
            "duration": duration_str,
            "person_id": int(contact_id) if contact_id.isdigit() else None,
            "note": f"Appointment booked via InsuranceGrokBot for {first_name or 'Lead'}",
            "done": 0,
        }

        # Remove None values
        activity_payload = {k: v for k, v in activity_payload.items() if v is not None}

        if self.owner_id:
            activity_payload["user_id"] = int(self.owner_id) if str(self.owner_id).isdigit() else None

        url = f"{self._base_url}/activities"
        try:
            resp = requests.post(url, json=activity_payload, headers=self._headers,
                               params=self._auth_params, timeout=PIPEDRIVE_TIMEOUT)
            if resp.status_code in [200, 201]:
                result = resp.json()
                if result.get("success"):
                    activity_id = result.get("data", {}).get("id")
                    logger.info(f"Pipedrive Activity created: {activity_id} | contact={contact_id}")
                    return True
            logger.error(f"Pipedrive book_appointment failed: {resp.status_code} | {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Pipedrive book_appointment error: {e}")
        return False

    def get_contact(self, contact_id: str) -> dict:
        url = f"{self._base_url}/persons/{contact_id}"
        try:
            resp = requests.get(url, headers=self._headers, params=self._auth_params, timeout=PIPEDRIVE_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                # Pipedrive stores name as single field, email/phone as arrays
                name_parts = (data.get("name", "") or "").split(" ", 1)
                emails = data.get("email", [])
                phones = data.get("phone", [])
                return {
                    "firstName": name_parts[0] if name_parts else "",
                    "lastName": name_parts[1] if len(name_parts) > 1 else "",
                    "email": emails[0].get("value", "") if emails else "",
                    "phone": phones[0].get("value", "") if phones else "",
                }
        except Exception as e:
            logger.error(f"Pipedrive get_contact error: {e}")
        return {}

    def create_contact(self, contact_data: dict) -> Optional[str]:
        url = f"{self._base_url}/persons"
        first = contact_data.get("first_name", "")
        last = contact_data.get("last_name", "")
        payload = {
            "name": f"{first} {last}".strip() or "Unknown",
            "email": [contact_data.get("email", "")] if contact_data.get("email") else [],
            "phone": [contact_data.get("phone", "")] if contact_data.get("phone") else [],
        }
        try:
            resp = requests.post(url, json=payload, headers=self._headers,
                               params=self._auth_params, timeout=PIPEDRIVE_TIMEOUT)
            if resp.status_code in [200, 201]:
                result = resp.json()
                if result.get("success"):
                    person_id = result.get("data", {}).get("id")
                    logger.info(f"Pipedrive Person created: {person_id}")
                    return str(person_id) if person_id else None
            logger.error(f"Pipedrive create_contact failed: {resp.status_code} | {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Pipedrive create_contact error: {e}")
        return None

    def validate_credentials(self) -> dict:
        if not self.api_token and not self.pd_oauth_token:
            return {"valid": False, "message": "No Pipedrive API token or OAuth token configured", "details": {}}
        url = f"{self._base_url}/users/me"
        try:
            resp = requests.get(url, headers=self._headers, params=self._auth_params, timeout=PIPEDRIVE_TIMEOUT)
            if resp.status_code == 200 and resp.json().get("success"):
                user_data = resp.json().get("data", {})
                return {
                    "valid": True,
                    "message": f"Pipedrive connected as {user_data.get('name', 'Unknown')}",
                    "details": {"user_id": user_data.get("id")}
                }
            return {"valid": False, "message": f"Pipedrive returned {resp.status_code}", "details": {}}
        except Exception as e:
            return {"valid": False, "message": f"Pipedrive connection error: {e}", "details": {}}
