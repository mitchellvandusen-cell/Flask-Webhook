import json
import os
import logging

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user

import twilio_provisioning
from db import get_db_connection, return_db_connection
from voice.helpers import _get_current_subscriber_voice, _save_voice_config

logger = logging.getLogger("voice_bridge.setup")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")

setup_bp = Blueprint('voice_setup', __name__)

TWILIO_FIELDS = [
    'twilio_sub_account_sid', 'twilio_sub_account_auth_token',
    'twilio_twiml_app_sid', 'twilio_phone_number', 'twilio_number_sid',
    'twilio_api_key_sid', 'twilio_api_key_secret',
]


def _clear_twilio_fields(vc: dict):
    """Clear all Twilio provisioning fields from voice_config, preserving user settings."""
    for field in TWILIO_FIELDS:
        vc.pop(field, None)
    vc['enabled'] = False


@setup_bp.route('/voice/ping', methods=['GET', 'HEAD'])
def voice_ping():
    """Health check endpoint — used by Railway/Render platform health monitors."""
    return '', 200


@setup_bp.route('/voice/config-status', methods=['GET'])
@login_required
def voice_config_status():
    """
    Real-time validation of voice config against live Twilio account.
    Returns whether the stored sub-account, TwiML app, and phone number
    actually exist on the current Twilio master. Used by the dashboard
    to show accurate status instead of trusting stale DB fields.
    """
    import redis as _redis

    subscriber, vc, _ = _get_current_subscriber_voice()
    if not subscriber:
        return jsonify({"account_valid": False, "app_valid": False, "number_valid": False, "number": ""})

    sub_sid = vc.get('twilio_sub_account_sid', '')
    twiml_app_sid = vc.get('twilio_twiml_app_sid', '')
    phone = vc.get('twilio_phone_number', '')

    # If nothing is provisioned, return immediately
    if not sub_sid:
        return jsonify({"account_valid": False, "app_valid": False, "number_valid": False, "number": "", "stale": False})

    # Check Redis cache first (5 min TTL)
    cache_key = f"voice_config_status:{sub_sid}"
    try:
        redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')
        r = _redis.from_url(redis_url, decode_responses=True)
        cached = r.get(cache_key)
        if cached:
            return jsonify(json.loads(cached))
    except Exception:
        pass

    # Validate against live Twilio
    result = {
        "account_valid": False,
        "app_valid": bool(twiml_app_sid),
        "number_valid": False,
        "number": phone,
        "stale": False,
    }

    try:
        master_client = twilio_provisioning.get_master_client()
        account = master_client.api.accounts(sub_sid).fetch()
        result["account_valid"] = account.status == 'active'
        if not result["account_valid"]:
            result["stale"] = True
    except Exception:
        # Sub-account doesn't exist on current master → stale
        result["stale"] = True
        logger.info(f"[config-status] Sub-account {sub_sid} not found on current master for {current_user.email}")

    # If account is valid and has a phone number, verify the number exists
    if result["account_valid"] and phone:
        try:
            client = twilio_provisioning.get_sub_account_client(sub_sid)
            numbers = client.incoming_phone_numbers.list(phone_number=phone, limit=1)
            result["number_valid"] = len(numbers) > 0
        except Exception:
            result["number_valid"] = False

    # Cache result for 5 minutes
    try:
        r.set(cache_key, json.dumps(result), ex=300)
    except Exception:
        pass

    return jsonify(result)


@setup_bp.route('/voice/automate-setup', methods=['POST'])
@login_required
def automate_voice_setup():
    """
    One-click voice activation. Creates a Twilio sub-account and TwiML app.
    The user buys their own phone number afterwards via the Numbers tab.
    """
    subscriber, vc, _ = _get_current_subscriber_voice()
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    location_id = subscriber.get('location_id', 'unknown')
    host = request.host
    webhook_base_url = f"https://{host}"

    # Check if already provisioned (guard against duplicate clicks)
    existing_sub_sid = vc.get('twilio_sub_account_sid', '')
    if existing_sub_sid:
        # If super_admin was incorrectly provisioned with a sub-account, re-provision with master
        if current_user.is_super_admin and existing_sub_sid != TWILIO_ACCOUNT_SID:
            logger.info(f"[activate] Super admin {current_user.email} has sub-account {existing_sub_sid}, re-provisioning with master account")
            _clear_twilio_fields(vc)
            _save_voice_config(current_user.email, vc)
            # Fall through to re-provision below

        # Detect stale sub-account from old Twilio master (ISV migration)
        elif not current_user.is_super_admin:
            try:
                master_client = twilio_provisioning.get_master_client()
                account = master_client.api.accounts(existing_sub_sid).fetch()
                if account.status != 'active':
                    raise ValueError(f"Sub-account status: {account.status}")
            except Exception as e:
                logger.info(f"[activate] {current_user.email}: stale sub-account {existing_sub_sid} — clearing and re-provisioning ({e})")
                _clear_twilio_fields(vc)
                _save_voice_config(current_user.email, vc)
                # Fall through to re-provision below
        else:
            return jsonify({
                "status": "success",
                "message": "Voice service already active!",
                "twilio_phone_number": vc.get('twilio_phone_number', ''),
            })

    try:
        # Every subscriber gets a Twilio sub-account — including super_admin.
        # Omnisconn (master account) is platform-level only, managed via Console.
        result = twilio_provisioning.provision_subscriber(
            subscriber_email=current_user.email,
            location_id=location_id,
            webhook_base_url=webhook_base_url,
        )

        # Save all provisioned IDs to voice_config
        vc.update(result)
        vc['enabled'] = True
        seat_id = getattr(current_user, 'seat_user_id', None) if getattr(current_user, 'is_seat_user', False) else None
        _save_voice_config(current_user.email, vc, seat_user_id=seat_id)

        logger.info(f"Voice activated for {current_user.email}: sub={result.get('twilio_sub_account_sid')}")

        phone = result.get('twilio_phone_number', '')
        if phone:
            msg = "Voice service activated!"
        else:
            msg = "Voice account created! Now buy a phone number in the Numbers tab."

        return jsonify({
            "status": "success",
            "message": msg,
            "twilio_phone_number": phone,
        })

    except Exception as e:
        logger.error(f"Voice activation error: {e}", exc_info=True)
        _save_voice_config(current_user.email, vc)
        return jsonify({"error": f"Activation failed: {str(e)}"}), 500


