# crm_providers/hubspot/crm_card.py — HubSpot CRM Card Endpoint
#
# Data fetch URL called by HubSpot when an agent views a contact in the CRM.
# Returns AI intelligence from the contact_intelligence cache: temperature,
# score, summary, recommended actions — zero AI cost (reads from cache only).
#
# HubSpot CRM Card specifics:
#   - HubSpot calls our URL with ?associatedObjectId=<contact_id>&associatedObjectType=CONTACT
#   - We must return JSON matching the HubSpot CRM Card data format
#   - Response includes "results" array of card sections and optional "primaryAction"
#   - Auth: HubSpot sends the request with our app's secret in X-HubSpot-Signature

import logging
import os

from flask import Blueprint, request, jsonify

from db import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)

hubspot_card_bp = Blueprint("hubspot_card", __name__)


def _verify_card_request():
    """
    Verify CRM Card request originated from HubSpot.

    HubSpot sends X-HubSpot-Signature header. For CRM cards, we also
    accept requests with a valid app_secret query parameter (simpler
    for development/testing).
    """
    app_secret = os.getenv("HUBSPOT_CLIENT_SECRET", "")
    if not app_secret:
        return True  # No secret configured — allow (dev mode)

    # Check query param auth (used in CRM card data fetch URLs)
    req_secret = request.args.get("app_secret", "")
    if req_secret and req_secret == app_secret:
        return True

    # Check HubSpot signature header
    signature = request.headers.get("X-HubSpot-Signature", "")
    if signature:
        import hashlib
        source = app_secret + request.method + request.url + (request.get_data(as_text=True) or "")
        expected = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return expected == signature

    return False


def _get_intelligence(contact_id, location_id):
    """
    Read AI intelligence from contact_intelligence cache.
    Zero AI cost — reads from cache only.
    """
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT analysis, analyzed_at
            FROM contact_intelligence
            WHERE contact_id = %s AND location_id = %s
        """, (contact_id, location_id))
        row = cur.fetchone()
        if row and row.get("analysis"):
            analysis = row["analysis"]
            if isinstance(analysis, str):
                import json
                analysis = json.loads(analysis)
            analysis["analyzed_at"] = str(row.get("analyzed_at", ""))
            return analysis
        return None
    except Exception as e:
        logger.error(f"CRM card intelligence fetch error: {e}")
        return None
    finally:
        return_db_connection(conn)


def _find_subscriber_by_hub_id(hub_id):
    """Find subscriber by HubSpot portal ID."""
    if not hub_id:
        return None
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT location_id, crm_config FROM subscribers
            WHERE crm_type = 'hubspot' AND crm_config->>'hub_id' = %s
            LIMIT 1
        """, (str(hub_id),))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"CRM card subscriber lookup error: {e}")
        return None
    finally:
        return_db_connection(conn)


def _build_card_response(intelligence, contact_id, domain):
    """
    Build HubSpot CRM Card response in the required format.

    HubSpot CRM Card v3 format:
    {
        "results": [
            {
                "objectId": 123,
                "title": "...",
                "properties": [{"label": "...", "dataType": "STRING", "value": "..."}],
                "actions": [{"type": "IFRAME", "width": 890, "height": 748, ...}]
            }
        ],
        "primaryAction": {...}
    }
    """
    if not intelligence:
        return {
            "results": [{
                "objectId": int(contact_id) if contact_id.isdigit() else 0,
                "title": "IGB Intelligence",
                "properties": [{
                    "label": "Status",
                    "dataType": "STRING",
                    "value": "No analysis available yet",
                }],
            }],
        }

    temperature = intelligence.get("temperature", "unknown")
    score = intelligence.get("score", 0)
    summary = intelligence.get("summary", "No summary available")
    actions_list = intelligence.get("actions", [])
    should_respond = intelligence.get("should_respond", False)
    engagement = intelligence.get("engagement_level", 0)

    # Temperature display
    temp_display = {
        "hot": "Hot Lead",
        "warm": "Warm Lead",
        "cool": "Cool Lead",
        "cold": "Cold Lead",
    }.get(temperature, temperature.title() if temperature else "Unknown")

    properties = [
        {"label": "Temperature", "dataType": "STRING", "value": temp_display},
        {"label": "Score", "dataType": "NUMERIC", "value": str(score)},
        {"label": "Summary", "dataType": "STRING", "value": summary[:500]},
        {"label": "Engagement", "dataType": "NUMERIC", "value": str(engagement)},
    ]

    if should_respond:
        properties.append({
            "label": "Action Needed",
            "dataType": "STRING",
            "value": intelligence.get("should_respond_reason", "Agent response needed"),
        })

    # Add top 2 recommended actions
    for i, action in enumerate(actions_list[:2]):
        action_text = action.get("text", "") if isinstance(action, dict) else str(action)
        if action_text:
            properties.append({
                "label": f"Next Step {i + 1}",
                "dataType": "STRING",
                "value": action_text[:200],
            })

    card_actions = []
    if domain:
        card_actions.append({
            "type": "IFRAME",
            "width": 890,
            "height": 748,
            "uri": f"{domain}/embed/intelligence/{contact_id}",
            "label": "Full AI Intelligence",
        })
        card_actions.append({
            "type": "IFRAME",
            "width": 890,
            "height": 748,
            "uri": f"{domain}/embed/dialer?contact={contact_id}",
            "label": "Open Dialer",
        })

    result = {
        "objectId": int(contact_id) if contact_id.isdigit() else 0,
        "title": f"IGB: {temp_display} (Score: {score})",
        "properties": properties,
    }
    if card_actions:
        result["actions"] = card_actions

    return {"results": [result]}


@hubspot_card_bp.route("/hubspot/crm-card", methods=["GET"])
def hubspot_crm_card():
    """
    HubSpot CRM Card data fetch endpoint.

    Called by HubSpot when an agent views a contact. Returns AI intelligence
    from cache — zero AI cost per view.

    Query params (set by HubSpot):
        associatedObjectId: HubSpot contact ID
        associatedObjectType: "CONTACT" (or "COMPANY", "DEAL")
        portalId: HubSpot portal ID
    """
    if not _verify_card_request():
        return jsonify({"message": "Unauthorized"}), 401

    contact_id = request.args.get("associatedObjectId", "")
    portal_id = request.args.get("portalId", "")

    if not contact_id:
        return jsonify({"results": []}), 200

    # Find the subscriber by portal ID
    subscriber = _find_subscriber_by_hub_id(portal_id)
    if not subscriber:
        return jsonify({"results": [{
            "objectId": int(contact_id) if contact_id.isdigit() else 0,
            "title": "IGB Not Connected",
            "properties": [{
                "label": "Status",
                "dataType": "STRING",
                "value": "Portal not linked to InsuranceGrokBot",
            }],
        }]}), 200

    location_id = subscriber.get("location_id", "")
    intelligence = _get_intelligence(contact_id, location_id)
    domain = os.getenv("YOUR_DOMAIN", "").rstrip("/")

    response = _build_card_response(intelligence, contact_id, domain)
    return jsonify(response), 200


@hubspot_card_bp.route("/hubspot/crm-card/health", methods=["GET"])
def hubspot_crm_card_health():
    """Health check for HubSpot CRM Card — used by HubSpot app validation."""
    return jsonify({"status": "ok", "service": "igb-crm-card"}), 200
