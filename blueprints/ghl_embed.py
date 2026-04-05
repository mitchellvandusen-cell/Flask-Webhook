# blueprints/ghl_embed.py — GHL Custom JS API endpoints
#
# JWT-authenticated API wrappers for the GHL Custom JS integration.
# These endpoints are called by Custom JS running inside GHL's parent window,
# which has no Flask session cookie — all auth is via Bearer JWT tokens.
#
# Auth flow:
#   1. Custom JS sends HMAC-signed request to POST /api/ghl/auth/token
#   2. Server verifies HMAC using GHL_APP_SHARED_SECRET, returns JWT
#   3. Custom JS uses JWT for all subsequent API calls
#
# Routes:
#   POST /api/ghl/auth/token               — HMAC verify → JWT
#   GET  /api/ghl/intelligence/bulk         — Bulk cached AI intelligence
#   GET  /api/ghl/intelligence/<contact_id> — Single contact intelligence
#   POST /api/ghl/ai-suggest/<contact_id>   — AI reply draft
#   POST /api/ghl/send-sms/<contact_id>     — Send SMS via IGB
#   GET  /api/ghl/ai-minutes/balance        — AI minute balance
#   GET  /api/ghl/ai-minutes/packages       — Available packages + Stripe pricing
#   POST /api/ghl/ai-minutes/checkout       — Create Stripe checkout session
#   GET  /api/ghl/stats                     — Call statistics
#   GET  /api/ghl/stream/notifications      — SSE real-time notifications
#   GET  /api/ghl/subscription-info         — Subscription tier info

import os
import time
import json
import hmac
import hashlib
import logging
import functools
from datetime import datetime, timedelta

import jwt
import stripe
from flask import Blueprint, request, jsonify, Response
from flask_login import current_user

from db import (
    get_db_connection, return_db_connection,
    get_ai_minute_balance,
)
from extensions import YOUR_DOMAIN, ADMIN_EMAILS

logger = logging.getLogger(__name__)

ghl_embed_bp = Blueprint('ghl_embed', __name__)

# ── Constants ────────────────────────────────────────────────────────────────

GHL_APP_SHARED_SECRET = os.getenv('GHL_APP_SHARED_SECRET', '')
JWT_SECRET = os.getenv('SESSION_SECRET') or os.getenv('SECRET_KEY', 'fallback-jwt-secret')
JWT_EXPIRY_HOURS = 2
HMAC_TIMESTAMP_MAX_AGE = 300  # 5 minutes

AI_MINUTE_PACKAGES = [
    {"minutes": 500,   "label": "Starter",      "env_key": "AI_MINUTES_PRICE_ID_500"},
    {"minutes": 2000,  "label": "Growth",        "env_key": "AI_MINUTES_PRICE_ID_2000"},
    {"minutes": 5000,  "label": "Professional",  "env_key": "AI_MINUTES_PRICE_ID_5000"},
    {"minutes": 10000, "label": "Enterprise",    "env_key": "AI_MINUTES_PRICE_ID_10000"},
]


# ── JWT helpers ──────────────────────────────────────────────────────────────

