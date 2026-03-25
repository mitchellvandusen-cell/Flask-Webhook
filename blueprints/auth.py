# blueprints/auth.py — Authentication routes
#
# Handles user registration, login, logout, password reset, set-password,
# and sub-user account claim. No GHL OAuth here — that's in blueprints/oauth.py.

import os
import logging
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, session, jsonify as flask_jsonify)
from flask_login import login_user, logout_user, login_required, current_user
from flask_mail import Message
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from werkzeug.security import generate_password_hash, check_password_hash
from psycopg2.extras import RealDictCursor
from flask import current_app

from extensions import mail, YOUR_DOMAIN, ADMIN_EMAILS
from forms import RegisterForm, LoginForm
from db import User, get_db_connection, return_db_connection

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__)


# ── Private helpers ───────────────────────────────────────────────────────────

def _get_reset_serializer():
    """Return a URLSafeTimedSerializer bound to the app's SECRET_KEY."""
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'])


def _send_reset_email(to_email: str, reset_url: str):
    """Send a password-reset link via Flask-Mail."""
    from email_templates import _build_password_reset_html
    html_body = _build_password_reset_html(reset_url, YOUR_DOMAIN)
    msg = Message(
        subject="InsuranceGrokBot - Password Reset",
        recipients=[to_email],
        html=html_body,
        body=f"Reset your password: {reset_url}\n\nThis link expires in 30 minutes."
    )
    mail.send(msg)


# ── Register ──────────────────────────────────────────────────────────────────

