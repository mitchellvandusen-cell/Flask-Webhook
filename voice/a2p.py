import json
import os
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user

import twilio_provisioning
from db import get_db_connection, return_db_connection, log_webhook_event, save_persistent_alert
from voice.helpers import _get_current_subscriber_voice, _save_voice_config

logger = logging.getLogger("voice_bridge.a2p")

a2p_bp = Blueprint('voice_a2p', __name__)

TWILIO_MASTER_SID = os.getenv("TWILIO_ACCOUNT_SID", "")


def _log_a2p_event(sub_sid, event_type, status, summary, details=None):
    """Log an A2P operation to webhook_logs with best-effort location_id."""
    try:
        conn = None
        location_id = ""
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT location_id FROM subscribers WHERE voice_config->>'twilio_sub_account_sid' = %s LIMIT 1",
                    (sub_sid,),
                )
                row = cur.fetchone()
                location_id = row[0] if row else ""
        except Exception:
            pass
        finally:
            if conn:
                return_db_connection(conn)
        log_webhook_event(
            location_id=location_id,
            event_type=event_type,
            status=status,
            summary=summary,
            details=details or {},
        )
    except Exception as e:
        logger.warning(f"Failed to log {event_type}: {e}")


# ──────────────────────────────────────────────────────────────
# A2P 10DLC — BRAND & CAMPAIGN REGISTRATION / IMPORT
# ──────────────────────────────────────────────────────────────

