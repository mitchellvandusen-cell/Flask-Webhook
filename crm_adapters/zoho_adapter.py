# crm_adapters/zoho_adapter.py - Zoho CRM API Adapter
# Uses Zoho CRM API v8 for contacts, events, and email.
#
# Required crm_config keys:
#   access_token: OAuth access token
#   refresh_token: OAuth refresh token (required - tokens expire in ~1 hour)
#   client_id: Zoho app client ID
#   client_secret: Zoho app client secret
#   data_center: "com" | "eu" | "in" | "com.au" | "jp" | "zohocloud.ca" (default: "com")
#   owner_id: (optional) Zoho user ID for assigning events
#
# SMS: Uses IGB Twilio sub-account (auto-provisioned) — no user Twilio config needed
#
# Authentication: Zoho-oauthtoken header (NOT Bearer)
# Contacts: POST https://www.zohoapis.{dc}/crm/v8/Contacts
# Events: POST https://www.zohoapis.{dc}/crm/v8/Events
# Search: GET https://www.zohoapis.{dc}/crm/v8/Contacts/search
# Email: POST https://www.zohoapis.{dc}/crm/v8/Contacts/{id}/actions/send_mail

import logging
import requests
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from crm_adapters.base import CRMAdapter
from db import update_crm_config_token

logger = logging.getLogger(__name__)

ZOHO_TIMEOUT = 20
ZOHO_API_VERSION = "v8"


