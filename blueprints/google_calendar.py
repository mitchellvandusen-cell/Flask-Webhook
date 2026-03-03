# blueprints/google_calendar.py — Google Calendar OAuth + Event Syncing
#
# Adds real Google Calendar event display to the dialer Calendar app.
# GHL's /calendars/events API only returns GHL-native appointments —
# it does NOT expose synced Google Calendar events. This blueprint
# connects directly to the Google Calendar API to fetch all events.

import os
import logging
import secrets
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from flask import (Blueprint, flash, redirect, url_for, session, request,
                   jsonify as flask_jsonify, render_template)
from flask_login import login_required, current_user

from db import (save_google_calendar_config, get_google_calendar_config,
                delete_google_calendar_config)

logger = logging.getLogger(__name__)

google_calendar_bp = Blueprint('google_calendar', __name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
SCOPES = "https://www.googleapis.com/auth/calendar.readonly"


# ── Private helpers ───────────────────────────────────────────────────────────

def _google_creds():
    """Return (client_id, client_secret, redirect_uri) from environment."""
    client_id = os.getenv("GOOGLE_CALENDAR_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET")
    redirect_uri = os.getenv(
        "GOOGLE_CALENDAR_REDIRECT_URI",
        "https://insurancegrokbot.click/google-calendar/callback"
    )
    return client_id, client_secret, redirect_uri


def _refresh_google_token(location_id: str, config: dict):
    """Refresh an expired Google OAuth token. Returns new access_token or None."""
    client_id, client_secret, _ = _google_creds()
    refresh_token = config.get("refresh_token")
    if not all([client_id, client_secret, refresh_token]):
        return None
    try:
        resp = requests.post(GOOGLE_TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }, timeout=10)
        if resp.status_code == 200:
            tok = resp.json()
            new_access = tok["access_token"]
            expires_in = tok.get("expires_in", 3600)
            expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
            config["access_token"] = new_access
            config["token_expiry"] = expiry
            save_google_calendar_config(location_id, config)
            logger.info(f"Google Calendar token refreshed for {location_id}")
            return new_access
        else:
            logger.error(f"Google token refresh failed ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Google Calendar token refresh failed for {location_id}: {e}")
    return None


def _get_valid_google_token(location_id: str):
    """Return a valid Google Calendar access token, refreshing if needed."""
    config = get_google_calendar_config(location_id)
    if not config or not config.get("access_token"):
        return None, config

    # Check if token is expired (refresh 5 min before expiry)
    expiry_str = config.get("token_expiry")
    if expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= expiry - timedelta(minutes=5):
                new_token = _refresh_google_token(location_id, config)
                if new_token:
                    config["access_token"] = new_token
                    return new_token, config
                return None, config
        except (ValueError, TypeError):
            pass

    return config["access_token"], config


# ── OAuth connect / callback / disconnect ─────────────────────────────────────

@google_calendar_bp.route("/google-calendar/connect")
@login_required
def google_calendar_connect():
    """Show scope permission screen before Google OAuth redirect."""
    client_id, _, redirect_uri = _google_creds()
    if not client_id:
        flash("Google Calendar integration is not configured. Contact support.", "error")
        return redirect(url_for("dashboard"))

    # Build the Google OAuth URL for the consent page's "Continue" button
    state = secrets.token_urlsafe(16)
    session["google_calendar_oauth_state"] = state
    params = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    })
    authorize_url = f"{GOOGLE_AUTH_URL}?{params}"

    return render_template("google_calendar_consent.html", authorize_url=authorize_url)


@google_calendar_bp.route("/google-calendar/callback")
@login_required
def google_calendar_callback():
    """Handle Google OAuth callback — saves tokens to DB."""
    code = request.args.get("code")
    state = request.args.get("state")
    stored_state = session.pop("google_calendar_oauth_state", None)

    if not code or state != stored_state:
        flash("Google Calendar authorization failed or was cancelled.", "error")
        return redirect(url_for("dashboard"))

    client_id, client_secret, redirect_uri = _google_creds()

    # Exchange code for tokens
    try:
        token_resp = requests.post(GOOGLE_TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        }, timeout=10)
        token_data = token_resp.json()
    except Exception as e:
        logger.error(f"Google Calendar token exchange failed: {e}")
        flash("Could not connect to Google Calendar. Try again.", "error")
        return redirect(url_for("dashboard"))

    if "access_token" not in token_data:
        logger.error(f"Google Calendar token error: {token_data}")
        flash("Google Calendar authorization failed. Please try again.", "error")
        return redirect(url_for("dashboard"))

    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in", 3600)
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    # Fetch user's email from Google
    google_email = ""
    try:
        userinfo = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        ).json()
        google_email = userinfo.get("email", "")
    except Exception:
        pass

    # Save config
    config = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_expiry": expiry,
        "connected": True,
        "email": google_email,
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    save_google_calendar_config(current_user.location_id, config)

    flash(f"Google Calendar connected{(' as ' + google_email) if google_email else ''}!", "success")
    return redirect(url_for("dashboard"))


@google_calendar_bp.route("/google-calendar/disconnect")
@login_required
def google_calendar_disconnect():
    """Disconnect Google Calendar integration."""
    delete_google_calendar_config(current_user.location_id)
    flash("Google Calendar disconnected.", "info")
    return redirect(url_for("dashboard"))


# ── API endpoints ─────────────────────────────────────────────────────────────

