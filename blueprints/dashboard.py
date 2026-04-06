# blueprints/dashboard.py — Main subscriber dashboard and all dashboard API endpoints
#
# Routes:
#   GET  /app                           — Smart GHL entry point (redirects based on state)
#   GET  /onboarding-status             — Live setup checklist
#   GET|POST /dashboard                 — Main subscriber dashboard
#   POST /save-profile                  — Update name/phone/bio
#   GET|POST /api/voice-config          — Voice AI configuration
#   POST /api/training/generate-code    — Generate training integration code
#   POST /api/training/revoke-code      — Revoke training integration code
#   GET  /api/training/status           — Training code status
#   GET|POST /api/carriers              — Contracted carrier list
#   GET|POST /api/bot-settings          — Bot behavior settings
#   POST /api/generate-key              — Generate external API key
#   POST /api/revoke-key                — Revoke external API key
#   POST /api/webhook-url               — Set outbound webhook URL
#   GET  /api/api-status                — External API key status
#   POST /api/save-config               — Save bot configuration (AJAX)
#   POST /api/integrations/save         — Save CRM integration settings
#   POST /api/integrations/test         — Test CRM integration credentials
#   GET  /api/logs                      — Activity logs (paginated)
#   GET  /api/alerts                    — Persistent alert banners
#   POST /api/alerts/<id>/dismiss       — Dismiss an alert
#   GET  /api/onboarding-check          — OAuth loading screen status check

import json
import logging
import os
import requests
from datetime import datetime

from flask import Blueprint, request, render_template, redirect, url_for, flash
from flask import jsonify as flask_jsonify
from flask_login import login_required, current_user

from extensions import ADMIN_EMAILS, safe_jsonify
from token_encryption import decrypt_token
from db import (
    get_db_connection, return_db_connection, User,
    get_contracted_carriers, save_contracted_carriers,
    get_bot_settings, save_bot_settings, BOT_SETTINGS_DEFAULTS,
    get_webhook_logs, get_persistent_alerts, dismiss_persistent_alert,
    create_api_key_for_user, revoke_api_key, save_outbound_webhook_url,
    create_training_token_for_user, revoke_training_token,
)
from forms import ConfigForm
from carrier_list import CARRIER_LIST, validate_carrier_keys
from blueprints.team import require_permission
from crm_adapters.factory import (CRM_CONFIG_FIELDS, CRM_DISPLAY_NAMES,
                                   CRM_REGISTRY, get_crm_adapter)
from translations import SUPPORTED_LANGUAGES

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint('dashboard', __name__)


# ── GHL App entry point ───────────────────────────────────────────────────────

@dashboard_bp.route("/dialer-popout")
@login_required
def dialer_popout():
    """Standalone top-level dialer window.

    Exists so agents running inside the GHL Custom Page iframe (where the
    parent's Permissions-Policy blocks `microphone` delegation to our origin)
    can pop the dialer out into a real browser window. A top-level window has
    no parent frame policy, so our own `Permissions-Policy: microphone=*`
    header is the only policy that applies and `getUserMedia` works.

    Auth-gated via the existing Flask-Login session cookie (same-origin popup
    inherits it). All voice runs here; the iframe becomes a remote control
    that talks to the popup via a same-origin BroadcastChannel.
    """
    display_name = (
        current_user.full_name
        or current_user.bot_first_name
        or (current_user.email.split("@")[0] if current_user.email else "Agent")
    )
    return render_template(
        "popout-dialer.html",
        display_name=display_name,
        location_id=current_user.location_id or "",
        bot_name=current_user.bot_first_name or "",
    )


@dashboard_bp.route("/app")
def app_entry():
    """Smart entry for GHL Custom Page sidebar link — redirects based on setup state."""
    if current_user.is_authenticated:
        is_admin         = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
        has_token        = bool(current_user.access_token)
        has_subscription = bool(current_user.stripe_customer_id) or is_admin
        loc_ok           = bool(current_user.location_id and
                                not str(current_user.location_id).startswith("temp_"))
        cal_ok           = bool(current_user.calendar_id)
        bot_ok           = bool(current_user.bot_first_name)
        setup_complete   = has_token and has_subscription and loc_ok and cal_ok and bot_ok

        if setup_complete:
            if current_user.role == 'agency_owner':
                return redirect("/agency-dashboard")
            return redirect("/dashboard")
        return redirect("/onboarding-status")

    return redirect("/login")


# ── Onboarding status ─────────────────────────────────────────────────────────