# ──────────────────────────────────────────────────────────────
# BROWSER VoIP: Twilio Client JS SDK support
# ──────────────────────────────────────────────────────────────

@setup_bp.route('/voice/setup-voip', methods=['POST'])
@login_required
def setup_voip():
    """
    Browser VoIP setup — verifies the TwiML app is configured for browser calling.
    With Twilio, the TwiML app is created during provisioning so this just confirms readiness.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    location_id = subscriber.get('location_id', 'unknown') if subscriber else 'unknown'
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    twiml_app_sid = vc.get('twilio_twiml_app_sid', '')
    if not twiml_app_sid:
        return jsonify({"error": "Voice not fully provisioned. Click Activate Voice first."}), 400

    logger.info(f"[setup-voip] Ready for location_id={location_id} twiml_app={twiml_app_sid}")
    return jsonify({"status": "ready", "credential_id": twiml_app_sid})


@setup_bp.route('/voice/token', methods=['POST'])
@login_required
def generate_voice_token():
    """Generate a short-lived Twilio Access Token for browser-based calling."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not subscriber:
        return jsonify({"error": "Account not found"}), 404

    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    twiml_app_sid = vc.get('twilio_twiml_app_sid', '')
    location_id = subscriber.get('location_id', 'unknown') if subscriber else 'unknown'
    if not twiml_app_sid:
        return jsonify({"error": "Browser calling not set up. Activate voice first."}), 400

    try:
        # Auto-create per-sub-account API key if missing (for subscribers provisioned before this feature)
        api_key_sid = vc.get('twilio_api_key_sid', '')
        api_key_secret = vc.get('twilio_api_key_secret', '')
        if not api_key_sid or not api_key_secret:
            logger.info(f"[voice/token] No per-subscriber API key found for {sub_sid}, creating one...")
            api_key_data = twilio_provisioning.create_api_key(sub_sid)
            api_key_sid = api_key_data["api_key_sid"]
            api_key_secret = api_key_data["api_key_secret"]
            vc['twilio_api_key_sid'] = api_key_sid
            vc['twilio_api_key_secret'] = api_key_secret
            _save_voice_config(current_user.email, vc)
            logger.info(f"[voice/token] Created and saved API key {api_key_sid} for {sub_sid}")

        # Ensure TwiML app + phone number webhooks point to the current server
        # (fixes stale URLs after domain/deployment changes)
        webhook_base_url = f"https://{request.host}"
        twilio_provisioning.update_twiml_app(sub_sid, twiml_app_sid, webhook_base_url)
        number_sid = vc.get('twilio_number_sid', '')
        if number_sid:
            twilio_provisioning.update_phone_number_webhooks(sub_sid, number_sid, webhook_base_url)

        identity = f"agent_{location_id}"
        token = twilio_provisioning.generate_voice_token(
            identity=identity,
            twiml_app_sid=twiml_app_sid,
            sub_account_sid=sub_sid,
            api_key_sid=api_key_sid,
            api_key_secret=api_key_secret,
        )
        logger.info(f"[voice/token] Token issued for {identity} (twiml_app={twiml_app_sid}, webhook={webhook_base_url})")
        return jsonify({"token": token, "identity": identity})

    except Exception as e:
        logger.error(f"[voice/token] Token generation failed: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


# ── Voice Insights Advanced Features ──

@setup_bp.route('/voice/insights/enable', methods=['POST'])
@login_required
def enable_voice_insights():
    """Enable Voice Insights Advanced Features on the user's sub-account."""
    subscriber, vc, _ = _get_current_subscriber_voice()
    if not vc:
        return jsonify({"error": "Voice config not found"}), 400
    sub_sid = vc.get('twilio_sub_account_sid', '')
    if not sub_sid:
        return jsonify({"error": "No Twilio sub-account"}), 400

    ok = twilio_provisioning.enable_voice_insights_advanced(sub_sid)
    if ok:
        return jsonify({"success": True, "message": "Voice Insights Advanced enabled"})
    return jsonify({"error": "Failed to enable — check Twilio Console"}), 500


@setup_bp.route('/voice/insights/status', methods=['GET'])
@login_required
def voice_insights_status():
    """Check Voice Insights Advanced Features status for the user's sub-account."""
    subscriber, vc, _ = _get_current_subscriber_voice()
    if not vc:
        return jsonify({"error": "Voice config not found"}), 400
    sub_sid = vc.get('twilio_sub_account_sid', '')
    if not sub_sid:
        return jsonify({"advanced_features": False})

    settings = twilio_provisioning.get_voice_insights_settings(sub_sid)
    return jsonify(settings)