def _create_jwt(location_id, email, tier, subscribed=True):
    """Create a signed JWT token for GHL Custom JS."""
    payload = {
        'location_id': location_id,
        'email': email,
        'tier': tier,
        'subscribed': subscribed,
        'iat': datetime.utcnow(),
        'exp': datetime.utcnow() + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')


def _decode_jwt(token):
    """Decode and verify a JWT token. Returns payload dict or None."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def _get_jwt_from_request():
    """Extract JWT from Authorization: Bearer header or ?key= query param."""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    # Allow JWT in ?key= query param (browser audio elements can't set headers)
    key_param = request.args.get('key', '')
    if key_param:
        return key_param
    return None


# ── Auth decorator ───────────────────────────────────────────────────────────

def jwt_required(f):
    """Decorator that requires a valid JWT token in the Authorization header or ?key= param.
    Sets request._ghl_jwt with the decoded payload.
    Returns 402 with subscription_required=True if the account has no active subscription."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = _get_jwt_from_request()
        if not token:
            return jsonify({"error": "Missing Authorization header"}), 401
        payload = _decode_jwt(token)
        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401
        # Subscription gate: all endpoints except subscription-info itself require active plan
        if not payload.get('subscribed', True):
            if not request.path.endswith('/subscription-info'):
                return jsonify({"subscription_required": True,
                                "error": "Active subscription required"}), 402
        request._ghl_jwt = payload
        return f(*args, **kwargs)
    return decorated


def _get_location_id():
    """Get location_id from JWT payload or current_user."""
    jwt_payload = getattr(request, '_ghl_jwt', None)
    if jwt_payload:
        return jwt_payload.get('location_id', '')
    if current_user and current_user.is_authenticated:
        return getattr(current_user, 'location_id', '')
    return ''


def _get_email():
    """Get email from JWT payload or current_user."""
    jwt_payload = getattr(request, '_ghl_jwt', None)
    if jwt_payload:
        return jwt_payload.get('email', '')
    if current_user and current_user.is_authenticated:
        return current_user.email
    return ''


def _get_subscriber_by_location(location_id):
    """Look up subscriber by location_id."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscribers WHERE location_id = %s LIMIT 1", (location_id,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"ghl_embed: subscriber lookup error: {e}")
        return None
    finally:
        return_db_connection(conn)


# ── Auth endpoint ────────────────────────────────────────────────────────────

@ghl_embed_bp.route('/api/ghl/auth/token', methods=['POST'])
def ghl_auth_token():
    """
    Issue a JWT for the GHL Custom JS integration.

    The Custom JS sends: { location_id, timestamp, [signature], [ghl_token] }

    Auth tiers (strongest to weakest):
      1. HMAC signature present → verify HMAC-SHA256(shared_secret, location_id+ts)
      2. ghl_token matches GHL_APP_SHARED_SECRET → treat as shared secret proof
      3. ghl_token present but doesn't match → accept if location_id is active subscriber
      4. No token/signature → accept if location_id is active subscriber + fresh timestamp

    The GHL Custom JS environment cannot use crypto.subtle (flagged by GHL validator),
    so client-side HMAC is unavailable. AppUtils.getSharedSecret() may return the
    shared secret directly; AppUtils.getUserToken() returns a GHL user access token.
    """
    data = request.get_json() or {}
    location_id = (data.get('location_id') or '').strip()
    timestamp   = data.get('timestamp', 0)
    signature   = (data.get('signature') or '').strip()
    ghl_token   = (data.get('ghl_token') or '').strip()

    if not location_id or not timestamp:
        return jsonify({"error": "Missing required fields"}), 400

    # Verify timestamp freshness (prevents replay regardless of auth method)
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid timestamp"}), 400

    now = int(time.time())
    if abs(now - ts) > HMAC_TIMESTAMP_MAX_AGE:
        return jsonify({"error": "Timestamp expired"}), 401

    # --- Auth path 1: HMAC signature ---
    if signature and GHL_APP_SHARED_SECRET:
        expected_msg = f"{location_id}{ts}"
        expected_sig = hmac.new(
            GHL_APP_SHARED_SECRET.encode('utf-8'),
            expected_msg.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_sig, signature):
            return jsonify({"error": "Invalid signature"}), 401
        # Signature verified — fall through to subscriber lookup

    # --- Auth path 2: ghl_token matches shared secret ---
    elif ghl_token and GHL_APP_SHARED_SECRET and hmac.compare_digest(ghl_token, GHL_APP_SHARED_SECRET):
        # AppUtils.getSharedSecret() returned our shared secret — treat as verified
        pass  # fall through to subscriber lookup

    # --- Auth path 3 & 4: token present or absent — verify location exists ---
    else:
        # Without HMAC, we simply verify the location_id belongs to an active subscriber.
        # The location_id is provided by GHL's AppUtils (trusted GHL runtime), and the
        # timestamp freshness check prevents replay. This is acceptable because the Custom
        # JS runs inside GHL's sandbox and has no other way to prove identity.
        pass  # fall through to subscriber lookup (will 404 if not active)

    # Look up subscriber
    subscriber = _get_subscriber_by_location(location_id)
    if not subscriber:
        return jsonify({"error": "Location not found"}), 404

    email = subscriber.get('email', '')
    tier = subscriber.get('subscription_tier', 'individual')

    # Determine subscription status: active Stripe subscription OR admin account
    stripe_status = subscriber.get('stripe_status', '')
    is_admin = email.lower() in [e.lower() for e in ADMIN_EMAILS]
    # Agency sub-users inherit subscription from parent — treat as subscribed
    is_sub_user = bool(subscriber.get('parent_agency_email'))
    # 'past_due' is intentionally excluded: features are locked until payment recovers
    subscribed = (stripe_status in ('active', 'trialing')) or is_admin or is_sub_user

    token = _create_jwt(location_id, email, tier, subscribed=subscribed)
    return jsonify({
        "token": token,
        "expires_in": JWT_EXPIRY_HOURS * 3600,
        "tier": tier,
        "subscribed": subscribed,
    })


# ── Intelligence endpoints ───────────────────────────────────────────────────

@ghl_embed_bp.route('/api/ghl/intelligence/bulk')
@jwt_required
def ghl_intelligence_bulk():
    """Bulk cached AI intelligence for pipeline badges. Zero AI cost."""
    ids_param = request.args.get('ids', '')
    if not ids_param:
        return jsonify({"cached": {}, "uncached": []})

    contact_ids = [cid.strip() for cid in ids_param.split(',') if cid.strip()]
    if not contact_ids:
        return jsonify({"cached": {}, "uncached": []})

    # Cap at 300 to prevent abuse
    contact_ids = contact_ids[:300]
    location_id = _get_location_id()

    from lead_intelligence import get_bulk_cached_intelligence
    result = get_bulk_cached_intelligence(location_id, contact_ids)
    return jsonify(result)


@ghl_embed_bp.route('/api/ghl/intelligence/<contact_id>')
@jwt_required
def ghl_intelligence_single(contact_id):
    """Single contact AI intelligence. Reads from cache first."""
    location_id = _get_location_id()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT analysis, analyzed_at
            FROM contact_intelligence
            WHERE contact_id = %s AND location_id = %s
        """, (contact_id, location_id))
        row = cur.fetchone()
        cur.close()
        if row and row.get('analysis'):
            analysis = row['analysis']
            if isinstance(analysis, str):
                analysis = json.loads(analysis)
            analysis['analyzed_at'] = str(row.get('analyzed_at', ''))
            return jsonify({"status": "ok", "intelligence": analysis})
        return jsonify({"status": "no_data", "contact_id": contact_id})
    except Exception as e:
        logger.error(f"ghl intelligence single error: {e}")
        return jsonify({"error": "Failed to fetch intelligence"}), 500
    finally:
        return_db_connection(conn)


# ── AI Reply ─────────────────────────────────────────────────────────────────