@dashboard_bp.route("/onboarding-status")
@login_required
def onboarding_status():
    """Live setup checklist — reads real account state and shows next action."""
    is_admin         = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
    has_token        = bool(current_user.access_token)
    has_password     = bool(current_user.password_hash)
    has_subscription = bool(current_user.stripe_customer_id) or is_admin
    loc_ok           = bool(current_user.location_id and
                            not str(current_user.location_id).startswith("temp_"))
    cal_ok           = bool(current_user.calendar_id)
    bot_ok           = bool(current_user.bot_first_name)
    config_ok        = loc_ok and cal_ok and bot_ok
    all_done         = has_token and has_password and has_subscription and config_ok

    user_type     = 'agency' if current_user.role == 'agency_owner' else 'individual'
    dashboard_url = '/agency-dashboard' if current_user.role == 'agency_owner' else '/dashboard'
    tier          = current_user.subscription_tier or 'individual'

    checkout_url = '/checkout'

    if not has_subscription:
        next_url = checkout_url
    elif not has_password:
        next_url = f'/set-password?type={user_type}'
    elif not has_token:
        next_url = '/oauth/initiate'
    else:
        next_url = dashboard_url

    steps = [
        {"label": "Connect Your CRM", "done": has_token, "icon": "fa-plug",
         "help": "Links your Lead Connector account so the bot can read and send messages.",
         "url": "/oauth/initiate", "button_text": "Connect Now"},
        {"label": "Activate Subscription", "done": has_subscription, "icon": "fa-credit-card",
         "help": "Choose your plan to turn on all bot features.",
         "url": checkout_url, "button_text": "Subscribe Now"},
        {"label": "Create Your Password", "done": has_password, "icon": "fa-lock",
         "help": "You'll use this to log in from now on.",
         "url": f"/set-password?type={user_type}", "button_text": "Set Password"},
        {"label": "Configure Your Bot", "done": config_ok, "icon": "fa-sliders",
         "help": "Pick your calendar, name your bot, and confirm your location.",
         "url": dashboard_url, "button_text": "Open Dashboard"},
    ]

    completed_count = sum(1 for s in steps if s["done"])
    return render_template('onboarding-status.html',
        steps=steps,
        completed_count=completed_count,
        total_steps=len(steps),
        all_done=all_done,
        next_url=next_url,
    )


# ── Main Dashboard ────────────────────────────────────────────────────────────

