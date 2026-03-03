# crm_adapters/twilio_messaging.py - Universal SMS via Twilio
# Provides SMS sending for any CRM adapter using the subscriber's
# IGB Twilio sub-account (provisioned automatically).
#
# Primary: Uses voice_config credentials (IGB sub-account)
# Fallback: Uses crm_config Twilio keys (user-provided, legacy)
# Last resort: Environment variables

import logging
import os
import requests
from typing import Optional

logger = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"
TWILIO_TIMEOUT = 15


def send_sms_via_twilio(
    to_phone: str,
    message: str,
    account_sid: str = None,
    auth_token: str = None,
    from_number: str = None,
    max_retries: int = 3
) -> bool:
    """
    Send an SMS via Twilio REST API.
    Falls back to environment variables if credentials not passed directly.
    """
    sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
    token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")
    from_num = from_number or os.environ.get("TWILIO_FROM_NUMBER", "")

    if not all([sid, token, from_num]):
        logger.error("Twilio SMS: Missing credentials (account_sid, auth_token, or from_number)")
        return False

    if not to_phone:
        logger.warning("Twilio SMS: No recipient phone number")
        return False

    url = f"{TWILIO_API_BASE}/Accounts/{sid}/Messages.json"

    payload = {
        "To": to_phone,
        "From": from_num,
        "Body": message.strip()
    }

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(
                url,
                data=payload,
                auth=(sid, token),
                timeout=TWILIO_TIMEOUT
            )
            if resp.status_code in [200, 201]:
                result = resp.json()
                msg_sid = result.get("sid", "")
                logger.info(f"Twilio SMS sent: {msg_sid} | to={to_phone}")
                return True
            elif resp.status_code == 429:
                logger.warning(f"Twilio rate limited, attempt {attempt}")
                import time
                time.sleep(5)
            elif resp.status_code in [401, 403]:
                logger.error(f"Twilio auth failure: {resp.status_code}")
                return False
            else:
                logger.warning(f"Twilio SMS failed: {resp.status_code} | {resp.text[:200]}")
        except requests.RequestException as e:
            logger.warning(f"Twilio SMS network error attempt {attempt}: {e}")

        if attempt < max_retries:
            import time
            time.sleep(2 * attempt)

    logger.error(f"Twilio SMS failed after {max_retries} attempts | to={to_phone}")
    return False


def get_twilio_config(crm_config: dict, voice_config: dict = None) -> dict:
    """
    Extract Twilio config, preferring IGB sub-account (voice_config) over
    user-provided CRM config keys. Falls back to env vars.
    """
    vc = voice_config or {}

    # Priority 1: IGB sub-account from voice_config
    igb_sid = vc.get("twilio_sub_account_sid", "")
    igb_token = vc.get("twilio_auth_token", "")
    igb_number = vc.get("twilio_phone_number", "")

    if igb_sid and igb_token and igb_number:
        return {
            "account_sid": igb_sid,
            "auth_token": igb_token,
            "from_number": igb_number,
        }

    # Priority 2: User-provided Twilio keys in crm_config (legacy)
    crm_sid = crm_config.get("twilio_account_sid", "")
    crm_token = crm_config.get("twilio_auth_token", "")
    crm_number = crm_config.get("twilio_from_number", "")

    if crm_sid and crm_token and crm_number:
        return {
            "account_sid": crm_sid,
            "auth_token": crm_token,
            "from_number": crm_number,
        }

    # Priority 3: Environment variables
    return {
        "account_sid": os.environ.get("TWILIO_ACCOUNT_SID", ""),
        "auth_token": os.environ.get("TWILIO_AUTH_TOKEN", ""),
        "from_number": os.environ.get("TWILIO_FROM_NUMBER", ""),
    }


def has_twilio_config(crm_config: dict, voice_config: dict = None) -> bool:
    """Check if Twilio credentials are available (IGB sub-account, config, or env vars)."""
    cfg = get_twilio_config(crm_config, voice_config)
    return bool(cfg["account_sid"] and cfg["auth_token"] and cfg["from_number"])
