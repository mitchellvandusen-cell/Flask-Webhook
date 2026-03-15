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


import twilio_provisioning
from db import get_db_connection, return_db_connection, log_webhook_event
from openai import OpenAI
from voice.audio import XAI_API_KEY
from voice.call_state import active_calls, transfer_requests, call_listeners, _twilio_hangup, _twilio_transfer
from voice.call_history_helpers import update_call_history_status
from voice.helpers import _get_current_subscriber_voice, _verify_call_ownership

logger = logging.getLogger("voice_bridge.call_history")

call_history_bp = Blueprint('voice_call_history', __name__)


# ──────────────────────────────────────────────────────────────
# ROUTE: Poll call status for the dialer queue
# ──────────────────────────────────────────────────────────────

@call_history_bp.route('/voice/call-status/<call_sid>', methods=['GET'])
@login_required
def get_call_status(call_sid):
    """Poll call status for the dialer queue."""
    if call_sid in active_calls:
        if not _verify_call_ownership(call_sid):
            return jsonify({"status": "unknown"}), 404
        info = active_calls[call_sid]
        # For terminal states, mark for cleanup but don't delete yet (allow re-polls)
        if info["status"] in ("completed", "busy", "no-answer", "failed", "canceled", "transferred"):
            poll_count = info.get('_terminal_polls', 0) + 1
            info['_terminal_polls'] = poll_count
            # Clean up after 20 polls of a terminal state (gives frontend plenty of time)
            if poll_count >= 20:
                status_copy = dict(info)
                del active_calls[call_sid]
                return jsonify(status_copy)
        logger.debug(f"Poll {call_sid[:16]}: status={info.get('status')}")
        return jsonify(info)
    logger.debug(f"Poll {call_sid[:16]}: not found in active_calls")
    return jsonify({"status": "unknown"}), 404


# ──────────────────────────────────────────────────────────────
# ROUTE: Hang up an active call from the dialer UI
# ──────────────────────────────────────────────────────────────

@call_history_bp.route('/voice/hangup', methods=['POST'])
@login_required
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
    was_transferred = (call_sid in active_calls and
                       active_calls[call_sid].get('status') == 'transferred')

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

    if call_sid in active_calls:
        active_calls[call_sid]['status'] = 'completed'
    transfer_requests.pop(call_sid, None)

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
@login_required
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
    if call_sid not in active_calls:
        return jsonify({"error": "Call not found or already ended"}), 404

    call_info = active_calls[call_sid]
    call_location = call_info.get('_location_id', '')
    if call_location and call_location != location_id:
        return jsonify({"error": "Call not found or already ended"}), 404

    if call_info.get('status') in ('completed', 'failed', 'transferred', 'no-answer'):
        return jsonify({"error": f"Call already in terminal state: {call_info.get('status')}"}), 400

    host = request.host

    if use_voip and location_id:
        # VoIP intercept: redirect call to browser client
        identity = f"agent_{location_id}"
        target = f"client:{identity}"
        logger.info(f"Takeover (VoIP): redirecting call {call_sid} to browser client={identity}")

        # Signal the WebSocket bridge to stop the AI audio loop
        transfer_requests[call_sid] = {
            'type': 'takeover',
            'target': target,
            'reason': 'Agent initiated VoIP intercept',
        }

        # Redirect the call to TwiML that dials the browser client
        try:
            client = twilio_provisioning.get_sub_account_client(sub_sid)
            client.calls(call_sid).update(
                url=f"https://{host}/voice/intercept-twiml?identity={identity}",
                method="POST",
            )
            active_calls[call_sid]['status'] = 'transferred'
            logger.info(f"Takeover (VoIP): call {call_sid} redirected to {identity}")
            return jsonify({"status": "transferred", "call_sid": call_sid, "target": "Browser (VoIP)"})
        except Exception as e:
            logger.error(f"Takeover (VoIP): redirect FAILED for call {call_sid}: {e}")
            transfer_requests.pop(call_sid, None)
            return jsonify({"error": f"Intercept failed: {e}"}), 400
    else:
        # Phone intercept: transfer to agent's phone number
        target = data.get('target') or voice_cfg.get('transfer_number', '')
        if not target:
            # No VoIP, no transfer number — at minimum STOP the AI by hanging up
            logger.info(f"Takeover (hangup): no VoIP/transfer number, stopping AI call {call_sid}")
            transfer_requests[call_sid] = {
                'type': 'takeover',
                'target': '',
                'reason': 'Agent stopped AI (no VoIP/transfer)',
            }
            try:
                _twilio_hangup(call_sid, sub_sid)
            except Exception as e:
                logger.warning(f"Takeover hangup failed: {e}")
            if call_sid in active_calls:
                active_calls[call_sid]['status'] = 'canceled'
            return jsonify({"status": "stopped", "call_sid": call_sid,
                            "target": "AI stopped (call ended — set up VoIP or Transfer Number to take over live)"})

        # Normalize target
        if not target.startswith('+'):
            target = '+1' + target.lstrip('1') if len(target.replace('-','').replace(' ','')) <= 10 else '+' + target

        logger.info(f"Takeover (phone): executing transfer for call {call_sid} -> {target}")

        # Signal the WebSocket bridge to stop the AI audio loop
        transfer_requests[call_sid] = {
            'type': 'takeover',
            'target': target,
            'reason': 'Agent initiated live takeover',
        }

        # Transfer the live call — Twilio automatically stops the media stream
        transfer_ok = _twilio_transfer(call_sid, sub_sid, target, f"https://{host}")
        if transfer_ok:
            logger.info(f"Takeover (phone): call {call_sid} transferred to {target}")
            active_calls[call_sid]['status'] = 'transferred'
            return jsonify({"status": "transferred", "call_sid": call_sid, "target": target})
        else:
            logger.error(f"Takeover (phone): transfer FAILED for call {call_sid} -> {target}")
            transfer_requests.pop(call_sid, None)
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
    if call_sid not in active_calls:
        return jsonify({"error": "Call not found or already ended"}), 404

    if not _verify_call_ownership(call_sid):
        return jsonify({"error": "Call not found or already ended"}), 404

    call_info = active_calls[call_sid]
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
        active_calls[call_sid]['status'] = 'transferred'
        # Persist transfer status to DB
        try:
            update_call_history_status(call_sid, 'completed', 0)
        except Exception as e:
            logger.warning(f"Transfer DB persist failed for {call_sid}: {e}")
        logger.info(f"Live transfer: call {call_sid} -> {transfer_to}")
        return jsonify({"status": "transferred", "call_sid": call_sid, "transfer_to": transfer_to})

    return jsonify({"error": "Transfer failed — call may have ended"}), 400


# ──────────────────────────────────────────────────────────────
# ROUTE: Call disposition
# ──────────────────────────────────────────────────────────────

@call_history_bp.route('/voice/call-disposition', methods=['POST'])
@login_required
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
        return jsonify({"error": str(e)}), 500
    finally:
        return_db_connection(conn)


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

    xai_key = os.getenv("XAI_API_KEY")
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

        client = OpenAI(api_key=xai_key, base_url="https://api.x.ai/v1")
        resp = client.chat.completions.create(
            model="grok-4-1-fast-non-reasoning",
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
                   pdd_ms, quality_tags
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
        return jsonify({"error": str(e)}), 500
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
        return jsonify({"error": str(e)}), 500
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