@dashboard_bp.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    if current_user.role == 'agency_owner':
        return redirect(url_for("agency.agency_dashboard"))

    is_admin         = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
    _stripe_inactive = getattr(current_user, 'stripe_status', '') in ('canceled', 'unpaid', 'incomplete_expired')
    needs_subscription = (not current_user.stripe_customer_id or _stripe_inactive) and not is_admin

    if needs_subscription:
        return render_template('dashboard.html',
            needs_subscription=True,
            needs_onboarding=False,
            subscription_price=149.99,
            form=ConfigForm(),
            access_token_display='',
            refresh_token_display='',
            token_readonly='',
            expires_in_str='',
            sub=current_user,
            profile={
                'full_name': current_user.full_name or '',
                'phone':     current_user.phone or '',
                'bio':       current_user.bio or '',
            }
        )

    form = ConfigForm()
    conn = get_db_connection()

    if request.method == 'POST' and not form.validate_on_submit():
        logger.warning(f"Dashboard form validation failed for {current_user.email}: {form.errors}")
        flash("Please fill in all required fields.", "error")

    if form.validate_on_submit():
        if not conn:
            flash("Database connection failed", "error")
        else:
            try:
                cur = conn.cursor()
                calendar_name = request.form.get('calendar_name', '')
                cur.execute("""
                    UPDATE subscribers
                    SET location_id      = %s,
                        calendar_id      = %s,
                        calendar_name    = %s,
                        crm_user_id      = %s,
                        bot_first_name   = %s,
                        timezone         = %s,
                        initial_message  = %s,
                        personal_website = %s,
                        updated_at       = NOW()
                    WHERE email = %s
                """, (
                    form.location_id.data,
                    form.calendar_id.data,
                    calendar_name,
                    form.crm_user_id.data,
                    form.bot_name.data,
                    form.timezone.data,
                    form.initial_message.data,
                    form.personal_website.data or None,
                    current_user.email,
                ))
                conn.commit()
                flash("Settings saved successfully!", "success")
                return redirect(url_for('dashboard.dashboard'))
            except Exception as e:
                conn.rollback()
                flash(f"Error saving settings: {str(e)}", "error")
            finally:
                cur.close()
                return_db_connection(conn)
                conn = None  # prevent double-return below

    if request.method == 'GET':
        form.location_id.data     = current_user.location_id
        form.calendar_id.data     = current_user.calendar_id
        form.crm_user_id.data     = current_user.crm_user_id
        form.bot_name.data        = current_user.bot_first_name
        form.timezone.data        = current_user.timezone
        form.initial_message.data = current_user.initial_message
        form.personal_website.data= current_user.personal_website

    # Token display — decrypt first so we mask real values, not ciphertext
    access_token_display = ''
    refresh_token_display= ''
    expires_in_str       = ''
    token_field_state    = ''

    if current_user.access_token:
        token_field_state = 'readonly'
        at = decrypt_token(current_user.access_token) or current_user.access_token
        access_token_display = at[:8] + '...' + at[-4:] if len(at) > 12 else at
        if current_user.token_expires_at:
            expires_at = current_user.token_expires_at
            if isinstance(expires_at, str):
                try:
                    expires_at = datetime.fromisoformat(expires_at)
                except Exception:
                    expires_at = datetime.now()
            delta = expires_at - datetime.now()
            if delta.total_seconds() > 0:
                h = int(delta.total_seconds() // 3600)
                m = int((delta.total_seconds() % 3600) // 60)
                expires_in_str = f"Expires in {h}h {m}m"
            else:
                expires_in_str = "Token Expired"
        else:
            expires_in_str = "Persistent"

    if current_user.refresh_token:
        rt = decrypt_token(current_user.refresh_token) or current_user.refresh_token
        refresh_token_display = rt[:8] + '...' + rt[-4:] if len(rt) > 12 else rt

    profile = {
        'full_name': current_user.full_name or '',
        'phone':     current_user.phone or '',
        'bio':       current_user.bio or '',
    }

    needs_oauth   = not bool(current_user.access_token)
    show_congrats = request.args.get('setup') == 'complete'

    loc_ok = bool(current_user.location_id and
                  not str(current_user.location_id).startswith("temp_"))
    cal_ok = bool(current_user.calendar_id)
    bot_ok = bool(current_user.bot_first_name)
    tz_ok  = bool(current_user.timezone)
    msg_ok = bool(current_user.initial_message)

    missing_fields = []
    if not bot_ok: missing_fields.append('bot_name')
    if not tz_ok:  missing_fields.append('timezone')
    if not msg_ok: missing_fields.append('initial_message')
    if not loc_ok: missing_fields.append('location_id')
    if not cal_ok: missing_fields.append('calendar_id')

    is_placeholder = bool(current_user.email and
                          current_user.email.endswith('@placeholder.grokbot'))
    is_incomplete  = bool(not current_user.crm_user_id or not current_user.location_id)

    # Return DB connection if still held (not consumed by POST path)
    if conn:
        return_db_connection(conn)
        conn = None

    selected_carriers = get_contracted_carriers(current_user.email)
    bot_settings      = get_bot_settings(current_user.email)
    voice_config      = current_user.voice_config or {}

    # Auto-sync primary phone number: if sub-account is provisioned but
    # twilio_phone_number is missing, look up the first active number from
    # Twilio and persist it so the Activate Voice panel shows correctly.
    _sub_sid = voice_config.get('twilio_sub_account_sid', '')
    if _sub_sid and not voice_config.get('twilio_phone_number'):
        try:
            import twilio_provisioning as _tp
            from voice.helpers import _save_voice_config as _svc
            _nums = _tp.list_phone_numbers(_sub_sid)
            if _nums:
                _first = _nums[0]
                voice_config['twilio_phone_number'] = _first.get('phone', '')
                voice_config['twilio_number_sid'] = _first.get('sid', '')
                _svc(current_user.email, voice_config)
                logger.info(f"[dashboard] Auto-synced primary number {voice_config['twilio_phone_number']} for {current_user.email}")
        except Exception as _e:
            logger.warning(f"[dashboard] Primary number auto-sync failed: {_e}")

    # GHL embed mode — hide sidebar/topbar, show compact tab bar
    embed_mode = request.args.get('embed') == '1'
    initial_tab = request.args.get('tab', 'voicedialer') if embed_mode else ''
    embed_contact_id = request.args.get('contact_id', '') if embed_mode else ''
    embed_dial_contacts = request.args.get('dial_contacts', '') if embed_mode else ''
    # Voice WebSocket host — where browser connects for listen/intercept.
    # Derive from VOICE_WSS_URL if VOICE_WSS_HOST not explicitly set,
    # so that if calls work (VOICE_WSS_URL is set), listen works too.
    voice_wss_host = os.getenv('VOICE_WSS_HOST', '')
    if not voice_wss_host:
        voice_wss_url = os.getenv('VOICE_WSS_URL', '')
        if voice_wss_url:
            # Extract host from "wss://voice-server.up.railway.app/voice/stream"
            from urllib.parse import urlparse
            parsed = urlparse(voice_wss_url)
            voice_wss_host = parsed.netloc or ''

    # White-label branding — agency owners get their own, sub-users get agency's
    from db import get_whitelabel_for_user
    whitelabel = get_whitelabel_for_user(current_user)

    # Agency context — for sidebar/tabs showing agency-specific items
    is_agency = current_user.role == 'agency_owner'

    # Business profile onboarding gate — all paying users must complete
    # the onboarding wizard before accessing the workspace.
    needs_onboarding = False
    if not is_admin and not current_user.business_profile_submitted:
        needs_onboarding = True

    return render_template('dashboard.html',
        form=form,
        access_token_display=access_token_display,
        refresh_token_display=refresh_token_display,
        token_readonly=token_field_state,
        expires_in_str=expires_in_str,
        sub=current_user,
        profile=profile,
        needs_oauth=needs_oauth,
        needs_onboarding=needs_onboarding,
        show_congrats=show_congrats,
        missing_fields=missing_fields,
        is_placeholder=is_placeholder,
        is_incomplete=is_incomplete,
        carrier_list=CARRIER_LIST,
        selected_carriers=selected_carriers,
        bot_settings=bot_settings,
        crm_config_fields=CRM_CONFIG_FIELDS,
        crm_display_names=CRM_DISPLAY_NAMES,
        voice_config=voice_config,
        embed_mode=embed_mode,
        initial_tab=initial_tab,
        embed_contact_id=embed_contact_id,
        embed_dial_contacts=embed_dial_contacts,
        whitelabel=whitelabel,
        is_agency=is_agency,
        voice_wss_host=voice_wss_host,
    )


# ── Profile ───────────────────────────────────────────────────────────────────

@dashboard_bp.route("/save-profile", methods=["POST"])
@login_required
def save_profile():
    data = request.get_json()
    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers
            SET full_name = %s, phone = %s, bio = %s, updated_at = NOW()
            WHERE email = %s
        """, (data.get('name'), data.get('phone'), data.get('bio'), current_user.email))
        conn.commit()
        return flask_jsonify({"status": "success", "message": "Profile updated"})
    except Exception as e:
        conn.rollback()
        return flask_jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Voice config ──────────────────────────────────────────────────────────────

@dashboard_bp.route("/api/voice-config", methods=["GET"])
@login_required
def get_voice_config():
    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row    = cur.fetchone()
        cur.close()
        config = (row['voice_config'] if row else {}) or {}
        return flask_jsonify({"voice_config": config})
    except Exception as e:
        return flask_jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)


@dashboard_bp.route("/api/voice-config", methods=["POST"])
@login_required
@require_permission('can_change_bot_config')
def save_voice_config():
    data = request.get_json()
    if not data:
        return flask_jsonify({"error": "No data provided"}), 400

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database error"}), 500

    is_agency = current_user.role == 'agency_owner'
    existing_vc = {}
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if row and row['voice_config']:
            existing_vc = row['voice_config'] if isinstance(row['voice_config'], dict) else {}
    except Exception:
        pass
    finally:
        return_db_connection(conn)

    def _safe_int(val, default):
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    import re as _re

    def _safe_hhmm(val, default):
        """Validate HH:MM format; return default if invalid."""
        if not val or not isinstance(val, str):
            return default
        val = val.strip()[:5]
        if _re.match(r'^([01]\d|2[0-3]):[0-5]\d$', val):
            return val
        return default

    def _safe_float(val, default):
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    def _parse_transfer_numbers(raw):
        """Parse transfer_numbers from JSON string or list. Returns list of {number, label} dicts (max 5)."""
        if not raw:
            return []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return []
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw[:5]:
            if isinstance(item, dict):
                num = (item.get('number') or '').strip()
                label = (item.get('label') or '').strip()[:50]
                if num and len(num.replace('-', '').replace(' ', '').replace('(', '').replace(')', '')) >= 10:
                    result.append({"number": num, "label": label})
        return result

    # Merge user-facing settings onto existing config (preserves Twilio provisioned fields)
    voice_config = dict(existing_vc)
    voice_config.update({
        "enabled":            bool(data.get("enabled", False)),
        "voice":              (data.get("voice") or "ara").strip().lower(),
        "voice_bot_name":     (data.get("voice_bot_name") or "").strip(),
        "voice_instructions": (data.get("voice_instructions") or "").strip(),
        "call_script":        (data.get("call_script") or "").strip(),
        "dial_attempts":      max(1, min(6, _safe_int(data.get("dial_attempts"), 2))),
        "auto_record":        bool(data.get("auto_record", True)),
        "auto_transcribe":    bool(data.get("auto_transcribe", False)),
        "local_presence":     bool(data.get("local_presence", False)),
        "transfer_number":    (data.get("transfer_number") or "").strip(),
        "transfer_numbers":   _parse_transfer_numbers(data.get("transfer_numbers")),
        "voicemail_drop":     bool(data.get("voicemail_drop", False)),
        # Enterprise dialer tuning
        "ring_timeout":          max(15, min(120, _safe_int(data.get("ring_timeout"), 45))),
        "pause_between_calls":   max(0, min(30, _safe_int(data.get("pause_between_calls"), 1))),
        "use_amd":               bool(data.get("use_amd", False)),
        "max_call_duration":     max(0, min(120, _safe_int(data.get("max_call_duration"), 0))),
        "retry_delay":           max(1, min(30, _safe_int(data.get("retry_delay"), 2))),
        "auto_callback":         bool(data.get("auto_callback", False)),
        # Multi-line / predictive dialer settings
        "max_lines_setting":          max(1, min(4, _safe_int(data.get("max_lines_setting"), 3))),
        "wrap_up_time":               max(0, min(120, _safe_int(data.get("wrap_up_time"), 15))),
        "require_disposition":        bool(data.get("require_disposition", True)),
        "calling_hours_start":        _safe_hhmm(data.get("calling_hours_start"), "08:00"),
        "calling_hours_end":          _safe_hhmm(data.get("calling_hours_end"), "21:00"),
        "same_number_cooldown_hours": 0,
        "on_machine_action":          (data.get("on_machine_action") or "hangup") if data.get("on_machine_action") in ("hangup", "voicemail_drop", "continue") else "hangup",
        "auto_disposition_no_answer": bool(data.get("auto_disposition_no_answer", True)),
        "auto_disposition_voicemail": bool(data.get("auto_disposition_voicemail", True)),
        "max_abandon_rate_pct":       max(1.0, min(10.0, _safe_float(data.get("max_abandon_rate_pct"), 3.0))),
        "quiet_hours_enabled":        bool(data.get("quiet_hours_enabled", False)),
        "bypass_calling_hours":       bool(data.get("bypass_calling_hours", False)),
        # Dossier display settings
        "show_ai_summary":       bool(data.get("show_ai_summary", True)),
        "show_known_facts":      bool(data.get("show_known_facts", True)),
    })

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers
            SET voice_config = %s::jsonb, updated_at = NOW()
            WHERE email = %s
        """, (json.dumps(voice_config), current_user.email))
        rows_updated = cur.rowcount
        conn.commit()
        cur.close()
        if rows_updated == 0:
            logger.error(f"Voice config save matched 0 rows for email={current_user.email!r}")
            return flask_jsonify({"error": "Account not found — please log out and back in"}), 400
        logger.info(f"Voice config saved for {current_user.email}: enabled={voice_config['enabled']}")
        return flask_jsonify({"status": "success", "voice_config": voice_config})
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to save voice config: {e}")
        return flask_jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)


