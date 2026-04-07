# blueprints/agency.py — Agency owner dashboard and sub-user management
#
# Routes:
#   GET|POST /agency-dashboard              — Agency owner main dashboard
#   GET|POST /agency-login                  — Agency owner login
#   POST /api/agency/invite-sub-user        — Invite a sub-account user
#   POST /api/agency/resend-invite          — Re-send invite email
#   POST /api/agency/invite-all             — Invite all pending sub-users
#   GET  /api/agency/logs/<location_id>     — Get logs for a sub-account
#   GET  /api/agency/kpis                   — Aggregated KPIs across all sub-users
#   GET  /api/agency/agent-stats            — Per-agent stats breakdown
#   GET  /api/agency/call-log               — Paginated call log across all agents

import logging
from datetime import datetime, timedelta

import pytz
from flask import (Blueprint, request, render_template, redirect,
                   url_for, flash, session, current_app)
from flask import jsonify as flask_jsonify
from flask_login import login_required, login_user, current_user
from flask_mail import Message
from itsdangerous import URLSafeTimedSerializer
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash

from extensions import ADMIN_EMAILS, YOUR_DOMAIN, safe_jsonify, mail
from db import (get_db_connection, return_db_connection,
                get_contracted_carriers, get_bot_settings,
                get_webhook_logs, User)
from forms import LoginForm, ConfigForm
from carrier_list import CARRIER_LIST
from db import BOT_SETTINGS_DEFAULTS

logger = logging.getLogger(__name__)

agency_bp = Blueprint('agency', __name__)


# ── Private helpers ───────────────────────────────────────────────────────────

def _send_invite_email(to_email: str, agent_name: str, agency_name: str, invite_url: str):
    """Send the onboarding invite email to a sub-account user via Flask-Mail."""
    from email_templates import _build_agency_invite_html
    html_body, text_body = _build_agency_invite_html(agent_name, agency_name, invite_url, YOUR_DOMAIN, recipient_email=to_email)
    msg = Message(
        subject=f"You're invited to Omnisconn by {agency_name}",
        recipients=[to_email],
        html=html_body,
        body=text_body,
    )
    mail.send(msg)
    logger.info(f"Invite email sent to {to_email}")


# ── Agency Dashboard ──────────────────────────────────────────────────────────

@agency_bp.route("/agency-dashboard", methods=["GET", "POST"])
@login_required
def agency_dashboard():
    """Agency owner dashboard — renders the SAME dashboard.html used by individuals,
    with is_agency=True so the sidebar shows agency-specific sections (Members, KPIs,
    White Label) alongside all individual features (Dialer, SMS Config, Voice, etc.)."""
    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]

    if current_user.role != 'agency_owner' and not is_admin:
        flash("Access restricted to agency owners only.", "error")
        return redirect("/dashboard")

    # Agency owners are FREE — no subscription paywall. They download the app
    # to appear on their agents' GHL sidebars. Agents pay for their own plans.

    form = ConfigForm()

    # ── Form save (POST) — updates BOTH subscribers and agency_billing ─────────
    conn = get_db_connection()

    if request.method == 'POST' and not form.validate_on_submit():
        logger.warning(f"Agency form validation failed for {current_user.email}: {form.errors}")
        flash("Please fill in all required fields.", "error")

    if form.validate_on_submit():
        if not conn:
            flash("Database connection failed", "error")
        else:
            try:
                cur = conn.cursor()
                calendar_name = request.form.get('calendar_name', '')
                params = (
                    form.location_id.data,
                    form.calendar_id.data,
                    calendar_name,
                    form.crm_user_id.data,
                    form.bot_name.data,
                    form.timezone.data,
                    form.initial_message.data,
                    form.personal_website.data or None,
                )
                # Update subscribers (operational data used by dialer, voice, webhooks)
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
                """, (*params, current_user.email))
                # Also update agency_billing (keeps agency-specific table in sync)
                cur.execute("""
                    UPDATE agency_billing
                    SET location_id      = %s,
                        calendar_id      = %s,
                        calendar_name    = %s,
                        crm_user_id      = %s,
                        bot_first_name   = %s,
                        timezone         = %s,
                        initial_message  = %s,
                        personal_website = %s,
                        updated_at       = NOW()
                    WHERE agency_email = %s
                """, (*params, current_user.email))
                conn.commit()
                flash("Settings saved successfully!", "success")
                return redirect(url_for('agency.agency_dashboard'))
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

    # ── Token display — same as individual dashboard ──────────────────────────
    from token_encryption import decrypt_token
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

    # Return DB connection if still held
    if conn:
        return_db_connection(conn)
        conn = None

    selected_carriers = get_contracted_carriers(current_user.email)
    bot_settings      = get_bot_settings(current_user.email)
    voice_config      = current_user.voice_config or {}

    # Auto-sync primary phone number (same as individual dashboard)
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
        except Exception as _e:
            logger.warning(f"[agency-dashboard] Primary number auto-sync failed: {_e}")

    # GHL embed mode
    embed_mode = request.args.get('embed') == '1'
    initial_tab = request.args.get('tab', 'voicedialer') if embed_mode else ''
    embed_contact_id = request.args.get('contact_id', '') if embed_mode else ''
    embed_dial_contacts = request.args.get('dial_contacts', '') if embed_mode else ''

    # White-label branding
    from db import get_whitelabel_for_user
    whitelabel = get_whitelabel_for_user(current_user)

    # CRM config fields for integrations tab
    from crm_adapters.factory import CRM_CONFIG_FIELDS, CRM_DISPLAY_NAMES

    # Business profile onboarding gate — agency owners must also complete
    from blueprints.dashboard import _check_needs_onboarding
    needs_onboarding = _check_needs_onboarding(current_user, voice_config)

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
        is_agency=True,
    )


@agency_bp.route("/agency-login", methods=["GET", "POST"])
def agency_login():
    if current_user.is_authenticated:
        if current_user.role == 'agency_owner':
            return redirect(url_for('agency.agency_dashboard'))
        flash("You're already logged in as a standard user. Use the agent dashboard.", "info")
        return redirect(url_for('dashboard.dashboard'))

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user  = User.get_from_agency(email)

        if not user:
            flash("No account found with that email.", "error")
            logger.info(f"Agency login attempt — email not found: {email}")
            return render_template("agency-login.html", form=form)

        if not check_password_hash(user.password_hash, form.password.data):
            flash("Incorrect password.", "error")
            logger.warning(f"Agency login failed — wrong password for {email}")
            return render_template("agency-login.html", form=form)

        if user.role != 'agency_owner':
            flash("Access Denied: This portal is for agency owners only. "
                  "Please use the standard login.", "error")
            logger.info(f"Non-agency user attempted agency login: {email} (role: {user.role})")
            return redirect(url_for('auth.login'))

        login_user(user, remember=form.remember.data)
        logger.info(f"Agency owner logged in successfully: {email}")

        next_url = request.args.get('next')
        if next_url and '//' not in next_url and next_url.startswith('/'):
            return redirect(next_url)
        return redirect(url_for('agency.agency_dashboard'))

    return render_template("agency-login.html", form=form)


# ── Sub-user invitation endpoints ─────────────────────────────────────────────

@agency_bp.route("/api/agency/invite-sub-user", methods=["POST"])
@login_required
def invite_sub_user():
    """Invite a sub-account user to create their login credentials."""
    if current_user.role != 'agency_owner':
        return flask_jsonify({"error": "Access denied"}), 403

    data        = request.get_json()
    location_id = data.get("location_id")
    target_email= data.get("email")

    if not location_id:
        return flask_jsonify({"error": "Missing location_id"}), 400

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT location_id, full_name, agent_email, onboarding_status
            FROM subscribers
            WHERE location_id = %s AND parent_agency_email = %s
        """, (location_id, current_user.email))
        sub = cur.fetchone()

        if not sub:
            return flask_jsonify({"error": "Location not found or not owned by you"}), 404

        invite_email = target_email or sub['agent_email']
        if not invite_email:
            return flask_jsonify({"error": "No email found for this location. Please provide one."}), 400

        if sub['onboarding_status'] == 'claimed':
            return flask_jsonify({"error": "This user has already claimed their account"}), 400

        # Generate signed 24-hour invite token containing location_id
        serializer = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
        invite_token = serializer.dumps(location_id)

        cur.execute("""
            UPDATE subscribers
            SET agent_email       = %s,
                invite_token      = %s,
                invite_sent_at    = NOW(),
                onboarding_status = 'invited',
                updated_at        = NOW()
            WHERE location_id = %s
        """, (invite_email, invite_token, location_id))
        conn.commit()

        invite_url = f"{YOUR_DOMAIN}/claim-account?token={invite_token}"

        try:
            _send_invite_email(
                to_email=invite_email,
                agent_name=sub['full_name'],
                agency_name=current_user.full_name or "Your Agency",
                invite_url=invite_url,
            )
        except Exception as email_err:
            logger.error(f"Email send failed: {email_err}")
            return flask_jsonify({
                "status":     "partial",
                "message":    "Invite created but email failed to send",
                "invite_url": invite_url,
            })

        return flask_jsonify({"status": "success", "message": f"Invite sent to {invite_email}"})

    except Exception as e:
        conn.rollback()
        logger.error(f"Invite sub-user error: {e}")
        return flask_jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close()
        return_db_connection(conn)


