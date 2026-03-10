# blueprints/discord.py — Discord integration routes
#
# Completely self-contained: OAuth connect/callback/disconnect, guild management,
# channel listing, and message read/send. Zero cross-blueprint dependencies.

import os
import logging
import secrets
import requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from flask import (Blueprint, flash, redirect, url_for, session, request,
                   jsonify as flask_jsonify)
from flask_login import login_required, current_user

from db import (save_discord_connection, get_discord_connection, delete_discord_connection,
                save_discord_servers, get_discord_servers)

logger = logging.getLogger(__name__)

discord_bp = Blueprint('discord', __name__)

DISCORD_API = "https://discord.com/api/v10"


# ── Private helpers ───────────────────────────────────────────────────────────

def _discord_creds():
    """Return (client_id, client_secret, redirect_uri) from environment."""
    client_id     = os.getenv("DISCORD_CLIENT_ID")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    redirect_uri  = os.getenv("DISCORD_REDIRECT_URI",
                               "https://insurancegrokbot.click/discord/callback")
    return client_id, client_secret, redirect_uri


def _discord_bot_headers():
    """Return Bot Authorization headers dict, or None if token not set."""
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if bot_token:
        return {"Authorization": f"Bot {bot_token}"}
    return None


def _discord_refresh_token(email: str, conn_row: dict):
    """Silently refresh an expired Discord OAuth token. Returns new access_token or None."""
    client_id     = os.getenv("DISCORD_CLIENT_ID")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    refresh_tok   = conn_row.get("refresh_token")
    if not all([client_id, client_secret, refresh_tok]):
        return None
    try:
        resp = requests.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "grant_type":    "refresh_token",
                "refresh_token": refresh_tok,
                "client_id":     client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if resp.status_code == 200:
            tok         = resp.json()
            new_access  = tok["access_token"]
            new_refresh = tok.get("refresh_token", refresh_tok)
            expires_in  = tok.get("expires_in", 604800)
            expires_at  = datetime.utcnow() + timedelta(seconds=expires_in)
            save_discord_connection(
                email=email,
                discord_user_id=conn_row["discord_user_id"],
                username=conn_row["username"],
                global_name=conn_row.get("global_name"),
                avatar=conn_row.get("avatar"),
                access_token=new_access,
                refresh_token=new_refresh,
                token_expires_at=expires_at,
            )
            return new_access
    except Exception as e:
        logger.error(f"Discord token refresh failed for {email}: {e}")
    return None


# ── OAuth connect / disconnect ────────────────────────────────────────────────

@discord_bp.route("/discord/connect")
@login_required
def discord_connect():
    """Redirect to Discord OAuth — identify + guilds only (bot handles the rest)."""
    client_id, _, redirect_uri = _discord_creds()
    if not client_id:
        flash("Discord integration is not configured. Contact support.", "error")
        return redirect(url_for("dashboard.dashboard"))

    state = secrets.token_urlsafe(16)
    session["discord_oauth_state"] = state
    params = urlencode({
        "response_type": "code",
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "scope":         "identify guilds",
        "state":         state,
    })
    return redirect(f"https://discord.com/oauth2/authorize?{params}")


