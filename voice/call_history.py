"""
voice/call_history.py - Call history routes: status polling, hangup, takeover,
transfer, disposition, recording callbacks, transcription webhook, scheduled
callbacks, call history listing, and voicemails.

Extracted from voice_bridge.py.
"""

import json
import os
import logging
import re
import time
import threading
from datetime import datetime, timedelta

import pytz
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from ghl_auth import jwt_or_session_required


import twilio_provisioning
from db import get_db_connection, return_db_connection, log_webhook_event
from openai import OpenAI
from voice.audio import XAI_API_KEY
from voice.call_state import (
    get_active_call, update_active_call, delete_active_call, call_exists,
    set_transfer_request, get_transfer_request, delete_transfer_request,
    call_listeners, _twilio_hangup, _twilio_transfer,
)
from voice.call_history_helpers import update_call_history_status
from voice.helpers import _get_current_subscriber_voice, _verify_call_ownership

logger = logging.getLogger("voice_bridge.call_history")

call_history_bp = Blueprint('voice_call_history', __name__)


# ──────────────────────────────────────────────────────────────
# ROUTE: Poll call status for the dialer queue
# ──────────────────────────────────────────────────────────────

@call_history_bp.route('/voice/call-status/<call_sid>', methods=['GET'])
@jwt_or_session_required
def get_call_status(call_sid):
    """Poll call status for the dialer queue."""
    if call_exists(call_sid):
        if not _verify_call_ownership(call_sid):
            return jsonify({"status": "unknown"}), 404
        info = get_active_call(call_sid)
        if info is None:
            return jsonify({"status": "unknown"}), 404
        # For terminal states, mark for cleanup but don't delete yet (allow re-polls)
        if info["status"] in ("completed", "busy", "no-answer", "failed", "canceled", "transferred"):
            poll_count = info.get('_terminal_polls', 0) + 1
            update_active_call(call_sid, _terminal_polls=poll_count)
            # Clean up after 20 polls of a terminal state (gives frontend plenty of time)
            if poll_count >= 20:
                status_copy = dict(info)
                delete_active_call(call_sid)
                return jsonify(status_copy)
        logger.debug(f"Poll {call_sid[:16]}: status={info.get('status')}")
        return jsonify(info)
    logger.debug(f"Poll {call_sid[:16]}: not found in active_calls")
    return jsonify({"status": "unknown"}), 404


# ──────────────────────────────────────────────────────────────
# ROUTE: Hang up an active call from the dialer UI
# ──────────────────────────────────────────────────────────────

@call_history_bp.route('/voice/hangup', methods=['POST'])
@jwt_or_session_required
def hangup_active_call():
    """Hang up the currently active call."""
    data = request.json or {}
    call_sid = data.get('call_sid', '')
    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        subscriber = dict(row)
    finally:
        return_db_connection(conn)

    sub_sid = (subscriber.get('voice_config') or {}).get('twilio_sub_account_sid', '')
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    # If the call was transferred, Twilio created a child call.  We need to
    # complete the *parent* (original) call, plus any child leg that's still up.
    call_info_hangup = get_active_call(call_sid)
    was_transferred = (call_info_hangup is not None and
                       call_info_hangup.get('status') == 'transferred')

    success = _twilio_hangup(call_sid, sub_sid)

    # Also try to complete any child calls spawned by the transfer <Dial>
    if was_transferred:
        try:
            client = twilio_provisioning.get_sub_account_client(sub_sid)
            child_calls = client.calls.list(parent_call_sid=call_sid, status='in-progress', limit=5)
            for child in child_calls:
                try:
                    child.update(status='completed')
                    logger.info(f"Completed child call {child.sid} of transferred parent {call_sid[:16]}")
                except Exception as ce:
                    logger.warning(f"Failed to complete child {child.sid}: {ce}")
        except Exception as e:
            logger.warning(f"Could not list child calls for {call_sid}: {e}")

    if call_exists(call_sid):
        update_active_call(call_sid, status='completed')
    delete_transfer_request(call_sid)

    # Persist to DB
    try:
        update_call_history_status(call_sid, 'completed', 0)
    except Exception as e:
        logger.warning(f"Hangup DB persist failed for {call_sid}: {e}")

    if success:
        return jsonify({"status": "hung_up", "success": True})
    # Twilio hangup failed — call may have already ended naturally.
    # Return success:false so the client can log it; we still return 200
    # so the UI cleans up (the call is gone either way).
    logger.warning(f"Twilio hangup API returned failure for {call_sid} — call may have already ended")
    return jsonify({"status": "hung_up", "success": False, "note": "call may have already ended"})


# ──────────────────────────────────────────────────────────────
# ROUTE: Live takeover — agent barges into an active AI call
# ──────────────────────────────────────────────────────────────

