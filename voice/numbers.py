"""
Phone number management routes — list, search, buy, release, nicknames,
primary number, number health, smart rotation, trust hub, spam protection.

Extracted from voice_bridge.py.
"""

import json
import os
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, session
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
from blueprints.team import require_permission
from ghl_sync import sync_ghl_users

logger = logging.getLogger("voice_bridge.numbers")

numbers_bp = Blueprint('voice_numbers', __name__)

YOUR_DOMAIN = os.getenv("YOUR_DOMAIN", "http://localhost:8080")
TWILIO_MASTER_SID = os.getenv("TWILIO_ACCOUNT_SID", "")

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

        # Auto-sync: if no primary number is saved but numbers exist on the
        # sub-account, pick the first one and persist it to voice_config.
        if not primary_number and numbers:
            primary_number = numbers[0].get('phone', '')
            if primary_number:
                vc['twilio_phone_number'] = primary_number
                vc['twilio_number_sid'] = numbers[0].get('sid', '')
                _save_voice_config(current_user.email, vc)
                logger.info(f"[numbers] Auto-synced primary number {primary_number} for {current_user.email}")

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
@require_permission('can_manage_numbers')
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


@numbers_bp.route('/voice/numbers/cart-checkout', methods=['POST'])
@login_required
def cart_checkout():
    """
    Handle cart checkout: provision free numbers directly, create Stripe session for paid ones.
    Body: { items: [{phone_number, number_type}, ...] }
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    data = request.json or {}
    items = data.get('items', [])
    if not items:
        return jsonify({"error": "Cart is empty"}), 400
    if len(items) > 20:
        return jsonify({"error": "Maximum 20 numbers per checkout"}), 400

    current_count = _count_current_numbers(sub_sid)
    free_remaining = max(0, FREE_NUMBERS_ALLOWANCE - current_count)

    # Split items into free and paid
    free_items = items[:free_remaining]
    paid_items = items[free_remaining:]

    # Provision free numbers immediately
    provisioned = 0
    errors = []
    for item in free_items:
        phone = item.get('phone_number', '')
        if not phone:
            continue
        try:
            resp = _provision_number(sub_sid, vc, phone)
            resp_data = resp.get_json() if hasattr(resp, 'get_json') else {}
            if isinstance(resp, tuple):
                resp_data = resp[0].get_json() if hasattr(resp[0], 'get_json') else {}
                if resp[1] != 200:
                    errors.append(f"{phone}: {resp_data.get('error', 'Failed')}")
                    continue
            provisioned += 1
            # Refresh vc after each provision in case primary was set
            _, vc, _ = _get_current_subscriber_voice()
        except Exception as e:
            errors.append(f"{phone}: {str(e)}")

    if not paid_items:
        return jsonify({
            "all_free": True,
            "provisioned": provisioned,
            "errors": errors,
        })

    # Calculate total for paid items
    total_cents = 0
    line_items = []
    phone_list = []
    for item in paid_items:
        phone = item.get('phone_number', '')
        ntype = item.get('number_type', 'local')
        price_cents = TOLL_FREE_PRICE_CENTS if ntype == 'toll_free' else NUMBER_PRICE_CENTS
        total_cents += price_cents
        phone_list.append(phone)
        line_items.append({
            "price_data": {
                "currency": "usd",
                "unit_amount": price_cents,
                "product_data": {
                    "name": f"Phone Number — {phone}",
                    "description": f"{'Toll-free' if ntype == 'toll_free' else 'Local'} number (${price_cents/100:.2f}/mo)",
                },
            },
            "quantity": 1,
        })

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            customer_email=current_user.email,
            line_items=line_items,
            metadata={
                "purchase_type": "phone_number_cart",
                "user_email": current_user.email,
                "phone_numbers": ",".join(phone_list),
                "item_count": str(len(paid_items)),
                "total_cents": str(total_cents),
            },
            success_url=f"{YOUR_DOMAIN}/dashboard?cart_purchased=1",
            cancel_url=f"{YOUR_DOMAIN}/dashboard?cart_cancel=1",
        )
        return jsonify({
            "all_free": False,
            "checkout_url": checkout_session.url,
            "free_provisioned": provisioned,
            "paid_count": len(paid_items),
            "total_cents": total_cents,
        })
    except Exception as e:
        logger.error(f"Cart checkout error: {e}")
        return jsonify({"error": "Unable to create checkout session."}), 500


@numbers_bp.route('/voice/numbers/complete-cart-purchase', methods=['POST'])
@login_required
def complete_cart_purchase():
    """
    Called after Stripe redirect to provision all paid numbers from cart.
    Verifies Stripe payment before provisioning.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    data = request.json or {}
    items = data.get('items', [])
    if not items:
        return jsonify({"error": "No items to provision"}), 400

    # Verify a cart payment exists in Stripe
    try:
        sessions = stripe.checkout.Session.list(
            customer_email=current_user.email,
            limit=10,
        )
        verified = False
        for sess in sessions.data:
            meta = sess.metadata or {}
            if (sess.payment_status == 'paid' and
                    meta.get('purchase_type') == 'phone_number_cart' and
                    meta.get('user_email') == current_user.email):
                verified = True
                break

        if not verified:
            return jsonify({"error": "No verified cart payment found."}), 403
    except Exception as e:
        logger.warning(f"Cart payment verification failed: {e}")
        return jsonify({"error": "Payment verification temporarily unavailable."}), 503

    # Provision all numbers
    provisioned = 0
    errors = []
    for item in items:
        phone = item.get('phone_number', '')
        if not phone:
            continue
        try:
            resp = _provision_number(sub_sid, vc, phone)
            resp_data = resp.get_json() if hasattr(resp, 'get_json') else {}
            if isinstance(resp, tuple):
                resp_data = resp[0].get_json() if hasattr(resp[0], 'get_json') else {}
                if resp[1] != 200:
                    errors.append(f"{phone}: {resp_data.get('error', 'Failed')}")
                    continue
            provisioned += 1
            _, vc, _ = _get_current_subscriber_voice()
        except Exception as e:
            errors.append(f"{phone}: {str(e)}")

    logger.info(f"Cart purchase complete: {provisioned} provisioned, {len(errors)} errors for {current_user.email}")
    return jsonify({
        "provisioned": provisioned,
        "errors": errors,
    })


