# blueprints/slack.py — Slack integration routes
#
# Public-distribution OAuth: any user can connect their own Slack workspace.
# Stores bot_token per user — no shared bot token needed.

import os
import logging
import secrets
import requests
from urllib.parse import urlencode

from flask import (Blueprint, flash, redirect, url_for, session, request,
                   jsonify as flask_jsonify)
from flask_login import login_required, current_user

from db import (save_slack_connection, get_slack_connection, delete_slack_connection,
                save_slack_workspaces, get_slack_workspaces)

logger = logging.getLogger(__name__)

slack_bp = Blueprint('slack', __name__)

SLACK_API = "https://slack.com/api"


# ── Private helpers ───────────────────────────────────────────────────────────

def _slack_creds():
    """Return (client_id, client_secret, redirect_uri) from environment."""
    client_id     = os.getenv("SLACK_CLIENT_ID")
    client_secret = os.getenv("SLACK_CLIENT_SECRET")
    redirect_uri  = os.getenv("SLACK_REDIRECT_URI",
                               "https://insurancegrokbot.click/slack/callback")
    return client_id, client_secret, redirect_uri


def _slack_bot_headers(conn_row: dict):
    """Return Bot Authorization headers from the user's stored bot_token."""
    bot_token = conn_row.get("bot_token")
    if bot_token:
        return {"Authorization": f"Bearer {bot_token}"}
    return None


# ── OAuth connect / disconnect ────────────────────────────────────────────────

@slack_bp.route("/slack/connect")
@login_required
def slack_connect():
    """Redirect to Slack OAuth — request bot + user scopes for channel access."""
    client_id, _, redirect_uri = _slack_creds()
    if not client_id:
        flash("Slack integration is not configured. Contact support.", "error")
        return redirect(url_for("dashboard.dashboard"))

    state = secrets.token_urlsafe(16)
    session["slack_oauth_state"] = state
    params = urlencode({
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "scope":         "channels:read,channels:history,chat:write,users:read,team:read",
        "user_scope":    "channels:read",
        "state":         state,
    })
    return redirect(f"https://slack.com/oauth/v2/authorize?{params}")


@slack_bp.route("/slack/callback")
@login_required
def slack_callback():
    """Handle Slack OAuth2 V2 callback — saves bot token + team info."""
    code         = request.args.get("code")
    state        = request.args.get("state")
    error        = request.args.get("error")
    stored_state = session.pop("slack_oauth_state", None)

    if error or not code or state != stored_state:
        flash("Slack authorization failed or was cancelled.", "error")
        return redirect(url_for("dashboard.dashboard"))

    client_id, client_secret, redirect_uri = _slack_creds()

    try:
        token_resp = requests.post(
            f"{SLACK_API}/oauth.v2.access",
            data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "code":          code,
                "redirect_uri":  redirect_uri,
            },
            timeout=10,
        )
        token_data = token_resp.json()
    except Exception as e:
        logger.error(f"Slack token exchange failed: {e}")
        flash("Could not connect to Slack. Try again.", "error")
        return redirect(url_for("dashboard.dashboard"))

    if not token_data.get("ok"):
        logger.error(f"Slack token error: {token_data}")
        flash("Slack authorization failed. Please try again.", "error")
        return redirect(url_for("dashboard.dashboard"))

    bot_token    = token_data.get("access_token", "")
    team         = token_data.get("team", {})
    team_id      = team.get("id", "")
    team_name    = team.get("name", "")
    authed_user  = token_data.get("authed_user", {})
    user_id      = authed_user.get("id", "")
    user_token   = authed_user.get("access_token", "")
    bot_user_id  = token_data.get("bot_user_id", "")

    # Fetch the user's display name
    authed_user_name = ""
    try:
        user_resp = requests.get(
            f"{SLACK_API}/users.info",
            headers={"Authorization": f"Bearer {bot_token}"},
            params={"user": user_id},
            timeout=10,
        )
        user_data = user_resp.json()
        if user_data.get("ok"):
            profile = user_data.get("user", {}).get("profile", {})
            authed_user_name = (profile.get("display_name")
                                or profile.get("real_name")
                                or user_data.get("user", {}).get("name", ""))
    except Exception as e:
        logger.warning(f"Slack user info fetch failed: {e}")

    save_slack_connection(
        email=current_user.email,
        slack_team_id=team_id,
        slack_team_name=team_name,
        slack_user_id=user_id,
        bot_token=bot_token,
        user_token=user_token,
        authed_user_name=authed_user_name,
        bot_user_id=bot_user_id,
    )

    # Auto-save this workspace
    save_slack_workspaces(current_user.email, [
        {"team_id": team_id, "name": team_name, "icon": None}
    ])

    flash(f"Slack connected to {team_name}!", "success")
    return redirect(url_for("dashboard.dashboard"))


