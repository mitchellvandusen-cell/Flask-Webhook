# crm_adapters/salesforce_adapter.py - Salesforce REST API Adapter
# Uses Salesforce REST API v66.0 for contacts, events, and email.
#
# Required crm_config keys:
#   instance_url: Salesforce instance (e.g., https://na1.salesforce.com)
#   access_token: OAuth Bearer token
#   refresh_token: (optional) For auto-refresh
#   client_id: (optional) Connected App client ID
#   client_secret: (optional) Connected App client secret
#
# SMS: Uses IGB Twilio sub-account (auto-provisioned) — no user Twilio config needed
#
# Authentication: OAuth 2.0 Bearer token
# Contacts: /services/data/v66.0/sobjects/Contact
# Events: /services/data/v66.0/sobjects/Event
# Search: /services/data/v66.0/query/?q=SOQL

import logging
import requests
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from crm_adapters.base import CRMAdapter
from db import update_crm_config_token

logger = logging.getLogger(__name__)

SF_API_VERSION = "v66.0"
SF_TIMEOUT = 20


class SalesforceAdapter(CRMAdapter):
    """
    Salesforce CRM adapter using REST API.
    Manages contacts (Contact sObject), appointments (Event sObject),
    and messaging via Twilio or webhook relay.
    """

    CRM_NAME = "Salesforce"
    SUPPORTS_MESSAGING = True  # Via Twilio or webhook relay
    SUPPORTS_CALENDAR = True
    SUPPORTS_CONTACTS = True

    def __init__(self, subscriber_data: dict):
        super().__init__(subscriber_data)
        self.instance_url = self.crm_config.get("instance_url", "").rstrip("/")
        self.sf_token = self.crm_config.get("access_token", "") or self.access_token
        self.refresh_token_sf = self.crm_config.get("refresh_token", "")
        self.client_id = self.crm_config.get("client_id", "")
        self.client_secret = self.crm_config.get("client_secret", "")
        self.messaging_webhook_url = self.crm_config.get("messaging_webhook_url", "")

    @property
    def _headers(self):
        return {
            "Authorization": f"Bearer {self.sf_token}",
            "Content-Type": "application/json"
        }

    @property
    def _base_url(self):
        return f"{self.instance_url}/services/data/{SF_API_VERSION}"

    def _refresh_token(self) -> bool:
        if not all([self.refresh_token_sf, self.client_id, self.client_secret]):
            return False
        try:
            resp = requests.post(f"{self.instance_url}/services/oauth2/token", data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token_sf,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }, timeout=SF_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                self.sf_token = data["access_token"]
                logger.info("Salesforce token refreshed successfully")
                if self.location_id:
                    update_crm_config_token(self.location_id, self.sf_token)
                return True
            logger.error(f"Salesforce token refresh failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Salesforce token refresh error: {e}")
        return False

    def _api_request(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """Make an API request with automatic token refresh on 401."""
        try:
            resp = getattr(requests, method)(url, headers=self._headers, timeout=SF_TIMEOUT, **kwargs)
            if resp.status_code == 401 and self._refresh_token():
                resp = getattr(requests, method)(url, headers=self._headers, timeout=SF_TIMEOUT, **kwargs)
            return resp
        except Exception as e:
            logger.error(f"Salesforce API error: {e}")
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
        """Send SMS via Twilio or webhook relay, then log as Task in Salesforce."""
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
                }, timeout=SF_TIMEOUT)
                sent = resp.status_code in [200, 201]
            except Exception as e:
                logger.error(f"Salesforce messaging webhook error: {e}")

        # Log the message as a Task in Salesforce regardless
        if sent and self.instance_url:
            self._log_activity(contact_id, message)

        if not sent:
            logger.warning(f"Salesforce: No messaging method available for {contact_id}")
        return sent

    def _log_activity(self, contact_id: str, message: str):
        """Log an outbound SMS as a Salesforce Task for the activity timeline."""
        url = f"{self._base_url}/sobjects/Task"
        payload = {
            "Subject": "SMS Sent via InsuranceGrokBot",
            "Description": message[:32000],
            "WhoId": contact_id,
            "Status": "Completed",
            "Priority": "Normal",
            "ActivityDate": datetime.now().strftime("%Y-%m-%d"),
            "Type": "Other",
        }
        try:
            resp = self._api_request("post", url, json=payload)
            if resp and resp.status_code in [200, 201]:
                logger.info(f"Salesforce: SMS logged as Task for {contact_id}")
        except Exception as e:
            logger.error(f"Salesforce: Failed to log SMS activity: {e}")

    def get_free_slots(self) -> str:
        logger.info("Salesforce adapter: No native free-slots API. Using generic response.")
        return "let me check my calendar and get back to you with some times"

    def book_appointment(self, contact_id: str, first_name: str, selected_time: str) -> bool:
        if not self.instance_url:
            logger.error("Salesforce: No instance_url configured")
            return False

        start_dt, end_dt = self._parse_booking_time(selected_time)

        event_payload = {
            "Subject": f"Life Insurance Review {first_name or 'Lead'}",
            "StartDateTime": start_dt.isoformat(),
            "EndDateTime": end_dt.isoformat(),
            "WhoId": contact_id,
            "Description": f"Appointment booked via InsuranceGrokBot for {first_name or 'Lead'}",
        }

        if self.crm_user_id:
            event_payload["OwnerId"] = self.crm_user_id

        url = f"{self._base_url}/sobjects/Event"
        resp = self._api_request("post", url, json=event_payload)
        if resp and resp.status_code in [200, 201]:
            result = resp.json()
            logger.info(f"Salesforce Event created: {result.get('id')} | contact={contact_id}")
            return True

        if resp:
            logger.error(f"Salesforce book_appointment failed: {resp.status_code} | {resp.text[:300]}")
        return False

    def search_contact(self, email: str = None, phone: str = None) -> Optional[dict]:
        """Search Salesforce contacts by email or phone using SOQL."""
        if not self.instance_url:
            return None

        conditions = []
        if email:
            safe_email = email.replace("'", "''")
            conditions.append(f"Email = '{safe_email}'")
        if phone:
            safe_phone = phone.replace("'", "''")
            conditions.append(f"Phone = '{safe_phone}'")

        if not conditions:
            return None

        where_clause = " OR ".join(conditions)
        soql = f"SELECT Id, FirstName, LastName, Email, Phone FROM Contact WHERE {where_clause} LIMIT 1"
        url = f"{self._base_url}/query/"

        resp = self._api_request("get", url, params={"q": soql})
        if resp and resp.status_code == 200:
            records = resp.json().get("records", [])
            if records:
                r = records[0]
                return {
                    "id": r.get("Id", ""),
                    "firstName": r.get("FirstName", ""),
                    "lastName": r.get("LastName", ""),
                    "email": r.get("Email", ""),
                    "phone": r.get("Phone", ""),
                }
        return None

    def update_contact(self, contact_id: str, contact_data: dict) -> bool:
        """Update a Salesforce Contact record."""
        if not self.instance_url:
            return False
        url = f"{self._base_url}/sobjects/Contact/{contact_id}"
        payload = {}
        if contact_data.get("first_name"):
            payload["FirstName"] = contact_data["first_name"]
        if contact_data.get("last_name"):
            payload["LastName"] = contact_data["last_name"]
        if contact_data.get("email"):
            payload["Email"] = contact_data["email"]
        if contact_data.get("phone"):
            payload["Phone"] = contact_data["phone"]

        if not payload:
            return True

        resp = self._api_request("patch", url, json=payload)
        if resp and resp.status_code == 204:
            logger.info(f"Salesforce Contact updated: {contact_id}")
            return True
        if resp:
            logger.error(f"Salesforce update_contact failed: {resp.status_code}")
        return False

    def get_contact(self, contact_id: str) -> dict:
        if not self.instance_url:
            return {}
        url = f"{self._base_url}/sobjects/Contact/{contact_id}"
        resp = self._api_request("get", url)
        if resp and resp.status_code == 200:
            data = resp.json()
            return {
                "firstName": data.get("FirstName", ""),
                "lastName": data.get("LastName", ""),
                "email": data.get("Email", ""),
                "phone": data.get("Phone", ""),
            }
        return {}

    def create_contact(self, contact_data: dict) -> Optional[str]:
        if not self.instance_url:
            return None
        url = f"{self._base_url}/sobjects/Contact"
        payload = {
            "FirstName": contact_data.get("first_name", ""),
            "LastName": contact_data.get("last_name", "Unknown"),
            "Email": contact_data.get("email", ""),
            "Phone": contact_data.get("phone", ""),
        }
        resp = self._api_request("post", url, json=payload)
        if resp and resp.status_code in [200, 201]:
            result = resp.json()
            logger.info(f"Salesforce Contact created: {result.get('id')}")
            return result.get("id")
        if resp:
            logger.error(f"Salesforce create_contact failed: {resp.status_code} | {resp.text[:300]}")
        return None

    def validate_credentials(self) -> dict:
        if not self.instance_url or not self.sf_token:
            return {"valid": False, "message": "Missing Salesforce instance URL or access token", "details": {}}
        resp = self._api_request("get", f"{self.instance_url}/services/data/{SF_API_VERSION}/limits")
        if resp and resp.status_code == 200:
            return {"valid": True, "message": "Salesforce credentials valid", "details": {"api_version": SF_API_VERSION}}
        return {"valid": False, "message": f"Salesforce validation failed", "details": {}}