@numbers_bp.route('/voice/numbers/release', methods=['POST'])
@login_required
@require_permission('can_manage_numbers')
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

    # Step 3: Mark auto-protection enabled + save profile_sid
    trust_hub['protection_active'] = True
    trust_hub['_sub_sid'] = sub_sid  # Tag which sub-account this belongs to
    # Save profile_sid so Voice Integrity can reuse this approved profile
    profile_step = next(
        (s for s in results.get('steps', []) if s.get('name') == 'customer_profile' and s.get('sid')),
        None,
    )
    if profile_step:
        trust_hub['profile_sid'] = profile_step['sid']
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
    sub_auth_token = (vc or {}).get('twilio_auth_token', '')

    # ── Sub-account isolation: Trust Hub scoping ──────────────────────────────
    #
    # Twilio Trust Hub Customer Profiles are a PARENT-ACCOUNT feature.  When you
    # call client.trusthub.v1.customer_profiles.list() with a sub-account client,
    # the Twilio API may still return the master account's profiles through account
    # hierarchy inheritance.  This caused every sub-account to show the master's
    # "VAN DUSEN LIFE INSURANCE, LLC" Trust Hub registration.
    #
    # Fix: We NEVER call the Twilio API for sub-accounts.
    #   • Sub-accounts:   only show data tagged with _sub_sid == sub_sid
    #                     (set during our platform's explicit registration flow)
    #   • Master account: auto-discover from Twilio (it IS their account)
    # ─────────────────────────────────────────────────────────────────────────

    # Step 1: Clear any stale data that doesn't belong to this sub-account
    th_sub = trust_hub.get('_sub_sid', '')
    if th_sub and th_sub != sub_sid:
        # Tagged to a different sub-account — definitely stale
        logger.info(f"[spam-protection] Clearing Trust Hub tagged for {th_sub}, current sub={sub_sid}")
        for k in ('protection_active', 'profile_sid', 'business_name', 'registered_at', '_sub_sid'):
            trust_hub.pop(k, None)
        vc['trust_hub'] = trust_hub
        _save_voice_config(current_user.email, vc)
    elif not th_sub and any(trust_hub.get(k) for k in ('protection_active', 'profile_sid', 'business_name')):
        # Untagged data = legacy contamination from old auto-discovery
        logger.info(f"[spam-protection] Clearing untagged Trust Hub for sub={sub_sid} (stale master data)")
        for k in ('protection_active', 'profile_sid', 'business_name', 'registered_at', '_sub_sid'):
            trust_hub.pop(k, None)
        vc['trust_hub'] = trust_hub
        _save_voice_config(current_user.email, vc)

    # Step 2: Determine protection_active / business_name
    is_master = (sub_sid == TWILIO_MASTER_SID)

    if is_master:
        # Master account: safe to auto-discover from Twilio
        protection_active = trust_hub.get('protection_active', False)
        business_name = trust_hub.get('business_name', '')
        if not protection_active and not business_name:
            try:
                profiles = twilio_provisioning.discover_trust_hub_profiles(sub_sid)
                approved = [p for p in profiles if p.get('status', '').lower() in
                            ('twilio-approved', 'compliant', 'approved')]
                if approved:
                    best = approved[0]
                    protection_active = True
                    business_name = best.get('friendly_name', '')
                    trust_hub.update({
                        'protection_active': True,
                        'profile_sid': best['profile_sid'],
                        'business_name': business_name,
                        'registered_at': best.get('date_created', ''),
                        '_sub_sid': sub_sid,
                    })
                    vc['trust_hub'] = trust_hub
                    _save_voice_config(current_user.email, vc)
            except Exception as e:
                logger.warning(f"[spam-protection] Master Trust Hub discovery failed: {e}")
    else:
        # Sub-account: ONLY show data that was explicitly registered through our platform
        # AND is tagged to THIS sub-account.  Never query Twilio (would return master data).
        if trust_hub.get('_sub_sid') == sub_sid:
            protection_active = trust_hub.get('protection_active', False)
            business_name = trust_hub.get('business_name', '')
            # One-time validation: if protection_active but we haven't confirmed the
            # Trust Hub profile actually lives on this sub-account, do a single API
            # check using the sub-account's own credentials.  Pre-fix registrations
            # used master credentials → no profile exists on the sub-account → clear.
            if protection_active and sub_auth_token and not trust_hub.get('_validated'):
                try:
                    profiles = twilio_provisioning.discover_trust_hub_profiles(
                        sub_sid, sub_auth_token)
                    approved = [p for p in profiles if p.get('status', '').lower() in
                                ('twilio-approved', 'compliant', 'approved')]
                    if approved:
                        trust_hub['_validated'] = True
                        vc['trust_hub'] = trust_hub
                        _save_voice_config(current_user.email, vc)
                    else:
                        logger.warning(
                            f"[spam-protection] No approved profiles on sub {sub_sid}; "
                            "clearing cross-account trust_hub data"
                        )
                        for k in ('protection_active', 'profile_sid', 'business_name',
                                  'registered_at', '_sub_sid', '_validated'):
                            trust_hub.pop(k, None)
                        vc['trust_hub'] = trust_hub
                        _save_voice_config(current_user.email, vc)
                        protection_active = False
                        business_name = ''
                except Exception as e:
                    logger.warning(f"[spam-protection] Sub-account profile validation failed: {e}")
        else:
            protection_active = False
            business_name = ''

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


