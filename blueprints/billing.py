# blueprints/billing.py — Stripe billing, subscription checkout, AI Minutes, A2P fee payments
#
# Routes:
#   POST /stripe-webhook            — Stripe event handler (checkout, AI minutes, A2P fee)
#   GET  /checkout                  — Individual plan checkout
#   GET  /cancel                    — Post-cancel page
#   GET  /success                   — Post-checkout success (provisions DB + auto-login)
#   POST /create-portal-session     — Stripe billing portal redirect
#   GET  /ai-minutes/balance        — Current AI minute balance
#   GET  /ai-minutes/packages       — Available AI minute packages
#   POST /ai-minutes/checkout       — Create AI minute purchase session
#   GET  /ai-minutes/usage          — Purchase + usage history
#   POST /a2p/checkout              — Create A2P 10DLC registration fee session
#   GET  /a2p/fee-schedule          — Return A2P fee schedule for frontend display

import os
import json
import uuid
import logging
import requests
from datetime import datetime

import stripe
from flask import (Blueprint, redirect, request, render_template_string,
                   render_template, flash, make_response, url_for)
from flask import jsonify as flask_jsonify
from flask_login import login_required, login_user, current_user

from extensions import YOUR_DOMAIN, safe_jsonify, ADMIN_EMAILS
from psycopg2.extras import RealDictCursor
from db import (
    get_db_connection, return_db_connection, User,
    get_ai_minute_balance, get_ai_minute_purchases,
    get_ai_minute_usage, credit_ai_minutes, audit_ai_minutes,
)

logger = logging.getLogger(__name__)

billing_bp = Blueprint('billing', __name__)


# ── Constants ─────────────────────────────────────────────────────────────────

AI_MINUTE_PACKAGES = [
    {"minutes": 500,   "label": "Starter",      "env_key": "AI_MINUTES_PRICE_ID_500"},
    {"minutes": 2000,  "label": "Growth",        "env_key": "AI_MINUTES_PRICE_ID_2000"},
    {"minutes": 5000,  "label": "Professional",  "env_key": "AI_MINUTES_PRICE_ID_5000"},
    {"minutes": 10000, "label": "Enterprise",    "env_key": "AI_MINUTES_PRICE_ID_10000"},
]

# A2P 10DLC fee schedule (cents). Brand fee + $15 campaign vetting per brand type.
# Prices from Twilio / TCR as of Aug 2025.
A2P_FEE_SCHEDULE = {
    "SOLE_PROPRIETOR": {"brand_fee": 450,  "campaign_fee": 1500, "label": "Sole Proprietor"},
    "LOW_VOLUME":      {"brand_fee": 450,  "campaign_fee": 1500, "label": "Low Volume Standard"},
    "STANDARD":        {"brand_fee": 4600, "campaign_fee": 1500, "label": "Standard"},
}

# ── Shared inline error page template ─────────────────────────────────────────

_ERROR_PAGE = """
<div style="background:#050505;color:#fff;height:100vh;display:flex;align-items:center;
            justify-content:center;font-family:'Outfit',sans-serif;">
  <div style="padding:40px;border:1px solid #ff4444;border-radius:20px;
              text-align:center;max-width:500px;">
    <h2 style="color:#ff4444;">{{ title }}</h2>
    <p style="color:#aaa;">{{ msg }}</p>
    <p style="color:#666;font-size:0.85rem;margin-top:20px;">{{ detail }}</p>
    <a href="/" style="color:#00ff88;text-decoration:none;margin-top:20px;display:inline-block;">
      &larr; Back to Home
    </a>
  </div>
</div>
"""


def _error_page(title, msg, detail=""):
    return render_template_string(_ERROR_PAGE, title=title, msg=msg, detail=detail), 500


# ── Rewardful helpers ──────────────────────────────────────────────────────────

_REWARDFUL_API_BASE = "https://api.getrewardful.com/v1"


