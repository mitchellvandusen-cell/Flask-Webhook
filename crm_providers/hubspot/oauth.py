# crm_providers/hubspot/oauth.py — HubSpot OAuth2 Flow
#
# Handles two distinct install scenarios:
#   1. Existing Omnisconn user connecting HubSpot as their CRM
#      (authenticated flow via /dashboard → Connect tab)
#   2. Fresh HubSpot marketplace install — user has zero Omnisconn account
#      (anonymous flow: HubSpot → /hubspot/oauth/initiate → callback → create subscriber)
#
# Routes:
#   GET  /hubspot/oauth/initiate       — redirect to HubSpot consent screen (works w/o login)
#   GET  /hubspot/oauth/callback       — exchange code, create/update subscriber, login
#   POST /hubspot/oauth/deauthorize    — HubSpot uninstall hook (clear tokens)

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

import requests
from flask import Blueprint, redirect, request, session, flash, url_for

from db import (
    get_db_connection,
    return_db_connection,
    update_crm_config_token,
)

logger = logging.getLogger(__name__)

hubspot_oauth_bp = Blueprint("hubspot_oauth", __name__)

HUBSPOT_AUTH_URL  = "https://app.hubspot.com/oauth/authorize"
HUBSPOT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
HUBSPOT_BASE      = "https://api.hubapi.com"

# Scopes must match app-hsmeta.json manifest exactly.
# Required: oauth, contacts R/W, deals R/W, timeline
# Optional: conversations R/W (HubSpot Conversations API)
HUBSPOT_SCOPES = [
    "oauth",
    "crm.objects.contacts.read",
    "crm.objects.contacts.write",
    "crm.objects.deals.read",
    "crm.objects.deals.write",
    "timeline",
    "conversations.read",
    "conversations.write",
]


def _client_id():
    return os.getenv("HUBSPOT_CLIENT_ID", "")

def _client_secret():
    return os.getenv("HUBSPOT_CLIENT_SECRET", "")

def _redirect_uri():
    domain = os.getenv("YOUR_DOMAIN", "http://localhost:8080").rstrip("/")
    return f"{domain}/hubspot/oauth/callback"


# ─── INITIATE ────────────────────────────────────────────────────────────────

@hubspot_oauth_bp.route("/hubspot/oauth/initiate")
def hubspot_oauth_initiate():
    """
    Redirect to HubSpot consent screen.
    Works for both:
      - Logged-in users reconnecting from the dashboard
      - Anonymous users arriving from the HubSpot marketplace install button
    """
    from flask_login import current_user

    cid = _client_id()
    if not cid:
        flash("HubSpot integration is not configured.", "error")
        return redirect("/dashboard" if current_user.is_authenticated else "/login")

    # State encodes who initiated so callback can route correctly.
    # Format: "new|<nonce>" for marketplace installs, "<location_id>|<nonce>" for reconnects.
    nonce = secrets.token_urlsafe(32)
    if current_user.is_authenticated:
        state = f"{current_user.location_id}|{nonce}"
    else:
        state = f"new|{nonce}"

    session["hubspot_oauth_state"] = state

    auth_url = f"{HUBSPOT_AUTH_URL}?{urlencode({
        'client_id':    cid,
        'redirect_uri': _redirect_uri(),
        'scope':        ' '.join(HUBSPOT_SCOPES),
        'state':        state,
    })}"
    logger.info("HubSpot OAuth initiated (authenticated=%s)", current_user.is_authenticated)
    return redirect(auth_url)


# ─── CALLBACK ────────────────────────────────────────────────────────────────

