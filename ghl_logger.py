# ghl_logger.py - Log externally-sent messages and calls into GHL conversations
#
# When SMS is sent via Twilio (not GHL) or calls are made through the dialer,
# GHL doesn't know about them. This module posts those events back to GHL
# so agents see complete conversation history in their CRM.
#
# Uses GHL Conversations API:
#   - POST /conversations/messages/outbound  (outbound SMS + call logs)
#   - POST /conversations/messages/inbound   (inbound SMS received on Twilio)

import logging
import uuid
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

GHL_BASE = "https://services.leadconnectorhq.com"
GHL_INBOUND_URL = f"{GHL_BASE}/conversations/messages/inbound"
GHL_OUTBOUND_URL = f"{GHL_BASE}/conversations/messages/outbound"
GHL_NOTES_URL = f"{GHL_BASE}/contacts/{{contact_id}}/notes"

# API version for v2 endpoints
GHL_V2_HEADERS = {"Version": "2021-07-28", "Content-Type": "application/json"}


def _ghl_headers(access_token: str) -> dict:
    """Build auth headers for GHL API calls."""
    return {
        "Authorization": f"Bearer {access_token}",
        **GHL_V2_HEADERS,
    }


def log_outbound_sms_to_ghl(
    contact_id: str,
    message: str,
    access_token: str,
    location_id: str,
    from_number: str = None,
    conversation_id: str = None,
) -> bool:
    """
    Log an outbound SMS (sent via Twilio) into GHL conversation history.

    Tries POST /conversations/messages/outbound first.
    Falls back to creating a contact note if the outbound endpoint
    requires a conversationProviderId or isn't available.

    Returns True if logged successfully, False otherwise.
    This is best-effort — failures are logged but never block the main flow.
    """
    if not contact_id or not access_token:
        return False

    headers = _ghl_headers(access_token)

    # -- Attempt 1: Use the outbound message endpoint --
    payload = {
        "type": "SMS",
        "contactId": contact_id,
        "message": message.strip(),
    }
    if conversation_id:
        payload["conversationId"] = conversation_id
    if from_number:
        payload["phone"] = from_number

    try:
        resp = requests.post(GHL_OUTBOUND_URL, json=payload, headers=headers, timeout=10)
        if resp.ok:
            logger.info(f"[ghl_logger] Outbound SMS logged to GHL for contact={contact_id}")
            return True
        else:
            logger.warning(
                f"[ghl_logger] Outbound SMS endpoint returned {resp.status_code} "
                f"for {contact_id}: {resp.text[:300]}"
            )
    except Exception as e:
        logger.warning(f"[ghl_logger] Outbound SMS endpoint error for {contact_id}: {e}")

    # -- Attempt 2: Fall back to a contact note --
    return _create_contact_note(
        contact_id=contact_id,
        body=f"[SMS sent via InsuranceGrokBot] {message.strip()}",
        access_token=access_token,
    )


def log_inbound_sms_to_ghl(
    contact_id: str,
    message: str,
    access_token: str,
    from_number: str = None,
) -> bool:
    """
    Log an inbound SMS (received on Twilio number) into GHL conversation.

    Uses POST /conversations/messages/inbound with contactId.
    """
    if not contact_id or not access_token:
        return False

    headers = _ghl_headers(access_token)
    payload = {
        "type": "SMS",
        "contactId": contact_id,
        "message": message.strip(),
    }
    if from_number:
        payload["phone"] = from_number

    try:
        resp = requests.post(GHL_INBOUND_URL, json=payload, headers=headers, timeout=10)
        if resp.ok:
            logger.info(f"[ghl_logger] Inbound SMS logged to GHL for contact={contact_id}")
            return True
        else:
            logger.warning(
                f"[ghl_logger] Inbound SMS log failed ({resp.status_code}) "
                f"for {contact_id}: {resp.text[:300]}"
            )
    except Exception as e:
        logger.warning(f"[ghl_logger] Inbound SMS log error for {contact_id}: {e}")

    return False


def log_call_to_ghl(
    contact_id: str,
    access_token: str,
    direction: str = "outbound",
    phone: str = None,
    status: str = "completed",
    duration: int = 0,
    from_number: str = None,
    recording_url: str = None,
) -> bool:
    """
    Log a call (made via Twilio dialer) into GHL conversation history.

    Uses POST /conversations/messages/outbound with type "Call" for outbound,
    or POST /conversations/messages/inbound with type "Call" for inbound.

    Returns True if logged successfully, False otherwise.
    """
    if not contact_id or not access_token:
        return False

    headers = _ghl_headers(access_token)

    # Build call payload
    call_data = {
        "status": _map_call_status(status),
    }
    if duration:
        call_data["duration"] = str(duration)

    payload = {
        "type": "Call",
        "contactId": contact_id,
        "call": call_data,
    }
    if phone:
        payload["phone"] = phone

    # Add recording as attachment if available
    if recording_url:
        payload["attachments"] = [recording_url]

    # Choose endpoint based on direction
    if direction.startswith("outbound"):
        url = GHL_OUTBOUND_URL
        log_dir = "outbound"
    else:
        url = GHL_INBOUND_URL
        log_dir = "inbound"

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.ok:
            logger.info(
                f"[ghl_logger] {log_dir} call logged to GHL for contact={contact_id} "
                f"(status={status}, duration={duration}s)"
            )
            return True
        else:
            logger.warning(
                f"[ghl_logger] Call log failed ({resp.status_code}) "
                f"for {contact_id}: {resp.text[:300]}"
            )
    except Exception as e:
        logger.warning(f"[ghl_logger] Call log error for {contact_id}: {e}")

    # Fall back to a note
    duration_str = f"{duration // 60}m {duration % 60}s" if duration else "unknown"
    note_body = (
        f"[{log_dir.title()} call via InsuranceGrokBot] "
        f"Status: {status}, Duration: {duration_str}"
    )
    if phone:
        note_body += f", Phone: {phone}"
    return _create_contact_note(
        contact_id=contact_id,
        body=note_body,
        access_token=access_token,
    )


def _map_call_status(status: str) -> str:
    """Map internal call status to GHL-accepted call status values."""
    mapping = {
        "completed": "Completed",
        "busy": "Busy",
        "no-answer": "No-answer",
        "failed": "Failed",
        "canceled": "Canceled",
        "initiated": "Pending",
        "ringing": "Pending",
        "in-progress": "Answered",
        "voicemail": "Voicemail",
    }
    return mapping.get(status.lower(), "Completed")


def _create_contact_note(contact_id: str, body: str, access_token: str) -> bool:
    """
    Fallback: create a note on the GHL contact.
    Shows in CRM activity feed (not conversation thread).
    """
    url = GHL_NOTES_URL.format(contact_id=contact_id)
    headers = _ghl_headers(access_token)
    payload = {"body": body}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.ok:
            logger.info(f"[ghl_logger] Note created on contact={contact_id} (fallback)")
            return True
        else:
            logger.warning(
                f"[ghl_logger] Note creation failed ({resp.status_code}) "
                f"for {contact_id}: {resp.text[:300]}"
            )
    except Exception as e:
        logger.warning(f"[ghl_logger] Note creation error for {contact_id}: {e}")

    return False