def _create_rewardful_affiliate(email, first_name="", last_name=""):
    """Create an affiliate account in Rewardful for a new subscriber.
    Returns the affiliate dict on success, or None on failure.
    Non-fatal — errors are logged but never surface to users.
    """
    api_secret = os.getenv("REWARDFUL_API_SECRET")
    if not api_secret:
        return None
    try:
        # Rewardful API requires form-encoded data (not JSON)
        payload = {"email": email, "first_name": first_name or "", "last_name": last_name or ""}
        resp = requests.post(
            f"{_REWARDFUL_API_BASE}/affiliates",
            auth=(api_secret, ""),
            data=payload,
            timeout=10,
        )
        if resp.status_code in (200, 201):
            affiliate = resp.json()
            link = affiliate.get("links", [{}])[0].get("url", "")
            logger.info(f"Rewardful: created affiliate for {email} — link={link}")
            return affiliate
        elif resp.status_code == 422:
            # Affiliate already exists — fetch them instead
            existing = _get_rewardful_affiliate_by_email(email, api_secret)
            if existing:
                return existing
            logger.warning(f"Rewardful: 422 creating affiliate for {email}: {resp.text}")
        else:
            logger.warning(f"Rewardful: error creating affiliate for {email}: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.warning(f"Rewardful: exception creating affiliate for {email}: {e}")
    return None


def _get_rewardful_affiliate_by_email(email, api_secret):
    """Look up an existing Rewardful affiliate by email."""
    try:
        resp = requests.get(
            f"{_REWARDFUL_API_BASE}/affiliates",
            auth=(api_secret, ""),
            params={"email": email},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            affiliates = data.get("data", []) if isinstance(data, dict) else data
            if affiliates:
                return affiliates[0]
    except Exception as e:
        logger.warning(f"Rewardful: exception fetching affiliate for {email}: {e}")
    return None


def _get_validated_coupon(coupon_id):
    """Validate a Stripe coupon ID before applying it to a checkout session.
    Returns the coupon ID if valid, None otherwise.
    """
    if not coupon_id:
        return None
    try:
        stripe.Coupon.retrieve(coupon_id)
        return coupon_id
    except stripe.error.InvalidRequestError:
        logger.warning(f"Rewardful coupon '{coupon_id}' not found in Stripe — skipping discount")
        return None
    except Exception as e:
        logger.warning(f"Stripe coupon validation error for '{coupon_id}': {e}")
        return None


# ── Stripe Webhook ────────────────────────────────────────────────────────────

@billing_bp.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

    if not endpoint_secret:
        logger.error("STRIPE_WEBHOOK_SECRET env var is not set — cannot verify webhook signature")
        return '', 500

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except stripe.error.SignatureVerificationError:
        logger.warning("Stripe webhook signature failed")
        return '', 400
    except Exception as e:
        logger.error(f"Stripe webhook error: {e}")
        return '', 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_id = session.customer
        _cd = getattr(session, 'customer_details', None)
        email = _cd.email.lower() if (_cd and _cd.email) else None

        # ── AI Minutes one-time purchase ──────────────────────────────────────
        metadata = session.metadata or {}
        if metadata.get("purchase_type") == "ai_minutes" and email:
            pkg_minutes = int(metadata.get("package_minutes", 0))
            pkg_label   = metadata.get("package_label", "")
            amount      = session.amount_total or 0
            credited = credit_ai_minutes(
                email=email,
                minutes=pkg_minutes,
                stripe_session_id=session.id,
                stripe_payment_intent=session.payment_intent,
                package_label=pkg_label,
                amount_cents=amount,
            )
            if credited:
                result = audit_ai_minutes(email)
                if result.get("corrected"):
                    logger.warning(
                        f"Post-purchase audit corrected {email}: "
                        f"drift was {result['drift']:+d} min "
                        f"(balance {result['actual_balance']} → {result['expected_balance']})"
                    )
                logger.info(
                    f"AI Minutes: Credited {pkg_minutes} min to {email} — "
                    f"balance now {result.get('expected_balance', '?')} min"
                )
            return '', 200

        # ── Phone number purchase ────────────────────────────────────────────
        if metadata.get("purchase_type") == "phone_number" and email:
            phone_number = metadata.get("phone_number", "")
            price_cents = metadata.get("price_cents", "90")
            logger.info(f"Phone number paid by {email} — {phone_number} amount=${int(price_cents)/100:.2f}")
            # Provisioning happens via /voice/numbers/complete-purchase (user-side redirect)
            return '', 200

        # ── A2P 10DLC registration fee ────────────────────────────────────────
        if metadata.get("purchase_type") == "a2p_registration" and email:
            brand_type = metadata.get("brand_type", "LOW_VOLUME")
            paid_cents = metadata.get("total_cents", "0")
            logger.info(f"A2P fee paid by {email} — brand_type={brand_type} amount=${int(paid_cents)/100:.2f}")
            try:
                conn = get_db_connection()
                if conn:
                    cur = None
                    try:
                        cur = conn.cursor()
                        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (email,))
                        row = cur.fetchone()
                        if row:
                            vc  = row['voice_config'] or {}
                            a2p = vc.get('a2p', {})
                            a2p['a2p_fee_paid']        = True
                            a2p['fee_paid_at']         = datetime.utcnow().isoformat()
                            a2p['stripe_session_id']   = session.id
                            a2p['paid_brand_type']     = brand_type
                            a2p['paid_amount_cents']   = int(paid_cents)
                            vc['a2p'] = a2p
                            cur.execute(
                                "UPDATE subscribers SET voice_config = %s WHERE email = %s",
                                (json.dumps(vc), email)
                            )
                            conn.commit()
                    except Exception as e:
                        logger.error(f"Failed to mark A2P fee paid: {e}")
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    finally:
                        if cur:
                            try:
                                cur.close()
                            except Exception:
                                pass
                        return_db_connection(conn)
            except Exception as e:
                logger.error(f"A2P fee webhook error: {e}")
            return '', 200

        # ── Seat user subscription ─────────────────────────────────────────
        if metadata.get("purchase_type") == "seat_user" and email:
            loc_id = metadata.get("location_id", "")
            sub_id = session.subscription  # Stripe subscription ID
            logger.info(f"Seat subscription paid by {email} — location={loc_id} subscription={sub_id}")
            try:
                conn = get_db_connection()
                if conn:
                    cur = None
                    try:
                        cur = conn.cursor()
                        # Store subscription ID for lifecycle tracking
                        # Associate with next unpaid seat user at this location
                        cur.execute("""
                            UPDATE location_users
                            SET stripe_seat_subscription_id = %s, updated_at = NOW()
                            WHERE location_id = %s
                              AND stripe_seat_subscription_id IS NULL
                              AND is_active = true
                            ORDER BY created_at DESC
                            LIMIT 1
                        """, (sub_id, loc_id))
                        conn.commit()
                    except Exception as e:
                        logger.error(f"Failed to link seat subscription: {e}")
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    finally:
                        if cur:
                            try:
                                cur.close()
                            except Exception:
                                pass
                        return_db_connection(conn)
            except Exception as e:
                logger.error(f"Seat subscription webhook error: {e}")
            return '', 200

        # ── Phone number cart purchase ────────────────────────────────────────
        if metadata.get("purchase_type") == "phone_number_cart" and email:
            logger.info(f"Phone number cart paid by {email}")
            return '', 200

        # ── Subscription checkout ─────────────────────────────────────────────
        target_role = metadata.get("target_role", "individual")
        target_tier = metadata.get("target_tier", "individual")

        if email and customer_id:
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    temp_id = f"temp_{uuid.uuid4().hex[:8]}"

                    cur.execute("""
                        INSERT INTO subscribers (
                            location_id, email, stripe_customer_id, role, subscription_tier,
                            stripe_status, crm_user_id, bot_first_name, timezone
                        )
                        VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s)
                        ON CONFLICT (email) DO UPDATE SET
                            stripe_customer_id = EXCLUDED.stripe_customer_id,
                            role = EXCLUDED.role,
                            subscription_tier = EXCLUDED.subscription_tier,
                            stripe_status = 'active';
                    """, (temp_id, email, customer_id, target_role, target_tier,
                          '', 'Grok', 'America/Chicago'))

                    if target_role == "agency_owner":
                        max_seats = 10 if target_tier == "starter" else 9999
                        cur.execute("""
                            INSERT INTO agency_billing (agency_email, subscription_tier, max_seats, active_seats)
                            VALUES (%s, %s, %s, 0)
                            ON CONFLICT (agency_email) DO UPDATE SET
                                subscription_tier = EXCLUDED.subscription_tier,
                                max_seats = EXCLUDED.max_seats;
                        """, (email, target_tier, max_seats))

                    conn.commit()
                    logger.info(f"Provisioned {target_tier.upper()} {target_role} account for: {email}")

                    # Solo Predictive includes 2000 AI minutes/month — credit on initial subscription
                    if target_tier == "solo_predictive":
                        try:
                            credited = credit_ai_minutes(
                                email=email,
                                minutes=2000,
                                stripe_session_id=session.id,
                                stripe_payment_intent=session.payment_intent or "",
                                package_label="Solo Predictive — 2,000 included minutes",
                                amount_cents=0,
                            )
                            if credited:
                                logger.info(f"Solo Predictive: Credited 2000 AI minutes to {email}")
                        except Exception as sp_err:
                            logger.warning(f"Solo Predictive AI minutes credit failed for {email}: {sp_err}")

                    # Auto-create Rewardful affiliate account for new subscriber (non-fatal)
                    try:
                        affiliate = _create_rewardful_affiliate(email)
                        if affiliate:
                            referral_link = affiliate.get("links", [{}])[0].get("url", "")
                            affiliate_id = affiliate.get("id", "")
                            if referral_link or affiliate_id:
                                cur.execute("""
                                    UPDATE subscribers
                                    SET config = COALESCE(config, '{}'::jsonb) ||
                                        jsonb_build_object(
                                            'rewardful_affiliate_id', %s::text,
                                            'rewardful_referral_link', %s::text
                                        )
                                    WHERE email = %s
                                """, (affiliate_id, referral_link, email))
                                conn.commit()
                    except Exception as rw_err:
                        logger.warning(f"Rewardful auto-affiliate skipped for {email}: {rw_err}")

                    # Auto-provision Twilio sub-account (voice activation)
                    # Removes the need for users to click "Activate Voice" separately
                    if target_tier != "sms_bot":
                        try:
                            from twilio_provisioning import provision_subscriber as _twilio_provision
                            webhook_base_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
                            # Need location_id — fetch from the subscriber we just created/updated
                            cur.execute("SELECT location_id, voice_config FROM subscribers WHERE email = %s", (email,))
                            _sub_row = cur.fetchone()
                            _loc_id = _sub_row["location_id"] if _sub_row else ""
                            _vc = (_sub_row["voice_config"] if _sub_row else None) or {}
                            if _loc_id and not _vc.get("twilio_sub_account_sid"):
                                _prov_result = _twilio_provision(
                                    subscriber_email=email,
                                    location_id=_loc_id,
                                    webhook_base_url=webhook_base_url,
                                )
                                if _prov_result and not _prov_result.get("error"):
                                    _vc.update(_prov_result)
                                    _vc["enabled"] = True
                                    cur.execute(
                                        "UPDATE subscribers SET voice_config = %s WHERE email = %s",
                                        (json.dumps(_vc), email)
                                    )
                                    conn.commit()
                                    logger.info(f"Auto-provisioned voice for {email} (sub_sid={_prov_result.get('twilio_sub_account_sid', '?')})")
                                else:
                                    logger.warning(f"Voice auto-provision returned error for {email}: {_prov_result}")
                            elif _vc.get("twilio_sub_account_sid"):
                                logger.info(f"Voice already provisioned for {email} — skipping auto-provision")
                        except Exception as voice_err:
                            logger.warning(f"Voice auto-provision failed for {email} (non-fatal): {voice_err}")

                    # Legacy Google Sheets redundant backup (non-critical)
                    try:
                        import extensions as _ext
                        if _ext.gc and _ext.sheet_url:
                            sh = _ext.gc.open_by_url(_ext.sheet_url)
                            user_sheet = sh.worksheet("Users")
                            user_sheet.append_row([email, "", "", "", "", target_role,
                                                   customer_id, datetime.now().isoformat()])
                    except Exception as sheet_err:
                        logger.warning(f"Sheet redundant sync skipped: {sheet_err}")

                except Exception as e:
                    logger.error(f"Post-checkout database sync failed: {e}")
                    conn.rollback()
                finally:
                    cur.close()
                    return_db_connection(conn)

    # ── Subscription updated / deleted — sync ALL status changes to DB ──
    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.updated"):
        subscription = event["data"]["object"]
        sub_id = subscription.id
        status = subscription.status  # 'active', 'past_due', 'canceled', 'unpaid', 'trialing', etc.
        customer_id = subscription.get("customer", "")

        # Derive tier from subscription price when status is active/trialing
        new_tier = None
        if status in ('active', 'trialing'):
            items = subscription.get("items", {}).get("data", [])
            price_id = items[0]["price"]["id"] if items else ""
            price_to_tier = {v: k for k, v in {
                "sms_bot": os.getenv("STRIPE_SMS_BOT_PRICE_ID"),
                "individual": os.getenv("STRIPE_PRICE_ID"),
                "pro_dialer": os.getenv("STRIPE_PRO_DIALER_PRICE_ID"),
                "solo_predictive": os.getenv("STRIPE_PREDICTIVE_DIALER_PRICE_ID"),
            }.items() if v}
            new_tier = price_to_tier.get(price_id, "individual")

        try:
            conn = get_db_connection()
            if conn:
                cur = None
                try:
                    cur = conn.cursor(cursor_factory=RealDictCursor)

                    # Check if this is a seat subscription
                    cur.execute("""
                        SELECT id, email, location_id FROM location_users
                        WHERE stripe_seat_subscription_id = %s
                    """, (sub_id,))
                    seat = cur.fetchone()
                    if seat:
                        if status in ('canceled', 'unpaid', 'incomplete_expired'):
                            cur.execute("""
                                UPDATE location_users
                                SET is_active = false, session_revoked_at = NOW(), updated_at = NOW()
                                WHERE id = %s
                            """, (seat['id'],))
                            conn.commit()
                            logger.info(f"Seat user {seat['email']} deactivated — subscription {sub_id} {status}")
                        elif status == 'active':
                            cur.execute("""
                                UPDATE location_users
                                SET is_active = true, updated_at = NOW()
                                WHERE id = %s
                            """, (seat['id'],))
                            conn.commit()
                            logger.info(f"Seat user {seat['email']} reactivated — subscription {sub_id}")
                    elif customer_id:
                        # Main subscriber subscription
                        cur.execute("""
                            SELECT email, subscription_tier, stripe_status FROM subscribers
                            WHERE stripe_customer_id = %s
                        """, (customer_id,))
                        subscriber = cur.fetchone()
                        if subscriber:
                            old_tier = subscriber['subscription_tier']
                            old_status = subscriber.get('stripe_status')

                            if status in ('canceled', 'unpaid', 'incomplete_expired'):
                                # Deactivate: clear tier, set status
                                cur.execute("""
                                    UPDATE subscribers
                                    SET stripe_status = %s,
                                        subscription_tier = NULL,
                                        updated_at = NOW()
                                    WHERE stripe_customer_id = %s
                                """, (status, customer_id))
                                conn.commit()
                                logger.info(
                                    f"Subscription {status} for {subscriber['email']} "
                                    f"(was tier={old_tier}) — paywall re-enabled"
                                )
                            elif status in ('active', 'trialing'):
                                # Active/trialing: update tier + status
                                cur.execute("""
                                    UPDATE subscribers
                                    SET stripe_status = %s,
                                        subscription_tier = %s,
                                        updated_at = NOW()
                                    WHERE stripe_customer_id = %s
                                """, (status, new_tier, customer_id))
                                conn.commit()
                                if old_tier != new_tier:
                                    logger.info(
                                        f"Plan changed for {subscriber['email']}: "
                                        f"{old_tier} → {new_tier}, status={status}"
                                    )
                                elif old_status != status:
                                    logger.info(
                                        f"Status changed for {subscriber['email']}: "
                                        f"{old_status} → {status}, tier={new_tier}"
                                    )
                            elif status == 'past_due':
                                # Past due: keep tier (grace period) but flag status
                                cur.execute("""
                                    UPDATE subscribers
                                    SET stripe_status = %s, updated_at = NOW()
                                    WHERE stripe_customer_id = %s
                                """, (status, customer_id))
                                conn.commit()
                                logger.warning(
                                    f"Subscription past_due for {subscriber['email']} "
                                    f"(tier={old_tier} preserved during grace period)"
                                )
                            else:
                                # Any other status (paused, incomplete, etc.)
                                cur.execute("""
                                    UPDATE subscribers
                                    SET stripe_status = %s, updated_at = NOW()
                                    WHERE stripe_customer_id = %s
                                """, (status, customer_id))
                                conn.commit()
                                logger.info(
                                    f"Subscription status={status} for {subscriber['email']}"
                                )

                except Exception as e:
                    logger.error(f"Subscription lifecycle handler failed: {e}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                finally:
                    if cur:
                        try:
                            cur.close()
                        except Exception:
                            pass
                    return_db_connection(conn)
        except Exception as e:
            logger.error(f"Subscription lifecycle error: {e}")

    # ── Recurring invoice paid — credit included AI minutes for solo_predictive ──
    elif event["type"] == "invoice.paid":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer", "")
        # Only process subscription renewal invoices (not the initial checkout)
        if invoice.get("billing_reason") == "subscription_cycle" and customer_id:
            try:
                conn = get_db_connection()
                if conn:
                    cur = None
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT email, subscription_tier FROM subscribers WHERE stripe_customer_id = %s",
                            (customer_id,)
                        )
                        row = cur.fetchone()
                        if row and row['subscription_tier'] == 'solo_predictive':
                            credited = credit_ai_minutes(
                                email=row['email'],
                                minutes=2000,
                                stripe_session_id=invoice.get("id", ""),
                                stripe_payment_intent=invoice.get("payment_intent", ""),
                                package_label="Solo Predictive — 2,000 monthly included minutes",
                                amount_cents=0,
                            )
                            if credited:
                                logger.info(f"Solo Predictive renewal: Credited 2000 AI minutes to {row['email']}")
                    except Exception as e:
                        logger.error(f"Solo Predictive monthly AI minutes credit failed: {e}")
                    finally:
                        if cur:
                            cur.close()
                        return_db_connection(conn)
            except Exception as e:
                logger.error(f"Invoice.paid handler error: {e}")

    # ── Payment failed — notify subscriber before they churn ──────────────────
    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        customer_id = invoice.get("customer", "")
        attempt_count = invoice.get("attempt_count", 0)
        if customer_id:
            try:
                conn = get_db_connection()
                if conn:
                    cur = None
                    try:
                        cur = conn.cursor(cursor_factory=RealDictCursor)
                        cur.execute(
                            "SELECT email, bot_first_name FROM subscribers WHERE stripe_customer_id = %s",
                            (customer_id,)
                        )
                        row = cur.fetchone()
                        if row:
                            email = row['email']
                            name = row.get('bot_first_name') or 'there'
                            # Update stripe_status to past_due
                            cur.execute("""
                                UPDATE subscribers
                                SET stripe_status = 'past_due', updated_at = NOW()
                                WHERE stripe_customer_id = %s
                            """, (customer_id,))
                            conn.commit()
                            logger.warning(
                                f"Payment failed for {email} — attempt #{attempt_count}, "
                                f"invoice {invoice.get('id', '?')}, stripe_status → past_due"
                            )
                            try:
                                from send_email_api import send_email_via_api
                                send_email_via_api(
                                    to_email=email,
                                    subject="Action required — your InsuranceGrokBot payment failed",
                                    html_body=(
                                        f"<p>Hey {name},</p>"
                                        f"<p>We weren't able to process your payment "
                                        f"(attempt #{attempt_count}). Your subscription will be "
                                        f"paused if we can't collect payment soon.</p>"
                                        f"<p>Please update your card to keep your AI dialer, "
                                        f"Smart Filters, and all your settings running:</p>"
                                        f'<p><a href="{YOUR_DOMAIN}/dashboard?tab=billing"'
                                        f' style="background:#00ff88;color:#000;padding:12px 24px;'
                                        f'border-radius:8px;text-decoration:none;font-weight:bold;">'
                                        f'Update Payment Method</a></p>'
                                        f"<p>— The InsuranceGrokBot Team</p>"
                                    ),
                                )
                            except Exception as mail_err:
                                logger.warning(f"Payment failed email to {email} failed: {mail_err}")
                    except Exception as e:
                        logger.error(f"Payment failed handler error: {e}")
                    finally:
                        if cur:
                            cur.close()
                        return_db_connection(conn)
            except Exception as e:
                logger.error(f"Invoice.payment_failed handler error: {e}")

    # ── Trial ending soon — remind user to keep their subscription ─────────
    elif event["type"] == "customer.subscription.trial_will_end":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer", "")
        trial_end = subscription.get("trial_end")
        if customer_id:
            try:
                conn = get_db_connection()
                if conn:
                    cur = None
                    try:
                        cur = conn.cursor(cursor_factory=RealDictCursor)
                        cur.execute(
                            "SELECT email, bot_first_name FROM subscribers WHERE stripe_customer_id = %s",
                            (customer_id,)
                        )
                        row = cur.fetchone()
                        if row:
                            email = row['email']
                            name = row.get('bot_first_name') or 'there'
                            logger.info(f"Trial ending soon for {email} — trial_end={trial_end}")
                            try:
                                from send_email_api import send_email_via_api
                                send_email_via_api(
                                    to_email=email,
                                    subject="Your InsuranceGrokBot trial ends in 3 days",
                                    html_body=(
                                        f"<p>Hey {name},</p>"
                                        f"<p>Your free trial is wrapping up in 3 days. "
                                        f"After that, your subscription will start automatically "
                                        f"— no action needed if you want to keep going.</p>"
                                        f"<p>Everything you've set up (your AI agent, Smart Filters, "
                                        f"phone numbers, and conversation history) will keep working "
                                        f"seamlessly.</p>"
                                        f"<p>If you have any questions before your trial ends, "
                                        f"just reply to this email.</p>"
                                        f'<p><a href="{YOUR_DOMAIN}/dashboard"'
                                        f' style="background:#00ff88;color:#000;padding:12px 24px;'
                                        f'border-radius:8px;text-decoration:none;font-weight:bold;">'
                                        f'Go to Dashboard</a></p>'
                                        f"<p>— The InsuranceGrokBot Team</p>"
                                    ),
                                )
                            except Exception as mail_err:
                                logger.warning(f"Trial ending email to {email} failed: {mail_err}")
                    except Exception as e:
                        logger.error(f"Trial will end handler error: {e}")
                    finally:
                        if cur:
                            cur.close()
                        return_db_connection(conn)
            except Exception as e:
                logger.error(f"Trial_will_end handler error: {e}")

    # ── Subscription paused — update DB status ──────────────────────────────
    elif event["type"] == "customer.subscription.paused":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer", "")
        if customer_id:
            try:
                conn = get_db_connection()
                if conn:
                    cur = None
                    try:
                        cur = conn.cursor()
                        cur.execute("""
                            UPDATE subscribers
                            SET stripe_status = 'paused', updated_at = NOW()
                            WHERE stripe_customer_id = %s
                        """, (customer_id,))
                        conn.commit()
                        logger.info(f"Subscription paused — customer={customer_id}")
                    except Exception as e:
                        logger.error(f"Subscription paused handler error: {e}")
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    finally:
                        if cur:
                            cur.close()
                        return_db_connection(conn)
            except Exception as e:
                logger.error(f"Subscription paused error: {e}")

    # ── Subscription resumed — re-enable access ───────────────────────────
    elif event["type"] == "customer.subscription.resumed":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer", "")
        if customer_id and subscription.get("status") == "active":
            try:
                conn = get_db_connection()
                if conn:
                    cur = None
                    try:
                        cur = conn.cursor(cursor_factory=RealDictCursor)
                        # Look up the customer's email from Stripe, then re-enable
                        customer_obj = stripe.Customer.retrieve(customer_id)
                        cust_email = (customer_obj.email or "").lower()
                        if cust_email:
                            # Determine tier from the resumed subscription's price
                            items = subscription.get("items", {}).get("data", [])
                            price_id = items[0]["price"]["id"] if items else ""
                            price_to_tier = {v: k for k, v in {
                                "sms_bot": os.getenv("STRIPE_SMS_BOT_PRICE_ID"),
                                "individual": os.getenv("STRIPE_PRICE_ID"),
                                "pro_dialer": os.getenv("STRIPE_PRO_DIALER_PRICE_ID"),
                                "solo_predictive": os.getenv("STRIPE_PREDICTIVE_DIALER_PRICE_ID"),
                            }.items() if v}
                            tier = price_to_tier.get(price_id, "individual")

                            cur.execute("""
                                UPDATE subscribers
                                SET stripe_customer_id = %s,
                                    subscription_tier = %s,
                                    stripe_status = 'active',
                                    updated_at = NOW()
                                WHERE email = %s
                            """, (customer_id, tier, cust_email))
                            conn.commit()
                            logger.info(f"Subscription resumed for {cust_email} — tier={tier}")
                        conn.commit()
                        logger.info(f"Subscription resumed — customer={customer_id}")
                    except Exception as e:
                        logger.error(f"Subscription resume handler error: {e}")
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    finally:
                        if cur:
                            cur.close()
                        return_db_connection(conn)
            except Exception as e:
                logger.error(f"Subscription resumed error: {e}")

    # ── Charge disputed — alert admins immediately ─────────────────────────
    elif event["type"] == "charge.dispute.created":
        dispute = event["data"]["object"]
        amount = dispute.get("amount", 0)
        reason = dispute.get("reason", "unknown")
        customer_id = dispute.get("customer", "")
        logger.critical(
            f"DISPUTE CREATED — customer={customer_id} amount=${amount/100:.2f} "
            f"reason={reason} dispute={dispute.get('id')}"
        )
        try:
            from send_email_api import send_email_via_api
            for admin_email in ADMIN_EMAILS:
                send_email_via_api(
                    to_email=admin_email,
                    subject=f"DISPUTE ALERT — ${amount/100:.2f} — {reason}",
                    html_body=(
                        f"<p><strong>A charge dispute was filed.</strong></p>"
                        f"<p>Amount: <strong>${amount/100:.2f}</strong><br>"
                        f"Reason: <strong>{reason}</strong><br>"
                        f"Customer: {customer_id}<br>"
                        f"Dispute ID: {dispute.get('id', '?')}</p>"
                        f'<p><a href="https://dashboard.stripe.com/disputes/{dispute.get("id", "")}">'
                        f'View in Stripe Dashboard</a></p>'
                    ),
                )
        except Exception as mail_err:
            logger.warning(f"Dispute alert email failed: {mail_err}")

    # ── Charge refunded — log for records ──────────────────────────────────
    elif event["type"] == "charge.refunded":
        charge = event["data"]["object"]
        amount_refunded = charge.get("amount_refunded", 0)
        customer_id = charge.get("customer", "")
        logger.info(
            f"Charge refunded — customer={customer_id} "
            f"refunded=${amount_refunded/100:.2f} charge={charge.get('id')}"
        )

    # ── Customer updated — sync email changes ─────────────────────────────
    elif event["type"] == "customer.updated":
        customer = event["data"]["object"]
        customer_id = customer.get("id", "")
        new_email = (customer.get("email") or "").lower()
        previous = event["data"].get("previous_attributes", {})
        old_email = (previous.get("email") or "").lower()

        if old_email and new_email and old_email != new_email:
            logger.info(f"Customer email changed: {old_email} → {new_email} (customer={customer_id})")
            try:
                conn = get_db_connection()
                if conn:
                    cur = None
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            "UPDATE subscribers SET email = %s, updated_at = NOW() WHERE stripe_customer_id = %s AND email = %s",
                            (new_email, customer_id, old_email)
                        )
                        if cur.rowcount:
                            conn.commit()
                            logger.info(f"Subscriber email synced: {old_email} → {new_email}")
                        else:
                            conn.rollback()
                    except Exception as e:
                        logger.error(f"Customer email sync failed: {e}")
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    finally:
                        if cur:
                            cur.close()
                        return_db_connection(conn)
            except Exception as e:
                logger.error(f"Customer.updated handler error: {e}")

    # ── Subscription created — log for analytics ───────────────────────────
    elif event["type"] == "customer.subscription.created":
        subscription = event["data"]["object"]
        customer_id = subscription.get("customer", "")
        status = subscription.get("status", "")
        logger.info(
            f"Subscription created — customer={customer_id} status={status} "
            f"sub={subscription.get('id')}"
        )

    return '', 200