@hubspot_oauth_bp.route("/hubspot/oauth/callback")
def hubspot_oauth_callback():
    """
    Handle HubSpot OAuth callback.
    - Validates CSRF state
    - Exchanges code for tokens
    - Looks up subscriber by email or hub_id
    - Creates subscriber row if this is a fresh marketplace install
    - Logs the user in via Flask-Login
    """
    from flask_login import current_user, login_user
    from db import User

    # ── CSRF check ──────────────────────────────────────────────────────────
    state         = request.args.get("state", "")
    expected      = session.pop("hubspot_oauth_state", "")
    if not state or state != expected:
        logger.warning("HubSpot OAuth state mismatch")
        flash("OAuth verification failed. Please try again.", "error")
        return redirect("/login")

    error = request.args.get("error", "")
    if error:
        flash(f"HubSpot connection failed: {request.args.get('error_description', error)}", "error")
        return redirect("/login")

    code = request.args.get("code", "")
    if not code:
        flash("No authorization code received from HubSpot.", "error")
        return redirect("/login")

    # ── Token exchange ───────────────────────────────────────────────────────
    try:
        token_resp = requests.post(HUBSPOT_TOKEN_URL, data={
            "grant_type":    "authorization_code",
            "client_id":     _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri":  _redirect_uri(),
            "code":          code,
        }, timeout=20)

        if token_resp.status_code != 200:
            logger.error("HubSpot token exchange failed: %s %s",
                         token_resp.status_code, token_resp.text[:300])
            flash("Failed to connect HubSpot. Please try again.", "error")
            return redirect("/login")

        td            = token_resp.json()
        access_token  = td.get("access_token", "")
        refresh_token = td.get("refresh_token", "")
        expires_in    = td.get("expires_in", 21600)

    except requests.RequestException as e:
        logger.error("HubSpot token exchange network error: %s", e)
        flash("Network error connecting to HubSpot. Please try again.", "error")
        return redirect("/login")

    # ── Fetch token identity (email + hub_id) ────────────────────────────────
    hub_email = ""
    hub_id    = ""
    try:
        id_resp = requests.get(
            f"{HUBSPOT_BASE}/oauth/v1/access-tokens/{access_token}",
            timeout=10,
        )
        if id_resp.status_code == 200:
            id_data   = id_resp.json()
            hub_email = id_data.get("user", "")       # e.g. "jane@acme.com"
            hub_id    = str(id_data.get("hub_id", "")) # e.g. "245561638"
    except Exception as e:
        logger.warning("HubSpot identity fetch failed: %s", e)

    if not hub_id:
        # Fallback: /account-info/v3/details
        try:
            ai_resp = requests.get(
                f"{HUBSPOT_BASE}/account-info/v3/details",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if ai_resp.status_code == 200:
                hub_id = str(ai_resp.json().get("portalId", ""))
        except Exception:
            pass

    crm_payload = {
        "access_token":      access_token,
        "refresh_token":     refresh_token,
        "client_id":         _client_id(),
        "client_secret":     _client_secret(),
        "hub_id":            hub_id,
        "hub_email":         hub_email,
        "token_expires_at":  int(time.time()) + expires_in,
        "connected_at":      int(time.time()),
    }

    # ── Is this a reconnect from an existing logged-in user? ─────────────────
    is_reconnect = not state.startswith("new|")

    if is_reconnect and current_user.is_authenticated:
        _update_existing_subscriber(current_user.location_id, crm_payload)
        logger.info("HubSpot reconnected for location_id=%s hub_id=%s",
                    current_user.location_id, hub_id)
        flash("HubSpot connected successfully!", "success")
        return redirect("/dashboard")

    # ── Fresh marketplace install — find or create subscriber ────────────────
    subscriber_row = _find_subscriber(hub_email, hub_id)

    if subscriber_row:
        # Existing Omnisconn account — just update tokens and log in
        _update_existing_subscriber(subscriber_row["location_id"], crm_payload)
        user = User.get(subscriber_row["email"] or hub_email)
        if user:
            login_user(user, remember=True)
            logger.info("HubSpot marketplace reconnect — existing user hub_id=%s", hub_id)
            flash("HubSpot connected successfully!", "success")
            return redirect("/dashboard")

    # ── Brand new user — create subscriber row ───────────────────────────────
    if not hub_email:
        flash("Could not retrieve your HubSpot email. Please try again.", "error")
        return redirect("/login")

    location_id = f"hub_{hub_id}"
    _create_hubspot_subscriber(location_id, hub_email, hub_id, crm_payload)
    logger.info("New HubSpot subscriber created location_id=%s hub_id=%s email=%s",
                location_id, hub_id, hub_email)

    # Log the new user in immediately (no password yet — they set it on onboarding)
    user = User.get(hub_email)
    if user:
        login_user(user, remember=True)
        flash("HubSpot connected! Choose a plan to activate your account.", "success")
        return redirect("/checkout")

    flash("Account created. Please log in.", "success")
    return redirect(url_for("auth.login"))


# ─── DEAUTHORIZE (uninstall hook) ────────────────────────────────────────────

@hubspot_oauth_bp.route("/hubspot/oauth/deauthorize", methods=["POST"])
def hubspot_oauth_deauthorize():
    """
    HubSpot fires this when a portal uninstalls the app.
    Clear the stored tokens so we stop trying to use them.
    HubSpot signs the request body with the client secret (SHA-256 HMAC).
    """
    raw_body = request.get_data()

    # Verify signature — HubSpot uses SHA-256 HMAC of raw body
    sig_header = request.headers.get("X-HubSpot-Signature", "")
    if sig_header and _client_secret():
        expected_sig = hmac.new(
            _client_secret().encode(),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_sig, sig_header):
            logger.warning("HubSpot deauthorize: invalid signature")
            return {"error": "invalid signature"}, 401

    try:
        data    = json.loads(raw_body) if raw_body else {}
        hub_id  = str(data.get("portalId", ""))
        user_id = data.get("userId", "")
    except (json.JSONDecodeError, Exception):
        data, hub_id, user_id = {}, "", ""

    if hub_id:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            # Null out the HubSpot tokens — keep the subscriber row for billing history
            cur.execute("""
                UPDATE subscribers
                SET crm_config = crm_config - 'access_token' - 'refresh_token'
                                           || %s::jsonb,
                    updated_at = NOW()
                WHERE crm_config->>'hub_id' = %s
            """, (json.dumps({"disconnected_at": int(time.time())}), hub_id))
            conn.commit()
            logger.info("HubSpot deauthorize: cleared tokens for hub_id=%s user_id=%s",
                        hub_id, user_id)
        except Exception as e:
            conn.rollback()
            logger.error("HubSpot deauthorize DB error: %s", e)
        finally:
            return_db_connection(conn)

    return {"status": "ok"}, 200


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _find_subscriber(email: str, hub_id: str) -> dict | None:
    """Find subscriber by email or hub_id in crm_config."""
    if not email and not hub_id:
        return None
    conn = get_db_connection()
    try:
        from psycopg2.extras import RealDictCursor
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT location_id, email, crm_config
            FROM subscribers
            WHERE (email = %s AND %s <> '')
               OR (crm_config->>'hub_id' = %s AND %s <> '')
            LIMIT 1
        """, (email, email, hub_id, hub_id))
        return cur.fetchone()
    except Exception as e:
        logger.error("_find_subscriber error: %s", e)
        return None
    finally:
        return_db_connection(conn)


def _update_existing_subscriber(location_id: str, crm_payload: dict):
    """Merge crm_payload into existing subscriber's crm_config."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers
            SET crm_type   = 'hubspot',
                crm_config = COALESCE(crm_config, '{}'::jsonb) || %s::jsonb,
                updated_at = NOW()
            WHERE location_id = %s
        """, (json.dumps(crm_payload), location_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error("_update_existing_subscriber error: %s", e)
    finally:
        return_db_connection(conn)


def _create_hubspot_subscriber(location_id: str, email: str, hub_id: str,
                                crm_payload: dict):
    """
    Create a minimal subscriber row for a fresh HubSpot marketplace install.
    The user will be prompted to set a password on their first dashboard visit.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO subscribers (
                location_id, email, full_name, role,
                subscription_tier, crm_type, crm_config,
                onboarding_status, timezone, sms_send_via,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, 'individual',
                'individual', 'hubspot', %s::jsonb,
                'pending', 'America/Chicago', 'ghl',
                NOW(), NOW()
            )
            ON CONFLICT (email) DO UPDATE SET
                crm_type   = 'hubspot',
                crm_config = COALESCE(subscribers.crm_config, '{}'::jsonb) || EXCLUDED.crm_config,
                updated_at = NOW()
        """, (
            location_id,
            email,
            email.split("@")[0],   # Use email prefix as placeholder name
            json.dumps(crm_payload),
        ))
        conn.commit()
        logger.info("Created HubSpot subscriber: location_id=%s email=%s", location_id, email)
    except Exception as e:
        conn.rollback()
        logger.error("_create_hubspot_subscriber error: %s", e)
    finally:
        return_db_connection(conn)


# ─── TOKEN REFRESH ───────────────────────────────────────────────────────────

def refresh_hubspot_token(subscriber: dict) -> dict:
    """
    Refresh a HubSpot OAuth token.
    Returns dict with {access_token, refresh_token, expires_in} or {} on failure.
    """
    crm_config    = subscriber.get("crm_config") or {}
    refresh_token = crm_config.get("refresh_token", "")
    cid           = crm_config.get("client_id", "") or _client_id()
    csecret       = crm_config.get("client_secret", "") or _client_secret()

    if not all([refresh_token, cid, csecret]):
        logger.warning("HubSpot token refresh: missing credentials for %s",
                       subscriber.get("location_id"))
        return {}

    try:
        resp = requests.post(HUBSPOT_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     cid,
            "client_secret": csecret,
        }, timeout=20)

        if resp.status_code != 200:
            logger.error("HubSpot token refresh failed: %s %s",
                         resp.status_code, resp.text[:300])
            return {}

        data        = resp.json()
        new_access  = data.get("access_token", "")
        new_refresh = data.get("refresh_token", refresh_token)
        expires_in  = data.get("expires_in", 21600)

        if not new_access:
            return {}

        location_id = subscriber.get("location_id", "")
        if location_id:
            conn = get_db_connection()
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE subscribers
                    SET crm_config = crm_config || %s::jsonb,
                        updated_at = NOW()
                    WHERE location_id = %s
                """, (
                    json.dumps({
                        "access_token":     new_access,
                        "refresh_token":    new_refresh,
                        "token_expires_at": int(time.time()) + expires_in,
                    }),
                    location_id,
                ))
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error("Failed to persist refreshed HubSpot token: %s", e)
            finally:
                return_db_connection(conn)

        logger.info("HubSpot token refreshed for %s", location_id)
        return {"access_token": new_access, "refresh_token": new_refresh,
                "expires_in": expires_in}

    except requests.RequestException as e:
        logger.error("HubSpot token refresh network error: %s", e)
        return {}
