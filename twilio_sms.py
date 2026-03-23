# twilio_sms.py - Direct Twilio SMS sender (bypasses GHL API)
# Used when subscriber chooses to send SMS via their Twilio sub-account number
# instead of through GHL/LeadConnector.
#
# Return format matches ghl_message.py's 3-tuple: (success, fail_reason, http_detail)

import logging
import time as time_module
from db import get_db_connection, return_db_connection
from reply_sanitizer import is_safe_to_send

logger = logging.getLogger(__name__)


def send_sms_via_twilio(
    phone_to: str,
    message: str,
    from_number: str,
    twilio_sub_account_sid: str,
    twilio_auth_token: str,
    contact_id: str = None,
    max_retries: int = 3,
    retry_delay: int = 3
) -> tuple:
    """
    Send SMS directly through Twilio sub-account (bypasses GHL API entirely).

    Args:
        phone_to: Recipient phone number (E.164 format: +1234567890)
        message: SMS text to send
        from_number: Twilio number to send from (E.164 format)
        twilio_sub_account_sid: Subscriber's Twilio sub-account SID
        twilio_auth_token: Subscriber's Twilio sub-account auth token
        contact_id: Optional GHL contact ID for dedup/logging
        max_retries: Number of retry attempts
        retry_delay: Base delay between retries (seconds)

    Returns 3-tuple matching ghl_message.py pattern:
        (True,  None,         None)  — SMS sent successfully
        (True,  'duplicate',  None)  — Already sent recently (dedup)
        (False, 'safety',     None)  — Blocked by safety filter
        (False, 'invalid',    None)  — Missing required params
        (False, 'auth',       {...}) — Twilio auth error
        (False, 'rate_limit', {...}) — Rate limited
        (False, 'network',    {...}) — Network/timeout error
        (False, 'error',      {...}) — Other error
    """
    if not phone_to or not message or not from_number:
        logger.warning(f"Cannot send Twilio SMS: missing params (to={bool(phone_to)}, "
                       f"from={bool(from_number)}, msg={bool(message)})")
        return False, 'invalid', None

    if not twilio_sub_account_sid or not twilio_auth_token:
        logger.warning("Cannot send Twilio SMS: missing sub-account credentials")
        return False, 'invalid', None

    # Safety filter
    if not is_safe_to_send(message):
        logger.error(f"TWILIO SMS BLOCKED BY SAFETY NET for {phone_to}")
        return False, 'safety', None

    # Duplicate prevention (same as ghl_message.py)
    if contact_id:
        conn = get_db_connection()
        if conn:
            cur = None
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT 1 FROM contact_messages
                    WHERE contact_id = %s
                      AND message_type = 'assistant'
                      AND message_text = %s
                      AND created_at > NOW() - INTERVAL '5 minutes'
                    LIMIT 1
                """, (contact_id, message.strip()))
                if cur.fetchone():
                    logger.warning(f"SKIP DUPLICATE TWILIO SMS: same message sent recently to {contact_id}")
                    return True, 'duplicate', None
            except Exception as e:
                logger.error(f"Duplicate check failed: {e}")
            finally:
                if cur:
                    cur.close()
                return_db_connection(conn)

    # Send via Twilio REST API
    from twilio.rest import Client as TwilioClient

    last_failure = 'error'
    last_status = 0
    last_body = ''
    total_attempts = 0

    for attempt in range(1, max_retries + 1):
        total_attempts = attempt
        try:
            client = TwilioClient(twilio_sub_account_sid, twilio_auth_token)
            msg = client.messages.create(
                body=message.strip(),
                from_=from_number,
                to=phone_to
            )
            logger.info(f"Twilio SMS sent to {phone_to} | SID={msg.sid} | attempt={attempt}")
            return True, None, None

        except Exception as e:
            err_str = str(e)
            err_code = getattr(e, 'code', 0)
            err_status = getattr(e, 'status', 0)

            if err_status in (401, 403) or err_code == 20003:
                logger.error(f"Twilio auth error sending to {phone_to}: {err_str}")
                return False, 'auth', {
                    "status_code": err_status,
                    "response_body": err_str[:500],
                    "attempts": attempt,
                }
            elif err_status == 429 or err_code == 20429:
                last_failure = 'rate_limit'
                logger.warning(f"Twilio rate limited on attempt {attempt}")
                time_module.sleep(10)
            elif err_code in (21211, 21214, 21217, 21610, 21612, 21408):
                # Invalid number, cannot route, not SMS-capable, blacklisted, etc.
                logger.error(f"Twilio number error {err_code} for {phone_to}: {err_str}")
                return False, 'invalid', {
                    "status_code": err_status,
                    "response_body": err_str[:500],
                    "attempts": attempt,
                    "twilio_code": err_code,
                }
            else:
                last_failure = 'network'
                last_body = err_str[:500]
                last_status = err_status
                logger.warning(f"Twilio SMS attempt {attempt} error: {err_str}")

        if attempt < max_retries:
            time_module.sleep(retry_delay * attempt)

    logger.error(f"Failed Twilio SMS to {phone_to} after {max_retries} attempts ({last_failure})")
    return False, last_failure, {
        "status_code": last_status,
        "response_body": last_body,
        "attempts": total_attempts,
    }


def get_twilio_credentials(location_id):
    """
    Get Twilio credentials for a subscriber.

    Resolution order:
    1. Subscriber's own Twilio sub-account (voice_config JSONB)
    2. Master Twilio account (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN env vars)

    Returns (account_sid, auth_token, from_number) or (None, None, None).
    """
    import os

    conn = get_db_connection()
    if not conn:
        # Even without DB, try master account as last resort
        return _get_master_twilio_credentials()

    try:
        cur = conn.cursor()
        # Check subscribers first
        cur.execute("""
            SELECT voice_config, sms_send_via FROM subscribers
            WHERE location_id = %s LIMIT 1
        """, (location_id,))
        row = cur.fetchone()

        if not row:
            # Check agency_billing
            cur.execute("""
                SELECT voice_config, sms_send_via FROM agency_billing
                WHERE location_id = %s LIMIT 1
            """, (location_id,))
            row = cur.fetchone()

        cur.close()

        if not row:
            return _get_master_twilio_credentials()

        vc = row.get('voice_config') or {}
        sms_via = row.get('sms_send_via', 'ghl')

        sub_sid = vc.get('twilio_sub_account_sid')
        auth_tok = vc.get('twilio_auth_token')

        # Determine the "from" number
        # If sms_send_via is a phone number (starts with +), use that
        # Otherwise use the primary Twilio number
        if sms_via and sms_via.startswith('+'):
            from_number = sms_via
        else:
            from_number = vc.get('twilio_phone_number')

        # Fallback: if no primary number set, find any active number from number_health
        if not from_number and sub_sid and auth_tok:
            try:
                nh_conn = get_db_connection()
                if nh_conn:
                    try:
                        nh_cur = nh_conn.cursor()
                        nh_cur.execute("""
                            SELECT phone FROM number_health
                            WHERE location_id = %s AND status = 'active'
                            ORDER BY created_at ASC LIMIT 1
                        """, (location_id,))
                        nh_row = nh_cur.fetchone()
                        if nh_row and nh_row.get('phone'):
                            from_number = nh_row['phone']
                            logger.info(f"Twilio fallback: using number_health phone {from_number} for {location_id}")
                        nh_cur.close()
                    finally:
                        return_db_connection(nh_conn)
            except Exception as nh_err:
                logger.debug(f"number_health phone lookup failed: {nh_err}")

        # If subscriber has their own sub-account creds, use them
        if sub_sid and auth_tok:
            return sub_sid, auth_tok, from_number

        # Subscriber has no Twilio sub-account — fall back to master account
        # so SMS can still be delivered when GHL OAuth is also unavailable
        master_sid, master_auth, master_phone = _get_master_twilio_credentials()
        if master_sid and master_auth and master_phone:
            logger.info(f"Twilio fallback: using master account for {location_id} (no sub-account provisioned)")
            return master_sid, master_auth, from_number or master_phone

        return None, None, None

    except Exception as e:
        logger.error(f"Failed to get Twilio credentials for {location_id}: {e}")
        return _get_master_twilio_credentials()
    finally:
        return_db_connection(conn)


def _get_master_twilio_credentials():
    """
    Get master Twilio account credentials from environment variables.
    Used as last-resort fallback when subscriber has no sub-account.
    Returns (account_sid, auth_token, phone_number) or (None, None, None).
    """
    import os
    master_sid = os.getenv('TWILIO_ACCOUNT_SID')
    master_auth = os.getenv('TWILIO_AUTH_TOKEN')
    master_phone = os.getenv('TWILIO_PHONE_NUMBER')
    if master_sid and master_auth and master_phone:
        return master_sid, master_auth, master_phone
    return None, None, None
