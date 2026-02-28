# crm_adapters/pipedrive_adapter.py - Pipedrive CRM API Adapter
# Uses Pipedrive REST API v1 for persons, activities, deals, and notes.
#
# Required crm_config keys:
#   api_token: Pipedrive API token (from Settings > Personal Preferences > API)
#   company_domain: Pipedrive company domain (e.g., "mycompany" for mycompany.pipedrive.com)
#   OR
#   access_token: OAuth Bearer token
#   refresh_token: (optional) OAuth refresh token
#   client_id / client_secret: (optional) For OAuth refresh
#   owner_id: (optional) Pipedrive user ID for assigning activities
#   pipeline_id: (optional) Pipeline ID for deal tracking
#   stage_id: (optional) Stage ID for new deals
#
# Optional for SMS:
#   twilio_account_sid, twilio_auth_token, twilio_from_number
#   OR messaging_webhook_url
#
# Contacts (Persons): POST https://{domain}.pipedrive.com/api/v1/persons
# Activities: POST https://{domain}.pipedrive.com/api/v1/activities
# Deals: POST https://{domain}.pipedrive.com/api/v1/deals
# Notes: POST https://{domain}.pipedrive.com/api/v1/notes

import logging
import requests
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from crm_adapters.base import CRMAdapter
from db import update_crm_config_token

logger = logging.getLogger(__name__)

PIPEDRIVE_TIMEOUT = 20


