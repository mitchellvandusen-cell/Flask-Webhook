import base64
import json
import os
import logging

from flask import Blueprint, request, Response, jsonify
from flask_login import current_user
from ghl_auth import jwt_or_session_required
import requests as http_requests
import httpx

import twilio_provisioning
from blueprints.team import require_permission
from db import get_db_connection, return_db_connection
from openai import OpenAI
from voice.audio import XAI_API_KEY
from voice.helpers import _get_current_subscriber_voice
from voice.call_history_helpers import save_call_transcript

logger = logging.getLogger("voice_bridge.recordings")

recordings_bp = Blueprint('voice_recordings', __name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")


@recordings_bp.route('/voice/recording-status', methods=['POST'])
def recording_status_callback():
    """Twilio posts recording status events here as form data."""
    call_sid = request.values.get('CallSid', '')
    recording_sid = request.values.get('RecordingSid', '')
    recording_url = request.values.get('RecordingUrl', '')
    recording_status = request.values.get('RecordingStatus', '')
    recording_duration = request.values.get('RecordingDuration', '0')

    logger.info(f"Recording callback: SID={call_sid} rec={recording_sid} status={recording_status} dur={recording_duration}s")

    if recording_status == 'completed' and (recording_url or recording_sid):
        # Use our proxy URL for Twilio recordings
        store_url = f"/voice/recording/{recording_sid}" if recording_sid else recording_url

        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE call_history
                    SET recording_url = %s, recording_sid = %s, duration = COALESCE(NULLIF(duration, 0), %s)
                    WHERE call_sid = %s
                """, (store_url, recording_sid, int(recording_duration or 0), call_sid))
                conn.commit()
                cur.close()
                logger.info(f"Recording saved for call {call_sid}: {store_url}")
            except Exception as e:
                logger.error(f"Failed to save recording: {e}")
                conn.rollback()
            finally:
                return_db_connection(conn)

    return '', 204


@recordings_bp.route('/voice/transcription', methods=['POST'])
def transcription_webhook():
    """
    Twilio posts transcription events here.
    Accumulates transcript segments and persists them to call_history on call end.
    """
    call_sid   = request.values.get('CallSid', '')
    transcript = request.values.get('TranscriptionText', '')
    transcription_status = request.values.get('TranscriptionStatus', 'completed')

    if not call_sid or not transcript:
        return '', 204

    logger.info(f"📝 Transcription [{transcription_status}] {call_sid}: {transcript[:80]}")

    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            # Append to existing transcript stored as JSONB array in call_history
            cur.execute("""
                UPDATE call_history
                SET transcript = COALESCE(transcript::jsonb, '[]'::jsonb) || %s::jsonb
                WHERE call_sid = %s
            """, (json.dumps([{"role": "auto", "text": transcript}]), call_sid))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.warning(f"Transcription save failed for {call_sid}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            return_db_connection(conn)

    return '', 204


@recordings_bp.route('/voice/recording/<recording_sid>', methods=['GET'])
@jwt_or_session_required
@require_permission('can_view_call_recordings')
def stream_recording(recording_sid):
    """
    Proxy a Twilio recording as an MP3 download. Fetches with Twilio auth
    so the browser never sees expiring pre-signed S3 URLs.
    """
    # Try to find the recording's actual sub-account from call_history
    # (agency owners viewing other agents' recordings need the agent's credentials)
    rec_sub_sid = None
    rec_auth = None
    try:
        from db import get_db_connection, return_db_connection
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT ch.location_id, s.voice_config
                    FROM call_history ch
                    JOIN subscribers s ON ch.location_id = s.location_id
                    WHERE ch.recording_sid = %s OR ch.recording_url LIKE %s
                    LIMIT 1
                """, (recording_sid, f'%{recording_sid}%'))
                row = cur.fetchone()
                cur.close()
                if row:
                    vc = (row.get('voice_config') or {}) if isinstance(row, dict) else {}
                    rec_sub_sid = vc.get('twilio_sub_account_sid', '')
                    rec_auth = vc.get('twilio_sub_account_auth_token', '')
            finally:
                return_db_connection(conn)
    except Exception as e:
        logger.debug(f"Recording owner lookup failed (non-fatal): {e}")

    # Fall back to current user's sub-account if lookup failed
    if not rec_sub_sid:
        subscriber, vc, sub_sid = _get_current_subscriber_voice()
        if not sub_sid:
            return jsonify({"error": "No Twilio account configured"}), 400
        rec_sub_sid = sub_sid
        rec_auth = vc.get('twilio_sub_account_auth_token', '')

    # Build URL using the recording owner's sub-account
    mp3_url = twilio_provisioning.get_recording_url(rec_sub_sid, recording_sid)

    try:
        # Auth with the recording's sub-account credentials, fall back to master
        auth_sid = rec_sub_sid if rec_auth else twilio_provisioning.TWILIO_ACCOUNT_SID
        auth_tok = rec_auth if rec_auth else twilio_provisioning.TWILIO_AUTH_TOKEN
        tw_resp = http_requests.get(
            mp3_url,
            auth=(auth_sid, auth_tok),
            stream=True,
            timeout=30,
        )
        if tw_resp.status_code != 200:
            logger.error(f"Twilio recording fetch failed: {tw_resp.status_code} for {recording_sid}")
            return jsonify({"error": "Recording not available"}), tw_resp.status_code

        # Stream the audio back to the browser
        def generate():
            for chunk in tw_resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        headers = {}
        # Only force download when ?dl=1 is passed (download button)
        if request.args.get('dl'):
            headers['Content-Disposition'] = f'attachment; filename="recording-{recording_sid}.mp3"'

        return Response(
            generate(),
            content_type=tw_resp.headers.get('Content-Type', 'audio/mpeg'),
            headers=headers,
        )
    except Exception as e:
        logger.error(f"Failed to proxy recording {recording_sid}: {e}")
        return jsonify({"error": "Failed to fetch recording"}), 500


