# voice/screen_share.py — Native WebRTC Screen Sharing
#
# Redis-backed session management + Flask routes for screen sharing.
# Video flows P2P via WebRTC between agent browser and lead's phone.
# Our server only handles signaling (SDP/ICE exchange via WebSocket in voice_server.py).
#
# Flow:
#   1. Agent clicks "Screen" → getDisplayMedia() captures screen
#   2. POST /voice/share/start → creates Redis session, SMSes link to lead
#   3. Agent + viewer connect to WS /voice/share/signal/{session_id}
#   4. SDP offer/answer + ICE candidates exchanged → P2P video stream
#   5. Agent clicks "Stop" → session cleaned up

import os
import json
import logging
import secrets
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, render_template
from flask_login import login_required, current_user

from db import get_db_connection, return_db_connection
from extensions import ensure_redis

logger = logging.getLogger(__name__)

screen_share_bp = Blueprint('screen_share', __name__)

SESSION_TTL = 600  # 10 minutes — auto-cleanup via Redis TTL


# ── Redis Session Management ─────────────────────────────────────────────────

def _redis():
    """Get Redis connection, ensuring it's alive."""
    return ensure_redis()


def create_share_session(location_id, agent_email, contact_phone, contact_name):
    """Create a new screen share session in Redis. Returns session_id."""
    r = _redis()
    if not r:
        return None
    session_id = secrets.token_urlsafe(16)
    data = {
        "location_id": location_id,
        "agent_email": agent_email,
        "contact_phone": contact_phone,
        "contact_name": contact_name,
        "status": "waiting",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "agent_connected": False,
        "viewer_connected": False,
    }
    r.set(f"screenshare:{session_id}", json.dumps(data), ex=SESSION_TTL)
    logger.info(f"[ScreenShare] Session created: {session_id} for {contact_name} ({contact_phone})")
    return session_id


def get_share_session(session_id):
    """Get session data from Redis. Returns dict or None."""
    r = _redis()
    if not r:
        return None
    raw = r.get(f"screenshare:{session_id}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def update_share_session(session_id, **fields):
    """Update specific fields on a session, preserving TTL."""
    r = _redis()
    if not r:
        return False
    key = f"screenshare:{session_id}"
    raw = r.get(key)
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    data.update(fields)
    ttl = r.ttl(key)
    if ttl and ttl > 0:
        r.set(key, json.dumps(data), ex=ttl)
    else:
        r.set(key, json.dumps(data), ex=SESSION_TTL)
    return True


def delete_share_session(session_id):
    """Delete a session from Redis."""
    r = _redis()
    if r:
        r.delete(f"screenshare:{session_id}")


# ── Flask Routes ──────────────────────────────────────────────────────────────

@screen_share_bp.route('/voice/share/start', methods=['POST'])
@login_required
def share_start():
    """
    Start a screen share session. Creates Redis session, SMSes link to lead.
    Body: {contact_phone, contact_name?, contact_id?}
    Returns: {session_id, share_url}
    """
    data = request.json or {}
    contact_phone = data.get('contact_phone', '')
    contact_name = data.get('contact_name', '')

    if not contact_phone:
        return jsonify({"error": "Contact phone number is required"}), 400

    location_id = current_user.location_id
    if not location_id:
        return jsonify({"error": "No location configured"}), 400

    # Create session
    session_id = create_share_session(location_id, current_user.email, contact_phone, contact_name)
    if not session_id:
        return jsonify({"error": "Could not create screen share session — Redis unavailable"}), 500

    # Build the viewer URL
    host = request.host
    scheme = "https" if not host.startswith("localhost") else "http"
    share_url = f"{scheme}://{host}/share/{session_id}"

    # SMS the link to the lead
    sms_sent = False
    try:
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
                row = cur.fetchone()
                cur.close()
                if row:
                    vc = (row.get('voice_config') or {}) if isinstance(row, dict) else {}
                    sub_sid = vc.get('twilio_sub_account_sid', '')
                    from_num = vc.get('twilio_phone_number', '')
                    if sub_sid and from_num:
                        from twilio_sms import send_sms_via_twilio
                        msg = f"View my screen here: {share_url}"
                        ok, fail_reason, _ = send_sms_via_twilio(sub_sid, from_num, contact_phone, msg)
                        sms_sent = ok
                        if not ok:
                            logger.warning(f"[ScreenShare] SMS failed for {session_id}: {fail_reason}")
            finally:
                return_db_connection(conn)
    except Exception as e:
        logger.warning(f"[ScreenShare] SMS send error for {session_id}: {e}")

    return jsonify({
        "session_id": session_id,
        "share_url": share_url,
        "sms_sent": sms_sent,
    })


@screen_share_bp.route('/voice/share/ice-servers', methods=['GET'])
@login_required
def share_ice_servers():
    """Return TURN/STUN ICE server credentials for WebRTC peer connections."""
    try:
        from twilio_provisioning import generate_turn_credentials
        # Try subscriber's sub-account first for proper billing
        conn = get_db_connection()
        sub_sid = None
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
                row = cur.fetchone()
                cur.close()
                if row:
                    vc = (row.get('voice_config') or {}) if isinstance(row, dict) else {}
                    sub_sid = vc.get('twilio_sub_account_sid', '')
            finally:
                return_db_connection(conn)

        ice_servers = generate_turn_credentials(sub_sid if sub_sid else None)
        return jsonify({"ice_servers": ice_servers})
    except Exception as e:
        logger.error(f"[ScreenShare] ICE server generation failed: {e}")
        # Fallback to public STUN
        return jsonify({"ice_servers": [{"urls": "stun:stun.l.google.com:19302"}]})


@screen_share_bp.route('/voice/share/end', methods=['POST'])
@login_required
def share_end():
    """End an active screen share session."""
    data = request.json or {}
    session_id = data.get('session_id', '')
    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    session = get_share_session(session_id)
    if not session:
        return jsonify({"error": "Session not found or expired"}), 404

    # Only the agent who created it can end it
    if session.get('agent_email') != current_user.email:
        return jsonify({"error": "Not authorized"}), 403

    update_share_session(session_id, status="ended")
    logger.info(f"[ScreenShare] Session ended: {session_id}")
    return jsonify({"success": True})


# ── Public viewer route (no login required) ───────────────────────────────────

@screen_share_bp.route('/share/<session_id>')
def share_viewer(session_id):
    """
    Public viewer page — lead opens this link on their phone to see the agent's screen.
    No login required. Session must exist in Redis.
    """
    session = get_share_session(session_id)
    if not session:
        return render_template('share_expired.html'), 404

    # Determine WebSocket host (FastAPI voice server)
    voice_wss_host = os.getenv('VOICE_WSS_HOST', '')
    if not voice_wss_host:
        voice_wss_host = request.host

    # Generate TURN/STUN credentials for the viewer (embedded in page, ~24h TTL)
    ice_servers = [{"urls": "stun:stun.l.google.com:19302"}]
    try:
        from twilio_provisioning import generate_turn_credentials
        ice_servers = generate_turn_credentials()
    except Exception as e:
        logger.warning(f"[ScreenShare] TURN credentials for viewer failed (using STUN fallback): {e}")

    return render_template('share.html',
                           session_id=session_id,
                           agent_name=session.get('contact_name', ''),
                           voice_wss_host=voice_wss_host,
                           ice_servers=ice_servers)