@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """
    Post-marketplace registration: user sets a password after OAuth install.
    Sub-users should use /claim-account instead.
    """
    form = RegisterForm()

    if request.method == "GET":
        url_location_id = request.args.get('location_id')
        if url_location_id:
            form.location_id.data = url_location_id
            flash("Lead Connector connected! Your location ID is pre-filled. Set a password to finish.", "success")

    if form.validate_on_submit():
        email                 = form.email.data.lower().strip()
        submitted_location_id = form.location_id.data.strip()
        password              = form.password.data

        existing_user = User.get(email)
        if existing_user:
            flash("Email already registered. Please log in.", "info")
            return redirect(url_for("auth.login"))

        conn = get_db_connection()
        if not conn:
            flash("Database unavailable. Please try again later.", "error")
            return redirect("/register")

        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT email, parent_agency_email, onboarding_status
                FROM subscribers
                WHERE location_id = %s
                LIMIT 1
            """, (submitted_location_id,))
            match = cur.fetchone()

            if not match:
                flash("Location ID not found. You must install the app from the Lead Connector Marketplace first.", "error")
                return redirect("/register")

            password_hash = generate_password_hash(password)

            if match['parent_agency_email'] and match.get('onboarding_status') == 'invited':
                flash("This is a sub-account. Please use the invitation link sent to your email to claim your account.", "info")
                return redirect(url_for("auth.login"))

            if match['email'] != email:
                flash("Location ID does not match your email. Please reconnect via OAuth.", "error")
                return redirect("/register")

            cur.execute("""
                UPDATE subscribers
                SET password_hash = %s,
                    onboarding_status = 'claimed',
                    updated_at = NOW()
                WHERE location_id = %s
            """, (password_hash, submitted_location_id))

            conn.commit()
            logger.info(f"Post-OAuth registration completed: {email}")
            flash("Account created successfully! Welcome aboard.", "success")
            return redirect(url_for("auth.login", registered=1))

        except Exception as e:
            conn.rollback()
            logger.error(f"Registration failed for {email}: {e}")
            flash("Account creation failed. Please try again or contact support.", "error")
            return redirect("/register")
        finally:
            cur.close()
            return_db_connection(conn)

    return render_template('register.html', form=form)


# ── Login / Logout ────────────────────────────────────────────────────────────

@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    form = LoginForm()

    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user  = User.get(email)

        if not user:
            flash("No account found with that email.", "error")
            return render_template("login.html", form=form)

        if not user.password_hash:
            flash("You haven't set a password yet. Please check your email or complete checkout first.", "error")
            return render_template("login.html", form=form)

        if not check_password_hash(user.password_hash, form.password.data):
            flash("Incorrect password.", "error")
            return render_template("login.html", form=form)

        # ── Two-Factor Authentication ──
        if getattr(user, 'two_factor_enabled', False) and getattr(user, 'two_factor_phone', None):
            from two_factor import send_verification_code
            result = send_verification_code(user.two_factor_phone)
            if result.get('success'):
                # Store email in session for the verify step (don't log in yet)
                session['_2fa_email'] = email
                session['_2fa_remember'] = form.remember.data
                return redirect(url_for('auth.verify_2fa'))
            else:
                # 2FA send failed — let them in with a warning
                logger.warning(f"2FA send failed for {email}, allowing login: {result.get('error')}")

        login_user(user, remember=form.remember.data)

        # Track login time for seat users (used for session revocation)
        if getattr(user, 'is_seat_user', False):
            from datetime import datetime as _dt
            session['_seat_login_at'] = _dt.utcnow().isoformat()

        # Route agency owners to agency dashboard, everyone else to individual dashboard
        role     = (user.role or 'individual').lower()
        is_admin = user.email.lower() in [e.lower() for e in ADMIN_EMAILS]

        if not is_admin and role == 'agency_owner':
            return redirect(url_for("agency.agency_dashboard"))
        return redirect(url_for("dashboard.dashboard"))

    return render_template("login.html", form=form)


# ── Sign in with GoHighLevel (SSO via OAuth) ─────────────────────────────────

@auth_bp.route("/auth/ghl")
def ghl_sso_initiate():
    """
    Start GHL SSO login flow. Same OAuth as /oauth/initiate but without
    @login_required — user authenticates via GHL and we log them in.
    Every sign-in refreshes their GHL OAuth tokens automatically.
    """
    from blueprints.oauth import GHL_OAUTH_SCOPES

    client_id = os.getenv("GHL_CLIENT_ID")
    domain = os.getenv("YOUR_DOMAIN")
    if not client_id or not domain:
        flash("GoHighLevel sign-in is not available.", "error")
        return redirect(url_for('auth.login'))

    redirect_uri = f"{domain}/oauth/callback"
    scope_string = " ".join(GHL_OAUTH_SCOPES)
    nonce = secrets.token_urlsafe(32)
    state = f"ghl_sso:{nonce}"
    session["ghl_oauth_state"] = state

    params = urlencode({
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'scope': scope_string,
        'state': state,
    })
    oauth_url = f"https://marketplace.gohighlevel.com/oauth/chooselocation?{params}"

    logger.info(f"GHL SSO initiated — redirecting to GHL consent page")
    return redirect(oauth_url)


@auth_bp.route("/verify-2fa", methods=["GET", "POST"])
def verify_2fa():
    """Second step of login: enter the 6-digit SMS code."""
    email = session.get('_2fa_email')
    if not email:
        return redirect(url_for('auth.login'))

    if request.method == "GET":
        user = User.get(email)
        # Mask phone for display: +1***...1234
        phone = getattr(user, 'two_factor_phone', '') or ''
        masked = phone[:3] + '***' + phone[-4:] if len(phone) > 7 else '***'
        return render_template("verify-2fa.html", masked_phone=masked)

    # POST: verify the code
    code = request.form.get('code', '').strip()
    if not code or len(code) != 6:
        flash("Please enter the 6-digit code.", "error")
        return redirect(url_for('auth.verify_2fa'))

    user = User.get(email)
    if not user:
        session.pop('_2fa_email', None)
        return redirect(url_for('auth.login'))

    from two_factor import check_verification_code
    result = check_verification_code(user.two_factor_phone, code)

    if result.get('success') and result.get('valid'):
        # Code verified — complete login
        remember = session.pop('_2fa_remember', False)
        session.pop('_2fa_email', None)
        login_user(user, remember=remember)

        if getattr(user, 'is_seat_user', False):
            from datetime import datetime as _dt
            session['_seat_login_at'] = _dt.utcnow().isoformat()

        role     = (user.role or 'individual').lower()
        is_admin = user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
        if not is_admin and role == 'agency_owner':
            return redirect(url_for("agency.agency_dashboard"))
        return redirect(url_for("dashboard.dashboard"))
    else:
        flash("Invalid or expired code. Please try again.", "error")
        return redirect(url_for('auth.verify_2fa'))


@auth_bp.route("/resend-2fa", methods=["POST"])
def resend_2fa():
    """Resend the 2FA code."""
    email = session.get('_2fa_email')
    if not email:
        return redirect(url_for('auth.login'))

    user = User.get(email)
    if user and getattr(user, 'two_factor_phone', None):
        from two_factor import send_verification_code
        result = send_verification_code(user.two_factor_phone)
        if result.get('success'):
            flash("New code sent. Check your phone.", "info")
        else:
            flash("Failed to send code. Please try again.", "error")

    return redirect(url_for('auth.verify_2fa'))


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/")


# ── Forgot / reset password ───────────────────────────────────────────────────

@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot-password.html")

    email = request.form.get("email", "").strip().lower()
    flash("If an account is registered with that email, you will receive a reset link. Check your spam or junk folder if you don't see it within a few minutes.", "info")

    if email:
        user = User.get(email)
        if not user:
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor(cursor_factory=RealDictCursor)
                    cur.execute("SELECT email FROM subscribers WHERE LOWER(email) = LOWER(%s)", (email,))
                    user = cur.fetchone()
                except Exception:
                    pass
                finally:
                    return_db_connection(conn)

        if user:
            try:
                s         = _get_reset_serializer()
                token     = s.dumps(email, salt='password-reset')
                reset_url = f"{YOUR_DOMAIN}/reset-password/{token}"
                _send_reset_email(email, reset_url)
                logger.info(f"Password reset email sent to {email}")
            except Exception as e:
                logger.error(f"Failed to send reset email to {email}: {e}")

    return redirect("/forgot-password")


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Validate token and allow user to set a new password (30-min expiry)."""
    s = _get_reset_serializer()
    try:
        email = s.loads(token, salt='password-reset', max_age=1800)
    except SignatureExpired:
        flash("This reset link has expired. Please request a new one.", "error")
        return redirect("/forgot-password")
    except BadSignature:
        flash("Invalid reset link.", "error")
        return redirect("/forgot-password")

    if request.method == "GET":
        return render_template("reset-password.html", token=token, email=email)

    password = request.form.get("password", "")
    confirm  = request.form.get("confirm_password", "")

    if not password or len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(f"/reset-password/{token}")

    if password != confirm:
        flash("Passwords do not match.", "error")
        return redirect(f"/reset-password/{token}")

    password_hash = generate_password_hash(password)
    conn = get_db_connection()
    if not conn:
        flash("Database unavailable. Please try again.", "error")
        return redirect(f"/reset-password/{token}")

    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers SET password_hash = %s, updated_at = NOW()
            WHERE LOWER(email) = %s
        """, (password_hash, email.lower()))
        conn.commit()
        logger.info(f"Password reset completed for {email}")
        flash("Password reset successfully! You can now log in.", "success")
        return redirect("/login")
    except Exception as e:
        conn.rollback()
        logger.error(f"Password reset DB error for {email}: {e}")
        flash("Something went wrong. Please try again.", "error")
        return redirect(f"/reset-password/{token}")
    finally:
        cur.close()
        return_db_connection(conn)


# ── Set password (post-checkout / post-OAuth) ─────────────────────────────────

@auth_bp.route("/set-password", methods=["GET", "POST"])
@login_required
def set_password():
    """Password setup for users after OAuth or Stripe checkout."""
    user_type = request.args.get("type", "individual")

    if request.method == "GET":
        return render_template('set_password.html',
                               email=current_user.email,
                               user_type=user_type)

    password = request.form.get("password")
    confirm  = request.form.get("confirm_password")

    if not password or len(password) < 8:
        flash("Password must be at least 8 characters.", "danger")
        return redirect(f"/set-password?type={user_type}")

    if password != confirm:
        flash("Passwords do not match.", "danger")
        return redirect(f"/set-password?type={user_type}")

    password_hash = generate_password_hash(password)
    conn = get_db_connection()
    if not conn:
        flash("Database unavailable. Please try again.", "error")
        return redirect(f"/set-password?type={user_type}")

    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers
            SET password_hash = %s,
                onboarding_status = 'claimed',
                updated_at = NOW()
            WHERE email = %s
        """, (password_hash, current_user.email))
        conn.commit()
        logger.info(f"Password set for {current_user.email} ({current_user.role})")
        logout_user()
        flash("Password created successfully! Please log in.", "success")
        return redirect("/login")
    except Exception as e:
        conn.rollback()
        logger.error(f"Set password error for {current_user.email}: {e}")
        flash("Error setting password. Please try again.", "error")
        return redirect(f"/set-password?type={user_type}")
    finally:
        cur.close()
        return_db_connection(conn)