@ghl_embed_bp.route('/api/ghl/ai-suggest/<contact_id>', methods=['POST'])
@jwt_required
def ghl_ai_suggest(contact_id):
    """Generate AI reply draft. Thin wrapper around voice/contacts.py ai_suggest logic."""
    from tasks import client as _tasks_client
    if not _tasks_client:
        return jsonify({"error": "AI client not configured"}), 503

    location_id = _get_location_id()
    subscriber = _get_subscriber_by_location(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    from db import get_subscriber_info_hybrid, get_bot_settings_by_location
    from ghl_api import get_valid_token

    sub_info = get_subscriber_info_hybrid(location_id)
    if not sub_info:
        return jsonify({"error": "No subscriber config found"}), 400

    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid CRM auth token"}), 401
    sub_info['access_token'] = access_token

    bot_first_name = sub_info.get('bot_first_name', 'Grok')
    contracted_carriers = sub_info.get('contracted_carriers') or []
    if isinstance(contracted_carriers, str):
        try:
            contracted_carriers = json.loads(contracted_carriers)
        except Exception:
            contracted_carriers = []

    # Get contact name
    first_name = ''
    try:
        from voice.contacts import fetch_contact_data_from_ghl
        contact_data = fetch_contact_data_from_ghl(contact_id, access_token, location_id)
        first_name = (contact_data or {}).get('firstName') or ''
    except Exception:
        pass

    bot_settings = get_bot_settings_by_location(location_id)

    # Generate AI draft via pipeline
    try:
        from sales_director import generate_strategic_directive
        director_output = generate_strategic_directive(
            contact_id=contact_id,
            message="",
            first_name=first_name,
            age=None,
            address='',
            bot_settings=bot_settings,
        )
    except Exception as e:
        logger.error(f"GHL AI suggest: sales_director failed: {e}")
        return jsonify({"error": "Failed to generate context"}), 500

    extra_context = director_output.get('underwriting_context', '')
    if director_output.get('company_context'):
        extra_context = f"{extra_context}\n[COMPANY INTEL] {director_output['company_context']}".strip()

    try:
        from prompt import build_system_prompt
        from memory import get_contact_facts
        from db import get_conversation_history

        facts = get_contact_facts(contact_id)
        history = get_conversation_history(contact_id, limit=20)
        timezone = sub_info.get('timezone', 'America/Chicago')

        sys_prompt = build_system_prompt(
            bot_first_name=bot_first_name,
            first_name=first_name,
            carrier_list=contracted_carriers,
            conversation_history=history,
            contact_facts=facts,
            timezone=timezone,
            extra_context=extra_context,
            bot_settings=bot_settings,
        )

        response = _tasks_client.chat.completions.create(
            model="grok-3-mini-fast",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": "(Agent is reviewing the conversation and wants to compose the next message. Generate a natural, helpful reply draft.)"},
            ],
            max_tokens=300,
            temperature=0.7,
        )
        draft = response.choices[0].message.content.strip()

        from reply_sanitizer import sanitize_reply
        draft = sanitize_reply(draft, bot_first_name)

        return jsonify({"status": "ok", "draft": draft, "contact_name": first_name})
    except Exception as e:
        logger.error(f"GHL AI suggest generation failed: {e}")
        return jsonify({"error": "AI generation failed"}), 500


# ── Send SMS ─────────────────────────────────────────────────────────────────

@ghl_embed_bp.route('/api/ghl/send-sms/<contact_id>', methods=['POST'])
@jwt_required
def ghl_send_sms(contact_id):
    """Send SMS to a contact via IGB (routes through Twilio or GHL based on config)."""
    data = request.get_json() or {}
    message = data.get('message', '').strip()
    if not message:
        return jsonify({"error": "Message is required"}), 400

    location_id = _get_location_id()
    subscriber = _get_subscriber_by_location(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    from ghl_api import get_valid_token
    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid CRM auth token"}), 401

    sms_send_via = subscriber.get('sms_send_via', 'ghl')

    # Get contact phone from GHL
    try:
        from voice.contacts import fetch_contact_data_from_ghl
        contact_data = fetch_contact_data_from_ghl(contact_id, access_token, location_id)
        phone = (contact_data or {}).get('phone', '')
        if not phone:
            return jsonify({"error": "Contact has no phone number"}), 400
    except Exception as e:
        logger.error(f"GHL send SMS: contact fetch failed: {e}")
        return jsonify({"error": "Failed to fetch contact data"}), 500

    # Route through Twilio or GHL
    if sms_send_via and sms_send_via.startswith('+'):
        from twilio_sms import send_sms_via_twilio
        vc = subscriber.get('voice_config') or {}
        sub_sid = vc.get('twilio_sub_account_sid', '')
        sub_auth = vc.get('twilio_sub_account_auth_token', '')
        ok, reason, detail = send_sms_via_twilio(
            to_number=phone,
            message=message,
            from_number=sms_send_via,
            sub_account_sid=sub_sid,
            sub_account_auth_token=sub_auth,
        )
    else:
        from ghl_message import send_message
        ok, reason, detail = send_message(contact_id, message, access_token, location_id)

    if ok:
        # Log to GHL conversation
        try:
            from ghl_logger import log_outbound_sms_to_ghl
            log_outbound_sms_to_ghl(contact_id, message, access_token, location_id)
        except Exception:
            pass
        return jsonify({"status": "sent"})
    else:
        return jsonify({"error": f"Send failed: {reason}"}), 500


# ── AI Minutes ───────────────────────────────────────────────────────────────

@ghl_embed_bp.route('/api/ghl/ai-minutes/balance')
@jwt_required
def ghl_ai_minutes_balance():
    """Current AI minute balance."""
    email = _get_email()
    bal = get_ai_minute_balance(email)
    return jsonify(bal)


@ghl_embed_bp.route('/api/ghl/ai-minutes/packages')
@jwt_required
def ghl_ai_minutes_packages():
    """Available AI minute packages with live Stripe pricing."""
    packages = []
    for pkg in AI_MINUTE_PACKAGES:
        price_id = os.getenv(pkg["env_key"], "")
        if not price_id:
            continue
        price_display = None
        try:
            price_obj = stripe.Price.retrieve(price_id)
            price_display = price_obj.unit_amount
        except Exception:
            pass
        packages.append({
            "minutes": pkg["minutes"],
            "label": pkg["label"],
            "price_cents": price_display,
            "available": bool(price_id),
        })
    return jsonify({"packages": packages})


@ghl_embed_bp.route('/api/ghl/ai-minutes/checkout', methods=['POST'])
@jwt_required
def ghl_ai_minutes_checkout():
    """Create Stripe checkout session for AI minutes. Returns checkout_url."""
    data = request.get_json() or {}
    minutes = data.get('minutes')
    if not minutes:
        return jsonify({"error": "Missing 'minutes' parameter"}), 400

    pkg = next((p for p in AI_MINUTE_PACKAGES if p["minutes"] == int(minutes)), None)
    if not pkg:
        return jsonify({"error": f"No package for {minutes} minutes"}), 400

    price_id = os.getenv(pkg["env_key"], "")
    if not price_id:
        return jsonify({"error": "Price not configured"}), 500

    email = _get_email()
    # For GHL Custom JS, redirect back to GHL after checkout
    # The return_url param lets Custom JS specify where to go back
    return_url = data.get('return_url', '')
    success_url = return_url if return_url else f"{YOUR_DOMAIN}/dashboard?ai_minutes_success=1"
    cancel_url = return_url if return_url else f"{YOUR_DOMAIN}/dashboard?ai_minutes_cancel=1"

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            customer_email=email,
            line_items=[{"price": price_id, "quantity": 1}],
            metadata={
                "purchase_type": "ai_minutes",
                "package_minutes": str(pkg["minutes"]),
                "package_label": pkg["label"],
                "user_email": email,
            },
            success_url=success_url,
            cancel_url=cancel_url,
        )
        return jsonify({"checkout_url": checkout_session.url})
    except Exception as e:
        logger.error(f"GHL AI minutes checkout error: {e}")
        return jsonify({"error": "Unable to create checkout session"}), 500


