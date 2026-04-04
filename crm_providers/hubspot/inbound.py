# crm_providers/hubspot/inbound.py — HubSpot Inbound Webhook Handler
#
# Receives batched webhook events from HubSpot, verifies signatures,
# normalizes to canonical payload format, and queues to RQ for processing.
#
# HubSpot webhook specifics:
#   - Events arrive as batched arrays (multiple events per request)
#   - Signature: HMAC-SHA256 v3 (client_secret, method + url + body + timestamp)
#   - Events only contain objectId + propertyName — NOT full contact/message
#   - Must fetch full contact data before queueing

import hashlib
import hmac
import json
import logging
import os
import time

import requests
from flask import Blueprint, request, jsonify

from db import get_db_connection, return_db_connection, get_subscriber_info_hybrid

logger = logging.getLogger(__name__)

hubspot_webhook_bp = Blueprint("hubspot_webhook", __name__)

HUBSPOT_BASE = "https://api.hubapi.com"

# HubSpot event types we care about → IGB canonical event types
HUBSPOT_EVENT_MAP = {
    "contact.creation": "ContactCreate",
    "contact.propertyChange": "ContactUpdate",
    "contact.deletion": "ContactDelete",
    "deal.creation": "DealCreate",
    "deal.propertyChange": "DealUpdate",
    "conversation.creation": "ConversationCreate",
}

# Properties that indicate SMS activity (triggers AI pipeline)
SMS_PROPERTIES = {
    "hs_latest_sms_message",
    "hs_sms_last_message_received_date",
    "notes_last_updated",
    "phone",                          # phone number changes
    "hs_lead_status",                 # lead status changes
    "lifecyclestage",                 # lifecycle stage changes
    "hs_latest_meeting_activity",     # meeting activity
}


def _get_client_secret():
    return os.getenv("HUBSPOT_CLIENT_SECRET", "")


