"""Module extracted from twilio_provisioning.py."""

import logging
from twilio.base.exceptions import TwilioRestException

from .client import get_master_client, get_sub_account_client

logger = logging.getLogger("twilio_provisioning")

US_STATE_ABBREVS = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}


def search_available_numbers(number_type: str = "local", area_code: str = "",
                              state: str = "", city: str = "",
                              zip_code: str = "", contains: str = "",
                              sms_enabled: bool = True,
                              country: str = "US",
                              limit: int = 20) -> list:
    """
    Search for available phone numbers to purchase.
    Mirrors Twilio's search filters: type (local/toll_free/mobile),
    area code, state, city, zip, contains pattern.
    """
    client = get_master_client()
    try:
        kwargs = {"limit": limit, "voice_enabled": True}
        if sms_enabled:
            kwargs["sms_enabled"] = True
        if area_code:
            kwargs["area_code"] = area_code
        if state:
            normalized = US_STATE_ABBREVS.get(state.strip().lower(), state.strip())
            kwargs["in_region"] = normalized
        if city:
            kwargs["in_locality"] = city
        if zip_code:
            kwargs["in_postal_code"] = zip_code
        if contains:
            kwargs["contains"] = contains

        available = client.available_phone_numbers(country)
        if number_type == "toll_free":
            numbers = available.toll_free.list(**kwargs)
        elif number_type == "mobile":
            if country == "US":
                logger.info("Mobile numbers unavailable for US; falling back to local")
                numbers = available.local.list(**kwargs)
            else:
                numbers = available.mobile.list(**kwargs)
        else:
            numbers = available.local.list(**kwargs)

        return [
            {
                "phone": n.phone_number,
                "friendly_name": n.friendly_name,
                "locality": n.locality or "",
                "region": n.region or "",
                "postal_code": getattr(n, "postal_code", "") or "",
                "capabilities": {
                    "voice": n.capabilities.get("voice", False),
                    "sms": n.capabilities.get("SMS", False),
                    "mms": n.capabilities.get("MMS", False),
                },
            }
            for n in numbers
        ]
    except TwilioRestException as e:
        logger.error(f"Number search failed: {e}")
        return []


def buy_phone_number(sub_account_sid: str, phone_number: str,
                      webhook_base_url: str, twiml_app_sid: str = "") -> dict:
    """
    Purchase a phone number on a sub-account and configure it for voice.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        kwargs = {
            "phone_number": phone_number,
            "voice_url": f"{webhook_base_url}/voice/inbound",
            "voice_method": "POST",
            "status_callback": f"{webhook_base_url}/voice/status",
            "status_callback_method": "POST",
        }
        if twiml_app_sid:
            kwargs["voice_application_sid"] = twiml_app_sid

        number = client.incoming_phone_numbers.create(**kwargs)
        logger.info(f"Purchased number {number.phone_number} (SID: {number.sid}) on sub-account {sub_account_sid}")
        return {
            "sid": number.sid,
            "phone": number.phone_number,
            "friendly_name": number.friendly_name,
        }
    except TwilioRestException as e:
        logger.error(f"Number purchase failed: {e}")
        raise


def list_phone_numbers(sub_account_sid: str) -> list:
    """List all phone numbers on a sub-account."""
    client = get_sub_account_client(sub_account_sid)
    try:
        numbers = client.incoming_phone_numbers.list()
        return [
            {
                "sid": n.sid,
                "phone": n.phone_number,
                "friendly_name": n.friendly_name,
                "capabilities": {
                    "voice": n.capabilities.get("voice", False),
                    "sms": n.capabilities.get("sms", False),
                    "mms": n.capabilities.get("mms", False),
                    "fax": n.capabilities.get("fax", False),
                },
                "status": "active",
                "voice_url": n.voice_url or "",
                "created_at": n.date_created.isoformat() if n.date_created else "",
            }
            for n in numbers
        ]
    except TwilioRestException as e:
        logger.error(f"Failed to list numbers: {e}")
        return []


def release_phone_number(sub_account_sid: str, number_sid: str) -> bool:
    """Release a phone number from a sub-account."""
    client = get_sub_account_client(sub_account_sid)
    try:
        client.incoming_phone_numbers(number_sid).delete()
        logger.info(f"Released number {number_sid} from sub-account {sub_account_sid}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to release number: {e}")
        return False


def update_phone_number_webhooks(sub_account_sid: str, number_sid: str,
                                   webhook_base_url: str) -> bool:
    """Update webhooks on an existing phone number."""
    client = get_sub_account_client(sub_account_sid)
    try:
        client.incoming_phone_numbers(number_sid).update(
            voice_url=f"{webhook_base_url}/voice/inbound",
            voice_method="POST",
            status_callback=f"{webhook_base_url}/voice/status",
            status_callback_method="POST",
        )
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to update number webhooks: {e}")
        return False