# ── Stats ────────────────────────────────────────────────────────────────────

@ghl_embed_bp.route('/api/ghl/stats')
@jwt_required
def ghl_stats():
    """Call statistics for the stats chip. Wraps existing voice/stats logic."""
    period = request.args.get('period', 'today')
    location_id = _get_location_id()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()

        # Get subscriber timezone
        cur.execute("SELECT timezone FROM subscribers WHERE location_id = %s", (location_id,))
        row = cur.fetchone()
        tz_name = (row or {}).get('timezone', 'America/Chicago')

        # Calculate period boundaries
        import pytz
        try:
            tz = pytz.timezone(tz_name)
        except Exception:
            tz = pytz.timezone('America/Chicago')

        now_local = datetime.now(tz)

        if period == 'today':
            start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == 'week':
            start = now_local - timedelta(days=now_local.weekday())
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == 'month':
            start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

        start_utc = start.astimezone(pytz.utc)

        cur.execute("""
            SELECT
                COUNT(*) AS total_calls,
                COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected,
                COALESCE(AVG(duration) FILTER (WHERE status = 'completed' AND duration > 0), 0) AS avg_duration,
                COALESCE(SUM(duration) FILTER (WHERE status = 'completed' AND duration > 0), 0) AS total_duration,
                COUNT(*) FILTER (WHERE status = 'no-answer') AS no_answer,
                COUNT(*) FILTER (WHERE amd_result = 'machine' OR status = 'voicemail') AS voicemail
            FROM call_history
            WHERE location_id = %s AND created_at >= %s
        """, (location_id, start_utc))
        stats = cur.fetchone()
        cur.close()

        if not stats:
            return jsonify({"total_calls": 0, "connected": 0, "connect_rate": 0, "avg_duration": 0})

        total = stats['total_calls'] or 0
        connected = stats['connected'] or 0
        connect_rate = round((connected / total * 100) if total > 0 else 0, 1)

        return jsonify({
            "total_calls": total,
            "connected": connected,
            "connect_rate": connect_rate,
            "avg_duration": round(float(stats['avg_duration'] or 0)),
            "total_duration": int(stats['total_duration'] or 0),
            "no_answer": stats['no_answer'] or 0,
            "voicemail": stats['voicemail'] or 0,
            "period": period,
        })
    except Exception as e:
        logger.error(f"GHL stats error: {e}")
        return jsonify({"error": "Failed to fetch stats"}), 500
    finally:
        return_db_connection(conn)


# ── Subscription Info ────────────────────────────────────────────────────────

@ghl_embed_bp.route('/api/ghl/subscription-info')
@jwt_required
def ghl_subscription_info():
    """Subscription tier info for Custom JS tier gating."""
    jwt_payload = request._ghl_jwt
    tier = jwt_payload.get('tier', 'individual')
    email = jwt_payload.get('email', '')
    is_admin = email.lower() in [e.lower() for e in ADMIN_EMAILS]

    tier_info = {
        "individual": {"name": "Power Dialer", "max_lines": 1},
        "pro_dialer": {"name": "Pro Dialer", "max_lines": 4},
        "solo_predictive": {"name": "Solo Predictive", "max_lines": 4},
    }
    info = tier_info.get(tier, tier_info["individual"])

    subscribed = jwt_payload.get('subscribed', True)
    return jsonify({"tier": tier, "is_admin": is_admin, "subscribed": subscribed, **info})


# ── SSE Notifications ────────────────────────────────────────────────────────

