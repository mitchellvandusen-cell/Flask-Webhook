# crm_adapters/twilio_messaging.py - Universal SMS via Twilio
# Provides SMS sending for any CRM that doesn't have native messaging.
# Configured via crm_config keys: twilio_account_sid, twilio_auth_token, twilio_from_number
# OR via environment variables: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER

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


def get_twilio_config(crm_config: dict) -> dict:
    """Extract Twilio config from CRM config, falling back to env vars."""
    return {
        "account_sid": crm_config.get("twilio_account_sid", "") or os.environ.get("TWILIO_ACCOUNT_SID", ""),
        "auth_token": crm_config.get("twilio_auth_token", "") or os.environ.get("TWILIO_AUTH_TOKEN", ""),
        "from_number": crm_config.get("twilio_from_number", "") or os.environ.get("TWILIO_FROM_NUMBER", ""),
    }


def has_twilio_config(crm_config: dict) -> bool:
    """Check if Twilio credentials are available (config or env vars)."""
    cfg = get_twilio_config(crm_config)
    return bool(cfg["account_sid"] and cfg["auth_token"] and cfg["from_number"])