@a2p_bp.route('/voice/a2p/status', methods=['GET'])
@login_required
def a2p_status():
    """Return A2P 10DLC registration status — sub-account isolated."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    a2p = (vc or {}).get('a2p', {})
    is_sub_user = bool((subscriber or {}).get('parent_agency_email'))
    sub_auth_token = (vc or {}).get('twilio_auth_token', '')

    # ── Sub-account isolation: A2P scoping ───────────────────────────────────
    #
    # A2P Brand Registrations ARE per-Twilio-account, but sub-account clients
    # may still see parent-account brands through the Twilio hierarchy.
    # We use the same isolation strategy as Trust Hub:
    #   • Sub-accounts: only show data tagged with _sub_sid == sub_sid
    #                   (set during our registration flow)
    #   • Master account: also validate via Twilio to catch external registrations
    # ─────────────────────────────────────────────────────────────────────────
    _SID_KEYS = ('brand_sid', 'brand_status', 'campaign_sid', 'campaign_status',
                 'messaging_service_sid', 'use_case', 'registered', '_sub_sid')

    # Step 1: Clear any stale/untagged SID data that doesn't belong to this sub
    a2p_sub = a2p.get('_sub_sid', '')
    if a2p_sub and a2p_sub != sub_sid:
        logger.info(f"[a2p-status] Clearing A2P tagged for {a2p_sub}, current sub={sub_sid}")
        for k in _SID_KEYS:
            a2p.pop(k, None)
        _save_a2p_to_voice_config(subscriber, vc, a2p)
    elif not a2p_sub and any(a2p.get(k) for k in ('brand_sid', 'campaign_sid', 'messaging_service_sid')):
        logger.info(f"[a2p-status] Clearing untagged A2P for sub={sub_sid} (stale master data)")
        for k in _SID_KEYS:
            a2p.pop(k, None)
        _save_a2p_to_voice_config(subscriber, vc, a2p)

    is_master = (sub_sid == TWILIO_MASTER_SID)

    if is_master:
        # Master account: auto-discover from Twilio to catch externally-registered brands
        if not a2p.get('brand_sid'):
            try:
                discovered = twilio_provisioning.discover_full_a2p_status(sub_sid)
                if discovered.get('best_brand'):
                    brand = discovered['best_brand']
                    a2p['brand_sid'] = brand['brand_sid']
                    a2p['brand_status'] = brand['status']
                    a2p['_sub_sid'] = sub_sid
                    if discovered.get('best_campaign'):
                        campaign = discovered['best_campaign']
                        a2p['campaign_sid'] = campaign['campaign_sid']
                        a2p['campaign_status'] = campaign['campaign_status']
                        a2p['messaging_service_sid'] = campaign.get('messaging_service_sid', '')
                        a2p['use_case'] = campaign.get('use_case', '')
                        a2p['registered'] = campaign.get('campaign_status', '').upper() in ('VERIFIED', 'APPROVED')
                    _save_a2p_to_voice_config(subscriber, vc, a2p)
            except Exception as e:
                logger.warning(f"[a2p-status] Master A2P discovery failed: {e}")
    # else: Sub-account — only show what's tagged to this sub_sid (already enforced above)

    # Derive registered from actual status fields — don't rely on stored boolean
    # which may not have been set during import or certain registration flows
    brand_ok = a2p.get('brand_status', '').upper() in ('APPROVED', 'VERIFIED')
    campaign_ok = a2p.get('campaign_status', '').upper() in ('VERIFIED', 'APPROVED')
    is_registered = (brand_ok and campaign_ok) or a2p.get('registered', False)

    # Fetch the actual phone number SIDs associated with the messaging service
    # so the frontend can show per-number A2P registration status accurately
    registered_number_sids = []
    ms_sid = a2p.get('messaging_service_sid', '')
    if is_registered and ms_sid:
        try:
            ms_numbers = twilio_provisioning.list_messaging_service_phone_numbers(
                ms_sid, sub_sid, sub_auth_token)
            registered_number_sids = [n['sid'] for n in ms_numbers]
        except Exception as e:
            err_str = str(e)
            if not is_master and ('404' in err_str or '20404' in err_str):
                # MS SID doesn't exist on this sub-account — was registered on the master
                # account before sub-account isolation was enforced.  Clear the stale data
                # so the user can re-register using their own sub-account credentials.
                logger.warning(
                    f"[a2p-status] MS {ms_sid} not found on sub {sub_sid}; "
                    "clearing cross-account A2P data"
                )
                fee_paid = a2p.get('a2p_fee_paid', False)
                for k in _SID_KEYS:
                    a2p.pop(k, None)
                _save_a2p_to_voice_config(subscriber, vc, a2p)
                return jsonify({
                    "registered": False, "brand_sid": "", "brand_status": "",
                    "campaign_sid": "", "campaign_status": "",
                    "messaging_service_sid": "", "use_case": "", "registered_at": "",
                    "is_sub_user": is_sub_user, "a2p_fee_paid": fee_paid,
                    "registered_number_sids": [],
                })
            logger.warning(f"Failed to fetch MS phone numbers (non-fatal): {e}")

    return jsonify({
        "registered": is_registered,
        "brand_sid": a2p.get('brand_sid', ''),
        "brand_status": a2p.get('brand_status', ''),
        "campaign_sid": a2p.get('campaign_sid', ''),
        "campaign_status": a2p.get('campaign_status', ''),
        "messaging_service_sid": ms_sid,
        "use_case": a2p.get('use_case', ''),
        "registered_at": a2p.get('registered_at', ''),
        "is_sub_user": is_sub_user,
        "a2p_fee_paid": a2p.get('a2p_fee_paid', False),
        "registered_number_sids": registered_number_sids,
    })


@a2p_bp.route('/voice/a2p/add-number', methods=['POST'])
@login_required
def a2p_add_number():
    """Add a phone number to the existing A2P messaging service."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    a2p = (vc or {}).get('a2p', {})
    ms_sid = a2p.get('messaging_service_sid', '')
    if not ms_sid:
        return jsonify({"error": "No messaging service found. Complete A2P registration first."}), 400

    data = request.get_json(silent=True) or {}
    phone_number_sid = data.get('phone_number_sid', '').strip()
    if not phone_number_sid or not phone_number_sid.startswith('PN'):
        return jsonify({"error": "Invalid phone number SID"}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    try:
        twilio_provisioning.add_phone_to_messaging_service(
            sub_sid, ms_sid, phone_number_sid, sub_auth_token
        )
        return jsonify({"ok": True, "message": "Number added to A2P messaging service"})
    except Exception as e:
        logger.error(f"Failed to add number {phone_number_sid} to MS {ms_sid}: {e}")
        return jsonify({"error": f"Failed to add number: {e}"}), 500


@a2p_bp.route('/voice/a2p/sync', methods=['POST'])
@login_required
def a2p_sync():
    """
    Force-sync A2P status from Twilio. Discovers existing brands,
    messaging services, and campaigns on both master and sub-account.
    Persists results to voice_config['a2p'].
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    try:
        discovered = twilio_provisioning.discover_full_a2p_status(sub_sid, sub_auth_token)

        a2p = (vc or {}).get('a2p', {})
        synced = False

        if discovered.get('best_brand'):
            brand = discovered['best_brand']
            a2p['brand_sid'] = brand['brand_sid']
            a2p['brand_status'] = brand['status']
            a2p['_sub_sid'] = sub_sid
            synced = True

        if discovered.get('best_campaign'):
            campaign = discovered['best_campaign']
            a2p['campaign_sid'] = campaign['campaign_sid']
            a2p['campaign_status'] = campaign['campaign_status']
            a2p['messaging_service_sid'] = campaign.get('messaging_service_sid', '')
            a2p['use_case'] = campaign.get('use_case', '')
            a2p['registered'] = campaign.get('campaign_status', '').upper() in ('VERIFIED', 'APPROVED')
            synced = True

        if synced:
            _save_a2p_to_voice_config(subscriber, vc, a2p)
            logger.info(f"A2P sync complete: brand={a2p.get('brand_sid')}, campaign={a2p.get('campaign_sid')}")

        return jsonify({
            "synced": synced,
            "brands_found": len(discovered.get('brands', [])),
            "campaigns_found": len(discovered.get('campaigns', [])),
            "messaging_services_found": len(discovered.get('messaging_services', [])),
            "brand_sid": a2p.get('brand_sid', ''),
            "brand_status": a2p.get('brand_status', ''),
            "campaign_sid": a2p.get('campaign_sid', ''),
            "campaign_status": a2p.get('campaign_status', ''),
        })

    except Exception as e:
        logger.error(f"A2P sync failed: {e}")
        return jsonify({"error": "Internal server error"}), 500


def _save_a2p_to_voice_config(subscriber, vc, a2p):
    """Persist a2p dict to voice_config JSONB."""
    import json as _json
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return
        cur = conn.cursor()
        vc = vc or {}
        vc['a2p'] = a2p
        email = (subscriber or {}).get('email', '')
        is_agency = getattr(current_user, 'role', '') == 'agency_owner'
        if is_agency:
            cur.execute(
                "UPDATE agency_billing SET voice_config = %s::jsonb, updated_at = NOW() WHERE agency_email = %s",
                (_json.dumps(vc), email)
            )
        else:
            cur.execute(
                "UPDATE subscribers SET voice_config = %s::jsonb, updated_at = NOW() WHERE email = %s",
                (_json.dumps(vc), email)
            )
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Failed to save A2P to voice_config: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            return_db_connection(conn)


@a2p_bp.route('/voice/a2p/register-brand', methods=['POST'])
@login_required
def a2p_register_brand():
    """
    Register a new A2P 10DLC Brand via Twilio.
    Requires business details (reuses trust_hub data if available).
    Sub-account users must have paid the A2P fee first.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    is_sub_user = bool((subscriber or {}).get('parent_agency_email'))
    a2p = (vc or {}).get('a2p', {})

    # Sub-users must pay before registering
    if is_sub_user and not a2p.get('a2p_fee_paid', False):
        return jsonify({
            "error": "A2P registration fee required. Please complete payment first.",
            "payment_required": True,
        }), 402

    # ── Gate: Require approved Secondary Customer Profile ──
    # Per Twilio ISV docs, A2P brand registration requires an approved
    # Secondary Profile. Without it, the brand Trust Product will fail evaluation.
    trust_hub = (vc or {}).get('trust_hub', {})
    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    profile_sid = trust_hub.get('profile_sid', '') or a2p.get('profile_sid', '')
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

    data = request.get_json() or {}

    # Validate required fields
    business_name = data.get('business_name', '').strip()
    ein = data.get('ein', '').strip()
    contact_email = data.get('contact_email', '').strip()
    contact_phone = data.get('contact_phone', '').strip()
    brand_type = data.get('brand_type', 'LOW_VOLUME').upper().strip()
    if not business_name:
        return jsonify({"error": "Business name is required"}), 400
    if brand_type != 'SOLE_PROPRIETOR' and not ein:
        return jsonify({"error": "EIN is required for non-Sole Proprietor brands"}), 400
    if not contact_email:
        return jsonify({"error": "Contact email is required"}), 400

    # Map frontend brand_type to Twilio business_type param
    biz_type_map = {
        'SOLE_PROPRIETOR': 'sole_proprietor',
        'LOW_VOLUME': 'private_profit',
        'STANDARD': 'private_profit',
    }
    business_type = biz_type_map.get(brand_type, 'private_profit')

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    try:
        result = twilio_provisioning.create_a2p_brand(
            sub_account_sid=sub_sid,
            business_name=business_name,
            ein=ein,
            street=data.get('street', ''),
            city=data.get('city', ''),
            state=data.get('state', ''),
            zip_code=data.get('zip', ''),
            contact_email=contact_email,
            contact_phone=contact_phone,
            business_type=business_type,
            website=data.get('website', ''),
            vertical=data.get('vertical', 'INSURANCE'),
            sub_account_auth_token=sub_auth_token,
        )

        # Persist to voice_config
        a2p.update({
            "brand_sid": result["brand_sid"],
            "brand_status": result["status"],
            "profile_sid": result.get("profile_sid", ""),
            "trust_product_sid": result.get("trust_product_sid", ""),
            "business_name": business_name,
            "brand_type": brand_type,
            "registered_at": datetime.utcnow().isoformat(),
            "_sub_sid": sub_sid,
        })
        vc['a2p'] = a2p
        _save_voice_config(current_user.email, vc)

        _log_a2p_event(sub_sid, "a2p_brand_registered", "success",
                       f"SMS brand registered: {business_name}",
                       {"brand_sid": result["brand_sid"], "status": result["status"],
                        "business_name": business_name, "brand_type": brand_type})

        return jsonify({
            "brand_sid": result["brand_sid"],
            "status": result["status"],
            "message": "Brand submitted for vetting. This typically takes 1-7 business days.",
        })
    except Exception as e:
        logger.error(f"A2P brand registration error: {e}", exc_info=True)
        _log_a2p_event(sub_sid, "a2p_brand_registered", "error",
                       f"SMS brand registration failed: {business_name}",
                       {"business_name": business_name, "error": str(e)})
        save_persistent_alert(
            email=current_user.email, location_id="",
            alert_type="a2p_registration_failed", severity="error",
            title="SMS Brand Registration Failed",
            message=f"Failed to register brand '{business_name}'. Please try again or contact support.",
        )
        return jsonify({"error": f"Brand registration failed: {str(e)}"}), 500


@a2p_bp.route('/voice/a2p/brand-status', methods=['GET'])
@login_required
def a2p_brand_status():
    """Poll the current vetting status of the A2P Brand."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    a2p = (vc or {}).get('a2p', {})
    brand_sid = a2p.get('brand_sid', '')
    if not brand_sid:
        return jsonify({"error": "No brand registered"}), 404

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    try:
        result = twilio_provisioning.get_a2p_brand_status(brand_sid, sub_sid,
                                                           sub_auth_token)

        # Update stored status if changed
        if result["status"] != a2p.get("brand_status"):
            a2p["brand_status"] = result["status"]
            vc['a2p'] = a2p
            _save_voice_config(current_user.email, vc)

        return jsonify(result)
    except Exception as e:
        logger.error(f"A2P brand status error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@a2p_bp.route('/voice/a2p/create-campaign', methods=['POST'])
@login_required
def a2p_create_campaign():
    """
    Create an A2P 10DLC Campaign and Messaging Service.
    Brand must be approved first.
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    a2p = (vc or {}).get('a2p', {})
    brand_sid = a2p.get('brand_sid', '')

    if not brand_sid:
        return jsonify({"error": "Register a brand first before creating a campaign"}), 400

    data = request.get_json() or {}
    description = data.get('description', 'Insurance agent SMS communication').strip()
    use_case = data.get('use_case', 'LOW_VOLUME').strip()
    sample_messages = data.get('sample_messages', [])
    message_flow = data.get('message_flow', '').strip()
    phone_number_sids = data.get('phone_number_sids', [])

    if use_case not in twilio_provisioning.A2P_USE_CASES:
        return jsonify({"error": f"Invalid use case. Must be one of: {', '.join(twilio_provisioning.A2P_USE_CASES)}"}), 400

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    try:
        # Step 1: Create Messaging Service (or reuse existing)
        ms_sid = a2p.get('messaging_service_sid', '')
        if not ms_sid:
            biz_name = a2p.get('business_name', 'Insurance Bot')
            ms_result = twilio_provisioning.create_messaging_service(
                sub_sid, f"A2P - {biz_name}", sub_auth_token
            )
            ms_sid = ms_result["messaging_service_sid"]
            a2p["messaging_service_sid"] = ms_sid

        # Step 2: Associate phone numbers with the Messaging Service
        if phone_number_sids:
            for pn_sid in phone_number_sids:
                try:
                    twilio_provisioning.add_phone_to_messaging_service(
                        sub_sid, ms_sid, pn_sid, sub_auth_token
                    )
                except Exception as e:
                    logger.warning(f"Failed to add {pn_sid} to MS: {e}")

        # Step 3: Create the Campaign
        campaign_result = twilio_provisioning.create_a2p_campaign(
            messaging_service_sid=ms_sid,
            brand_registration_sid=brand_sid,
            description=description,
            use_case=use_case,
            sample_messages=sample_messages if sample_messages else None,
            message_flow=message_flow or None,
            has_embedded_links=data.get('has_embedded_links', False),
            has_embedded_phone=data.get('has_embedded_phone', False),
            sub_account_sid=sub_sid,
            sub_account_auth_token=sub_auth_token,
        )

        # Persist
        a2p.update({
            "campaign_sid": campaign_result["campaign_sid"],
            "campaign_status": campaign_result["campaign_status"],
            "use_case": use_case,
            "registered": True,
        })
        vc['a2p'] = a2p
        _save_voice_config(current_user.email, vc)

        _log_a2p_event(sub_sid, "a2p_campaign_created", "success",
                       f"SMS campaign submitted for approval",
                       {"campaign_sid": campaign_result["campaign_sid"],
                        "campaign_status": campaign_result["campaign_status"],
                        "messaging_service_sid": ms_sid, "use_case": use_case})

        return jsonify({
            "campaign_sid": campaign_result["campaign_sid"],
            "campaign_status": campaign_result["campaign_status"],
            "messaging_service_sid": ms_sid,
            "message": "Campaign submitted for approval. Typically approved within 24-48 hours.",
        })
    except Exception as e:
        logger.error(f"A2P campaign creation error: {e}", exc_info=True)
        _log_a2p_event(sub_sid, "a2p_campaign_created", "error",
                       f"SMS campaign creation failed",
                       {"brand_sid": brand_sid, "use_case": use_case, "error": str(e)})
        save_persistent_alert(
            email=current_user.email, location_id="",
            alert_type="a2p_campaign_failed", severity="error",
            title="SMS Campaign Creation Failed",
            message="Failed to create SMS campaign. Please try again or contact support.",
        )
        return jsonify({"error": f"Campaign creation failed: {str(e)}"}), 500


@a2p_bp.route('/voice/a2p/campaign-status', methods=['GET'])
@login_required
def a2p_campaign_status():
    """Poll the approval status of the A2P Campaign."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    a2p = (vc or {}).get('a2p', {})
    ms_sid = a2p.get('messaging_service_sid', '')
    campaign_sid = a2p.get('campaign_sid', '')
    if not ms_sid or not campaign_sid:
        return jsonify({"error": "No campaign registered"}), 404

    sub_auth_token = (vc or {}).get('twilio_auth_token', '')
    try:
        result = twilio_provisioning.get_a2p_campaign_status(
            ms_sid, campaign_sid, sub_sid, sub_auth_token)

        if result["campaign_status"] != a2p.get("campaign_status"):
            a2p["campaign_status"] = result["campaign_status"]
            vc['a2p'] = a2p
            _save_voice_config(current_user.email, vc)

        return jsonify(result)
    except Exception as e:
        logger.error(f"A2P campaign status error: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@a2p_bp.route('/voice/a2p/mark-fee-paid', methods=['POST'])
@login_required
def a2p_mark_fee_paid():
    """
    Called after successful Stripe payment redirect for A2P registration fee.
    Verifies Stripe checkout session before marking paid (primary path is
    the Stripe webhook; this is a fallback for webhook race conditions).
    """
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not sub_sid:
        return jsonify({"error": "Voice service not provisioned"}), 400

    a2p = (vc or {}).get('a2p', {})

    # If already marked paid (e.g., by Stripe webhook), just return ok
    if a2p.get('a2p_fee_paid'):
        return jsonify({"ok": True, "message": "A2P fee already marked as paid."})

    # Verify via Stripe: check if there's a completed a2p checkout session for this user
    try:
        import stripe
        sessions = stripe.checkout.Session.list(
            customer_email=current_user.email,
            limit=5,
        )
        verified = False
        for sess in sessions.data:
            if (sess.payment_status == 'paid' and
                    sess.metadata.get('purchase_type') == 'a2p_registration' and
                    sess.metadata.get('user_email') == current_user.email):
                verified = True
                break

        if not verified:
            return jsonify({"error": "No verified A2P payment found. Please complete payment first."}), 403
    except Exception as e:
        logger.warning(f"A2P fee verification via Stripe failed: {e}")
        # If Stripe check fails, don't allow bypass — the webhook path is the primary handler
        return jsonify({"error": "Payment verification temporarily unavailable. The Stripe webhook will process your payment shortly."}), 503

    a2p['a2p_fee_paid'] = True
    a2p['fee_paid_at'] = datetime.utcnow().isoformat()
    vc['a2p'] = a2p
    _save_voice_config(current_user.email, vc)

    return jsonify({"ok": True, "message": "A2P fee marked as paid."})