# ──────────────────────────────────────────────────────────────
# CNAM MONITOR & LOOKUP
# Monitor CNAM status across all numbers and look up what
# carriers see as the caller name for any phone number.
# ──────────────────────────────────────────────────────────────


@numbers_bp.route('/voice/cnam/monitor', methods=['GET'])
@login_required
def cnam_monitor():
    """
    CNAM Status Monitor — shows each number's current CNAM name,
    propagation status, and allows inline editing.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    trust_hub = (vc or {}).get('trust_hub', {})
    business_name = trust_hub.get('business_name', '')

    numbers = twilio_provisioning.get_cnam_monitor(sub_sid)

    # Enrich with local nicknames
    nicknames = (vc or {}).get('number_nicknames', {})

    result = []
    for n in numbers:
        phone = n.get('phone', '')
        friendly = n.get('friendly_name', '')
        result.append({
            "phone": phone,
            "sid": n.get('sid', ''),
            "cnam_name": friendly,
            "cnam_enabled": n.get('cnam_enabled', False),
            "cnam_matches_business": (
                friendly.strip().lower() == business_name[:15].strip().lower()
                if friendly and business_name else False
            ),
            "nickname": nicknames.get(phone, ''),
            "date_created": n.get('date_created', ''),
        })

    return jsonify({
        "business_name": business_name,
        "cnam_display_name": business_name[:15].strip() if business_name else "",
        "numbers": result,
        "total": len(result),
        "cnam_set": sum(1 for n in result if n['cnam_enabled']),
        "cnam_matching": sum(1 for n in result if n['cnam_matches_business']),
    })


@numbers_bp.route('/voice/cnam/update', methods=['POST'])
@login_required
def cnam_update():
    """Update CNAM name for a specific phone number."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    data = request.json or {}
    number_sid = data.get('number_sid', '').strip()
    cnam_name = data.get('cnam_name', '').strip()

    if not number_sid:
        return jsonify({"error": "number_sid is required"}), 400

    # If no name provided, fall back to business name from trust_hub
    if not cnam_name:
        cnam_name = (vc or {}).get('trust_hub', {}).get('business_name', '')

    if not cnam_name:
        return jsonify({"error": "No CNAM name provided and no business name registered"}), 400

    result = twilio_provisioning.update_cnam_for_number(sub_sid, number_sid, cnam_name)
    if result.get('status') == 'ok':
        return jsonify(result)
    return jsonify(result), 500