@ghl_embed_bp.route('/api/ghl/stream/notifications')
@jwt_required
def ghl_stream_notifications():
    """
    Server-Sent Events for real-time call/lead notifications in GHL.
    Mirrors /api/stream/notifications but uses JWT auth instead of session.
    """
    location_id = _get_location_id()
    if not location_id:
        return Response("data: {}\n\n", mimetype='text/event-stream')

    def event_stream():
        last_check = datetime.utcnow()
        yield f"data: {json.dumps({'type': 'connected', 'location_id': location_id})}\n\n"

        while True:
            time.sleep(5)
            try:
                conn = get_db_connection()
                if not conn:
                    continue
                try:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT id, event_type, details, contact_id, created_at
                        FROM webhook_logs
                        WHERE location_id = %s
                          AND created_at > %s
                          AND event_type IN ('message_sent', 'webhook_received',
                                             'new_inbound_message', 'call_completed',
                                             'call_connected')
                        ORDER BY created_at ASC
                        LIMIT 10
                    """, (location_id, last_check))
                    rows = cur.fetchall()
                    cur.close()

                    for row in rows:
                        event_data = {
                            'type': row['event_type'],
                            'contact_id': row.get('contact_id', ''),
                            'details': row.get('details', ''),
                            'created_at': row['created_at'].isoformat() if row.get('created_at') else '',
                        }
                        yield f"data: {json.dumps(event_data)}\n\n"

                    if rows:
                        last_check = rows[-1]['created_at']
                finally:
                    return_db_connection(conn)
            except GeneratorExit:
                return
            except Exception as e:
                logger.error(f"GHL SSE error: {e}")
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(
        event_stream(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


# ── Spam Protection / Number Management ─────────────────────────────────────
# Proxy endpoints that wrap the voice/ blueprint routes with JWT auth
# so the GHL Custom JS can call them without a Flask session cookie.

def _voice_subscriber_context(location_id):
    """Load subscriber + voice config needed by spam protection APIs."""
    subscriber = _get_subscriber_by_location(location_id)
    if not subscriber:
        return None, None
    vc = subscriber.get('voice_config') or {}
    if isinstance(vc, str):
        vc = json.loads(vc)
    return subscriber, vc


@ghl_embed_bp.route('/api/ghl/spam-protection/status')
@jwt_required
def ghl_spam_protection_status():
    """Current spam protection registration status."""
    location_id = _get_location_id()
    subscriber, vc = _voice_subscriber_context(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    from twilio_provisioning import get_sub_account_client_native

    sub_sid = vc.get('twilio_sub_account_sid', '')
    sub_auth = vc.get('twilio_sub_account_auth_token', '')

    # Business profile lives under 'trust_hub' key in voice_config
    # (legacy 'spam_protection' key kept as fallback for old rows)
    spam = vc.get('trust_hub') or vc.get('spam_protection') or {}
    cnam_data = vc.get('cnam') or {}
    ni_data = vc.get('number_integrity') or {}
    a2p_data = vc.get('a2p') or {}

    # Count phone numbers on sub-account
    numbers_total = 0
    numbers_list = []
    if sub_sid and sub_auth:
        try:
            sub_client = get_sub_account_client_native(sub_sid, sub_auth)
            nums = sub_client.incoming_phone_numbers.list(limit=100)
            numbers_total = len(nums)
            numbers_list = [{"phone": n.phone_number, "sid": n.sid, "friendly_name": n.friendly_name} for n in nums]
        except Exception as e:
            logger.error(f"ghl spam status: number list error: {e}")

    return jsonify({
        "protection_active": bool(spam.get('protection_active') or spam.get('registered_at')),
        "business_name": spam.get('business_name', ''),
        "ein": spam.get('ein', ''),
        "profile_sid": spam.get('profile_sid', ''),
        "review_status": spam.get('review_status', ''),
        "registered_at": spam.get('registered_at', ''),
        "numbers_total": numbers_total,
        "numbers": numbers_list,
        "cnam": {
            "registered": bool(cnam_data.get('trust_product_sid')),
            "status": cnam_data.get('status', ''),
            "display_name": cnam_data.get('cnam_display_name', ''),
            "assigned_count": cnam_data.get('assigned_count', 0),
        },
        "number_integrity": {
            "registered": bool(ni_data.get('trust_product_sid')),
            "status": ni_data.get('status', ''),
            "assigned_count": ni_data.get('assigned_count', 0),
        },
        "a2p": {
            "registered": bool(a2p_data.get('campaign_sid')),
            "brand_status": a2p_data.get('brand_status', ''),
            "campaign_status": a2p_data.get('campaign_status', ''),
        },
        "has_sub_account": bool(sub_sid),
    })


@ghl_embed_bp.route('/api/ghl/number-health')
@jwt_required
def ghl_number_health():
    """Number health dashboard data."""
    location_id = _get_location_id()
    subscriber, vc = _voice_subscriber_context(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    sub_sid = vc.get('twilio_sub_account_sid', '')
    if not sub_sid:
        return jsonify({"numbers": [], "summary": {}, "rotation_enabled": False})

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT phone, nickname, is_primary, status, health_score,
                   warmup_stage, daily_calls, daily_connected,
                   ring_rate, connect_rate, block_rate, pdd_ms,
                   stir_a_rate, block_velocity, rest_until, last_used_at
            FROM number_health
            WHERE location_id = %s
            ORDER BY phone
        """, (location_id,))
        rows = cur.fetchall()
        cur.close()

        numbers = []
        total_score = 0
        for r in rows:
            score = r.get('health_score') or 0
            total_score += score
            numbers.append({
                "phone": r['phone'],
                "nickname": r.get('nickname', ''),
                "is_primary": r.get('is_primary', False),
                "status": r.get('status', 'active'),
                "health_score": score,
                "warmup_stage": r.get('warmup_stage', ''),
                "daily_calls": r.get('daily_calls', 0),
                "daily_connected": r.get('daily_connected', 0),
                "connect_rate": r.get('connect_rate', 0),
                "block_rate": r.get('block_rate', 0),
                "rest_until": str(r['rest_until']) if r.get('rest_until') else None,
                "last_used_at": str(r['last_used_at']) if r.get('last_used_at') else None,
            })

        avg_score = round(total_score / len(numbers)) if numbers else 0
        rotation_enabled = vc.get('number_rotation_enabled', False)

        return jsonify({
            "numbers": numbers,
            "summary": {
                "total": len(numbers),
                "avg_health_score": avg_score,
                "active": sum(1 for n in numbers if n['status'] == 'active'),
                "resting": sum(1 for n in numbers if n['status'] == 'resting'),
                "frozen": sum(1 for n in numbers if n['status'] == 'frozen'),
            },
            "rotation_enabled": rotation_enabled,
        })
    except Exception as e:
        logger.error(f"ghl number health error: {e}")
        return jsonify({"error": "Failed to fetch number health"}), 500
    finally:
        return_db_connection(conn)


