"""
Phone number management routes — list, search, buy, release, nicknames,
primary number, number health, smart rotation, trust hub, spam protection.

Extracted from voice_bridge.py.
"""

import json
import os
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user

import stripe
import twilio_provisioning
from db import get_db_connection, return_db_connection
from number_health import (
    get_number_health_batch,
    get_all_number_health,
    get_number_health_summary,
    set_number_status,
    ensure_number_health_records,
)
from voice.helpers import _get_current_subscriber_voice, _save_voice_config

logger = logging.getLogger("voice_bridge.numbers")

numbers_bp = Blueprint('voice_numbers', __name__)

YOUR_DOMAIN = os.getenv("YOUR_DOMAIN", "http://localhost:8080")

# ── Phone Number Pricing ──────────────────────────────────────────────────
FREE_NUMBERS_ALLOWANCE = 5          # First 5 numbers are included free
NUMBER_PRICE_CENTS = 90              # $0.90 per additional number
TOLL_FREE_PRICE_CENTS = 215          # $2.15 per toll-free number


def _count_current_numbers(sub_sid: str) -> int:
    """Return count of phone numbers on the sub-account."""
    try:
        return len(twilio_provisioning.list_phone_numbers(sub_sid))
    except Exception:
        return 0


# ── Phone Number CRUD ─────────────────────────────────────────────────────


@numbers_bp.route('/voice/numbers', methods=['GET'])
@login_required
def list_voice_numbers():
    """List all phone numbers on the subscriber's Twilio sub-account."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned. Click Activate Voice first."}), 400

    nicknames = vc.get('number_nicknames', {})
    primary_number = vc.get('twilio_phone_number', '')

    try:
        numbers = twilio_provisioning.list_phone_numbers(sub_sid)
        result = []
        for n in numbers:
            phone = n.get('phone', '')
            is_primary = phone == primary_number
            nickname = nicknames.get(phone, '')
            caps = n.get('capabilities', {})
            result.append({
                "sid": n.get('sid', ''),
                "phone": phone,
                "nickname": nickname,
                "is_primary": is_primary,
                "capabilities": {
                    "voice": caps.get('voice', False),
                    "sms": caps.get('sms', False),
                    "mms": caps.get('mms', False),
                    "fax": caps.get('fax', False),
                },
                "status": n.get('status', 'active'),
                "created_at": n.get('created_at', ''),
            })

        free_remaining = max(0, FREE_NUMBERS_ALLOWANCE - len(result))
        return jsonify({
            "numbers": result,
            "total": len(result),
            "free_remaining": free_remaining,
            "free_allowance": FREE_NUMBERS_ALLOWANCE,
        })

    except Exception as e:
        logger.error(f"Failed to list numbers: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@numbers_bp.route('/voice/numbers/search', methods=['GET'])
@login_required
def search_available_numbers():
    """Search for available phone numbers to purchase."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    area_code = request.args.get('area_code', '')
    state = request.args.get('state', '')
    city = request.args.get('city', '')
    zip_code = request.args.get('zip_code', '')
    contains = request.args.get('contains', '')
    number_type = request.args.get('number_type', 'local')

    try:
        numbers = twilio_provisioning.search_available_numbers(
            number_type=number_type,
            area_code=area_code,
            state=state,
            city=city,
            zip_code=zip_code,
            contains=contains,
        )
        return jsonify({"numbers": numbers, "total": len(numbers)})

    except Exception as e:
        logger.error(f"Number search failed: {e}")
        return jsonify({"error": str(e)}), 500