@slack_bp.route("/slack/disconnect")
@login_required
def slack_disconnect():
    """Remove Slack connection."""
    delete_slack_connection(current_user.email)
    flash("Slack disconnected.", "success")
    return redirect(url_for("dashboard.dashboard"))


# ── Status ────────────────────────────────────────────────────────────────────

@slack_bp.route("/api/slack/status")
@login_required
def api_slack_status():
    """Return connection status + saved workspaces."""
    conn_row = get_slack_connection(current_user.email)
    if not conn_row:
        return flask_jsonify({"connected": False})

    # Fetch team icon
    team_icon = None
    bot_token = conn_row.get("bot_token")
    if bot_token:
        try:
            resp = requests.get(
                f"{SLACK_API}/team.info",
                headers={"Authorization": f"Bearer {bot_token}"},
                timeout=8,
            )
            data = resp.json()
            if data.get("ok"):
                team_icon = data.get("team", {}).get("icon", {}).get("image_68")
        except Exception:
            pass

    workspaces = get_slack_workspaces(current_user.email)
    for ws in workspaces:
        ws["icon_url"] = ws.get("icon") or team_icon

    return flask_jsonify({
        "connected": True,
        "user": {
            "slack_user_id": conn_row.get("slack_user_id"),
            "name":          conn_row.get("authed_user_name") or "Slack User",
            "team_name":     conn_row.get("slack_team_name"),
            "team_id":       conn_row.get("slack_team_id"),
        },
        "workspaces": workspaces,
        "team_icon":  team_icon,
    })


# ── Workspaces ────────────────────────────────────────────────────────────────

@slack_bp.route("/api/slack/workspaces", methods=["GET", "POST"])
@login_required
def api_slack_workspaces():
    """GET saved workspaces / POST to save."""
    if request.method == "GET":
        return flask_jsonify({"workspaces": get_slack_workspaces(current_user.email)})
    data       = request.get_json(silent=True) or {}
    workspaces = data.get("workspaces", [])[:3]
    if save_slack_workspaces(current_user.email, workspaces):
        return flask_jsonify({"status": "saved"})
    return flask_jsonify({"error": "Failed to save workspaces"}), 500


# ── Channels ──────────────────────────────────────────────────────────────────

@slack_bp.route("/api/slack/channels")
@login_required
def api_slack_channels():
    """List public channels for the connected workspace using the bot token."""
    conn_row = get_slack_connection(current_user.email)
    if not conn_row:
        return flask_jsonify({"error": "Not connected to Slack", "channels": []}), 401

    headers = _slack_bot_headers(conn_row)
    if not headers:
        return flask_jsonify({"error": "No bot token available.", "channels": []}), 500

    try:
        channels = []
        cursor = None
        for _ in range(10):  # Max 10 pages
            params = {"types": "public_channel", "exclude_archived": "true", "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            resp = requests.get(f"{SLACK_API}/conversations.list",
                                headers=headers, params=params, timeout=10)
            data = resp.json()
            if not data.get("ok"):
                error_msg = data.get("error", "Unknown error")
                if error_msg in ("not_authed", "invalid_auth", "token_revoked"):
                    return flask_jsonify({"error": "Slack session expired. Please reconnect.",
                                          "needs_reconnect": True, "channels": []})
                return flask_jsonify({"error": f"Slack API error: {error_msg}", "channels": []})
            for ch in data.get("channels", []):
                channels.append({
                    "id":   ch["id"],
                    "name": ch.get("name", ""),
                    "topic": (ch.get("topic") or {}).get("value", ""),
                    "num_members": ch.get("num_members", 0),
                })
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        channels.sort(key=lambda c: c["name"])
        return flask_jsonify({
            "channels":  channels,
            "team_name": conn_row.get("slack_team_name", ""),
        })
    except Exception as e:
        logger.error(f"Slack channels fetch failed: {e}")
        return flask_jsonify({"error": "Failed to fetch channels.", "channels": []})


# ── Messages ──────────────────────────────────────────────────────────────────