@call_history_bp.route('/voice/takeover', methods=['POST'])
@jwt_or_session_required
def voice_takeover():
    """
    Let a human agent take over an active AI call.
    Supports two modes:
    1. VoIP (browser): Redirects call to <Dial><Client>agent_{location_id}</Client></Dial>
    2. Phone: Transfers to the agent's phone number via <Dial>{number}</Dial>
    """
    data = request.json or {}
    call_sid = data.get('call_sid', '')
    use_voip = data.get('use_voip', False)
    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    # Look up subscriber info
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        voice_cfg = row.get('voice_config') or {}
        location_id = row.get('location_id', '')
    finally:
        return_db_connection(conn)

    sub_sid = voice_cfg.get('twilio_sub_account_sid', '')
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    # Verify the call is actually active and belongs to this user
    if not call_exists(call_sid):
        return jsonify({"error": "Call not found or already ended"}), 404

    call_info = get_active_call(call_sid)
    if call_info is None:
        return jsonify({"error": "Call not found or already ended"}), 404
    call_location = call_info.get('_location_id', '')
    if call_location and call_location != location_id:
        return jsonify({"error": "Call not found or already ended"}), 404

    if call_info.get('status') in ('completed', 'failed', 'transferred', 'no-answer', 'busy'):
        return jsonify({"error": f"Call already ended ({call_info.get('status')})"}), 400

    # Guard 2: Pre-verify with Twilio that call is still in-progress.
    # The in-memory status can be stale (stream closed but status callback not yet arrived).
    try:
        pre_client = twilio_provisioning.get_sub_account_client(sub_sid)
        live_call = pre_client.calls(call_sid).fetch()
        live_status = live_call.status  # queued/ringing/in-progress/completed/busy/no-answer/canceled/failed
        if live_status not in ('queued', 'ringing', 'in-progress'):
            # Sync local state so future checks don't hit Twilio again
            update_active_call(call_sid, status=live_status)
            return jsonify({"error": f"Call already ended ({live_status})"}), 400
    except Exception as pre_err:
        err_str = str(pre_err)
        if '21220' in err_str or 'not in-progress' in err_str.lower():
            update_active_call(call_sid, status='completed')
            return jsonify({"error": "Call already ended"}), 400
        logger.warning(f"Takeover pre-verify failed (proceeding): {pre_err}")

    host = request.host

    if use_voip and location_id:
        # VoIP intercept: redirect call to browser client
        identity = f"agent_{location_id}"
        target = f"client:{identity}"
        logger.info(f"Takeover (VoIP): redirecting call {call_sid} to browser client={identity}")

        # IMPORTANT: Redirect FIRST, then signal the voice bridge.
        # If we signal first, the voice bridge closes the Twilio media stream
        # before the redirect takes effect, causing the call to drop.
        try:
            client = twilio_provisioning.get_sub_account_client(sub_sid)
            client.calls(call_sid).update(
                url=f"https://{host}/voice/intercept-twiml?identity={identity}",
                method="POST",
            )
        except Exception as e:
            logger.error(f"Takeover (VoIP): redirect FAILED for call {call_sid}: {e}")
            return jsonify({"error": f"Intercept failed: {e}"}), 400

        # Now signal the voice bridge to stop AI audio (stream is already
        # being redirected by Twilio, so closing it is safe)
        set_transfer_request(call_sid, {
            'type': 'takeover',
            'target': target,
            'reason': 'Agent initiated VoIP intercept',
        })
        update_active_call(call_sid, status='transferred')
        logger.info(f"Takeover (VoIP): call {call_sid} redirected to {identity}")
        return jsonify({"status": "transferred", "call_sid": call_sid, "target": "Browser (VoIP)"})
    else:
        # Phone intercept: transfer to agent's phone number
        target = data.get('target') or voice_cfg.get('transfer_number', '')
        if not target:
            # No VoIP, no transfer number — at minimum STOP the AI by hanging up
            logger.info(f"Takeover (hangup): no VoIP/transfer number, stopping AI call {call_sid}")
            set_transfer_request(call_sid, {
                'type': 'takeover',
                'target': '',
                'reason': 'Agent stopped AI (no VoIP/transfer)',
            })
            try:
                _twilio_hangup(call_sid, sub_sid)
            except Exception as e:
                logger.warning(f"Takeover hangup failed: {e}")
            if call_exists(call_sid):
                update_active_call(call_sid, status='canceled')
            return jsonify({"status": "stopped", "call_sid": call_sid,
                            "target": "AI stopped (call ended — set up VoIP or Transfer Number to take over live)"})

        # Normalize target
        if not target.startswith('+'):
            target = '+1' + target.lstrip('1') if len(target.replace('-','').replace(' ','')) <= 10 else '+' + target

        logger.info(f"Takeover (phone): executing transfer for call {call_sid} -> {target}")

        # IMPORTANT: Transfer FIRST, then signal the voice bridge.
        # If we signal first, the voice bridge closes the Twilio media stream
        # before the transfer takes effect, causing the call to drop.
        transfer_ok = _twilio_transfer(call_sid, sub_sid, target, f"https://{host}")
        if transfer_ok:
            # Now signal the voice bridge to stop AI audio
            set_transfer_request(call_sid, {
                'type': 'takeover',
                'target': target,
                'reason': 'Agent initiated live takeover',
            })
            logger.info(f"Takeover (phone): call {call_sid} transferred to {target}")
            update_active_call(call_sid, status='transferred')
            return jsonify({"status": "transferred", "call_sid": call_sid, "target": target})
        else:
            logger.error(f"Takeover (phone): transfer FAILED for call {call_sid} -> {target}")
            return jsonify({"error": "Transfer failed — the call may have ended."}), 400


