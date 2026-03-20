# blueprints/oauth.py — GHL OAuth flow: initiate, callback, loading screen, subscriber refresh
#
# Routes:
#   GET  /oauth/initiate    — Start GHL OAuth (redirect to consent page)
#   GET  /oauth/callback    — GHL OAuth callback (token exchange, onboarding, DB write)
#   GET  /oauth/loading     — Post-OAuth loading screen (private app flow)
#   GET  /refresh           — Manually trigger subscriber sync

import os
import json
import logging
import secrets
import requests
from urllib.parse import urlencode

from flask import (
    Blueprint, request, redirect, url_for, render_template,
    flash, jsonify as flask_jsonify, session
)
from flask_login import login_required, current_user, login_user

from db import (
    get_db_connection, return_db_connection, get_db_connection_with_retry,
    User, log_webhook_event, save_persistent_alert,
    mark_install_oauth_complete, find_marketplace_email,
)
from extensions import ADMIN_EMAILS, YOUR_DOMAIN
from email_templates import _email_wrapper, _build_welcome_email, _build_agency_owner_welcome_email
from send_email_api import send_email_via_api
from sync_subscribers import sync_subscribers
from token_encryption import encrypt_token, decrypt_token
from ghl_api import fetch_all_ghl_items, ghl_api_call as _ghl_api_call

logger = logging.getLogger(__name__)

oauth_bp = Blueprint('oauth', __name__)

# ── OAuth scopes ─────────────────────────────────────────────────────────────
# Synced exactly to the marketplace app install URL as of 2026-03-11.
GHL_OAUTH_SCOPES = [
    "calendars.readonly",
    "calendars/events.readonly",
    "calendars/events.write",
    "calendars/groups.readonly",
    "contacts.readonly",
    "contacts.write",
    "conversations.readonly",
    "conversations.write",
    "conversations/message.readonly",
    "conversations/message.write",
    "locations.readonly",
    "locations/customFields.readonly",
    "locations/customFields.write",
    "locations/customValues.readonly",
    "locations/tags.readonly",
    "locations/tags.write",
    "oauth.readonly",
    "opportunities.readonly",
    "phonenumbers.read",
    "users.readonly",
    "workflows.readonly",
    "twilioaccount.read",
]


# ── Routes ────────────────────────────────────────────────────────────────────

@oauth_bp.route("/refresh")
def refresh_subscribers():
    """Manually trigger subscriber sync from external source."""
    try:
        sync_subscribers()
        return "Synced", 200
    except Exception:
        return "Failed", 500


@oauth_bp.route("/oauth/initiate")
@login_required
def oauth_initiate():
    """
    Initiates OAuth flow with Lead Connector.
    Uses the public marketplace app by default (all scopes now approved).
    Falls back to private app if USE_PRIVATE_APP=true.

    User clicks "Connect with Lead Connector" → Redirected to consent page → Back to /oauth/callback

    SECURITY: Requires active login and valid Stripe subscription.
    """
    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
    needs_subscription = not current_user.stripe_customer_id and not is_admin

    if needs_subscription:
        flash("You must have an active subscription to connect Lead Connector. Please subscribe first.", "error")
        logger.warning(f"OAuth initiate blocked for {current_user.email} - no active subscription")
        if current_user.role == 'agency_owner':
            return redirect(url_for('agency.agency_dashboard'))
        return redirect(url_for('dashboard.dashboard'))

    use_private = os.getenv("USE_PRIVATE_APP", "").lower() in ("true", "1", "yes")

    if use_private:
        client_id = os.getenv("PRIVATE_APP_CLIENT_ID") or os.getenv("GHL_PRIVATE_CLIENT_ID")
        env_label = "PRIVATE_APP_CLIENT_ID"
    else:
        client_id = os.getenv("GHL_CLIENT_ID")
        env_label = "GHL_CLIENT_ID"

    domain = os.getenv("YOUR_DOMAIN")
    if not client_id or not domain:
        logger.error(
            f"OAuth initiate failed: {env_label}={'set' if client_id else 'MISSING'}, "
            f"YOUR_DOMAIN={'set' if domain else 'MISSING'}"
        )
        flash("OAuth is not configured. Please contact support.", "error")
        return redirect(url_for('dashboard.dashboard'))

    redirect_uri = f"{domain}/oauth/callback"

    scope_string = " ".join(GHL_OAUTH_SCOPES)

    # ── CSRF protection: cryptographic state nonce ────────────────────────
    # Encode the flow type ("private_app" or "website_user") alongside a
    # random nonce so the callback can both (a) validate the request origin
    # and (b) determine which credential set to use.
    flow_type = "private_app" if use_private else "website_user"
    nonce = secrets.token_urlsafe(32)
    state = f"{flow_type}:{nonce}"
    session["ghl_oauth_state"] = state

    oauth_params_dict = {
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'scope': scope_string,
        'state': state,
    }

    oauth_params = urlencode(oauth_params_dict)
    oauth_url = f"https://marketplace.gohighlevel.com/oauth/chooselocation?{oauth_params}"

    logger.info(
        f"Initiating OAuth flow for {current_user.email} (private={use_private}). "
        f"Redirecting to GHL consent page."
    )
    return redirect(oauth_url)