# ── Training integration code ─────────────────────────────────────────────────

@dashboard_bp.route("/api/training/generate-code", methods=["POST"])
@login_required
def generate_training_code():
    """Generate a new training integration token stored in voice_config."""
    result = create_training_token_for_user(current_user.email)
    if "error" in result:
        return flask_jsonify({"error": result["error"]}), 500
    logger.info(f"Training code generated for {current_user.email}")
    return flask_jsonify({
        "status": "success",
        "training_token": result["training_token"],
    })


@dashboard_bp.route("/api/training/revoke-code", methods=["POST"])
@login_required
def revoke_training_code():
    """Revoke the current training integration token."""
    ok = revoke_training_token(current_user.email)
    if not ok:
        return flask_jsonify({"error": "Failed to revoke training code"}), 500
    logger.info(f"Training code revoked for {current_user.email}")
    return flask_jsonify({"status": "success"})


@dashboard_bp.route("/api/training/status", methods=["GET"])
@login_required
def training_code_status():
    """Return the current training token status (masked) from voice_config."""
    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        vc = (row['voice_config'] if row and row['voice_config'] else {}) if row else {}
        token = vc.get("training_token")
        if token:
            return flask_jsonify({
                "has_token": True,
                "training_token": token,
                "created_at": vc.get("training_token_created_at", ""),
            })
        return flask_jsonify({"has_token": False})
    except Exception as e:
        return flask_jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)


