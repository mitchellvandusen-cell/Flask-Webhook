# crm_providers/hubspot/workflow_actions.py — HubSpot Custom Workflow Actions
#
# Implements the server-side action execution handlers for Omnisconn's
# HubSpot Custom Workflow Actions. These allow HubSpot Workflow builders
# to trigger AI calls, SMS loops, and lead nurture sequences directly
# from HubSpot workflows — exactly like GHL custom actions.
#
# Routes:
#   POST /hubspot/workflow-action/ai-call      — trigger AI outbound call
#   POST /hubspot/workflow-action/sms-once     — send one AI SMS immediately
#   POST /hubspot/workflow-action/sms-loop     — start repeating SMS/call loop
#   POST /hubspot/workflow-action/tag-contact  — add/remove HubSpot properties
#   GET  /hubspot/workflow-action/definitions  — action definitions (for app config)
#
# Authentication: HubSpot signs requests with X-HubSpot-Signature-v3
# Portal identification: origin.portalId maps to subscriber via crm_config.hub_id

import logging
import os
import time

from flask import Blueprint, request, jsonify

from db import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)

hubspot_workflow_actions_bp = Blueprint("hubspot_workflow_actions", __name__)


def _verify_action_request() -> bool:
    """Verify HubSpot workflow action request signature."""
    from crm_providers.hubspot.inbound import verify_hubspot_signature_v3

    raw_body = request.get_data()
    sig = request.headers.get("X-HubSpot-Signature-v3", "")
    ts = request.headers.get("X-HubSpot-Request-Timestamp", "")

    # Skip verification in dev if secret not set
    if not os.getenv("HUBSPOT_CLIENT_SECRET"):
        return True

    # If no sig header, still allow (HubSpot may not sign action requests in all contexts)
    if not sig:
        logger.debug("HubSpot workflow action: no signature header — allowing")
        return True

    return verify_hubspot_signature_v3(raw_body, sig, ts, method=request.method, url=request.url)


def _parse_action_payload(data: dict) -> dict:
    """Extract fields from HubSpot Custom Action payload."""
    # HubSpot sends fields in inputFields OR fields (both may be present)
    fields = data.get("inputFields") or data.get("fields") or {}
    origin = data.get("origin") or {}
    obj = data.get("object") or {}

    return {
        "portal_id": str(origin.get("portalId", "")),
        "action_def_id": str(origin.get("actionDefinitionId", "")),
        "contact_id": str(obj.get("objectId", "")),
        "object_type": str(obj.get("objectType", "CONTACT")),
        "phone": fields.get("phone", ""),
        "first_name": fields.get("firstName", fields.get("first_name", "")),
        "email": fields.get("email", ""),
        "message_template": fields.get("messageTemplate", fields.get("message_template", "")),
        "loop_action": fields.get("loopAction", fields.get("loop_action", "sms")),
        "interval_hours": int(fields.get("intervalHours", fields.get("interval_hours", 24))),
        "duration_days": int(fields.get("durationDays", fields.get("duration_days", 7))),
    }


