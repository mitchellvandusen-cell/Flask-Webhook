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
            # Clear old voice_config but preserve phone number if any
            old_phone = vc.get('twilio_phone_number', '')
            old_number_sid = vc.get('twilio_number_sid', '')
            vc.clear()
            vc['enabled'] = False
            if old_phone:
                vc['twilio_phone_number'] = old_phone
                vc['twilio_number_sid'] = old_number_sid
            _save_voice_config(current_user.email, vc)
            # Fall through to re-provision below
        else:
            return jsonify({
                "status": "success",
                "message": "Voice service already active!",
                "twilio_phone_number": vc.get('twilio_phone_number', ''),
            })

    try:
        # Only the platform owner (super_admin) uses the master Twilio account.
        # Everyone else — individual, agency_owner, any tier — gets a sub-account.
        if current_user.is_super_admin:
            result = twilio_provisioning.provision_master(
                webhook_base_url=webhook_base_url,
            )
        else:
            result = twilio_provisioning.provision_subscriber(
                subscriber_email=current_user.email,
                location_id=location_id,
                webhook_base_url=webhook_base_url,
            )

        # Save all provisioned IDs to voice_config
        vc.update(result)
        vc['enabled'] = True
        _save_voice_config(current_user.email, vc)

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
        return jsonify({"error": str(e)}), 500