@discord_bp.route("/discord/callback")
@login_required
def discord_callback():
    """Handle OAuth2 callback — saves user identity + access token."""
    code         = request.args.get("code")
    state        = request.args.get("state")
    stored_state = session.pop("discord_oauth_state", None)

    if not code or state != stored_state:
        flash("Discord authorization failed or was cancelled.", "error")
        return redirect(url_for("dashboard.dashboard"))

    client_id, client_secret, redirect_uri = _discord_creds()

    try:
        token_resp = requests.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": redirect_uri,
            },
            auth=(client_id, client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        token_data = token_resp.json()
    except Exception as e:
        logger.error(f"Discord token exchange failed: {e}")
        flash("Could not connect to Discord. Try again.", "error")
        return redirect(url_for("dashboard.dashboard"))

    if "access_token" not in token_data:
        logger.error(f"Discord token error: {token_data}")
        flash("Discord authorization failed. Please try again.", "error")
        return redirect(url_for("dashboard.dashboard"))

    access_token  = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")
    expires_in    = token_data.get("expires_in", 604800)
    expires_at    = datetime.utcnow() + timedelta(seconds=expires_in)

    try:
        user_resp    = requests.get(f"{DISCORD_API}/users/@me",
                                     headers={"Authorization": f"Bearer {access_token}"},
                                     timeout=10)
        discord_user = user_resp.json()
    except Exception as e:
        logger.error(f"Discord user fetch failed: {e}")
        flash("Connected to Discord but could not fetch user info.", "error")
        return redirect(url_for("dashboard.dashboard"))

    discord_user_id = discord_user.get("id", "")
    username        = discord_user.get("username", "")
    global_name     = discord_user.get("global_name") or username
    avatar_hash     = discord_user.get("avatar")

    save_discord_connection(
        email=current_user.email,
        discord_user_id=discord_user_id,
        username=username,
        global_name=global_name,
        avatar=avatar_hash,
        access_token=access_token,
        refresh_token=refresh_token,
        token_expires_at=expires_at,
    )

    flash(f"Discord connected as {global_name or username}! Add a server below.", "success")
    return redirect(url_for("dashboard.dashboard"))


@discord_bp.route("/discord/disconnect")
@login_required
def discord_disconnect():
    """Remove Discord connection."""
    delete_discord_connection(current_user.email)
    flash("Discord disconnected.", "success")
    return redirect(url_for("dashboard.dashboard"))


# ── Status / guilds / servers ─────────────────────────────────────────────────

@discord_bp.route("/api/discord/status")
@login_required
def api_discord_status():
    """Return connection status + saved servers with bot_in_server flag."""
    conn_row = get_discord_connection(current_user.email)
    if not conn_row:
        return flask_jsonify({"connected": False})

    needs_reauth = False
    expires_at   = conn_row.get("token_expires_at")
    if expires_at:
        if getattr(expires_at, 'tzinfo', None) is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if expires_at <= now + timedelta(hours=24):
            new_tok = _discord_refresh_token(current_user.email, conn_row)
            if new_tok:
                conn_row = get_discord_connection(current_user.email) or conn_row
            elif expires_at <= now:
                needs_reauth = True

    avatar_url = None
    if conn_row.get("avatar"):
        avatar_url = (f"https://cdn.discordapp.com/avatars/"
                      f"{conn_row['discord_user_id']}/{conn_row['avatar']}.png?size=64")

    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    servers   = get_discord_servers(current_user.email)
    for srv in servers:
        srv["icon_url"] = (
            f"https://cdn.discordapp.com/icons/{srv['guild_id']}/{srv['icon']}.png?size=64"
            if srv.get("icon") else None
        )
        srv["bot_in_server"] = False
        if bot_token:
            try:
                r = requests.get(f"{DISCORD_API}/guilds/{srv['guild_id']}",
                                  headers={"Authorization": f"Bot {bot_token}"}, timeout=5)
                srv["bot_in_server"] = (r.status_code == 200)
            except Exception:
                pass

    return flask_jsonify({
        "connected":    True,
        "needs_reauth": needs_reauth,
        "user": {
            "discord_id":  conn_row["discord_user_id"],
            "username":    conn_row["username"],
            "global_name": conn_row.get("global_name"),
            "avatar_url":  avatar_url,
        },
        "servers": servers,
    })


@discord_bp.route("/api/discord/guilds")
@login_required
def api_discord_guilds():
    """Return user's guilds (via OAuth) with a bot_in_server flag for each."""
    conn_row = get_discord_connection(current_user.email)
    if not conn_row:
        return flask_jsonify({"error": "Not connected to Discord"}), 401

    access_token = conn_row["access_token"]
    url = f"{DISCORD_API}/users/@me/guilds"
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
        if resp.status_code == 401:
            new_tok = _discord_refresh_token(current_user.email, conn_row)
            if new_tok:
                resp = requests.get(url, headers={"Authorization": f"Bearer {new_tok}"}, timeout=10)
        if resp.status_code == 401:
            return flask_jsonify({"error": "Discord session expired. Please reconnect.",
                                  "needs_reconnect": True, "guilds": []})

        guilds = resp.json()
        if not isinstance(guilds, list):
            return flask_jsonify({"error": "Could not fetch servers from Discord", "guilds": []})

        bot_token = os.getenv("DISCORD_BOT_TOKEN")
        result = []
        for g in guilds:
            bot_in = False
            if bot_token:
                try:
                    br = requests.get(f"{DISCORD_API}/guilds/{g['id']}",
                                      headers={"Authorization": f"Bot {bot_token}"}, timeout=5)
                    bot_in = (br.status_code == 200)
                except Exception:
                    pass
            result.append({"id": g["id"], "name": g["name"],
                            "icon": g.get("icon"), "bot_in_server": bot_in})
        return flask_jsonify({"guilds": result})
    except Exception as e:
        logger.error(f"Discord guilds fetch failed: {e}")
        return flask_jsonify({"error": "Failed to fetch servers", "guilds": []})


@discord_bp.route("/api/discord/bot-invite/<guild_id>")
@login_required
def api_discord_bot_invite(guild_id):
    """Return a pre-filled bot invite URL for a specific guild."""
    client_id = os.getenv("DISCORD_CLIENT_ID")
    if not client_id:
        return flask_jsonify({"error": "Discord not configured"}), 500

    invite_url = "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id":            client_id,
        "permissions":          "274877974528",
        "scope":                "bot",
        "guild_id":             guild_id,
        "disable_guild_select": "true",
    })
    return flask_jsonify({"invite_url": invite_url})