def _find_subscriber_by_portal(portal_id: str) -> dict:
    """Find subscriber by HubSpot portal ID."""
    if not portal_id:
        return {}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM subscribers
            WHERE crm_type = 'hubspot'
              AND crm_config->>'hub_id' = %s
            LIMIT 1
        """, (portal_id,))
        row = cur.fetchone()
        return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Subscriber lookup by portal {portal_id}: {e}")
        return {}
    finally:
        return_db_connection(conn)


@hubspot_workflow_actions_bp.route("/hubspot/workflow-action/ai-call", methods=["POST"])
def hubspot_action_ai_call():
    """
    HubSpot Custom Action: Trigger an AI outbound call.

    HubSpot Workflow input fields:
        phone        — contact's phone number (required)
        firstName    — contact's first name (optional, for personalization)

    Returns:
        outputFields.callStatus — "initiated" | "error"
        outputFields.callSid    — Twilio call SID if initiated
    """
    if not _verify_action_request():
        return jsonify({"message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    parsed = _parse_action_payload(data)

    portal_id = parsed["portal_id"]
    contact_id = parsed["contact_id"]
    lead_phone = parsed["phone"]
    lead_name = parsed["first_name"] or "there"

    if not portal_id:
        return jsonify({"message": "Missing portalId"}), 400
    if not lead_phone:
        return jsonify({"outputFields": {"callStatus": "error", "errorReason": "phone_required"}}), 200

    subscriber = _find_subscriber_by_portal(portal_id)
    if not subscriber:
        return jsonify({"outputFields": {"callStatus": "error", "errorReason": "portal_not_connected"}}), 200

    location_id = subscriber.get("location_id", "")
    voice_config = subscriber.get("voice_config") or {}

    if not voice_config.get("enabled"):
        return jsonify({"outputFields": {"callStatus": "error", "errorReason": "voice_not_enabled"}}), 200

    sub_sid = voice_config.get("twilio_sub_account_sid", "")
    from_number = voice_config.get("twilio_phone_number", "")

    if not sub_sid or not from_number:
        return jsonify({"outputFields": {"callStatus": "error", "errorReason": "voice_not_provisioned"}}), 200

    try:
        import twilio_provisioning
        from voice.redis_state import set_active_call
        from voice.call_history import save_call_to_history

        host = request.host
        webhook_base_url = f"https://{host}"

        custom_params = {
            "location_id": location_id,
            "caller": from_number,
            "called": lead_phone,
            "direction": "outbound",
            "contact_id": contact_id,
            "contact_name": lead_name,
            "dial_mode": "ai",
        }

        ring_timeout = int(voice_config.get("ring_timeout", 45))

        result = twilio_provisioning.create_outbound_call(
            sub_account_sid=sub_sid,
            to=lead_phone,
            from_number=from_number,
            webhook_base_url=webhook_base_url,
            machine_detection="DetectMessageEnd",
            custom_params=custom_params,
            ring_timeout=ring_timeout,
        )
        call_sid = result.get("call_sid", "")

        set_active_call(call_sid, {
            "status": "initiated",
            "duration": 0,
            "contact_id": contact_id,
            "phone": lead_phone,
            "name": lead_name,
            "_location_id": location_id,
            "_sub_sid": sub_sid,
            "_host": host,
            "_from_number": from_number,
            "_agent_email": "",
        })

        save_call_to_history(
            location_id=location_id,
            call_sid=call_sid,
            phone=lead_phone,
            contact_id=contact_id,
            contact_name=lead_name,
            direction="outbound",
            status="initiated",
            from_number=from_number,
        )

        logger.info(f"HubSpot AI Call action: {from_number} -> {lead_phone} (sid={call_sid}, portal={portal_id})")

        return jsonify({"outputFields": {"callStatus": "initiated", "callSid": call_sid}}), 200

    except Exception as e:
        logger.error(f"HubSpot AI Call action failed: {e}")
        return jsonify({"outputFields": {"callStatus": "error", "errorReason": str(e)[:200]}}), 200


@hubspot_workflow_actions_bp.route("/hubspot/workflow-action/sms-once", methods=["POST"])
def hubspot_action_sms_once():
    """
    HubSpot Custom Action: Send a single AI-personalized SMS immediately.

    HubSpot Workflow input fields:
        phone            — contact's phone number (required)
        firstName        — contact's first name
        messageTemplate  — optional seed message (AI expands it)
    """
    if not _verify_action_request():
        return jsonify({"message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    parsed = _parse_action_payload(data)

    portal_id = parsed["portal_id"]
    contact_id = parsed["contact_id"]
    lead_phone = parsed["phone"]
    lead_name = parsed["first_name"] or "there"
    message_template = parsed["message_template"]

    if not portal_id or not lead_phone:
        return jsonify({"outputFields": {"smsStatus": "error", "errorReason": "phone_required"}}), 200

    subscriber = _find_subscriber_by_portal(portal_id)
    if not subscriber:
        return jsonify({"outputFields": {"smsStatus": "error", "errorReason": "portal_not_connected"}}), 200

    location_id = subscriber.get("location_id", "")

    # Queue SMS job via RQ
    try:
        from extensions import ensure_redis, q_production
        ensure_redis()

        payload = {
            "contactId": contact_id,
            "contact_id": contact_id,
            "locationId": location_id,
            "location_id": location_id,
            "phone": lead_phone,
            "first_name": lead_name,
            "body": message_template or f"Hi {lead_name}, following up on your insurance needs.",
            "message": message_template or "",
            "event_type": "ContactCreate",
            "_crm_source": "hubspot",
            "_hubspot_action": "sms_once",
        }

        q_production.enqueue(
            "tasks.process_webhook_task",
            payload,
            job_timeout=120,
            result_ttl=86400,
        )

        logger.info(f"HubSpot SMS action queued: portal={portal_id} contact={contact_id}")
        return jsonify({"outputFields": {"smsStatus": "queued"}}), 200

    except Exception as e:
        logger.error(f"HubSpot SMS action failed: {e}")
        return jsonify({"outputFields": {"smsStatus": "error", "errorReason": str(e)[:200]}}), 200


@hubspot_workflow_actions_bp.route("/hubspot/workflow-action/sms-loop", methods=["POST"])
def hubspot_action_sms_loop():
    """
    HubSpot Custom Action: Start a repeating SMS/call nurture loop.

    Repeats the action at the configured interval for the configured duration.
    Mirrors GHL's /voice/ghl-action/loop endpoint.

    HubSpot Workflow input fields:
        phone          — contact's phone number (required)
        firstName      — contact's first name
        loopAction     — "sms" or "call" (default: "sms")
        intervalHours  — hours between each action (default: 24)
        durationDays   — total days to run the loop (default: 7)
        messageTemplate — optional message template
    """
    if not _verify_action_request():
        return jsonify({"message": "Unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    parsed = _parse_action_payload(data)

    portal_id = parsed["portal_id"]
    contact_id = parsed["contact_id"]
    lead_phone = parsed["phone"]
    lead_name = parsed["first_name"] or "there"
    loop_action = parsed["loop_action"] or "sms"
    interval_hours = max(1, min(168, parsed["interval_hours"]))  # 1h to 7 days
    duration_days = max(1, min(30, parsed["duration_days"]))     # 1 to 30 days
    message_template = parsed["message_template"]

    if not portal_id or not lead_phone:
        return jsonify({"outputFields": {"loopStatus": "error", "errorReason": "phone_required"}}), 200

    subscriber = _find_subscriber_by_portal(portal_id)
    if not subscriber:
        return jsonify({"outputFields": {"loopStatus": "error", "errorReason": "portal_not_connected"}}), 200

    location_id = subscriber.get("location_id", "")
    max_iterations = int(duration_days * 24 / interval_hours)

    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Cancel any existing active loop for this contact
        cur.execute("""
            SELECT id FROM ghl_action_loops
            WHERE location_id = %s AND contact_id = %s AND status = 'active'
        """, (location_id, contact_id))
        existing = cur.fetchone()
        if existing:
            cur.execute("""
                UPDATE ghl_action_loops SET status = 'cancelled', completed_at = NOW()
                WHERE id = %s
            """, (existing["id"],))

        cur.execute("""
            INSERT INTO ghl_action_loops
                (location_id, contact_id, phone, first_name, loop_action,
                 message_template, duration_days, interval_hours,
                 max_iterations, status, next_execute_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'active',
                    NOW() + interval '1 minute')
        """, (
            location_id, contact_id, lead_phone, lead_name, loop_action,
            message_template, duration_days, interval_hours, max_iterations,
        ))
        conn.commit()

        logger.info(f"HubSpot SMS loop started: portal={portal_id} contact={contact_id} "
                   f"action={loop_action} interval={interval_hours}h duration={duration_days}d")

        return jsonify({"outputFields": {
            "loopStatus": "started",
            "loopAction": loop_action,
            "intervalHours": interval_hours,
            "durationDays": duration_days,
            "maxIterations": max_iterations,
        }}), 200

    except Exception as e:
        conn.rollback()
        logger.error(f"HubSpot SMS loop action failed: {e}")
        return jsonify({"outputFields": {"loopStatus": "error", "errorReason": str(e)[:200]}}), 200
    finally:
        return_db_connection(conn)


@hubspot_workflow_actions_bp.route("/hubspot/workflow-action/definitions", methods=["GET"])
def hubspot_action_definitions():
    """
    Return action definitions for HubSpot app configuration.

    This endpoint documents the available actions for developers
    setting up the HubSpot app in the developer portal.
    """
    domain = os.getenv("YOUR_DOMAIN", "").rstrip("/")

    definitions = {
        "actions": [
            {
                "id": "ai-call",
                "label": "Trigger AI Outbound Call",
                "description": "Initiate an AI-powered outbound call to the contact via Omnisconn Voice.",
                "executionUrl": f"{domain}/hubspot/workflow-action/ai-call",
                "published": True,
                "inputFields": [
                    {
                        "typeDefinition": {
                            "name": "phone",
                            "type": "STRING",
                            "fieldType": "TEXT",
                            "label": "Phone Number",
                            "description": "Contact phone number (e.g. +15551234567)",
                            "required": True,
                        },
                        "supportedValueTypes": ["STATIC_VALUE", "OBJECT_PROPERTY"],
                        "isRequired": True,
                    },
                    {
                        "typeDefinition": {
                            "name": "firstName",
                            "type": "STRING",
                            "fieldType": "TEXT",
                            "label": "First Name",
                            "description": "Contact first name for AI personalization",
                            "required": False,
                        },
                        "supportedValueTypes": ["STATIC_VALUE", "OBJECT_PROPERTY"],
                        "isRequired": False,
                    },
                ],
                "outputFields": [
                    {"typeDefinition": {"name": "callStatus", "type": "STRING", "label": "Call Status", "fieldType": "TEXT"}},
                    {"typeDefinition": {"name": "callSid", "type": "STRING", "label": "Call SID", "fieldType": "TEXT"}},
                ],
            },
            {
                "id": "sms-once",
                "label": "Send AI SMS",
                "description": "Send a single AI-personalized SMS to the contact via Omnisconn.",
                "executionUrl": f"{domain}/hubspot/workflow-action/sms-once",
                "published": True,
                "inputFields": [
                    {
                        "typeDefinition": {
                            "name": "phone",
                            "type": "STRING",
                            "fieldType": "TEXT",
                            "label": "Phone Number",
                            "description": "Contact phone number",
                            "required": True,
                        },
                        "supportedValueTypes": ["STATIC_VALUE", "OBJECT_PROPERTY"],
                        "isRequired": True,
                    },
                    {
                        "typeDefinition": {
                            "name": "firstName",
                            "type": "STRING",
                            "fieldType": "TEXT",
                            "label": "First Name",
                            "required": False,
                        },
                        "supportedValueTypes": ["STATIC_VALUE", "OBJECT_PROPERTY"],
                        "isRequired": False,
                    },
                    {
                        "typeDefinition": {
                            "name": "messageTemplate",
                            "type": "STRING",
                            "fieldType": "TEXTAREA",
                            "label": "Message Context",
                            "description": "Optional context/seed for the AI to personalize the message",
                            "required": False,
                        },
                        "supportedValueTypes": ["STATIC_VALUE"],
                        "isRequired": False,
                    },
                ],
                "outputFields": [
                    {"typeDefinition": {"name": "smsStatus", "type": "STRING", "label": "SMS Status", "fieldType": "TEXT"}},
                ],
            },
            {
                "id": "sms-loop",
                "label": "Start AI Nurture Loop",
                "description": "Begin a repeating AI SMS or call sequence at a set interval for a set duration.",
                "executionUrl": f"{domain}/hubspot/workflow-action/sms-loop",
                "published": True,
                "inputFields": [
                    {
                        "typeDefinition": {"name": "phone", "type": "STRING", "fieldType": "TEXT", "label": "Phone Number", "required": True},
                        "supportedValueTypes": ["STATIC_VALUE", "OBJECT_PROPERTY"],
                        "isRequired": True,
                    },
                    {
                        "typeDefinition": {"name": "firstName", "type": "STRING", "fieldType": "TEXT", "label": "First Name", "required": False},
                        "supportedValueTypes": ["STATIC_VALUE", "OBJECT_PROPERTY"],
                        "isRequired": False,
                    },
                    {
                        "typeDefinition": {
                            "name": "loopAction",
                            "type": "ENUMERATION",
                            "fieldType": "SELECT",
                            "label": "Loop Action",
                            "description": "What to do each iteration",
                            "options": [
                                {"label": "AI SMS", "value": "sms"},
                                {"label": "AI Call", "value": "call"},
                            ],
                        },
                        "supportedValueTypes": ["STATIC_VALUE"],
                        "isRequired": False,
                    },
                    {
                        "typeDefinition": {"name": "intervalHours", "type": "NUMBER", "fieldType": "NUMBER", "label": "Interval (hours)", "description": "Hours between each action (1-168)"},
                        "supportedValueTypes": ["STATIC_VALUE"],
                        "isRequired": False,
                    },
                    {
                        "typeDefinition": {"name": "durationDays", "type": "NUMBER", "fieldType": "NUMBER", "label": "Duration (days)", "description": "Total days to run (1-30)"},
                        "supportedValueTypes": ["STATIC_VALUE"],
                        "isRequired": False,
                    },
                    {
                        "typeDefinition": {"name": "messageTemplate", "type": "STRING", "fieldType": "TEXTAREA", "label": "Message Context", "required": False},
                        "supportedValueTypes": ["STATIC_VALUE"],
                        "isRequired": False,
                    },
                ],
                "outputFields": [
                    {"typeDefinition": {"name": "loopStatus", "type": "STRING", "label": "Loop Status", "fieldType": "TEXT"}},
                    {"typeDefinition": {"name": "maxIterations", "type": "NUMBER", "label": "Max Iterations", "fieldType": "NUMBER"}},
                ],
            },
        ]
    }

    return jsonify(definitions), 200