# ──────────────────────────────────────────────────────────────
# ROUTE: Live transfer to another phone number
# ──────────────────────────────────────────────────────────────

@call_history_bp.route('/voice/transfer', methods=['POST'])
@login_required
def voice_transfer():
    """Transfer an active call to another phone number via Twilio."""
    data = request.json or {}
    call_sid = data.get('call_sid', '')
    transfer_to = data.get('transfer_to', '').strip()

    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400
    if not transfer_to:
        return jsonify({"error": "transfer_to number required"}), 400

    # Normalize phone number
    if not transfer_to.startswith('+'):
        transfer_to = '+1' + transfer_to.lstrip('1') if len(transfer_to.replace('-','').replace(' ','')) <= 10 else '+' + transfer_to

    # Verify the call is active and belongs to this user
    if not call_exists(call_sid):
        return jsonify({"error": "Call not found or already ended"}), 404

    if not _verify_call_ownership(call_sid):
        return jsonify({"error": "Call not found or already ended"}), 404

    call_info = get_active_call(call_sid)
    if call_info is None:
        return jsonify({"error": "Call not found or already ended"}), 404
    if call_info.get('status') in ('completed', 'failed', 'transferred', 'no-answer'):
        return jsonify({"error": f"Call already in terminal state: {call_info.get('status')}"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        voice_cfg = (row['voice_config'] if row else None) or {}
    finally:
        return_db_connection(conn)

    sub_sid = voice_cfg.get('twilio_sub_account_sid', '')
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    host = request.host
    success = _twilio_transfer(call_sid, sub_sid, transfer_to, f"https://{host}")
    if success:
        update_active_call(call_sid, status='transferred')
        # Persist transfer status to DB
        try:
            update_call_history_status(call_sid, 'completed', 0)
        except Exception as e:
            logger.warning(f"Transfer DB persist failed for {call_sid}: {e}")
        logger.info(f"Live transfer: call {call_sid} -> {transfer_to}")
        return jsonify({"status": "transferred", "call_sid": call_sid, "transfer_to": transfer_to})

    return jsonify({"error": "Transfer failed — call may have ended"}), 400


# ──────────────────────────────────────────────────────────────
# ROUTE: Enterprise Warm Transfer (Conference-based)
# ──────────────────────────────────────────────────────────────

from voice.redis_state import (
    set_warm_transfer, get_warm_transfer, update_warm_transfer, delete_warm_transfer,
)


@call_history_bp.route('/voice/warm-transfer/initiate', methods=['POST'])
@login_required
def warm_transfer_initiate():
    """
    Initiate a warm (consultative) transfer.
    1. Move the caller into a Conference room
    2. Dial the transfer target into the same Conference
    3. Agent joins the Conference to consult with target
    All three parties can talk; agent drops off when ready.
    """
    data = request.json or {}
    call_sid = data.get('call_sid', '').strip()
    transfer_to = data.get('transfer_to', '').strip()

    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400
    if not transfer_to:
        return jsonify({"error": "transfer_to number required"}), 400

    # Normalize phone number
    if not transfer_to.startswith('+'):
        clean = transfer_to.replace('-', '').replace(' ', '').replace('(', '').replace(')', '').replace('.', '')
        transfer_to = '+1' + clean.lstrip('1') if len(clean) <= 10 else '+' + clean

    # Verify call is active and belongs to this user
    if not call_exists(call_sid):
        return jsonify({"error": "Call not found or already ended"}), 404
    if not _verify_call_ownership(call_sid):
        return jsonify({"error": "Call not found or already ended"}), 404

    call_info = get_active_call(call_sid)
    if call_info is None:
        return jsonify({"error": "Call not found or already ended"}), 404
    if call_info.get('status') in ('completed', 'failed', 'transferred', 'no-answer'):
        return jsonify({"error": f"Call already in terminal state: {call_info.get('status')}"}), 400

    # Check if already in a warm transfer
    existing = get_warm_transfer(call_sid)
    if existing and existing.get('status') == 'consulting':
        return jsonify({"error": "Warm transfer already in progress"}), 400

    # Get subscriber voice config
    subscriber, voice_cfg, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    from_number = call_info.get('_from_number', '') or voice_cfg.get('twilio_phone_number', '')
    if not from_number:
        return jsonify({"error": "No caller ID number available"}), 400

    location_id = call_info.get('_location_id', '')
    host = request.host
    webhook_base = f"https://{host}"
    conf_name = f"warmxfer_{call_sid[-12:]}"

    try:
        # Step 1: Redirect the caller's call to Conference TwiML
        from urllib.parse import quote
        conf_twiml_url = f"{webhook_base}/voice/warm-transfer/conference-twiml?conf_name={quote(conf_name)}"
        ok = twilio_provisioning.redirect_call_to_twiml(sub_sid, call_sid, conf_twiml_url)
        if not ok:
            return jsonify({"error": "Failed to move caller to conference — call may have ended"}), 400

        # Step 2: Dial the transfer target into the same Conference
        target_twiml_url = f"{webhook_base}/voice/warm-transfer/target-twiml?conf_name={quote(conf_name)}"
        target_status_cb = f"{webhook_base}/voice/warm-transfer/conference-status?conf_name={quote(conf_name)}"

        target_result = twilio_provisioning.add_conference_participant(
            sub_account_sid=sub_sid,
            conference_name=conf_name,
            to=transfer_to,
            from_number=from_number,
            twiml_url=target_twiml_url,
            status_callback=target_status_cb,
        )
        target_call_sid = target_result.get('call_sid', '')

        # Step 3: Store warm transfer state
        set_warm_transfer(call_sid, {
            "conference_name": conf_name,
            "transfer_to": transfer_to,
            "transfer_call_sid": target_call_sid,
            "caller_call_sid": call_sid,
            "location_id": location_id,
            "agent_identity": f"agent_{location_id}",
            "status": "consulting",
            "initiated_at": datetime.utcnow().isoformat(),
        })

        update_active_call(call_sid, status='warm-transfer', _warm_transfer_conf=conf_name)

        logger.info(f"Warm transfer initiated: caller={call_sid[:16]} -> target={transfer_to} conf={conf_name}")
        return jsonify({
            "status": "consulting",
            "conference_name": conf_name,
            "transfer_call_sid": target_call_sid,
            "transfer_to": transfer_to,
        })

    except Exception as e:
        logger.error(f"Warm transfer initiate failed: {e}", exc_info=True)
        # Try to recover — redirect caller back to agent
        try:
            identity = f"agent_{location_id}"
            reconnect_url = f"{webhook_base}/voice/warm-transfer/reconnect-twiml?identity={quote(identity)}"
            twilio_provisioning.redirect_call_to_twiml(sub_sid, call_sid, reconnect_url)
        except Exception:
            pass
        delete_warm_transfer(call_sid)
        return jsonify({"error": "Warm transfer failed — recovered caller connection"}), 500


@call_history_bp.route('/voice/warm-transfer/complete', methods=['POST'])
@login_required
def warm_transfer_complete():
    """
    Complete a warm transfer: agent drops from Conference.
    Caller and transfer target continue talking.
    """
    data = request.json or {}
    call_sid = data.get('call_sid', '').strip()

    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    xfer = get_warm_transfer(call_sid)
    if not xfer:
        return jsonify({"error": "No warm transfer in progress for this call"}), 404

    if xfer.get('status') != 'consulting':
        return jsonify({"error": f"Warm transfer not in consulting state: {xfer.get('status')}"}), 400

    subscriber, voice_cfg, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    conf_name = xfer['conference_name']

    # Update state
    update_warm_transfer(call_sid, status='completed')
    update_active_call(call_sid, status='transferred')

    # Persist to call history DB
    try:
        update_call_history_status(call_sid, 'completed', 0)
    except Exception as e:
        logger.warning(f"Warm transfer complete DB persist failed for {call_sid}: {e}")

    logger.info(f"Warm transfer completed: agent dropped from conf={conf_name}, caller+target continue")

    # Clean up after a delay (let the conference settle)
    def _cleanup():
        time.sleep(5)
        delete_warm_transfer(call_sid)

    threading.Thread(target=_cleanup, daemon=True).start()

    return jsonify({"status": "completed", "conference_name": conf_name})


@call_history_bp.route('/voice/warm-transfer/cancel', methods=['POST'])
@login_required
def warm_transfer_cancel():
    """
    Cancel a warm transfer: hang up the transfer target, reconnect caller to agent.
    """
    data = request.json or {}
    call_sid = data.get('call_sid', '').strip()

    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    xfer = get_warm_transfer(call_sid)
    if not xfer:
        return jsonify({"error": "No warm transfer in progress for this call"}), 404

    subscriber, voice_cfg, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    conf_name = xfer['conference_name']
    target_call_sid = xfer.get('transfer_call_sid', '')
    location_id = xfer.get('location_id', '')
    identity = xfer.get('agent_identity', f"agent_{location_id}")

    host = request.host
    webhook_base = f"https://{host}"

    try:
        # Step 1: Hang up the transfer target
        if target_call_sid:
            try:
                twilio_provisioning.hangup_call(sub_sid, target_call_sid)
                logger.info(f"Warm transfer cancel: hung up target {target_call_sid}")
            except Exception as e:
                logger.warning(f"Failed to hang up transfer target {target_call_sid}: {e}")

        # Step 2: Redirect the caller back to agent's browser client
        from urllib.parse import quote
        reconnect_url = f"{webhook_base}/voice/warm-transfer/reconnect-twiml?identity={quote(identity)}"
        ok = twilio_provisioning.redirect_call_to_twiml(sub_sid, call_sid, reconnect_url)
        if not ok:
            logger.warning(f"Failed to reconnect caller {call_sid} to agent — call may have ended")

        # Clean up state
        update_warm_transfer(call_sid, status='canceled')
        update_active_call(call_sid, status='in-progress', _warm_transfer_conf='')
        delete_warm_transfer(call_sid)

        logger.info(f"Warm transfer canceled: caller {call_sid} reconnected to agent")
        return jsonify({"status": "canceled", "reconnected": ok})

    except Exception as e:
        logger.error(f"Warm transfer cancel failed: {e}", exc_info=True)
        delete_warm_transfer(call_sid)
        return jsonify({"error": "Cancel failed"}), 500


@call_history_bp.route('/voice/warm-transfer/status', methods=['GET'])
@login_required
def warm_transfer_status():
    """Check the current warm transfer state for a call."""
    call_sid = request.args.get('call_sid', '').strip()
    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    xfer = get_warm_transfer(call_sid)
    if not xfer:
        return jsonify({"status": "none"})

    return jsonify(xfer)


# ──────────────────────────────────────────────────────────────
# ROUTE: Call disposition
# ──────────────────────────────────────────────────────────────

@call_history_bp.route('/voice/call-disposition', methods=['POST'])
@jwt_or_session_required
def set_call_disposition():
    """Save a disposition for a completed call."""
    data = request.json or {}
    call_sid = data.get('call_sid', '')
    disposition = data.get('disposition', '')
    if not call_sid or not disposition:
        return jsonify({"error": "call_sid and disposition required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE call_history SET disposition = %s WHERE call_sid = %s
        """, (disposition, call_sid))
        conn.commit()
        cur.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        logger.error(f"Failed to save disposition: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)


# ──────────────────────────────────────────────────────────────
# ROUTE: Voicemail drop
# ──────────────────────────────────────────────────────────────
# "Voicemail drop" = the agent on a live call hits one button, the server
# redirects the Twilio call leg to play the agent's pre-recorded greeting
# then hangs up. Saves agents from repeating the same voicemail script 50
# times a day in high-volume outbound dialing.
#
# Flow:
#   1. Agent uploads greeting once in Voice Config — stored as base64 in
#      subscribers.voice_config.voicemail_greeting_data (see recordings.py).
#   2. On a live call, agent clicks VM in the dialer.
#   3. This endpoint verifies ownership + greeting presence, signs a short-
#      lived token (itsdangerous, 5-min TTL), builds TwiML pointing at a
#      PUBLIC serve endpoint Twilio can fetch without auth, and calls
#      client.calls(call_sid).update(twiml=...).
#   4. Twilio fetches the greeting audio and plays it to the recipient,
#      then hangs up. The agent's client leg disconnects normally.


@call_history_bp.route('/voice/voicemail-drop', methods=['POST'])
@login_required
def voicemail_drop():
    """Play the agent's pre-recorded voicemail greeting into a live call, then hang up."""
    data = request.json or {}
    call_sid = (data.get('call_sid') or '').strip()
    if not call_sid:
        return jsonify({"error": "call_sid required"}), 400

    if not call_exists(call_sid):
        return jsonify({"error": "Call not found or already ended"}), 404
    if not _verify_call_ownership(call_sid):
        return jsonify({"error": "Call not found or already ended"}), 404

    call_info = get_active_call(call_sid)
    if call_info is None:
        return jsonify({"error": "Call not found or already ended"}), 404
    if call_info.get('status') in ('completed', 'failed', 'transferred', 'no-answer', 'canceled'):
        return jsonify({"error": f"Call already in terminal state: {call_info.get('status')}"}), 400

    # Load subscriber + verify greeting + verify provisioning
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT location_id, voice_config FROM subscribers WHERE email = %s",
            (current_user.email,)
        )
        row = cur.fetchone()
        cur.close()
    finally:
        return_db_connection(conn)

    if not row:
        return jsonify({"error": "User not found"}), 404

    vc = row.get('voice_config') or {}
    if isinstance(vc, str):
        vc = json.loads(vc)

    if not vc.get('voicemail_greeting_data'):
        return jsonify({
            "error": "No voicemail greeting uploaded. Upload one in Voice Config \u2192 Voicemail Greeting first."
        }), 400

    sub_sid = vc.get('twilio_sub_account_sid', '')
    sub_auth = vc.get('twilio_sub_account_auth_token', '')
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    # Sign a short-lived token so Twilio can fetch the greeting audio without auth.
    try:
        from itsdangerous import URLSafeTimedSerializer
        secret = os.getenv('SESSION_SECRET') or os.getenv('SECRET_KEY') or 'dev-insecure'
        serializer = URLSafeTimedSerializer(secret, salt='voicemail-drop-v1')
        token = serializer.dumps(row['location_id'])
    except Exception as e:
        logger.error(f"Voicemail drop: failed to sign token: {e}")
        return jsonify({"error": "Internal error"}), 500

    # Absolute URL Twilio can fetch. Use the request host so it works on any
    # deployment without env config.
    scheme = 'https' if request.is_secure or request.headers.get('X-Forwarded-Proto') == 'https' else 'http'
    audio_url = f"{scheme}://{request.host}/voice/voicemail-greeting/public/{token}"

    from xml.sax.saxutils import escape as xml_escape
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Play>{xml_escape(audio_url)}</Play>'
        '<Hangup/>'
        '</Response>'
    )

    try:
        client = twilio_provisioning.get_sub_account_client_native(sub_sid, sub_auth)
        client.calls(call_sid).update(twiml=twiml)
        update_active_call(call_sid, status='voicemail-dropped')
        try:
            update_call_history_status(call_sid, 'completed', 0)
        except Exception as persist_err:
            logger.warning(f"Voicemail drop DB persist failed for {call_sid}: {persist_err}")
        logger.info(f"Voicemail drop fired on call {call_sid}")
        return jsonify({"status": "dropped", "call_sid": call_sid})
    except Exception as e:
        logger.error(f"Voicemail drop failed for {call_sid}: {e}")
        return jsonify({"error": f"Voicemail drop failed: {e}"}), 500


# ──────────────────────────────────────────────────────────────
# AI Auto-Callback Detection
# ──────────────────────────────────────────────────────────────

def _analyze_callback_from_transcript(call_sid, transcript, location_id, contact_id, timezone_str='America/New_York'):
    """
    Analyze a call transcript for callback requests using xAI Grok.
    Detects phrases like "call me back at 5", "try me tomorrow morning", etc.
    If detected, sets disposition to 'callback' and stores callback_at timestamp.
    """
    if not transcript:
        return None

    # Build transcript text
    text_parts = []
    for turn in transcript:
        role = turn.get('role', 'unknown')
        text = turn.get('text', '')
        if text:
            if role == 'call_recording':
                text_parts.append(f"Recording: {text}")
            else:
                label = 'Lead' if role == 'lead' else 'Agent'
                text_parts.append(f"{label}: {text}")
    transcript_text = '\n'.join(text_parts)

    if len(transcript_text) < 10:
        return None

    xai_key = os.getenv("XAI_API_KEY_VOICE") or os.getenv("XAI_API_KEY")
    if not xai_key:
        logger.warning("Auto-callback: XAI_API_KEY not configured")
        return None

    # Quick AI analysis — single micro-prompt
    try:
        try:
            tz = pytz.timezone(timezone_str)
        except Exception:
            tz = pytz.timezone('America/New_York')
        now = datetime.now(tz)
        now_str = now.strftime('%Y-%m-%d %H:%M %Z (%A)')

        prompt = f"""Analyze this phone call transcript for callback requests.
Current time: {now_str}

TRANSCRIPT:
{transcript_text[:3000]}

Does the lead request a callback? Look for phrases like:
- "call me back at 5" / "try me at 3pm" / "call me later"
- "tomorrow morning" / "next week" / "after lunch"
- "I'm busy right now, can you call later?"
- Any specific time/day mentioned for a return call

Respond with JSON only (no markdown):
{{"callback_requested": true/false, "callback_time": "YYYY-MM-DD HH:MM" or null, "confidence": "high"/"medium"/"low", "quote": "exact words from transcript" or null}}

If no specific time is given but they want a callback (e.g., "call me later"), estimate a reasonable time.
If callback_requested is false, set callback_time to null."""

        from free_llm import get_free_llm
        client, _model = get_free_llm("fast")
        if not client:
            return None
        resp = client.chat.completions.create(
            model=_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0,
        )
        raw = (resp.choices[0].message.content or '').strip()

        # Parse JSON response
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            logger.debug(f"Auto-callback: no JSON in response for {call_sid}")
            return None

        result = json.loads(json_match.group())

        if not result.get('callback_requested'):
            logger.debug(f"Auto-callback: no callback detected for {call_sid}")
            return None

        callback_time_str = result.get('callback_time')
        confidence = result.get('confidence', 'low')
        quote = result.get('quote', '')

        if confidence == 'low':
            logger.debug(f"Auto-callback: low confidence for {call_sid}, skipping")
            return None

        # Parse callback time (stored as naive TIMESTAMP — subscriber's local time)
        callback_at = None
        if callback_time_str:
            try:
                callback_at = datetime.strptime(callback_time_str, '%Y-%m-%d %H:%M')
            except ValueError:
                logger.warning(f"Auto-callback: could not parse time '{callback_time_str}' for {call_sid}")

        # Save to DB
        conn = get_db_connection()
        if not conn:
            logger.warning(f"Auto-callback: no DB connection for {call_sid}, result not persisted")
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE call_history
                    SET disposition = 'callback',
                        callback_at = %s
                    WHERE call_sid = %s
                """, (callback_at, call_sid))
                conn.commit()
                cur.close()
                logger.info(f"Auto-callback scheduled for {call_sid}: {callback_at} (confidence={confidence}, quote='{quote}')")
            except Exception as e:
                logger.error(f"Auto-callback DB save failed for {call_sid}: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                return_db_connection(conn)

        return {
            "callback_at": callback_at.isoformat() if callback_at else None,
            "confidence": confidence,
            "quote": quote,
        }

    except Exception as e:
        logger.error(f"Auto-callback AI analysis failed for {call_sid}: {e}")
        return None


@call_history_bp.route('/voice/scheduled-callbacks', methods=['GET'])
@login_required
def get_scheduled_callbacks():
    """Return upcoming scheduled callbacks for the current user's contacts."""
    conn = get_db_connection()
    if not conn:
        return jsonify([])
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify([])
        location_id = row['location_id']

        cur.execute("""
            SELECT call_sid, contact_id, contact_name, phone, callback_at, disposition
            FROM call_history
            WHERE location_id = %s AND callback_at IS NOT NULL AND callback_at > NOW() - INTERVAL '1 day'
            ORDER BY callback_at ASC
            LIMIT 50
        """, (location_id,))
        from datetime import datetime as _dt, timezone as _tz
        _now_naive = _dt.now()  # Server-local naive time (matches TIMESTAMP column)
        callbacks = []
        for r in cur.fetchall():
            cb_at = r['callback_at']
            callbacks.append({
                "call_sid": r['call_sid'],
                "contact_id": r['contact_id'],
                "contact_name": r['contact_name'],
                "phone": r['phone'],
                "callback_at": cb_at.isoformat() if cb_at else None,
                "is_past": cb_at < _now_naive if cb_at else False,
            })
        cur.close()
        return jsonify(callbacks)
    except Exception as e:
        logger.error(f"Failed to fetch scheduled callbacks: {e}")
        return jsonify([])
    finally:
        return_db_connection(conn)


# ──────────────────────────────────────────────────────────────
# ROUTE: Call history API
# ──────────────────────────────────────────────────────────────

@call_history_bp.route('/voice/call-history', methods=['GET'])
@login_required
def get_call_history():
    """Fetch call history for the current user."""
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = int(request.args.get('offset', 0))

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']

        cur.execute("""
            SELECT id, contact_id, contact_name, phone, direction, call_sid,
                   status, duration, recording_url, recording_sid, transcript,
                   started_at, ended_at, created_at,
                   COALESCE(disposition, '') as disposition,
                   COALESCE(stir_status, '') as stir_status,
                   COALESCE(ring_confirmed, FALSE) as ring_confirmed,
                   pdd_ms, quality_tags, quality_metrics
            FROM call_history
            WHERE location_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (location_id, limit, offset))
        rows = cur.fetchall()
        cur.close()

        calls = []
        for r in rows:
            call = dict(r)
            # Convert timestamps to ISO strings
            for ts_field in ('started_at', 'ended_at', 'created_at'):
                if call.get(ts_field):
                    call[ts_field] = call[ts_field].isoformat()
            calls.append(call)

        return jsonify({"calls": calls, "total": len(calls)})
    except Exception as e:
        logger.error(f"Failed to fetch call history: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)


@call_history_bp.route('/voice/voicemails', methods=['GET'])
@login_required
def get_voicemails():
    """
    Fetch inbound voicemails — calls that came IN and have recordings.
    These are calls where the caller left a message because the agent
    didn't answer. Sorted newest first.
    """
    limit = min(int(request.args.get('limit', 50)), 200)
    offset = int(request.args.get('offset', 0))

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']

        cur.execute("""
            SELECT id, contact_id, contact_name, phone, direction, call_sid,
                   status, duration, recording_url, recording_sid, transcript,
                   started_at, ended_at, created_at,
                   COALESCE(disposition, '') as disposition,
                   COALESCE(stir_status, '') as stir_status
            FROM call_history
            WHERE location_id = %s
              AND recording_url IS NOT NULL
              AND recording_url != ''
              AND (
                  direction = 'inbound'
                  OR disposition = 'left_voicemail'
                  OR status IN ('no-answer', 'busy')
              )
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (location_id, limit, offset))
        rows = cur.fetchall()

        # Count unread (no transcript yet = not listened to)
        cur.execute("""
            SELECT COUNT(*) as cnt FROM call_history
            WHERE location_id = %s
              AND recording_url IS NOT NULL AND recording_url != ''
              AND (direction = 'inbound' OR disposition = 'left_voicemail'
                   OR status IN ('no-answer', 'busy'))
              AND (transcript IS NULL OR transcript = '[]'::jsonb)
        """, (location_id,))
        unread_row = cur.fetchone()
        unread_count = unread_row['cnt'] if unread_row else 0

        cur.close()

        voicemails = []
        for r in rows:
            vm = dict(r)
            for ts_field in ('started_at', 'ended_at', 'created_at'):
                if vm.get(ts_field):
                    vm[ts_field] = vm[ts_field].isoformat()
            # Extract transcript preview text
            tx = vm.get('transcript')
            if tx:
                try:
                    parsed = json.loads(tx) if isinstance(tx, str) else tx
                    if isinstance(parsed, list) and parsed:
                        vm['transcript_preview'] = parsed[0].get('text', '')[:120]
                    else:
                        vm['transcript_preview'] = ''
                except Exception:
                    vm['transcript_preview'] = ''
            else:
                vm['transcript_preview'] = ''
            vm['is_new'] = not bool(tx)
            voicemails.append(vm)

        logger.info(f"Voicemails: Returned {len(voicemails)} for {location_id} ({unread_count} unread)")
        return jsonify({"voicemails": voicemails, "total": len(voicemails), "unread": unread_count})
    except Exception as e:
        logger.error(f"Failed to fetch voicemails: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)


# ── Voice Insights list (bulk summary for Insights app) ──

@call_history_bp.route('/voice/call-insights', methods=['GET'])
@login_required
def get_call_insights_list():
    """
    Return calls that have insights data — lightweight summary for the Insights app.

    Returns: call_sid, contact_name, phone, direction, status, duration,
    created_at, pdd_ms, quality_tags, plus extracted insights fields
    (last_sip_response, disconnected_by, call_state, from/to carrier, trust).
    """
    limit = min(int(request.args.get('limit', 100)), 200)
    offset = int(request.args.get('offset', 0))

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']

        cur.execute("""
            SELECT call_sid, contact_id, contact_name, phone, direction,
                   status, duration, created_at, started_at,
                   COALESCE(stir_status, '') as stir_status,
                   pdd_ms, quality_tags, insights
            FROM call_history
            WHERE location_id = %s AND insights IS NOT NULL
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (location_id, limit, offset))
        rows = cur.fetchall()
        cur.close()

        calls = []
        for r in rows:
            ins = r['insights'] or {}
            if isinstance(ins, str):
                try:
                    ins = json.loads(ins)
                except Exception:
                    ins = {}
            calls.append({
                "call_sid": r['call_sid'],
                "contact_id": r.get('contact_id', ''),
                "contact_name": r.get('contact_name') or r.get('phone') or 'Unknown',
                "phone": r.get('phone', ''),
                "direction": r.get('direction', 'outbound'),
                "status": r.get('status', ''),
                "duration": r.get('duration'),
                "created_at": r['created_at'].isoformat() if r.get('created_at') else None,
                "started_at": r['started_at'].isoformat() if r.get('started_at') else None,
                "stir_status": r.get('stir_status', ''),
                "pdd_ms": r.get('pdd_ms'),
                "quality_tags": r.get('quality_tags') or [],
                "last_sip_response": ins.get('_last_sip_response'),
                "disconnected_by": ins.get('_disconnected_by'),
                "call_state": ins.get('call_state'),
                "call_type": ins.get('call_type'),
                "from_carrier": (ins.get('from_info') or {}).get('carrier'),
                "to_carrier": (ins.get('to_info') or {}).get('carrier'),
                "trust": ins.get('trust') or {},
                "carrier_edge": ins.get('carrier_edge') or {},
            })

        return jsonify({"calls": calls, "total": len(calls)})
    except Exception as e:
        logger.error(f"Failed to fetch call insights list: {e}")
        return jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)


# ── Voice Insights per-call detail ──

@call_history_bp.route('/voice/call-insights/<call_sid>', methods=['GET'])
@login_required
def get_call_insights_detail(call_sid):
    """
    Return Voice Insights data for a specific call.

    Includes: PDD, SIP response, carrier edge metrics, quality tags,
    call state/type, from/to carrier info, trust data.

    Returns {} if insights not yet available (fetched ~90s after call ends).
    """
    if not _verify_call_ownership(call_sid):
        return jsonify({"error": "Not found"}), 404

    from voice.insights import get_call_insights
    data = get_call_insights(call_sid)
    if not data:
        return jsonify({})

    # Return the full insights + extracted fields
    insights = data.get('insights') or {}
    return jsonify({
        "call_sid": call_sid,
        "pdd_ms": data.get('pdd_ms'),
        "quality_tags": data.get('quality_tags', []),
        "call_state": insights.get('call_state'),
        "call_type": insights.get('call_type'),
        "last_sip_response": insights.get('_last_sip_response'),
        "disconnected_by": insights.get('_disconnected_by'),
        "carrier_edge": insights.get('carrier_edge', {}),
        "from_carrier": (insights.get('from_info') or {}).get('carrier'),
        "to_carrier": (insights.get('to_info') or {}).get('carrier'),
        "trust": insights.get('trust', {}),
        "properties": insights.get('properties', {}),
        "tags": insights.get('tags', []),
    })