@agency_bp.route("/api/agency/resend-invite", methods=["POST"])
@login_required
def resend_invite():
    """Re-send the invite email with a fresh token to a sub-account user."""
    if current_user.role != 'agency_owner':
        return flask_jsonify({"error": "Access denied"}), 403

    data        = request.get_json()
    location_id = data.get("location_id")
    if not location_id:
        return flask_jsonify({"error": "Missing location_id"}), 400

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT location_id, full_name, agent_email, invite_token, onboarding_status
            FROM subscribers
            WHERE location_id = %s AND parent_agency_email = %s
        """, (location_id, current_user.email))
        sub = cur.fetchone()

        if not sub:
            return flask_jsonify({"error": "Location not found"}), 404
        if sub['onboarding_status'] == 'claimed':
            return flask_jsonify({"error": "User has already claimed their account"}), 400
        if not sub['agent_email']:
            return flask_jsonify({"error": "No email on file for this user"}), 400

        new_token = secrets.token_urlsafe(32)
        cur.execute("""
            UPDATE subscribers
            SET invite_token      = %s,
                invite_sent_at    = NOW(),
                onboarding_status = 'invited',
                updated_at        = NOW()
            WHERE location_id = %s
        """, (new_token, location_id))
        conn.commit()

        invite_url = f"{YOUR_DOMAIN}/claim-account?token={new_token}"

        try:
            _send_invite_email(
                to_email=sub['agent_email'],
                agent_name=sub['full_name'],
                agency_name=current_user.full_name or "Your Agency",
                invite_url=invite_url,
            )
        except Exception as email_err:
            logger.error(f"Resend email failed: {email_err}")
            return flask_jsonify({
                "status":     "partial",
                "message":    "Token refreshed but email failed",
                "invite_url": invite_url,
            })

        return flask_jsonify({"status": "success",
                              "message": f"Invite re-sent to {sub['agent_email']}"})

    except Exception as e:
        conn.rollback()
        logger.error(f"Resend invite error: {e}")
        return flask_jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close()
        return_db_connection(conn)


@agency_bp.route("/api/agency/invite-all", methods=["POST"])
@login_required
def invite_all_sub_users():
    """Invite all pending sub-account users who haven't been invited yet."""
    if current_user.role != 'agency_owner':
        return flask_jsonify({"error": "Access denied"}), 403

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT location_id, full_name, agent_email
            FROM subscribers
            WHERE parent_agency_email = %s
              AND onboarding_status = 'pending'
              AND agent_email IS NOT NULL
        """, (current_user.email,))
        pending = cur.fetchall()

        if not pending:
            return flask_jsonify({"status": "info", "message": "No pending users with emails found"})

        invited_count = 0
        failed_count  = 0

        for sub in pending:
            try:
                # Generate signed 24-hour invite token containing location_id
                serializer = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
                invite_token = serializer.dumps(sub['location_id'])
                cur.execute("""
                    UPDATE subscribers
                    SET invite_token      = %s,
                        invite_sent_at    = NOW(),
                        onboarding_status = 'invited',
                        updated_at        = NOW()
                    WHERE location_id = %s
                """, (invite_token, sub['location_id']))

                invite_url = f"{YOUR_DOMAIN}/claim-account?token={invite_token}"
                _send_invite_email(
                    to_email=sub['agent_email'],
                    agent_name=sub['full_name'],
                    agency_name=current_user.full_name or "Your Agency",
                    invite_url=invite_url,
                )
                invited_count += 1

            except Exception as e:
                logger.error(f"Failed to invite {sub['agent_email']}: {e}")
                failed_count += 1

        conn.commit()
        return flask_jsonify({
            "status":  "success",
            "invited": invited_count,
            "failed":  failed_count,
            "message": f"Invited {invited_count} users ({failed_count} failed)",
        })

    except Exception as e:
        conn.rollback()
        logger.error(f"Bulk invite error: {e}")
        return flask_jsonify({"error": "Internal server error"}), 500
    finally:
        cur.close()
        return_db_connection(conn)


@agency_bp.route("/api/agency/logs/<location_id>", methods=["GET"])
@login_required
def get_agency_logs(location_id):
    """Fetch webhook logs for a specific sub-account (agency owners only)."""
    if current_user.role != 'agency_owner':
        return flask_jsonify({"error": "Access denied"}), 403

    # Verify this location belongs to the requesting agency owner
    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Service temporarily unavailable"}), 503
    try:
        _cur = conn.cursor()
        _cur.execute(
            "SELECT 1 FROM subscribers WHERE location_id = %s AND parent_agency_email = %s LIMIT 1",
            (location_id, current_user.email)
        )
        if not _cur.fetchone():
            # Also allow if this is the owner's own location_id
            if location_id != getattr(current_user, 'location_id', None):
                _cur.close()
                return flask_jsonify({"error": "Access denied"}), 403
        _cur.close()
    finally:
        return_db_connection(conn)

    try:
        limit    = min(int(request.args.get("limit", 50)), 200)
        offset   = int(request.args.get("offset", 0))
    except (ValueError, TypeError):
        limit, offset = 50, 0
    event_type   = request.args.get("event_type", "").strip() or None
    status_filter= request.args.get("status", "").strip() or None

    logs = get_webhook_logs(location_id, limit=limit, offset=offset,
                            event_type=event_type, status=status_filter)
    for log in logs:
        if log.get("created_at"):
            log["created_at"] = log["created_at"].isoformat() + "Z"

    return safe_jsonify({"logs": logs, "total": len(logs)})


# ── Agency KPI & Stats API Endpoints ─────────────────────────────────────────

def _get_sub_location_ids(conn, agency_email, company_id=None):
    """Return list of location_ids for all sub-accounts of this agency owner.
    Uses both company_id AND parent_agency_email (OR) to match all members."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    if company_id:
        cur.execute(
            "SELECT location_id FROM subscribers "
            "WHERE (company_id = %s OR LOWER(parent_agency_email) = LOWER(%s)) "
            "AND LOWER(email) != LOWER(%s)",
            (company_id, agency_email, agency_email)
        )
    else:
        cur.execute(
            "SELECT location_id FROM subscribers "
            "WHERE LOWER(parent_agency_email) = LOWER(%s) "
            "AND LOWER(email) != LOWER(%s)",
            (agency_email, agency_email)
        )
    ids = [r['location_id'] for r in cur.fetchall() if r['location_id']]
    cur.close()
    return ids