# ──────────────────────────────────────────────────────────────
@recordings_bp.route('/voice/transcribe-recording', methods=['POST'])
@jwt_or_session_required
def transcribe_recording():
    """
    Trigger on-demand transcription for a call recording.
    Body: { "call_sid": "CA...", "recording_url": "https://..." }
    Downloads the audio and sends to xAI Whisper, saves result to call_history.
    """
    data = request.get_json(silent=True) or {}
    call_sid = (data.get('call_sid') or '').strip()
    recording_url = (data.get('recording_url') or '').strip()

    if not call_sid or not recording_url:
        return jsonify({"error": "call_sid and recording_url are required"}), 400

    # Verify this call belongs to the logged-in user
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        sub_row = cur.fetchone()
        if not sub_row or not sub_row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = sub_row['location_id']

        cur.execute(
            "SELECT call_sid, transcript FROM call_history WHERE call_sid = %s AND location_id = %s",
            (call_sid, location_id)
        )
        call_row = cur.fetchone()
        if not call_row:
            return jsonify({"error": "Call not found"}), 404
        if call_row['transcript']:
            # Already transcribed — return existing
            try:
                existing = json.loads(call_row['transcript']) if isinstance(call_row['transcript'], str) else call_row['transcript']
            except Exception:
                existing = []
            return jsonify({"transcript": existing, "cached": True})
    except Exception as e:
        logger.error(f"transcribe_recording DB check failed: {e}")
        return jsonify({"error": "Database error"}), 500
    finally:
        return_db_connection(conn)

    # Download audio from Twilio (mp3 format)
    try:
        mp3_url = recording_url if recording_url.endswith('.mp3') else recording_url.rstrip('/') + '.mp3'
        audio_resp = httpx.get(
            mp3_url,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=30,
            follow_redirects=True
        )
        if audio_resp.status_code != 200:
            return jsonify({"error": f"Failed to download recording (HTTP {audio_resp.status_code})"}), 502
        audio_bytes = audio_resp.content
    except Exception as e:
        logger.error(f"transcribe_recording download failed for {call_sid}: {e}")
        return jsonify({"error": f"Download failed: {str(e)}"}), 502

    # Send to xAI Whisper API for transcription
    xai_key = os.getenv("XAI_API_KEY_VOICE") or os.getenv("XAI_API_KEY")
    if not xai_key:
        return jsonify({"error": "XAI_API_KEY not configured"}), 500

    try:
        import io
        xai_client = OpenAI(api_key=xai_key, base_url="https://api.x.ai/v1")
        audio_file = io.BytesIO(audio_bytes)
        audio_file.name = "recording.mp3"
        whisper_resp = xai_client.audio.transcriptions.create(
            model="whisper-large-3",
            file=audio_file,
            response_format="verbose_json",
            timestamp_granularities=["segment"]
        )
        raw_text = (whisper_resp.text or '').strip()
        if not raw_text:
            return jsonify({"error": "Empty transcription returned"}), 502

        # Format as the same structure used by real-time call transcripts
        transcript = [{"role": "call_recording", "text": raw_text}]
        save_call_transcript(call_sid, transcript)
        logger.info(f"On-demand transcription saved for {call_sid} ({len(raw_text)} chars)")
        return jsonify({"transcript": transcript, "cached": False})

    except Exception as e:
        logger.error(f"transcribe_recording xAI failed for {call_sid}: {e}")
        return jsonify({"error": f"Transcription failed: {str(e)}"}), 500


# ──────────────────────────────────────────────────────────────────────────────
# Voicemail Greeting — save + serve
# ──────────────────────────────────────────────────────────────────────────────

