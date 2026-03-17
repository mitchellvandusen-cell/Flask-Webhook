"""
Outbound call initiation and call status webhook routes.

Extracted from voice_bridge.py — trigger_outbound_call() and voice_status().
"""

import logging
import threading
import time
import os
import json
import hmac
import hashlib

from flask import Blueprint, request, jsonify
from flask_login import current_user

import twilio_provisioning
from db import get_db_connection, return_db_connection, log_webhook_event, deduct_ai_minutes
from number_health import select_outbound_number, update_number_health
from voice.call_state import active_calls, transfer_requests
from voice.helpers import _get_subscriber_by_location
from voice.call_history_helpers import save_call_to_history, update_call_history_status, mark_ring_confirmed
from voice.predictive_engine import tcpa_tracker, agent_state_manager, callback_queue, AgentState

logger = logging.getLogger("voice_bridge.outbound")

outbound_bp = Blueprint('voice_outbound', __name__)


# ──────────────────────────────────────────────────────────────
# ROUTE: Trigger outbound call
# ──────────────────────────────────────────────────────────────

@outbound_bp.route('/voice/outbound-call', methods=['POST'])
def trigger_outbound_call():
    """
    API endpoint to initiate an outbound AI voice call via Twilio.
    Called by CRM automations (webhook) or the dashboard.

    Authentication: requires either a valid session (@login_required) OR
    a Bearer token matching the subscriber's API key.
    """
    # Auth gate: accept logged-in session OR Bearer API key
    import secrets as _secrets
    authenticated = False
    if current_user and getattr(current_user, 'is_authenticated', False):
        authenticated = True
    else:
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            provided_key = auth_header[7:].strip()
            if provided_key:
                # Will validate against subscriber's api_key below after we resolve location_id
                pass  # deferred validation
            else:
                return jsonify({"error": "Missing API key"}), 401
        else:
            return jsonify({"error": "Authentication required. Provide a valid session or Bearer token."}), 401

    data = request.json or {}
    # Accept both GHL camelCase (locationId) and our snake_case (location_id)
    location_id = data.get('location_id') or data.get('locationId', '')
    lead_phone  = data.get('phone') or data.get('toNumber', '')
    lead_name   = data.get('first_name') or data.get('firstName', 'there')
    contact_id  = data.get('contact_id') or data.get('contactId', '')

    if not location_id or not lead_phone:
        return jsonify({"error": "location_id and phone are required"}), 400

    subscriber = _get_subscriber_by_location(location_id)
    if not subscriber:
        return jsonify({"error": "Subscriber not found"}), 404

    # Deferred Bearer token validation (now that we have the subscriber)
    if not authenticated:
        auth_header = request.headers.get('Authorization', '')
        provided_key = auth_header[7:].strip() if auth_header.startswith('Bearer ') else ''
        stored_key = subscriber.get("api_key", "")
        if not stored_key or not _secrets.compare_digest(provided_key, stored_key):
            return jsonify({"error": "Invalid API key"}), 403

    voice_config = subscriber.get("voice_config") or {}
    if not voice_config.get("enabled"):
        return jsonify({"error": "Voice is not enabled for this account"}), 400

    sub_sid       = voice_config.get("twilio_sub_account_sid", "")
    from_number   = voice_config.get("twilio_phone_number", "")

    # Smart number rotation for API-triggered calls
    rotation_result = select_outbound_number(location_id, voice_config, dest_phone=lead_phone)
    if rotation_result:
        from_number = rotation_result["phone"]
        logger.info(f"Smart rotation (outbound-call API) selected {from_number} (reason={rotation_result['reason']})")

    if not sub_sid or not from_number:
        return jsonify({"error": "Voice service not fully provisioned"}), 400

    try:
        host = request.host
        webhook_base_url = f"https://{host}"

        # Custom params passed via URL to the outbound-twiml endpoint
        custom_params = {
            'location_id':  location_id,
            'caller':       from_number,
            'called':       lead_phone,
            'direction':    'outbound',
            'contact_id':   contact_id,
            'contact_name': lead_name,
            'dial_mode':    'ai',
        }

        # Create outbound call via Twilio REST API
        # AI mode always needs AMD to detect voicemail greetings
        try:
            ring_timeout = int(voice_config.get('ring_timeout', 45))
        except (ValueError, TypeError):
            ring_timeout = 45
        result = twilio_provisioning.create_outbound_call(
            sub_account_sid=sub_sid,
            to=lead_phone,
            from_number=from_number,
            webhook_base_url=webhook_base_url,
            machine_detection='DetectMessageEnd',
            custom_params=custom_params,
            ring_timeout=ring_timeout,
        )
        call_sid = result.get('call_sid', '')

        logger.info(f"Outbound call initiated: {from_number} -> {lead_phone} (sid={call_sid})")

        # Track in active calls
        agent_email = ''
        if current_user and getattr(current_user, 'is_authenticated', False):
            agent_email = getattr(current_user, 'email', '')
        voice_config_wt = subscriber.get("voice_config") or {}
        active_calls[call_sid] = {
            "status": "initiated",
            "duration": 0,
            "contact_id": contact_id,
            "phone": lead_phone,
            "name": lead_name,
            "_location_id": location_id,
            "_sub_sid": sub_sid,
            "_host": host,
            "_from_number": from_number,
            "_agent_email": agent_email,
            "_wrap_up_time": int(voice_config_wt.get('wrap_up_time', 15)),
        }

        # Persist to call_history DB
        save_call_to_history(
            location_id=location_id,
            call_sid=call_sid,
            phone=lead_phone,
            contact_id=contact_id,
            contact_name=lead_name,
            direction='outbound',
            status='initiated',
            from_number=from_number,
        )

        try:
            log_webhook_event(
                location_id=location_id,
                contact_id=contact_id,
                event_type="voice_outbound_initiated",
                status="success",
                summary=f"Outbound call to {lead_name} ({lead_phone})",
                details={"call_sid": call_sid, "to": lead_phone, "from": from_number}
            )
        except Exception:
            pass

        return jsonify({"status": "calling", "call_sid": call_sid})

    except Exception as e:
        logger.error(f"Failed to initiate outbound call: {e}")
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# ROUTE: GHL Custom Action — AI Call
# ──────────────────────────────────────────────────────────────