@discord_bp.route("/api/discord/bot-check/<guild_id>")
@login_required
def api_discord_bot_check(guild_id):
    """Poll whether the bot is now in a guild (used after invite redirect)."""
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if not bot_token:
        return flask_jsonify({"in_server": False, "error": "DISCORD_BOT_TOKEN not set"})
    try:
        r = requests.get(f"{DISCORD_API}/guilds/{guild_id}",
                          headers={"Authorization": f"Bot {bot_token}"}, timeout=8)
        return flask_jsonify({"in_server": r.status_code == 200})
    except Exception as e:
        return flask_jsonify({"in_server": False, "error": str(e)})


@discord_bp.route("/api/discord/servers", methods=["GET", "POST"])
@login_required
def api_discord_servers():
    """GET saved servers / POST to save a new server list."""
    if request.method == "GET":
        return flask_jsonify({"servers": get_discord_servers(current_user.email)})
    data    = request.get_json(silent=True) or {}
    servers = data.get("servers", [])[:10]
    if save_discord_servers(current_user.email, servers):
        return flask_jsonify({"status": "saved"})
    return flask_jsonify({"error": "Failed to save servers"}), 500


# ── Channels and messages ─────────────────────────────────────────────────────

@discord_bp.route("/api/discord/channels/<guild_id>")
@login_required
def api_discord_channels(guild_id):
    """List text channels for a guild using the bot token."""
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if not bot_token:
        return flask_jsonify({"error": "DISCORD_BOT_TOKEN not configured.", "channels": []}), 500

    saved      = get_discord_servers(current_user.email)
    guild_name = next((s["name"] for s in saved if s["guild_id"] == guild_id), "")

    try:
        resp = requests.get(f"{DISCORD_API}/guilds/{guild_id}/channels",
                             headers={"Authorization": f"Bot {bot_token}"}, timeout=10)
        if resp.status_code == 401:
            return flask_jsonify({"error": "Invalid bot token.", "channels": []})
        if resp.status_code in (403, 404):
            return flask_jsonify({"error": "Bot is not in this server yet.",
                                  "needs_invite": True, "channels": []})
        data = resp.json()
        if not isinstance(data, list):
            return flask_jsonify({"error": "Could not load channels.", "channels": []})
        channels = sorted(
            [{"id": c["id"], "name": c["name"], "position": c.get("position", 0)}
             for c in data if c.get("type") == 0],
            key=lambda c: c["position"]
        )
        return flask_jsonify({"channels": channels, "guild_name": guild_name})
    except Exception as e:
        logger.error(f"Discord channels fetch failed for {guild_id}: {e}")
        return flask_jsonify({"error": "Failed to fetch channels.", "channels": []})


