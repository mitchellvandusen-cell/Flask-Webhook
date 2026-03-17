# crm_providers/hubspot/oauth.py — HubSpot OAuth2 Flow
#
# Provides the OAuth2 authorization code flow for HubSpot:
#   GET  /hubspot/oauth/initiate  — redirect to HubSpot consent screen
#   GET  /hubspot/oauth/callback  — exchange code for tokens, store in DB
#
# Token refresh is handled by the HubSpotProvider.refresh_token() method.
# HubSpot OAuth tokens expire every 6 hours (vs GHL's 24h).

import hashlib
import hmac
import logging
import os
import secrets
import time

import requests
from flask import Blueprint, redirect, request, session, flash, url_for

from db import (
    get_db_connection,
    return_db_connection,
    get_subscriber_info_hybrid,
    update_crm_config_token,
)

logger = logging.getLogger(__name__)

hubspot_oauth_bp = Blueprint("hubspot_oauth", __name__)

HUBSPOT_AUTH_URL = "https://app.hubspot.com/oauth/authorize"
HUBSPOT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
HUBSPOT_BASE = "https://api.hubapi.com"

# Required scopes for full IGB integration
HUBSPOT_SCOPES = [
    "crm.objects.contacts.read",
    "crm.objects.contacts.write",
    "crm.objects.deals.read",
    "crm.objects.deals.write",
    "crm.objects.communications.read",
    "crm.objects.communications.write",
    "crm.objects.meetings.write",
    "timeline",
]


def _get_hubspot_client_id():
    return os.getenv("HUBSPOT_CLIENT_ID", "")


def _get_hubspot_client_secret():
    return os.getenv("HUBSPOT_CLIENT_SECRET", "")


def _get_redirect_uri():
    domain = os.getenv("YOUR_DOMAIN", "http://localhost:8080").rstrip("/")
    return f"{domain}/hubspot/oauth/callback"


@hubspot_oauth_bp.route("/hubspot/oauth/initiate")
def hubspot_oauth_initiate():
    """Start HubSpot OAuth flow — redirect to HubSpot consent screen."""
    from flask_login import current_user

    if not current_user.is_authenticated:
        flash("Please log in first.", "error")
        return redirect("/login")

    client_id = _get_hubspot_client_id()
    if not client_id:
        flash("HubSpot integration is not configured. Please contact support.", "error")
        return redirect("/dashboard")

    # CSRF protection via cryptographic state parameter
    state_nonce = secrets.token_urlsafe(32)
    state_data = f"{current_user.location_id}|{state_nonce}|{int(time.time())}"
    session["hubspot_oauth_state"] = state_data

    scope_str = " ".join(HUBSPOT_SCOPES)
    redirect_uri = _get_redirect_uri()

    auth_url = (
        f"{HUBSPOT_AUTH_URL}"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={scope_str}"
        f"&state={state_data}"
    )

    logger.info(f"HubSpot OAuth initiated for {current_user.location_id}")
    return redirect(auth_url)