@numbers_bp.route('/voice/cnam/update-all', methods=['POST'])
@login_required
def cnam_update_all():
    """Apply CNAM to all numbers that don't have it set or have a mismatched name."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    trust_hub = (vc or {}).get('trust_hub', {})
    business_name = trust_hub.get('business_name', '')

    if not business_name:
        return jsonify({"error": "No business name registered. Complete Spam Protection first."}), 400

    cnam_name = business_name[:15].strip()

    numbers = twilio_provisioning.get_cnam_monitor(sub_sid)
    updated = 0
    failed = 0
    for n in numbers:
        current = (n.get('friendly_name') or '').strip()
        if current.lower() != cnam_name.lower():
            r = twilio_provisioning.update_cnam_for_number(sub_sid, n['sid'], cnam_name)
            if r.get('status') == 'ok':
                updated += 1
            else:
                failed += 1

    return jsonify({
        "status": "ok",
        "cnam_name": cnam_name,
        "updated": updated,
        "failed": failed,
        "already_set": len(numbers) - updated - failed,
    })


@numbers_bp.route('/voice/cnam/lookup', methods=['POST'])
@login_required
def cnam_lookup():
    """
    Look up what carriers see as the caller name for a phone number.
    Uses Twilio Lookup API v2 with caller_name field.
    """
    data = request.json or {}
    phone = (data.get('phone') or '').strip()

    if not phone:
        return jsonify({"error": "Phone number is required"}), 400

    # Normalize: ensure E.164 format
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        digits = '1' + digits
    if len(digits) == 11 and digits[0] == '1':
        phone_e164 = '+' + digits
    else:
        phone_e164 = '+' + digits

    result = twilio_provisioning.cnam_lookup(phone_e164)
    return jsonify(result)


@numbers_bp.route('/voice/cnam/lookup-own', methods=['GET'])
@login_required
def cnam_lookup_own():
    """
    Look up CNAM for all of the subscriber's own phone numbers.
    Shows what carriers actually see (may differ from what's set via friendly_name).
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    numbers = twilio_provisioning.get_cnam_monitor(sub_sid)
    results = []
    for n in numbers:
        phone = n.get('phone', '')
        if phone:
            lookup = twilio_provisioning.cnam_lookup(phone)
            results.append({
                "phone": phone,
                "sid": n.get('sid', ''),
                "set_name": n.get('friendly_name', ''),
                "carrier_name": lookup.get('caller_name', ''),
                "caller_type": lookup.get('caller_type', ''),
                "propagated": (
                    lookup.get('caller_name', '').strip().lower() ==
                    (n.get('friendly_name') or '').strip().lower()
                    if lookup.get('caller_name') and n.get('friendly_name')
                    else False
                ),
                "error": lookup.get('error', ''),
            })

    return jsonify({
        "numbers": results,
        "total": len(results),
        "propagated": sum(1 for r in results if r.get('propagated')),
    })


# ──────────────────────────────────────────────────────────────
# NUMBER INTEGRITY (Voice Integrity)
# Registers numbers with AT&T/Hiya, T-Mobile/CallHub, Verizon
# carrier analytics to remediate spam labels & improve answer rates.
# ──────────────────────────────────────────────────────────────


@numbers_bp.route('/voice/number-integrity/status', methods=['GET'])
@login_required
def number_integrity_status():
    """Get current Number Integrity registration status."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    ni = (vc or {}).get('number_integrity', {})
    trust_product_sid = ni.get('trust_product_sid', '')
    sub_auth_token = (vc or {}).get('twilio_auth_token', '')

    # If we have a trust product, check live status from Twilio
    live_status = ni.get('status', 'not_registered')
    assigned_numbers = ni.get('assigned_numbers', [])
    assigned_count = ni.get('assigned_count', 0)

    if trust_product_sid:
        try:
            live = twilio_provisioning.get_voice_integrity_status(
                sub_sid, trust_product_sid, sub_auth_token)
            live_status = live.get('status', live_status)
            assigned_numbers = live.get('assigned_numbers', assigned_numbers)
            assigned_count = live.get('assigned_count', assigned_count)
            # Persist updated status
            ni['status'] = live_status
            ni['assigned_numbers'] = assigned_numbers
            ni['assigned_count'] = assigned_count
            vc['number_integrity'] = ni
            _save_voice_config(current_user.email, vc)
        except Exception as e:
            logger.warning(f"[NumberIntegrity] Live status check failed, using cached: {e}")

    # Get all numbers on the sub-account for the UI
    all_numbers = []
    try:
        nums = twilio_provisioning.list_phone_numbers(sub_sid)
        for n in nums:
            pn_sid = n.get('sid', '')
            all_numbers.append({
                "phone": n.get('phone', ''),
                "sid": pn_sid,
                "friendly_name": n.get('friendly_name', ''),
                "registered": pn_sid in assigned_numbers,
            })
    except Exception as e:
        logger.warning(f"[NumberIntegrity] Could not list numbers: {e}")

    # Map status to user-friendly display
    status_map = {
        "not_registered": {"label": "Not Registered", "color": "gray", "icon": "fa-circle-xmark"},
        "draft": {"label": "Draft", "color": "yellow", "icon": "fa-pen"},
        "pending-review": {"label": "Pending Review", "color": "orange", "icon": "fa-clock"},
        "in-review": {"label": "Under Review", "color": "blue", "icon": "fa-magnifying-glass"},
        "twilio-approved": {"label": "Approved & Active", "color": "green", "icon": "fa-circle-check"},
        "twilio-rejected": {"label": "Rejected", "color": "red", "icon": "fa-circle-xmark"},
    }
    display = status_map.get(live_status, status_map["not_registered"])

    return jsonify({
        "status": live_status,
        "display": display,
        "trust_product_sid": trust_product_sid,
        "profile_sid": ni.get('profile_sid', ''),
        "business_name": ni.get('business_name', ''),
        "registered_at": ni.get('registered_at', ''),
        "assigned_count": assigned_count,
        "numbers": all_numbers,
        "carriers": twilio_provisioning.VOICE_INTEGRITY_CARRIERS,
    })


@numbers_bp.route('/voice/number-integrity/register', methods=['POST'])
@login_required
def number_integrity_register():
    """
    Register for Number Integrity (Voice Integrity).
    Creates Trust Product, assigns selected numbers, submits for review.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned. Click Activate Voice first."}), 400

    data = request.json or {}
    phone_sids = data.get('phone_number_sids', [])

    if not phone_sids:
        return jsonify({"error": "Select at least one phone number to register"}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    trust_hub = (vc or {}).get('trust_hub', {})
    business_name = trust_hub.get('business_name', '')
    contact_email = trust_hub.get('contact_email', '') or getattr(current_user, 'email', '')

    if not business_name:
        return jsonify({"error": "Business profile required. Register in the Spam Protection tab first."}), 400

    ni = vc.get('number_integrity', {})
    trust_product_sid = ni.get('trust_product_sid', '')

    # Fetch location user count for Voice Integrity attributes
    location_id = subscriber.get('location_id', '')
    try:
        ghl_users = sync_ghl_users(location_id) if location_id else []
    except Exception:
        ghl_users = []
    user_count = max(len(ghl_users), 1)
    avg_call_volume = str(user_count * 500)
    employee_count = str(user_count)

    try:
        # Step 1: Create Trust Product if we don't have one
        if not trust_product_sid:
            existing_profile = ni.get('profile_sid', '') or trust_hub.get('profile_sid', '')
            result = twilio_provisioning.create_voice_integrity_trust_product(
                sub_account_sid=sub_sid,
                business_name=business_name,
                contact_email=contact_email,
                sub_account_auth_token=sub_auth_token,
                existing_profile_sid=existing_profile,
                business_employee_count=employee_count,
                average_call_volume=avg_call_volume,
            )
            trust_product_sid = result['trust_product_sid']
            ni['trust_product_sid'] = trust_product_sid
            ni['profile_sid'] = result['profile_sid']
            ni['end_user_sid'] = result.get('end_user_sid', '')
            ni['business_name'] = business_name
            ni['registered_at'] = datetime.utcnow().isoformat()

        # Step 2: Assign phone numbers (to profile first, then trust product)
        assign_result = twilio_provisioning.assign_numbers_to_voice_integrity(
            sub_account_sid=sub_sid,
            trust_product_sid=trust_product_sid,
            phone_number_sids=phone_sids,
            sub_account_auth_token=sub_auth_token,
            profile_sid=ni.get('profile_sid', ''),
        )

        if assign_result.get('assigned', 0) == 0 and assign_result.get('failed'):
            # All numbers failed to assign — don't submit for review
            ni['status'] = 'draft'
            vc['number_integrity'] = ni
            _save_voice_config(current_user.email, vc)
            failed_details = assign_result.get('failed', [])
            first_err = failed_details[0]['error'] if failed_details else 'Unknown error'
            return jsonify({"error": f"Failed to assign numbers: {first_err}"}), 500

        # Persist assigned numbers list so "registered" badges work immediately
        existing_assigned = set(ni.get('assigned_numbers', []))
        existing_assigned.update(phone_sids)
        ni['assigned_numbers'] = list(existing_assigned)
        ni['assigned_count'] = len(ni['assigned_numbers'])

        # Step 3: Submit for review
        submit_result = twilio_provisioning.submit_voice_integrity_for_review(
            sub_account_sid=sub_sid,
            trust_product_sid=trust_product_sid,
            sub_account_auth_token=sub_auth_token,
        )

        ni['status'] = submit_result.get('status', 'pending-review')
        vc['number_integrity'] = ni
        _save_voice_config(current_user.email, vc)

        return jsonify({
            "status": "ok",
            "trust_product_sid": trust_product_sid,
            "review_status": ni['status'],
            "numbers_assigned": assign_result.get('assigned', 0),
            "numbers_failed": len(assign_result.get('failed', [])),
            "failed_details": assign_result.get('failed', []),
        })

    except Exception as e:
        # Save partial progress
        if trust_product_sid:
            ni['trust_product_sid'] = trust_product_sid
            vc['number_integrity'] = ni
            _save_voice_config(current_user.email, vc)
        logger.error(f"[NumberIntegrity] Registration failed: {e}", exc_info=True)
        return jsonify({"error": f"Registration failed: {str(e)}"}), 500


@numbers_bp.route('/voice/number-integrity/add-numbers', methods=['POST'])
@login_required
def number_integrity_add_numbers():
    """Add additional phone numbers to an existing Voice Integrity registration."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    ni = vc.get('number_integrity', {})
    trust_product_sid = ni.get('trust_product_sid', '')
    if not trust_product_sid:
        return jsonify({"error": "No Number Integrity registration found. Register first."}), 400

    data = request.json or {}
    phone_sids = data.get('phone_number_sids', [])
    if not phone_sids:
        return jsonify({"error": "Select at least one phone number"}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')

    try:
        result = twilio_provisioning.assign_numbers_to_voice_integrity(
            sub_account_sid=sub_sid,
            trust_product_sid=trust_product_sid,
            phone_number_sids=phone_sids,
            sub_account_auth_token=sub_auth_token,
            profile_sid=ni.get('profile_sid', ''),
        )

        if result.get('assigned', 0) == 0 and result.get('failed'):
            failed_details = result.get('failed', [])
            first_err = failed_details[0]['error'] if failed_details else 'Unknown error'
            return jsonify({"error": f"Failed to assign numbers: {first_err}"}), 500

        # Persist updated assigned numbers list
        existing_assigned = set(ni.get('assigned_numbers', []))
        existing_assigned.update(phone_sids)
        ni['assigned_numbers'] = list(existing_assigned)
        ni['assigned_count'] = len(ni['assigned_numbers'])
        vc['number_integrity'] = ni
        _save_voice_config(current_user.email, vc)

        return jsonify({
            "status": "ok",
            "numbers_assigned": result.get('assigned', 0),
            "numbers_failed": len(result.get('failed', [])),
            "failed_details": result.get('failed', []),
        })
    except Exception as e:
        logger.error(f"[NumberIntegrity] Add numbers failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@numbers_bp.route('/voice/number-integrity/remove-number', methods=['POST'])
@login_required
def number_integrity_remove_number():
    """Remove a phone number from Voice Integrity registration."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    ni = vc.get('number_integrity', {})
    trust_product_sid = ni.get('trust_product_sid', '')
    if not trust_product_sid:
        return jsonify({"error": "No Number Integrity registration found"}), 400

    data = request.json or {}
    phone_sid = data.get('phone_number_sid', '')
    if not phone_sid:
        return jsonify({"error": "Phone number SID required"}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')

    try:
        removed = twilio_provisioning.remove_number_from_voice_integrity(
            sub_account_sid=sub_sid,
            trust_product_sid=trust_product_sid,
            phone_number_sid=phone_sid,
            sub_account_auth_token=sub_auth_token,
        )
        if removed:
            # Update persisted assigned numbers list
            assigned = ni.get('assigned_numbers', [])
            if phone_sid in assigned:
                assigned.remove(phone_sid)
                ni['assigned_numbers'] = assigned
                ni['assigned_count'] = len(assigned)
                vc['number_integrity'] = ni
                _save_voice_config(current_user.email, vc)
        return jsonify({"status": "ok", "removed": removed})
    except Exception as e:
        logger.error(f"[NumberIntegrity] Remove number failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@numbers_bp.route('/voice/number-integrity/remediate', methods=['POST'])
@login_required
def number_integrity_remediate():
    """
    Trigger remediation for numbers flagged as spam.
    Re-submits the Trust Product to refresh carrier registrations.
    Remediation typically takes 24-48 hours.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    ni = vc.get('number_integrity', {})
    trust_product_sid = ni.get('trust_product_sid', '')
    if not trust_product_sid:
        return jsonify({"error": "No Number Integrity registration found. Register first."}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')

    try:
        # Check current status first
        try:
            status = twilio_provisioning.get_voice_integrity_status(
                sub_sid, trust_product_sid, sub_auth_token)
            current_status = status.get('status', '')
        except Exception as status_err:
            logger.warning(f"[NumberIntegrity] Status check failed during remediate: {status_err}")
            current_status = ni.get('status', '')

        if current_status in ('pending-review', 'in-review'):
            return jsonify({
                "error": "Remediation is already in progress. Please allow 24-48 hours.",
                "current_status": current_status,
            }), 409

        if current_status == 'draft':
            return jsonify({
                "error": "Registration has not been submitted yet. Register your numbers first.",
            }), 400

        # Re-submit for review to trigger carrier re-registration
        result = twilio_provisioning.submit_voice_integrity_for_review(
            sub_account_sid=sub_sid,
            trust_product_sid=trust_product_sid,
            sub_account_auth_token=sub_auth_token,
        )

        ni['status'] = result.get('status', 'pending-review')
        ni['last_remediation'] = datetime.utcnow().isoformat()
        vc['number_integrity'] = ni
        _save_voice_config(current_user.email, vc)

        return jsonify({
            "status": "ok",
            "message": "Remediation submitted. Carrier re-registration typically takes 24-48 hours.",
            "review_status": ni['status'],
        })

    except Exception as e:
        logger.error(f"[NumberIntegrity] Remediation failed: {e}", exc_info=True)
        return jsonify({"error": f"Remediation failed: {str(e)}"}), 500