class ZohoAdapter(CRMAdapter):
    """
    Zoho CRM adapter using their REST API v8.
    Manages contacts, events (appointments), email sending, and search.
    Note: Zoho uses 'Zoho-oauthtoken' header, not 'Bearer'.
    """

    CRM_NAME = "Zoho CRM"
    SUPPORTS_MESSAGING = True  # Via Twilio + Zoho email
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
        self.messaging_webhook_url = self.crm_config.get("messaging_webhook_url", "")

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
                    if self.location_id:
                        update_crm_config_token(self.location_id, self.zoho_token)
                    return True
        except Exception as e:
            logger.error(f"Zoho token refresh error: {e}")
        return False

    def _api_request(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """Make an API request with automatic token refresh on 401."""
        try:
            resp = getattr(requests, method)(url, headers=self._headers, timeout=ZOHO_TIMEOUT, **kwargs)
            if resp.status_code == 401 and self._refresh_token():
                resp = getattr(requests, method)(url, headers=self._headers, timeout=ZOHO_TIMEOUT, **kwargs)
            return resp
        except Exception as e:
            logger.error(f"Zoho API error: {e}")
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
        """Send SMS via Twilio or webhook, then log via Zoho Note."""
        phone = kwargs.get("phone", "")
        sent = False

        # Try Twilio (IGB sub-account first, then crm_config, then env vars)
        from crm_adapters.twilio_messaging import has_twilio_config, send_sms_via_twilio, get_twilio_config
        if has_twilio_config(self.crm_config, self.voice_config):
            if not phone:
                contact = self.get_contact(contact_id)
                phone = contact.get("phone", "")
            if phone:
                cfg = get_twilio_config(self.crm_config, self.voice_config)
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
                }, timeout=ZOHO_TIMEOUT)
                sent = resp.status_code in [200, 201]
            except Exception as e:
                logger.error(f"Zoho messaging webhook error: {e}")

        # Log SMS as a Note in Zoho
        if sent and contact_id:
            self._log_note(contact_id, message)

        if not sent:
            logger.warning(f"Zoho: No messaging method available for {contact_id}")
        return sent

    def _log_note(self, contact_id: str, message: str):
        """Log an outbound SMS as a Zoho Note linked to a contact."""
        url = f"{self._api_url}/Notes"
        payload = {
            "data": [{
                "Note_Title": "SMS via InsuranceGrokBot",
                "Note_Content": message[:32000],
                "se_module": "Contacts",
                "Parent_Id": contact_id,
            }]
        }
        try:
            resp = self._api_request("post", url, json=payload)
            if resp and resp.status_code in [200, 201]:
                logger.info(f"Zoho: SMS logged as Note for contact {contact_id}")
        except Exception as e:
            logger.error(f"Zoho: Failed to log Note: {e}")

    def send_email(self, contact_id: str, subject: str, body: str, from_email: str = "") -> bool:
        """Send email via Zoho CRM's native Send Mail API."""
        url = f"{self._api_url}/Contacts/{contact_id}/actions/send_mail"
        contact = self.get_contact(contact_id)
        to_email = contact.get("email", "")
        if not to_email:
            logger.warning(f"Zoho: No email address for contact {contact_id}")
            return False

        payload = {
            "data": [{
                "from": {"user_name": from_email or "noreply", "email": from_email},
                "to": [{"user_name": contact.get("firstName", ""), "email": to_email}],
                "subject": subject,
                "content": body,
                "mail_format": "text",
            }]
        }
        resp = self._api_request("post", url, json=payload)
        if resp and resp.status_code in [200, 201]:
            logger.info(f"Zoho: Email sent to {to_email}")
            return True
        if resp:
            logger.error(f"Zoho send_email failed: {resp.status_code}")
        return False

    def get_free_slots(self) -> str:
        logger.info("Zoho adapter: No native free-slots API. Using generic response.")
        return "let me check my calendar and get back to you with some times"

    def book_appointment(self, contact_id: str, first_name: str, selected_time: str) -> bool:
        start_dt, end_dt = self._parse_booking_time(selected_time)

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
        resp = self._api_request("post", url, json=event_payload)
        if resp and resp.status_code in [200, 201]:
            result = resp.json()
            data_list = result.get("data", [])
            if data_list and data_list[0].get("status") == "success":
                event_id = data_list[0].get("details", {}).get("id")
                logger.info(f"Zoho Event created: {event_id} | contact={contact_id}")
                return True
            logger.warning(f"Zoho Event creation response: {result}")
        if resp:
            logger.error(f"Zoho book_appointment failed: {resp.status_code} | {resp.text[:300]}")
        return False

    def search_contact(self, email: str = None, phone: str = None) -> Optional[dict]:
        """Search Zoho CRM contacts by email or phone."""
        params = {}
        if email:
            params["email"] = email
        elif phone:
            params["phone"] = phone
        else:
            return None

        url = f"{self._api_url}/Contacts/search"
        resp = self._api_request("get", url, params=params)
        if resp and resp.status_code == 200:
            data_list = resp.json().get("data", [])
            if data_list:
                record = data_list[0]
                return {
                    "id": record.get("id", ""),
                    "firstName": record.get("First_Name", ""),
                    "lastName": record.get("Last_Name", ""),
                    "email": record.get("Email", ""),
                    "phone": record.get("Phone", ""),
                }
        return None

    def update_contact(self, contact_id: str, contact_data: dict) -> bool:
        """Update a Zoho CRM Contact."""
        url = f"{self._api_url}/Contacts"
        record = {"id": contact_id}
        if contact_data.get("first_name"):
            record["First_Name"] = contact_data["first_name"]
        if contact_data.get("last_name"):
            record["Last_Name"] = contact_data["last_name"]
        if contact_data.get("email"):
            record["Email"] = contact_data["email"]
        if contact_data.get("phone"):
            record["Phone"] = contact_data["phone"]

        payload = {"data": [record]}
        resp = self._api_request("put", url, json=payload)
        if resp and resp.status_code in [200, 201]:
            result = resp.json()
            data_list = result.get("data", [])
            if data_list and data_list[0].get("status") == "success":
                logger.info(f"Zoho Contact updated: {contact_id}")
                return True
        if resp:
            logger.error(f"Zoho update_contact failed: {resp.status_code}")
        return False

    def get_contact(self, contact_id: str) -> dict:
        url = f"{self._api_url}/Contacts/{contact_id}"
        resp = self._api_request("get", url)
        if resp and resp.status_code == 200:
            data_list = resp.json().get("data", [])
            if data_list:
                record = data_list[0]
                return {
                    "firstName": record.get("First_Name", ""),
                    "lastName": record.get("Last_Name", ""),
                    "email": record.get("Email", ""),
                    "phone": record.get("Phone", ""),
                }
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
        resp = self._api_request("post", url, json=payload)
        if resp and resp.status_code in [200, 201]:
            result = resp.json()
            data_list = result.get("data", [])
            if data_list and data_list[0].get("status") == "success":
                contact_id = data_list[0].get("details", {}).get("id")
                logger.info(f"Zoho Contact created: {contact_id}")
                return contact_id
        if resp:
            logger.error(f"Zoho create_contact failed: {resp.status_code} | {resp.text[:300]}")
        return None

    def validate_credentials(self) -> dict:
        if not self.zoho_token:
            return {"valid": False, "message": "No Zoho access token configured", "details": {}}
        url = f"{self._api_url}/users?type=CurrentUser"
        resp = self._api_request("get", url)
        if resp and resp.status_code == 200:
            users = resp.json().get("users", [])
            name = users[0].get("full_name", "Unknown") if users else "Unknown"
            return {"valid": True, "message": f"Zoho connected as {name}", "details": {"data_center": self.data_center}}
        return {"valid": False, "message": "Zoho validation failed", "details": {}}