# ── Carriers ──────────────────────────────────────────────────────────────────

@dashboard_bp.route("/api/carriers", methods=["GET"])
@login_required
def get_carriers():
    selected = get_contracted_carriers(current_user.email)
    return flask_jsonify({"carriers": CARRIER_LIST, "selected": selected})


@dashboard_bp.route("/api/carriers", methods=["POST"])
@login_required
@require_permission('can_change_bot_config')
def save_carriers():
    data = request.get_json()
    if not data or "carriers" not in data:
        return flask_jsonify({"error": "Missing carriers list"}), 400
    carriers = validate_carrier_keys(data["carriers"])
    ok = save_contracted_carriers(current_user.email, carriers)
    if ok:
        return flask_jsonify({"status": "success", "saved": carriers, "count": len(carriers)})
    return flask_jsonify({"error": "Failed to save carriers"}), 500


# ── Bot settings ──────────────────────────────────────────────────────────────

@dashboard_bp.route("/api/bot-settings", methods=["GET"])
@login_required
def get_bot_settings_api():
    settings = get_bot_settings(current_user.email)
    return flask_jsonify({"settings": settings, "defaults": BOT_SETTINGS_DEFAULTS})


@dashboard_bp.route("/api/bot-settings", methods=["POST"])
@login_required
@require_permission('can_change_bot_config')
def save_bot_settings_api():
    data = request.get_json()
    if not data or "settings" not in data:
        return flask_jsonify({"error": "Missing settings object"}), 400

    incoming = data["settings"]
    clean    = {}
    for key, default_val in BOT_SETTINGS_DEFAULTS.items():
        if key in incoming:
            val = incoming[key]
            if isinstance(default_val, bool):
                clean[key] = bool(val)
            elif isinstance(default_val, int):
                clean[key] = max(0, min(5, int(val)))
            elif isinstance(default_val, list):
                clean[key] = val if isinstance(val, list) else []
            elif isinstance(default_val, str):
                clean[key] = str(val)[:2000]
            else:
                clean[key] = val

    ok = save_bot_settings(current_user.email, clean)
    if ok:
        return flask_jsonify({"status": "success", "saved": clean})
    return flask_jsonify({"error": "Failed to save settings"}), 500


# ── External API key management ───────────────────────────────────────────────