@numbers_bp.route('/voice/numbers/buy', methods=['POST'])
@login_required
def buy_voice_number():
    """Purchase a phone number. First 5 are free; after that requires Stripe payment."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    data = request.json or {}
    phone_number = data.get('phone_number', '')
    number_type = data.get('number_type', 'local')
    if not phone_number:
        return jsonify({"error": "Phone number is required"}), 400

    current_count = _count_current_numbers(sub_sid)
    is_free = current_count < FREE_NUMBERS_ALLOWANCE

    if not is_free:
        # After free allowance, require Stripe checkout
        return jsonify({
            "payment_required": True,
            "current_count": current_count,
            "free_allowance": FREE_NUMBERS_ALLOWANCE,
            "message": "Free numbers used. Complete payment to add more.",
        }), 402

    # Free number — buy directly from Twilio
    return _provision_number(sub_sid, vc, phone_number)


def _provision_number(sub_sid, vc, phone_number):
    """Actually purchase and configure a phone number on Twilio."""
    twiml_app_sid = vc.get('twilio_twiml_app_sid', '')
    webhook_base_url = YOUR_DOMAIN

    try:
        result = twilio_provisioning.buy_phone_number(
            sub_account_sid=sub_sid,
            phone_number=phone_number,
            webhook_base_url=webhook_base_url,
            twiml_app_sid=twiml_app_sid,
        )
        purchased_phone = result.get("phone", phone_number)
        purchased_sid = result.get("sid", "")
        logger.info(f"Purchased number: {purchased_phone} (SID: {purchased_sid})")

        # Set as primary number if this is the first number on the account
        if not vc.get('twilio_phone_number'):
            vc['twilio_phone_number'] = purchased_phone
            vc['twilio_number_sid'] = purchased_sid
            _save_voice_config(current_user.email, vc)
            logger.info(f"Set {purchased_phone} as primary number for {current_user.email}")

        # Invalidate live numbers cache so smart rotation picks up the new number
        from number_health import invalidate_live_numbers_cache
        invalidate_live_numbers_cache(sub_sid)

        return jsonify({
            "status": "purchased",
            "phone": purchased_phone,
            "sid": purchased_sid,
        })

    except Exception as e:
        logger.error(f"Number purchase failed: {e}")
        return jsonify({"error": str(e)}), 500


@numbers_bp.route('/voice/numbers/checkout', methods=['POST'])
@login_required
def number_checkout():
    """Create a Stripe one-time payment session for a phone number purchase ($0.90)."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    data = request.json or {}
    phone_number = data.get('phone_number', '')
    number_type = data.get('number_type', 'local')
    if not phone_number:
        return jsonify({"error": "Phone number is required"}), 400

    price_cents = TOLL_FREE_PRICE_CENTS if number_type == 'toll_free' else NUMBER_PRICE_CENTS
    price_label = f"${price_cents / 100:.2f}/mo"

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            customer_email=current_user.email,
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": price_cents,
                    "product_data": {
                        "name": f"Phone Number — {phone_number}",
                        "description": f"Monthly phone number ({price_label})",
                    },
                },
                "quantity": 1,
            }],
            metadata={
                "purchase_type": "phone_number",
                "user_email": current_user.email,
                "phone_number": phone_number,
                "number_type": number_type,
                "price_cents": str(price_cents),
            },
            success_url=f"{YOUR_DOMAIN}/dashboard?number_purchased=1&phone={phone_number}",
            cancel_url=f"{YOUR_DOMAIN}/dashboard?number_purchase_cancel=1",
        )
        return jsonify({"checkout_url": checkout_session.url})
    except Exception as e:
        logger.error(f"Number checkout error: {e}")
        return jsonify({"error": "Unable to create checkout session."}), 500


