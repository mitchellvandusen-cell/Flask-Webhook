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
from voice import hiya as hiya_connect
from db import get_db_connection, return_db_connection, log_webhook_event, save_persistent_alert
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


def _location_for_sub(sub_sid: str) -> str:
    """Look up location_id for a Twilio sub-account SID. Best-effort."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT location_id FROM subscribers WHERE voice_config->>'twilio_sub_account_sid' = %s LIMIT 1",
                (sub_sid,),
            )
            row = cur.fetchone()
            return row[0] if row else ""
    except Exception:
        return ""
    finally:
        if conn:
            return_db_connection(conn)


def _log_number_event(sub_sid, event_type, status, summary, details=None):
    """Log a voice/number operation to webhook_logs with best-effort location_id."""
    try:
        location_id = _location_for_sub(sub_sid)
        log_webhook_event(
            location_id=location_id,
            event_type=event_type,
            status=status,
            summary=summary,
            details=details or {},
        )
    except Exception as e:
        logger.warning(f"Failed to log {event_type}: {e}")


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

        # CNAM: check which numbers are assigned to the CNAM Trust Product
        cnam_config = vc.get('cnam', {})
        cnam_assigned_sids = set(cnam_config.get('assigned_numbers', []))

        result = []
        for n in numbers:
            phone = n.get('phone', '')
            sid = n.get('sid', '')
            is_primary = phone == primary_number
            nickname = nicknames.get(phone, '')
            caps = n.get('capabilities', {})
            result.append({
                "sid": sid,
                "phone": phone,
                "friendly_name": n.get('friendly_name', ''),
                "nickname": nickname,
                "is_primary": is_primary,
                "cnam_listed": sid in cnam_assigned_sids,
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
        return jsonify({"error": "Internal server error"}), 500


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
        return jsonify({"error": "Internal server error"}), 500


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


def _auto_register_for_spam(sub_sid: str, vc: dict, phone_sid: str, phone_e164: str) -> None:
    """
    Fire-and-forget: add a newly purchased number to any active spam-protection
    registrations.  Called right after _provision_number() succeeds.
    Never raises — purchase must not fail because of this.

    Covers:
      Voice Integrity (Twilio) — adds phone SID to existing Trust Product so
        AT&T/Hiya, T-Mobile/FirstOrion, and Verizon/TNS all see the number.
      Hiya Connect — adds E.164 number to existing approved brand for branded display.
    """
    sub_auth_token = vc.get('twilio_auth_token', '')

    # ── Voice Integrity ────────────────────────────────────────────────────────
    ni = vc.get('number_integrity', {})
    vi_tp_sid = ni.get('trust_product_sid', '')
    vi_status = ni.get('status', '')
    # Only add if Trust Product exists and has been submitted (not just draft)
    vi_active = vi_tp_sid and vi_status in (
        'pending-review', 'in-review', 'twilio-approved', 'approved'
    )
    if vi_active:
        try:
            profile_sid = ni.get('profile_sid', '')
            twilio_provisioning.assign_numbers_to_voice_integrity(
                sub_account_sid=sub_sid,
                trust_product_sid=vi_tp_sid,
                phone_number_sids=[phone_sid],
                sub_account_auth_token=sub_auth_token,
                profile_sid=profile_sid,
            )
            assigned = ni.get('assigned_numbers', [])
            if phone_sid not in assigned:
                assigned.append(phone_sid)
            ni['assigned_numbers'] = assigned
            ni['assigned_count'] = len(assigned)
            vc['number_integrity'] = ni
            logger.info(f"[AutoSpam] {phone_e164} added to Voice Integrity TP {vi_tp_sid}")
        except Exception as e:
            logger.warning(f"[AutoSpam] Voice Integrity auto-add failed for {phone_e164}: {e}")

    # ── Hiya Connect (branded display) ────────────────────────────────────────
    hiya_data = vc.get('hiya_connect', {})
    brand_id = hiya_data.get('brand_id', '')
    brand_status = hiya_data.get('brand_status', '')
    hiya_active = brand_id and brand_status in ('VERIFIED', 'APPROVED', 'verified', 'approved')
    if hiya_active and hiya_connect.is_connect_configured():
        try:
            trust_hub = vc.get('trust_hub', {})
            display_name = hiya_data.get('display_name') or trust_hub.get('business_name', '')[:15].strip()
            hiya_connect.register_number(
                phone_e164=phone_e164,
                brand_id=brand_id,
                display_name=display_name,
                call_reason="Insurance Sales",
                state=trust_hub.get('state', ''),
                city=trust_hub.get('city', ''),
            )
            registered = hiya_data.get('registered_phones', [])
            if phone_e164 not in registered:
                registered.append(phone_e164)
            hiya_data['registered_phones'] = registered
            hiya_data['registered_count'] = len(registered)
            vc['hiya_connect'] = hiya_data
            logger.info(f"[AutoSpam] {phone_e164} added to Hiya Connect brand {brand_id}")
        except Exception as e:
            logger.warning(f"[AutoSpam] Hiya Connect auto-add failed for {phone_e164}: {e}")


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

        # Auto-register for spam protection (Voice Integrity + Hiya Connect if active)
        _auto_register_for_spam(sub_sid, vc, purchased_sid, purchased_phone)
        # Save updated vc (assigned_numbers lists may have been mutated in-place)
        _save_voice_config(current_user.email, vc)

        # Log successful purchase
        _log_number_event(sub_sid, "phone_number_purchased", "success",
                          f"Phone number purchased: {purchased_phone}",
                          {"phone": purchased_phone, "sid": purchased_sid})

        return jsonify({
            "status": "purchased",
            "phone": purchased_phone,
            "sid": purchased_sid,
        })

    except Exception as e:
        logger.error(f"Number purchase failed: {e}")
        _log_number_event(sub_sid, "phone_number_purchased", "error",
                          f"Phone number purchase failed: {phone_number}",
                          {"phone": phone_number, "error": str(e)})
        save_persistent_alert(
            email=current_user.email, location_id=_location_for_sub(sub_sid),
            alert_type="phone_purchase_failed", severity="error",
            title="Phone Number Purchase Failed",
            message=f"Failed to purchase {phone_number}. Please try again or contact support.",
        )
        return jsonify({"error": "Internal server error"}), 500


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
    _log_number_event(sub_sid, "phone_number_cart_purchased", "success" if not errors else "partial",
                      f"Cart purchase: {provisioned} purchased, {len(errors)} failed",
                      {"provisioned": provisioned, "errors": errors})
    if errors:
        save_persistent_alert(
            email=current_user.email, location_id=_location_for_sub(sub_sid),
            alert_type="phone_purchase_failed", severity="warning",
            title="Some Numbers Failed to Purchase",
            message=f"{provisioned} numbers purchased, {len(errors)} failed. Check your Logs for details.",
        )
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
            _log_number_event(sub_sid, "phone_number_released", "success",
                              f"Phone number released: {phone_sid}",
                              {"sid": phone_sid})
            return jsonify({"status": "released"})
        _log_number_event(sub_sid, "phone_number_released", "error",
                          f"Phone number release failed: {phone_sid}",
                          {"sid": phone_sid, "error": "Release returned false"})
        return jsonify({"error": "Release failed"}), 400

    except Exception as e:
        logger.error(f"Number release failed: {e}")
        _log_number_event(sub_sid, "phone_number_released", "error",
                          f"Phone number release failed: {phone_sid}",
                          {"sid": phone_sid, "error": str(e)})
        return jsonify({"error": "Internal server error"}), 500


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
        return jsonify({"error": "Internal server error"}), 500


@numbers_bp.route('/voice/profile-status', methods=['GET'])
@login_required
def get_profile_status():
    """
    Check Secondary Customer Profile approval status.

    Frontend can call this to determine whether Voice Integrity, A2P, and
    other Trust Products are allowed to proceed. Returns approval state and
    a human-readable message.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    trust_hub = (vc or {}).get('trust_hub', {})
    a2p = (vc or {}).get('a2p', {})
    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    profile_sid = trust_hub.get('profile_sid', '') or a2p.get('profile_sid', '')

    result = twilio_provisioning.check_secondary_profile_status(
        sub_account_sid=sub_sid,
        sub_account_auth_token=sub_auth_token,
        profile_sid=profile_sid,
    )
    return jsonify(result)


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
        return jsonify({"error": "Internal server error"}), 500


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
            "daily_ring_confirmed": h.get('daily_ring_confirmed', 0),
            "total_ring_confirmed": h.get('total_ring_confirmed', 0),
            "ring_rate": round(h['total_ring_confirmed'] / h['total_calls'] * 100, 1) if h.get('total_calls') and h.get('total_ring_confirmed') else 0,
            "connect_rate": round(h['total_connected'] / h['total_calls'] * 100, 1) if h.get('total_calls') else 0,
            "daily_connect_rate": round(h['daily_connected'] / h['daily_calls_today'] * 100, 1) if h.get('daily_calls_today') else 0,
            "block_rate": round(h['total_carrier_blocked'] / h['total_calls'] * 100, 2) if h.get('total_calls') and h.get('total_carrier_blocked') else 0,
            # Voice Insights signals
            "avg_pdd_ms": round(float(h.get('avg_pdd_ms') or 0), 0) if h.get('avg_pdd_ms') else None,
            "pdd_trend": round(float(h.get('recent_pdd_trend') or 0), 0) if h.get('recent_pdd_trend') else None,
            "quality_issues": int(h.get('insights_quality_issues') or 0),
            "stir_a_rate": round(float(h.get('stir_a_rate') or 0), 1) if h.get('stir_a_rate') else None,
            "block_velocity": round(float(h.get('carrier_block_velocity') or 0), 1),
            "rest_until": str(h.get('rest_until', '')) if h.get('rest_until') else '',
            "last_used_at": str(h.get('last_used_at', '')),
            "created_at": str(h.get('created_at', '')),
            "state": state or '',
            "state_name": nh.STATE_NAMES.get(state, '') if state else '',
        })

    # Spam protection: requires Trust Hub profile + CNAM or A2P actually approved
    trust_hub = vc.get('trust_hub', {})
    cnam_info = vc.get('cnam', {})
    a2p_info = vc.get('a2p', {})

    # A2P fully approved (brand + campaign)
    a2p_approved = (a2p_info.get('brand_status', '').upper() == 'APPROVED' and
                    a2p_info.get('campaign_status', '').upper() in ('VERIFIED', 'APPROVED'))

    # Trust Hub profile validated on this sub-account + CNAM Trust Product submitted
    trust_hub_valid = (trust_hub.get('_validated', False) and
                       trust_hub.get('protection_active', False))
    cnam_submitted = bool(cnam_info.get('trust_product_sid')) and \
        cnam_info.get('status', '') in ('pending-review', 'twilio-approved', 'approved')

    # Protected = A2P approved OR (Trust Hub validated AND CNAM submitted)
    a2p_registered = a2p_approved or (trust_hub_valid and cnam_submitted)

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
    business_type = (data.get('business_type') or '').strip()
    website = (data.get('website') or '').strip()
    contact_name = (data.get('contact_name') or '').strip()
    contact_title = (data.get('contact_title') or '').strip()
    contact_email = (data.get('contact_email') or '').strip()
    contact_phone = (data.get('contact_phone') or '').strip()

    is_sole_prop = business_type == 'Sole Proprietorship'

    if not business_name:
        return jsonify({"error": "Business name is required"}), 400
    if not ein:
        err = "SSN is required for Sole Proprietorship registration" if is_sole_prop else "EIN is required"
        return jsonify({"error": err}), 400
    if not business_type:
        return jsonify({"error": "Business type is required"}), 400
    if not website:
        return jsonify({"error": "Website or social media page URL is required. Enter your business website, Facebook page, or LinkedIn profile. Twilio verifies your business identity against this URL."}), 400

    # Step 1: Save business profile to voice_config
    trust_hub = vc.get('trust_hub', {})
    trust_hub.update({
        'business_name': business_name,
        'ein': ein,
        'business_type': business_type,
        'website': website,
        'street': street,
        'city': city,
        'state': state,
        'zip': zip_code,
        'contact_name': contact_name,
        'contact_title': contact_title,
        'contact_email': contact_email,
        'contact_phone': contact_phone,
        'registered_at': datetime.utcnow().isoformat(),
        'auto_cnam': True,
    })
    vc['trust_hub'] = trust_hub
    _save_voice_config(current_user.email, vc)

    # Step 2: Register with Twilio Trust Hub (Customer Profile)
    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    existing_profile = trust_hub.get('profile_sid', '')
    results = twilio_provisioning.register_business_profile(
        sub_account_sid=sub_sid,
        business_name=business_name,
        ein=ein,
        business_type=business_type,
        website=website,
        street=street,
        city=city,
        state=state,
        zip_code=zip_code,
        contact_name=contact_name,
        contact_title=contact_title,
        contact_email=contact_email or current_user.email,
        contact_phone=contact_phone,
        sub_account_auth_token=sub_auth_token,
        existing_profile_sid=existing_profile,
    )

    # Step 3: Only mark protection active if a profile was actually created
    has_profile = bool(results.get('profile_sid'))
    if not has_profile:
        # Try to find profile_sid in the steps
        profile_step = next(
            (s for s in results.get('steps', []) if s.get('name') in ('secondary_profile', 'customer_profile') and s.get('sid')),
            None,
        )
        if profile_step:
            has_profile = True
            results['profile_sid'] = profile_step['sid']

    trust_hub['protection_active'] = has_profile
    trust_hub['_sub_sid'] = sub_sid  # Tag which sub-account this belongs to

    # Save profile_sid so Voice Integrity / CNAM can reuse this approved profile
    if results.get('profile_sid'):
        trust_hub['profile_sid'] = results['profile_sid']
    else:
        profile_step = next(
            (s for s in results.get('steps', []) if s.get('name') in ('secondary_profile', 'customer_profile') and s.get('sid')),
            None,
        )
        if profile_step:
            trust_hub['profile_sid'] = profile_step['sid']

    # Save end_user_sid for reuse
    if results.get('end_user_sid'):
        trust_hub['end_user_sid'] = results['end_user_sid']

    # Check if profile was submitted for review or had evaluation issues
    eval_step = next((s for s in results.get('steps', []) if s.get('name') == 'evaluation'), None)
    submit_step = next((s for s in results.get('steps', []) if s.get('name') == 'submit_review'), None)
    if submit_step:
        trust_hub['review_status'] = 'pending-review'
    elif eval_step and eval_step.get('status') == 'noncompliant':
        trust_hub['review_status'] = 'noncompliant'
        trust_hub['evaluation_issues'] = eval_step.get('issues', [])
    if has_profile:
        trust_hub['_validated'] = True
    else:
        trust_hub['_validated'] = False
        logger.warning(f"[SpamProtection] No profile created for {sub_sid} — protection_active=False")

    vc['trust_hub'] = trust_hub
    _save_voice_config(current_user.email, vc)

    # Step 4: Create CNAM Trust Product for proper carrier registration
    # This is the real CNAM — not just setting friendly_name on numbers.
    # GATE: Only create CNAM Trust Product if the Secondary Customer Profile
    # is already approved by Twilio. Submitting CNAM against an unapproved
    # profile wastes API calls and creates orphaned Trust Products.
    cnam_display_name = data.get('cnam_display_name', '').strip()
    if not cnam_display_name:
        cnam_display_name = business_name[:15].strip()

    cnam_result = {"status": "skipped"}
    profile_sid = trust_hub.get('profile_sid', '')
    review_status = trust_hub.get('review_status', '')
    profile_is_approved = review_status in ('twilio-approved', 'compliant', 'approved')

    # Idempotency: check if CNAM Trust Product already exists
    existing_cnam = vc.get('cnam', {})
    existing_cnam_tp = existing_cnam.get('trust_product_sid', '')
    if existing_cnam_tp:
        cnam_result = {
            "status": "already_registered",
            "trust_product_sid": existing_cnam_tp,
            "cnam_display_name": existing_cnam.get('cnam_display_name', ''),
        }
        logger.info(f"[CNAM] Already registered — TP {existing_cnam_tp}, skipping duplicate creation")
    elif not profile_is_approved:
        cnam_result = {
            "status": "deferred",
            "reason": "CNAM registration will proceed automatically once your business profile is approved.",
        }
        logger.info(f"[CNAM] Deferred — profile status is '{review_status}', not approved yet")
    elif profile_sid:
        try:
            # Validate display name
            valid, validation_msg = twilio_provisioning.validate_cnam_display_name(cnam_display_name)
            if not valid:
                cnam_result = {"status": "error", "error": validation_msg}
            else:
                # Create CNAM Trust Product
                cnam_tp = twilio_provisioning.create_cnam_trust_product(
                    sub_account_sid=sub_sid,
                    business_name=business_name,
                    cnam_display_name=cnam_display_name,
                    contact_email=contact_email or current_user.email,
                    sub_account_auth_token=sub_auth_token,
                    existing_profile_sid=profile_sid,
                )

                # Assign all phone numbers to CNAM Trust Product
                client = twilio_provisioning.get_sub_account_client(sub_sid)
                numbers = client.incoming_phone_numbers.list()
                pn_sids = [n.sid for n in numbers]

                if pn_sids:
                    assign_result = twilio_provisioning.assign_numbers_to_cnam(
                        sub_account_sid=sub_sid,
                        trust_product_sid=cnam_tp['trust_product_sid'],
                        phone_number_sids=pn_sids,
                        sub_account_auth_token=sub_auth_token,
                        profile_sid=profile_sid,
                    )
                else:
                    assign_result = {"assigned": 0, "failed": [], "total": 0}

                # Submit for review
                try:
                    submit_result = twilio_provisioning.submit_cnam_for_review(
                        sub_account_sid=sub_sid,
                        trust_product_sid=cnam_tp['trust_product_sid'],
                        sub_account_auth_token=sub_auth_token,
                    )
                    cnam_status = submit_result.get('status', 'draft')
                except Exception as submit_err:
                    logger.warning(f"[CNAM] Submit for review failed: {submit_err}")
                    cnam_status = 'draft'

                # Save CNAM state to voice_config
                cnam_data = vc.get('cnam', {})
                cnam_data.update({
                    'trust_product_sid': cnam_tp['trust_product_sid'],
                    'profile_sid': cnam_tp['profile_sid'],
                    'end_user_sid': cnam_tp['end_user_sid'],
                    'cnam_display_name': cnam_tp['cnam_display_name'],
                    'status': cnam_status,
                    'assigned_numbers': pn_sids,
                    'assigned_count': assign_result.get('assigned', 0),
                    'registered_at': datetime.utcnow().isoformat(),
                    '_sub_sid': sub_sid,
                })
                vc['cnam'] = cnam_data
                _save_voice_config(current_user.email, vc)

                cnam_result = {
                    "status": "ok",
                    "trust_product_sid": cnam_tp['trust_product_sid'],
                    "cnam_display_name": cnam_tp['cnam_display_name'],
                    "numbers_assigned": assign_result.get('assigned', 0),
                    "review_status": cnam_status,
                }

        except Exception as cnam_err:
            logger.error(f"[CNAM] Trust Product registration failed: {cnam_err}")
            cnam_result = {"status": "error", "error": str(cnam_err)}

    # ── Log the registration attempt to webhook_logs ──
    # User-facing logs use "Voice Protection" terminology (not "Twilio")
    location_id = getattr(current_user, 'location_id', '')
    reg_errors = results.get('errors', [])
    step_summary = []
    for s in results.get('steps', []):
        name = s.get('name', '')
        label = {
            'customer_profile': 'Business Profile',
            'secondary_profile': 'Business Profile',
            'end_user_business': 'Business Identity',
            'auth_representative': 'Authorized Contact',
            'address': 'Business Address',
            'assign_numbers': 'Number Assignment',
            'evaluation': 'Profile Evaluation',
            'submit_review': 'Submit for Review',
            'cnam_all_numbers': 'Caller ID Labels',
        }.get(name, name)
        status = s.get('status', 'unknown')
        step_summary.append(f"{label}: {status}")

    if has_profile and not reg_errors:
        log_webhook_event(
            location_id=location_id,
            event_type="voice_protection",
            status="success",
            summary=f"Spam Protection registered — {business_name}",
            details={
                "steps": step_summary,
                "profile_sid": trust_hub.get('profile_sid', ''),
                "cnam": cnam_result.get('status', 'skipped'),
                "cnam_display_name": cnam_display_name,
            },
        )
    else:
        # Log failure with details
        error_detail = "; ".join(str(e) for e in reg_errors) if reg_errors else "Business Profile creation failed"
        log_webhook_event(
            location_id=location_id,
            event_type="voice_protection",
            status="error",
            summary=f"Spam Protection registration failed — {error_detail[:200]}",
            details={
                "steps": step_summary,
                "errors": [str(e)[:200] for e in reg_errors],
                "cnam": cnam_result.get('status', 'skipped'),
            },
        )
        # Create dismissable persistent alert so user sees the failure
        save_persistent_alert(
            email=current_user.email,
            alert_type="voice_protection_failed",
            title="Spam Protection Setup Incomplete",
            message=(
                f"Business Profile registration could not be completed. "
                f"{'Errors: ' + error_detail[:200] if reg_errors else 'Please try again or contact support.'}"
            ),
            severity="error",
            location_id=location_id,
        )

    # Also log CNAM result if it was attempted
    if cnam_result.get('status') == 'error':
        log_webhook_event(
            location_id=location_id,
            event_type="voice_protection",
            status="error",
            summary=f"Caller ID (CNAM) registration failed — {cnam_result.get('error', 'Unknown')[:200]}",
            details={"cnam_error": cnam_result.get('error', '')},
        )
    elif cnam_result.get('status') == 'ok':
        log_webhook_event(
            location_id=location_id,
            event_type="voice_protection",
            status="success",
            summary=f"Caller ID registered as '{cnam_result.get('cnam_display_name', '')}' — {cnam_result.get('numbers_assigned', 0)} numbers assigned",
            details={
                "trust_product_sid": cnam_result.get('trust_product_sid', ''),
                "review_status": cnam_result.get('review_status', ''),
            },
        )

    # ── Step 5: Auto-create Voice Integrity (Number Integrity) Trust Product ──
    # Covers: HIYA (AT&T), First Orion (T-Mobile), TNS (Verizon).
    # Works for both sole props (SSN) and businesses (EIN) — business_type is
    # already saved to trust_hub above and reused via existing_profile_sid.
    # CREATE immediately; SUBMIT is attempted now and deferred if profile not
    # yet approved (Twilio approves profiles within ~24h).
    vi_result = {"status": "skipped"}
    existing_ni = vc.get('number_integrity', {})
    existing_vi_tp = existing_ni.get('trust_product_sid', '')

    if existing_vi_tp:
        vi_result = {
            "status": "already_registered",
            "trust_product_sid": existing_vi_tp,
        }
        logger.info(f"[VoiceIntegrity] Already registered — TP {existing_vi_tp}, skipping")
    elif has_profile:
        try:
            # Fetch all phone numbers on this sub-account
            vi_client = twilio_provisioning.get_sub_account_client(sub_sid)
            vi_numbers = vi_client.incoming_phone_numbers.list()
            vi_pn_sids = [n.sid for n in vi_numbers]

            # Estimate call volume from number count
            avg_vol = str(max(len(vi_pn_sids), 1) * 500)
            emp_count = "1" if is_sole_prop else "5"

            existing_vi_profile = trust_hub.get('profile_sid', '')
            vi_tp = twilio_provisioning.create_voice_integrity_trust_product(
                sub_account_sid=sub_sid,
                business_name=business_name,
                contact_email=contact_email or current_user.email,
                sub_account_auth_token=sub_auth_token,
                existing_profile_sid=existing_vi_profile,
                business_employee_count=emp_count,
                average_call_volume=avg_vol,
            )

            ni_data = vc.get('number_integrity', {})
            ni_data.update({
                'trust_product_sid': vi_tp['trust_product_sid'],
                'profile_sid': vi_tp['profile_sid'],
                'end_user_sid': vi_tp.get('end_user_sid', ''),
                'business_name': business_name,
                'registered_at': datetime.utcnow().isoformat(),
            })

            # Assign all phone numbers if we have any
            if vi_pn_sids:
                vi_assign = twilio_provisioning.assign_numbers_to_voice_integrity(
                    sub_account_sid=sub_sid,
                    trust_product_sid=vi_tp['trust_product_sid'],
                    phone_number_sids=vi_pn_sids,
                    sub_account_auth_token=sub_auth_token,
                    profile_sid=vi_tp['profile_sid'],
                )
                ni_data['assigned_numbers'] = vi_pn_sids
                ni_data['assigned_count'] = vi_assign.get('assigned', 0)
            else:
                vi_assign = {"assigned": 0}
                ni_data['assigned_numbers'] = []
                ni_data['assigned_count'] = 0

            # Attempt submit — deferred if profile not yet approved
            vi_submit_status = 'deferred'
            if profile_is_approved:
                try:
                    vi_submit = twilio_provisioning.submit_voice_integrity_for_review(
                        sub_account_sid=sub_sid,
                        trust_product_sid=vi_tp['trust_product_sid'],
                        sub_account_auth_token=sub_auth_token,
                    )
                    vi_submit_status = vi_submit.get('status', 'pending-review')
                except Exception as vi_sub_err:
                    logger.warning(f"[VoiceIntegrity] Submit deferred (profile pending): {vi_sub_err}")
                    vi_submit_status = 'deferred'
            else:
                logger.info(f"[VoiceIntegrity] Submit deferred — profile status '{review_status}' not yet approved")

            ni_data['status'] = vi_submit_status
            vc['number_integrity'] = ni_data
            _save_voice_config(current_user.email, vc)

            vi_result = {
                "status": "ok",
                "trust_product_sid": vi_tp['trust_product_sid'],
                "numbers_assigned": vi_assign.get('assigned', 0),
                "review_status": vi_submit_status,
                "deferred": vi_submit_status == 'deferred',
            }

            log_webhook_event(
                location_id=location_id,
                event_type="voice_protection",
                status="success",
                summary=f"Voice Integrity registered — {vi_assign.get('assigned', 0)} numbers {'submitted for review' if vi_submit_status != 'deferred' else 'pending profile approval'}",
                details={"trust_product_sid": vi_tp['trust_product_sid'], "status": vi_submit_status},
            )

        except Exception as vi_err:
            logger.error(f"[VoiceIntegrity] Auto-registration failed: {vi_err}", exc_info=True)
            vi_result = {"status": "error", "error": str(vi_err)}

    return jsonify({
        "status": "ok" if has_profile and not reg_errors else "partial",
        "results": results,
        "cnam": cnam_result,
        "cnam_display_name": cnam_display_name,
        "voice_integrity": vi_result,
        "has_profile": has_profile,
        "errors": reg_errors,
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
                    # Accept any profile that exists on this sub-account and isn't rejected.
                    # Profiles in draft/pending-review/in-review are legitimate — they just
                    # haven't been approved yet. Only clear if NO profiles exist at all
                    # (meaning the registration was on the wrong account).
                    _VALID_STATUSES = ('twilio-approved', 'compliant', 'approved',
                                       'in-review', 'pending-review', 'draft')
                    valid = [p for p in profiles if p.get('status', '').lower() in _VALID_STATUSES]
                    if valid:
                        trust_hub['_validated'] = True
                        # Store the live status so the UI can reflect reality
                        trust_hub['review_status'] = valid[0].get('status', '')
                        vc['trust_hub'] = trust_hub
                        _save_voice_config(current_user.email, vc)
                    else:
                        logger.warning(
                            f"[spam-protection] No profiles on sub {sub_sid}; "
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

    # A number is truly "protected" only when ALL of these are true:
    #   1. A Customer Profile exists on the sub-account (protection_active + _validated)
    #   2. A CNAM Trust Product has been submitted (status != not_registered/draft)
    #   3. The number is assigned to the CNAM Trust Product
    # Setting friendly_name alone does NOT protect the number — it's just a label.
    #
    # A number is "protected" if covered by ANY active protection mechanism:
    #   1. CNAM Trust Product: profile validated + CNAM TP submitted + number assigned to CNAM TP
    #   2. Voice Integrity Trust Product: VI TP approved + number assigned to VI TP
    #   3. STIR/SHAKEN: profile approved → all numbers on the account get full attestation
    cnam = (vc or {}).get('cnam', {})
    cnam_tp_submitted = cnam.get('status', '') in ('pending-review', 'in-review', 'twilio-approved', 'approved')
    cnam_assigned_sids = set(cnam.get('assigned_numbers', []))
    # _validated means profile EXISTS on this sub-account (not cross-account contamination).
    # profile_approved means Twilio has actually approved the profile.
    profile_exists = protection_active and trust_hub.get('_validated', False)
    review_st = trust_hub.get('review_status', '')
    profile_approved = profile_exists and review_st in ('twilio-approved', 'compliant', 'approved')

    # Voice Integrity: numbers assigned to an approved/pending VI Trust Product
    ni = (vc or {}).get('number_integrity', {})
    vi_active = ni.get('status', '') in ('pending-review', 'in-review', 'twilio-approved', 'approved')
    vi_assigned_sids = set(ni.get('assigned_numbers', [])) if vi_active else set()

    # STIR/SHAKEN: when profile is APPROVED, ALL numbers on the account get full attestation.
    # Profile under review does NOT grant protection yet.
    stir_shaken_active = profile_approved

    numbers_detail = []
    protected_count = 0
    for n in status.get('numbers', []):
        phone = n.get('phone', '')
        sid = n.get('sid', '')
        # Number is protected if covered by ANY active APPROVED protection mechanism
        cnam_protected = profile_approved and cnam_tp_submitted and sid in cnam_assigned_sids
        vi_protected = sid in vi_assigned_sids
        is_protected = cnam_protected or vi_protected or stir_shaken_active
        if is_protected:
            protected_count += 1
        numbers_detail.append({
            "phone": phone,
            "id": sid,
            "cnam_enabled": is_protected,
            "status": n.get('status', 'active'),
        })

    # Check live profile status from Twilio if we have a profile_sid
    profile_review_status = trust_hub.get('review_status', '')
    profile_sid = trust_hub.get('profile_sid', '')
    if profile_sid and protection_active:
        sub_auth_token_val = (vc or {}).get('twilio_auth_token', '')
        try:
            client = twilio_provisioning.get_sub_account_client_native(sub_sid, sub_auth_token_val)
            profile = client.trusthub.v1.customer_profiles(profile_sid).fetch()
            profile_review_status = getattr(profile, 'status', profile_review_status)
            # Persist
            if profile_review_status != trust_hub.get('review_status', ''):
                trust_hub['review_status'] = profile_review_status
                vc['trust_hub'] = trust_hub
                _save_voice_config(current_user.email, vc)
        except Exception as e:
            logger.warning(f"[spam-protection] Could not check live profile status: {e}")

    # Recompute profile_approved using live Twilio status (may have changed above)
    profile_approved = (protection_active and profile_exists
                        and profile_review_status in ('twilio-approved', 'compliant', 'approved'))

    # Include CNAM Trust Product status
    cnam = (vc or {}).get('cnam', {})
    cnam_info = {
        "registered": bool(cnam.get('trust_product_sid')),
        "status": cnam.get('status', 'not_registered'),
        "cnam_display_name": cnam.get('cnam_display_name', ''),
        "trust_product_sid": cnam.get('trust_product_sid', ''),
        "assigned_count": cnam.get('assigned_count', 0),
    }

    return jsonify({
        "protection_active": protection_active,
        "profile_approved": profile_approved,
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
        "numbers_protected": protected_count,
        "numbers_total": len(numbers_detail),
        "numbers": numbers_detail,
        "stir_shaken": "active" if profile_approved else ("pending" if profile_exists else "inactive"),
        "auto_cnam": trust_hub.get('auto_cnam', False),
        "profile_sid": profile_sid,
        "review_status": profile_review_status,
        "evaluation_issues": trust_hub.get('evaluation_issues', []),
        "cnam": cnam_info,
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

    Auto-discovers CNAM Trust Product from Twilio if not in local DB.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    trust_hub = (vc or {}).get('trust_hub', {})
    business_name = trust_hub.get('business_name', '')
    cnam = (vc or {}).get('cnam', {})

    # ── Auto-discover: if no trust_product_sid in local DB, check Twilio ──
    if not cnam.get('trust_product_sid'):
        sub_auth_token = (vc or {}).get('twilio_auth_token', '')
        try:
            discovered = twilio_provisioning.discover_cnam_trust_product(
                sub_account_sid=sub_sid,
                sub_account_auth_token=sub_auth_token,
            )
            if discovered:
                cnam['trust_product_sid'] = discovered['trust_product_sid']
                cnam['status'] = discovered['status']
                cnam['assigned_numbers'] = discovered.get('assigned_numbers', [])
                cnam['_sub_sid'] = sub_sid
                if discovered.get('cnam_display_name'):
                    cnam['cnam_display_name'] = discovered['cnam_display_name']
                vc['cnam'] = cnam
                _save_voice_config(current_user.email, vc)
                logger.info(f"[CNAM] Auto-discovered and synced Trust Product {discovered['trust_product_sid']}")
        except Exception as e:
            logger.warning(f"[CNAM] Auto-discovery failed: {e}")

    cnam_display_name = cnam.get('cnam_display_name', business_name[:15].strip() if business_name else '')

    numbers = twilio_provisioning.get_cnam_monitor(sub_sid)

    # Enrich with local nicknames and CNAM Trust Product assignment status
    nicknames = (vc or {}).get('number_nicknames', {})
    assigned_to_tp = set(cnam.get('assigned_numbers', []))

    # CNAM Trust Product status determines registration
    cnam_tp_approved = cnam.get('status', '') in ('twilio-approved', 'approved')

    result = []
    for n in numbers:
        phone = n.get('phone', '')
        friendly = n.get('friendly_name', '')
        sid = n.get('sid', '')
        in_tp = sid in assigned_to_tp
        # Registered = assigned to an approved CNAM Trust Product
        compliant = in_tp and cnam_tp_approved
        result.append({
            "phone": phone,
            "sid": sid,
            "cnam_name": friendly,
            "cnam_enabled": n.get('cnam_enabled', False),
            "cnam_compliant": compliant,
            "assigned_to_trust_product": in_tp,
            "nickname": nicknames.get(phone, ''),
            "date_created": n.get('date_created', ''),
        })

    return jsonify({
        "business_name": business_name,
        "cnam_display_name": cnam_display_name,
        "numbers": result,
        "total": len(result),
        "cnam_set": sum(1 for n in result if n['cnam_enabled']),
        "cnam_compliant": sum(1 for n in result if n['cnam_compliant']),
        "trust_product": {
            "registered": bool(cnam.get('trust_product_sid')),
            "status": cnam.get('status', 'not_registered'),
            "trust_product_sid": cnam.get('trust_product_sid', ''),
            "assigned_count": sum(1 for n in result if n['assigned_to_trust_product']),
        },
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
        _log_number_event(sub_sid, "cnam_updated", "success",
                          f"Caller ID updated: {cnam_name}",
                          {"number_sid": number_sid, "cnam_name": cnam_name})
        return jsonify(result)
    _log_number_event(sub_sid, "cnam_updated", "error",
                      f"Caller ID update failed for {number_sid}",
                      {"number_sid": number_sid, "cnam_name": cnam_name,
                       "error": result.get('error', 'Unknown')})
    return jsonify(result), 500


@numbers_bp.route('/voice/cnam/update-all', methods=['POST'])
@login_required
def cnam_update_all():
    """Apply CNAM business name to numbers that don't have any friendly name set."""
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

    _log_number_event(sub_sid, "cnam_update_all", "success" if not failed else "partial",
                      f"Caller ID bulk update: {updated} updated, {failed} failed",
                      {"cnam_name": cnam_name, "updated": updated, "failed": failed,
                       "already_set": len(numbers) - updated - failed})
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
# CNAM TRUST PRODUCT — STATUS & MANAGEMENT
# Poll CNAM registration status, add numbers to existing
# registration, and re-register after rejection.
# ──────────────────────────────────────────────────────────────


@numbers_bp.route('/voice/cnam/status', methods=['GET'])
@login_required
def cnam_trust_product_status():
    """
    Get current CNAM Trust Product registration status.
    Polls Twilio for live status and updates voice_config.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    cnam = (vc or {}).get('cnam', {})
    trust_product_sid = cnam.get('trust_product_sid', '')

    if not trust_product_sid:
        return jsonify({
            "registered": False,
            "status": "not_registered",
            "cnam_display_name": "",
        })

    # Only show data tagged to this sub-account
    if cnam.get('_sub_sid', '') and cnam['_sub_sid'] != sub_sid:
        return jsonify({
            "registered": False,
            "status": "not_registered",
            "cnam_display_name": "",
        })

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    try:
        live_status = twilio_provisioning.get_cnam_trust_product_status(
            sub_account_sid=sub_sid,
            trust_product_sid=trust_product_sid,
            sub_account_auth_token=sub_auth_token,
        )

        # Persist updated status
        old_status = cnam.get('status', '')
        new_status = live_status.get('status', old_status)
        if new_status != old_status:
            cnam['status'] = new_status
            if new_status == 'twilio-rejected':
                cnam['failure_reasons'] = live_status.get('failure_reasons', [])
            vc['cnam'] = cnam
            _save_voice_config(current_user.email, vc)

        return jsonify({
            "registered": True,
            "status": new_status,
            "cnam_display_name": cnam.get('cnam_display_name', ''),
            "trust_product_sid": trust_product_sid,
            "assigned_count": live_status.get('assigned_count', 0),
            "assigned_numbers": live_status.get('assigned_numbers', []),
            "date_created": live_status.get('date_created', ''),
            "failure_reasons": live_status.get('failure_reasons', cnam.get('failure_reasons', [])),
        })
    except Exception as e:
        logger.error(f"[CNAM] Status check failed: {e}")
        return jsonify({
            "registered": True,
            "status": cnam.get('status', 'unknown'),
            "cnam_display_name": cnam.get('cnam_display_name', ''),
            "trust_product_sid": trust_product_sid,
            "error": str(e),
        })


@numbers_bp.route('/voice/cnam/add-numbers', methods=['POST'])
@login_required
def cnam_add_numbers():
    """
    Add phone numbers to an existing CNAM Trust Product.
    Used when new numbers are purchased after initial registration.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    cnam = (vc or {}).get('cnam', {})
    trust_product_sid = cnam.get('trust_product_sid', '')
    profile_sid = cnam.get('profile_sid', '')

    if not trust_product_sid:
        return jsonify({"error": "No CNAM registration found. Complete Spam Protection first."}), 400

    data = request.json or {}
    phone_number_sids = data.get('phone_number_sids', [])

    if not phone_number_sids:
        # Default: add all numbers not yet assigned
        try:
            client = twilio_provisioning.get_sub_account_client(sub_sid)
            numbers = client.incoming_phone_numbers.list()
        except Exception as e:
            logger.error(f"CNAM add-numbers: failed to list numbers: {e}")
            return jsonify({"error": "Failed to fetch phone numbers from Twilio"}), 502
        already_assigned = set(cnam.get('assigned_numbers', []))
        phone_number_sids = [n.sid for n in numbers if n.sid not in already_assigned]

    if not phone_number_sids:
        return jsonify({"status": "ok", "message": "All numbers already assigned", "added": 0})

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    try:
        result = twilio_provisioning.assign_numbers_to_cnam(
            sub_account_sid=sub_sid,
            trust_product_sid=trust_product_sid,
            phone_number_sids=phone_number_sids,
            sub_account_auth_token=sub_auth_token,
            profile_sid=profile_sid,
        )

        # Update saved state
        existing = set(cnam.get('assigned_numbers', []))
        existing.update(phone_number_sids)
        cnam['assigned_numbers'] = list(existing)
        cnam['assigned_count'] = len(existing)
        vc['cnam'] = cnam
        _save_voice_config(current_user.email, vc)

        _log_number_event(sub_sid, "cnam_numbers_added", "success",
                          f"Caller ID: {result.get('assigned', 0)} numbers added",
                          {"assigned": result.get('assigned', 0),
                           "failed_count": len(result.get('failed', [])),
                           "total": len(existing)})

        return jsonify({
            "status": "ok",
            "added": result.get('assigned', 0),
            "failed": result.get('failed', []),
            "total_assigned": len(existing),
        })
    except Exception as e:
        logger.error(f"[CNAM] Add numbers failed: {e}")
        _log_number_event(sub_sid, "cnam_numbers_added", "error",
                          f"Caller ID: failed to add numbers",
                          {"error": str(e)})
        return jsonify({"error": "Internal server error"}), 500


@numbers_bp.route('/voice/cnam/remove-number', methods=['POST'])
@login_required
def cnam_remove_number():
    """Remove a phone number from the CNAM Trust Product."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    cnam = (vc or {}).get('cnam', {})
    trust_product_sid = cnam.get('trust_product_sid', '')

    if not trust_product_sid:
        return jsonify({"error": "No CNAM registration found"}), 400

    data = request.json or {}
    phone_number_sid = data.get('phone_number_sid', '').strip()
    if not phone_number_sid:
        return jsonify({"error": "phone_number_sid is required"}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    try:
        result = twilio_provisioning.unassign_numbers_from_trust_product(
            sub_account_sid=sub_sid,
            trust_product_sid=trust_product_sid,
            phone_number_sids=[phone_number_sid],
            sub_account_auth_token=sub_auth_token,
        )

        # Update saved state
        assigned = cnam.get('assigned_numbers', [])
        if phone_number_sid in assigned:
            assigned.remove(phone_number_sid)
            cnam['assigned_numbers'] = assigned
            cnam['assigned_count'] = len(assigned)
            vc['cnam'] = cnam
            _save_voice_config(current_user.email, vc)

        _log_number_event(sub_sid, "cnam_number_removed", "success",
                          f"Caller ID: number removed",
                          {"phone_sid": phone_number_sid, "total": len(assigned)})

        return jsonify({
            "status": "ok",
            "removed": result.get('removed', 0),
            "total_assigned": len(assigned),
        })
    except Exception as e:
        logger.error(f"[CNAM] Remove number failed: {e}")
        _log_number_event(sub_sid, "cnam_number_removed", "error",
                          f"Caller ID: failed to remove number",
                          {"phone_sid": phone_number_sid, "error": str(e)})
        return jsonify({"error": "Internal server error"}), 500


@numbers_bp.route('/voice/cnam/update-name', methods=['POST'])
@login_required
def cnam_update_display_name():
    """
    Update the CNAM display name (what shows on the lead's phone).
    Changes the EndUser attribute on the CNAM Trust Product.
    Propagates to carrier databases in 48-72 hours after Twilio processes.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    cnam = (vc or {}).get('cnam', {})
    end_user_sid = cnam.get('end_user_sid', '')
    trust_product_sid = cnam.get('trust_product_sid', '')
    if not end_user_sid or not trust_product_sid:
        return jsonify({"error": "No CNAM registration found. Register via Spam Protection first."}), 400

    data = request.json or {}
    new_name = data.get('display_name', '').strip()
    if not new_name:
        return jsonify({"error": "display_name is required"}), 400

    # Validate before hitting Twilio
    valid, validation_result = twilio_provisioning.validate_cnam_display_name(new_name)
    if not valid:
        return jsonify({"error": validation_result}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    location_id = subscriber['location_id']

    try:
        result = twilio_provisioning.update_cnam_display_name(
            sub_account_sid=sub_sid,
            end_user_sid=end_user_sid,
            new_display_name=new_name,
            sub_account_auth_token=sub_auth_token,
        )

        # Update cached config
        cnam['cnam_display_name'] = new_name.upper()
        vc['cnam'] = cnam
        _save_voice_config(current_user.email, vc)

        # Also update friendly_name on all assigned numbers
        assigned = cnam.get('assigned_numbers', [])
        if assigned:
            try:
                client = twilio_provisioning.get_sub_account_client_native(sub_sid, sub_auth_token)
                for pn_sid in assigned:
                    try:
                        client.incoming_phone_numbers(pn_sid).update(friendly_name=new_name.upper()[:15])
                    except Exception:
                        pass
            except Exception:
                pass

        log_webhook_event(location_id, "cnam_name_updated", "success",
                          f"Caller ID name changed to: {new_name.upper()}",
                          details={"old": cnam.get('cnam_display_name', ''), "new": new_name.upper()})

        return jsonify({
            "status": "ok",
            "cnam_display_name": new_name.upper(),
            "message": f"Caller ID name updated to '{new_name.upper()}'. Changes propagate to carrier databases in 48-72 hours.",
        })

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"[CNAM] Display name update failed: {e}", exc_info=True)
        return jsonify({"error": f"Update failed: {str(e)}"}), 500


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
    cached_status = ni.get('status', 'not_registered')
    live_status = cached_status
    assigned_numbers = ni.get('assigned_numbers', [])
    assigned_count = ni.get('assigned_count', 0)

    # Status progression order — never regress to an earlier status.
    # Twilio's Trust Hub API has eventual consistency: a .fetch() right after
    # .update(status="pending-review") may still return "draft". Guard against
    # overwriting a more-advanced cached status with a stale live read.
    _STATUS_ORDER = {
        'not_registered': 0,
        'draft': 1,
        'pending-review': 2,
        'in-review': 3,
        'twilio-approved': 4,
        'twilio-rejected': 4,  # same rank as approved (terminal state)
    }

    if trust_product_sid:
        try:
            live = twilio_provisioning.get_voice_integrity_status(
                sub_sid, trust_product_sid, sub_auth_token)
            twilio_status = live.get('status', '')
            assigned_numbers = live.get('assigned_numbers', assigned_numbers)
            assigned_count = live.get('assigned_count', assigned_count)

            # Only update status if the live status is at least as advanced as cached,
            # OR if live says rejected (terminal — always trust Twilio's rejection).
            twilio_rank = _STATUS_ORDER.get(twilio_status, -1)
            cached_rank = _STATUS_ORDER.get(cached_status, -1)
            if twilio_rank >= cached_rank:
                live_status = twilio_status
            else:
                logger.info(
                    f"[NumberIntegrity] Ignoring stale Twilio status '{twilio_status}' "
                    f"(cached='{cached_status}') — eventual consistency"
                )
                live_status = cached_status

            # Persist if status actually changed
            if live_status != cached_status or ni.get('assigned_count') != assigned_count:
                ni['status'] = live_status
                ni['assigned_numbers'] = assigned_numbers
                ni['assigned_count'] = assigned_count
                if live_status == 'twilio-rejected':
                    ni['failure_reasons'] = live.get('failure_reasons', ni.get('failure_reasons', []))
                vc['number_integrity'] = ni
                _save_voice_config(current_user.email, vc)
        except Exception as e:
            logger.warning(f"[NumberIntegrity] Live status check failed, using cached: {e}")

    # Get all numbers on the sub-account for the UI
    # A number is only truly "registered" if it's assigned to an APPROVED Trust Product.
    # If the Trust Product is rejected/draft/not_registered, numbers are NOT registered
    # even if they were previously submitted — the assignment is meaningless without approval.
    is_truly_registered = live_status in ('twilio-approved', 'pending-review', 'in-review')
    all_numbers = []
    try:
        nums = twilio_provisioning.list_phone_numbers(sub_sid)
        for n in nums:
            pn_sid = n.get('sid', '')
            all_numbers.append({
                "phone": n.get('phone', ''),
                "sid": pn_sid,
                "friendly_name": n.get('friendly_name', ''),
                "registered": is_truly_registered and pn_sid in assigned_numbers,
                "assigned": pn_sid in assigned_numbers,  # was submitted (may or may not be approved)
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

    # Collect failure reasons if rejected
    failure_reasons = ni.get('failure_reasons', [])
    if live_status == 'twilio-rejected' and trust_product_sid:
        try:
            live = twilio_provisioning.get_voice_integrity_status(
                sub_sid, trust_product_sid, sub_auth_token)
            failure_reasons = live.get('failure_reasons', failure_reasons)
            if failure_reasons:
                ni['failure_reasons'] = failure_reasons
                vc['number_integrity'] = ni
                _save_voice_config(current_user.email, vc)
        except Exception:
            pass  # use cached failure_reasons

    return jsonify({
        "status": live_status,
        "display": display,
        "trust_product_sid": trust_product_sid,
        "profile_sid": ni.get('profile_sid', ''),
        "end_user_sid": ni.get('end_user_sid', ''),
        "business_name": ni.get('business_name', ''),
        "registered_at": ni.get('registered_at', ''),
        "assigned_count": assigned_count,
        "numbers": all_numbers,
        "carriers": twilio_provisioning.VOICE_INTEGRITY_CARRIERS,
        "failure_reasons": failure_reasons if live_status == 'twilio-rejected' else [],
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

    # ── Gate: Require approved Secondary Customer Profile ──
    # Per Twilio ISV docs, Trust Products (Voice Integrity) linked to an
    # unapproved Secondary Profile will get stuck in "draft" and cannot
    # be submitted for review. Gate here to prevent wasted API calls.
    profile_sid = trust_hub.get('profile_sid', '')
    profile_check = twilio_provisioning.check_secondary_profile_status(
        sub_account_sid=sub_sid,
        sub_account_auth_token=sub_auth_token,
        profile_sid=profile_sid,
    )
    if not profile_check['approved']:
        return jsonify({
            "error": profile_check['message'],
            "profile_status": profile_check['status'],
            "profile_pending": profile_check['status'] in ('in-review', 'pending-review'),
        }), 409

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
        # Step 1: Create Trust Product if we don't have one, or if the old one is rejected/failed.
        # ALWAYS check live Twilio status — cached status may be stale (e.g. still says
        # "pending-review" after Twilio rejected it asynchronously).
        old_tp_sid = trust_product_sid
        need_new_product = not trust_product_sid

        if trust_product_sid:
            # Live-check the existing Trust Product status from Twilio
            try:
                live = twilio_provisioning.get_voice_integrity_status(
                    sub_sid, trust_product_sid, sub_auth_token)
                live_status = live.get('status', '')
                logger.info(f"[NumberIntegrity] Existing TP {trust_product_sid} live status: {live_status}")
                if live_status == 'twilio-rejected':
                    need_new_product = True
            except Exception as e:
                logger.warning(f"[NumberIntegrity] Could not check TP status, using cached: {e}")
                # Fall back to cached status
                if ni.get('status', '') == 'twilio-rejected':
                    need_new_product = True

        if need_new_product and old_tp_sid:
            logger.info(f"[NumberIntegrity] Old Trust Product {old_tp_sid} needs replacement, unassigning all numbers")
            # Unassign ALL numbers from old Trust Product — pass empty list to unassign everything
            # found on Twilio, not just what's in our cached assigned_numbers list
            unassign_result = twilio_provisioning.unassign_numbers_from_trust_product(
                sub_account_sid=sub_sid,
                trust_product_sid=old_tp_sid,
                phone_number_sids=[],  # empty = unassign ALL found on TP
                sub_account_auth_token=sub_auth_token,
            )
            logger.info(f"[NumberIntegrity] Unassigned {unassign_result.get('removed', 0)} numbers from old TP {old_tp_sid}")
            if unassign_result.get('failed'):
                logger.warning(f"[NumberIntegrity] Some unassigns failed: {unassign_result['failed']}")
            ni['old_trust_product_sid'] = old_tp_sid
            ni['assigned_numbers'] = []
            ni['assigned_count'] = 0
            trust_product_sid = ''  # force creation of new product

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

        log_webhook_event(
            location_id=location_id,
            event_type="voice_protection",
            status="success",
            summary=f"Number Integrity registered — {assign_result.get('assigned', 0)} numbers submitted for carrier review",
            details={
                "trust_product_sid": trust_product_sid,
                "review_status": ni['status'],
                "numbers_assigned": assign_result.get('assigned', 0),
            },
        )

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

        log_webhook_event(
            location_id=location_id,
            event_type="voice_protection",
            status="error",
            summary=f"Number Integrity registration failed — {str(e)[:200]}",
            details={"error": str(e)[:500]},
        )
        save_persistent_alert(
            email=current_user.email,
            alert_type="voice_integrity_failed",
            title="Number Integrity Registration Failed",
            message=f"Could not register numbers for carrier spam protection. Error: {str(e)[:200]}",
            severity="error",
            location_id=location_id,
        )

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

        _log_number_event(sub_sid, "voice_integrity_numbers_added", "success",
                          f"Voice Integrity: {result.get('assigned', 0)} numbers added",
                          {"assigned": result.get('assigned', 0),
                           "failed": len(result.get('failed', [])),
                           "trust_product_sid": trust_product_sid})

        return jsonify({
            "status": "ok",
            "numbers_assigned": result.get('assigned', 0),
            "numbers_failed": len(result.get('failed', [])),
            "failed_details": result.get('failed', []),
        })
    except Exception as e:
        logger.error(f"[NumberIntegrity] Add numbers failed: {e}", exc_info=True)
        _log_number_event(sub_sid, "voice_integrity_numbers_added", "error",
                          f"Voice Integrity: failed to add numbers",
                          {"error": str(e), "trust_product_sid": trust_product_sid})
        return jsonify({"error": "Internal server error"}), 500


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
        _log_number_event(sub_sid, "voice_integrity_number_removed",
                          "success" if removed else "warning",
                          f"Voice Integrity: number {'removed' if removed else 'not found'}",
                          {"phone_sid": phone_sid, "removed": removed})
        return jsonify({"status": "ok", "removed": removed})
    except Exception as e:
        logger.error(f"[NumberIntegrity] Remove number failed: {e}", exc_info=True)
        _log_number_event(sub_sid, "voice_integrity_number_removed", "error",
                          f"Voice Integrity: failed to remove number",
                          {"phone_sid": phone_sid, "error": str(e)})
        return jsonify({"error": "Internal server error"}), 500


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


@numbers_bp.route('/voice/number-integrity/resubmit', methods=['POST'])
@login_required
def number_integrity_resubmit():
    """
    Resubmit a rejected Voice Integrity registration.
    Resets the Trust Product to draft, then re-submits for review.
    Use after fixing issues that caused the original rejection.
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
        # Check current status
        try:
            status = twilio_provisioning.get_voice_integrity_status(
                sub_sid, trust_product_sid, sub_auth_token)
            current_status = status.get('status', '')
        except Exception:
            current_status = ni.get('status', '')

        if current_status in ('pending-review', 'in-review'):
            return jsonify({
                "error": "Registration is already under review. Please allow 24-48 hours.",
                "current_status": current_status,
            }), 409

        if current_status not in ('twilio-rejected', 'draft'):
            return jsonify({
                "error": f"Resubmit is only available for rejected registrations (current: {current_status}).",
            }), 400

        # Resubmit: create new Trust Product (can't recycle rejected ones)
        trust_hub = (vc or {}).get('trust_hub', {})
        result = twilio_provisioning.resubmit_voice_integrity(
            sub_account_sid=sub_sid,
            trust_product_sid=trust_product_sid,
            sub_account_auth_token=sub_auth_token,
            business_name=ni.get('business_name', trust_hub.get('business_name', '')),
            contact_email=trust_hub.get('contact_email', '') or getattr(current_user, 'email', ''),
            existing_profile_sid=ni.get('profile_sid', ''),
        )

        # Update to new Trust Product SID
        ni['trust_product_sid'] = result.get('trust_product_sid', trust_product_sid)
        ni['old_trust_product_sid'] = trust_product_sid  # Keep reference
        ni['status'] = result.get('status', 'pending-review')
        ni['assigned_numbers'] = result.get('assigned_numbers', ni.get('assigned_numbers', []))
        ni['assigned_count'] = len(ni['assigned_numbers'])
        ni['last_resubmit'] = datetime.utcnow().isoformat()
        ni.pop('failure_reasons', None)  # Clear old failure reasons
        vc['number_integrity'] = ni
        _save_voice_config(current_user.email, vc)

        return jsonify({
            "status": "ok",
            "message": "Registration resubmitted for review. Carrier registration typically takes 24-48 hours.",
            "review_status": ni['status'],
        })

    except Exception as e:
        logger.error(f"[NumberIntegrity] Resubmit failed: {e}", exc_info=True)
        return jsonify({"error": f"Resubmit failed: {str(e)}"}), 500


@numbers_bp.route('/voice/number-integrity/update-info', methods=['POST'])
@login_required
def number_integrity_update_info():
    """
    Update Voice Integrity EndUser attributes before resubmitting.
    Allows correcting employee count and call volume after rejection.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    ni = vc.get('number_integrity', {})
    end_user_sid = ni.get('end_user_sid', '')
    if not end_user_sid:
        return jsonify({"error": "No EndUser found. Register first."}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    data = request.json or {}

    employee_count = data.get('employee_count', '').strip()
    call_volume = data.get('call_volume', '').strip()

    # Validate: must be positive integers as strings
    for val, name in [(employee_count, 'employee_count'), (call_volume, 'call_volume')]:
        if val and (not val.isdigit() or int(val) < 1):
            return jsonify({"error": f"{name} must be a positive integer"}), 400

    try:
        result = twilio_provisioning.update_voice_integrity_end_user(
            sub_account_sid=sub_sid,
            end_user_sid=end_user_sid,
            sub_account_auth_token=sub_auth_token,
            business_employee_count=employee_count,
            average_call_volume=call_volume,
        )

        return jsonify({
            "status": "ok",
            "message": "Business information updated. You can now resubmit.",
            "attributes": result.get('attributes', {}),
        })

    except Exception as e:
        logger.error(f"[NumberIntegrity] Update info failed: {e}", exc_info=True)
        return jsonify({"error": f"Update failed: {str(e)}"}), 500


# ──────────────────────────────────────────────────────────────
# SHAKEN/STIR — Full attestation (A-level) for outbound calls.
# Tells carriers "this call is legitimate." Improves answer rates
# beyond Voice Integrity alone.
# ──────────────────────────────────────────────────────────────


@numbers_bp.route('/voice/shaken-stir/status')
@login_required
def shaken_stir_status():
    """
    SHAKEN/STIR registration status.
    Returns current status, assigned numbers, and carrier display info.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    ss = vc.get('shaken_stir', {})
    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    tp_sid = ss.get('trust_product_sid', '')

    _STATUS_ORDER = {
        'not_registered': 0, 'draft': 1, 'pending-review': 2,
        'in-review': 3, 'twilio-approved': 4, 'twilio-rejected': 4,
    }

    if not tp_sid:
        return jsonify({
            "registered": False,
            "status": "not_registered",
            "display": {"label": "Not Registered", "color": "gray", "icon": "fa-shield-halved"},
            "numbers": [],
        })

    # Live status from Twilio
    try:
        live = twilio_provisioning.get_shaken_stir_status(sub_sid, tp_sid, sub_auth_token)
        live_status = live.get('status', ss.get('status', 'draft'))

        # Status monotonicity — never regress due to Twilio eventual consistency
        cached_status = ss.get('status', 'not_registered')
        if _STATUS_ORDER.get(live_status, 0) >= _STATUS_ORDER.get(cached_status, 0):
            if live_status != cached_status:
                ss['status'] = live_status
                if live_status == 'twilio-approved':
                    ss['approved_at'] = datetime.utcnow().isoformat()
                vc['shaken_stir'] = ss
                _save_voice_config(current_user.email, vc)

        assigned_sids = set(live.get('assigned_numbers', []))
        ss_assigned = set(ss.get('assigned_numbers', []))
        if assigned_sids != ss_assigned:
            ss['assigned_numbers'] = list(assigned_sids)
            ss['assigned_count'] = len(assigned_sids)
            vc['shaken_stir'] = ss
            _save_voice_config(current_user.email, vc)

    except Exception as e:
        logger.warning(f"[SHAKEN/STIR] Live status check failed: {e}")
        live_status = ss.get('status', 'draft')
        assigned_sids = set(ss.get('assigned_numbers', []))

    # Status display mapping
    display_map = {
        'draft': {"label": "Draft", "color": "yellow", "icon": "fa-file-pen"},
        'pending-review': {"label": "Pending Review", "color": "blue", "icon": "fa-clock"},
        'in-review': {"label": "Under Review", "color": "blue", "icon": "fa-magnifying-glass"},
        'twilio-approved': {"label": "Approved", "color": "green", "icon": "fa-circle-check"},
        'twilio-rejected': {"label": "Rejected", "color": "red", "icon": "fa-circle-xmark"},
    }
    display = display_map.get(live_status, {"label": live_status, "color": "gray", "icon": "fa-question"})

    # List all numbers on the sub-account with registration status
    numbers = []
    try:
        client = twilio_provisioning.get_sub_account_client_native(sub_sid, sub_auth_token)
        phone_numbers = client.incoming_phone_numbers.list(limit=100)
        for num in phone_numbers:
            numbers.append({
                "sid": num.sid,
                "phone_number": num.phone_number,
                "friendly_name": num.friendly_name or num.phone_number,
                "assigned": num.sid in assigned_sids,
                "registered": live_status == 'twilio-approved' and num.sid in assigned_sids,
            })
    except Exception as e:
        logger.warning(f"[SHAKEN/STIR] Could not list numbers: {e}")

    result = {
        "registered": bool(tp_sid),
        "status": live_status,
        "display": display,
        "trust_product_sid": tp_sid,
        "profile_sid": ss.get('profile_sid', ''),
        "assigned_numbers": list(assigned_sids),
        "assigned_count": len(assigned_sids),
        "numbers": numbers,
        "registered_at": ss.get('registered_at', ''),
    }

    if live_status == 'twilio-rejected':
        result["failure_reasons"] = ss.get('failure_reasons', [])

    return jsonify(result)


@numbers_bp.route('/voice/shaken-stir/register', methods=['POST'])
@login_required
def shaken_stir_register():
    """
    Register phone numbers for SHAKEN/STIR attestation.
    Requires an approved Secondary Customer Profile (from spam protection).
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    data = request.json or {}
    phone_sids = data.get('phone_number_sids', [])
    if not phone_sids:
        return jsonify({"error": "Select at least one phone number"}), 400

    trust_hub = vc.get('trust_hub', {})
    business_name = trust_hub.get('business_name', '')
    contact_email = trust_hub.get('contact_email', subscriber.get('email', ''))
    if not business_name:
        return jsonify({"error": "Complete your business profile in Spam Protection first"}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    location_id = subscriber['location_id']
    ss = vc.get('shaken_stir', {})

    # Gate: Secondary Profile must be approved
    try:
        profile_check = twilio_provisioning.check_secondary_profile_status(
            sub_sid, sub_auth_token, existing_profile_sid=ss.get('profile_sid', ''))
        if not profile_check.get('approved'):
            pstatus = profile_check.get('status', 'unknown')
            if pstatus in ('pending-review', 'in-review'):
                return jsonify({"error": "Your business profile is still under review. SHAKEN/STIR registration will be available once approved."}), 409
            elif pstatus == 'twilio-rejected':
                return jsonify({"error": "Your business profile was rejected. Please update your Spam Protection profile and resubmit."}), 409
            else:
                return jsonify({"error": "Register for Spam Protection first to create your business profile."}), 400
    except Exception as e:
        logger.error(f"[SHAKEN/STIR] Profile check failed: {e}")
        return jsonify({"error": f"Could not verify business profile: {str(e)}"}), 500

    try:
        # Check if existing TP needs replacement (rejected)
        tp_sid = ss.get('trust_product_sid', '')
        needs_new_tp = True
        if tp_sid:
            try:
                live = twilio_provisioning.get_shaken_stir_status(sub_sid, tp_sid, sub_auth_token)
                live_status = live.get('status', '')
                if live_status == 'twilio-rejected':
                    logger.info(f"[SHAKEN/STIR] Existing TP {tp_sid} rejected, will create new")
                    twilio_provisioning.unassign_numbers_from_trust_product(
                        sub_sid, tp_sid, [], sub_auth_token)
                    ss['assigned_numbers'] = []
                elif live_status in ('twilio-approved', 'pending-review', 'in-review'):
                    needs_new_tp = False
                elif live_status == 'draft':
                    needs_new_tp = False
            except Exception:
                pass

        if needs_new_tp:
            result = twilio_provisioning.create_shaken_stir_trust_product(
                sub_account_sid=sub_sid,
                business_name=business_name,
                contact_email=contact_email,
                sub_account_auth_token=sub_auth_token,
                existing_profile_sid=ss.get('profile_sid', ''),
            )
            tp_sid = result['trust_product_sid']
            ss['trust_product_sid'] = tp_sid
            ss['profile_sid'] = result.get('profile_sid', '')
            ss['business_name'] = business_name
            ss['registered_at'] = datetime.utcnow().isoformat()
            vc['shaken_stir'] = ss
            _save_voice_config(current_user.email, vc)

        # Assign numbers
        assign_result = twilio_provisioning.assign_numbers_to_shaken_stir(
            sub_account_sid=sub_sid,
            trust_product_sid=tp_sid,
            phone_number_sids=phone_sids,
            sub_account_auth_token=sub_auth_token,
            profile_sid=ss.get('profile_sid', ''),
        )

        current_assigned = set(ss.get('assigned_numbers', []))
        current_assigned.update(phone_sids)
        ss['assigned_numbers'] = list(current_assigned)
        ss['assigned_count'] = len(current_assigned)

        # Submit for review
        submit_result = twilio_provisioning.submit_shaken_stir_for_review(
            sub_account_sid=sub_sid,
            trust_product_sid=tp_sid,
            sub_account_auth_token=sub_auth_token,
        )
        ss['status'] = submit_result.get('status', 'pending-review')
        vc['shaken_stir'] = ss
        _save_voice_config(current_user.email, vc)

        log_webhook_event(location_id, "shaken_stir_registered", "success",
                          f"SHAKEN/STIR submitted with {len(phone_sids)} numbers",
                          details={"trust_product_sid": tp_sid, "numbers": len(phone_sids)})

        return jsonify({
            "status": "ok",
            "message": f"SHAKEN/STIR registration submitted with {assign_result.get('assigned', 0)} numbers. Approval typically takes 24-48 hours.",
            "trust_product_sid": tp_sid,
            "assigned": assign_result.get('assigned', 0),
        })

    except Exception as e:
        logger.error(f"[SHAKEN/STIR] Registration failed: {e}", exc_info=True)
        if ss.get('trust_product_sid'):
            vc['shaken_stir'] = ss
            _save_voice_config(current_user.email, vc)
        save_persistent_alert(location_id, f"SHAKEN/STIR registration failed: {str(e)}", level="error")
        return jsonify({"error": f"Registration failed: {str(e)}"}), 500


@numbers_bp.route('/voice/shaken-stir/add-numbers', methods=['POST'])
@login_required
def shaken_stir_add_numbers():
    """Add phone numbers to an existing SHAKEN/STIR registration."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    ss = vc.get('shaken_stir', {})
    tp_sid = ss.get('trust_product_sid', '')
    if not tp_sid:
        return jsonify({"error": "Register for SHAKEN/STIR first"}), 400

    data = request.json or {}
    phone_sids = data.get('phone_number_sids', [])
    if not phone_sids:
        return jsonify({"error": "Select at least one phone number"}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    location_id = subscriber['location_id']

    try:
        result = twilio_provisioning.assign_numbers_to_shaken_stir(
            sub_account_sid=sub_sid,
            trust_product_sid=tp_sid,
            phone_number_sids=phone_sids,
            sub_account_auth_token=sub_auth_token,
            profile_sid=ss.get('profile_sid', ''),
        )

        current_assigned = set(ss.get('assigned_numbers', []))
        current_assigned.update(phone_sids)
        ss['assigned_numbers'] = list(current_assigned)
        ss['assigned_count'] = len(current_assigned)
        vc['shaken_stir'] = ss
        _save_voice_config(current_user.email, vc)

        return jsonify({
            "status": "ok",
            "assigned": result.get('assigned', 0),
            "failed": result.get('failed', []),
        })
    except Exception as e:
        logger.error(f"[SHAKEN/STIR] Add numbers failed: {e}", exc_info=True)
        return jsonify({"error": f"Failed: {str(e)}"}), 500


@numbers_bp.route('/voice/shaken-stir/remove-number', methods=['POST'])
@login_required
def shaken_stir_remove_number():
    """Remove a phone number from SHAKEN/STIR registration."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    ss = vc.get('shaken_stir', {})
    tp_sid = ss.get('trust_product_sid', '')
    if not tp_sid:
        return jsonify({"error": "Not registered for SHAKEN/STIR"}), 400

    data = request.json or {}
    pn_sid = data.get('phone_number_sid', '')
    if not pn_sid:
        return jsonify({"error": "phone_number_sid required"}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    location_id = subscriber['location_id']

    try:
        twilio_provisioning.remove_number_from_shaken_stir(
            sub_account_sid=sub_sid,
            trust_product_sid=tp_sid,
            phone_number_sid=pn_sid,
            sub_account_auth_token=sub_auth_token,
        )

        assigned = set(ss.get('assigned_numbers', []))
        assigned.discard(pn_sid)
        ss['assigned_numbers'] = list(assigned)
        ss['assigned_count'] = len(assigned)
        vc['shaken_stir'] = ss
        _save_voice_config(current_user.email, vc)

        return jsonify({"status": "ok", "message": "Number removed"})
    except Exception as e:
        logger.error(f"[SHAKEN/STIR] Remove number failed: {e}", exc_info=True)
        return jsonify({"error": f"Failed: {str(e)}"}), 500


@numbers_bp.route('/voice/shaken-stir/resubmit', methods=['POST'])
@login_required
def shaken_stir_resubmit():
    """Resubmit a rejected SHAKEN/STIR registration (creates new Trust Product)."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    ss = vc.get('shaken_stir', {})
    tp_sid = ss.get('trust_product_sid', '')
    if not tp_sid:
        return jsonify({"error": "Not registered for SHAKEN/STIR"}), 400

    status = ss.get('status', '')
    if status not in ('twilio-rejected', 'draft'):
        return jsonify({"error": f"Cannot resubmit — current status is {status}"}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    location_id = subscriber['location_id']
    trust_hub = vc.get('trust_hub', {})
    business_name = trust_hub.get('business_name', '')
    contact_email = trust_hub.get('contact_email', subscriber.get('email', ''))

    try:
        result = twilio_provisioning.resubmit_shaken_stir(
            sub_account_sid=sub_sid,
            trust_product_sid=tp_sid,
            sub_account_auth_token=sub_auth_token,
            business_name=business_name,
            contact_email=contact_email,
            existing_profile_sid=ss.get('profile_sid', ''),
        )

        ss['trust_product_sid'] = result['trust_product_sid']
        ss['old_trust_product_sid'] = result.get('old_trust_product_sid', tp_sid)
        ss['status'] = result.get('status', 'pending-review')
        ss['assigned_numbers'] = result.get('assigned_numbers', [])
        ss['assigned_count'] = len(ss['assigned_numbers'])
        ss['failure_reasons'] = []
        ss['last_resubmit'] = datetime.utcnow().isoformat()
        vc['shaken_stir'] = ss
        _save_voice_config(current_user.email, vc)

        log_webhook_event(location_id, "shaken_stir_resubmitted", "success",
                          f"SHAKEN/STIR resubmitted: {result['trust_product_sid']}")

        return jsonify({
            "status": "ok",
            "message": "SHAKEN/STIR resubmitted for review.",
            "trust_product_sid": result['trust_product_sid'],
        })
    except Exception as e:
        logger.error(f"[SHAKEN/STIR] Resubmit failed: {e}", exc_info=True)
        return jsonify({"error": f"Resubmit failed: {str(e)}"}), 500


# ──────────────────────────────────────────────────────────────
# TRUST HUB STATUS CALLBACK
# Twilio POSTs here when a Customer Profile, Trust Product, or
# Brand Registration changes status (e.g., pending → approved).
# ──────────────────────────────────────────────────────────────


@numbers_bp.route('/voice/trust-hub-status', methods=['POST'])
def trust_hub_status_callback():
    """
    Twilio Trust Hub status callback webhook.
    Receives status change notifications for Customer Profiles,
    Trust Products (CNAM, Voice Integrity, A2P), and Brand Registrations.

    No @login_required — Twilio calls this server-to-server.
    Validates request via Twilio signature.
    """
    from twilio.request_validator import RequestValidator

    auth_token = os.getenv('TWILIO_AUTH_TOKEN', '')
    validator = RequestValidator(auth_token)

    # Build the full URL Twilio signed against
    url = request.url
    # If behind a proxy, use the X-Forwarded-Proto scheme
    if request.headers.get('X-Forwarded-Proto') == 'https':
        url = url.replace('http://', 'https://', 1)

    signature = request.headers.get('X-Twilio-Signature', '')
    params = request.form.to_dict()

    if not validator.validate(url, params, signature):
        logger.warning("[TrustHub-Callback] Invalid Twilio signature — rejecting")
        return '', 403

    # Extract status update fields from Twilio's POST body
    resource_sid = params.get('ResourceSid', '')
    status = params.get('Status', '')
    resource_type = params.get('ResourceType', '')  # e.g. customer_profile, trust_product
    account_sid = params.get('AccountSid', '')

    logger.info(
        f"[TrustHub-Callback] {resource_type} {resource_sid} → {status} "
        f"(account={account_sid})"
    )

    # Find the subscriber whose voice_config references this SID
    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Search voice_config JSONB for the resource SID across all relevant keys
        cur.execute("""
            SELECT location_id, email, voice_config
            FROM subscribers
            WHERE voice_config::text LIKE %s
            LIMIT 1
        """, (f'%{resource_sid}%',))
        row = cur.fetchone()

        if not row:
            logger.info(f"[TrustHub-Callback] No subscriber found for SID {resource_sid} — may be master account")
            return '', 204

        location_id, email, vc = row
        vc = vc or {}

        updated = False

        # Update the matching section in voice_config
        # Customer Profile status
        trust_hub = vc.get('trust_hub', {})
        if trust_hub.get('profile_sid') == resource_sid:
            trust_hub['review_status'] = status
            if status in ('twilio-approved', 'approved', 'compliant'):
                trust_hub['protection_active'] = True
            vc['trust_hub'] = trust_hub
            updated = True
            logger.info(f"[TrustHub-Callback] Updated trust_hub.review_status={status} for {email}")

        # CNAM Trust Product
        cnam = vc.get('cnam', {})
        if cnam.get('trust_product_sid') == resource_sid:
            cnam['status'] = status
            vc['cnam'] = cnam
            updated = True
            logger.info(f"[TrustHub-Callback] Updated cnam.status={status} for {email}")

        # Voice Integrity Trust Product
        ni = vc.get('number_integrity', {})
        if ni.get('trust_product_sid') == resource_sid:
            ni['status'] = status
            vc['number_integrity'] = ni
            updated = True
            logger.info(f"[TrustHub-Callback] Updated number_integrity.status={status} for {email}")

        # SHAKEN/STIR Trust Product
        ss = vc.get('shaken_stir', {})
        if ss.get('trust_product_sid') == resource_sid:
            ss['status'] = status
            vc['shaken_stir'] = ss
            updated = True
            logger.info(f"[TrustHub-Callback] Updated shaken_stir.status={status} for {email}")

        # A2P Brand/Campaign
        a2p = vc.get('a2p', {})
        if a2p.get('brand_sid') == resource_sid:
            a2p['brand_status'] = status
            vc['a2p'] = a2p
            updated = True
            logger.info(f"[TrustHub-Callback] Updated a2p.brand_status={status} for {email}")
        if a2p.get('campaign_sid') == resource_sid:
            a2p['campaign_status'] = status
            vc['a2p'] = a2p
            updated = True
            logger.info(f"[TrustHub-Callback] Updated a2p.campaign_status={status} for {email}")

        if updated:
            import json as _json
            cur.execute(
                "UPDATE subscribers SET voice_config = %s WHERE location_id = %s",
                (_json.dumps(vc), location_id)
            )
            conn.commit()

            # Log it as an activity event
            log_webhook_event(
                location_id,
                f"Trust Hub status update: {resource_type} → {status}",
                {"resource_sid": resource_sid, "status": status, "type": resource_type},
            )
        else:
            logger.info(f"[TrustHub-Callback] SID {resource_sid} found in text but no matching field for {email}")

    except Exception as e:
        logger.error(f"[TrustHub-Callback] Error processing status update: {e}", exc_info=True)
    finally:
        if conn:
            return_db_connection(conn)

    return '', 204


# ──────────────────────────────────────────────────────────────
# HIYA CONNECT — BRANDED CALLING MANAGEMENT
# Direct Hiya API routes for brand status, adding numbers,
# removing numbers, and reputation queries (Protect API).
# ──────────────────────────────────────────────────────────────


@numbers_bp.route('/voice/hiya/status', methods=['GET'])
@login_required
def hiya_status():
    """
    Get Hiya Connect registration status for the current subscriber.
    Includes brand vetting status and list of registered phone numbers.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    hiya_data = vc.get('hiya_connect', {})
    brand_id = hiya_data.get('brand_id', '')

    if not hiya_connect.is_connect_configured():
        return jsonify({
            "configured": False,
            "message": "Hiya Connect credentials not set. Add HIYA_CONNECT_APP_ID and HIYA_CONNECT_APP_SECRET to environment.",
        })

    if not brand_id:
        return jsonify({
            "configured": True,
            "registered": False,
            "message": "No Hiya brand registered yet. Submit your business profile to register.",
        })

    # Poll live brand status from Hiya
    brand_info = None
    try:
        brand_info = hiya_connect.get_brand(brand_id)
    except Exception as e:
        logger.warning(f"[Hiya] Could not fetch live brand status: {e}")

    live_status = None
    if brand_info:
        live_status = brand_info.get("status") or brand_info.get("verificationStatus")
        if live_status and live_status != hiya_data.get('brand_status'):
            hiya_data['brand_status'] = live_status
            vc['hiya_connect'] = hiya_data
            _save_voice_config(current_user.email, vc)

    return jsonify({
        "configured": True,
        "registered": True,
        "brand_id": brand_id,
        "brand_status": live_status or hiya_data.get('brand_status', 'PENDING_REVIEW'),
        "display_name": hiya_data.get('display_name', ''),
        "registered_phones": hiya_data.get('registered_phones', []),
        "registered_count": hiya_data.get('registered_count', 0),
        "registered_at": hiya_data.get('registered_at', ''),
        "note": "Branded display activates once brand is VERIFIED (48-72 hours after registration).",
    })


@numbers_bp.route('/voice/hiya/add-numbers', methods=['POST'])
@login_required
def hiya_add_numbers():
    """
    Add phone numbers to an existing Hiya Connect brand registration.
    Accepts a list of E.164 numbers, or omit to auto-add all unregistered numbers.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    if not hiya_connect.is_connect_configured():
        return jsonify({"error": "Hiya Connect not configured"}), 400

    hiya_data = vc.get('hiya_connect', {})
    brand_id = hiya_data.get('brand_id', '')
    if not brand_id:
        return jsonify({"error": "No Hiya brand registered yet. Register via Spam Protection first."}), 400

    data = request.json or {}
    phone_list = data.get('phones', [])  # E.164 list, or empty to auto-fetch all

    if not phone_list:
        # Auto-fetch all numbers from sub-account not yet registered
        try:
            client = twilio_provisioning.get_sub_account_client(sub_sid)
            numbers = client.incoming_phone_numbers.list()
            already_registered = set(hiya_data.get('registered_phones', []))
            phone_list = [n.phone_number for n in numbers
                          if n.phone_number and n.phone_number not in already_registered]
        except Exception as e:
            return jsonify({"error": f"Could not fetch phone numbers: {e}"}), 500

    if not phone_list:
        return jsonify({"status": "ok", "message": "All numbers already registered with Hiya", "added": 0})

    trust_hub = vc.get('trust_hub', {})
    display_name = hiya_data.get('display_name') or trust_hub.get('business_name', '')[:15].strip()
    state = trust_hub.get('state', '')
    city = trust_hub.get('city', '')

    try:
        result = hiya_connect.register_numbers_batch(
            phone_e164_list=phone_list,
            brand_id=brand_id,
            display_name=display_name,
            call_reason="Insurance Sales",
            state=state,
            city=city,
        )

        # Update persisted list
        existing = set(hiya_data.get('registered_phones', []))
        for item in result.get('registered', []):
            existing.add(item['phone'])
        hiya_data['registered_phones'] = list(existing)
        hiya_data['registered_count'] = len(existing)
        vc['hiya_connect'] = hiya_data
        _save_voice_config(current_user.email, vc)

        return jsonify({
            "status": "ok",
            "added": len(result.get('registered', [])),
            "failed": len(result.get('failed', [])),
            "failures": result.get('failed', []),
        })
    except Exception as e:
        logger.error(f"[Hiya] add-numbers failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@numbers_bp.route('/voice/hiya/remove-number', methods=['POST'])
@login_required
def hiya_remove_number():
    """Remove a phone number from Hiya Connect branded calling."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    if not hiya_connect.is_connect_configured():
        return jsonify({"error": "Hiya Connect not configured"}), 400

    data = request.json or {}
    phone = (data.get('phone') or '').strip()
    if not phone:
        return jsonify({"error": "phone (E.164) required"}), 400

    try:
        removed = hiya_connect.delete_number(phone)

        hiya_data = vc.get('hiya_connect', {})
        phones = set(hiya_data.get('registered_phones', []))
        phones.discard(phone)
        hiya_data['registered_phones'] = list(phones)
        hiya_data['registered_count'] = len(phones)
        vc['hiya_connect'] = hiya_data
        _save_voice_config(current_user.email, vc)

        return jsonify({"status": "ok", "removed": removed, "phone": phone})
    except Exception as e:
        logger.error(f"[Hiya] remove-number failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@numbers_bp.route('/voice/hiya/reputation', methods=['GET'])
@login_required
def hiya_reputation():
    """
    Query Hiya Protect for spam labeling status of this subscriber's numbers.
    Requires HIYA_PROTECT_APP_ID and HIYA_PROTECT_APP_SECRET (service agreement).
    Returns spamLabelingStatus and report card grades per number.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    if not hiya_connect.is_protect_configured():
        return jsonify({
            "error": "Hiya Protect API not configured. Requires signed service agreement with Hiya (api@hiya.com).",
            "configured": False,
        }), 400

    try:
        client = twilio_provisioning.get_sub_account_client(sub_sid)
        numbers = client.incoming_phone_numbers.list()
        phones = [n.phone_number for n in numbers if n.phone_number]

        if not phones:
            return jsonify({"numbers": [], "message": "No phone numbers on account"})

        reputation = hiya_connect.check_reputation(phones)
        return jsonify({"numbers": reputation, "total_queried": len(phones)})

    except Exception as e:
        logger.error(f"[Hiya] reputation check failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500