@google_calendar_bp.route("/api/google-calendar/status")
@login_required
def google_calendar_status():
    """Return Google Calendar connection status."""
    config = get_google_calendar_config(current_user.location_id)
    connected = bool(config and config.get("connected") and config.get("access_token"))
    return flask_jsonify({
        "connected": connected,
        "email": config.get("email", "") if connected else "",
    })


@google_calendar_bp.route("/api/google-calendar/calendars")
@login_required
def google_calendar_list():
    """List the user's Google Calendars."""
    location_id = current_user.location_id
    if not location_id:
        return flask_jsonify({"error": "No location configured"}), 400

    access_token, config = _get_valid_google_token(location_id)
    if not access_token:
        return flask_jsonify({"error": "Google Calendar not connected", "connected": False}), 401

    try:
        resp = requests.get(
            f"{GOOGLE_CALENDAR_API}/users/me/calendarList",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"minAccessRole": "reader"},
            timeout=15,
        )
        if resp.status_code in (401, 403):
            # Try refresh once
            new_token = _refresh_google_token(location_id, config)
            if new_token:
                resp = requests.get(
                    f"{GOOGLE_CALENDAR_API}/users/me/calendarList",
                    headers={"Authorization": f"Bearer {new_token}"},
                    params={"minAccessRole": "reader"},
                    timeout=15,
                )
            if resp.status_code in (401, 403):
                return flask_jsonify({"error": "Google Calendar token expired. Please reconnect."}), 401

        resp.raise_for_status()
        data = resp.json()

        calendars = []
        for cal in data.get("items", []):
            calendars.append({
                "id": cal.get("id"),
                "name": cal.get("summary", "Unnamed Calendar"),
                "primary": cal.get("primary", False),
                "backgroundColor": cal.get("backgroundColor", "#4285F4"),
            })

        return flask_jsonify({"calendars": calendars})

    except requests.exceptions.Timeout:
        return flask_jsonify({"error": "Google Calendar timed out"}), 504
    except requests.exceptions.RequestException as e:
        logger.error(f"Google Calendar list failed for {location_id}: {e}")
        return flask_jsonify({"error": "Failed to fetch Google calendars"}), 500


@google_calendar_bp.route("/api/google-calendar/events")
@login_required
def google_calendar_events():
    """
    Fetch events from Google Calendar for a date range.
    Query params: calendar_id (default: 'primary'), start (ISO date), end (ISO date)
    """
    location_id = current_user.location_id
    if not location_id:
        return flask_jsonify({"error": "No location configured"}), 400

    access_token, config = _get_valid_google_token(location_id)
    if not access_token:
        return flask_jsonify({"error": "Google Calendar not connected", "connected": False}), 401

    cal_id = request.args.get("calendar_id", "primary")
    start_date = request.args.get("start")
    end_date = request.args.get("end")

    from zoneinfo import ZoneInfo
    tz_str = getattr(current_user, 'timezone', None) or "America/Chicago"
    local_tz = ZoneInfo(tz_str)
    now = datetime.now(local_tz)

    if start_date:
        start_dt = datetime.fromisoformat(start_date + "T00:00:00").replace(tzinfo=local_tz)
    else:
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    if end_date:
        end_dt = datetime.fromisoformat(end_date + "T23:59:59").replace(tzinfo=local_tz)
    else:
        next_month = start_dt.replace(day=28) + timedelta(days=4)
        end_dt = next_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)

    params = {
        "timeMin": start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "timeMax": end_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 250,
        "timeZone": tz_str,
    }

    try:
        resp = requests.get(
            f"{GOOGLE_CALENDAR_API}/calendars/{cal_id}/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=20,
        )
        if resp.status_code in (401, 403):
            new_token = _refresh_google_token(location_id, config)
            if new_token:
                resp = requests.get(
                    f"{GOOGLE_CALENDAR_API}/calendars/{cal_id}/events",
                    headers={"Authorization": f"Bearer {new_token}"},
                    params=params,
                    timeout=20,
                )
            if resp.status_code in (401, 403):
                return flask_jsonify({"error": "Google Calendar token expired. Please reconnect."}), 401

        resp.raise_for_status()
        data = resp.json()

        events = []
        for ev in data.get("items", []):
            if ev.get("status") == "cancelled":
                continue

            # Google Calendar events can be all-day (date) or timed (dateTime)
            start_info = ev.get("start", {})
            end_info = ev.get("end", {})
            start_time = start_info.get("dateTime") or start_info.get("date")
            end_time = end_info.get("dateTime") or end_info.get("date")
            is_all_day = "date" in start_info and "dateTime" not in start_info

            events.append({
                "id": ev.get("id"),
                "title": ev.get("summary") or "No Title",
                "startTime": start_time,
                "endTime": end_time,
                "status": ev.get("status", "confirmed"),
                "description": ev.get("description", ""),
                "location": ev.get("location", ""),
                "allDay": is_all_day,
                "source": "google",
                "htmlLink": ev.get("htmlLink", ""),
                "colorId": ev.get("colorId", ""),
            })

        logger.info(f"Google Calendar: Fetched {len(events)} events for {location_id} ({start_date} to {end_date})")
        return flask_jsonify({"events": events, "timezone": tz_str})

    except requests.exceptions.Timeout:
        return flask_jsonify({"error": "Google Calendar timed out"}), 504
    except requests.exceptions.RequestException as e:
        logger.error(f"Google Calendar events fetch failed for {location_id}: {e}")
        return flask_jsonify({"error": "Failed to fetch Google Calendar events"}), 500
