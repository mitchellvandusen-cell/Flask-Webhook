# crm_adapters/salesforce_adapter.py - Salesforce REST API Adapter
# Uses Salesforce REST API v66.0 for contacts and events.
#
# Required crm_config keys:
#   instance_url: Salesforce instance (e.g., https://na1.salesforce.com)
#   access_token: OAuth Bearer token
#   refresh_token: (optional) For auto-refresh
#   client_id: (optional) Connected App client ID
#   client_secret: (optional) Connected App client secret
#
# Authentication: OAuth 2.0 Bearer token
# Contacts: POST /services/data/v66.0/sobjects/Contact
# Events: POST /services/data/v66.0/sobjects/Event

import logging
import requests
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from crm_adapters.base import CRMAdapter

logger = logging.getLogger(__name__)

SF_API_VERSION = "v66.0"
SF_TIMEOUT = 20


class SalesforceAdapter(CRMAdapter):
    """
    Salesforce CRM adapter using REST API.
    Manages contacts (Contact sObject) and appointments (Event sObject).
    """

    CRM_NAME = "Salesforce"
    SUPPORTS_MESSAGING = False  # Salesforce doesn't have native SMS
    SUPPORTS_CALENDAR = True
    SUPPORTS_CONTACTS = True

    def __init__(self, subscriber_data: dict):
        super().__init__(subscriber_data)
        self.instance_url = self.crm_config.get("instance_url", "").rstrip("/")
        self.sf_token = self.crm_config.get("access_token", "") or self.access_token
        self.refresh_token_sf = self.crm_config.get("refresh_token", "")
        self.client_id = self.crm_config.get("client_id", "")
        self.client_secret = self.crm_config.get("client_secret", "")

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
        """Attempt to refresh the Salesforce OAuth token."""
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
                return True
            logger.error(f"Salesforce token refresh failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Salesforce token refresh error: {e}")
        return False

    def send_message(self, contact_id: str, message: str, **kwargs) -> bool:
        # Salesforce doesn't have native SMS. Use Zapier/external for messaging.
        logger.info(f"Salesforce adapter: Messaging not supported natively. Use Zapier integration for SMS.")
        return False

    def get_free_slots(self) -> str:
        # Salesforce doesn't have a native free-slots API like GHL.
        # Query existing events to find gaps, or return generic response.
        logger.info("Salesforce adapter: No native free-slots API. Using generic response.")
        return "let me check my calendar and get back to you with some times"

    def book_appointment(self, contact_id: str, first_name: str, selected_time: str) -> bool:
        """Create a Salesforce Event linked to a Contact."""
        if not self.instance_url:
            logger.error("Salesforce: No instance_url configured")
            return False

        # Parse the selected time
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
            period = match.group(3).lower()
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
                    h += 12  # Assume PM for business hours
                hour = h

        start_dt = datetime.combine(target_date, dt_time(max(9, min(19, hour)), minute), tzinfo=local_tz)
        end_dt = start_dt + timedelta(minutes=30)

        event_payload = {
            "Subject": f"Life Insurance Review {first_name or 'Lead'}",
            "StartDateTime": start_dt.isoformat(),
            "EndDateTime": end_dt.isoformat(),
            "WhoId": contact_id,  # Link to Contact record
            "Description": f"Appointment booked via InsuranceGrokBot for {first_name or 'Lead'}",
        }

        if self.crm_user_id:
            event_payload["OwnerId"] = self.crm_user_id

        url = f"{self._base_url}/sobjects/Event"
        try:
            resp = requests.post(url, json=event_payload, headers=self._headers, timeout=SF_TIMEOUT)
            if resp.status_code in [200, 201]:
                result = resp.json()
                logger.info(f"Salesforce Event created: {result.get('id')} | contact={contact_id}")
                return True
            elif resp.status_code == 401:
                # Try token refresh
                if self._refresh_token():
                    resp = requests.post(url, json=event_payload, headers=self._headers, timeout=SF_TIMEOUT)
                    if resp.status_code in [200, 201]:
                        logger.info(f"Salesforce Event created after refresh: {resp.json().get('id')}")
                        return True
            logger.error(f"Salesforce book_appointment failed: {resp.status_code} | {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Salesforce book_appointment error: {e}")
        return False

    def get_contact(self, contact_id: str) -> dict:
        if not self.instance_url:
            return {}
        url = f"{self._base_url}/sobjects/Contact/{contact_id}"
        try:
            resp = requests.get(url, headers=self._headers, timeout=SF_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "firstName": data.get("FirstName", ""),
                    "lastName": data.get("LastName", ""),
                    "email": data.get("Email", ""),
                    "phone": data.get("Phone", ""),
                }
            elif resp.status_code == 401 and self._refresh_token():
                resp = requests.get(url, headers=self._headers, timeout=SF_TIMEOUT)
                if resp.status_code == 200:
                    data = resp.json()
                    return {
                        "firstName": data.get("FirstName", ""),
                        "lastName": data.get("LastName", ""),
                        "email": data.get("Email", ""),
                        "phone": data.get("Phone", ""),
                    }
        except Exception as e:
            logger.error(f"Salesforce get_contact error: {e}")
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
        try:
            resp = requests.post(url, json=payload, headers=self._headers, timeout=SF_TIMEOUT)
            if resp.status_code in [200, 201]:
                result = resp.json()
                logger.info(f"Salesforce Contact created: {result.get('id')}")
                return result.get("id")
            elif resp.status_code == 401 and self._refresh_token():
                resp = requests.post(url, json=payload, headers=self._headers, timeout=SF_TIMEOUT)
                if resp.status_code in [200, 201]:
                    return resp.json().get("id")
            logger.error(f"Salesforce create_contact failed: {resp.status_code} | {resp.text[:300]}")
        except Exception as e:
            logger.error(f"Salesforce create_contact error: {e}")
        return None

    def validate_credentials(self) -> dict:
        if not self.instance_url or not self.sf_token:
            return {"valid": False, "message": "Missing Salesforce instance URL or access token", "details": {}}
        try:
            resp = requests.get(
                f"{self.instance_url}/services/data/{SF_API_VERSION}/limits",
                headers=self._headers, timeout=SF_TIMEOUT
            )
            if resp.status_code == 200:
                return {"valid": True, "message": "Salesforce credentials valid", "details": {"api_version": SF_API_VERSION}}
            elif resp.status_code == 401:
                if self._refresh_token():
                    return {"valid": True, "message": "Salesforce token refreshed and valid", "details": {}}
                return {"valid": False, "message": "Salesforce token expired and refresh failed", "details": {}}
            return {"valid": False, "message": f"Salesforce returned {resp.status_code}", "details": {}}
        except Exception as e:
            return {"valid": False, "message": f"Salesforce connection error: {e}", "details": {}}
