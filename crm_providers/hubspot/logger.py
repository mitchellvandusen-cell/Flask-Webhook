# crm_providers/hubspot/logger.py — HubSpot Activity Logger
#
# Logs IGB activity (SMS, calls, notes) back into HubSpot so agents see
# everything in their CRM timeline. Uses the CRM v3 Objects API to create
# Communication, Call, and Note objects linked to contacts via associations.

import logging
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

HUBSPOT_BASE = "https://api.hubapi.com"
HUBSPOT_TIMEOUT = 20

# HubSpot-defined association type IDs
ASSOC_COMMUNICATION_TO_CONTACT = 81
ASSOC_CALL_TO_CONTACT = 194
ASSOC_NOTE_TO_CONTACT = 202


def _hs_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


def _make_association(contact_id, assoc_type_id):
    """Build a HubSpot v3 association object linking to a contact."""
    return [{
        "to": {"id": str(contact_id)},
        "types": [{
            "associationCategory": "HUBSPOT_DEFINED",
            "associationTypeId": assoc_type_id,
        }]
    }]


def log_outbound_sms(contact_id, message, access_token, **kwargs):
    """
    Log an outbound SMS as a HubSpot Communication record.

    Creates a Communication object with channel_type=SMS and links it
    to the specified contact via association.

    Args:
        contact_id: HubSpot contact ID
        message: SMS message body
        access_token: HubSpot API token
        **kwargs: Additional metadata (location_id, etc.)

    Returns:
        True if successfully logged, False otherwise.
    """
    if not contact_id or not message or not access_token:
        return False

    now_ms = str(int(datetime.utcnow().timestamp() * 1000))

    payload = {
        "properties": {
            "hs_communication_channel_type": "SMS",
            "hs_communication_body": message[:65536],
            "hs_communication_logged_from": "CRM",
            "hs_timestamp": now_ms,
        },
        "associations": _make_association(contact_id, ASSOC_COMMUNICATION_TO_CONTACT),
    }

    url = f"{HUBSPOT_BASE}/crm/v3/objects/communications"
    try:
        resp = requests.post(url, headers=_hs_headers(access_token),
                           json=payload, timeout=HUBSPOT_TIMEOUT)
        if resp.status_code in (200, 201):
            logger.info(f"HubSpot: SMS logged for contact {contact_id}")
            return True

        # Auto-retry on 401 if caller provides refresh capability
        if resp.status_code == 401:
            logger.warning(f"HubSpot SMS log auth error — token may be expired")
            return False

        logger.error(f"HubSpot SMS log failed: {resp.status_code} {resp.text[:300]}")
        return False

    except requests.RequestException as e:
        logger.error(f"HubSpot SMS log network error: {e}")
        return False


def log_inbound_sms(contact_id, message, access_token, **kwargs):
    """Log an inbound SMS as a HubSpot Communication record."""
    if not contact_id or not message or not access_token:
        return False

    now_ms = str(int(datetime.utcnow().timestamp() * 1000))

    payload = {
        "properties": {
            "hs_communication_channel_type": "SMS",
            "hs_communication_body": message[:65536],
            "hs_communication_logged_from": "INTEGRATION",
            "hs_timestamp": now_ms,
        },
        "associations": _make_association(contact_id, ASSOC_COMMUNICATION_TO_CONTACT),
    }

    url = f"{HUBSPOT_BASE}/crm/v3/objects/communications"
    try:
        resp = requests.post(url, headers=_hs_headers(access_token),
                           json=payload, timeout=HUBSPOT_TIMEOUT)
        if resp.status_code in (200, 201):
            logger.info(f"HubSpot: Inbound SMS logged for contact {contact_id}")
            return True
        logger.error(f"HubSpot inbound SMS log failed: {resp.status_code}")
        return False
    except requests.RequestException as e:
        logger.error(f"HubSpot inbound SMS log error: {e}")
        return False


