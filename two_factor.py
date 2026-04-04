# two_factor.py — SMS two-factor authentication via Twilio Verify
#
# Uses Twilio Verify Service to send and check 6-digit codes.
# No Twilio branding visible to users — they just get an SMS code.
#
# Env vars:
#   TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN — master account creds (already set)
#   VERIFY_SERVICE_SID — Twilio Verify service ID

import os
import logging
from twilio.rest import Client

logger = logging.getLogger(__name__)

VERIFY_SERVICE_SID = os.getenv('VERIFY_SERVICE_SID', 'VA839faa5d072a897bf732a43880ce27cd')

_client = None


def _get_client():
    """Lazy-init Twilio client using master account credentials."""
    global _client
    if _client is None:
        sid = os.getenv('TWILIO_ACCOUNT_SID')
        token = os.getenv('TWILIO_AUTH_TOKEN')
        if not sid or not token:
            logger.error("Twilio credentials not configured for 2FA")
            return None
        _client = Client(sid, token)
    return _client


def send_verification_code(phone: str) -> dict:
    """Send a 6-digit verification code via SMS.

    Args:
        phone: E.164 format phone number (e.g. '+14155551234')

    Returns:
        {'success': True, 'status': 'pending'} on success
        {'success': False, 'error': '...'} on failure
    """
    client = _get_client()
    if not client:
        return {'success': False, 'error': 'SMS service unavailable'}

    try:
        verification = client.verify.v2 \
            .services(VERIFY_SERVICE_SID) \
            .verifications \
            .create(to=phone, channel='sms')
        logger.info(f"2FA code sent to {phone[:6]}*** — status={verification.status}")
        return {'success': True, 'status': verification.status}
    except Exception as e:
        logger.error(f"2FA send failed for {phone[:6]}***: {e}")
        return {'success': False, 'error': str(e)}


def send_confirmation_sms(phone: str) -> None:
    """Send a one-time confirmation SMS after 2FA is successfully enabled.

    Uses the master Twilio account (same credentials as _get_client()).
    Fires-and-forgets — failure is logged but never raises.
    """
    client = _get_client()
    if not client:
        return

    from_number = os.getenv('TWILIO_PHONE_NUMBER')
    if not from_number:
        logger.warning("TWILIO_PHONE_NUMBER not set — skipping 2FA confirmation SMS")
        return

    body = (
        "You've successfully enabled two-factor authentication on your Omnisconn "
        "account. You'll now receive a verification code each time you log in. "
        "Reply STOP to opt out."
    )

    try:
        client.messages.create(to=phone, from_=from_number, body=body)
        logger.info(f"2FA confirmation SMS sent to {phone[:6]}***")
    except Exception as e:
        logger.error(f"2FA confirmation SMS failed for {phone[:6]}***: {e}")


def check_verification_code(phone: str, code: str) -> dict:
    """Verify a 6-digit code entered by the user.

    Args:
        phone: E.164 format phone number (must match what was sent)
        code: 6-digit code from SMS

    Returns:
        {'success': True, 'valid': True} if code is correct
        {'success': True, 'valid': False} if code is wrong
        {'success': False, 'error': '...'} on failure
    """
    client = _get_client()
    if not client:
        return {'success': False, 'error': 'SMS service unavailable'}

    try:
        check = client.verify.v2 \
            .services(VERIFY_SERVICE_SID) \
            .verification_checks \
            .create(to=phone, code=code)
        valid = check.status == 'approved'
        logger.info(f"2FA check for {phone[:6]}***: status={check.status} valid={valid}")
        return {'success': True, 'valid': valid}
    except Exception as e:
        logger.error(f"2FA check failed for {phone[:6]}***: {e}")
        return {'success': False, 'error': str(e)}