def _get_period_range(period, tz_name='America/Chicago'):
    """Return (start_utc, end_utc, days, now).
    end_utc is the exclusive upper bound for bounded periods (yesterday, two_days_ago, last_week),
    or None for open-ended periods (today, this_week, month, etc.).
    """
    try:
        user_tz = pytz.timezone(tz_name)
    except Exception:
        user_tz = pytz.timezone('America/Chicago')
    now = datetime.now(user_tz)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_utc = None  # None = open-ended (no upper bound)

    if period == 'today':
        start = today_midnight
        days = 1
    elif period == 'yesterday':
        start = today_midnight - timedelta(days=1)
        end_utc = today_midnight
        days = 1
    elif period == 'two_days_ago':
        start = today_midnight - timedelta(days=2)
        end_utc = today_midnight - timedelta(days=1)
        days = 1
    elif period == 'this_week':
        days_since_monday = now.weekday()  # 0=Mon, 6=Sun
        start = today_midnight - timedelta(days=days_since_monday)
        days = max(1, days_since_monday + 1)
    elif period == 'last_week':
        days_since_monday = now.weekday()
        this_monday = today_midnight - timedelta(days=days_since_monday)
        start = this_monday - timedelta(days=7)
        end_utc = this_monday
        days = 7
    elif period == 'week':
        start = now - timedelta(days=7)
        days = 7
    elif period == 'this_month':
        start = today_midnight.replace(day=1)
        days = max(1, now.day)
    elif period == 'month':
        start = now - timedelta(days=30)
        days = 30
    elif period == 'this_year':
        start = today_midnight.replace(month=1, day=1)
        days = max(1, (today_midnight - today_midnight.replace(month=1, day=1)).days + 1)
    elif period == 'year':
        start = now - timedelta(days=365)
        days = 365
    else:  # 'all'
        start = datetime(2000, 1, 1, tzinfo=pytz.utc)
        days = 0

    start_utc = start.astimezone(pytz.utc) if start.tzinfo else pytz.utc.localize(start)
    if end_utc is not None:
        end_utc = end_utc.astimezone(pytz.utc) if end_utc.tzinfo else pytz.utc.localize(end_utc)
    return start_utc, end_utc, days, now