@slack_bp.route("/api/slack/messages/<channel_id>", methods=["GET", "POST"])
@login_required
def api_slack_messages(channel_id):
    """GET messages from a channel / POST a message — uses the user's bot token."""
    conn_row = get_slack_connection(current_user.email)
    if not conn_row:
        return flask_jsonify({"error": "Not connected to Slack", "messages": []}), 401

    headers = _slack_bot_headers(conn_row)
    if not headers:
        return flask_jsonify({"error": "No bot token available.", "messages": []}), 500

    if request.method == "POST":
        data    = request.get_json(silent=True) or {}
        content = (data.get("content") or "").strip()
        if not content:
            return flask_jsonify({"error": "Empty message"}), 400

        sender = conn_row.get("authed_user_name") or ""
        text = f"*{sender}:* {content}" if sender else content

        try:
            resp = requests.post(
                f"{SLACK_API}/chat.postMessage",
                headers={**headers, "Content-Type": "application/json"},
                json={"channel": channel_id, "text": text},
                timeout=10,
            )
            data = resp.json()
            if not data.get("ok"):
                error_msg = data.get("error", "Failed to send")
                return flask_jsonify({"error": error_msg}), 400
            return flask_jsonify({"status": "sent"})
        except Exception as e:
            logger.error(f"Slack send exception: {e}")
            return flask_jsonify({"error": "Network error sending message"}), 500

    # GET — fetch messages
    limit = min(int(request.args.get("limit", 50)), 100)
    try:
        resp = requests.get(
            f"{SLACK_API}/conversations.history",
            headers=headers,
            params={"channel": channel_id, "limit": limit},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            error_msg = data.get("error", "Unknown error")
            if error_msg == "not_in_channel":
                # Bot needs to join the channel first
                join_resp = requests.post(
                    f"{SLACK_API}/conversations.join",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"channel": channel_id},
                    timeout=10,
                )
                join_data = join_resp.json()
                if not join_data.get("ok"):
                    return flask_jsonify({"error": "Bot cannot access this channel. "
                                                   "Invite the bot to the channel first.",
                                          "messages": []})
                # Retry after joining
                resp = requests.get(
                    f"{SLACK_API}/conversations.history",
                    headers=headers,
                    params={"channel": channel_id, "limit": limit},
                    timeout=10,
                )
                data = resp.json()
                if not data.get("ok"):
                    return flask_jsonify({"error": f"Slack API error: {data.get('error')}",
                                          "messages": []})
            else:
                return flask_jsonify({"error": f"Slack API error: {error_msg}",
                                      "messages": []})

        # Collect user IDs to resolve display names
        raw_msgs = data.get("messages", [])
        raw_msgs.reverse()  # oldest first

        user_ids = set()
        for m in raw_msgs:
            uid = m.get("user")
            if uid:
                user_ids.add(uid)

        # Batch-resolve user names
        user_map = _resolve_slack_users(headers, user_ids)
        bot_user_id = conn_row.get("bot_user_id", "")

        messages = []
        for m in raw_msgs:
            uid      = m.get("user", "")
            user_info = user_map.get(uid, {})
            author    = user_info.get("name", uid)
            avatar    = user_info.get("avatar")
            messages.append({
                "id":          m.get("ts", ""),
                "author":      author,
                "author_id":   uid,
                "avatar_url":  avatar,
                "content":     m.get("text", ""),
                "timestamp":   _ts_to_iso(m.get("ts")),
                "is_bot":      uid == bot_user_id,
                "attachments": [{"url": f.get("url_private", ""),
                                  "filename": f.get("name", ""),
                                  "content_type": f.get("mimetype", "")}
                                 for f in (m.get("files") or [])],
                "reactions":   [{"emoji": r.get("name", "?"), "count": r.get("count", 0)}
                                 for r in (m.get("reactions") or [])],
            })
        return flask_jsonify({"messages": messages})
    except Exception as e:
        logger.error(f"Slack fetch messages exception for {channel_id}: {e}")
        return flask_jsonify({"error": "Failed to fetch messages.", "messages": []})


# ── Private: user name resolution ─────────────────────────────────────────────

_user_cache = {}  # In-process cache: {bot_token_hash: {user_id: {name, avatar}}}


def _resolve_slack_users(headers: dict, user_ids: set) -> dict:
    """Resolve Slack user IDs to display names + avatar URLs."""
    token_key = headers.get("Authorization", "")[:20]
    if token_key not in _user_cache:
        _user_cache[token_key] = {}
    cache = _user_cache[token_key]

    result = {}
    to_fetch = []
    for uid in user_ids:
        if uid in cache:
            result[uid] = cache[uid]
        else:
            to_fetch.append(uid)

    for uid in to_fetch:
        try:
            resp = requests.get(
                f"{SLACK_API}/users.info",
                headers=headers,
                params={"user": uid},
                timeout=8,
            )
            data = resp.json()
            if data.get("ok"):
                user = data.get("user", {})
                profile = user.get("profile", {})
                info = {
                    "name":   (profile.get("display_name")
                               or profile.get("real_name")
                               or user.get("name", uid)),
                    "avatar": profile.get("image_48"),
                }
                cache[uid] = info
                result[uid] = info
            else:
                result[uid] = {"name": uid, "avatar": None}
        except Exception:
            result[uid] = {"name": uid, "avatar": None}

    return result


def _ts_to_iso(ts_str):
    """Convert Slack timestamp (e.g. '1234567890.123456') to ISO 8601."""
    if not ts_str:
        return None
    try:
        from datetime import datetime, timezone
        epoch = float(ts_str)
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None
