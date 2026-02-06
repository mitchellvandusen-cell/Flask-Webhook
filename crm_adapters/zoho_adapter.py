# crm_adapters/zoho_adapter.py - Zoho CRM API Adapter
# Uses Zoho CRM API v8 for contacts and events.
#
# Required crm_config keys:
#   access_token: OAuth access token
#   refresh_token: OAuth refresh token (required - tokens expire in ~1 hour)
#   client_id: Zoho app client ID
#   client_secret: Zoho app client secret
#   data_center: "com" | "eu" | "in" | "com.au" | "jp" | "zohocloud.ca" (default: "com")
#   owner_id: (optional) Zoho user ID for assigning events
#
# Authentication: Zoho-oauthtoken header (NOT Bearer)
# Contacts: POST https://www.zohoapis.{dc}/crm/v8/Contacts
# Events: POST https://www.zohoapis.{dc}/crm/v8/Events

import logging
import requests
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from crm_adapters.base import CRMAdapter

logger = logging.getLogger(__name__)

ZOHO_TIMEOUT = 20
ZOHO_API_VERSION = "v8"


class ZohoAdapter(CRMAdapter):
    """
    Zoho CRM adapter using their REST API v8.
    Manages contacts and events (appointments).
    Note: Zoho uses 'Zoho-oauthtoken' header, not 'Bearer'.
    """

    CRM_NAME = "Zoho CRM"
    SUPPORTS_MESSAGING = False
    SUPPORTS_CALENDAR = True
    SUPPORTS_CONTACTS = True

    def __init__(self, subscriber_data: dict):
        super().__init__(subscriber_data)
        self.zoho_token = self.crm_config.get("access_token", "") or self.access_token
        self.refresh_token_zoho = self.crm_config.get("refresh_token", "")
        self.client_id = self.crm_config.get("client_id", "")
        self.client_secret = self.crm_config.get("client_secret", "")
        self.data_center = self.crm_config.get("data_center", "com")
        self.owner_id = self.crm_config.get("owner_id", "") or self.crm_user_id

    @property
    def _accounts_url(self):
        dc = self.data_center
        if dc == "zohocloud.ca":
            return f"https://accounts.{dc}"
        return f"https://accounts.zoho.{dc}"

    @property
    def _api_url(self):
        dc = self.data_center
        if dc == "zohocloud.ca":
            return f"https://www.zohoapis.ca/crm/{ZOHO_API_VERSION}"
        return f"https://www.zohoapis.{dc}/crm/{ZOHO_API_VERSION}"

    @property
    def _headers(self):
        return {
            "Authorization": f"Zoho-oauthtoken {self.zoho_token}",
            "Content-Type": "application/json"
        }

    def _refresh_token(self) -> bool:
        if not all([self.refresh_token_zoho, self.client_id, self.client_secret]):
            return False
        try:
            resp = requests.post(f"{self._accounts_url}/oauth/v2/token", params={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token_zoho,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }, timeout=ZOHO_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                self.zoho_token = data.get("access_token", "")
                if self.zoho_token:
                    logger.info("Zoho token refreshed successfully")
                    return True
        except Exception as e:
            logger.error(f"Zoho token refresh error: {e}")
        return False

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
        logger.info("Zoho adapter: No native SMS API. Use Zapier integration for messaging.")
        return False

    def get_free_slots(self) -> str:
        logger.info("Zoho adapter: No native free-slots API. Using generic response.")
        return "let me check my calendar and get back to you with some times"

    def book_appointment(self, contact_id: str, first_name: str, selected_time: str) -> bool:
        """Create a Zoho Event linked to a contact."""
        start_dt, end_dt = self._parse_booking_time(selected_time)

        # Zoho uses ISO 8601 with timezone offset
        event_payload = {
            "data": [{
                "Event_Title": f"Life Insurance Review {first_name or 'Lead'}",
                "Start_DateTime": start_dt.isoformat(),
                "End_DateTime": end_dt.isoformat(),
                "Description": f"Appointment booked via InsuranceGrokBot for {first_name or 'Lead'}",
                "se_module": "Contacts",
                "Participants": [{
                    "type": "contact",
                    "participant": contact_id
                }]
            }]
        }

        if self.owner_id:
            event_payload["data"][0]["Owner"] = {"id": self.owner_id}

        url = f"{self._api_url}/Events"
        try:
            resp = requests.post(url, json=event_payload, headers=self._headers, timeout=ZOHO_TIMEOUT)
            if resp.status_code in [200, 201]:
                result = resp.json()
                data_list = result.get("data", [])
                if data_list and data_list[0].get("status") == "success":
                    event_id = data_list[0].get("details", {}).get("id")
                    logger.info(f"Zoho Event created: {event_id} | contact={contact_id}")
                    return True
                logger.warning(f"Zoho Event creation response: {result}")
            elif resp.status_code == 401:
                if self._refresh_token():
                    resp = requests.post(url, json=event_payload, headers=self._headers, timeout=ZOHO_TIMEOUT)
                    if resp.status_code in [200, 201]:
                        result = resp.json()
                        data_list = result.get("data", [])
                        if data_list and data_list[0].get("status") == "success":
                            return True
            logger.error(f"Zoho book_appointment failed: {resp.status_code} | {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Zoho book_appointment error: {e}")
        return False

    def get_contact(self, contact_id: str) -> dict:
        url = f"{self._api_url}/Contacts/{contact_id}"
        try:
            resp = requests.get(url, headers=self._headers, timeout=ZOHO_TIMEOUT)
            if resp.status_code == 200:
                data_list = resp.json().get("data", [])
                if data_list:
                    record = data_list[0]
                    return {
                        "firstName": record.get("First_Name", ""),
                        "lastName": record.get("Last_Name", ""),
                        "email": record.get("Email", ""),
                        "phone": record.get("Phone", ""),
                    }
            elif resp.status_code == 401 and self._refresh_token():
                resp = requests.get(url, headers=self._headers, timeout=ZOHO_TIMEOUT)
                if resp.status_code == 200:
                    data_list = resp.json().get("data", [])
                    if data_list:
                        record = data_list[0]
                        return {
                            "firstName": record.get("First_Name", ""),
                            "lastName": record.get("Last_Name", ""),
                            "email": record.get("Email", ""),
                            "phone": record.get("Phone", ""),
                        }
        except Exception as e:
            logger.error(f"Zoho get_contact error: {e}")
        return {}

    def create_contact(self, contact_data: dict) -> Optional[str]:
        url = f"{self._api_url}/Contacts"
        payload = {
            "data": [{
                "First_Name": contact_data.get("first_name", ""),
                "Last_Name": contact_data.get("last_name", "Unknown"),
                "Email": contact_data.get("email", ""),
                "Phone": contact_data.get("phone", ""),
            }]
        }
        try:
            resp = requests.post(url, json=payload, headers=self._headers, timeout=ZOHO_TIMEOUT)
            if resp.status_code in [200, 201]:
                result = resp.json()
                data_list = result.get("data", [])
                if data_list and data_list[0].get("status") == "success":
                    contact_id = data_list[0].get("details", {}).get("id")
                    logger.info(f"Zoho Contact created: {contact_id}")
                    return contact_id
            elif resp.status_code == 401 and self._refresh_token():
                resp = requests.post(url, json=payload, headers=self._headers, timeout=ZOHO_TIMEOUT)
                if resp.status_code in [200, 201]:
                    result = resp.json()
                    data_list = result.get("data", [])
                    if data_list and data_list[0].get("status") == "success":
                        return data_list[0].get("details", {}).get("id")
            logger.error(f"Zoho create_contact failed: {resp.status_code} | {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Zoho create_contact error: {e}")
        return None

    def validate_credentials(self) -> dict:
        if not self.zoho_token:
            return {"valid": False, "message": "No Zoho access token configured", "details": {}}
        url = f"{self._api_url}/users?type=CurrentUser"
        try:
            resp = requests.get(url, headers=self._headers, timeout=ZOHO_TIMEOUT)
            if resp.status_code == 200:
                users = resp.json().get("users", [])
                name = users[0].get("full_name", "Unknown") if users else "Unknown"
                return {"valid": True, "message": f"Zoho connected as {name}", "details": {"data_center": self.data_center}}
            elif resp.status_code == 401:
                if self._refresh_token():
                    return {"valid": True, "message": "Zoho token refreshed and valid", "details": {}}
                return {"valid": False, "message": "Zoho token expired and refresh failed", "details": {}}
            return {"valid": False, "message": f"Zoho returned {resp.status_code}", "details": {}}
        except Exception as e:
            return {"valid": False, "message": f"Zoho connection error: {e}", "details": {}}