@numbers_bp.route('/voice/numbers/complete-purchase', methods=['POST'])
@login_required
def complete_number_purchase():
    """
    Called after Stripe redirect to actually provision the number.
    Verifies Stripe payment before provisioning.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    data = request.json or {}
    phone_number = data.get('phone_number', '')
    if not phone_number:
        return jsonify({"error": "Phone number is required"}), 400

    # Verify payment via Stripe
    try:
        sessions = stripe.checkout.Session.list(
            customer_email=current_user.email,
            limit=10,
        )
        verified = False
        for sess in sessions.data:
            meta = sess.metadata or {}
            if (sess.payment_status == 'paid' and
                    meta.get('purchase_type') == 'phone_number' and
                    meta.get('phone_number') == phone_number and
                    meta.get('user_email') == current_user.email):
                verified = True
                break

        if not verified:
            return jsonify({"error": "No verified payment found for this number."}), 403
    except Exception as e:
        logger.warning(f"Number purchase payment verification failed: {e}")
        return jsonify({"error": "Payment verification temporarily unavailable."}), 503

    return _provision_number(sub_sid, vc, phone_number)


@numbers_bp.route('/voice/numbers/release', methods=['POST'])
@login_required
def release_voice_number():
    """Release a phone number from the sub-account."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    data = request.json or {}
    phone_sid = data.get('sid', '')

    if not phone_sid:
        return jsonify({"error": "Number SID is required"}), 400

    try:
        success = twilio_provisioning.release_phone_number(sub_sid, phone_sid)
        if success:
            logger.info(f"Released number: {phone_sid}")
            # Invalidate live numbers cache so smart rotation stops using this number
            from number_health import invalidate_live_numbers_cache
            invalidate_live_numbers_cache(sub_sid)
            return jsonify({"status": "released"})
        return jsonify({"error": "Release failed"}), 400

    except Exception as e:
        logger.error(f"Number release failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── Trust Hub ──────────────────────────────────────────────────────────────


@numbers_bp.route('/voice/trust-hub', methods=['GET'])
@login_required
def get_trust_hub_status():
    """Get number health and carrier trust status."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned. Click Activate Voice first."}), 400

    trust_hub = vc.get('trust_hub', {})
    business_name = trust_hub.get('business_name', '')
    ein = trust_hub.get('ein', '')

    result = {
        "stir_shaken": {
            "status": "auto_managed",
            "attestation": "A",
            "note": "STIR/SHAKEN attestation is automatically handled for verified business numbers. Full (A) attestation means carriers trust your calls.",
        },
        "business_profile": {
            "business_name": business_name,
            "ein": ein,
            "registered": bool(business_name and ein),
        },
        "carrier_registration": {
            "free_caller_registry": {
                "name": "Free Caller Registry",
                "url": "https://www.freecallerregistry.com/fcr/",
                "status": trust_hub.get('fcr_status', 'not_registered'),
                "description": "Cross-carrier registry that links your number to your business. Recommended first step.",
            },
            "att_hiya": {
                "name": "AT&T / Hiya",
                "url": "https://hiya.com/branded-call/",
                "status": trust_hub.get('att_status', 'not_registered'),
                "description": "Register with Hiya to display your business name on AT&T devices and reduce spam flags.",
            },
            "tmobile": {
                "name": "T-Mobile",
                "url": "https://callhub.t-mobile.com/",
                "status": trust_hub.get('tmobile_status', 'not_registered'),
                "description": "T-Mobile Verified Caller — display verified business name to T-Mobile subscribers.",
            },
            "verizon": {
                "name": "Verizon",
                "url": "https://www.verizon.com/business/products/security/spam-call-protection/",
                "status": trust_hub.get('verizon_status', 'not_registered'),
                "description": "Register with Verizon to prevent spam flagging on their network.",
            },
        },
        "numbers": [],
        "cnam_info": {
            "description": "CNAM (Caller Name) displays your business name on recipient phones. Register via Spam Protection tab.",
        },
    }

    try:
        numbers = twilio_provisioning.list_phone_numbers(sub_sid)
        for n in numbers:
            result["numbers"].append({
                "phone": n.get('phone', ''),
                "id": n.get('sid', ''),
                "status": n.get('status', 'active'),
                "friendly_name": n.get('friendly_name', ''),
            })
        return jsonify(result)

    except Exception as e:
        logger.error(f"Trust hub check failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@numbers_bp.route('/voice/numbers/<number_id>/cnam', methods=['POST'])
@login_required
def toggle_cnam(number_id):
    """Update friendly name (CNAM) for a phone number."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    data = request.json or {}
    business_name = data.get('business_name', vc.get('trust_hub', {}).get('business_name', ''))

    try:
        client = twilio_provisioning.get_sub_account_client(sub_sid)
        client.incoming_phone_numbers(number_id).update(
            friendly_name=business_name[:15] if business_name else '',
        )
        return jsonify({"status": "ok", "cnam_listed": bool(business_name)})
    except Exception as e:
        logger.error(f"CNAM toggle failed: {e}")
        return jsonify({"error": str(e)}), 500


@numbers_bp.route('/voice/numbers/nickname', methods=['POST'])
@login_required
def set_number_nickname():
    """Set a friendly nickname for a phone number (stored in voice_config)."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not vc:
        return jsonify({"error": "Voice config not found"}), 400

    data = request.json or {}
    phone = data.get('phone', '')
    nickname = data.get('nickname', '').strip()

    if not phone:
        return jsonify({"error": "Phone number required"}), 400

    nicknames = vc.get('number_nicknames', {})
    if nickname:
        nicknames[phone] = nickname
    else:
        nicknames.pop(phone, None)
    vc['number_nicknames'] = nicknames
    _save_voice_config(current_user.email, vc)

    return jsonify({"status": "ok", "nickname": nickname})


@numbers_bp.route('/voice/numbers/set-primary', methods=['POST'])
@login_required
def set_primary_number():
    """Set a phone number as the primary caller ID."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not vc:
        return jsonify({"error": "Voice config not found"}), 400

    data = request.json or {}
    phone = data.get('phone', '')
    if not phone:
        return jsonify({"error": "Phone number required"}), 400

    vc['twilio_phone_number'] = phone
    _save_voice_config(current_user.email, vc)
    logger.info(f"Set primary number to {phone}")

    return jsonify({"status": "ok", "phone": phone})


# ── Number Health & Smart Rotation API ────────────────────────────────────

@numbers_bp.route('/voice/number-health', methods=['GET'])
@login_required
def get_number_health():
    """Return health data for all numbers. Powers the Number Health dashboard."""
    import number_health as nh

    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not vc:
        return jsonify({"error": "Voice config not found"}), 400

    location_id = getattr(current_user, 'location_id', '')
    if not location_id:
        return jsonify({"error": "No location configured"}), 400

    # Ensure health records exist (only when rotation is enabled to avoid needless DB writes)
    rotation_config = vc.get('number_rotation', {})
    if rotation_config.get('enabled', False):
        nh.ensure_number_health_records(location_id, vc)

    # Get per-number health data
    health_records = nh.get_all_number_health(location_id)

    # Enrich with nicknames and primary flag
    nicknames = vc.get('number_nicknames', {})
    primary = vc.get('twilio_phone_number', '')
    rotation_config = vc.get('number_rotation', {})

    numbers = []
    all_phones = []
    for h in health_records:
        phone = h.get('phone', '')
        all_phones.append(phone)
        stage = h.get('warmup_stage', 0)
        stage_info = nh.WARMUP_STAGES.get(stage, nh.WARMUP_STAGES[4])
        state = nh.phone_to_state(phone)
        numbers.append({
            "phone": phone,
            "nickname": nicknames.get(phone, ''),
            "is_primary": phone == primary,
            "status": h.get('status', 'active'),
            "health_score": float(h.get('health_score', 0)),
            "warmup_stage": stage,
            "warmup_label": stage_info["label"],
            "daily_cap": stage_info["daily_cap"],
            "daily_calls": h.get('daily_calls_today', 0),
            "daily_connected": h.get('daily_connected', 0),
            "daily_no_answer": h.get('daily_no_answer', 0),
            "daily_failed": h.get('daily_failed', 0),
            "daily_busy": h.get('daily_busy', 0),
            "daily_duration_secs": h.get('daily_duration_secs', 0),
            "total_calls": h.get('total_calls', 0),
            "total_connected": h.get('total_connected', 0),
            "total_no_answer": h.get('total_no_answer', 0),
            "total_failed": h.get('total_failed', 0),
            "total_busy": h.get('total_busy', 0),
            "total_duration_secs": h.get('total_duration_secs', 0),
            "daily_carrier_blocked": h.get('daily_carrier_blocked', 0),
            "total_carrier_blocked": h.get('total_carrier_blocked', 0),
            "connect_rate": round(h['total_connected'] / h['total_calls'] * 100, 1) if h.get('total_calls') else 0,
            "daily_connect_rate": round(h['daily_connected'] / h['daily_calls_today'] * 100, 1) if h.get('daily_calls_today') else 0,
            "rest_until": str(h.get('rest_until', '')) if h.get('rest_until') else '',
            "last_used_at": str(h.get('last_used_at', '')),
            "created_at": str(h.get('created_at', '')),
            "state": state or '',
            "state_name": nh.STATE_NAMES.get(state, '') if state else '',
        })

    # A2P / STIR-SHAKEN spam protection status
    a2p_info = vc.get('a2p', {})
    a2p_registered = (a2p_info.get('brand_status', '').upper() == 'APPROVED' and
                      a2p_info.get('campaign_status', '').upper() in ('VERIFIED', 'APPROVED'))

    # Summary stats
    summary = nh.get_number_health_summary(location_id)

    # Licensed states + state coverage analysis
    # Use live Twilio numbers for accurate state coverage (not stale voice_config)
    licensed_states = vc.get('licensed_states', [])
    sub_sid = vc.get('twilio_sub_account_sid', '')
    if sub_sid:
        try:
            live_numbers = twilio_provisioning.list_phone_numbers(sub_sid)
            all_vc_numbers = [n.get('phone', '') for n in live_numbers if n.get('phone')]
        except Exception as e:
            logger.warning(f"Number health: Could not fetch live numbers, falling back to voice_config: {e}")
            all_vc_numbers = list(set([primary] + (vc.get('local_presence_numbers', []))))
            all_vc_numbers = [p for p in all_vc_numbers if p]
    else:
        all_vc_numbers = list(set([primary] + (vc.get('local_presence_numbers', []))))
        all_vc_numbers = [p for p in all_vc_numbers if p]
    state_coverage = nh.get_state_coverage(all_vc_numbers)

    # Build per-licensed-state coverage info
    licensed_coverage = []
    for state in sorted(licensed_states):
        owned_phones = state_coverage.get(state, [])
        licensed_coverage.append({
            "state": state,
            "state_name": nh.STATE_NAMES.get(state, state),
            "owned": len(owned_phones),
            "numbers": owned_phones,
            "need": max(0, nh.RECOMMENDED_NUMBERS_PER_STATE - len(owned_phones)),
        })

    return jsonify({
        "numbers": numbers,
        "summary": summary,
        "rotation_enabled": rotation_config.get('enabled', False),
        "rotation_strategy": rotation_config.get('strategy', 'weighted_health'),
        "spam_protected": a2p_registered,
        "licensed_states": licensed_states,
        "licensed_coverage": licensed_coverage,
        "state_coverage": {state: len(phones) for state, phones in state_coverage.items()},
    })


@numbers_bp.route('/voice/number-health/toggle', methods=['POST'])
@login_required
def toggle_number_rotation():
    """Enable/disable smart number rotation."""
    import number_health as nh

    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not vc:
        return jsonify({"error": "Voice config not found"}), 400

    data = request.json or {}
    enabled = data.get('enabled', False)
    strategy = data.get('strategy', 'weighted_health')

    rotation_config = vc.get('number_rotation', {})
    rotation_config['enabled'] = bool(enabled)
    if strategy in ('weighted_health', 'round_robin', 'highest_health'):
        rotation_config['strategy'] = strategy
    vc['number_rotation'] = rotation_config
    _save_voice_config(current_user.email, vc)

    # Initialize health records when enabling
    if enabled:
        location_id = getattr(current_user, 'location_id', '')
        if location_id:
            nh.ensure_number_health_records(location_id, vc)

    logger.info(f"Number rotation {'enabled' if enabled else 'disabled'} (strategy={strategy}) for {current_user.email}")
    return jsonify({"status": "ok", "enabled": enabled, "strategy": strategy})


@numbers_bp.route('/voice/licensed-states', methods=['POST'])
@login_required
def save_licensed_states():
    """Save the list of states the agent is licensed in."""
    from number_health import STATE_NAMES

    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not vc:
        return jsonify({"error": "Voice config not found"}), 400

    data = request.json or {}
    states = data.get('states', [])

    # Validate: only accept known 2-letter state codes
    valid_states = sorted(set(s.upper() for s in states if s.upper() in STATE_NAMES))

    vc['licensed_states'] = valid_states
    _save_voice_config(current_user.email, vc)

    logger.info(f"Licensed states updated ({len(valid_states)} states) for {current_user.email}")
    return jsonify({"status": "ok", "licensed_states": valid_states})


@numbers_bp.route('/voice/number-health/set-status', methods=['POST'])
@login_required
def set_number_health_status():
    """Manually set a number's status (freeze/unfreeze/rest)."""
    import number_health as nh

    data = request.json or {}
    phone = data.get('phone', '')
    status = data.get('status', '')
    rest_hours = data.get('rest_hours')

    if not phone or status not in ('active', 'resting', 'frozen', 'warmup'):
        return jsonify({"error": "Valid phone and status required"}), 400

    location_id = getattr(current_user, 'location_id', '')
    if not location_id:
        return jsonify({"error": "No location configured"}), 400

    success = nh.set_number_status(location_id, phone, status,
                                    rest_hours=int(rest_hours) if rest_hours else None)
    if success:
        logger.info(f"Number {phone} status set to {status} by {current_user.email}")
        return jsonify({"status": "ok"})
    return jsonify({"error": "Failed to update status"}), 500


@numbers_bp.route('/voice/trust-hub/save', methods=['POST'])
@login_required
def save_trust_hub():
    """Save business profile and carrier registration status for Trust Hub."""
    subscriber, vc, api_key = _get_current_subscriber_voice()
    if not vc:
        return jsonify({"error": "Voice config not found"}), 400

    data = request.json or {}
    trust_hub = vc.get('trust_hub', {})
    # Update business profile
    if 'business_name' in data:
        trust_hub['business_name'] = data['business_name'].strip()
    if 'ein' in data:
        trust_hub['ein'] = data['ein'].strip()
    # Update carrier registration statuses
    for carrier in ['fcr_status', 'att_status', 'tmobile_status', 'verizon_status']:
        if carrier in data:
            trust_hub[carrier] = data[carrier]

    vc['trust_hub'] = trust_hub
    _save_voice_config(current_user.email, vc)

    return jsonify({"status": "ok"})


# ──────────────────────────────────────────────────────────────
# AUTOMATED SPAM PROTECTION
# One form -> registers business identity, enables CNAM on all
# numbers, and auto-protects future purchases.
# ──────────────────────────────────────────────────────────────

@numbers_bp.route('/voice/spam-protection/register', methods=['POST'])
@login_required
def register_spam_protection():
    """
    One-click spam protection registration.
    1. Saves business profile to voice_config
    2. Creates Twilio Trust Hub Customer Profile
    3. Sets CNAM (friendly name) on all phone numbers
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned. Click Activate Voice first."}), 400

    data = request.json or {}
    business_name = (data.get('business_name') or '').strip()
    ein = (data.get('ein') or '').strip()
    street = (data.get('street') or '').strip()
    city = (data.get('city') or '').strip()
    state = (data.get('state') or '').strip()
    zip_code = (data.get('zip') or '').strip()
    contact_name = (data.get('contact_name') or '').strip()
    contact_email = (data.get('contact_email') or '').strip()
    contact_phone = (data.get('contact_phone') or '').strip()

    if not business_name:
        return jsonify({"error": "Business name is required"}), 400
    if not ein:
        return jsonify({"error": "EIN is required"}), 400

    # Step 1: Save business profile to voice_config
    trust_hub = vc.get('trust_hub', {})
    trust_hub.update({
        'business_name': business_name,
        'ein': ein,
        'street': street,
        'city': city,
        'state': state,
        'zip': zip_code,
        'contact_name': contact_name,
        'contact_email': contact_email,
        'contact_phone': contact_phone,
        'registered_at': datetime.utcnow().isoformat(),
        'auto_cnam': True,
    })
    vc['trust_hub'] = trust_hub
    _save_voice_config(current_user.email, vc)

    # Step 2: Register with Twilio Trust Hub + set CNAM on all numbers
    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    results = twilio_provisioning.register_business_profile(
        sub_account_sid=sub_sid,
        business_name=business_name,
        ein=ein,
        street=street,
        city=city,
        state=state,
        zip_code=zip_code,
        contact_name=contact_name,
        contact_email=contact_email or current_user.email,
        contact_phone=contact_phone,
        sub_account_auth_token=sub_auth_token,
    )

    # Step 3: Mark auto-protection enabled
    trust_hub['protection_active'] = True
    trust_hub['_sub_sid'] = sub_sid  # Tag which sub-account this belongs to
    vc['trust_hub'] = trust_hub
    _save_voice_config(current_user.email, vc)

    cnam_name = business_name[:15].strip()
    cnam_step = next((s for s in results.get('steps', []) if s.get('name') == 'cnam_all_numbers'), {})

    return jsonify({
        "status": "ok" if not results.get("errors") else "partial",
        "results": results,
        "cnam_name": cnam_name,
        "numbers_protected": cnam_step.get('enabled', 0),
        "numbers_failed": cnam_step.get('total', 0) - cnam_step.get('enabled', 0),
    })


@numbers_bp.route('/voice/spam-protection/status', methods=['GET'])
@login_required
def spam_protection_status():
    """Get current spam protection registration status."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    trust_hub = (vc or {}).get('trust_hub', {})

    # ALWAYS live-query Twilio sub-account for Trust Hub profiles.
    # This is the ONLY safe way to prevent master account data from appearing in
    # sub-account UI — cached business_name may be stale master data from
    # old auto-discovery, and the previous _sub_sid tag approach failed because
    # impersonation keeps is_super_admin=True and because Trust Hub API may
    # return parent-account profiles to sub-account clients.
    protection_active = False
    business_name = ''
    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    try:
        profiles = twilio_provisioning.discover_trust_hub_profiles(sub_sid, sub_auth_token)
        approved = [p for p in profiles if p.get('status', '').lower() in ('twilio-approved', 'compliant', 'approved')]
        if approved:
            best = approved[0]
            protection_active = True
            business_name = best.get('friendly_name', '')
            # Persist correct sub-account data to cache, keyed by sub_sid
            trust_hub['protection_active'] = True
            trust_hub['profile_sid'] = best['profile_sid']
            trust_hub['business_name'] = business_name
            trust_hub['registered_at'] = best.get('date_created', '')
            trust_hub['_sub_sid'] = sub_sid
            vc['trust_hub'] = trust_hub
            _save_voice_config(current_user.email, vc)
            logger.info(f"[spam-protection] Live Trust Hub for sub={sub_sid}: {business_name}")
        else:
            # No approved profiles on this sub-account — clear any stale cached data
            stale_keys = ('protection_active', 'profile_sid', 'business_name', 'registered_at', '_sub_sid')
            changed = any(trust_hub.get(k) for k in stale_keys)
            if changed:
                for k in stale_keys:
                    trust_hub.pop(k, None)
                vc['trust_hub'] = trust_hub
                _save_voice_config(current_user.email, vc)
                logger.info(f"[spam-protection] Cleared stale Trust Hub data for sub={sub_sid}")
    except Exception as e:
        # Twilio unreachable — fall back to cache but only show data tagged to this sub_sid
        logger.warning(f"[spam-protection] Live Trust Hub query failed for sub={sub_sid}: {e}")
        cached_sub_sid = trust_hub.get('_sub_sid', '')
        if cached_sub_sid == sub_sid:
            # Cache belongs to this sub-account — safe to use as fallback
            protection_active = trust_hub.get('protection_active', False)
            business_name = trust_hub.get('business_name', '')
        else:
            # Cache is untagged or mismatched — do not display it; show not-registered
            protection_active = False
            business_name = ''
            logger.warning(f"[spam-protection] Suppressed potentially stale cache for sub={sub_sid}")

    # Get number details from Twilio
    status = twilio_provisioning.get_spam_protection_status(sub_sid)
    numbers_detail = [
        {
            "phone": n.get('phone', ''),
            "id": n.get('sid', ''),
            "cnam_enabled": bool(n.get('friendly_name')),
            "status": n.get('status', 'active'),
        }
        for n in status.get('numbers', [])
    ]

    return jsonify({
        "protection_active": protection_active,
        "business_name": business_name,
        "ein": trust_hub.get('ein', ''),
        "street": trust_hub.get('street', ''),
        "city": trust_hub.get('city', ''),
        "state": trust_hub.get('state', ''),
        "zip": trust_hub.get('zip', ''),
        "contact_name": trust_hub.get('contact_name', ''),
        "contact_email": trust_hub.get('contact_email', ''),
        "contact_phone": trust_hub.get('contact_phone', ''),
        "registered_at": trust_hub.get('registered_at', ''),
        "numbers_protected": status.get('numbers_protected', 0),
        "numbers_total": status.get('numbers_total', 0),
        "numbers": numbers_detail,
        "stir_shaken": "active",
        "auto_cnam": trust_hub.get('auto_cnam', False),
    })