@recordings_bp.route('/voice/voicemail-greeting', methods=['POST'])
@jwt_or_session_required
def save_voicemail_greeting():
    """
    Accept a voicemail greeting audio upload from the dashboard.
    Stores the raw bytes as base64 in voice_config JSONB so it survives
    container restarts (no ephemeral-filesystem dependency).
    Returns the serving URL so the JS can show "Greeting saved".
    """
    file = request.files.get('greeting')
    if not file:
        return jsonify({"error": "No greeting file provided"}), 400

    audio_bytes = file.read()
    if len(audio_bytes) > 5 * 1024 * 1024:
        return jsonify({"error": "Greeting file too large (max 5 MB)"}), 413

    audio_b64    = base64.b64encode(audio_bytes).decode('utf-8')
    content_type = file.content_type or 'audio/mpeg'
    filename     = file.filename or 'greeting.mp3'

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, voice_config FROM subscribers WHERE email = %s",
                    (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "User not found"}), 404

        location_id = row['location_id']
        vc = row.get('voice_config') or {}
        if isinstance(vc, str):
            vc = json.loads(vc)

        vc['voicemail_greeting_data'] = audio_b64
        vc['voicemail_greeting_type'] = content_type
        vc['voicemail_greeting_name'] = filename

        cur.execute(
            "UPDATE subscribers SET voice_config = %s WHERE location_id = %s",
            (json.dumps(vc), location_id)
        )
        conn.commit()
        cur.close()

        domain = os.getenv('YOUR_DOMAIN', 'http://localhost:8080').rstrip('/')
        greeting_url = f"{domain}/voice/voicemail-greeting/audio"
        logger.info(f"Voicemail greeting saved for {location_id}: {filename} ({len(audio_bytes)} bytes)")
        return jsonify({"ok": True, "url": greeting_url})
    except Exception as e:
        logger.error(f"Failed to save voicemail greeting: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": "Failed to save greeting"}), 500
    finally:
        return_db_connection(conn)


@recordings_bp.route('/voice/voicemail-greeting/public/<token>', methods=['GET'])
def serve_voicemail_greeting_public(token):
    """
    Public, token-authenticated greeting fetch used by Twilio during a
    voicemail drop. Twilio cannot send session cookies, so the drop endpoint
    signs a short-lived (5-minute) token keyed by location_id and embeds the
    resulting URL in the `<Play>` TwiML. This route validates that token and
    streams the exact same audio bytes as the authenticated `/audio` route.
    """
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    secret = os.getenv('SESSION_SECRET') or os.getenv('SECRET_KEY') or 'dev-insecure'
    serializer = URLSafeTimedSerializer(secret, salt='voicemail-drop-v1')
    try:
        location_id = serializer.loads(token, max_age=300)  # 5 minutes
    except SignatureExpired:
        logger.warning("Voicemail drop public fetch: token expired")
        return "Token expired", 403
    except BadSignature:
        logger.warning("Voicemail drop public fetch: bad signature")
        return "Invalid token", 403

    conn = get_db_connection()
    if not conn:
        return "DB unavailable", 503
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT voice_config FROM subscribers WHERE location_id = %s",
            (location_id,)
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return "Not found", 404

        vc = row.get('voice_config') or {}
        if isinstance(vc, str):
            vc = json.loads(vc)

        b64 = vc.get('voicemail_greeting_data')
        if not b64:
            return "No greeting configured", 404

        audio_bytes  = base64.b64decode(b64)
        content_type = vc.get('voicemail_greeting_type', 'audio/mpeg')
        return Response(audio_bytes, content_type=content_type)
    except Exception as e:
        logger.error(f"Public voicemail greeting serve failed: {e}")
        return "Failed", 500
    finally:
        return_db_connection(conn)


@recordings_bp.route('/voice/voicemail-greeting/audio', methods=['GET'])
@jwt_or_session_required
def serve_voicemail_greeting():
    """Serve the subscriber's stored voicemail greeting audio (used by Twilio and the browser preview)."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB unavailable"}), 503
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Not found"}), 404

        vc = row.get('voice_config') or {}
        if isinstance(vc, str):
            vc = json.loads(vc)

        b64 = vc.get('voicemail_greeting_data')
        if not b64:
            return jsonify({"error": "No greeting configured"}), 404

        audio_bytes  = base64.b64decode(b64)
        content_type = vc.get('voicemail_greeting_type', 'audio/mpeg')
        filename     = vc.get('voicemail_greeting_name', 'greeting.mp3')

        return Response(
            audio_bytes,
            content_type=content_type,
            headers={'Content-Disposition': f'inline; filename="{filename}"'}
        )
    except Exception as e:
        logger.error(f"Failed to serve voicemail greeting: {e}")
        return jsonify({"error": "Failed to serve greeting"}), 500
    finally:
        return_db_connection(conn)