@oauth_bp.route("/oauth/callback")
def oauth_callback():
    """
    GHL OAuth callback: exchanges authorization code for tokens, provisions
    subscriber records in the DB, and sends welcome email.

    Handles:
    - Marketplace installs (no state)
    - Private app installs (state="private_app")
    - Website user reconnects (state="website_user")
    - Dual-app auto-detection (tries marketplace creds first, then private)
    - Company-level (agency) and Location-level installs
    - Robust 5-step email recovery chain (never fails silently)
    - Agency sub-account provisioning with paginated location fetch
    """
    code = request.args.get("code")
    raw_state = request.args.get("state")

    # GHL may pass locationId as a URL query parameter on the redirect.
    # Capture it now — if the token exchange returns locationId=None, this
    # is often the only way to identify which sub-account was installed.
    url_location_id = request.args.get("locationId")

    logger.info(
        f"=== OAUTH CALLBACK START === state={'present' if raw_state else 'MISSING'}, "
        f"code={'present' if code else 'MISSING'}, "
        f"locationId_from_url={'present: ' + url_location_id if url_location_id else 'MISSING'}"
    )

    try:
        log_webhook_event("oauth_global", "oauth_callback_hit", "info",
                          f"OAuth callback received: state={'yes' if raw_state else 'NO'}, "
                          f"code={'yes' if code else 'NO'}",
                          details={"has_state": bool(raw_state), "has_code": bool(code)})
    except Exception:
        logger.debug("Failed to log oauth_callback_hit event")

    if not code:
        logger.warning("OAuth callback: No authorization code in request params")
        try:
            log_webhook_event("oauth_global", "oauth_callback_error", "error",
                              "No authorization code in callback params")
        except Exception:
            logger.debug("Failed to log oauth_callback_error event")
        flash("No authorization code received.", "danger")
        return redirect(url_for('public.home'))

    try:
        # ── Validate state parameter (CSRF protection) ────────────────────────
        # Website users (state contains "website_user:" or "private_app:" prefix
        # followed by a nonce) MUST have a matching session state. Marketplace
        # installs arrive with state=None from GHL directly and bypass validation.
        flow_type = None

        if raw_state and ":" in raw_state:
            # New-format state: "flow_type:nonce"
            stored_state = session.pop("ghl_oauth_state", None)
            if not stored_state or not secrets.compare_digest(raw_state, stored_state):
                logger.warning(
                    f"OAuth CSRF validation failed: state mismatch "
                    f"(received={'present' if raw_state else 'NONE'}, "
                    f"stored={'present' if stored_state else 'NONE'})"
                )
                flash("OAuth session expired or invalid. Please try connecting again.", "danger")
                return redirect(url_for('dashboard.dashboard'))
            flow_type = raw_state.split(":", 1)[0]
            session.pop("ghl_pkce_verifier", None)  # Clean up legacy session key
            logger.info(f"OAuth state validated (flow_type={flow_type})")
        elif raw_state in ("website_user", "private_app"):
            # Legacy static state (from sessions started before this update)
            flow_type = raw_state
            logger.info(f"OAuth callback: Legacy static state ({raw_state})")
        else:
            # state=None — marketplace install
            logger.info("OAuth callback: Marketplace installation flow (no state)")

        is_website_user = flow_type in ("website_user", "private_app")

        if is_website_user:
            if not current_user.is_authenticated:
                flash("You must be logged in to connect Lead Connector.", "error")
                logger.warning("OAuth callback blocked - user not authenticated")
                return redirect(url_for('auth.login'))

            is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
            needs_subscription = not current_user.stripe_customer_id and not is_admin

            if needs_subscription:
                flash("Active subscription required to connect Lead Connector. Please subscribe first.", "error")
                logger.warning(f"OAuth callback blocked for {current_user.email} - no active subscription")
                if current_user.role == 'agency_owner':
                    return redirect(url_for('agency.agency_dashboard'))
                return redirect(url_for('dashboard.dashboard'))

            logger.info(f"OAuth callback: Website user flow for {current_user.email}")
        else:
            logger.info("OAuth callback: Marketplace installation flow")

        # ── Pick credentials ──────────────────────────────────────────────────
        use_private_env = os.getenv("USE_PRIVATE_APP", "").lower() in ("true", "1", "yes")

        marketplace_client_id = os.getenv("GHL_CLIENT_ID")
        marketplace_client_secret = os.getenv("GHL_CLIENT_SECRET")
        private_client_id = os.getenv("PRIVATE_APP_CLIENT_ID") or os.getenv("GHL_PRIVATE_CLIENT_ID")
        private_client_secret = os.getenv("PRIVATE_APP_SECRET_ID") or os.getenv("GHL_PRIVATE_CLIENT_SECRET")
        has_marketplace_creds = bool(marketplace_client_id and marketplace_client_secret)
        has_private_creds = bool(private_client_id and private_client_secret)

        if flow_type == "private_app":
            is_private_app = True
            client_id = private_client_id
            client_secret = private_client_secret
            cred_label = "PRIVATE_APP"
        elif flow_type is None and has_marketplace_creds and has_private_creds:
            # DUAL-APP MODE: try marketplace first, fallback to private
            is_private_app = False
            client_id = marketplace_client_id
            client_secret = marketplace_client_secret
            cred_label = "AUTO-DETECT (trying marketplace first)"
        elif flow_type is None and use_private_env:
            is_private_app = True
            client_id = private_client_id
            client_secret = private_client_secret
            cred_label = "PRIVATE_APP (only app)"
        else:
            is_private_app = False
            client_id = marketplace_client_id
            client_secret = marketplace_client_secret
            cred_label = "GHL (marketplace)"

        domain = os.getenv("YOUR_DOMAIN")
        if not client_id or not client_secret or not domain:
            logger.error(
                f"OAuth env vars missing ({cred_label}): "
                f"client_id={'set' if client_id else 'MISSING'}, "
                f"client_secret={'set' if client_secret else 'MISSING'}, "
                f"YOUR_DOMAIN={'set' if domain else 'MISSING'}"
            )
            flash("OAuth is not configured. Please contact support.", "danger")
            return redirect(url_for('public.home'))

        logger.info(f"OAuth callback using {cred_label} credentials (flow_type={flow_type})")

        # ── Step 1: Token exchange ─────────────────────────────────────────────
        # Try Location user_type first, then Company (for agency-level installs).
        # DUAL-APP: if both credential sets exist and state=None, auto-detect.
        token_url = "https://services.leadconnectorhq.com/oauth/token"

        cred_sets = [{"client_id": client_id, "client_secret": client_secret,
                      "label": cred_label, "is_private": is_private_app}]
        if flow_type is None and has_marketplace_creds and has_private_creds:
            cred_sets.append({
                "client_id": private_client_id, "client_secret": private_client_secret,
                "label": "PRIVATE_APP (fallback)", "is_private": True
            })

        token_data = None
        token_user_type_used = None
        for cred_set in cred_sets:
            base_payload = {
                "client_id": cred_set["client_id"],
                "client_secret": cred_set["client_secret"],
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{domain}/oauth/callback",
            }
            for user_type in ["Location", "Company"]:
                payload = {**base_payload, "user_type": user_type}
                logger.info(f"Token exchange attempt with user_type={user_type}, creds={cred_set['label']}")

                token_resp, token_err = _ghl_api_call(
                    'POST', token_url, data=payload, timeout=15,
                    label=f"Token exchange ({user_type}, {cred_set['label']})"
                )

                if token_resp is None:
                    logger.warning(f"Token exchange ({user_type}, {cred_set['label']}) unreachable: {token_err}")
                    continue

                if token_resp.ok:
                    try:
                        token_data = token_resp.json()
                        token_user_type_used = user_type
                        is_private_app = cred_set["is_private"]
                        cred_label = cred_set["label"]
                        logger.info(f"Token exchange SUCCESS with user_type={user_type}, creds={cred_set['label']}")
                        break
                    except ValueError:
                        logger.error(
                            f"Token exchange ({user_type}, {cred_set['label']}) "
                            f"returned non-JSON: {token_resp.text[:500]}"
                        )
                        continue
                elif token_resp.status_code == 400:
                    logger.warning(
                        f"Token exchange ({user_type}, {cred_set['label']}) got 400: "
                        f"{token_resp.text[:300]} — trying next"
                    )
                    continue
                else:
                    logger.error(
                        f"Token exchange ({user_type}, {cred_set['label']}) rejected: "
                        f"{token_resp.status_code} {token_resp.text[:500]}"
                    )
                continue

            if token_data:
                break

        if not token_data:
            err_msg = "Token exchange failed for all user_types (Location, Company)"
            logger.error(err_msg)
            try:
                log_webhook_event("oauth_global", "oauth_token_exchange_failed", "error",
                                  err_msg, details={"flow_type": flow_type, "code_present": bool(code)})
            except Exception:
                logger.debug("Failed to log token exchange failure event")
            flash("Failed to connect to Lead Connector. Please try again.", "danger")
            return redirect(url_for('public.home'))

        access_token = token_data.get('access_token')
        if not access_token:
            logger.error("Token exchange returned no access_token")
            try:
                log_webhook_event("oauth_global", "oauth_no_access_token", "error",
                                  "Token exchange missing access_token")
            except Exception:
                logger.debug("Failed to log missing access_token event")
            flash("Authorization failed — no access token received. Please try again.", "danger")
            return redirect(url_for('public.home'))

        primary_location_id = token_data.get('locationId')
        company_id = token_data.get('companyId')
        refresh_token = token_data.get('refresh_token')
        expires_in = token_data.get('expires_in', 86400)

        # CRITICAL FIX: GHL sometimes returns locationId=None in the token
        # response even for Location-scoped installs. Fall back to the
        # locationId from the callback URL query parameters.
        if not primary_location_id and url_location_id:
            primary_location_id = url_location_id
            logger.info(
                f"Token response had locationId=None — using locationId from "
                f"URL query param: {url_location_id}"
            )

        # ── Scope validation ─────────────────────────────────────────────────
        # Verify that the granted scopes include the critical ones we need.
        granted_scope_str = token_data.get('scope', '')
        if granted_scope_str:
            granted_scopes = set(granted_scope_str.split())
            critical_scopes = {
                "contacts.readonly", "conversations/message.write",
                "conversations/message.readonly", "oauth.readonly",
            }
            missing_critical = critical_scopes - granted_scopes
            if missing_critical:
                logger.error(f"Critical scopes MISSING from grant: {missing_critical}")
                try:
                    log_webhook_event(
                        primary_location_id or "unknown", "oauth_scope_mismatch", "error",
                        f"Critical scopes missing: {missing_critical}",
                        details={"granted": list(granted_scopes), "missing": list(missing_critical)})
                except Exception:
                    logger.debug("Failed to log scope mismatch event")
                flash("Some required permissions were not granted. Please reconnect and accept all permissions.", "danger")
                return redirect(url_for('dashboard.dashboard'))

            # Log non-critical scope differences as warnings
            requested_scopes = set(GHL_OAUTH_SCOPES)
            missing_optional = requested_scopes - granted_scopes
            if missing_optional:
                logger.warning(f"Optional scopes not granted: {missing_optional}")

        # Encrypt tokens before any DB storage
        enc_access_token = encrypt_token(access_token)
        enc_refresh_token = encrypt_token(refresh_token) if refresh_token else None

        logger.info(
            f"Step 1 complete: Token exchange OK via user_type={token_user_type_used}. "
            f"locationId={primary_location_id}, companyId={company_id}, expires_in={expires_in}"
        )

        try:
            log_webhook_event(
                primary_location_id or company_id or "unknown",
                "oauth_token_success", "success",
                f"Token exchange OK: user_type={token_user_type_used}, "
                f"locationId={primary_location_id}, companyId={company_id}",
                details={
                    "user_type_used": token_user_type_used,
                    "locationId": primary_location_id,
                    "companyId": company_id,
                    "scopes_granted": len(granted_scope_str.split()) if granted_scope_str else 0,
                }
            )
        except Exception:
            logger.debug("Failed to log token success event")

        headers_ghl = {'Authorization': f'Bearer {access_token}', 'Version': '2021-07-28'}

        # ── Step 2: Get user info ──────────────────────────────────────────────
        # GHL has no /users/me endpoint. Use GET /users/{userId} with the
        # userId from the token exchange.  Note: this only works with
        # Company-scoped tokens; Location-scoped tokens will get 401/403,
        # which the fallback chain below handles gracefully.
        me_data = {}
        user_email = None
        user_name = None

        ghl_user_id = token_data.get('userId')
        if ghl_user_id:
            me_data['id'] = ghl_user_id
            me_resp, me_err = _ghl_api_call(
                'GET', f"https://services.leadconnectorhq.com/users/{ghl_user_id}",
                headers=headers_ghl, timeout=10, label=f"/users/{ghl_user_id}"
            )

            if me_resp and me_resp.ok:
                try:
                    me_data = me_resp.json()
                    user_email = me_data.get('email')
                    user_name = me_data.get('name') or me_data.get('firstName')
                except ValueError:
                    logger.warning(f"/users/{ghl_user_id} returned non-JSON: {me_resp.text[:300]}")
            elif me_resp and me_resp.status_code in (401, 403):
                logger.info(
                    f"/users/{ghl_user_id} returned {me_resp.status_code} — "
                    f"expected with Location-scoped tokens. Using fallback chain."
                )
            elif me_resp:
                logger.warning(f"/users/{ghl_user_id} failed: {me_resp.status_code} {me_resp.text[:300]}")
            else:
                logger.warning(f"/users/{ghl_user_id} unreachable: {me_err}")
        else:
            logger.info("No userId in token_data — skipping /users/ call")

        # ── Capture CRM email from GHL ───────────────────────────────────────
        # The email from /users/{userId} or token_data is the CRM email.
        # This is stored separately so it never overwrites the login email.
        crm_email_resolved = user_email  # from /users/{userId} if it worked
        if not crm_email_resolved:
            crm_email_resolved = token_data.get('userEmail') or token_data.get('email')

        # ── Robust email recovery chain ──────────────────────────────────────
        # OAuth must NEVER fail silently. Try every source; placeholder as last resort.
        # user_email = the account/login email (what they log in with).

        # Priority 1: logged-in user — their login email is always canonical
        if current_user.is_authenticated:
            user_email = current_user.email
            user_name = current_user.full_name or user_name
            logger.info(f"Using logged-in user's email: {user_email} (CRM email: {crm_email_resolved})")

        # Fallback 1: email in token_data (marketplace installs — user isn't logged in)
        if not user_email:
            user_email = token_data.get('userEmail') or token_data.get('email')
            if user_email:
                logger.info(f"Fallback 1: Got email from token_data: {user_email}")

        # Fallback 3: marketplace_installs table bridge
        if not user_email:
            market_data = find_marketplace_email(
                location_id=primary_location_id, company_id=company_id
            )
            if market_data:
                user_email = market_data.get('user_email')
                user_name = market_data.get('user_name') or user_name
                logger.info(f"Fallback 3: BRIDGED email from marketplace_installs: {user_email}")

        # Fallback 4: look up by GHL userId in subscribers table
        if not user_email:
            ghl_user_id = token_data.get('userId')
            if ghl_user_id:
                try:
                    conn_lookup = get_db_connection()
                    if conn_lookup:
                        cur_lookup = conn_lookup.cursor()
                        cur_lookup.execute(
                            "SELECT email FROM subscribers WHERE crm_user_id = %s LIMIT 1",
                            (ghl_user_id,)
                        )
                        found = cur_lookup.fetchone()
                        if found:
                            user_email = found['email']
                            logger.info(f"Fallback 4: Found email via userId lookup: {user_email}")
                        cur_lookup.close()
                        return_db_connection(conn_lookup)
                except Exception:
                    pass

        # Fallback 5: placeholder — onboarding completes, admin gets notified
        if not user_email:
            ghl_user_id = token_data.get('userId') or 'unknown'
            user_email = f"install_{ghl_user_id}@placeholder.grokbot"
            user_name = "New User (Update Email)"
            logger.warning(f"Fallback 5: ALL email sources exhausted. Using placeholder: {user_email}")

            try:
                log_webhook_event(
                    primary_location_id or "unknown", "oauth_placeholder_account", "warning",
                    f"Placeholder account created: {user_email} — userId={ghl_user_id}, "
                    f"locationId={primary_location_id}, companyId={company_id}",
                    details={
                        "userId": ghl_user_id,
                        "locationId": primary_location_id,
                        "companyId": company_id,
                        "token_keys": list(token_data.keys()),
                    }
                )
            except Exception:
                pass

            # Alert admin via email
            try:
                admin_target = ADMIN_EMAILS[0] if ADMIN_EMAILS else "mitchell_vandusen@hotmail.com"
                domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
                alert_inner = f'''
<tr><td style="padding: 20px 40px 30px;">
    <h1 style="margin: 0 0 16px; font-size: 22px; font-weight: 800; color: #ff6b35;">Ghost Install Detected</h1>
    <p style="font-size: 15px; color: #ccc; line-height: 1.6;">
        A user installed the app but Lead Connector permissions blocked their email.
        A placeholder account was created so they can access the dashboard.
    </p>
    <table cellpadding="0" cellspacing="0" width="100%" style="background: rgba(255,255,255,0.04); border-radius: 10px; border: 1px solid rgba(255,255,255,0.08); margin: 20px 0;">
        <tr><td style="padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #888; width: 130px;">LC User ID</td>
            <td style="padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #fff; font-family: monospace;">{ghl_user_id}</td></tr>
        <tr><td style="padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #888;">Location ID</td>
            <td style="padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #fff; font-family: monospace;">{primary_location_id or 'N/A'}</td></tr>
        <tr><td style="padding: 12px 16px; color: #888;">Company ID</td>
            <td style="padding: 12px 16px; color: #fff; font-family: monospace;">{company_id or 'N/A'}</td></tr>
    </table>
    <p style="font-size: 14px; color: #aaa;">Search this Location ID in your Lead Connector Agency View to find the user\'s real email, then update their record in the database.</p>
</td></tr>'''
                send_email_via_api(
                    to_email=admin_target,
                    subject="Ghost Install — Action Required",
                    html_body=_email_wrapper(alert_inner, domain_url),
                    text_body=f"Ghost install: userId={ghl_user_id}, "
                              f"locationId={primary_location_id}, companyId={company_id}"
                )
                logger.info(f"Admin ghost-install alert sent to {admin_target}")
            except Exception as e:
                logger.error(f"Failed to send admin ghost-install alert: {e}")

            try:
                save_persistent_alert(
                    email=ADMIN_EMAILS[0] if ADMIN_EMAILS else "admin",
                    alert_type="ghost_install",
                    title="Ghost Install — Email Unknown",
                    message=(
                        f"User installed app but email couldn't be retrieved. "
                        f"userId={ghl_user_id}, locationId={primary_location_id}, companyId={company_id}. "
                        f"Placeholder account: {user_email}"
                    ),
                    severity="warning",
                    location_id=primary_location_id or "unknown",
                )
            except Exception:
                pass

        logger.info(f"Step 2 complete: User info retrieved. email={user_email}")

        # ── Step 3: Detect agency status via companyId ────────────────────────
        # Agency owner = has companyId but NO locationId (regardless of token scope).
        # GHL marketplace installs for agency owners come in as Location-scoped
        # tokens with companyId present but locationId=None.
        # Individual agent = has locationId (regardless of whether companyId is also present).
        is_agency_owner = False
        company_metadata = {}

        if company_id and not primary_location_id:
            # companyId present but no locationId = agency owner install
            is_agency_owner = True
            logger.info(f"Agency owner detected: companyId={company_id}, no locationId, token_type={token_user_type_used}")

            # Capture all available company/owner metadata from GHL
            company_metadata = {
                'company_id': company_id,
                'company_name': None,
                'company_owner_name': me_data.get('name') or me_data.get('firstName', ''),
                'company_owner_email': me_data.get('email'),
                'company_owner_phone': me_data.get('phone'),
            }
            # Try to get company name from user data or token data
            if me_data.get('companyName'):
                company_metadata['company_name'] = me_data['companyName']
            elif me_data.get('company', {}).get('name') if isinstance(me_data.get('company'), dict) else None:
                company_metadata['company_name'] = me_data['company']['name']

            # Construct owner name from parts if full name not available
            if not company_metadata['company_owner_name']:
                first = me_data.get('firstName', '')
                last = me_data.get('lastName', '')
                company_metadata['company_owner_name'] = f"{first} {last}".strip() or None

            logger.info(f"Company metadata captured: {company_metadata}")
        elif company_id:
            logger.info(f"Individual install with companyId={company_id} — will auto-link to agency if exists")
        else:
            logger.info("Location-scoped token without companyId — individual user")

        logger.info(f"Step 3 complete: Agency detection. is_agency={is_agency_owner}, companyId={company_id}")

        # ── Step 4: Fetch all locations ──────────────────────────────────────
        # /locations/search requires a Company-scoped token.  For Location-
        # scoped tokens, fetch the single location directly via
        # GET /locations/{locationId}.
        #
        # EDGE CASE: GHL sometimes returns a Location-scoped token with a
        # companyId but NO locationId (marketplace installs where user didn't
        # pick a specific sub-account). In that case, try /locations/search
        # with the companyId to discover all locations.
        sub_accounts = []
        using_location_fallback = False

        if token_user_type_used == 'Company' and company_id:
            locations_url = f"https://services.leadconnectorhq.com/locations/search?companyId={company_id}"
            sub_accounts = fetch_all_ghl_items(locations_url, headers_ghl, item_key='locations')
        elif not primary_location_id and company_id:
            # Location-scoped token with companyId but no locationId.
            # The token IS scoped to a specific location, but GHL didn't
            # tell us which one. Try multiple discovery approaches.
            logger.info(
                f"No locationId in token or URL but companyId={company_id} present. "
                f"Attempting location discovery..."
            )

            # Approach 1: /oauth/installedLocations — returns locations where
            # this app is installed under this company
            app_id = client_id  # GHL appId = our OAuth client_id
            installed_url = (
                f"https://services.leadconnectorhq.com/oauth/installedLocations"
                f"?companyId={company_id}"
            )
            installed_resp, installed_err = _ghl_api_call(
                'GET', installed_url,
                headers=headers_ghl, timeout=10,
                label=f"/oauth/installedLocations?companyId={company_id}"
            )
            if installed_resp and installed_resp.ok:
                try:
                    installed_data = installed_resp.json()
                    # Response format: {"locations": [{"_id": "xxx", "name": "...", ...}]}
                    # or: {"installedLocations": [...]}
                    installed_locs = (
                        installed_data.get('locations')
                        or installed_data.get('installedLocations')
                        or []
                    )
                    if isinstance(installed_data, list):
                        installed_locs = installed_data
                    if installed_locs:
                        # Use first installed location
                        first_loc = installed_locs[0]
                        primary_location_id = (
                            first_loc.get('_id')
                            or first_loc.get('id')
                            or first_loc.get('locationId')
                        )
                        sub_accounts = [{
                            'id': primary_location_id,
                            'name': first_loc.get('name', user_name or 'Primary Location'),
                            'timezone': first_loc.get('timezone'),
                        }]
                        logger.info(
                            f"installedLocations returned {len(installed_locs)} location(s). "
                            f"Primary set to: {primary_location_id}"
                        )
                except (ValueError, KeyError) as e:
                    logger.warning(f"/oauth/installedLocations parse error: {e}")
            elif installed_resp:
                logger.info(
                    f"/oauth/installedLocations returned {installed_resp.status_code}: "
                    f"{installed_resp.text[:200]}"
                )
            else:
                logger.info(f"/oauth/installedLocations unreachable: {installed_err}")

            # Approach 2: /locations/search — may work if token has enough scope
            if not sub_accounts:
                locations_url = f"https://services.leadconnectorhq.com/locations/search?companyId={company_id}"
                sub_accounts = fetch_all_ghl_items(locations_url, headers_ghl, item_key='locations')
                if sub_accounts:
                    primary_location_id = sub_accounts[0].get('id')
                    logger.info(
                        f"/locations/search discovered {len(sub_accounts)} location(s). "
                        f"Primary set to: {primary_location_id}"
                    )
                else:
                    logger.warning(
                        f"All location discovery failed for companyId={company_id}. "
                        f"Will use companyId fallback."
                    )
        elif primary_location_id:
            # Location-scoped token — fetch this single location's details
            loc_resp, loc_err = _ghl_api_call(
                'GET', f"https://services.leadconnectorhq.com/locations/{primary_location_id}",
                headers=headers_ghl, timeout=10, label=f"/locations/{primary_location_id}"
            )
            if loc_resp and loc_resp.ok:
                try:
                    loc_data = loc_resp.json().get('location', loc_resp.json())
                    sub_accounts = [{
                        'id': loc_data.get('id', primary_location_id),
                        'name': loc_data.get('name', user_name or 'Primary Location'),
                        'timezone': loc_data.get('timezone'),
                    }]
                except (ValueError, KeyError):
                    logger.warning(f"/locations/{primary_location_id} returned unparseable response")
            else:
                logger.info(
                    f"/locations/{primary_location_id} returned "
                    f"{loc_resp.status_code if loc_resp else loc_err} — using token fallback"
                )

        num_subs = len(sub_accounts)
        logger.info(f"Step 4 complete: {num_subs} locations fetched for {user_email}")

        # Fallback: if API returned 0 but we have locationId from the token,
        # synthesize a minimal entry so onboarding still works.
        if num_subs == 0 and primary_location_id:
            using_location_fallback = True
            logger.warning(
                f"Location API returned 0 results but token has locationId={primary_location_id}. "
                f"Using token-based fallback."
            )
            sub_accounts = [{'id': primary_location_id,
                             'name': user_name or 'Primary Location',
                             'timezone': None}]
            num_subs = 1

        # Last resort: no locationId AND no locations discovered, but we have
        # companyId. Use companyId as the location_id so the user at least gets
        # a subscriber row and can log in. Admin alert sent so we can fix it.
        if num_subs == 0 and not primary_location_id and company_id:
            using_location_fallback = True
            primary_location_id = company_id
            logger.warning(
                f"NO locationId from token and ALL location discovery failed. "
                f"Using companyId={company_id} as location_id fallback for {user_email}."
            )
            sub_accounts = [{'id': company_id,
                             'name': user_name or 'Primary Location',
                             'timezone': None}]
            num_subs = 1

            # Alert admin — this user needs manual location resolution
            try:
                save_persistent_alert(
                    email=ADMIN_EMAILS[0] if ADMIN_EMAILS else "admin",
                    alert_type="company_id_fallback",
                    title="OAuth: CompanyID Used as LocationID",
                    message=(
                        f"User {user_email} installed via marketplace but GHL returned NO "
                        f"locationId. companyId={company_id} was used as a fallback. "
                        f"This user needs their location_id corrected once their real "
                        f"GHL location is identified."
                    ),
                    severity="warning",
                    location_id=company_id,
                )
            except Exception:
                pass

        # ── Step 5-6: Determine tier and primary location ─────────────────────
        # Agency owners: FREE — no subscription needed. They just download to get
        # on all their agents' GHL sidebars. Agents pay for their own plans.
        # Agency owners get agency_billing row; individuals get subscribers row.
        if is_agency_owner:
            plan_tier = 'agency_owner'
            use_agency_flow = True
        elif is_website_user:
            plan_tier = current_user.subscription_tier or 'individual'
            use_agency_flow = False
        else:
            plan_tier = 'individual'
            use_agency_flow = False

        primary_sub = next((s for s in sub_accounts if s['id'] == primary_location_id), None)
        primary_name = primary_sub.get('name', 'Unknown Location') if primary_sub else user_name
        primary_timezone = primary_sub.get('timezone', None) if primary_sub else None

        logger.info(
            f"Step 5-6 complete: tier={plan_tier}, agency_flow={use_agency_flow}, "
            f"primary_location={primary_name}"
        )

        # ── Step 7: Database operations ───────────────────────────────────────
        conn = get_db_connection_with_retry(max_attempts=3)
        if not conn:
            logger.error("OAuth callback: DB connection failed after 3 retries — cannot complete onboarding")
            flash("Database temporarily unavailable. Please try connecting again in a few minutes.", "danger")
            return redirect(url_for('public.home'))

        locations_to_provision = []
        try:
            cur = conn.cursor()

            # A. Agency owner: insert/upsert agency_billing row
            logger.info(
                f"Step 7a: use_agency_flow={use_agency_flow}, is_website_user={is_website_user}, "
                f"primary_location_id={primary_location_id}"
            )
            if use_agency_flow:
                # Agency owners are FREE — no seat caps, no subscription required.
                # They download to appear on agents' GHL sidebars. Agents pay individually.
                max_seats = 9999
                active_seats = 0
                app_type = 'private' if is_private_app else ('website' if is_website_user else 'marketplace')

                # For agency owners with no locationId, use companyId as location_id
                # so they can log in and use the dashboard
                agency_location_id = primary_location_id or company_id

                cur.execute("""
                    INSERT INTO agency_billing (
                        agency_email, location_id, full_name, subscription_tier,
                        max_seats, active_seats, access_token, refresh_token,
                        token_expires_at, timezone, crm_user_id, crm_email,
                        oauth_app_type,
                        company_id, company_name, company_owner_name,
                        company_owner_email, company_owner_phone,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        NOW() + interval '%s seconds', %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        NOW(), NOW()
                    )
                    ON CONFLICT (agency_email) DO UPDATE SET
                        location_id = COALESCE(EXCLUDED.location_id, agency_billing.location_id),
                        access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        token_expires_at = EXCLUDED.token_expires_at,
                        crm_user_id = COALESCE(EXCLUDED.crm_user_id, agency_billing.crm_user_id),
                        crm_email = EXCLUDED.crm_email,
                        oauth_app_type = EXCLUDED.oauth_app_type,
                        company_id = COALESCE(EXCLUDED.company_id, agency_billing.company_id),
                        company_name = COALESCE(EXCLUDED.company_name, agency_billing.company_name),
                        company_owner_name = COALESCE(EXCLUDED.company_owner_name, agency_billing.company_owner_name),
                        company_owner_email = COALESCE(EXCLUDED.company_owner_email, agency_billing.company_owner_email),
                        company_owner_phone = COALESCE(EXCLUDED.company_owner_phone, agency_billing.company_owner_phone),
                        updated_at = NOW()
                """, (
                    user_email, agency_location_id, primary_name, plan_tier,
                    max_seats, active_seats, enc_access_token, enc_refresh_token,
                    expires_in, primary_timezone or 'America/Chicago', me_data.get('id'),
                    crm_email_resolved, app_type,
                    company_metadata.get('company_id') or company_id,
                    company_metadata.get('company_name'),
                    company_metadata.get('company_owner_name'),
                    company_metadata.get('company_owner_email'),
                    company_metadata.get('company_owner_phone'),
                ))

                # Also upsert subscribers row so all operational code works
                # (dialer, voice, webhooks, token refresh all query subscribers)
                cur.execute("""
                    INSERT INTO subscribers (
                        email, location_id, full_name, role,
                        subscription_tier, access_token, refresh_token,
                        token_expires_at, timezone, crm_user_id, crm_email,
                        oauth_app_type, company_id,
                        created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        NOW() + interval '%s seconds', %s, %s, %s, %s, %s,
                        NOW(), NOW()
                    )
                    ON CONFLICT (email) DO UPDATE SET
                        location_id = COALESCE(EXCLUDED.location_id, subscribers.location_id),
                        role = 'agency_owner',
                        access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        token_expires_at = EXCLUDED.token_expires_at,
                        crm_user_id = COALESCE(EXCLUDED.crm_user_id, subscribers.crm_user_id),
                        crm_email = EXCLUDED.crm_email,
                        oauth_app_type = EXCLUDED.oauth_app_type,
                        company_id = COALESCE(EXCLUDED.company_id, subscribers.company_id),
                        updated_at = NOW()
                """, (
                    user_email, agency_location_id, primary_name, 'agency_owner',
                    plan_tier, enc_access_token, enc_refresh_token,
                    expires_in, primary_timezone or 'America/Chicago', me_data.get('id'),
                    crm_email_resolved, app_type,
                    company_metadata.get('company_id') or company_id,
                ))
                logger.info(f"Agency owner {user_email} synced to both agency_billing and subscribers")

            # B. Reconnect/reinstall sync: update OAuth tokens on existing rows.
            # Look up by location_id (PK) first — this is the definitive match.
            # Avoids PK collision when email owns a DIFFERENT location.
            app_type = 'private' if is_private_app else ('website' if is_website_user else 'marketplace')
            sync_role = 'agency_owner' if use_agency_flow else None

            existing_by_location = None
            if primary_location_id:
                cur.execute(
                    "SELECT location_id, email FROM subscribers WHERE location_id = %s",
                    (primary_location_id,)
                )
                existing_by_location = cur.fetchone()

            if existing_by_location:
                # Location already has a subscriber row — update tokens + crm_email.
                # Never overwrite email (login identity) with CRM email.
                #
                # CRITICAL: Correct user_email to the subscriber's actual login email.
                # When a non-logged-in user (marketplace install) connects OAuth, user_email
                # is set from GHL's /users/ API (the CRM email, e.g. mitchvandusenlife@gmail.com).
                # But the subscriber's login email may differ (e.g. mitchell_vandusen@hotmail.com).
                # If we don't correct this, Step 9 calls User.get(crm_email) and may find a
                # stale temp_* row from Stripe checkout, logging the user into the wrong account.
                existing_login_email = existing_by_location['email']
                if existing_login_email and existing_login_email != user_email:
                    logger.info(
                        f"Correcting user_email from CRM email {user_email!r} to "
                        f"subscriber's login email {existing_login_email!r} "
                        f"(existing subscriber found by location_id={primary_location_id})"
                    )
                    user_email = existing_login_email

                cur.execute("""
                    UPDATE subscribers
                    SET crm_email = %s,
                        access_token = %s,
                        refresh_token = %s,
                        token_expires_at = NOW() + interval '%s seconds',
                        crm_user_id = COALESCE(%s, crm_user_id),
                        oauth_app_type = %s,
                        role = COALESCE(%s, role),
                        parent_agency_email = COALESCE(%s, parent_agency_email),
                        company_id = COALESCE(%s, company_id),
                        onboarding_status = CASE
                            WHEN onboarding_status IN ('pending', 'invited') THEN 'claimed'
                            ELSE onboarding_status
                        END,
                        updated_at = NOW()
                    WHERE location_id = %s
                """, (
                    crm_email_resolved, enc_access_token, enc_refresh_token,
                    expires_in, me_data.get('id'), app_type,
                    sync_role, user_email if use_agency_flow else None,
                    company_id,
                    primary_location_id
                ))
                logger.info(
                    f"Synced OAuth tokens for existing location {primary_location_id} "
                    f"(login: {user_email}, crm_email: {crm_email_resolved}, app_type={app_type})"
                )

                # Clean up any orphaned temp_* rows that share the CRM email.
                # These are created by Stripe checkout before OAuth completes. Now that
                # we've identified the real subscriber, the temp row is no longer needed
                # and could cause User.get(crm_email) to return the wrong (token-less) account.
                if crm_email_resolved and crm_email_resolved != user_email:
                    cur.execute(
                        "DELETE FROM subscribers WHERE email = %s AND location_id LIKE 'temp_%%'",
                        (crm_email_resolved,)
                    )
                    deleted = cur.rowcount
                    if deleted:
                        logger.info(
                            f"Cleaned up {deleted} orphaned temp account(s) for CRM email "
                            f"{crm_email_resolved!r} (real subscriber: {user_email!r})"
                        )
            else:
                # Location doesn't exist yet — check if email owns a different location
                cur.execute(
                    "SELECT location_id FROM subscribers WHERE email = %s",
                    (user_email,)
                )
                existing_by_email = cur.fetchone()

                if existing_by_email and primary_location_id:
                    existing_loc = existing_by_email['location_id']
                    # Email owns a different location — update tokens + crm_email
                    # but do NOT change the PK. New location provisioned in Step 7C.
                    cur.execute("""
                        UPDATE subscribers
                        SET crm_email = %s,
                            access_token = %s,
                            refresh_token = %s,
                            token_expires_at = NOW() + interval '%s seconds',
                            crm_user_id = COALESCE(%s, crm_user_id),
                            oauth_app_type = %s,
                            updated_at = NOW()
                        WHERE email = %s
                    """, (
                        crm_email_resolved, enc_access_token, enc_refresh_token,
                        expires_in, me_data.get('id'), app_type,
                        user_email
                    ))
                    logger.info(
                        f"Updated tokens for {user_email}'s existing location {existing_loc}. "
                        f"CRM email: {crm_email_resolved}. "
                        f"New location {primary_location_id} will be provisioned in Step 7C."
                    )

            # existing_row: controls whether Step 7C skips the primary location.
            # Only set when the location_id already has a row (already updated above).
            existing_row = existing_by_location

            # C. Provision subscriber rows
            if use_agency_flow and not using_location_fallback:
                locations_to_provision = [s for s in sub_accounts if s['id'] != primary_location_id]
            else:
                locations_to_provision = [s for s in sub_accounts if s['id'] == primary_location_id]

            if using_location_fallback and use_agency_flow:
                logger.warning(
                    f"Agency owner {user_email} in FALLBACK MODE: only provisioning primary "
                    f"location {primary_location_id}. Sub-accounts added once locations.readonly approved."
                )

            if existing_row and primary_location_id:
                locations_to_provision = [s for s in locations_to_provision
                                          if s['id'] != primary_location_id]

            logger.info(
                f"Step 7c: Provisioning {len(locations_to_provision)} NEW subscriber rows "
                f"(agency_flow={use_agency_flow}, fallback={using_location_fallback}, "
                f"total_ghl_locations={num_subs})"
            )

            # Auto-link: if this is an individual install with a companyId,
            # check if an agency with this companyId exists and auto-set parent_agency_email.
            auto_linked_agency_email = None
            if not use_agency_flow and company_id:
                from db import get_agency_by_company_id
                agency_row = get_agency_by_company_id(company_id)
                if agency_row:
                    auto_linked_agency_email = agency_row.get('agency_email')
                    logger.info(
                        f"Auto-linking individual {user_email} to agency "
                        f"{auto_linked_agency_email} via companyId={company_id}"
                    )

            for sub in locations_to_provision:
                sub_id = sub['id']
                sub_name = sub.get('name', 'Unknown Location')
                sub_timezone = sub.get('timezone')

                is_owner_location = (sub_id == primary_location_id)
                if use_agency_flow and is_owner_location:
                    role = 'agency_owner'
                elif use_agency_flow:
                    role = 'agency_sub_account_user'
                else:
                    role = 'individual'
                parent_agency_email = user_email if use_agency_flow else auto_linked_agency_email

                cur.execute("""
                    INSERT INTO subscribers (
                        location_id, email, crm_email, full_name, role,
                        subscription_tier, parent_agency_email, company_id,
                        access_token, refresh_token,
                        token_expires_at, timezone, crm_user_id,
                        onboarding_status, oauth_app_type, created_at, updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        NOW() + interval '%s seconds',
                        %s, %s, %s, %s, NOW(), NOW()
                    )
                    ON CONFLICT (location_id) DO UPDATE SET
                        crm_email = EXCLUDED.crm_email,
                        access_token = EXCLUDED.access_token,
                        refresh_token = EXCLUDED.refresh_token,
                        token_expires_at = EXCLUDED.token_expires_at,
                        crm_user_id = COALESCE(EXCLUDED.crm_user_id, subscribers.crm_user_id),
                        oauth_app_type = EXCLUDED.oauth_app_type,
                        company_id = COALESCE(EXCLUDED.company_id, subscribers.company_id),
                        parent_agency_email = COALESCE(EXCLUDED.parent_agency_email, subscribers.parent_agency_email),
                        updated_at = NOW()
                """, (
                    sub_id, user_email, crm_email_resolved, sub_name, role,
                    plan_tier, parent_agency_email, company_id,
                    enc_access_token, enc_refresh_token,
                    expires_in,
                    sub_timezone or 'America/Chicago', me_data.get('id'),
                    'pending', app_type
                ))

            conn.commit()
            logger.info(
                f"Step 7 complete: Onboarded {user_email} (tier={plan_tier}, "
                f"agency_flow={use_agency_flow}) — provisioned {len(locations_to_provision)} "
                f"locations out of {num_subs} total in GHL."
            )

        except Exception as e:
            conn.rollback()
            logger.error(f"Database onboarding error for {user_email}: {e}", exc_info=True)
            flash("Error completing setup. Please contact support.", "danger")
            return redirect(url_for('public.home'))
        finally:
            cur.close()
            return_db_connection(conn)

        # ── Step 8: Post-onboarding (logging, alerts, email) ──────────────────

        # 8a. Log onboarding event to webhook_logs
        try:
            log_webhook_event(
                location_id=primary_location_id,
                event_type="oauth_onboarding",
                status="success",
                summary=f"OAuth onboarding complete for {user_email}",
                details={
                    "email": user_email,
                    "tier": plan_tier,
                    "agency_flow": use_agency_flow,
                    "locations_provisioned": len(locations_to_provision),
                    "total_ghl_locations": num_subs,
                    "fallback_mode": using_location_fallback,
                    "is_website_user": is_website_user,
                }
            )
        except Exception as e:
            logger.warning(f"Failed to log onboarding event: {e}")

        # 8a-2. Stamp install_completed_at for reminder scheduling
        try:
            _conn = get_db_connection_with_retry(2)
            if _conn:
                _cur = _conn.cursor()
                if use_agency_flow:
                    _cur.execute(
                        "UPDATE agency_billing SET install_completed_at = NOW() "
                        "WHERE agency_email = %s AND install_completed_at IS NULL",
                        (user_email,)
                    )
                else:
                    _cur.execute(
                        "UPDATE subscribers SET install_completed_at = NOW() "
                        "WHERE email = %s AND install_completed_at IS NULL",
                        (user_email,)
                    )
                _conn.commit()
                _cur.close()
                return_db_connection(_conn)
                logger.info(f"Install timestamp set for {user_email}")
        except Exception as e:
            logger.warning(f"Failed to set install_completed_at: {e}")

        # 8a-3. Mark marketplace install as OAuth-complete
        try:
            if primary_location_id:
                mark_install_oauth_complete(location_id=primary_location_id)
            if company_id:
                mark_install_oauth_complete(company_id=company_id)
            logger.info(
                f"Marketplace install marked OAuth-complete: "
                f"location={primary_location_id}, company={company_id}"
            )
        except Exception as e:
            logger.debug(f"mark_install_oauth_complete note: {e}")

        # 8b. Persistent alert if location fetch returned 0 results
        if using_location_fallback:
            try:
                alert_msg = (
                    "Your Lead Connector account connected successfully, but the "
                    "locations API returned no results. This may indicate a temporary "
                    "API issue or insufficient permissions. "
                    "Your primary location is active and the bot is operational. "
                )
                if use_agency_flow:
                    alert_msg += (
                        f"However, your sub-account locations could not be discovered. "
                        f"Try reconnecting via the Connect tab. "
                        f"Contact support if this persists."
                    )
                else:
                    alert_msg += "Try reconnecting via the Connect tab if issues persist."

                save_persistent_alert(
                    email=user_email,
                    alert_type="scope_locations_readonly",
                    title="Location Discovery Issue",
                    message=alert_msg,
                    severity="warning" if use_agency_flow else "info",
                    location_id=primary_location_id,
                )
                log_webhook_event(
                    location_id=primary_location_id,
                    event_type="scope_issue",
                    status="warning",
                    summary="locations API returned 0 results — using fallback",
                    details={"fallback": True, "agency_flow": use_agency_flow},
                )
            except Exception as e:
                logger.warning(f"Failed to save scope alert: {e}")

        # 8c. Welcome email — agency owners get a different email (no subscription step)
        try:
            domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
            dashboard_link = (
                f"{domain_url}/agency-dashboard" if use_agency_flow
                else f"{domain_url}/dashboard"
            )
            if use_agency_flow:
                welcome_html = _build_agency_owner_welcome_email(user_name, dashboard_link, domain_url)
            else:
                welcome_html = _build_welcome_email(user_name, dashboard_link, domain_url)
            email_subject = (
                "Your Agency Dashboard is Ready — InsuranceGrokBot" if use_agency_flow
                else "Welcome to InsuranceGrokBot — Your AI Assistant is Ready"
            )
            email_sent = send_email_via_api(
                to_email=user_email,
                subject=email_subject,
                html_body=welcome_html,
                text_body=(
                    f"Welcome to InsuranceGrokBot, {user_name}! "
                    f"Dashboard: {dashboard_link} | "
                    f"Support: {domain_url}/support"
                )
            )
            if email_sent:
                logger.info(f"Welcome email sent to {user_email}")
                log_webhook_event(primary_location_id, "welcome_email", "success",
                                  f"Welcome email sent to {user_email}")
            else:
                logger.warning(f"Welcome email failed for {user_email}")
                log_webhook_event(primary_location_id, "welcome_email", "error",
                                  f"Welcome email failed for {user_email}")
        except Exception as e:
            logger.warning(f"Welcome email error for {user_email}: {e}")

        # ── Step 9: Login and redirect ─────────────────────────────────────────
        # Session regeneration: clear session data before login to prevent
        # session fixation attacks (OAuth state/PKCE already consumed above).
        session.clear()

        user = User.get(user_email)
        if user:
            login_user(user)
            logger.info(f"Step 9 complete: Logged in {user_email}")
        else:
            logger.error(
                f"User.get({user_email}) returned None after successful DB commit — login failed"
            )
            if is_website_user:
                flash("Account created but login failed. Please log in manually.", "warning")
                return redirect(url_for('auth.login'))

        # Marketplace install → send to dashboard
        if not is_website_user:
            logger.info(f"=== MARKETPLACE INSTALL COMPLETE for {user_email} ===")
            try:
                log_webhook_event(
                    primary_location_id or "unknown", "oauth_complete", "success",
                    f"Marketplace install complete for {user_email} "
                    f"(tier={plan_tier}, user_type_used={token_user_type_used})"
                )
            except Exception:
                pass
            if user:
                flash("App installed successfully! Complete your dashboard setup to activate your bot.", "success")
                if use_agency_flow:
                    return redirect(url_for('agency.agency_dashboard'))
                return redirect(url_for('dashboard.dashboard'))
            else:
                flash("App installed! Please log in or create a password to access your dashboard.", "success")
                return redirect(url_for('auth.login'))

        # Website user flow → dashboard (same as marketplace)
        app_type_label = 'private' if is_private_app else 'website'
        logger.info(f"=== {app_type_label.upper()} APP OAUTH COMPLETE for {user_email} ===")
        try:
            log_webhook_event(
                primary_location_id or "unknown", "oauth_complete", "success",
                f"{app_type_label.capitalize()} app OAuth complete for {user_email} (tier={plan_tier})"
            )
        except Exception:
            pass
        if is_private_app:
            return redirect(url_for('oauth.oauth_loading'))
        flash("CRM connected successfully!", "success")
        if use_agency_flow:
            return redirect(url_for('agency.agency_dashboard'))
        return redirect(url_for('dashboard.dashboard'))

    except requests.RequestException as e:
        logger.error(f"OAuth network error: {e}", exc_info=True)
        try:
            log_webhook_event("oauth_global", "oauth_network_error", "error",
                              f"OAuth network error: {e}")
        except Exception:
            pass
        flash("Failed to connect to Lead Connector. Please try again.", "danger")
        return redirect(url_for('public.home'))
    except Exception as e:
        logger.error(f"Critical OAuth failure: {e}", exc_info=True)
        try:
            log_webhook_event("oauth_global", "oauth_critical_error", "error",
                              f"Critical OAuth failure: {e}")
        except Exception:
            pass
        flash("An unexpected error occurred. Please try again or contact support.", "danger")
        return redirect(url_for('public.home'))


@oauth_bp.route("/oauth/loading")
@login_required
def oauth_loading():
    """Loading screen shown after OAuth to visualize data gathering progress."""
    return render_template('oauth-loading.html')