# ── Sub-user account claim ────────────────────────────────────────────────────

@auth_bp.route("/claim-account", methods=["GET", "POST"])
def claim_account():
    """Sub-user claims their account using the invite token."""
    token = request.args.get("token") or request.form.get("token")

    if not token:
        flash("Invalid or missing invite link.", "danger")
        return redirect(url_for('public.home'))

    conn = get_db_connection()
    if not conn:
        flash("System error. Please try again.", "danger")
        return redirect(url_for('public.home'))

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT location_id, agent_email, full_name, onboarding_status, invite_sent_at
            FROM subscribers
            WHERE invite_token = %s
        """, (token,))
        sub = cur.fetchone()

        if not sub:
            flash("Invalid or expired invite link.", "danger")
            return redirect(url_for('public.home'))

        if sub['onboarding_status'] == 'claimed':
            flash("This account has already been claimed. Please log in.", "info")
            return redirect(url_for('auth.login'))

        if sub['invite_sent_at']:
            expiry = sub['invite_sent_at'] + timedelta(days=7)
            if datetime.now() > expiry:
                flash("This invite link has expired. Please ask your agency owner to resend.", "danger")
                return redirect(url_for('public.home'))

        if request.method == 'GET':
            return render_template('claim_account.html',
                                   email=sub['agent_email'],
                                   name=sub['full_name'],
                                   token=token)

        password         = request.form.get("password")
        confirm_password = request.form.get("confirm_password")

        if not password or len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template('claim_account.html',
                                   email=sub['agent_email'],
                                   name=sub['full_name'],
                                   token=token)

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template('claim_account.html',
                                   email=sub['agent_email'],
                                   name=sub['full_name'],
                                   token=token)

        password_hash = generate_password_hash(password)
        cur.execute("""
            UPDATE subscribers
            SET password_hash = %s,
                email = %s,
                invite_token = NULL,
                invite_claimed_at = NOW(),
                onboarding_status = 'claimed',
                updated_at = NOW()
            WHERE location_id = %s
        """, (password_hash, sub['agent_email'], sub['location_id']))
        conn.commit()

        logger.info(f"Account claimed: {sub['agent_email']} for location {sub['location_id']}")
        flash("Account activated! You can now log in.", "success")
        return redirect(url_for('auth.login'))

    except Exception as e:
        conn.rollback()
        logger.error(f"Claim account error: {e}")
        flash("An error occurred. Please try again.", "danger")
        return redirect(url_for('public.home'))
    finally:
        cur.close()
        return_db_connection(conn)


# ── Two-Factor Authentication Setup ─────────────────────────────────────────

@auth_bp.route("/api/2fa/status", methods=["GET"])
@login_required
def twofa_status():
    """Get current 2FA status for the logged-in user."""
    phone = getattr(current_user, 'two_factor_phone', None) or ''
    enabled = getattr(current_user, 'two_factor_enabled', False)
    masked = phone[:3] + '***' + phone[-4:] if len(phone) > 7 else ''
    return flask_jsonify({
        "enabled": enabled,
        "phone": masked,
    })


@auth_bp.route("/api/2fa/setup", methods=["POST"])
@login_required
def twofa_setup():
    """Start 2FA setup: send verification code to provided phone number."""
    import re
    data = request.get_json() or {}
    phone = data.get('phone', '').strip()

    # Basic E.164 validation
    if not phone.startswith('+') or len(phone) < 10:
        return flask_jsonify({"error": "Please enter a valid phone number with country code (e.g. +14155551234)"}), 400
    if not re.match(r'^\+\d{10,15}$', phone):
        return flask_jsonify({"error": "Invalid phone number format"}), 400

    from two_factor import send_verification_code
    result = send_verification_code(phone)
    if result.get('success'):
        session['_2fa_setup_phone'] = phone
        return flask_jsonify({"status": "code_sent"})
    else:
        return flask_jsonify({"error": "Failed to send verification code. Please check the number and try again."}), 500


@auth_bp.route("/api/2fa/confirm", methods=["POST"])
@login_required
def twofa_confirm():
    """Confirm 2FA setup: verify the code and enable 2FA."""
    data = request.get_json() or {}
    code = data.get('code', '').strip()
    phone = session.get('_2fa_setup_phone')

    if not phone:
        return flask_jsonify({"error": "No phone number in setup flow. Please start over."}), 400
    if not code or len(code) != 6:
        return flask_jsonify({"error": "Please enter the 6-digit code"}), 400

    from two_factor import check_verification_code
    result = check_verification_code(phone, code)

    if result.get('success') and result.get('valid'):
        # Enable 2FA in database
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE subscribers
                    SET two_factor_enabled = TRUE, two_factor_phone = %s, updated_at = NOW()
                    WHERE email = %s
                """, (phone, current_user.email))
                conn.commit()
                cur.close()
                session.pop('_2fa_setup_phone', None)
                logger.info(f"2FA enabled for {current_user.email}")
                return flask_jsonify({"status": "enabled"})
            except Exception as e:
                conn.rollback()
                logger.error(f"2FA enable DB error: {e}")
                return flask_jsonify({"error": "Database error"}), 500
            finally:
                return_db_connection(conn)
        return flask_jsonify({"error": "Database unavailable"}), 503
    else:
        return flask_jsonify({"error": "Invalid or expired code. Please try again."}), 400


@auth_bp.route("/api/2fa/disable", methods=["POST"])
@login_required
def twofa_disable():
    """Disable 2FA for the logged-in user. Requires current password."""
    data = request.get_json() or {}
    password = data.get('password', '')

    if not password or not check_password_hash(current_user.password_hash, password):
        return flask_jsonify({"error": "Incorrect password"}), 403

    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE subscribers
                SET two_factor_enabled = FALSE, two_factor_phone = NULL, updated_at = NOW()
                WHERE email = %s
            """, (current_user.email,))
            conn.commit()
            cur.close()
            logger.info(f"2FA disabled for {current_user.email}")
            return flask_jsonify({"status": "disabled"})
        except Exception as e:
            conn.rollback()
            logger.error(f"2FA disable DB error: {e}")
            return flask_jsonify({"error": "Database error"}), 500
        finally:
            return_db_connection(conn)
    return flask_jsonify({"error": "Database unavailable"}), 503