@hubspot_oauth_bp.route("/hubspot/oauth/callback")
def hubspot_oauth_callback():
    """Handle HubSpot OAuth callback — exchange code for tokens."""
    from flask_login import current_user

    if not current_user.is_authenticated:
        flash("Session expired. Please log in again.", "error")
        return redirect("/login")

    # Validate CSRF state
    state = request.args.get("state", "")
    expected_state = session.pop("hubspot_oauth_state", "")
    if not state or state != expected_state:
        logger.warning(f"HubSpot OAuth state mismatch for {current_user.location_id}")
        flash("OAuth verification failed. Please try again.", "error")
        return redirect("/dashboard")

    # Check for errors
    error = request.args.get("error", "")
    if error:
        error_desc = request.args.get("error_description", "Unknown error")
        logger.error(f"HubSpot OAuth error: {error} — {error_desc}")
        flash(f"HubSpot connection failed: {error_desc}", "error")
        return redirect("/dashboard")

    # Exchange authorization code for tokens
    code = request.args.get("code", "")
    if not code:
        flash("No authorization code received from HubSpot.", "error")
        return redirect("/dashboard")

    client_id = _get_hubspot_client_id()
    client_secret = _get_hubspot_client_secret()
    redirect_uri = _get_redirect_uri()

    try:
        token_resp = requests.post(HUBSPOT_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        }, timeout=20)

        if token_resp.status_code != 200:
            logger.error(f"HubSpot token exchange failed: {token_resp.status_code} "
                        f"{token_resp.text[:500]}")
            flash("Failed to connect HubSpot. Please try again.", "error")
            return redirect("/dashboard")

        token_data = token_resp.json()
        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")
        expires_in = token_data.get("expires_in", 21600)  # Default 6 hours

        if not access_token:
            flash("No access token received from HubSpot.", "error")
            return redirect("/dashboard")

    except requests.RequestException as e:
        logger.error(f"HubSpot token exchange network error: {e}")
        flash("Network error connecting to HubSpot. Please try again.", "error")
        return redirect("/dashboard")

    # Fetch HubSpot portal info for identification
    hub_id = ""
    try:
        info_resp = requests.get(
            f"{HUBSPOT_BASE}/account-info/v3/details",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if info_resp.status_code == 200:
            hub_id = str(info_resp.json().get("portalId", ""))
    except Exception as e:
        logger.warning(f"Failed to fetch HubSpot portal info: {e}")

    # Store tokens in subscriber's crm_config JSONB
    location_id = current_user.location_id
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers SET
                crm_type = 'hubspot',
                crm_config = crm_config || %s::jsonb
            WHERE location_id = %s
        """, (
            _json_dumps({
                "access_token": access_token,
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
                "hub_id": hub_id,
                "token_expires_at": int(time.time()) + expires_in,
                "connected_at": int(time.time()),
            }),
            location_id,
        ))
        conn.commit()
        logger.info(f"HubSpot OAuth complete for {location_id} | hub_id={hub_id}")
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to store HubSpot tokens: {e}")
        flash("Failed to save HubSpot connection. Please try again.", "error")
        return redirect("/dashboard")
    finally:
        return_db_connection(conn)

    flash("HubSpot connected successfully!", "success")
    return redirect("/dashboard")


def refresh_hubspot_token(subscriber: dict) -> dict:
    """
    Refresh a HubSpot OAuth token.

    Args:
        subscriber: Full subscriber row from DB

    Returns:
        dict with {access_token, refresh_token, expires_in} or empty dict on failure.
    """
    crm_config = subscriber.get("crm_config") or {}
    refresh_token = crm_config.get("refresh_token", "")
    client_id = crm_config.get("client_id", "") or _get_hubspot_client_id()
    client_secret = crm_config.get("client_secret", "") or _get_hubspot_client_secret()

    if not all([refresh_token, client_id, client_secret]):
        logger.warning("HubSpot token refresh: missing credentials")
        return {}

    try:
        resp = requests.post(HUBSPOT_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }, timeout=20)

        if resp.status_code != 200:
            logger.error(f"HubSpot token refresh failed: {resp.status_code} {resp.text[:300]}")
            return {}

        data = resp.json()
        new_access = data.get("access_token", "")
        new_refresh = data.get("refresh_token", refresh_token)
        expires_in = data.get("expires_in", 21600)

        if not new_access:
            return {}

        # Persist new tokens to DB
        location_id = subscriber.get("location_id", "")
        if location_id:
            conn = get_db_connection()
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE subscribers SET
                        crm_config = crm_config || %s::jsonb
                    WHERE location_id = %s
                """, (
                    _json_dumps({
                        "access_token": new_access,
                        "refresh_token": new_refresh,
                        "token_expires_at": int(time.time()) + expires_in,
                    }),
                    location_id,
                ))
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"Failed to persist refreshed HubSpot token: {e}")
            finally:
                return_db_connection(conn)

        logger.info(f"HubSpot token refreshed for {location_id}")
        return {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_in": expires_in,
        }

    except requests.RequestException as e:
        logger.error(f"HubSpot token refresh network error: {e}")
        return {}


def _json_dumps(obj):
    """JSON serialize for psycopg2 JSONB parameter."""
    import json
    return json.dumps(obj)