@dashboard_bp.route("/api/generate-key", methods=["POST"])
@login_required
def generate_key_endpoint():
    result = create_api_key_for_user(current_user.email)
    if "error" in result:
        return flask_jsonify({"error": result["error"]}), 500
    return flask_jsonify({
        "status":         "success",
        "api_key":        result["api_key"],
        "webhook_secret": result["webhook_secret"],
        "message":        "Store your API key securely. It will not be shown again in full.",
    })


@dashboard_bp.route("/api/revoke-key", methods=["POST"])
@login_required
def revoke_key_endpoint():
    ok = revoke_api_key(current_user.email)
    if ok:
        return flask_jsonify({"status": "success", "message": "API key revoked."})
    return flask_jsonify({"error": "Failed to revoke key"}), 500


@dashboard_bp.route("/api/webhook-url", methods=["POST"])
@login_required
def save_webhook_url_endpoint():
    data = request.get_json()
    if not data or not data.get("url"):
        return flask_jsonify({"error": "Missing 'url' field"}), 400
    url = data["url"].strip()
    if not url.startswith("https://"):
        return flask_jsonify({"error": "Webhook URL must use HTTPS"}), 400
    ok = save_outbound_webhook_url(current_user.email, url)
    if ok:
        return flask_jsonify({"status": "success", "url": url})
    return flask_jsonify({"error": "Failed to save webhook URL"}), 500


@dashboard_bp.route("/api/api-status", methods=["GET"])
@login_required
def api_status_endpoint():
    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 503
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT api_key, webhook_secret, outbound_webhook_url, api_key_created_at
            FROM subscribers WHERE email = %s LIMIT 1
        """, (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return flask_jsonify({"has_key": False})
        api_key = row.get("api_key") or ""
        return flask_jsonify({
            "has_key":                 bool(api_key),
            "key_prefix":              (api_key[:12] + "..." + api_key[-4:]) if len(api_key) > 16 else "",
            "webhook_url":             row.get("outbound_webhook_url") or "",
            "webhook_secret_preview":  ((row.get("webhook_secret") or "")[:10] + "..."
                                        if row.get("webhook_secret") else ""),
            "created_at":              str(row.get("api_key_created_at") or ""),
        })
    except Exception as e:
        logger.error(f"api_status_endpoint error: {e}")
        return flask_jsonify({"error": "Failed to fetch status"}), 500
    finally:
        return_db_connection(conn)


# ── Bot config save (AJAX) ────────────────────────────────────────────────────

@dashboard_bp.route("/api/save-config", methods=["POST"])
@login_required
@require_permission('can_change_bot_config')
def api_save_config():
    """Save bot configuration via AJAX — returns JSON for overlay feedback."""
    data = request.get_json()
    if not data:
        return safe_jsonify({"success": False, "error": "No data provided"}), 400

    conn = get_db_connection()
    if not conn:
        return safe_jsonify({"success": False, "error": "Database connection failed"}), 500

    try:
        cur = conn.cursor()
        calendar_name = data.get('calendar_name', '')

        sms_send_via = data.get('sms_send_via', 'ghl')

        cur.execute("""
            UPDATE subscribers
            SET calendar_id      = %s,
                calendar_name    = %s,
                crm_user_id      = %s,
                bot_first_name   = %s,
                timezone         = %s,
                initial_message  = %s,
                personal_website = %s,
                sms_send_via     = %s,
                updated_at       = NOW()
            WHERE email = %s
        """, (
            data.get('calendar_id', ''),
            calendar_name,
            data.get('crm_user_id', ''),
            data.get('bot_name', ''),
            data.get('timezone', ''),
            data.get('initial_message', ''),
            data.get('personal_website') or None,
            sms_send_via,
            current_user.email,
            ))

        rows = cur.rowcount
        conn.commit()
        if rows == 0:
            logger.error(
                f"API config save: UPDATE matched 0 rows for {current_user.email} "
                f"(role={current_user.role}). Row may not exist in the target table."
            )
            return safe_jsonify({"success": False, "error": "No matching account found in database"}), 404

        logger.info(f"Config saved via API for {current_user.email} ({rows} row updated)")
        return safe_jsonify({"success": True})

    except Exception as e:
        conn.rollback()
        logger.error(f"API config save failed for {current_user.email}: {e}")
        return safe_jsonify({"success": False, "error": "Internal server error"}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── CRM integrations ──────────────────────────────────────────────────────────

@dashboard_bp.route("/api/integrations/save", methods=["POST"])
@login_required
def save_integration_config():
    """Save CRM integration settings for the logged-in subscriber."""
    data = request.get_json()
    if not data:
        return safe_jsonify({"error": "No data provided"}), 400

    crm_type   = data.get("crm_type", "ghl").strip().lower()
    crm_config = data.get("crm_config", {})

    if crm_type not in CRM_REGISTRY:
        return safe_jsonify({"error": f"Unsupported CRM type: {crm_type}"}), 400

    conn = get_db_connection()
    if not conn:
        return safe_jsonify({"error": "Database error"}), 500

    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers SET crm_type = %s, crm_config = %s, updated_at = NOW()
            WHERE email = %s
        """, (crm_type, json.dumps(crm_config), current_user.email))

        conn.commit()
        logger.info(f"Integration saved: {crm_type} for {current_user.email}")
        return safe_jsonify({"success": True, "crm_type": crm_type})
    except Exception as e:
        logger.error(f"Failed to save integration config: {e}")
        conn.rollback()
        return safe_jsonify({"error": "Failed to save configuration"}), 500
    finally:
        cur.close()
        return_db_connection(conn)