def log_call(contact_id, direction, duration, access_token, **kwargs):
    """
    Log a call as a HubSpot Call engagement.

    Args:
        contact_id: HubSpot contact ID
        direction: "inbound" or "outbound"
        duration: Call duration in seconds
        access_token: HubSpot API token
        **kwargs: recording_url, disposition, call_sid, etc.

    Returns:
        True if successfully logged.
    """
    if not contact_id or not access_token:
        return False

    now_ms = str(int(datetime.utcnow().timestamp() * 1000))
    duration_ms = str(int(duration * 1000)) if duration else "0"

    # Map direction to HubSpot call direction
    hs_direction = "OUTBOUND" if direction == "outbound" else "INBOUND"

    # Map disposition
    disposition = kwargs.get("disposition", "")
    hs_disposition = _map_call_disposition(disposition)

    properties = {
        "hs_timestamp": now_ms,
        "hs_call_title": f"{'Outbound' if direction == 'outbound' else 'Inbound'} Call via Omnisconn",
        "hs_call_direction": hs_direction,
        "hs_call_duration": duration_ms,
        "hs_call_status": "COMPLETED",
        "hs_call_disposition": hs_disposition,
    }

    # Add recording URL if available
    recording_url = kwargs.get("recording_url", "")
    if recording_url:
        properties["hs_call_recording_url"] = recording_url

    # Add call body/notes
    body = kwargs.get("body", "")
    if body:
        properties["hs_call_body"] = body[:65536]

    payload = {
        "properties": properties,
        "associations": _make_association(contact_id, ASSOC_CALL_TO_CONTACT),
    }

    url = f"{HUBSPOT_BASE}/crm/v3/objects/calls"
    try:
        resp = requests.post(url, headers=_hs_headers(access_token),
                           json=payload, timeout=HUBSPOT_TIMEOUT)
        if resp.status_code in (200, 201):
            logger.info(f"HubSpot: Call logged for contact {contact_id} "
                       f"({direction}, {duration}s)")
            return True
        logger.error(f"HubSpot call log failed: {resp.status_code} {resp.text[:300]}")
        return False
    except requests.RequestException as e:
        logger.error(f"HubSpot call log error: {e}")
        return False


def log_note(contact_id, note, access_token, **kwargs):
    """
    Log a note on a HubSpot contact.

    Used for AI intelligence summaries, conversation analysis, etc.
    """
    if not contact_id or not note or not access_token:
        return False

    now_ms = str(int(datetime.utcnow().timestamp() * 1000))

    payload = {
        "properties": {
            "hs_timestamp": now_ms,
            "hs_note_body": note[:65536],
        },
        "associations": _make_association(contact_id, ASSOC_NOTE_TO_CONTACT),
    }

    url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
    try:
        resp = requests.post(url, headers=_hs_headers(access_token),
                           json=payload, timeout=HUBSPOT_TIMEOUT)
        if resp.status_code in (200, 201):
            logger.info(f"HubSpot: Note logged for contact {contact_id}")
            return True
        logger.error(f"HubSpot note log failed: {resp.status_code}")
        return False
    except requests.RequestException as e:
        logger.error(f"HubSpot note log error: {e}")
        return False


def _map_call_disposition(disposition):
    """Map IGB call disposition to HubSpot call disposition."""
    mapping = {
        "connected": "f240bbac-87c9-4f6e-bf70-924b57d47db7",  # Connected
        "no_answer": "9d9162e7-6cf3-4944-bf63-4dff82258764",  # No answer
        "busy": "a4c4c377-d246-4b32-a13b-75a56a4cd0ff",       # Busy
        "voicemail": "b2cf5968-551e-4856-9783-52b3da59a7d0",   # Left voicemail
        "wrong_number": "73a0d17f-1163-4015-bdd5-ec830791da20", # Wrong number
        "": "f240bbac-87c9-4f6e-bf70-924b57d47db7",           # Default: Connected
    }
    return mapping.get(disposition, mapping[""])