@agency_bp.route("/api/agency/kpis")
@login_required
def agency_kpis():
    """Aggregated KPIs across all sub-accounts for the agency owner."""
    if current_user.role != 'agency_owner' and current_user.email.lower() not in [e.lower() for e in ADMIN_EMAILS]:
        return flask_jsonify({"error": "Access denied"}), 403

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "DB unavailable"}), 503

    try:
        location_ids = _get_sub_location_ids(conn, current_user.email,
                                              company_id=getattr(current_user, 'company_id', None))
        # Also include the owner's own location_id
        if current_user.location_id and current_user.location_id not in location_ids:
            location_ids.append(current_user.location_id)

        if not location_ids:
            return flask_jsonify({
                "total_calls": 0, "connected_calls": 0, "connect_rate": 0,
                "total_duration": 0, "avg_duration": 0, "total_messages": 0,
                "active_agents": 0, "outbound_calls": 0, "inbound_calls": 0,
                "over_1min": 0, "over_5min": 0, "unique_contacts": 0,
                "calls_per_day": 0, "daily": [], "hourly": [], "prior": None,
            })

        period = request.args.get('period', 'month')
        tz_name = (current_user.timezone or 'America/Chicago').replace(' ', '_')
        start_utc, end_utc, days, now = _get_period_range(period, tz_name)
        far_future = datetime(2099, 12, 31, tzinfo=pytz.utc)
        end_param = end_utc if end_utc else far_future

        # Optional per-agent filter
        agent_filter = request.args.get('agent', '').strip()
        if agent_filter and agent_filter in location_ids:
            location_ids = [agent_filter]

        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Core call KPIs
        cur.execute("""
            SELECT
                COUNT(*)                                                      AS total_calls,
                COUNT(*) FILTER (WHERE direction = 'outbound')                AS outbound_calls,
                COUNT(*) FILTER (WHERE direction = 'inbound')                 AS inbound_calls,
                COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected_calls,
                COALESCE(AVG(duration) FILTER (WHERE duration > 0), 0)        AS avg_duration,
                COALESCE(SUM(duration), 0)                                    AS total_duration,
                COUNT(*) FILTER (WHERE duration >= 60)                        AS over_1min,
                COUNT(*) FILTER (WHERE duration >= 300)                       AS over_5min,
                COUNT(DISTINCT contact_id)                                    AS unique_contacts
            FROM call_history
            WHERE location_id = ANY(%s) AND created_at >= %s AND created_at < %s
        """, (location_ids, start_utc, end_param))
        r = cur.fetchone()
        total = r['total_calls'] or 0
        connected = r['connected_calls'] or 0
        connect_rate = round(connected / total * 100, 1) if total else 0.0

        # Outbound SMS count only (message_type='assistant')
        cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM contact_messages cm
            JOIN contact_cache cc ON cm.contact_id = cc.contact_id
            WHERE cc.location_id = ANY(%s) AND cm.created_at >= %s AND cm.created_at < %s
              AND cm.message_type = 'assistant'
        """, (location_ids, start_utc, end_param))
        total_messages = cur.fetchone()['cnt'] or 0

        # Active agents (have at least 1 call or message in period)
        cur.execute("""
            SELECT COUNT(DISTINCT location_id) AS cnt
            FROM call_history
            WHERE location_id = ANY(%s) AND created_at >= %s AND created_at < %s
        """, (location_ids, start_utc, end_param))
        active_agents = cur.fetchone()['cnt'] or 0

        # Days for per-day calc
        if days == 0:
            cur.execute("SELECT MIN(created_at) AS first FROM call_history WHERE location_id = ANY(%s)", (location_ids,))
            first = cur.fetchone()['first']
            if first:
                if first.tzinfo is None:
                    first = pytz.utc.localize(first)
                days = max(1, (now - first).days)
            else:
                days = 1

        # Prior period comparison
        prior = None
        if period != 'all':
            period_len = now - start_utc.astimezone(pytz.timezone(tz_name))
            prior_end = start_utc
            prior_start = start_utc - period_len
            cur.execute("""
                SELECT
                    COUNT(*) AS total_calls,
                    COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected_calls,
                    COALESCE(SUM(duration), 0) AS total_duration
                FROM call_history
                WHERE location_id = ANY(%s) AND created_at >= %s AND created_at < %s
            """, (location_ids, prior_start, prior_end))
            pr = cur.fetchone()
            p_total = pr['total_calls'] or 0
            p_connected = pr['connected_calls'] or 0
            p_dur = int(pr['total_duration'] or 0)

            def _pct(curr, prev):
                return round((curr - prev) / prev * 100, 1) if prev else None

            prior = {
                "delta_calls": _pct(total, p_total),
                "delta_connected": _pct(connected, p_connected),
                "delta_duration": _pct(int(r['total_duration'] or 0), p_dur),
            }

        # Daily volume
        cur.execute("""
            SELECT DATE(created_at AT TIME ZONE 'UTC' AT TIME ZONE %s) AS day,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected,
                   COALESCE(SUM(duration), 0) AS total_secs
            FROM call_history
            WHERE location_id = ANY(%s) AND created_at >= %s AND created_at < %s
            GROUP BY day ORDER BY day
        """, (tz_name, location_ids, start_utc, end_param))
        daily = [
            {"day": str(row['day']), "calls": row['calls'], "connected": row['connected'], "total_secs": row['total_secs']}
            for row in cur.fetchall()
        ]

        # Hourly distribution
        cur.execute("""
            SELECT EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC' AT TIME ZONE %s)::int AS hr,
                   COUNT(*) AS calls
            FROM call_history
            WHERE location_id = ANY(%s) AND created_at >= %s AND created_at < %s
            GROUP BY hr ORDER BY hr
        """, (tz_name, location_ids, start_utc, end_param))
        hourly_map = {row['hr']: row['calls'] for row in cur.fetchall()}
        hourly = [{"hour": h, "calls": hourly_map.get(h, 0)} for h in range(24)]

        cur.close()
        return flask_jsonify({
            "period": period,
            "total_calls": total,
            "outbound_calls": r['outbound_calls'] or 0,
            "inbound_calls": r['inbound_calls'] or 0,
            "connected_calls": connected,
            "connect_rate": connect_rate,
            "avg_duration": round(float(r['avg_duration'] or 0), 1),
            "total_duration": int(r['total_duration'] or 0),
            "over_1min": r['over_1min'] or 0,
            "over_5min": r['over_5min'] or 0,
            "unique_contacts": r['unique_contacts'] or 0,
            "total_messages": total_messages,
            "active_agents": active_agents,
            "total_agents": len(location_ids),
            "calls_per_day": round(total / days, 1),
            "daily": daily,
            "hourly": hourly,
            "prior": prior,
        })
    except Exception as e:
        logger.error(f"agency_kpis error: {e}", exc_info=True)
        return flask_jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)


@agency_bp.route("/api/agency/agent-stats")
@login_required
def agency_agent_stats():
    """Per-agent stats breakdown for the agency dashboard."""
    if current_user.role != 'agency_owner' and current_user.email.lower() not in [e.lower() for e in ADMIN_EMAILS]:
        return flask_jsonify({"error": "Access denied"}), 403

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "DB unavailable"}), 503

    try:
        period = request.args.get('period', 'month')
        tz_name = (current_user.timezone or 'America/Chicago').replace(' ', '_')
        start_utc, end_utc, _, _ = _get_period_range(period, tz_name)
        far_future = datetime(2099, 12, 31, tzinfo=pytz.utc)
        end_param = end_utc if end_utc else far_future

        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Get sub-accounts with their names (prefer company_id, fallback to parent_agency_email)
        _cid = getattr(current_user, 'company_id', None)
        if _cid:
            cur.execute("""
                SELECT location_id, full_name, email, bot_first_name
                FROM subscribers
                WHERE company_id = %s
            """, (_cid,))
        else:
            cur.execute("""
                SELECT location_id, full_name, email, bot_first_name
                FROM subscribers
                WHERE parent_agency_email = %s
            """, (current_user.email,))
        subs = {r['location_id']: r for r in cur.fetchall()}
        location_ids = list(subs.keys())

        if not location_ids:
            cur.close()
            return flask_jsonify({"agents": []})

        # Per-agent call stats
        cur.execute("""
            SELECT location_id,
                   COUNT(*) AS total_calls,
                   COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected,
                   COALESCE(SUM(duration), 0) AS total_secs,
                   COALESCE(AVG(duration) FILTER (WHERE duration > 0), 0) AS avg_dur,
                   MAX(created_at) AS last_call
            FROM call_history
            WHERE location_id = ANY(%s) AND created_at >= %s AND created_at < %s
            GROUP BY location_id
        """, (location_ids, start_utc, end_param))
        call_stats = {r['location_id']: r for r in cur.fetchall()}

        # Per-agent outbound SMS counts only (message_type='assistant' = bot outbound texts)
        cur.execute("""
            SELECT cc.location_id, COUNT(*) AS cnt
            FROM contact_messages cm
            JOIN contact_cache cc ON cm.contact_id = cc.contact_id
            WHERE cc.location_id = ANY(%s) AND cm.created_at >= %s AND cm.created_at < %s
              AND cm.message_type = 'assistant'
            GROUP BY cc.location_id
        """, (location_ids, start_utc, end_param))
        msg_stats = {r['location_id']: r['cnt'] for r in cur.fetchall()}

        agents = []
        for lid in location_ids:
            sub = subs[lid]
            cs = call_stats.get(lid, {})
            agents.append({
                "location_id": lid,
                "name": sub['full_name'] or sub['email'] or 'Unknown',
                "email": sub['email'] or '',
                "bot_name": sub['bot_first_name'] or '',
                "total_calls": cs.get('total_calls', 0) or 0,
                "connected": cs.get('connected', 0) or 0,
                "total_secs": int(cs.get('total_secs', 0) or 0),
                "avg_duration": round(float(cs.get('avg_dur', 0) or 0), 1),
                "last_call": str(cs['last_call']) if cs.get('last_call') else None,
                "messages": msg_stats.get(lid, 0),
            })

        # Sort by total calls descending
        agents.sort(key=lambda a: a['total_calls'], reverse=True)

        cur.close()
        return flask_jsonify({"agents": agents})
    except Exception as e:
        logger.error(f"agency_agent_stats error: {e}", exc_info=True)
        return flask_jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)


@agency_bp.route("/api/agency/call-log")
@login_required
def agency_call_log():
    """Paginated call log across all agents for the agency dashboard."""
    if current_user.role != 'agency_owner' and current_user.email.lower() not in [e.lower() for e in ADMIN_EMAILS]:
        return flask_jsonify({"error": "Access denied"}), 403

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "DB unavailable"}), 503

    try:
        location_ids = _get_sub_location_ids(conn, current_user.email,
                                              company_id=getattr(current_user, 'company_id', None))
        if current_user.location_id and current_user.location_id not in location_ids:
            location_ids.append(current_user.location_id)
        if not location_ids:
            return flask_jsonify({"calls": [], "total": 0})

        limit = min(int(request.args.get('limit', 50)), 200)
        offset = int(request.args.get('offset', 0))
        agent_filter = request.args.get('agent', '').strip() or None

        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Build query
        where = "ch.location_id = ANY(%s)"
        params = [location_ids]
        if agent_filter:
            where += " AND ch.location_id = %s"
            params.append(agent_filter)

        cur.execute(f"""
            SELECT ch.call_sid, ch.location_id, ch.contact_id, ch.contact_name,
                   ch.direction, ch.status, ch.duration, ch.from_number, ch.phone,
                   ch.created_at, ch.recording_url, ch.transcript,
                   COALESCE(ch.stir_status, '') AS stir_status,
                   s.full_name AS agent_name, s.email AS agent_email
            FROM call_history ch
            LEFT JOIN subscribers s ON ch.location_id = s.location_id
            WHERE {where}
            ORDER BY ch.created_at DESC
            LIMIT %s OFFSET %s
        """, params + [limit, offset])
        calls = []
        for row in cur.fetchall():
            calls.append({
                "call_sid": row['call_sid'],
                "agent_name": row['agent_name'] or row['agent_email'] or 'Unknown',
                "agent_email": row['agent_email'] or '',
                "location_id": row['location_id'],
                "contact_name": row['contact_name'] or 'Unknown',
                "direction": row['direction'],
                "status": row['status'],
                "duration": row['duration'] or 0,
                "from_number": row['from_number'],
                "to_number": row.get('phone', ''),
                "created_at": row['created_at'].isoformat() + "Z" if row['created_at'] else None,
                "recording_url": row['recording_url'] or '',
                "has_recording": bool(row['recording_url']),
                "transcript": row['transcript'] if row['transcript'] else [],
                "has_transcript": bool(row['transcript']),
                "stir_status": row['stir_status'] or '',
            })

        # Total count
        cur.execute(f"SELECT COUNT(*) AS cnt FROM call_history ch WHERE {where}", params)
        total = cur.fetchone()['cnt']

        cur.close()
        return flask_jsonify({"calls": calls, "total": total})
    except Exception as e:
        logger.error(f"agency_call_log error: {e}")
        return flask_jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)


# ── Agency Dashboard Stats (comprehensive insurance KPIs) ────────────────────

@agency_bp.route("/api/agency/dashboard-stats")
@login_required
def agency_dashboard_stats():
    """Comprehensive agency-wide statistics for the tiled dashboard.
    Returns insurance-specific KPIs: connect rate, duration buckets,
    daily dials per agent, top performers, speed to lead, and more."""
    if current_user.role != 'agency_owner' and current_user.email.lower() not in [e.lower() for e in ADMIN_EMAILS]:
        return flask_jsonify({"error": "Access denied"}), 403

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "DB unavailable"}), 503

    try:
        company_id = getattr(current_user, 'company_id', None)
        location_ids = _get_sub_location_ids(conn, current_user.email,
                                              company_id=company_id)
        if current_user.location_id and current_user.location_id not in location_ids:
            location_ids.append(current_user.location_id)

        logger.info(
            f"agency_dashboard_stats: email={current_user.email} company_id={company_id} "
            f"location_ids={location_ids} (count={len(location_ids)})"
        )

        if not location_ids:
            return flask_jsonify(_empty_dashboard_stats())

        period = request.args.get('period', 'month')
        tz_name = (current_user.timezone or 'America/Chicago').replace(' ', '_')
        start_utc, end_utc, days, now = _get_period_range(period, tz_name)
        far_future = datetime(2099, 12, 31, tzinfo=pytz.utc)
        end_param = end_utc if end_utc else far_future

        # Optional per-agent filter
        agent_filter = request.args.get('agent', '').strip()
        if agent_filter and agent_filter in location_ids:
            location_ids = [agent_filter]

        cur = conn.cursor(cursor_factory=RealDictCursor)

        # ── Core call metrics ────────────────────────────────────────────────
        cur.execute("""
            SELECT
                COUNT(*)                                                      AS total_calls,
                COUNT(*) FILTER (WHERE direction = 'outbound')                AS outbound_calls,
                COUNT(*) FILTER (WHERE direction = 'inbound')                 AS inbound_calls,
                COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected_calls,
                COALESCE(AVG(duration) FILTER (WHERE duration > 0), 0)        AS avg_duration,
                COALESCE(SUM(duration), 0)                                    AS total_duration,
                COUNT(*) FILTER (WHERE duration >= 45)                        AS over_45s,
                COUNT(*) FILTER (WHERE duration >= 120)                       AS over_2min,
                COUNT(*) FILTER (WHERE duration >= 300)                       AS over_5min,
                COUNT(*) FILTER (WHERE duration >= 600)                       AS over_10min,
                COUNT(*) FILTER (WHERE duration >= 60)                        AS over_1min,
                COUNT(DISTINCT contact_id)                                    AS unique_contacts,
                COUNT(DISTINCT location_id)                                   AS active_agents
            FROM call_history
            WHERE location_id = ANY(%s) AND created_at >= %s AND created_at < %s
        """, (location_ids, start_utc, end_param))
        r = cur.fetchone()
        total_calls = r['total_calls'] or 0
        connected = r['connected_calls'] or 0
        connect_rate = round(connected / total_calls * 100, 1) if total_calls else 0.0

        logger.info(
            f"agency_dashboard_stats: period={period} start_utc={start_utc} "
            f"total_calls={total_calls} connected={connected} active_agents={r['active_agents']}"
        )

        # Duration bucket percentages (of connected calls)
        over_45s = r['over_45s'] or 0
        over_2min = r['over_2min'] or 0
        over_5min = r['over_5min'] or 0
        over_10min = r['over_10min'] or 0
        pct_45s = round(over_45s / connected * 100, 1) if connected else 0
        pct_2min = round(over_2min / connected * 100, 1) if connected else 0
        pct_5min = round(over_5min / connected * 100, 1) if connected else 0
        pct_10min = round(over_10min / connected * 100, 1) if connected else 0

        # ── Messages (outbound bot SMS only: message_type='assistant') ────────
        cur.execute("""
            SELECT COUNT(*) AS cnt
            FROM contact_messages cm
            JOIN contact_cache cc ON cm.contact_id = cc.contact_id
            WHERE cc.location_id = ANY(%s) AND cm.created_at >= %s AND cm.created_at < %s
              AND cm.message_type = 'assistant'
        """, (location_ids, start_utc, end_param))
        total_messages = cur.fetchone()['cnt'] or 0

        # ── Compute days for per-day averages ────────────────────────────────
        if days == 0:
            cur.execute("SELECT MIN(created_at) AS first FROM call_history WHERE location_id = ANY(%s)", (location_ids,))
            first = cur.fetchone()['first']
            if first:
                if first.tzinfo is None:
                    first = pytz.utc.localize(first)
                days = max(1, (now - first).days)
            else:
                days = 1

        # ── Per-agent detailed stats (for leaderboard + averages) ────────────
        cur.execute("""
            SELECT location_id,
                   COUNT(*) AS total_calls,
                   COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected,
                   COALESCE(SUM(duration), 0) AS total_secs,
                   COALESCE(AVG(duration) FILTER (WHERE duration > 0), 0) AS avg_dur,
                   COUNT(*) FILTER (WHERE duration >= 45) AS over_45s,
                   COUNT(*) FILTER (WHERE duration >= 120) AS over_2min,
                   COUNT(*) FILTER (WHERE duration >= 300) AS over_5min,
                   COUNT(*) FILTER (WHERE duration >= 600) AS over_10min,
                   MAX(created_at) AS last_call
            FROM call_history
            WHERE location_id = ANY(%s) AND created_at >= %s AND created_at < %s
            GROUP BY location_id
        """, (location_ids, start_utc, end_param))
        agent_call_stats = {row['location_id']: row for row in cur.fetchall()}

        # Per-agent outbound SMS counts only
        cur.execute("""
            SELECT cc.location_id, COUNT(*) AS cnt
            FROM contact_messages cm
            JOIN contact_cache cc ON cm.contact_id = cc.contact_id
            WHERE cc.location_id = ANY(%s) AND cm.created_at >= %s AND cm.created_at < %s
              AND cm.message_type = 'assistant'
            GROUP BY cc.location_id
        """, (location_ids, start_utc, end_param))
        agent_msg_stats = {row['location_id']: row['cnt'] for row in cur.fetchall()}

        # Agent names
        _cid = getattr(current_user, 'company_id', None)
        if _cid:
            cur.execute("SELECT location_id, full_name, email, bot_first_name FROM subscribers WHERE company_id = %s", (_cid,))
        else:
            cur.execute("SELECT location_id, full_name, email, bot_first_name FROM subscribers WHERE parent_agency_email = %s", (current_user.email,))
        agent_names = {row['location_id']: row for row in cur.fetchall()}

        # Build agent list with all metrics
        agents = []
        for lid in location_ids:
            cs = agent_call_stats.get(lid, {})
            info = agent_names.get(lid, {})
            agent_total = cs.get('total_calls', 0) or 0
            agent_connected = cs.get('connected', 0) or 0
            agent_connect_rate = round(agent_connected / agent_total * 100, 1) if agent_total else 0
            agent_over_45s = cs.get('over_45s', 0) or 0
            agent_pct_45s = round(agent_over_45s / agent_connected * 100, 1) if agent_connected else 0

            agents.append({
                "location_id": lid,
                "name": info.get('full_name') or info.get('email') or 'Unknown',
                "email": info.get('email', ''),
                "total_calls": agent_total,
                "connected": agent_connected,
                "connect_rate": agent_connect_rate,
                "total_secs": int(cs.get('total_secs', 0) or 0),
                "avg_duration": round(float(cs.get('avg_dur', 0) or 0), 1),
                "over_45s": agent_over_45s,
                "pct_45s": agent_pct_45s,
                "over_2min": cs.get('over_2min', 0) or 0,
                "over_5min": cs.get('over_5min', 0) or 0,
                "over_10min": cs.get('over_10min', 0) or 0,
                "messages": agent_msg_stats.get(lid, 0),
                "daily_avg_dials": round(agent_total / days, 1),
                "last_call": str(cs['last_call']) if cs.get('last_call') else None,
            })

        # Sort by connect rate for leaderboard (with minimum call threshold)
        agents_with_calls = [a for a in agents if a['total_calls'] >= 5]
        agents_with_calls.sort(key=lambda a: a['connect_rate'], reverse=True)

        # Top 5 by connect rate
        top_connect_rate = agents_with_calls[:5]

        # Top 5 by total calls (activity)
        top_by_calls = sorted(agents, key=lambda a: a['total_calls'], reverse=True)[:5]

        # Top 5 by avg duration (quality conversations)
        top_by_duration = sorted(
            [a for a in agents if a['connected'] >= 3],
            key=lambda a: a['avg_duration'], reverse=True
        )[:5]

        # ── Agency-wide averages ─────────────────────────────────────────────
        active_count = len([a for a in agents if a['total_calls'] > 0])
        avg_daily_dials = round(total_calls / days / max(active_count, 1), 1)
        avg_connect_rate_per_agent = round(
            sum(a['connect_rate'] for a in agents if a['total_calls'] >= 5) / max(len(agents_with_calls), 1), 1
        )

        # ── Speed to lead: contact_cache uses synced_at not created_at ──────
        # Use synced_at as a proxy for contact creation time
        cur.execute("""
            SELECT AVG(EXTRACT(EPOCH FROM (ch.created_at - cc.synced_at))) AS avg_speed
            FROM call_history ch
            JOIN contact_cache cc ON ch.contact_id = cc.contact_id AND ch.location_id = cc.location_id
            WHERE ch.location_id = ANY(%s)
              AND ch.created_at >= %s AND ch.created_at < %s
              AND ch.direction = 'outbound'
              AND cc.synced_at IS NOT NULL
              AND ch.created_at > cc.synced_at
              AND EXTRACT(EPOCH FROM (ch.created_at - cc.synced_at)) < 86400
        """, (location_ids, start_utc, end_param))
        speed_row = cur.fetchone()
        avg_speed_to_lead = round(float(speed_row['avg_speed'] or 0), 0) if speed_row and speed_row['avg_speed'] else None

        # ── Prior period comparison ──────────────────────────────────────────
        prior = None
        if period != 'all':
            period_len = now - start_utc.astimezone(pytz.timezone(tz_name))
            prior_end = start_utc
            prior_start = start_utc - period_len
            cur.execute("""
                SELECT
                    COUNT(*) AS total_calls,
                    COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected_calls,
                    COALESCE(SUM(duration), 0) AS total_duration,
                    COUNT(*) FILTER (WHERE duration >= 45) AS over_45s
                FROM call_history
                WHERE location_id = ANY(%s) AND created_at >= %s AND created_at < %s
            """, (location_ids, prior_start, prior_end))
            pr = cur.fetchone()
            p_total = pr['total_calls'] or 0
            p_connected = pr['connected_calls'] or 0
            p_over_45s = pr['over_45s'] or 0

            def _pct(curr, prev):
                return round((curr - prev) / prev * 100, 1) if prev else None

            prior = {
                "delta_calls": _pct(total_calls, p_total),
                "delta_connected": _pct(connected, p_connected),
                "delta_connect_rate": _pct(connect_rate, round(p_connected / p_total * 100, 1) if p_total else 0),
                "delta_over_45s": _pct(over_45s, p_over_45s),
            }

        # ── Daily volume chart data ──────────────────────────────────────────
        cur.execute("""
            SELECT DATE(created_at AT TIME ZONE 'UTC' AT TIME ZONE %s) AS day,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected
            FROM call_history
            WHERE location_id = ANY(%s) AND created_at >= %s AND created_at < %s
            GROUP BY day ORDER BY day
        """, (tz_name, location_ids, start_utc, end_param))
        daily = [
            {"day": str(row['day']), "calls": row['calls'], "connected": row['connected']}
            for row in cur.fetchall()
        ]

        # ── Hourly distribution ──────────────────────────────────────────────
        cur.execute("""
            SELECT EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC' AT TIME ZONE %s)::int AS hr,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) AS connected
            FROM call_history
            WHERE location_id = ANY(%s) AND created_at >= %s AND created_at < %s
            GROUP BY hr ORDER BY hr
        """, (tz_name, location_ids, start_utc, end_param))
        hourly_map = {row['hr']: row for row in cur.fetchall()}
        hourly = [{"hour": h, "calls": hourly_map.get(h, {}).get('calls', 0),
                   "connected": hourly_map.get(h, {}).get('connected', 0)} for h in range(24)]

        cur.close()

        return flask_jsonify({
            "period": period,
            "days_in_period": days,
            # Core metrics
            "total_calls": total_calls,
            "outbound_calls": r['outbound_calls'] or 0,
            "inbound_calls": r['inbound_calls'] or 0,
            "connected_calls": connected,
            "connect_rate": connect_rate,
            "avg_duration": round(float(r['avg_duration'] or 0), 1),
            "total_duration": int(r['total_duration'] or 0),
            "total_messages": total_messages,
            "unique_contacts": r['unique_contacts'] or 0,
            # Duration buckets
            "over_45s": over_45s,
            "over_2min": over_2min,
            "over_5min": over_5min,
            "over_10min": over_10min,
            "pct_45s": pct_45s,
            "pct_2min": pct_2min,
            "pct_5min": pct_5min,
            "pct_10min": pct_10min,
            # Agency averages
            "active_agents": r['active_agents'] or 0,
            "total_agents": len(location_ids),
            "avg_daily_dials": avg_daily_dials,
            "avg_daily_dials_total": round(total_calls / days, 1),
            "avg_connect_rate_per_agent": avg_connect_rate_per_agent,
            "avg_speed_to_lead_secs": avg_speed_to_lead,
            # Leaderboards (top 5)
            "top_connect_rate": top_connect_rate,
            "top_by_calls": top_by_calls,
            "top_by_duration": top_by_duration,
            # All agents (for "see more")
            "agents": sorted(agents, key=lambda a: a['total_calls'], reverse=True),
            # Charts
            "daily": daily,
            "hourly": hourly,
            # Comparison
            "prior": prior,
        })
    except Exception as e:
        logger.error(f"agency_dashboard_stats error: {e}", exc_info=True)
        return flask_jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)


def _empty_dashboard_stats():
    """Return empty dashboard stats structure."""
    return {
        "period": "month", "days_in_period": 1,
        "total_calls": 0, "outbound_calls": 0, "inbound_calls": 0,
        "connected_calls": 0, "connect_rate": 0, "avg_duration": 0,
        "total_duration": 0, "total_messages": 0, "unique_contacts": 0,
        "over_45s": 0, "over_2min": 0, "over_5min": 0, "over_10min": 0,
        "pct_45s": 0, "pct_2min": 0, "pct_5min": 0, "pct_10min": 0,
        "active_agents": 0, "total_agents": 0, "avg_daily_dials": 0,
        "avg_daily_dials_total": 0, "avg_connect_rate_per_agent": 0,
        "avg_speed_to_lead_secs": None,
        "top_connect_rate": [], "top_by_calls": [], "top_by_duration": [],
        "agents": [], "daily": [], "hourly": [], "prior": None,
    }


# ── White-Label API ──────────────────────────────────────────────────────────

@agency_bp.route("/api/agency/whitelabel", methods=["GET", "POST"])
@login_required
def agency_whitelabel():
    """Get or save white-label branding config for agency owner."""
    if current_user.role != 'agency_owner' and current_user.email.lower() not in [e.lower() for e in ADMIN_EMAILS]:
        return flask_jsonify({"error": "Access denied"}), 403

    from db import get_whitelabel_config, save_whitelabel_config

    if request.method == 'GET':
        config = get_whitelabel_config(current_user.email)
        return flask_jsonify({"whitelabel": config})

    # POST — save config
    data = request.get_json()
    if not data:
        return flask_jsonify({"error": "No data provided"}), 400

    # Validate and sanitize
    config = {}
    if isinstance(data.get('enabled'), bool):
        config['enabled'] = data['enabled']
    if data.get('company_name'):
        name = str(data['company_name']).strip()[:100]
        if name:
            config['company_name'] = name
    if data.get('logo_url'):
        logo = str(data['logo_url']).strip()[:500]
        # Basic URL validation
        if logo.startswith(('https://', 'http://')):
            config['logo_url'] = logo
    # Logo dimensions — store max recommended size
    if data.get('logo_width'):
        config['logo_width'] = min(int(data['logo_width']), 400)
    if data.get('logo_height'):
        config['logo_height'] = min(int(data['logo_height']), 120)
    # Company name styling
    if data.get('name_font'):
        allowed_fonts = [
            'Inter', 'Roboto', 'Poppins', 'Montserrat', 'Open Sans',
            'Lato', 'Oswald', 'Raleway', 'Playfair Display', 'Merriweather',
        ]
        if data['name_font'] in allowed_fonts:
            config['name_font'] = data['name_font']
    if isinstance(data.get('name_bold'), bool):
        config['name_bold'] = data['name_bold']
    if isinstance(data.get('name_italic'), bool):
        config['name_italic'] = data['name_italic']
    if isinstance(data.get('name_underline'), bool):
        config['name_underline'] = data['name_underline']
    # Accent color
    if data.get('accent_color'):
        import re
        color = str(data['accent_color']).strip()
        if re.match(r'^#[0-9a-fA-F]{6}$', color):
            config['accent_color'] = color
    # Dashboard font
    if data.get('font_family'):
        allowed_dash_fonts = [
            'Inter', 'Roboto', 'Poppins', 'Montserrat', 'Open Sans',
            'Lato', 'Nunito', 'Source Sans 3', 'DM Sans', 'Manrope',
        ]
        if data['font_family'] in allowed_dash_fonts:
            config['font_family'] = data['font_family']

    ok = save_whitelabel_config(current_user.email, config)
    if ok:
        return flask_jsonify({"status": "ok", "whitelabel": config})
    return flask_jsonify({"error": "Failed to save"}), 500


@agency_bp.route("/api/agency/members")
@login_required
def agency_members():
    """List all agency members via company_id AND parent_agency_email (unioned, deduped)."""
    if current_user.role != 'agency_owner' and current_user.email.lower() not in [e.lower() for e in ADMIN_EMAILS]:
        return flask_jsonify({"error": "Access denied"}), 403

    company_id = getattr(current_user, 'company_id', None)
    agency_email = current_user.email

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "DB unavailable"}), 503
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        select_cols = """
                SELECT location_id, email, full_name, phone, role, subscription_tier,
                       bot_first_name, timezone, access_token, token_expires_at,
                       onboarding_status, created_at, stripe_customer_id, stripe_status
        """
        if company_id:
            cur.execute(select_cols + """
                FROM subscribers
                WHERE (company_id = %s OR LOWER(parent_agency_email) = LOWER(%s))
                  AND LOWER(email) != LOWER(%s)
                ORDER BY created_at DESC
            """, (company_id, agency_email, agency_email))
        else:
            cur.execute(select_cols + """
                FROM subscribers
                WHERE LOWER(parent_agency_email) = LOWER(%s)
                  AND LOWER(email) != LOWER(%s)
                ORDER BY created_at DESC
            """, (agency_email, agency_email))
        members = [dict(r) for r in cur.fetchall()]
        cur.close()
    except Exception as e:
        logger.error(f"agency_members error: {e}")
        return flask_jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)

    # Sanitize for JSON response — determine real status from Stripe + OAuth
    result = []
    for m in members:
        has_token = bool(m.get('access_token'))
        expires = m.get('token_expires_at')
        token_valid = has_token and expires and expires > datetime.utcnow() if expires else False
        tier = m.get('subscription_tier')
        stripe_status = m.get('stripe_status')
        has_stripe = bool(m.get('stripe_customer_id'))

        # Status priority: Cancelled > Active > Token Expired > Pending
        if tier is None and (stripe_status in ('canceled', 'unpaid') or has_stripe):
            status = 'Cancelled'
        elif tier and token_valid:
            status = 'Active'
        elif tier and not token_valid:
            status = 'Token Expired'
        else:
            status = 'Pending'

        result.append({
            'location_id': m['location_id'],
            'email': m.get('email'),
            'full_name': m.get('full_name'),
            'phone': m.get('phone'),
            'role': m.get('role'),
            'subscription_tier': m.get('subscription_tier'),
            'bot_name': m.get('bot_first_name'),
            'timezone': m.get('timezone'),
            'status': status,
            'stripe_status': stripe_status,
            'onboarding_status': m.get('onboarding_status'),
            'created_at': m['created_at'].isoformat() + 'Z' if m.get('created_at') else None,
        })

    return flask_jsonify({"members": result, "total": len(result)})


@agency_bp.route("/api/agency/conversion-stats")
@login_required
def agency_conversion_stats():
    """Aggregated conversion analytics across all agency sub-accounts."""
    if current_user.role != 'agency_owner' and current_user.email.lower() not in [e.lower() for e in ADMIN_EMAILS]:
        return flask_jsonify({"error": "Access denied"}), 403

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "DB unavailable"}), 503

    try:
        location_ids = _get_sub_location_ids(conn, current_user.email,
                                              company_id=getattr(current_user, 'company_id', None))
        if current_user.location_id and current_user.location_id not in location_ids:
            location_ids.append(current_user.location_id)
    finally:
        return_db_connection(conn)

    if not location_ids:
        return flask_jsonify({"period": "week", "booking_rate": 0,
                              "bookings_confirmed": 0, "bookings_attempted": 0,
                              "objection_win_rate": 0, "event_counts": {},
                              "daily_trend": [], "per_agent": []})

    period = request.args.get('period', 'week')
    tz_name = (current_user.timezone or 'America/Chicago').replace(' ', '_')

    from db import get_conversion_stats_multi
    stats = get_conversion_stats_multi(location_ids, period=period, tz_name=tz_name)
    return flask_jsonify(stats)