@outbound_bp.route('/voice/ghl-action/ai-call', methods=['POST'])
def ghl_action_ai_call():
    """
    GHL Marketplace Custom Action endpoint for triggering AI outbound calls.

    Authentication: verifies GHL webhook signature (MARKETPLACE_WEBHOOK_SECRET)
    so subscribers don't need to manage API keys. The locationId in the payload
    identifies the subscriber.

    GHL custom action payload contains the fields defined in the action config:
    contactId, phone, firstName, locationId.
    """
    # ── Verify GHL webhook signature ──
    webhook_secret = os.getenv("MARKETPLACE_WEBHOOK_SECRET")
    if webhook_secret:
        signature = (request.headers.get("X-Ghl-Signature")
                     or request.headers.get("X-Hook-Secret")
                     or "")
        if signature:
            body = request.get_data(as_text=True)
            expected = hmac.new(
                webhook_secret.encode(), body.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                logger.warning("GHL AI Call action: signature mismatch — rejecting")
                return jsonify({"status": "error", "reason": "invalid_signature"}), 401
        else:
            logger.debug("GHL AI Call action: no signature header — skipping verification")
    else:
        logger.warning("GHL AI Call action: MARKETPLACE_WEBHOOK_SECRET not set — cannot verify")

    data = request.get_json(silent=True) or {}

    # GHL custom actions may wrap fields in customData
    if "customData" in data and isinstance(data["customData"], dict):
        fields = data["customData"]
    else:
        fields = data

    location_id = fields.get("locationId") or fields.get("location_id") or ""
    lead_phone = fields.get("phone") or fields.get("toNumber") or ""
    lead_name = fields.get("firstName") or fields.get("first_name") or "there"
    contact_id = fields.get("contactId") or fields.get("contact_id") or ""

    if not location_id or not lead_phone:
        return jsonify({"error": "locationId and phone are required"}), 400

    subscriber = _get_subscriber_by_location(location_id)
    if not subscriber:
        return jsonify({"error": "Subscriber not found for this location"}), 404

    voice_config = subscriber.get("voice_config") or {}
    if not voice_config.get("enabled"):
        return jsonify({"error": "Voice is not enabled for this account"}), 400

    sub_sid = voice_config.get("twilio_sub_account_sid", "")
    from_number = voice_config.get("twilio_phone_number", "")

    # Smart number rotation
    rotation_result = select_outbound_number(location_id, voice_config, dest_phone=lead_phone)
    if rotation_result:
        from_number = rotation_result["phone"]
        logger.info(f"Smart rotation (ghl-action) selected {from_number} (reason={rotation_result['reason']})")

    if not sub_sid or not from_number:
        return jsonify({"error": "Voice service not fully provisioned"}), 400

    try:
        host = request.host
        webhook_base_url = f"https://{host}"

        custom_params = {
            'location_id':  location_id,
            'caller':       from_number,
            'called':       lead_phone,
            'direction':    'outbound',
            'contact_id':   contact_id,
            'contact_name': lead_name,
            'dial_mode':    'ai',
        }

        try:
            ring_timeout = int(voice_config.get('ring_timeout', 45))
        except (ValueError, TypeError):
            ring_timeout = 45

        result = twilio_provisioning.create_outbound_call(
            sub_account_sid=sub_sid,
            to=lead_phone,
            from_number=from_number,
            webhook_base_url=webhook_base_url,
            machine_detection='DetectMessageEnd',
            custom_params=custom_params,
            ring_timeout=ring_timeout,
        )
        call_sid = result.get('call_sid', '')

        logger.info(f"GHL AI Call action: {from_number} -> {lead_phone} (sid={call_sid}, location={location_id})")

        # Track in active calls
        active_calls[call_sid] = {
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
            "_wrap_up_time": int(voice_config.get('wrap_up_time', 15)),
        }

        # Persist to call_history DB
        save_call_to_history(
            location_id=location_id,
            call_sid=call_sid,
            phone=lead_phone,
            contact_id=contact_id,
            contact_name=lead_name,
            direction='outbound',
            status='initiated',
            from_number=from_number,
        )

        try:
            log_webhook_event(
                location_id=location_id,
                contact_id=contact_id,
                event_type="ghl_action_ai_call",
                status="success",
                summary=f"GHL action: AI call to {lead_name} ({lead_phone})",
                details={"call_sid": call_sid, "to": lead_phone, "from": from_number}
            )
        except Exception:
            pass

        return jsonify({"status": "calling", "call_sid": call_sid})

    except Exception as e:
        logger.error(f"GHL AI Call action failed: {e}")
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# ROUTE: GHL Trigger Subscription Management
# ──────────────────────────────────────────────────────────────

@outbound_bp.route('/voice/ghl-trigger/subscribe', methods=['POST'])
def ghl_trigger_subscribe():
    """
    GHL calls this when a user adds/removes a trigger in their workflow.

    GHL sends:
      - locationId: which location is subscribing
      - webhookUrl: where to fire events (GHL's internal webhook receiver)
      - action: "subscribe" or "unsubscribe" (may vary by GHL version)

    We store the subscription so we know which locations to notify
    when events (like AI Call Completed) occur.
    """
    data = request.get_json(silent=True) or {}

    location_id = data.get("locationId") or data.get("location_id") or ""
    webhook_url = data.get("webhookUrl") or data.get("webhook_url") or ""
    trigger_type = data.get("triggerType") or data.get("trigger_type") or "ai_call_completed"
    action = (data.get("action") or "subscribe").lower()

    logger.info(f"GHL trigger subscription: location={location_id} type={trigger_type} action={action} url={webhook_url[:60]}")

    if not location_id:
        return jsonify({"error": "locationId is required"}), 400

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Database unavailable"}), 503
        cur = conn.cursor()

        if action == "unsubscribe" or action == "delete":
            cur.execute(
                "DELETE FROM ghl_trigger_subscriptions WHERE location_id = %s AND trigger_type = %s",
                (location_id, trigger_type)
            )
            conn.commit()
            cur.close()
            logger.info(f"GHL trigger unsubscribed: location={location_id} type={trigger_type}")
            return jsonify({"status": "unsubscribed"})
        else:
            if not webhook_url:
                return jsonify({"error": "webhookUrl is required for subscribe"}), 400
            cur.execute("""
                INSERT INTO ghl_trigger_subscriptions (location_id, trigger_type, webhook_url)
                VALUES (%s, %s, %s)
                ON CONFLICT (location_id, trigger_type)
                DO UPDATE SET webhook_url = EXCLUDED.webhook_url
            """, (location_id, trigger_type, webhook_url))
            conn.commit()
            cur.close()
            logger.info(f"GHL trigger subscribed: location={location_id} type={trigger_type}")
            return jsonify({"status": "subscribed"})

    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"GHL trigger subscription failed: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            return_db_connection(conn)


# ──────────────────────────────────────────────────────────────
# HELPER: Fire GHL trigger events
# ──────────────────────────────────────────────────────────────

def _fire_ghl_trigger(location_id, trigger_type, payload):
    """
    Fire a trigger event to GHL for a subscribed location.
    Runs in a background thread to avoid blocking the status callback.

    Args:
        location_id: GHL location ID
        trigger_type: e.g. "ai_call_completed"
        payload: dict with trigger data (contactId, callStatus, etc.)
    """
    def _send():
        import requests as http_requests
        conn = None
        try:
            conn = get_db_connection()
            if not conn:
                return
            cur = conn.cursor()
            cur.execute(
                "SELECT webhook_url FROM ghl_trigger_subscriptions WHERE location_id = %s AND trigger_type = %s",
                (location_id, trigger_type)
            )
            row = cur.fetchone()
            cur.close()

            if not row:
                return  # No subscription for this location/trigger

            webhook_url = row["webhook_url"]
            logger.info(f"Firing GHL trigger: type={trigger_type} location={location_id} url={webhook_url[:60]}")

            resp = http_requests.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            logger.info(f"GHL trigger fired: status={resp.status_code} type={trigger_type} location={location_id}")

        except Exception as e:
            logger.warning(f"GHL trigger fire failed: {e} (type={trigger_type}, location={location_id})")
        finally:
            if conn:
                return_db_connection(conn)

    t = threading.Thread(target=_send, daemon=True, name=f"ghl-trigger-{trigger_type[:12]}-{location_id[:8]}")
    t.start()


# ──────────────────────────────────────────────────────────────
# ROUTE: Call status webhook
# ──────────────────────────────────────────────────────────────

@outbound_bp.route('/voice/status', methods=['POST'])
def voice_status():
    """
    Twilio posts call status events here as form data.
    Status values: initiated, ringing, in-progress, completed, busy, no-answer, canceled, failed.
    """
    call_sid    = request.values.get('CallSid', '')
    call_status = request.values.get('CallStatus', '')
    duration    = request.values.get('CallDuration', '0')
    sip_code    = request.values.get('SipResponseCode', '')
    stir_status = request.values.get('StirStatus', '')  # STIR/SHAKEN attestation (A/B/C) for outbound
    # For inbound calls, StirVerstat was captured at webhook time and stored in active_calls
    if not stir_status and call_sid in active_calls:
        stir_status = active_calls[call_sid].get('_stir_verstat', '') or ''

    logger.info(f"📞 Call status: SID={call_sid} status={call_status} duration={duration}s sip={sip_code} stir={stir_status or 'n/a'}")

    # Track status in memory for dialer queue polling
    # Twilio can deliver callbacks out of order (e.g. 'ringing' after 'in-progress'),
    # so only allow forward transitions to prevent status regression.
    _STATUS_ORDER = {
        'queued': 0, 'initiated': 1, 'ringing': 2,
        'in-progress': 3,
        'completed': 4, 'busy': 4, 'no-answer': 4, 'failed': 4, 'canceled': 4, 'transferred': 4,
    }
    if call_sid in active_calls:
        # If AMD hung up the call, Twilio still fires 'completed'. Preserve the
        # AMD-set status ('no-answer') so the frontend retry logic can trigger.
        effective_status = call_status
        amd_result = active_calls[call_sid].get('_amd_result')
        if call_status == 'completed' and amd_result:
            effective_status = amd_result
            logger.info(f"📞 AMD call {call_sid[:16]} ended — reporting as '{amd_result}' for retry")

        current_status = active_calls[call_sid].get("status", "")
        new_order = _STATUS_ORDER.get(effective_status, 99)
        cur_order = _STATUS_ORDER.get(current_status, 99)
        if new_order >= cur_order:
            active_calls[call_sid]["status"] = effective_status
        else:
            logger.info(f"📞 Ignoring out-of-order status '{effective_status}' for {call_sid[:16]} (current='{current_status}')")
        active_calls[call_sid]["duration"] = int(duration or 0)

    # ── SIP 180 Ring Confirmation ──
    # When Twilio fires 'ringing', it means the carrier returned a SIP 180/183 —
    # the lead's phone is legitimately ringing (not fake ringback or immediate VM).
    if call_status == 'ringing' and call_sid in active_calls:
        if not active_calls[call_sid].get('_ring_confirmed'):
            active_calls[call_sid]['_ring_confirmed'] = True
            logger.info(f"📞 Ring confirmed (SIP 180): {call_sid[:16]}")
            mark_ring_confirmed(call_sid)

    # ── Agent state machine: auto-transition to ON_CALL when call goes in-progress ──
    if call_status == 'in-progress' and call_sid in active_calls:
        asm_info = active_calls[call_sid]
        asm_loc = asm_info.get('_location_id', '')
        asm_email = asm_info.get('_agent_email', '')
        if asm_loc and asm_email:
            agent_state_manager.set_state(asm_loc, asm_email, AgentState.ON_CALL, call_sid=call_sid)

    # Persist to call_history DB
    if call_sid:
        try:
            update_call_history_status(call_sid, call_status, duration,
                                       stir_status=stir_status or None)
        except Exception as e:
            logger.warning(f"call_history update failed for {call_sid}: {e}")

    # ── TCPA compliance: record call outcome for abandon rate tracking ──
    terminal_statuses = {'completed', 'busy', 'no-answer', 'failed', 'canceled'}
    if call_status in terminal_statuses and call_sid:
        call_info = active_calls.get(call_sid, {})
        tcpa_location = call_info.get('_location_id', '')
        if tcpa_location:
            dur_int = int(duration or 0)
            if call_status == 'completed' and dur_int > 0:
                tcpa_tracker.record_call_outcome(tcpa_location, 'answered')
            elif call_status == 'completed' and dur_int == 0:
                # Call answered but 0 duration — agent likely abandoned before pickup
                tcpa_tracker.record_call_outcome(tcpa_location, 'abandoned')
            elif call_status == 'no-answer':
                tcpa_tracker.record_call_outcome(tcpa_location, 'no_answer')
            elif call_status == 'busy':
                tcpa_tracker.record_call_outcome(tcpa_location, 'busy')
            elif call_status in ('failed', 'canceled'):
                tcpa_tracker.record_call_outcome(tcpa_location, 'no_answer')

    # ── Auto-callback scheduling: queue re-dial for no-answer/busy if enabled ──
    if call_status in ('no-answer', 'busy') and call_sid:
        cb_info = active_calls.get(call_sid, {})
        cb_location = cb_info.get('_location_id', '')
        if cb_location:
            try:
                subscriber = _get_subscriber_by_location(cb_location)
                if subscriber:
                    vc = subscriber.get('voice_config') or {}
                    if vc.get('auto_callback'):
                        cb_phone = cb_info.get('phone', '')
                        cb_name = cb_info.get('name', 'Unknown')
                        cb_contact_id = cb_info.get('contact_id', '')
                        cb_delay = 30  # default 30 minutes
                        if cb_phone:
                            callback_queue.schedule_callback(
                                cb_location, cb_contact_id, cb_phone, cb_name,
                                time.time() + (cb_delay * 60), call_status
                            )
                            logger.info(f"Auto-scheduled callback for {cb_phone} in {cb_delay}min (reason={call_status})")
            except Exception as e:
                logger.debug(f"Auto-callback scheduling failed (non-fatal): {e}")

    # ── Agent state machine: auto-transition ON_CALL → WRAP_UP on terminal ──
    if call_status in terminal_statuses and call_sid:
        call_info_asm = active_calls.get(call_sid, {})
        asm_location = call_info_asm.get('_location_id', '')
        if asm_location and call_status == 'in-progress':
            pass  # handled below
        elif asm_location and call_status in terminal_statuses:
            # Find the agent email from the subscriber lookup (cached in call_info)
            asm_email = call_info_asm.get('_agent_email', '')
            if asm_email:
                agent_current = agent_state_manager.get_agent_state(asm_location, asm_email)
                if agent_current.get('state') == AgentState.ON_CALL:
                    wrap_time = call_info_asm.get('_wrap_up_time', 15)
                    if int(duration or 0) > 0:
                        agent_state_manager.start_wrap_up(asm_location, asm_email, wrap_time)
                    else:
                        agent_state_manager.set_state(asm_location, asm_email, AgentState.READY)

    # Update number health metrics on terminal statuses
    if call_status in terminal_statuses and call_sid:
        call_info = active_calls.get(call_sid, {})
        nh_location = call_info.get('_location_id', '')
        nh_from = call_info.get('_from_number', '')
        nh_effective = call_info.get('_amd_result', call_status)  # Use AMD result if available
        nh_ring_confirmed = call_info.get('_ring_confirmed', False)
        if nh_location and nh_from:
            try:
                update_number_health(nh_location, nh_from, nh_effective, int(duration or 0),
                                     sip_code=sip_code, ring_confirmed=nh_ring_confirmed)
            except Exception as e:
                logger.warning(f"Number health update failed for {nh_from}: {e}")

    # Deduct AI minutes for completed calls with duration > 0
    dur_s = int(duration or 0)
    if dur_s > 0 and call_status == 'completed' and call_sid:
        conn_m = None
        try:
            conn_m = get_db_connection()
            if conn_m:
                cur_m = conn_m.cursor()
                cur_m.execute("""
                    SELECT ch.location_id, ch.phone, ch.direction, s.email
                    FROM call_history ch
                    JOIN subscribers s ON s.location_id = ch.location_id
                    WHERE ch.call_sid = %s
                """, (call_sid,))
                row_m = cur_m.fetchone()
                cur_m.close()
        except Exception as e:
            logger.warning(f"AI minute deduction DB lookup failed for {call_sid}: {e}")
            row_m = None
        finally:
            if conn_m:
                return_db_connection(conn_m)
        try:
            if row_m and row_m['email']:
                result = deduct_ai_minutes(
                    email=row_m['email'],
                    duration_seconds=dur_s,
                    call_sid=call_sid,
                    phone=row_m.get('phone', ''),
                    direction=row_m.get('direction', 'outbound'),
                )
                if result.get('success'):
                    logger.info(f"AI Minutes: Deducted {result['minutes_deducted']}min from {row_m['email']}, balance={result['balance_after']}")
        except Exception as e:
            logger.warning(f"AI minute deduction failed for {call_sid}: {e}")

    # Log completed/terminal calls to GHL so they appear in CRM conversation history
    if call_status in terminal_statuses and call_sid and call_sid in active_calls:
        call_info = active_calls[call_sid]
        ghl_contact_id = call_info.get('contact_id', '')
        ghl_location_id = call_info.get('_location_id', '')
        ghl_phone = call_info.get('phone', '')
        ghl_from = call_info.get('_from_number', '')

        # For inbound calls, contact_id may be empty in active_calls.
        # Resolve from call_history DB (populated during WebSocket bridge).
        call_direction = 'outbound'
        if ghl_location_id:
            try:
                conn_dir = get_db_connection()
                if conn_dir:
                    try:
                        cur_dir = conn_dir.cursor()
                        cur_dir.execute(
                            "SELECT direction, contact_id FROM call_history WHERE call_sid = %s",
                            (call_sid,))
                        dir_row = cur_dir.fetchone()
                        if dir_row:
                            call_direction = dir_row['direction'] or 'outbound'
                            if not ghl_contact_id and dir_row.get('contact_id'):
                                ghl_contact_id = dir_row['contact_id']
                        cur_dir.close()
                    finally:
                        return_db_connection(conn_dir)
            except Exception:
                pass

        if ghl_contact_id and ghl_location_id:
            try:
                from ghl_logger import log_call_to_ghl
                from ghl_api import get_valid_token
                ghl_token = get_valid_token(ghl_location_id)
                if ghl_token:
                    log_call_to_ghl(
                        contact_id=ghl_contact_id,
                        access_token=ghl_token,
                        direction=call_direction,
                        phone=ghl_phone,
                        status=call_status,
                        duration=int(duration or 0),
                        from_number=ghl_from,
                    )
            except Exception as ghl_call_err:
                logger.debug(f"GHL call log skipped for {call_sid}: {ghl_call_err}")

    # ── Voice Insights: queue background fetch of Call Summary ──
    if call_status in terminal_statuses and call_sid and call_sid in active_calls:
        _queue_insights_fetch(call_sid, active_calls.get(call_sid, {}))

    # ── Fire GHL trigger: AI Call Completed ──
    if call_status in terminal_statuses and call_sid and call_sid in active_calls:
        trigger_info = active_calls[call_sid]
        trigger_location = trigger_info.get('_location_id', '')
        if trigger_location:
            effective_status = trigger_info.get('_amd_result', call_status)
            if effective_status == 'completed' and int(duration or 0) == 0:
                effective_status = 'no-answer'
            _fire_ghl_trigger(trigger_location, 'ai_call_completed', {
                'contactId': trigger_info.get('contact_id', ''),
                'locationId': trigger_location,
                'phone': trigger_info.get('phone', ''),
                'firstName': trigger_info.get('name', ''),
                'callSid': call_sid,
                'callStatus': effective_status,
                'callDuration': int(duration or 0),
                'direction': 'outbound',
            })

    return '', 204


def _queue_insights_fetch(call_sid, call_info):
    """Queue a background thread to fetch Voice Insights Call Summary ~90s after call ends."""
    sub_sid = call_info.get('_sub_sid', '')
    location_id = call_info.get('_location_id', '')
    from_number = call_info.get('_from_number', '')

    if not sub_sid:
        return

    # Look up sub-account auth token for Insights API access
    auth_token = None
    try:
        subscriber = _get_subscriber_by_location(location_id)
        if subscriber:
            vc = subscriber.get('voice_config') or {}
            auth_token = vc.get('twilio_auth_token', '')
    except Exception:
        pass

    def _fetch():
        try:
            from voice.insights import fetch_and_store_call_insights
            fetch_and_store_call_insights(
                call_sid=call_sid,
                sub_account_sid=sub_sid,
                sub_account_auth_token=auth_token,
                location_id=location_id,
                from_number=from_number,
            )
        except Exception as e:
            logger.debug(f"Insights fetch failed for {call_sid}: {e}")

    t = threading.Thread(target=_fetch, daemon=True, name=f"insights-{call_sid[:12]}")
    t.start()
