"""Module extracted from twilio_provisioning.py."""

import logging
from twilio.base.exceptions import TwilioRestException

from .client import get_sub_account_client

logger = logging.getLogger("twilio_provisioning")


def get_spam_protection_status(sub_account_sid: str) -> dict:
    """Get current spam/CNAM protection status for a sub-account.

    Note: 'protected' count only includes numbers whose friendly_name was
    explicitly set to a business/personal CNAM name — NOT numbers that still
    have Twilio's default friendly_name (which is the phone number itself).
    Setting friendly_name does NOT constitute real CNAM registration.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        numbers = client.incoming_phone_numbers.list()
        total = len(numbers)
        nums_list = []
        protected = 0
        for n in numbers:
            fn = (n.friendly_name or "").strip()
            phone = n.phone_number or ""
            # A number has a CNAM name only if friendly_name is not the phone number default
            is_cnam = bool(fn) and fn != phone and not fn.startswith('+')
            if is_cnam:
                protected += 1
            nums_list.append({
                "phone": phone,
                "sid": n.sid,
                "friendly_name": fn,
                "status": "active",
            })

        return {
            "numbers_total": total,
            "numbers_protected": protected,
            "stir_shaken": "active",  # Twilio auto-manages STIR/SHAKEN
            "numbers": nums_list,
        }
    except TwilioRestException as e:
        logger.error(f"Failed to get spam protection status: {e}")
        return {"numbers_total": 0, "numbers_protected": 0}


def get_cnam_monitor(sub_account_sid: str) -> list:
    """
    Get CNAM status for all numbers on a sub-account.
    Returns a list of dicts with phone, sid, friendly_name (the CNAM we set),
    and cnam_enabled flag.

    Note: friendly_name is just a Twilio-internal label. It does NOT mean
    the number has real CNAM registration with carriers. A number's
    friendly_name is always set (Twilio defaults it to the phone number).
    cnam_enabled is True only if the friendly_name was explicitly set to
    a business/personal name (not the phone number default).
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        numbers = client.incoming_phone_numbers.list()
        result = []
        for n in numbers:
            fn = (n.friendly_name or "").strip()
            phone = n.phone_number or ""
            # friendly_name is a real CNAM name only if it's not the phone number itself
            is_cnam = bool(fn) and fn != phone and not fn.startswith('+')
            result.append({
                "phone": phone,
                "sid": n.sid,
                "friendly_name": fn,
                "cnam_enabled": is_cnam,
                "date_created": str(n.date_created) if n.date_created else "",
            })
        return result
    except TwilioRestException as e:
        logger.error(f"CNAM monitor failed for {sub_account_sid}: {e}")
        return []


def update_cnam_for_number(sub_account_sid: str, number_sid: str,
                           business_name: str) -> dict:
    """Update CNAM (friendly_name) for a single phone number."""
    client = get_sub_account_client(sub_account_sid)
    try:
        cnam_name = business_name[:15].strip() if business_name else ""
        updated = client.incoming_phone_numbers(number_sid).update(
            friendly_name=cnam_name,
        )
        return {
            "status": "ok",
            "phone": updated.phone_number,
            "sid": updated.sid,
            "friendly_name": updated.friendly_name,
        }
    except TwilioRestException as e:
        logger.error(f"CNAM update failed for {number_sid}: {e}")
        return {"status": "error", "error": str(e)}


def cnam_lookup(phone_number: str) -> dict:
    """
    Look up CNAM (caller name) for a phone number using Twilio Lookup API v2.
    This queries what carriers see as the caller name for a given number.
    Uses master account credentials (lookup is not sub-account scoped).
    """
    client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    try:
        result = client.lookups.v2.phone_numbers(phone_number).fetch(
            fields="caller_name"
        )
        caller_name_info = result.caller_name or {}
        return {
            "phone": result.phone_number or phone_number,
            "caller_name": caller_name_info.get("caller_name", ""),
            "caller_type": caller_name_info.get("caller_type", ""),
            "error_code": caller_name_info.get("error_code"),
            "valid": result.valid if hasattr(result, 'valid') else True,
            "national_format": getattr(result, 'national_format', ''),
        }
    except TwilioRestException as e:
        logger.error(f"CNAM lookup failed for {phone_number}: {e}")
        return {
            "phone": phone_number,
            "caller_name": "",
            "caller_type": "",
            "error_code": e.code if hasattr(e, 'code') else None,
            "error": str(e),
        }