def verify_hubspot_signature_v3(request_body: bytes, signature: str,
                                  timestamp: str, method: str = "POST",
                                  url: str = "") -> bool:
    """
    Verify HubSpot v3 webhook signature.

    HubSpot v3 uses: HMAC-SHA256(client_secret, method + url + body + timestamp)
    The signature is sent in X-HubSpot-Signature-v3 header.
    """
    secret = _get_client_secret()
    if not secret:
        logger.warning("HUBSPOT_CLIENT_SECRET not set — skipping signature verification")
        return True

    if not signature or not timestamp:
        return False

    # Reject timestamps older than 5 minutes (replay protection)
    try:
        ts = int(timestamp)
        if abs(time.time() - ts / 1000) > 300:
            logger.warning("HubSpot webhook timestamp too old")
            return False
    except (ValueError, TypeError):
        return False

    # Build the signature base string
    body_str = request_body.decode("utf-8") if isinstance(request_body, bytes) else request_body
    source_string = f"{method}{url}{body_str}{timestamp}"

    expected = hmac.new(
        secret.encode("utf-8"),
        source_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


def _find_subscriber_by_hub_id(hub_id: str) -> dict:
    """Find a subscriber by their HubSpot portal ID stored in crm_config."""
    if not hub_id:
        return {}

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM subscribers
            WHERE crm_type = 'hubspot'
              AND crm_config->>'hub_id' = %s
            LIMIT 1
        """, (str(hub_id),))
        row = cur.fetchone()
        return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Failed to find subscriber by hub_id {hub_id}: {e}")
        return {}
    finally:
        return_db_connection(conn)


def _fetch_hubspot_contact(contact_id: str, access_token: str) -> dict:
    """
    Fetch full contact details from HubSpot API.

    HubSpot webhooks only send objectId — we need to fetch the actual
    contact data (name, phone, email) before we can process the event.
    """
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    params = {
        "properties": "firstname,lastname,email,phone,hs_lead_status,"
                      "lifecyclestage,company,address,city,state,zip,"
                      "hs_latest_sms_message,hs_sms_last_message_received_date",
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            props = data.get("properties", {})
            return {
                "id": str(data.get("id", contact_id)),
                "firstName": props.get("firstname", ""),
                "lastName": props.get("lastname", ""),
                "email": props.get("email", ""),
                "phone": props.get("phone", ""),
                "company": props.get("company", ""),
                "address": props.get("address", ""),
                "city": props.get("city", ""),
                "state": props.get("state", ""),
                "zip": props.get("zip", ""),
                "lifecyclestage": props.get("lifecyclestage", ""),
                "hs_lead_status": props.get("hs_lead_status", ""),
            }
        elif resp.status_code == 404:
            logger.warning(f"HubSpot contact {contact_id} not found")
        else:
            logger.error(f"HubSpot contact fetch failed: {resp.status_code}")
    except requests.RequestException as e:
        logger.error(f"HubSpot contact fetch network error: {e}")

    return {"id": str(contact_id)}


def _fetch_latest_sms(contact_id: str, access_token: str) -> str:
    """
    Fetch the most recent SMS/communication for a HubSpot contact.

    Uses the Communications API to find the latest SMS message body.
    """
    url = f"{HUBSPOT_BASE}/crm/v3/objects/communications/search"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "associations.contact",
                "operator": "EQ",
                "value": str(contact_id),
            }]
        }],
        "properties": [
            "hs_communication_body",
            "hs_communication_channel_type",
            "hs_timestamp",
        ],
        "sorts": [{"propertyName": "hs_timestamp", "direction": "DESCENDING"}],
        "limit": 1,
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            if results:
                props = results[0].get("properties", {})
                if props.get("hs_communication_channel_type") == "SMS":
                    return props.get("hs_communication_body", "")
    except Exception as e:
        logger.error(f"HubSpot SMS fetch error: {e}")

    return ""


def normalize_hubspot_event(event: dict, subscriber: dict) -> dict:
    """
    Normalize a single HubSpot webhook event into the canonical payload
    format expected by process_webhook_task().
    """
    subscription_type = event.get("subscriptionType", "")
    object_id = str(event.get("objectId", ""))
    property_name = event.get("propertyName", "")
    property_value = event.get("propertyValue", "")
    portal_id = str(event.get("portalId", ""))

    location_id = subscriber.get("location_id", "")
    crm_config = subscriber.get("crm_config") or {}
    access_token = crm_config.get("access_token", "")

    # Map HubSpot event to IGB event type
    event_type = HUBSPOT_EVENT_MAP.get(subscription_type, "")
    if not event_type:
        return {}

    # Fetch full contact data (HubSpot webhooks only send objectId)
    contact = _fetch_hubspot_contact(object_id, access_token)

    # For property change events on SMS fields, fetch the message body
    message_body = ""
    if subscription_type == "contact.propertyChange" and property_name in SMS_PROPERTIES:
        message_body = _fetch_latest_sms(object_id, access_token)

    # Build canonical payload
    payload = {
        "contactId": object_id,
        "contact_id": object_id,
        "locationId": location_id,
        "location_id": location_id,
        "first_name": contact.get("firstName", ""),
        "last_name": contact.get("lastName", ""),
        "phone": contact.get("phone", ""),
        "email": contact.get("email", ""),
        "message": message_body,
        "body": message_body,
        "event_type": event_type,
        "_crm_source": "hubspot",
        "_hubspot_event": subscription_type,
        "_hubspot_property": property_name,
        "_hubspot_portal_id": portal_id,
    }

    return payload


@hubspot_webhook_bp.route("/hubspot/webhook", methods=["POST"])
def hubspot_webhook():
    """
    Receive batched webhook events from HubSpot.

    HubSpot sends events as a JSON array. Each event contains:
        subscriptionType: "contact.creation", "contact.propertyChange", etc.
        objectId: HubSpot record ID
        propertyName: Changed property name (for propertyChange events)
        propertyValue: New property value
        portalId: HubSpot account ID
    """
    # Signature verification
    raw_body = request.get_data()
    signature = request.headers.get("X-HubSpot-Signature-v3", "")
    timestamp = request.headers.get("X-HubSpot-Request-Timestamp", "")
    request_url = request.url

    if not verify_hubspot_signature_v3(
        raw_body, signature, timestamp,
        method=request.method, url=request_url
    ):
        logger.warning("HubSpot webhook signature verification failed")
        return jsonify({"error": "Invalid signature"}), 401

    # Parse batched events
    try:
        events = request.get_json(force=True)
        if not isinstance(events, list):
            events = [events]
    except Exception as e:
        logger.error(f"HubSpot webhook parse error: {e}")
        return jsonify({"error": "Invalid payload"}), 400

    if not events:
        return jsonify({"status": "ok", "processed": 0}), 200

    # Identify the subscriber from the portal ID
    portal_id = str(events[0].get("portalId", ""))
    subscriber = _find_subscriber_by_hub_id(portal_id)
    if not subscriber:
        logger.warning(f"HubSpot webhook: no subscriber for portal {portal_id}")
        return jsonify({"error": "Unknown portal"}), 404

    # Process each event
    queued = 0
    for event in events:
        try:
            payload = normalize_hubspot_event(event, subscriber)
            if not payload:
                continue

            # Skip events without a contact ID
            contact_id = payload.get("contact_id", "")
            if not contact_id:
                continue

            # Queue to RQ for async processing
            _queue_webhook_task(payload)
            queued += 1

        except Exception as e:
            logger.error(f"HubSpot webhook event processing error: {e}", exc_info=True)

    logger.info(f"HubSpot webhook: processed {queued}/{len(events)} events "
                f"for portal {portal_id}")
    return jsonify({"status": "ok", "processed": queued}), 200


def _queue_webhook_task(payload: dict):
    """Queue a normalized HubSpot event for async processing via RQ."""
    try:
        from extensions import ensure_redis, q_production

        ensure_redis()
        q_production.enqueue(
            "tasks.process_webhook_task",
            payload,
            job_timeout=120,
            result_ttl=86400,
        )
    except Exception as e:
        logger.error(f"Failed to queue HubSpot webhook task: {e}")
        # Store for later recovery
        from db import save_failed_webhook_payload
        save_failed_webhook_payload(
            payload.get("location_id", ""),
            payload.get("contact_id", ""),
            payload,
            f"queue_error: {e}",
        )