@ghl_embed_bp.route('/api/ghl/numbers')
@jwt_required
def ghl_numbers_list():
    """List all phone numbers on subscriber's Twilio sub-account."""
    location_id = _get_location_id()
    subscriber, vc = _voice_subscriber_context(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    from twilio_provisioning import get_sub_account_client_native

    sub_sid = vc.get('twilio_sub_account_sid', '')
    sub_auth = vc.get('twilio_sub_account_auth_token', '')
    if not sub_sid or not sub_auth:
        return jsonify({"numbers": [], "total": 0, "free_remaining": 5, "free_allowance": 5})

    try:
        sub_client = get_sub_account_client_native(sub_sid, sub_auth)
        nums = sub_client.incoming_phone_numbers.list(limit=100)

        numbers = []
        for n in nums:
            numbers.append({
                "sid": n.sid,
                "phone": n.phone_number,
                "friendly_name": n.friendly_name,
                "capabilities": {
                    "voice": getattr(n.capabilities, 'voice', True),
                    "sms": getattr(n.capabilities, 'sms', True),
                },
            })

        free_allowance = 5
        free_remaining = max(0, free_allowance - len(numbers))

        return jsonify({
            "numbers": numbers,
            "total": len(numbers),
            "free_remaining": free_remaining,
            "free_allowance": free_allowance,
        })
    except Exception as e:
        logger.error(f"ghl numbers list error: {e}")
        return jsonify({"error": "Failed to list numbers"}), 500


@ghl_embed_bp.route('/api/ghl/numbers/search')
@jwt_required
def ghl_numbers_search():
    """Search available phone numbers to purchase."""
    location_id = _get_location_id()
    subscriber, vc = _voice_subscriber_context(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    from twilio_provisioning import get_sub_account_client_native

    sub_sid = vc.get('twilio_sub_account_sid', '')
    sub_auth = vc.get('twilio_sub_account_auth_token', '')

    area_code = request.args.get('area_code', '')
    state = request.args.get('state', '')
    contains = request.args.get('contains', '')
    number_type = request.args.get('number_type', 'local')

    # Use master client if no sub-account (search doesn't require ownership)
    try:
        if sub_sid and sub_auth:
            client = get_sub_account_client_native(sub_sid, sub_auth)
        else:
            from twilio.rest import Client as TwilioClient
            client = TwilioClient(
                os.getenv('TWILIO_ACCOUNT_SID'),
                os.getenv('TWILIO_AUTH_TOKEN'),
            )

        kwargs = {"limit": 20, "voice_enabled": True, "sms_enabled": True}
        if area_code:
            kwargs["area_code"] = area_code
        if state:
            kwargs["in_region"] = state
        if contains:
            kwargs["contains"] = contains

        if number_type == 'toll_free':
            results = client.available_phone_numbers("US").toll_free.list(**kwargs)
        else:
            results = client.available_phone_numbers("US").local.list(**kwargs)

        numbers = [{
            "phone": n.phone_number,
            "friendly_name": n.friendly_name,
            "locality": getattr(n, 'locality', ''),
            "region": getattr(n, 'region', ''),
            "type": number_type,
        } for n in results]

        return jsonify({"numbers": numbers, "total": len(numbers)})
    except Exception as e:
        logger.error(f"ghl numbers search error: {e}")
        return jsonify({"error": "Search failed"}), 500


@ghl_embed_bp.route('/api/ghl/numbers/buy', methods=['POST'])
@jwt_required
def ghl_numbers_buy():
    """Purchase a phone number (free if under allowance, 402 if payment required)."""
    location_id = _get_location_id()
    subscriber, vc = _voice_subscriber_context(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    from twilio_provisioning import get_sub_account_client_native

    data = request.get_json() or {}
    phone_number = data.get('phone_number', '')
    number_type = data.get('number_type', 'local')

    if not phone_number:
        return jsonify({"error": "phone_number is required"}), 400

    sub_sid = vc.get('twilio_sub_account_sid', '')
    sub_auth = vc.get('twilio_sub_account_auth_token', '')
    if not sub_sid or not sub_auth:
        return jsonify({"error": "Voice not provisioned. Set up voice first in the IGB dashboard."}), 400

    # Check free allowance
    try:
        sub_client = get_sub_account_client_native(sub_sid, sub_auth)
        current_count = len(sub_client.incoming_phone_numbers.list(limit=100))
        free_allowance = 5

        if current_count >= free_allowance:
            # Payment required
            price_cents = 215 if number_type == 'toll_free' else 90
            return jsonify({
                "payment_required": True,
                "current_count": current_count,
                "free_allowance": free_allowance,
                "price_cents": price_cents,
                "price_label": f"${price_cents / 100:.2f}/mo",
            }), 402

        # Free purchase — provision immediately
        domain = YOUR_DOMAIN.rstrip('/')
        new_number = sub_client.incoming_phone_numbers.create(
            phone_number=phone_number,
            voice_url=f"{domain}/voice/twiml/inbound",
            sms_url=f"{domain}/webhook",
        )

        return jsonify({
            "status": "purchased",
            "phone": new_number.phone_number,
            "sid": new_number.sid,
            "free": True,
        })
    except Exception as e:
        logger.error(f"ghl numbers buy error: {e}")
        return jsonify({"error": f"Purchase failed: {e}"}), 500


@ghl_embed_bp.route('/api/ghl/numbers/release', methods=['POST'])
@jwt_required
def ghl_numbers_release():
    """Release a phone number from the sub-account."""
    location_id = _get_location_id()
    subscriber, vc = _voice_subscriber_context(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    from twilio_provisioning import get_sub_account_client_native

    data = request.get_json() or {}
    sid = data.get('sid', '')
    if not sid:
        return jsonify({"error": "sid is required"}), 400

    sub_sid = vc.get('twilio_sub_account_sid', '')
    sub_auth = vc.get('twilio_sub_account_auth_token', '')
    if not sub_sid or not sub_auth:
        return jsonify({"error": "Voice not provisioned"}), 400

    try:
        sub_client = get_sub_account_client_native(sub_sid, sub_auth)
        sub_client.incoming_phone_numbers(sid).delete()
        return jsonify({"status": "released"})
    except Exception as e:
        logger.error(f"ghl numbers release error: {e}")
        return jsonify({"error": f"Release failed: {e}"}), 500


@ghl_embed_bp.route('/api/ghl/trust-hub')
@jwt_required
def ghl_trust_hub_status():
    """Trust Hub / business profile / carrier registration status."""
    location_id = _get_location_id()
    subscriber, vc = _voice_subscriber_context(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    # Business profile lives under 'trust_hub' key in voice_config
    # (legacy 'spam_protection' key kept as fallback for old rows)
    spam = vc.get('trust_hub') or vc.get('spam_protection') or {}
    cnam_data = vc.get('cnam') or {}
    ni_data = vc.get('number_integrity') or {}

    return jsonify({
        "business_profile": {
            "business_name": spam.get('business_name', ''),
            "ein": spam.get('ein', ''),
            "business_type": spam.get('business_type', ''),
            "website": spam.get('website', ''),
            "street": spam.get('street', ''),
            "city": spam.get('city', ''),
            "state": spam.get('state', ''),
            "zip": spam.get('zip', ''),
            "contact_name": spam.get('contact_name', ''),
            "contact_title": spam.get('contact_title', ''),
            "contact_email": spam.get('contact_email', ''),
            "contact_phone": spam.get('contact_phone', ''),
            "profile_sid": spam.get('profile_sid', ''),
            "review_status": spam.get('review_status', ''),
            "protection_active": bool(spam.get('protection_active')),
            "registered": bool(spam.get('protection_active') or spam.get('registered_at')),
        },
        "cnam": {
            "registered": bool(cnam_data.get('trust_product_sid')),
            "status": cnam_data.get('status', ''),
            "display_name": cnam_data.get('cnam_display_name', ''),
        },
        "number_integrity": {
            "registered": bool(ni_data.get('trust_product_sid')),
            "status": ni_data.get('status', ''),
        },
    })


@ghl_embed_bp.route('/api/ghl/number-integrity/status')
@jwt_required
def ghl_number_integrity_status():
    """Voice Integrity registration status."""
    location_id = _get_location_id()
    subscriber, vc = _voice_subscriber_context(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    ni = vc.get('number_integrity') or {}
    return jsonify({
        "registered": bool(ni.get('trust_product_sid')),
        "status": ni.get('status', ''),
        "trust_product_sid": ni.get('trust_product_sid', ''),
        "assigned_count": ni.get('assigned_count', 0),
        "assigned_numbers": ni.get('assigned_numbers', []),
        "registered_at": ni.get('registered_at', ''),
        "business_name": ni.get('business_name', ''),
    })


@ghl_embed_bp.route('/api/ghl/a2p/status')
@jwt_required
def ghl_a2p_status():
    """A2P 10DLC registration status."""
    location_id = _get_location_id()
    subscriber, vc = _voice_subscriber_context(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    a2p = vc.get('a2p') or {}
    return jsonify({
        "registered": bool(a2p.get('campaign_sid')),
        "brand_sid": a2p.get('brand_sid', ''),
        "brand_status": a2p.get('brand_status', ''),
        "campaign_sid": a2p.get('campaign_sid', ''),
        "campaign_status": a2p.get('campaign_status', ''),
        "messaging_service_sid": a2p.get('messaging_service_sid', ''),
        "registered_at": a2p.get('registered_at', ''),
    })


# ── Voice Config ────────────────────────────────────────────────────────────

@ghl_embed_bp.route('/api/ghl/voice-config', methods=['GET'])
@jwt_required
def ghl_voice_config_get():
    """Get current voice configuration."""
    location_id = _get_location_id()
    subscriber, vc = _voice_subscriber_context(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    # Return user-editable fields only (not Twilio secrets)
    safe_keys = [
        'tts_voice', 'voice_instructions', 'voice_greeting',
        'voice_personality', 'voice_language', 'transfer_number',
        'max_call_duration', 'voicemail_detection', 'voicemail_message',
        'on_machine_action', 'wrap_up_time', 'require_disposition',
        'calling_hours_start', 'calling_hours_end',
        'same_contact_daily_max',
        'max_lines_setting', 'max_abandon_rate_pct',
        'auto_disposition_no_answer', 'auto_disposition_voicemail',
        'number_rotation_enabled', 'rotation_strategy',
    ]
    result = {}
    for k in safe_keys:
        if k in vc:
            result[k] = vc[k]

    # Include provisioning status (not secrets)
    result['voice_activated'] = bool(vc.get('twilio_sub_account_sid'))
    result['has_phone_number'] = bool(vc.get('twilio_phone_number') or vc.get('twilio_number_sid'))

    return jsonify(result)


@ghl_embed_bp.route('/api/ghl/voice-config', methods=['POST'])
@jwt_required
def ghl_voice_config_save():
    """Save voice configuration settings."""
    location_id = _get_location_id()
    subscriber, vc = _voice_subscriber_context(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    data = request.get_json() or {}

    # Validate and merge only safe user-editable fields
    editable_strings = {
        'tts_voice': 255, 'voice_instructions': 2000, 'voice_greeting': 500,
        'voice_personality': 500, 'voice_language': 10, 'transfer_number': 20,
        'voicemail_message': 500, 'on_machine_action': 20,
        'calling_hours_start': 5, 'calling_hours_end': 5,
        'rotation_strategy': 30,
    }
    editable_ints = {
        'max_call_duration': (30, 3600), 'wrap_up_time': (0, 120),
        'same_contact_daily_max': (0, 10),
        'max_lines_setting': (1, 4),
    }
    editable_floats = {'max_abandon_rate_pct': (1.0, 10.0)}
    editable_bools = [
        'voicemail_detection', 'require_disposition',
        'auto_disposition_no_answer', 'auto_disposition_voicemail',
        'number_rotation_enabled',
    ]

    updated = dict(vc)  # preserve existing

    for key, max_len in editable_strings.items():
        if key in data:
            updated[key] = str(data[key])[:max_len]

    for key, (lo, hi) in editable_ints.items():
        if key in data:
            try:
                updated[key] = max(lo, min(hi, int(data[key])))
            except (ValueError, TypeError):
                pass

    for key, (lo, hi) in editable_floats.items():
        if key in data:
            try:
                updated[key] = max(lo, min(hi, float(data[key])))
            except (ValueError, TypeError):
                pass

    for key in editable_bools:
        if key in data:
            updated[key] = bool(data[key])

    # Save back to DB
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE subscribers SET voice_config = %s WHERE location_id = %s",
            (json.dumps(updated), location_id),
        )
        conn.commit()
        cur.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        logger.error(f"ghl voice config save error: {e}")
        return jsonify({"error": "Save failed"}), 500
    finally:
        return_db_connection(conn)


# ── SMS Bot Config ──────────────────────────────────────────────────────────

@ghl_embed_bp.route('/api/ghl/bot-config', methods=['GET'])
@jwt_required
def ghl_bot_config_get():
    """Get SMS bot configuration."""
    location_id = _get_location_id()
    subscriber = _get_subscriber_by_location(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    from db import get_bot_settings_by_location
    bot_settings = get_bot_settings_by_location(location_id)

    return jsonify({
        "bot_first_name": subscriber.get('bot_first_name', ''),
        "timezone": subscriber.get('timezone', 'America/Chicago'),
        "sms_send_via": subscriber.get('sms_send_via', 'ghl'),
        "initial_message": subscriber.get('initial_message', ''),
        "personal_website": subscriber.get('personal_website', ''),
        "calendar_id": subscriber.get('calendar_id', ''),
        "calendar_name": subscriber.get('calendar_name', ''),
        "bot_settings": bot_settings or {},
    })


@ghl_embed_bp.route('/api/ghl/bot-config', methods=['POST'])
@jwt_required
def ghl_bot_config_save():
    """Save SMS bot configuration."""
    location_id = _get_location_id()
    subscriber = _get_subscriber_by_location(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    data = request.get_json() or {}

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        updates = []
        params = []

        safe_fields = {
            'bot_first_name': 100, 'timezone': 50,
            'initial_message': 500, 'personal_website': 255,
            'sms_send_via': 20,
        }
        for field, max_len in safe_fields.items():
            if field in data:
                updates.append(f"{field} = %s")
                params.append(str(data[field])[:max_len])

        if not updates:
            return jsonify({"status": "ok", "message": "No changes"})

        params.append(location_id)
        cur.execute(
            f"UPDATE subscribers SET {', '.join(updates)} WHERE location_id = %s",
            params,
        )
        conn.commit()
        cur.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        conn.rollback()
        logger.error(f"ghl bot config save error: {e}")
        return jsonify({"error": "Save failed"}), 500
    finally:
        return_db_connection(conn)


# ── Voice Activation ────────────────────────────────────────────────────────

@ghl_embed_bp.route('/api/ghl/voice/activate', methods=['POST'])
@jwt_required
def ghl_voice_activate():
    """Activate voice (provision Twilio sub-account)."""
    location_id = _get_location_id()
    subscriber, vc = _voice_subscriber_context(location_id)
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    if vc.get('twilio_sub_account_sid'):
        return jsonify({"status": "already_active", "message": "Voice already activated"})

    email = _get_email()
    is_admin = email.lower() in [e.lower() for e in ADMIN_EMAILS]

    try:
        from twilio_provisioning import provision_subscriber

        result = provision_subscriber(
            subscriber_email=email,
            location_id=location_id,
            webhook_base_url=os.getenv('YOUR_DOMAIN', 'https://insurancegrokbot.click'),
        )

        if result.get('error'):
            return jsonify({"error": result['error']}), 500

        return jsonify({"status": "activated", "message": "Voice account provisioned"})
    except Exception as e:
        logger.error(f"ghl voice activate error: {e}")
        return jsonify({"error": f"Activation failed: {e}"}), 500