@discord_bp.route("/api/discord/messages/<channel_id>", methods=["GET", "POST"])
@login_required
def api_discord_messages(channel_id):
    """GET messages from a channel / POST a message — always uses bot token."""
    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    if not bot_token:
        return flask_jsonify({"error": "DISCORD_BOT_TOKEN not configured.", "messages": []}), 500

    bot_headers = {"Authorization": f"Bot {bot_token}"}

    if request.method == "POST":
        data    = request.get_json(silent=True) or {}
        content = (data.get("content") or "").strip()
        if not content:
            return flask_jsonify({"error": "Empty message"}), 400
        conn_row = get_discord_connection(current_user.email)
        sender   = ""
        if conn_row:
            sender = conn_row.get("global_name") or conn_row.get("username") or ""
        payload = {"content": f"**{sender}:** {content}" if sender else content}
        try:
            resp = requests.post(f"{DISCORD_API}/channels/{channel_id}/messages",
                                  headers={**bot_headers, "Content-Type": "application/json"},
                                  json=payload, timeout=10)
            if resp.status_code not in (200, 201):
                err = resp.json() if resp.content else {}
                return flask_jsonify({"error": err.get("message", "Failed to send")}), 400
            return flask_jsonify({"status": "sent"})
        except Exception as e:
            logger.error(f"Discord send exception: {e}")
            return flask_jsonify({"error": "Network error sending message"}), 500

    # GET — fetch messages
    limit = min(int(request.args.get("limit", 50)), 100)
    try:
        resp = requests.get(f"{DISCORD_API}/channels/{channel_id}/messages",
                             headers=bot_headers, params={"limit": limit}, timeout=10)
        if resp.status_code in (401, 403):
            return flask_jsonify({"error": "Bot cannot access this channel.", "messages": []})
        msgs_data = resp.json()
        if not isinstance(msgs_data, list):
            return flask_jsonify({"error": "Could not fetch messages.", "messages": []})
        msgs_data.reverse()
        messages = []
        for m in msgs_data:
            author    = m.get("author", {})
            author_id = author.get("id", "")
            av_hash   = author.get("avatar")
            ref       = m.get("referenced_message")
            reply_to  = None
            if ref:
                ra       = ref.get("author", {})
                reply_to = {"author":  ra.get("global_name") or ra.get("username", "Unknown"),
                            "content": (ref.get("content") or "")[:120]}
            messages.append({
                "id":          m["id"],
                "author":      author.get("global_name") or author.get("username", "Unknown"),
                "author_id":   author_id,
                "avatar_url":  (f"https://cdn.discordapp.com/avatars/{author_id}/{av_hash}.png?size=64"
                                if av_hash else None),
                "content":     m.get("content", ""),
                "timestamp":   m.get("timestamp"),
                "reply_to":    reply_to,
                "reactions":   [{"emoji": r["emoji"].get("name", "?"), "count": r["count"]}
                                 for r in (m.get("reactions") or [])],
                "attachments": [{"url": a["url"], "filename": a.get("filename", ""),
                                  "content_type": a.get("content_type", "")}
                                 for a in (m.get("attachments") or [])],
            })
        return flask_jsonify({"messages": messages})
    except Exception as e:
        logger.error(f"Discord fetch messages exception for {channel_id}: {e}")
        return flask_jsonify({"error": "Failed to fetch messages.", "messages": []})
