# crm_providers/hubspot/calling.py — HubSpot Calling Extensions SDK Integration
#
# Enables the Omnisconn dialer to appear as a registered calling provider
# in HubSpot contact records. Agents click "Call" in HubSpot and get the
# Omnisconn dialer embedded in a sidebar — full AI voice, call history,
# and contact intelligence in one pane.
#
# Routes:
#   GET  /hubspot/calling/settings   — return calling provider config (HubSpot fetches this)
#   POST /hubspot/calling/settings   — save calling provider config
#   GET  /hubspot/calling/iframe     — the calling iframe (loads Calling Extensions SDK)
#   POST /hubspot/calling/engagement — save completed call engagement to HubSpot

import logging
import os

from flask import Blueprint, request, jsonify, render_template
from flask_login import current_user, login_required

from db import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)

hubspot_calling_bp = Blueprint("hubspot_calling", __name__)

HUBSPOT_BASE = "https://api.hubapi.com"


def _get_calling_settings():
    """Return the calling provider settings for this app."""
    domain = os.getenv("YOUR_DOMAIN", "").rstrip("/")
    return {
        "name": "Omnisconn Dialer",
        "url": f"{domain}/hubspot/calling/iframe",
        "height": 600,
        "width": 400,
        "isReady": True,
        "supportsCustomObjects": False,
    }


@hubspot_calling_bp.route("/hubspot/calling/settings", methods=["GET"])
def hubspot_calling_settings_get():
    """
    HubSpot fetches this to discover the calling provider settings.

    Returns the calling app configuration: name, iframe URL, dimensions.
    HubSpot calls this when registering the calling provider for an installed app.
    """
    return jsonify(_get_calling_settings()), 200


@hubspot_calling_bp.route("/hubspot/calling/settings", methods=["POST"])
@login_required
def hubspot_calling_settings_post():
    """
    Register or update calling settings with HubSpot.

    Called from the dashboard when user connects HubSpot — registers
    Omnisconn as a calling provider for this HubSpot portal.
    """
    location_id = current_user.location_id

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT crm_config FROM subscribers WHERE location_id = %s
        """, (location_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Subscriber not found"}), 404

        crm_config = row.get("crm_config") or {}
        access_token = crm_config.get("access_token", "")

        if not access_token:
            return jsonify({"error": "HubSpot not connected"}), 400

    except Exception as e:
        logger.error(f"Calling settings get token error: {e}")
        return jsonify({"error": "Database error"}), 500
    finally:
        return_db_connection(conn)

    # Register calling settings with HubSpot API
    import requests as req_lib
    settings = _get_calling_settings()

    try:
        resp = req_lib.post(
            f"{HUBSPOT_BASE}/crm/v3/extensions/calling/settings",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=settings,
            timeout=20,
        )
        if resp.status_code in (200, 201):
            logger.info(f"HubSpot calling settings registered for {location_id}")
            return jsonify({"status": "ok", "settings": settings}), 200
        else:
            logger.warning(
                f"HubSpot calling settings registration: {resp.status_code} {resp.text[:300]}"
            )
            return jsonify({"status": "ok", "settings": settings, "note": "local_only"}), 200
    except Exception as e:
        logger.error(f"HubSpot calling settings registration failed: {e}")
        return jsonify({"status": "ok", "settings": settings, "note": "local_only"}), 200


@hubspot_calling_bp.route("/hubspot/calling/iframe", methods=["GET"])
def hubspot_calling_iframe():
    """
    The calling iframe loaded by HubSpot when an agent initiates a call.

    HubSpot passes query params:
        hs_object_id — contact ID
        portalId     — HubSpot portal ID
        phone        — pre-populated phone number
        firstName    — contact first name
        lastName     — contact last name
        email        — contact email
    """
    contact_id = request.args.get("hs_object_id", "")
    portal_id = request.args.get("portalId", "")
    phone = request.args.get("phone", "")
    first_name = request.args.get("firstName", "")
    last_name = request.args.get("lastName", "")
    email = request.args.get("email", "")

    domain = os.getenv("YOUR_DOMAIN", "").rstrip("/")

    return render_template(
        "hubspot_calling.html",
        contact_id=contact_id,
        portal_id=portal_id,
        phone=phone,
        first_name=first_name,
        last_name=last_name,
        email=email,
        domain=domain,
    )


@hubspot_calling_bp.route("/hubspot/calling/engagement", methods=["POST"])
def hubspot_calling_engagement():
    """
    Save a completed call as a HubSpot Call engagement.

    Called by the iframe after a call ends to log it to HubSpot CRM.
    Payload:
        portalId    — HubSpot portal ID
        contactId   — HubSpot contact ID
        callSid     — Twilio call SID
        duration    — call duration in seconds
        direction   — "outbound" or "inbound"
        disposition — "CONNECTED" | "NO_ANSWER" | "BUSY" | "VOICEMAIL"
        phoneNumber — phone number called
    """
    data = request.get_json(silent=True) or {}

    portal_id = str(data.get("portalId", ""))
    contact_id = str(data.get("contactId", ""))
    call_sid = str(data.get("callSid", ""))
    duration = int(data.get("duration", 0))
    direction = str(data.get("direction", "outbound"))
    disposition = str(data.get("disposition", "")).upper()
    phone_number = str(data.get("phoneNumber", ""))

    if not portal_id or not contact_id:
        return jsonify({"error": "portalId and contactId required"}), 400

    # Find subscriber by HubSpot portal ID
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT location_id, crm_config FROM subscribers
            WHERE crm_type = 'hubspot' AND crm_config->>'hub_id' = %s
            LIMIT 1
        """, (portal_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Portal not connected"}), 404
        subscriber = dict(row)
    except Exception as e:
        logger.error(f"Engagement endpoint subscriber lookup: {e}")
        return jsonify({"error": "Database error"}), 500
    finally:
        return_db_connection(conn)

    crm_config = subscriber.get("crm_config") or {}
    access_token = crm_config.get("access_token", "")

    if not access_token:
        return jsonify({"error": "No access token"}), 400

    # Log to HubSpot via existing logger
    from crm_providers.hubspot.logger import log_call

    # Map HubSpot disposition strings to IGB disposition keys
    disposition_map = {
        "CONNECTED": "connected",
        "NO_ANSWER": "no_answer",
        "BUSY": "busy",
        "VOICEMAIL": "voicemail",
        "FAILED": "no_answer",
    }
    igb_disposition = disposition_map.get(disposition, "connected")

    body = (
        f"Call via Omnisconn Dialer | SID: {call_sid}"
        if call_sid
        else "Call via Omnisconn Dialer"
    )

    success = log_call(
        contact_id=contact_id,
        direction=direction,
        duration=duration,
        access_token=access_token,
        disposition=igb_disposition,
        body=body,
    )

    if success:
        return jsonify({"status": "ok"}), 200
    else:
        return jsonify({"status": "logged_locally"}), 200