class PipedriveAdapter(CRMAdapter):
    """
    Pipedrive CRM adapter using their REST API v1.
    Manages persons (contacts), activities (appointments), deals, and notes.
    """

    CRM_NAME = "Pipedrive"
    SUPPORTS_MESSAGING = True  # Via Twilio + Pipedrive note logging
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
        self.pipeline_id = self.crm_config.get("pipeline_id", "")
        self.stage_id = self.crm_config.get("stage_id", "")
        self.messaging_webhook_url = self.crm_config.get("messaging_webhook_url", "")

    @property
    def _base_url(self):
        if self.company_domain:
            # Validate company_domain to prevent hostname injection
            import re
            if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9-]*$', self.company_domain):
                logger.error(f"Pipedrive: Invalid company_domain '{self.company_domain}' — contains disallowed characters")
                return "https://api.pipedrive.com/api/v1"
            return f"https://{self.company_domain}.pipedrive.com/api/v1"
        return "https://api.pipedrive.com/api/v1"

    @property
    def _auth_params(self):
        if self.api_token:
            return {"api_token": self.api_token}
        return {}

    @property
    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if not self.api_token and self.pd_oauth_token:
            headers["Authorization"] = f"Bearer {self.pd_oauth_token}"
        return headers

    def _refresh_token(self) -> bool:
        """Refresh Pipedrive OAuth token using refresh_token grant."""
        if not all([self.refresh_token_pd, self.client_id, self.client_secret]):
            return False
        try:
            resp = requests.post("https://oauth.pipedrive.com/oauth/token", data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token_pd,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }, timeout=PIPEDRIVE_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                self.pd_oauth_token = data["access_token"]
                # Pipedrive rotates refresh tokens on each use
                if data.get("refresh_token"):
                    self.refresh_token_pd = data["refresh_token"]
                logger.info("Pipedrive token refreshed successfully")
                if self.location_id:
                    update_crm_config_token(self.location_id, self.pd_oauth_token)
                return True
            logger.error(f"Pipedrive token refresh failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Pipedrive token refresh error: {e}")
        return False

    def _api_request(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """Make an API request with auth params and automatic token refresh on 401."""
        params = kwargs.pop("params", {})
        params.update(self._auth_params)
        try:
            resp = getattr(requests, method)(
                url, headers=self._headers, params=params, timeout=PIPEDRIVE_TIMEOUT, **kwargs
            )
            if resp.status_code == 401 and self._refresh_token():
                params.update(self._auth_params)
                resp = getattr(requests, method)(
                    url, headers=self._headers, params=params, timeout=PIPEDRIVE_TIMEOUT, **kwargs
                )
            return resp
        except Exception as e:
            logger.error(f"Pipedrive API error: {e}")
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
        """Send SMS via Twilio, log as a Note in Pipedrive."""
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
                }, timeout=PIPEDRIVE_TIMEOUT)
                sent = resp.status_code in [200, 201]
            except Exception as e:
                logger.error(f"Pipedrive messaging webhook error: {e}")

        # Log SMS as a Note in Pipedrive
        if sent and contact_id:
            self._log_note(contact_id, message)

        if not sent:
            logger.warning(f"Pipedrive: No messaging method available for {contact_id}")
        return sent

    def _log_note(self, contact_id: str, message: str):
        """Log an outbound SMS as a Note linked to a Person."""
        url = f"{self._base_url}/notes"
        payload = {
            "content": f"[SMS via InsuranceGrokBot] {message}",
            "person_id": int(contact_id) if str(contact_id).isdigit() else None,
        }
        if not payload["person_id"]:
            return
        try:
            resp = self._api_request("post", url, json=payload)
            if resp and resp.status_code in [200, 201] and resp.json().get("success"):
                logger.info(f"Pipedrive: SMS logged as Note for person {contact_id}")
        except Exception as e:
            logger.error(f"Pipedrive: Failed to log Note: {e}")

    def get_free_slots(self) -> str:
        logger.info("Pipedrive adapter: No native free-slots API. Using generic response.")
        return "let me check my calendar and get back to you with some times"

    def book_appointment(self, contact_id: str, first_name: str, selected_time: str) -> bool:
        start_dt, end_dt = self._parse_booking_time(selected_time)
        duration = end_dt - start_dt
        duration_str = f"{duration.seconds // 3600:02d}:{(duration.seconds % 3600) // 60:02d}"

        activity_payload = {
            "subject": f"Life Insurance Review {first_name or 'Lead'}",
            "type": "meeting",
            "due_date": start_dt.strftime("%Y-%m-%d"),
            "due_time": start_dt.strftime("%H:%M"),
            "duration": duration_str,
            "person_id": int(contact_id) if str(contact_id).isdigit() else None,
            "note": f"Appointment booked via InsuranceGrokBot for {first_name or 'Lead'}",
            "done": 0,
        }
        activity_payload = {k: v for k, v in activity_payload.items() if v is not None}

        if self.owner_id:
            activity_payload["user_id"] = int(self.owner_id) if str(self.owner_id).isdigit() else None

        url = f"{self._base_url}/activities"
        resp = self._api_request("post", url, json=activity_payload)
        if resp and resp.status_code in [200, 201]:
            result = resp.json()
            if result.get("success"):
                activity_id = result.get("data", {}).get("id")
                logger.info(f"Pipedrive Activity created: {activity_id} | contact={contact_id}")
                return True
        if resp:
            logger.error(f"Pipedrive book_appointment failed: {resp.status_code} | {resp.text[:300]}")
        return False

    def create_deal(self, person_id: str, first_name: str, value: float = 0) -> Optional[str]:
        """Create a deal in Pipedrive linked to a person."""
        url = f"{self._base_url}/deals"
        payload = {
            "title": f"Life Insurance - {first_name or 'Lead'}",
            "person_id": int(person_id) if str(person_id).isdigit() else None,
            "status": "open",
        }
        if value:
            payload["value"] = value
            payload["currency"] = "USD"
        if self.pipeline_id:
            payload["pipeline_id"] = int(self.pipeline_id)
        if self.stage_id:
            payload["stage_id"] = int(self.stage_id)
        if self.owner_id:
            payload["user_id"] = int(self.owner_id) if str(self.owner_id).isdigit() else None

        payload = {k: v for k, v in payload.items() if v is not None}

        resp = self._api_request("post", url, json=payload)
        if resp and resp.status_code in [200, 201]:
            result = resp.json()
            if result.get("success"):
                deal_id = result.get("data", {}).get("id")
                logger.info(f"Pipedrive Deal created: {deal_id} | person={person_id}")
                return str(deal_id) if deal_id else None
        if resp:
            logger.error(f"Pipedrive create_deal failed: {resp.status_code} | {resp.text[:300]}")
        return None

    def search_contact(self, email: str = None, phone: str = None) -> Optional[dict]:
        """Search Pipedrive persons by email or phone."""
        term = email or phone
        if not term:
            return None

        fields = "email" if email else "phone"
        url = f"{self._base_url}/persons/search"
        resp = self._api_request("get", url, params={
            "term": term,
            "fields": fields,
            "limit": 1,
        })
        if resp and resp.status_code == 200:
            result = resp.json()
            if result.get("success"):
                items = result.get("data", {}).get("items", [])
                if items:
                    item = items[0].get("item", {})
                    name_parts = (item.get("name", "") or "").split(" ", 1)
                    emails = item.get("emails", []) or []
                    phones = item.get("phones", []) or []
                    return {
                        "id": str(item.get("id", "")),
                        "firstName": name_parts[0] if name_parts else "",
                        "lastName": name_parts[1] if len(name_parts) > 1 else "",
                        "email": emails[0] if emails else "",
                        "phone": phones[0] if phones else "",
                    }
        return None

    def update_contact(self, contact_id: str, contact_data: dict) -> bool:
        """Update a Pipedrive person."""
        url = f"{self._base_url}/persons/{contact_id}"
        payload = {}
        first = contact_data.get("first_name", "")
        last = contact_data.get("last_name", "")
        if first or last:
            payload["name"] = f"{first} {last}".strip()
        if contact_data.get("email"):
            payload["email"] = [{"value": contact_data["email"], "primary": True}]
        if contact_data.get("phone"):
            payload["phone"] = [{"value": contact_data["phone"], "primary": True}]

        if not payload:
            return True

        resp = self._api_request("put", url, json=payload)
        if resp and resp.status_code == 200 and resp.json().get("success"):
            logger.info(f"Pipedrive Person updated: {contact_id}")
            return True
        if resp:
            logger.error(f"Pipedrive update_contact failed: {resp.status_code}")
        return False

    def get_contact(self, contact_id: str) -> dict:
        url = f"{self._base_url}/persons/{contact_id}"
        resp = self._api_request("get", url)
        if resp and resp.status_code == 200:
            data = resp.json().get("data", {})
            name_parts = (data.get("name", "") or "").split(" ", 1)
            emails = data.get("email", [])
            phones = data.get("phone", [])
            return {
                "firstName": name_parts[0] if name_parts else "",
                "lastName": name_parts[1] if len(name_parts) > 1 else "",
                "email": emails[0].get("value", "") if emails else "",
                "phone": phones[0].get("value", "") if phones else "",
            }
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
        resp = self._api_request("post", url, json=payload)
        if resp and resp.status_code in [200, 201]:
            result = resp.json()
            if result.get("success"):
                person_id = result.get("data", {}).get("id")
                logger.info(f"Pipedrive Person created: {person_id}")

                # Auto-create a deal for the new person
                if person_id:
                    self.create_deal(str(person_id), first)

                return str(person_id) if person_id else None
        if resp:
            logger.error(f"Pipedrive create_contact failed: {resp.status_code} | {resp.text[:300]}")
        return None

    def validate_credentials(self) -> dict:
        if not self.api_token and not self.pd_oauth_token:
            return {"valid": False, "message": "No Pipedrive API token or OAuth token configured", "details": {}}
        url = f"{self._base_url}/users/me"
        resp = self._api_request("get", url)
        if resp and resp.status_code == 200 and resp.json().get("success"):
            user_data = resp.json().get("data", {})
            return {
                "valid": True,
                "message": f"Pipedrive connected as {user_data.get('name', 'Unknown')}",
                "details": {"user_id": user_data.get("id")}
            }
        return {"valid": False, "message": "Pipedrive validation failed", "details": {}}
