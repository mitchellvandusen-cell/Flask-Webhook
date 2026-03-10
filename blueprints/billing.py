# blueprints/billing.py — Stripe billing, subscription checkout, AI Minutes, A2P fee payments
#
# Routes:
#   POST /stripe-webhook            — Stripe event handler (checkout, AI minutes, A2P fee)
#   GET  /checkout                  — Individual plan checkout
#   GET  /checkout/agency-starter   — Agency Starter checkout
#   GET  /checkout/agency-pro       — Agency Pro checkout
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
from datetime import datetime

import stripe
from flask import (Blueprint, redirect, request, render_template_string,
                   render_template, flash, make_response)
from flask import jsonify as flask_jsonify
from flask_login import login_required, login_user, current_user

from extensions import YOUR_DOMAIN, safe_jsonify
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

        # ── A2P 10DLC registration fee ────────────────────────────────────────
        if metadata.get("purchase_type") == "a2p_registration" and email:
            brand_type = metadata.get("brand_type", "LOW_VOLUME")
            paid_cents = metadata.get("total_cents", "0")
            logger.info(f"A2P fee paid by {email} — brand_type={brand_type} amount=${int(paid_cents)/100:.2f}")
            try:
                conn = get_db_connection()
                if conn:
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
                        cur.close()
                    except Exception as e:
                        logger.error(f"Failed to mark A2P fee paid: {e}")
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                    finally:
                        return_db_connection(conn)
            except Exception as e:
                logger.error(f"A2P fee webhook error: {e}")
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
                            crm_user_id, bot_first_name, timezone
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (email) DO UPDATE SET
                            stripe_customer_id = EXCLUDED.stripe_customer_id,
                            role = EXCLUDED.role,
                            subscription_tier = EXCLUDED.subscription_tier;
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

    return '', 200


# ── Checkout pages ────────────────────────────────────────────────────────────

@billing_bp.route("/checkout")
def checkout():
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

        checkout_params = dict(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            allow_promotion_codes=True,
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

        # Rewardful affiliate tracking — pass referral ID so Rewardful can attribute the conversion
        referral = request.args.get("referral", "").strip()
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


@billing_bp.route("/checkout/agency-starter")
def checkout_agency_starter():
    """
    Agency Starter guest checkout. No login required — webhook provisions account
    after payment. Seat count verification happens after OAuth, not at checkout.
    """
    try:
        price_id = os.getenv("STRIPE_AGENCY_STARTER_PRICE_ID")
        if not price_id:
            logger.error("STRIPE_AGENCY_STARTER_PRICE_ID environment variable is not set!")
            return _error_page(
                "Configuration Error",
                "The Agency Starter price ID is not configured. Please contact support.",
                "Error Code: MISSING_PRICE_ID"
            )

        customer_email = current_user.email if current_user.is_authenticated else None
        logger.info(f"Creating Agency Starter checkout with price_id: {price_id}")

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            customer_email=customer_email,
            line_items=[{"price": price_id, "quantity": 1}],
            allow_promotion_codes=True,
            metadata={
                "target_role": "agency_owner",
                "target_tier": "agency_starter",
                "source": "website_checkout"
            },
            subscription_data={
                "trial_period_days": 7,
                "metadata": {
                    "target_role": "agency_owner",
                    "target_tier": "agency_starter"
                }
            },
            success_url=f"{YOUR_DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )
        return redirect(session.url, code=303)

    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe Invalid Request Error (Agency Starter): {e}")
        return _error_page(
            "Stripe Configuration Error",
            "There's an issue with the payment configuration. Please contact support.",
            f"Error: {e}"
        )
    except Exception as e:
        logger.error(f"Agency Starter checkout error: {e}")
        return _error_page("Checkout Error",
                           "Unable to create checkout session. Please contact support.",
                           f"Error Code: {e}")


@billing_bp.route("/checkout/agency-pro")
def checkout_agency_pro():
    """
    Agency Pro guest checkout. No login required. Requires agency domain field to
    deter single-user buyers from accidentally picking the enterprise plan.
    """
    try:
        price_id = os.getenv("STRIPE_AGENCY_PRO_PRICE_ID")
        if not price_id:
            logger.error("STRIPE_AGENCY_PRO_PRICE_ID environment variable is not set!")
            return _error_page(
                "Configuration Error",
                "The Agency Pro price ID is not configured. Please contact support.",
                "Error Code: MISSING_PRICE_ID"
            )

        customer_email = current_user.email if current_user.is_authenticated else None
        logger.info(f"Creating Agency Pro checkout with price_id: {price_id}")

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            customer_email=customer_email,
            line_items=[{"price": price_id, "quantity": 1}],
            allow_promotion_codes=True,
            custom_fields=[{
                "key": "agency_whitelabel_domain",
                "label": {
                    "type": "custom",
                    "custom": "Agency Domain (e.g. app.youragency.com)"
                },
                "type": "text",
            }],
            metadata={
                "target_role": "agency_owner",
                "target_tier": "agency_pro",
                "source": "high_ticket_portal"
            },
            success_url=f"{YOUR_DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{YOUR_DOMAIN}/cancel",
        )
        return redirect(session.url, code=303)

    except stripe.error.InvalidRequestError as e:
        logger.error(f"Stripe Invalid Request Error (Agency Pro): {e}")
        return _error_page(
            "Stripe Configuration Error",
            "There's an issue with the payment configuration. Please contact support.",
            f"Error: {e}"
        )
    except Exception as e:
        logger.critical(f"Pro Checkout Launch Error: {e}")
        return _error_page("Checkout Error",
                           "The Enterprise Portal is temporarily unavailable. Please contact support.",
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
