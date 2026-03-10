# blueprints/agency.py — Agency owner dashboard and sub-user management
#
# Routes:
#   GET|POST /agency-dashboard              — Agency owner main dashboard
#   GET|POST /agency-login                  — Agency owner login
#   POST /api/agency/invite-sub-user        — Invite a sub-account user
#   POST /api/agency/resend-invite          — Re-send invite email
#   POST /api/agency/invite-all             — Invite all pending sub-users
#   GET  /api/agency/logs/<location_id>     — Get logs for a sub-account

import secrets
import logging
from datetime import datetime, timedelta

from flask import (Blueprint, request, render_template, redirect,
                   url_for, flash, session)
from flask import jsonify as flask_jsonify
from flask_login import login_required, login_user, current_user
from flask_mail import Message
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
    html_body, text_body = _build_agency_invite_html(agent_name, agency_name, invite_url, YOUR_DOMAIN)
    msg = Message(
        subject=f"You're invited to InsuranceGrokBot by {agency_name}",
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
    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]

    if current_user.role != 'agency_owner' and not is_admin:
        flash("Access restricted to agency owners only.", "error")
        return redirect("/dashboard")

    needs_subscription = not current_user.stripe_customer_id and not is_admin
    if needs_subscription:
        detected_tier = current_user.subscription_tier or 'agency_starter'
        return render_template('agency-dashboard.html',
            needs_subscription=True,
            detected_tier=detected_tier,
            agency_starter_price=797.99,
            agency_pro_price=1597.99,
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
            },
            carrier_list=CARRIER_LIST,
            selected_carriers=[],
            bot_settings=dict(BOT_SETTINGS_DEFAULTS),
            sub_accounts=[],
            stats={'max_seats': 0, 'active_seats': 0, 'tier': 'Not Subscribed'},
            user=current_user,
        )

    conn = get_db_connection()
    if not conn:
        flash("System error: Database unavailable.", "error")
        return redirect("/dashboard")

    form = ConfigForm()

    if request.method == 'POST' and not form.validate_on_submit():
        logger.warning(f"Agency form validation failed for {current_user.email}: {form.errors}")
        flash("Please fill in all required fields.", "error")

    if form.validate_on_submit():
        try:
            cur = conn.cursor()
            calendar_name = request.form.get('calendar_name', '')
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
            return redirect(url_for('agency.agency_dashboard'))
        except Exception as e:
            conn.rollback()
            flash(f"Error saving settings: {str(e)}", "error")
        finally:
            cur.close()

    if request.method == 'GET':
        form.location_id.data     = current_user.location_id
        form.calendar_id.data     = current_user.calendar_id
        form.crm_user_id.data     = current_user.crm_user_id
        form.bot_name.data        = current_user.bot_first_name
        form.timezone.data        = current_user.timezone
        form.initial_message.data = current_user.initial_message
        form.personal_website.data= current_user.personal_website

    # Token display logic
    access_token_display = ''
    refresh_token_display= ''
    expires_in_str       = ''
    token_field_state    = ''

    if current_user.access_token:
        token_field_state = 'readonly'
        at = current_user.access_token
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

    profile = {
        'full_name': current_user.full_name or '',
        'phone':     current_user.phone or '',
        'bio':       current_user.bio or '',
    }

    sub_accounts = []
    agency_stats = {'max_seats': 10, 'active_seats': 0, 'tier': 'Agency Starter'}

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT subscription_tier, max_seats
            FROM agency_billing
            WHERE agency_email = %s
        """, (current_user.email,))
        billing_row = cur.fetchone()
        if billing_row:
            agency_stats['max_seats'] = billing_row['max_seats']
            agency_stats['tier'] = billing_row['subscription_tier'].replace('_', ' ').title()

        cur.execute("""
            SELECT location_id, full_name, email, bot_first_name, timezone,
                   access_token, subscription_tier, token_expires_at, created_at,
                   refresh_token, onboarding_status, invite_sent_at
            FROM subscribers
            WHERE parent_agency_email = %s
            ORDER BY created_at DESC
        """, (current_user.email,))

        current_time = datetime.now()
        for sub in cur.fetchall():
            is_connected = False
            if sub['access_token']:
                if sub['token_expires_at']:
                    expires = sub['token_expires_at']
                    if isinstance(expires, str):
                        try:
                            expires = datetime.fromisoformat(expires)
                        except Exception:
                            expires = datetime.now()
                    is_connected = expires > current_time
                else:
                    is_connected = True

            sub_accounts.append({
                'name':               sub['full_name'] or 'Unnamed Location',
                'location_id':        sub['location_id'],
                'email':              sub['email'] or 'No Email Assigned',
                'agent_email':        sub['email'] or 'No Agent Email',
                'status':             'Active' if is_connected else 'Pending Auth',
                'status_class':       'success' if is_connected else 'warning',
                'tier':               sub['subscription_tier'].replace('_', ' ').title(),
                'bot_name':           sub['bot_first_name'],
                'timezone':           sub['timezone'],
                'access_token':       sub['access_token'],
                'refresh_token':      sub['refresh_token'],
                'onboarding_status':  sub['onboarding_status'] or 'pending',
                'invite_sent_at':     sub['invite_sent_at'],
            })

        agency_stats['active_seats'] = len(sub_accounts)

    except Exception as e:
        logger.error(f"Agency Dashboard Error: {e}")
        flash("Error loading agency data.", "error")
    finally:
        cur.close()
        return_db_connection(conn)

    agency_carriers    = get_contracted_carriers(current_user.email)
    agency_bot_settings= get_bot_settings(current_user.email)

    return render_template(
        'agency-dashboard.html',
        form=form,
        access_token_display=access_token_display,
        refresh_token_display=refresh_token_display,
        token_readonly=token_field_state,
        expires_in_str=expires_in_str,
        sub=current_user,
        profile=profile,
        sub_accounts=sub_accounts,
        stats=agency_stats,
        user=current_user,
        carrier_list=CARRIER_LIST,
        selected_carriers=agency_carriers,
        bot_settings=agency_bot_settings,
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

        invite_token = secrets.token_urlsafe(32)

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
        return flask_jsonify({"error": str(e)}), 500
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
        return flask_jsonify({"error": str(e)}), 500
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
                invite_token = secrets.token_urlsafe(32)
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
        return flask_jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


@agency_bp.route("/api/agency/logs/<location_id>", methods=["GET"])
@login_required
def get_agency_logs(location_id):
    """Fetch webhook logs for a specific sub-account (agency owners only)."""
    if current_user.role != 'agency_owner':
        return flask_jsonify({"error": "Access denied"}), 403

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
