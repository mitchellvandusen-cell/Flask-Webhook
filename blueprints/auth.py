# blueprints/auth.py — Authentication routes
#
# Handles user registration, login, logout, password reset, set-password,
# and sub-user account claim. No GHL OAuth here — that's in blueprints/oauth.py.

import logging
import secrets
from datetime import datetime, timedelta

from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, session)
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

        login_user(user, remember=form.remember.data)

        # Track login time for seat users (used for session revocation)
        if getattr(user, 'is_seat_user', False):
            from datetime import datetime as _dt
            session['_seat_login_at'] = _dt.utcnow().isoformat()

        # Unified dashboard for all users — agency owners and individuals alike
        return redirect(url_for("dashboard.dashboard"))

    return render_template("login.html", form=form)


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
    flash("If an account is registered with that email, you will receive a reset link.", "info")

    if email:
        user = User.get(email)
        if not user:
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor(cursor_factory=RealDictCursor)
                    cur.execute("SELECT agency_email FROM agency_billing WHERE agency_email = %s", (email,))
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
        cur.execute("""
            UPDATE agency_billing SET password_hash = %s, updated_at = NOW()
            WHERE LOWER(agency_email) = %s
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
        if current_user.role == 'agency_owner':
            cur.execute("""
                UPDATE agency_billing
                SET password_hash = %s, updated_at = NOW()
                WHERE agency_email = %s
            """, (password_hash, current_user.email))
        else:
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