@dashboard_bp.route("/api/integrations/test", methods=["POST"])
@login_required
def test_integration():
    """Test CRM credentials by attempting a validation call."""
    data = request.get_json()
    if not data:
        return safe_jsonify({"error": "No data provided"}), 400

    crm_type   = data.get("crm_type", "ghl").strip().lower()
    crm_config = data.get("crm_config", {})

    subscriber_data = {
        "access_token": crm_config.get("access_token", current_user.access_token or ""),
        "location_id":  current_user.location_id or "",
        "calendar_id":  current_user.calendar_id or "",
        "timezone":     current_user.timezone or "America/Chicago",
        "crm_user_id":  current_user.crm_user_id or "",
        "crm_type":     crm_type,
        "crm_config":   crm_config,
    }

    try:
        adapter = get_crm_adapter(crm_type, subscriber_data)
        result  = adapter.validate_credentials()
        return safe_jsonify(result)
    except Exception as e:
        logger.error(f"Integration test failed: {e}")
        return safe_jsonify({"valid": False, "message": str(e)}), 500


# ── Logs & Alerts ─────────────────────────────────────────────────────────────

@dashboard_bp.route("/api/logs", methods=["GET"])
@login_required
def get_webhook_logs_api():
    """Fetch webhook logs for the current user's location (paginated)."""
    location_id = current_user.location_id
    if not location_id:
        return safe_jsonify({"logs": [], "total": 0})

    limit        = min(int(request.args.get("limit", 50)), 200)
    offset       = int(request.args.get("offset", 0))
    event_type   = request.args.get("event_type", "").strip() or None
    status_filter= request.args.get("status", "").strip() or None

    logs = get_webhook_logs(location_id, limit=limit, offset=offset,
                            event_type=event_type, status=status_filter)
    for log in logs:
        if log.get("created_at"):
            log["created_at"] = log["created_at"].isoformat() + "Z"

    return safe_jsonify({"logs": logs, "total": len(logs)})


@dashboard_bp.route("/api/alerts", methods=["GET"])
@login_required
def api_get_alerts():
    """Fetch undismissed persistent alerts for the current user."""
    alerts = get_persistent_alerts(current_user.email)
    for a in alerts:
        if a.get("created_at"):
            a["created_at"] = a["created_at"].isoformat() + "Z"
    return safe_jsonify({"alerts": alerts})


@dashboard_bp.route("/api/alerts/<int:alert_id>/dismiss", methods=["POST"])
@login_required
def api_dismiss_alert(alert_id):
    """Dismiss a persistent alert."""
    dismiss_persistent_alert(alert_id, current_user.email)
    return safe_jsonify({"success": True})


# ── Onboarding check (OAuth loading screen) ───────────────────────────────────

@dashboard_bp.route("/api/onboarding-check")
@login_required
def onboarding_check():
    """Real-time status check used by the OAuth loading screen."""
    user = User.get(current_user.email)
    if not user:
        return flask_jsonify({"error": "User not found"}), 404

    loc_ok = bool(user.location_id and not str(user.location_id).startswith("temp_"))

    checks = [
        {"key": "location_id",   "label": "Location ID",
         "status": "success" if loc_ok else "pending",
         "value":  user.location_id if loc_ok else None},
        {"key": "user_id",       "label": "CRM User ID",
         "status": "success" if user.crm_user_id else "pending",
         "value":  user.crm_user_id},
        {"key": "access_token",  "label": "Access Token",
         "status": "success" if user.access_token else "pending",
         "value":  "Connected" if user.access_token else None},
        {"key": "refresh_token", "label": "Recovery Token",
         "status": "success" if user.refresh_token else "pending",
         "value":  "Connected" if user.refresh_token else None},
        {"key": "calendars",     "label": "Calendars",
         "status": "success" if user.calendar_id else "pending",
         "value":  user.calendar_name if user.calendar_id else "Available after dashboard config"},
    ]

    core_keys    = ["location_id", "user_id", "access_token", "refresh_token"]
    all_connected= all(c["status"] == "success" for c in checks if c["key"] in core_keys)

    return flask_jsonify({"checks": checks, "all_connected": all_connected, "email": user.email})


# ── Trust Hub status (lightweight AJAX for dashboard load) ──────────────────