# ── Checkout pages ────────────────────────────────────────────────────────────

@billing_bp.route("/checkout")
def checkout():
    # Terms acceptance handled in Stripe Checkout
    try:
        price_id = os.getenv("STRIPE_PRICE_ID")
        if not price_id:
            logger.error("STRIPE_PRICE_ID environment variable is not set!")
            return _error_page(
                "Configuration Error",
                "The Individual plan price ID is not configured. Please contact support.",
                "Error Code: MISSING_PRICE_ID"
            )

        customer_email = current_user.email if current_user.is_authenticated else None
        logger.info(f"Creating Individual checkout with price_id: {price_id}")

        # Rewardful affiliate tracking
        referral = request.args.get("referral", "").strip()
        coupon_id = _get_validated_coupon(request.args.get("coupon", "").strip())

        checkout_params = dict(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=customer_email,
            metadata={
                "user_email": customer_email,
                "target_role": "individual",
                "target_tier": "individual",
                "source": "website"
            },
            subscription_data={
                "trial_period_days": 7,
                "metadata": {
                    "user_email": customer_email,
                    "target_role": "individual",
                    "target_tier": "individual"
                },
            },
            success_url=f"{YOUR_DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )

        if coupon_id:
            # Apply Rewardful double-sided coupon (15% off first month)
            checkout_params["discounts"] = [{"coupon": coupon_id}]
        else:
            checkout_params["allow_promotion_codes"] = True

        if referral:
            checkout_params["client_reference_id"] = referral

        session = stripe.checkout.Session.create(**checkout_params)
        return redirect(session.url, code=303)

    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe Invalid Request Error (Individual): {e}")
        return _error_page(
            "Stripe Configuration Error",
            "There's an issue with the payment configuration. Please contact support.",
            f"Error: {e}"
        )
    except Exception as e:
        logger.error(f"Stripe checkout error: {e}")
        return _error_page("Checkout Error",
                           "Unable to create checkout session. Please contact support.",
                           f"Error Code: {e}")


@billing_bp.route("/checkout/sms-bot")
def checkout_sms_bot():
    """SMS Bot plan checkout — AI texting only, no dialer/voice features."""
    # Terms acceptance handled in Stripe Checkout
    try:
        price_id = os.getenv("STRIPE_SMS_BOT_PRICE_ID")
        if not price_id:
            logger.error("STRIPE_SMS_BOT_PRICE_ID environment variable is not set!")
            return _error_page(
                "Configuration Error",
                "The SMS Bot price ID is not configured. Please contact support.",
                "Error Code: MISSING_PRICE_ID"
            )

        customer_email = current_user.email if current_user.is_authenticated else None
        logger.info(f"Creating SMS Bot checkout with price_id: {price_id}")

        referral = request.args.get("referral", "").strip()
        coupon_id = _get_validated_coupon(request.args.get("coupon", "").strip())

        checkout_params = dict(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=customer_email,
            metadata={
                "user_email": customer_email,
                "target_role": "individual",
                "target_tier": "sms_bot",
                "source": "website"
            },
            subscription_data={
                "trial_period_days": 7,
                "metadata": {
                    "user_email": customer_email,
                    "target_role": "individual",
                    "target_tier": "sms_bot"
                },
            },
            success_url=f"{YOUR_DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )
        if referral:
            checkout_params["metadata"]["referral"] = referral
            checkout_params.setdefault("subscription_data", {}).setdefault("metadata", {})["referral"] = referral
        if coupon_id:
            checkout_params["discounts"] = [{"coupon": coupon_id}]
            checkout_params.pop("subscription_data", {}).pop("trial_period_days", None)
        session = stripe.checkout.Session.create(**checkout_params)
        return redirect(session.url, code=303)

    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe SMS Bot checkout error: {e}")
        return _error_page("Checkout Error",
                           "Unable to create checkout session. Please contact support.",
                           f"Error Code: {e}")


@billing_bp.route("/checkout/pro-dialer")
def checkout_pro_dialer():
    """Pro Dialer plan checkout — multi-line dialing + predictive features."""
    # Terms acceptance handled in Stripe Checkout
    try:
        price_id = os.getenv("STRIPE_PRO_DIALER_PRICE_ID")
        if not price_id:
            logger.error("STRIPE_PRO_DIALER_PRICE_ID environment variable is not set!")
            return _error_page(
                "Configuration Error",
                "The Pro Dialer price ID is not configured. Please contact support.",
                "Error Code: MISSING_PRICE_ID"
            )

        customer_email = current_user.email if current_user.is_authenticated else None
        logger.info(f"Creating Pro Dialer checkout with price_id: {price_id}")

        referral = request.args.get("referral", "").strip()
        coupon_id = _get_validated_coupon(request.args.get("coupon", "").strip())

        checkout_params = dict(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=customer_email,
            metadata={
                "user_email": customer_email,
                "target_role": "individual",
                "target_tier": "pro_dialer",
                "source": "website"
            },
            subscription_data={
                "trial_period_days": 7,
                "metadata": {
                    "user_email": customer_email,
                    "target_role": "individual",
                    "target_tier": "pro_dialer"
                },
            },
            success_url=f"{YOUR_DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )

        if coupon_id:
            checkout_params["discounts"] = [{"coupon": coupon_id}]
        else:
            checkout_params["allow_promotion_codes"] = True

        if referral:
            checkout_params["client_reference_id"] = referral

        session = stripe.checkout.Session.create(**checkout_params)
        return redirect(session.url, code=303)

    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe Invalid Request Error (Pro Dialer): {e}")
        return _error_page(
            "Stripe Configuration Error",
            "There's an issue with the payment configuration. Please contact support.",
            f"Error: {e}"
        )
    except Exception as e:
        logger.error(f"Pro Dialer checkout error: {e}")
        return _error_page("Checkout Error",
                           "Unable to create checkout session. Please contact support.",
                           f"Error Code: {e}")


@billing_bp.route("/checkout/predictive-dialer")
@billing_bp.route("/checkout/solo-predictive")
def checkout_solo_predictive():
    """Solo Predictive + AI Overflow checkout — $349/mo with 2000 AI minutes included.

    Erlang-C predictive dialing for solo agents. When the dialer dials multiple
    lines and more than one lead answers, the first call bridges to the human
    and overflow calls bridge to Voice AI which books the appointment.
    """
    # Terms acceptance handled in Stripe Checkout
    try:
        price_id = os.getenv("STRIPE_PREDICTIVE_DIALER_PRICE_ID")
        if not price_id:
            logger.error("STRIPE_PREDICTIVE_DIALER_PRICE_ID environment variable is not set!")
            return _error_page(
                "Configuration Error",
                "The Solo Predictive price ID is not configured. Please contact support.",
                "Error Code: MISSING_PRICE_ID"
            )

        customer_email = current_user.email if current_user.is_authenticated else None
        logger.info(f"Creating Solo Predictive checkout with price_id: {price_id}")

        referral = request.args.get("referral", "").strip()
        coupon_id = _get_validated_coupon(request.args.get("coupon", "").strip())

        checkout_params = dict(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=customer_email,
            metadata={
                "user_email": customer_email,
                "target_role": "individual",
                "target_tier": "solo_predictive",
                "source": "website"
            },
            subscription_data={
                "trial_period_days": 7,
                "metadata": {
                    "user_email": customer_email,
                    "target_role": "individual",
                    "target_tier": "solo_predictive"
                },
            },
            success_url=f"{YOUR_DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )

        if coupon_id:
            checkout_params["discounts"] = [{"coupon": coupon_id}]
        else:
            checkout_params["allow_promotion_codes"] = True

        if referral:
            checkout_params["client_reference_id"] = referral

        session = stripe.checkout.Session.create(**checkout_params)
        return redirect(session.url, code=303)

    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe Invalid Request Error (Solo Predictive): {e}")
        return _error_page(
            "Stripe Configuration Error",
            "There's an issue with the payment configuration. Please contact support.",
            f"Error: {e}"
        )
    except Exception as e:
        logger.error(f"Solo Predictive checkout error: {e}")
        return _error_page("Checkout Error",
                           "Unable to create checkout session. Please contact support.",
                           f"Error Code: {e}")




@billing_bp.route("/cancel")
def cancel():
    return render_template('cancel.html')


@billing_bp.route("/success")
def success():
    session_id   = request.args.get("session_id")
    email        = None
    customer_id  = None

    if session_id:
        try:
            checkout_session = stripe.checkout.Session.retrieve(session_id)
            email = (checkout_session.customer_details.email.lower()
                     if checkout_session.customer_details.email else None)
            customer_id = checkout_session.customer
        except Exception as e:
            logger.error(f"Stripe session retrieve failed: {e}")

    if not email:
        flash("Could not verify payment. Please contact support.", "error")
        return redirect("/")

    # Ensure user record exists — handles race condition with Stripe webhook delivery.
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            temp_id = f"temp_{uuid.uuid4().hex[:8]}"
            cur.execute("""
                INSERT INTO subscribers (
                    location_id, email, stripe_customer_id, role, subscription_tier,
                    crm_user_id, bot_first_name, timezone
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET
                    stripe_customer_id = COALESCE(EXCLUDED.stripe_customer_id,
                                                  subscribers.stripe_customer_id),
                    role = COALESCE(subscribers.role, EXCLUDED.role),
                    subscription_tier = COALESCE(subscribers.subscription_tier,
                                                  EXCLUDED.subscription_tier),
                    updated_at = NOW()
            """, (temp_id, email, customer_id, 'individual', 'individual',
                  '', 'Grok', 'America/Chicago'))
            conn.commit()
            logger.info(f"Success page: ensured user record exists for {email}")
        except Exception as e:
            conn.rollback()
            logger.error(f"Success page user provision error: {e}")
        finally:
            cur.close()
            return_db_connection(conn)

    user = User.get(email)
    if user:
        if user.password_hash:
            flash("Payment confirmed! Please log in to continue.", "success")
            return redirect("/login")

        if not current_user.is_authenticated:
            login_user(user)
            logger.info(f"Auto-login after checkout for {email}")

    return render_template('checkout-success-generate-password.html', email=email)


# ── Stripe billing portal ─────────────────────────────────────────────────────

@billing_bp.route("/create-portal-session", methods=["POST"])
@login_required
def create_portal_session():
    try:
        if not current_user.stripe_customer_id:
            flash("No subscription found! Please subscribe first", "error")
            return redirect("/dashboard")

        session = stripe.billing_portal.Session.create(
            customer=current_user.stripe_customer_id,
            return_url=f"{YOUR_DOMAIN}/dashboard",
        )
        return redirect(session.url)
    except Exception as e:
        logger.error(f"Portal error: {e}")
        flash("Unable to open billing portal", "error")
        return redirect("/dashboard")


# ── Plan switching ────────────────────────────────────────────────────────────

@billing_bp.route("/change-plan", methods=["POST"])
@login_required
def change_plan():
    """
    Change subscription plan. Creates a new Stripe checkout session
    with the target plan, managing the transition through Stripe's
    subscription update flow.
    """
    data = request.get_json(silent=True) or {}
    target_tier = data.get("target_tier", "")

    tier_to_price = {
        "sms_bot": os.getenv("STRIPE_SMS_BOT_PRICE_ID"),
        "individual": os.getenv("STRIPE_PRICE_ID"),
        "pro_dialer": os.getenv("STRIPE_PRO_DIALER_PRICE_ID"),
        "solo_predictive": os.getenv("STRIPE_PREDICTIVE_DIALER_PRICE_ID"),
    }

    if target_tier not in tier_to_price:
        return flask_jsonify({"error": f"Unknown plan: {target_tier}"}), 400

    # Guard: prevent no-op plan change to same tier
    current_tier = current_user.subscription_tier or 'individual'
    if target_tier == current_tier:
        return flask_jsonify({"error": "Already on this plan", "current_tier": current_tier}), 400

    price_id = tier_to_price[target_tier]
    if not price_id:
        return flask_jsonify({"error": f"Price not configured for {target_tier}"}), 500

    if not current_user.stripe_customer_id:
        return flask_jsonify({"error": "No active subscription to change"}), 400

    try:
        # Get current subscription
        subscriptions = stripe.Subscription.list(
            customer=current_user.stripe_customer_id,
            status='active',
            limit=1
        )
        if not subscriptions.data:
            # Try trialing
            subscriptions = stripe.Subscription.list(
                customer=current_user.stripe_customer_id,
                status='trialing',
                limit=1
            )

        if not subscriptions.data:
            return flask_jsonify({"error": "No active subscription found"}), 400

        subscription = subscriptions.data[0]
        if not subscription['items']['data']:
            return flask_jsonify({"error": "Subscription has no line items"}), 400
        sub_item_id = subscription['items']['data'][0]['id']

        # Update subscription to the new price
        stripe.Subscription.modify(
            subscription.id,
            items=[{
                'id': sub_item_id,
                'price': price_id,
            }],
            proration_behavior='create_prorations',
            metadata={
                'target_tier': target_tier,
                'changed_at': datetime.utcnow().isoformat(),
            }
        )

        # Update local DB
        conn = get_db_connection()
        if conn:
            cur = None
            try:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE subscribers SET subscription_tier = %s, updated_at = NOW() WHERE email = %s",
                    (target_tier, current_user.email)
                )
                conn.commit()
                logger.info(f"Plan changed to {target_tier} for {current_user.email}")
            except Exception as db_err:
                logger.error(f"Plan change DB update failed: {db_err}")
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                if cur:
                    try:
                        cur.close()
                    except Exception:
                        pass
                return_db_connection(conn)

        tier_names = {
            "sms_bot": "SMS Bot ($99.98/mo)",
            "individual": "Power Dialer ($149.98/mo)",
            "pro_dialer": "Pro Dialer ($224.98/mo)",
            "solo_predictive": "Solo Predictive + AI Overflow ($349/mo)",
        }
        return flask_jsonify({
            "success": True,
            "new_tier": target_tier,
            "message": f"Plan changed to {tier_names.get(target_tier, target_tier)}"
        })

    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe plan change error: {e}")
        return flask_jsonify({"error": "Unable to change plan. Please contact support."}), 500
    except Exception as e:
        logger.error(f"Plan change error: {e}")
        return flask_jsonify({"error": "Internal server error"}), 500


@billing_bp.route("/subscription-info")
@login_required
def subscription_info():
    """Return current subscription tier info for the dashboard billing UI."""
    tier = current_user.subscription_tier or 'individual'
    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]

    tier_info = {
        "sms_bot": {
            "name": "SMS Bot",
            "price": "$99.98/mo",
            "max_lines": 0,
            "features": ["AI Texting — outbound, inbound & booking", "Rapport-building conversation engine",
                         "6-type objection handling", "Smart Filters & Lead Intelligence",
                         "270+ carrier recognition", "7 CRM integrations", "Auto-booking"],
        },
        "individual": {
            "name": "Power Dialer",
            "price": "$149.98/mo",
            "max_lines": 1,
            "features": ["Single-line dialing", "AI Texting", "AI Voice Agent", "Smart Filters", "Lead Intelligence"],
        },
        "pro_dialer": {
            "name": "Pro Dialer",
            "price": "$224.98/mo",
            "max_lines": 4,
            "features": ["Multi-line dialing (up to 4)", "Predictive dialer", "AI Texting", "AI Voice Agent", "Smart Filters", "Lead Intelligence", "Priority queue"],
        },
        "solo_predictive": {
            "name": "Solo Predictive + AI Overflow",
            "price": "$349/mo",
            "max_lines": 4,
            "included_ai_minutes": 2000,
            "features": [
                "Erlang-C predictive pacing (solo agent)", "AI Overflow safety net",
                "2,000 AI minutes included/month",
                "Multi-line dialing (up to 4)", "TCPA auto-throttle (3% abandon rate)",
                "Recipient timezone enforcement", "Agent state machine",
                "Compliance dashboard", "Recording consent tracking",
                "Callback queue with scheduled re-dials", "Advanced AMD",
                "AI Texting", "AI Voice Agent", "Smart Filters", "Lead Intelligence",
            ],
        },
    }

    info = tier_info.get(tier, tier_info["individual"])
    has_subscription = bool(getattr(current_user, 'stripe_customer_id', None))
    return flask_jsonify({
        "tier": tier,
        "is_admin": is_admin,
        "has_subscription": has_subscription,
        **info
    })


# ── AI Minutes ────────────────────────────────────────────────────────────────

@billing_bp.route("/ai-minutes/balance")
@login_required
def ai_minutes_balance():
    """Get current AI minute balance for the logged-in user."""
    bal = get_ai_minute_balance(current_user.email)
    return flask_jsonify(bal)


@billing_bp.route("/ai-minutes/packages")
@login_required
def ai_minutes_packages():
    """List available AI minute packages with Stripe pricing."""
    packages = []
    for pkg in AI_MINUTE_PACKAGES:
        price_id = os.getenv(pkg["env_key"], "")
        if not price_id:
            continue
        price_display = None
        try:
            price_obj = stripe.Price.retrieve(price_id)
            price_display = price_obj.unit_amount  # cents
        except Exception:
            pass
        packages.append({
            "minutes":     pkg["minutes"],
            "label":       pkg["label"],
            "price_cents": price_display,
            "available":   bool(price_id),
        })
    return flask_jsonify({"packages": packages})


@billing_bp.route("/ai-minutes/checkout", methods=["POST"])
@login_required
def ai_minutes_checkout():
    """Create a Stripe checkout session for an AI minute package."""
    data    = request.get_json() or {}
    minutes = data.get("minutes")
    if not minutes:
        return flask_jsonify({"error": "Missing 'minutes' parameter"}), 400

    pkg = next((p for p in AI_MINUTE_PACKAGES if p["minutes"] == int(minutes)), None)
    if not pkg:
        return flask_jsonify({"error": f"No package found for {minutes} minutes"}), 400

    price_id = os.getenv(pkg["env_key"], "")
    if not price_id:
        return flask_jsonify({"error": f"Price ID not configured for {pkg['label']} package"}), 500

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            customer_email=current_user.email,
            line_items=[{"price": price_id, "quantity": 1}],
            metadata={
                "purchase_type":    "ai_minutes",
                "package_minutes":  str(pkg["minutes"]),
                "package_label":    pkg["label"],
                "user_email":       current_user.email,
            },
            success_url=f"{YOUR_DOMAIN}/dashboard?ai_minutes_success=1",
            cancel_url=f"{YOUR_DOMAIN}/dashboard?ai_minutes_cancel=1",
        )
        return flask_jsonify({"checkout_url": checkout_session.url})
    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe AI minutes checkout error: {e}")
        return flask_jsonify({"error": "Payment configuration error. Contact support."}), 500
    except Exception as e:
        logger.error(f"AI minutes checkout error: {e}")
        return flask_jsonify({"error": "Unable to create checkout session."}), 500


@billing_bp.route("/ai-minutes/usage")
@login_required
def ai_minutes_usage():
    """Return AI minute purchase and usage history for the current user."""
    purchases = get_ai_minute_purchases(current_user.email)
    usage     = get_ai_minute_usage(current_user.email)

    for p in purchases:
        for k in ('created_at', 'completed_at'):
            if p.get(k):
                p[k] = p[k].isoformat()
    for u in usage:
        if u.get('created_at'):
            u['created_at'] = u['created_at'].isoformat()

    return flask_jsonify({"purchases": purchases, "usage": usage})


# ── A2P 10DLC fee payments ────────────────────────────────────────────────────

@billing_bp.route("/a2p/checkout", methods=["POST"])
@login_required
def a2p_checkout():
    """
    Create a Stripe one-time payment session for A2P 10DLC registration fees.
    Fee varies by brand type. The frontend sends brand_type; we compute the
    total from A2P_FEE_SCHEDULE and create a dynamic checkout via price_data.
    """
    data       = request.get_json(silent=True) or {}
    brand_type = (data.get("brand_type") or "LOW_VOLUME").upper().strip()

    fee_info = A2P_FEE_SCHEDULE.get(brand_type)
    if not fee_info:
        return flask_jsonify({"error": f"Unknown brand type: {brand_type}"}), 400

    total_cents = fee_info["brand_fee"] + fee_info["campaign_fee"]
    label       = fee_info["label"]

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            customer_email=current_user.email,
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": total_cents,
                    "product_data": {
                        "name": f"A2P 10DLC Registration — {label}",
                        "description": (
                            f"Brand registration (${fee_info['brand_fee'] / 100:.2f}) "
                            f"+ Campaign vetting ($15.00)"
                        ),
                    },
                },
                "quantity": 1,
            }],
            metadata={
                "purchase_type": "a2p_registration",
                "user_email":   current_user.email,
                "brand_type":   brand_type,
                "total_cents":  str(total_cents),
            },
            success_url=f"{YOUR_DOMAIN}/dashboard?a2p_payment_success=1",
            cancel_url=f"{YOUR_DOMAIN}/dashboard?a2p_payment_cancel=1",
        )
        return flask_jsonify({"checkout_url": checkout_session.url})
    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe A2P checkout error: {e}")
        return flask_jsonify({"error": "Payment configuration error. Contact support."}), 500
    except Exception as e:
        logger.error(f"A2P checkout error: {e}")
        return flask_jsonify({"error": "Unable to create checkout session."}), 500


@billing_bp.route("/a2p/fee-schedule")
@login_required
def a2p_fee_schedule():
    """Return the A2P fee schedule so the frontend can display correct prices."""
    schedule = {}
    for key, info in A2P_FEE_SCHEDULE.items():
        total = info["brand_fee"] + info["campaign_fee"]
        schedule[key] = {
            "label":        info["label"],
            "brand_fee":    info["brand_fee"] / 100,
            "campaign_fee": info["campaign_fee"] / 100,
            "total":        total / 100,
        }
    return flask_jsonify(schedule)
