# ghl_logger.py - Log IGB-sent messages and calls into GHL conversations
#
# Uses GHL Conversation Provider API to sync messages/calls from
# Omnisconn back to GoHighLevel so the CRM stays the source of truth.
#
# Two registered Conversation Providers (GHL Marketplace App):
#   - Omnisconn SMS (Custom SMS) → logs SMS sent via Twilio
#   - Omnisconn (Call)           → logs calls made via dialer
#
# API Endpoints:
#   POST /conversations/messages/inbound   — log inbound messages/calls (contact → agent)
#   POST /conversations/messages/outbound  — log outbound messages/calls (agent → contact)
#
# Ref: https://marketplace.gohighlevel.com/docs/marketplace-modules/ConversationProviders/

import os
import logging
import requests

logger = logging.getLogger(__name__)

GHL_BASE = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com")
GHL_INBOUND_URL = f"{GHL_BASE}/conversations/messages/inbound"
GHL_OUTBOUND_URL = f"{GHL_BASE}/conversations/messages/outbound"

# Conversation Provider IDs from the GHL Marketplace App
# These are app-level (same for all subscribers)
GHL_SMS_PROVIDER_ID = os.getenv(
    "GHL_SMS_CONVERSATION_PROVIDER_ID",
    "699c84aef36d66cc10a56e82",  # Omnisconn SMS (Custom SMS)
)
GHL_CALL_PROVIDER_ID = os.getenv(
    "GHL_CALL_CONVERSATION_PROVIDER_ID",
    "699c83535fc465bbff87a78d",  # Omnisconn (Call)
)


def _ghl_headers(access_token: str) -> dict:
    """Build auth headers for GHL API v2 calls."""
    return {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }


# ── SMS Logging ──────────────────────────────────────────────────────────────


def log_outbound_sms_to_ghl(
    contact_id: str,
    message: str,
    access_token: str,
    location_id: str = None,
    from_number: str = None,
    contact_phone: str = None,
) -> bool:
    """
    Log an outbound SMS (sent via Twilio by bot or agent) into GHL conversation.

    Uses POST /conversations/messages/outbound with the Custom SMS provider.
    The message appears in GHL's conversation thread under the IGB SMS tab.

    Best-effort — failures are logged but never block the main flow.
    """
    if not contact_id or not access_token or not GHL_SMS_PROVIDER_ID:
        return False

    headers = _ghl_headers(access_token)
    payload = {
        "type": "Custom",
        "contactId": contact_id,
        "conversationProviderId": GHL_SMS_PROVIDER_ID,
        "message": message.strip(),
    }
    if contact_phone:
        payload["phone"] = contact_phone

    try:
        resp = requests.post(GHL_OUTBOUND_URL, json=payload, headers=headers, timeout=10)
        if resp.ok:
            logger.info(f"[ghl_logger] ✅ Outbound SMS logged to GHL for contact={contact_id}")
            return True
        else:
            logger.warning(
                f"[ghl_logger] Outbound SMS log failed ({resp.status_code}) "
                f"for {contact_id}: {resp.text[:300]}"
            )
    except Exception as e:
        logger.warning(f"[ghl_logger] Outbound SMS log error for {contact_id}: {e}")

    return False


def log_inbound_sms_to_ghl(
    contact_id: str,
    message: str,
    access_token: str,
    contact_phone: str = None,
) -> bool:
    """
    Log an inbound SMS (received on Twilio number from a contact) into GHL.

    Uses POST /conversations/messages/inbound with the Custom SMS provider.
    """
    if not contact_id or not access_token or not GHL_SMS_PROVIDER_ID:
        return False

    headers = _ghl_headers(access_token)
    payload = {
        "type": "SMS",
        "contactId": contact_id,
        "conversationProviderId": GHL_SMS_PROVIDER_ID,
        "message": message.strip(),
    }
    if contact_phone:
        payload["phone"] = contact_phone

    try:
        resp = requests.post(GHL_INBOUND_URL, json=payload, headers=headers, timeout=10)
        if resp.ok:
            logger.info(f"[ghl_logger] ✅ Inbound SMS logged to GHL for contact={contact_id}")
            return True
        else:
            logger.warning(
                f"[ghl_logger] Inbound SMS log failed ({resp.status_code}) "
                f"for {contact_id}: {resp.text[:300]}"
            )
    except Exception as e:
        logger.warning(f"[ghl_logger] Inbound SMS log error for {contact_id}: {e}")

    return False


# ── Call Logging ─────────────────────────────────────────────────────────────


def log_call_to_ghl(
    contact_id: str,
    access_token: str,
    direction: str = "outbound",
    phone: str = None,
    status: str = "completed",
    duration: int = 0,
    from_number: str = None,
    recording_url: str = None,
    contact_name: str = None,
) -> bool:
    """
    Log a call (made/received via Twilio dialer) into GHL conversation history.

    Uses the Call conversation provider with:
      - POST /conversations/messages/outbound for outbound calls
      - POST /conversations/messages/inbound  for inbound calls

    The call appears in GHL's conversation thread with status and duration.
    """
    if not contact_id or not access_token or not GHL_CALL_PROVIDER_ID:
        return False

    headers = _ghl_headers(access_token)

    # GHL Conversation Provider API requires a nested "call" object
    # with to/from/status. See: marketplace.gohighlevel.com/docs/ghl/conversations/add-an-outbound-message
    mapped_status = _map_call_status(status)
    call_obj = {}
    if phone:
        call_obj["to"] = phone
    if from_number:
        call_obj["from"] = from_number
    if mapped_status:
        call_obj["status"] = mapped_status

    # Build descriptive body for the call log entry
    dir_text = "Outbound" if direction.startswith("outbound") else "Inbound"
    dur_min = duration // 60
    dur_sec = duration % 60
    dur_text = f"{dur_min}:{dur_sec:02d}" if duration else ""
    body_parts = [f"{dir_text} call"]
    if contact_name:
        body_parts.append(f"with {contact_name}")
    if dur_text:
        body_parts.append(f"({dur_text})")
    if mapped_status and mapped_status != "completed":
        body_parts.append(f"— {mapped_status}")

    payload = {
        "type": "Call",
        "contactId": contact_id,
        "conversationProviderId": GHL_CALL_PROVIDER_ID,
        "call": call_obj,
        "body": " ".join(body_parts),
    }

    if recording_url:
        payload["attachments"] = [recording_url]

    is_outbound = direction.startswith("outbound")
    url = GHL_OUTBOUND_URL if is_outbound else GHL_INBOUND_URL
    dir_label = "outbound" if is_outbound else "inbound"

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.ok:
            logger.info(
                f"[ghl_logger] ✅ {dir_label} call logged to GHL for contact={contact_id} "
                f"(status={status}, duration={duration}s)"
            )
            return True
        else:
            logger.warning(
                f"[ghl_logger] {dir_label} call log failed ({resp.status_code}) "
                f"for {contact_id}: {resp.text[:300]}"
            )
    except Exception as e:
        logger.warning(f"[ghl_logger] {dir_label} call log error for {contact_id}: {e}")

    return False


def _map_call_status(status: str) -> str:
    """Map internal call status to GHL-accepted call status values.
    GHL valid: pending, completed, answered, busy, no-answer, failed, canceled, voicemail."""
    mapping = {
        "completed": "completed",
        "busy": "busy",
        "no-answer": "no-answer",
        "failed": "failed",
        "canceled": "canceled",
        "initiated": "pending",
        "ringing": "pending",
        "in-progress": "answered",
        "voicemail": "voicemail",
    }
    return mapping.get(status.lower(), "completed")