@dashboard_bp.route("/api/trust-hub-status")
@login_required
def api_trust_hub_status():
    """Lightweight carrier verification status check.
    Called by frontend on dashboard load to show current Trust Hub state.
    Makes a single Twilio API call if profile_sid exists."""
    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"status": "error", "message": "DB unavailable"}), 503
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT voice_config, twilio_sub_account_sid, twilio_sub_account_auth_token
            FROM subscribers WHERE email = %s
        """, (current_user.email,))
        row = cur.fetchone()
        if not row:
            return flask_jsonify({"status": "not_registered"})

        vc = row['voice_config'] or {}
        trust_hub = vc.get('trust_hub', {})
        profile_sid = trust_hub.get('profile_sid', '')

        if not profile_sid:
            return flask_jsonify({"status": "not_registered"})

        sub_sid = row['twilio_sub_account_sid'] or vc.get('twilio_sub_account_sid', '')
        sub_auth = row['twilio_sub_account_auth_token'] or vc.get('twilio_auth_token', '')

        if not sub_sid:
            return flask_jsonify({
                "status": trust_hub.get('review_status', 'unknown'),
                "profile_sid": profile_sid,
                "message": "Sub-account not provisioned",
            })

        # Single Twilio API call to check live status
        import twilio_provisioning as tp
        result = tp.check_secondary_profile_status(
            sub_account_sid=sub_sid,
            sub_account_auth_token=sub_auth,
            profile_sid=profile_sid,
        )
        new_status = result.get('status', '')
        old_status = trust_hub.get('review_status', '')

        # Persist if changed
        if new_status and new_status != old_status:
            trust_hub['review_status'] = new_status
            vc['trust_hub'] = trust_hub
            cur.execute("UPDATE subscribers SET voice_config = %s WHERE email = %s",
                        (json.dumps(vc), current_user.email))
            conn.commit()

        return flask_jsonify({
            "status": new_status or old_status or "unknown",
            "profile_sid": profile_sid,
            "message": result.get('message', ''),
            "business_name": trust_hub.get('business_name', ''),
            "submitted_at": trust_hub.get('registered_at', ''),
            "cnam_status": vc.get('cnam', {}).get('status', 'not_registered'),
            "voice_integrity_status": vc.get('number_integrity', {}).get('status', 'not_registered'),
        })
    except Exception as e:
        logger.error(f"[TrustHubStatus] Error for {current_user.email}: {e}")
        return flask_jsonify({
            "status": "error",
            "message": "Could not check carrier verification status",
        })
    finally:
        return_db_connection(conn)


# ── Language preference ──────────────────────────────────────────────────────

@dashboard_bp.route("/api/set-language", methods=["POST"])
@login_required
def api_set_language():
    """Save the user's preferred language. Called from the topbar language picker."""
    data = request.get_json(silent=True) or {}
    lang = data.get("language", "en")
    if lang not in SUPPORTED_LANGUAGES:
        lang = "en"
    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"ok": False}), 500
    result = None
    try:
        cur = conn.cursor()
        cur.execute("UPDATE subscribers SET preferred_language = %s WHERE email = %s",
                    (lang, current_user.email))
        conn.commit()
        cur.close()
        result = flask_jsonify({"ok": True, "language": lang})
    except Exception:
        conn.rollback()
        result = flask_jsonify({"ok": False}), 500
    finally:
        return_db_connection(conn)
    return result


# ── CRM auto-detect ──────────────────────────────────────────────────────────

@dashboard_bp.route("/api/integrations/detect", methods=["POST"])
@login_required
def detect_crm_from_key():
    """Auto-detect CRM type from an API key by probing known endpoints."""
    data = request.get_json()
    if not data or not data.get("api_key"):
        return safe_jsonify({"detected": False, "error": "No API key provided"}), 400

    api_key = data["api_key"].strip()

    # Try HubSpot (Private App tokens start with "pat-")
    if api_key.startswith("pat-") or api_key.startswith("eu1-"):
        try:
            r = requests.get("https://api.hubapi.com/crm/v3/objects/contacts",
                             headers={"Authorization": f"Bearer {api_key}"},
                             params={"limit": 1}, timeout=10)
            if r.status_code == 200:
                return safe_jsonify({"detected": True, "crm_type": "hubspot",
                                     "label": "HubSpot", "field": "access_token"})
        except Exception:
            pass

    # Try Pipedrive (40-char hex tokens)
    try:
        r = requests.get("https://api.pipedrive.com/api/v1/users/me",
                         params={"api_token": api_key}, timeout=10)
        if r.status_code == 200 and r.json().get("success"):
            company = r.json().get("data", {}).get("company_domain", "")
            return safe_jsonify({"detected": True, "crm_type": "pipedrive",
                                 "label": "Pipedrive", "field": "api_token",
                                 "extra": {"company_domain": company}})
    except Exception:
        pass

    # Try HubSpot (non-pat tokens — OAuth or legacy)
    try:
        r = requests.get("https://api.hubapi.com/crm/v3/objects/contacts",
                         headers={"Authorization": f"Bearer {api_key}"},
                         params={"limit": 1}, timeout=10)
        if r.status_code == 200:
            return safe_jsonify({"detected": True, "crm_type": "hubspot",
                                 "label": "HubSpot", "field": "access_token"})
    except Exception:
        pass

    return safe_jsonify({"detected": False, "message": "Could not auto-detect CRM. Please select manually."})
