# blueprints/team.py — Multi-user seat management for GHL locations
#
# Allows a location manager (subscriber) to invite seat users who get their
# own Twilio sub-account, phone number, and restricted dashboard access.
# Seat users share the manager's GHL OAuth tokens, bot config, and carriers
# but have their own voice_config, call history, and permissions.

import json
import os
import secrets
import logging
from datetime import datetime, timedelta
from functools import wraps

import stripe
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash
from flask_login import login_required, current_user
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash

from db import get_db_connection, return_db_connection
import twilio_provisioning

stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')

logger = logging.getLogger(__name__)

team_bp = Blueprint('team', __name__)

YOUR_DOMAIN = os.getenv('YOUR_DOMAIN', 'http://localhost:8080')
SEAT_PRICE_CENTS = 5000  # $50/month per seat
MAX_SEATS = 10

# ── Roles ─────────────────────────────────────────────────────────────────────

ROLES = {
    'admin':   'Full access — can manage team, billing, config, and all leads',
    'manager': 'Can manage agents, view all leads, and view KPIs',
    'agent':   'Can dial and text assigned leads only',
}

# ── Default permissions by role ───────────────────────────────────────────────

ROLE_DEFAULTS = {
    'admin': {
        "can_dial": True,
        "can_text": True,
        "can_view_all_leads": True,
        "can_import_leads": True,
        "can_change_bot_config": True,
        "can_view_call_recordings": True,
        "can_manage_numbers": True,
        "can_view_billing": True,
        "can_invite_users": True,
    },
    'manager': {
        "can_dial": True,
        "can_text": True,
        "can_view_all_leads": True,
        "can_import_leads": True,
        "can_change_bot_config": False,
        "can_view_call_recordings": True,
        "can_manage_numbers": False,
        "can_view_billing": False,
        "can_invite_users": True,
    },
    'agent': {
        "can_dial": True,
        "can_text": True,
        "can_view_all_leads": False,
        "can_import_leads": False,
        "can_change_bot_config": False,
        "can_view_call_recordings": True,
        "can_manage_numbers": False,
        "can_view_billing": False,
        "can_invite_users": False,
    },
}

DEFAULT_PERMISSIONS = ROLE_DEFAULTS['agent']


# ── Permission Enforcement Decorator ─────────────────────────────────────────

def require_permission(perm):
    """Decorator that enforces a specific permission on seat users.
    Non-seat users (managers/owners) always pass.
    Returns 403 JSON if seat user lacks the permission."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if getattr(current_user, 'is_seat_user', False):
                if not current_user.has_permission(perm):
                    return jsonify({"error": f"Permission denied: {perm}"}), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator


# ── Audit Logging ─────────────────────────────────────────────────────────────

def _audit_log(location_id, actor_email, action, target_email=None, details=None):
    """Log a team management action to the team_audit_log table."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO team_audit_log (location_id, actor_email, action, target_email, details)
            VALUES (%s, %s, %s, %s, %s)
        """, (location_id, actor_email, action, target_email,
              json.dumps(details) if details else None))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Audit log write failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


# ── Session Revocation ────────────────────────────────────────────────────────

def _revoke_seat_sessions(seat_email):
    """Invalidate all Flask sessions for a seat user by bumping their session version.
    Uses a simple approach: set a revocation timestamp that User.get checks."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE location_users
            SET session_revoked_at = NOW(), updated_at = NOW()
            WHERE email = %s
        """, (seat_email,))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Session revocation failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_manager():
    """Verify current user is a location manager (subscriber), not a seat user.
    Seat users with 'can_invite_users' permission can also manage team."""
    if getattr(current_user, 'is_seat_user', False):
        # Seat admins/managers with invite permission can manage
        if current_user.has_permission('can_invite_users'):
            return True
        return False
    if current_user.role in ('individual', 'agency_owner', 'super_admin',
                              'agency_sub_account_user'):
        return True
    return False


def _get_active_seat_count(location_id):
    """Get count of active seats for a location."""
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM location_users
            WHERE location_id = %s AND is_active = true
        """, (location_id,))
        count = cur.fetchone()[0]
        cur.close()
        return count
    except Exception:
        return 0
    finally:
        return_db_connection(conn)


def _get_ghl_users(location_id, access_token):
    """Fetch GHL users for a location using the existing sync function."""
    try:
        from ghl_sync import sync_ghl_users
        return sync_ghl_users(location_id, access_token)
    except Exception as e:
        logger.error(f"Failed to fetch GHL users: {e}")
        return []


def _send_seat_invite_email(to_email, seat_name, manager_name, invite_url):
    """Send invite email to a seat user."""
    try:
        from send_email_api import send_email
        subject = f"{manager_name} invited you to InsuranceGrokBot"
        html_body = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
            <h2 style="color:#00ff88;">You've been invited!</h2>
            <p>Hi{' ' + seat_name if seat_name else ''},</p>
            <p><strong>{manager_name}</strong> has invited you to join their InsuranceGrokBot dialer team.</p>
            <p>Click the button below to set up your account and start dialing:</p>
            <div style="text-align:center;margin:30px 0;">
                <a href="{invite_url}" style="background:#00ff88;color:#000;padding:14px 32px;border-radius:8px;text-decoration:none;font-weight:700;font-size:16px;">
                    Activate Your Account
                </a>
            </div>
            <p style="color:#888;font-size:13px;">This invite link expires in 7 days.</p>
        </div>
        """
        send_email(to=to_email, subject=subject, html=html_body)
        return True
    except Exception as e:
        logger.error(f"Seat invite email failed: {e}")
        return False


# ── GHL Users Detection ──────────────────────────────────────────────────────

@team_bp.route('/api/team/ghl-users', methods=['GET'])
@login_required
def get_ghl_users():
    """Fetch GHL users for the current location to show multi-user detection."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    from ghl_api import get_valid_token
    token = get_valid_token(current_user.location_id)
    if not token:
        return jsonify({"error": "No valid GHL token"}), 400

    users = _get_ghl_users(current_user.location_id, token)
    return jsonify({"users": users, "count": len(users)})


# ── List Seat Users ───────────────────────────────────────────────────────────

@team_bp.route('/api/team/members', methods=['GET'])
@login_required
def list_members():
    """List all seat users for the current location."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, email, full_name, ghl_user_id, role, permissions,
                   voice_activated, onboarding_status, is_active,
                   voice_config->>'twilio_phone_number' as phone_number,
                   created_at, invite_sent_at, invite_claimed_at,
                   stripe_seat_subscription_id
            FROM location_users
            WHERE location_id = %s
            ORDER BY created_at DESC
        """, (current_user.location_id,))
        members = cur.fetchall()

        now = datetime.now()
        result = []
        for m in members:
            row = dict(m)
            if row.get('created_at'):
                row['created_at'] = row['created_at'].isoformat()
            if row.get('invite_sent_at'):
                row['invite_sent_at'] = row['invite_sent_at'].isoformat()
                # Calculate expiry countdown
                expiry = m['invite_sent_at'] + timedelta(days=7)
                remaining = expiry - now
                if remaining.total_seconds() > 0:
                    row['invite_expires_in_hours'] = round(remaining.total_seconds() / 3600, 1)
                    row['invite_expired'] = False
                else:
                    row['invite_expires_in_hours'] = 0
                    row['invite_expired'] = True
            if row.get('invite_claimed_at'):
                row['invite_claimed_at'] = row['invite_claimed_at'].isoformat()
            row['has_paid_seat'] = bool(row.get('stripe_seat_subscription_id'))
            result.append(row)

        # Also return seat limit info
        active_count = sum(1 for m in result if m.get('is_active'))

        return jsonify({
            "members": result,
            "max_seats": MAX_SEATS,
            "active_seats": active_count,
            "seats_remaining": max(0, MAX_SEATS - active_count),
        })

    except Exception as e:
        logger.error(f"List members error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Invite Seat User ─────────────────────────────────────────────────────────

@team_bp.route('/api/team/invite', methods=['POST'])
@login_required
def invite_member():
    """Invite a new seat user to the location. Enforces 10-seat maximum."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json()
    email = (data.get('email') or '').strip().lower()
    full_name = (data.get('full_name') or '').strip()
    ghl_user_id = (data.get('ghl_user_id') or '').strip()
    role = (data.get('role') or 'agent').strip()

    if role not in ROLES:
        return jsonify({"error": f"Invalid role. Must be one of: {', '.join(ROLES.keys())}"}), 400

    if not email:
        return jsonify({"error": "Email is required"}), 400

    # Enforce seat limit
    active_count = _get_active_seat_count(current_user.location_id)
    if active_count >= MAX_SEATS:
        return jsonify({
            "error": f"Seat limit reached ({MAX_SEATS} max). Deactivate an existing user or contact support to increase your limit."
        }), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Check if email already exists in any table
        cur.execute("SELECT email FROM subscribers WHERE email = %s", (email,))
        if cur.fetchone():
            return jsonify({"error": "This email is already registered as a subscriber"}), 409

        cur.execute("SELECT email FROM location_users WHERE email = %s", (email,))
        if cur.fetchone():
            return jsonify({"error": "This email is already a seat user"}), 409

        invite_token = secrets.token_urlsafe(32)
        permissions = ROLE_DEFAULTS.get(role, DEFAULT_PERMISSIONS)

        cur.execute("""
            INSERT INTO location_users (location_id, email, full_name, ghl_user_id,
                                         role, permissions, invite_token, invite_sent_at,
                                         onboarding_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), 'invited')
            RETURNING id
        """, (current_user.location_id, email, full_name, ghl_user_id or None,
              role, json.dumps(permissions), invite_token))
        new_id = cur.fetchone()['id']
        conn.commit()

        # Audit log
        _audit_log(
            location_id=current_user.location_id,
            actor_email=current_user.email,
            action='invite_sent',
            target_email=email,
            details={'role': role, 'full_name': full_name, 'ghl_user_id': ghl_user_id or None}
        )

        invite_url = f"{YOUR_DOMAIN}/claim-seat?token={invite_token}"
        email_sent = _send_seat_invite_email(
            to_email=email,
            seat_name=full_name,
            manager_name=current_user.full_name or current_user.email,
            invite_url=invite_url,
        )

        return jsonify({
            "status": "success",
            "id": new_id,
            "message": f"Invite sent to {email}" if email_sent else "Invite created but email failed",
            "invite_url": invite_url if not email_sent else None,
        })

    except Exception as e:
        conn.rollback()
        logger.error(f"Invite member error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Resend Invite ─────────────────────────────────────────────────────────────

@team_bp.route('/api/team/resend-invite', methods=['POST'])
@login_required
def resend_invite():
    """Resend invite email to a pending seat user."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json()
    member_id = data.get('member_id')
    if not member_id:
        return jsonify({"error": "member_id required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, email, full_name, onboarding_status
            FROM location_users
            WHERE id = %s AND location_id = %s
        """, (member_id, current_user.location_id))
        member = cur.fetchone()

        if not member:
            return jsonify({"error": "Member not found"}), 404
        if member['onboarding_status'] == 'claimed':
            return jsonify({"error": "User has already claimed their account"}), 400

        invite_token = secrets.token_urlsafe(32)
        cur.execute("""
            UPDATE location_users
            SET invite_token = %s, invite_sent_at = NOW(), updated_at = NOW()
            WHERE id = %s
        """, (invite_token, member_id))
        conn.commit()

        _audit_log(current_user.location_id, current_user.email,
                   'invite_resent', member['email'])

        invite_url = f"{YOUR_DOMAIN}/claim-seat?token={invite_token}"
        _send_seat_invite_email(
            to_email=member['email'],
            seat_name=member['full_name'],
            manager_name=current_user.full_name or current_user.email,
            invite_url=invite_url,
        )

        return jsonify({"status": "success", "message": f"Invite resent to {member['email']}"})

    except Exception as e:
        conn.rollback()
        logger.error(f"Resend invite error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Claim Seat Account ────────────────────────────────────────────────────────

@team_bp.route('/claim-seat', methods=['GET', 'POST'])
def claim_seat():
    """Seat user claims their account using the invite token."""
    token = request.args.get('token') or request.form.get('token')
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
            SELECT lu.*, s.full_name as manager_name
            FROM location_users lu
            JOIN subscribers s ON s.location_id = lu.location_id
            WHERE lu.invite_token = %s
        """, (token,))
        seat = cur.fetchone()

        if not seat:
            flash("Invalid or expired invite link.", "danger")
            return redirect(url_for('public.home'))

        if seat['onboarding_status'] == 'claimed':
            flash("This account has already been claimed. Please log in.", "info")
            return redirect(url_for('auth.login'))

        if seat.get('invite_sent_at'):
            expiry = seat['invite_sent_at'] + timedelta(days=7)
            if datetime.now() > expiry:
                flash("This invite link has expired. Please ask your manager to resend.", "danger")
                return redirect(url_for('public.home'))

        if request.method == 'GET':
            return render_template('claim_seat.html',
                                   email=seat['email'],
                                   name=seat['full_name'],
                                   manager_name=seat.get('manager_name', ''),
                                   token=token)

        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if not password or len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template('claim_seat.html',
                                   email=seat['email'],
                                   name=seat['full_name'],
                                   manager_name=seat.get('manager_name', ''),
                                   token=token)

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template('claim_seat.html',
                                   email=seat['email'],
                                   name=seat['full_name'],
                                   manager_name=seat.get('manager_name', ''),
                                   token=token)

        password_hash = generate_password_hash(password)
        cur.execute("""
            UPDATE location_users
            SET password_hash = %s,
                invite_token = NULL,
                invite_claimed_at = NOW(),
                onboarding_status = 'claimed',
                updated_at = NOW()
            WHERE id = %s
        """, (password_hash, seat['id']))
        conn.commit()

        _audit_log(seat['location_id'], seat['email'],
                   'account_claimed', seat['email'])

        logger.info(f"Seat account claimed: {seat['email']} for location {seat['location_id']}")
        flash("Account activated! You can now log in.", "success")
        return redirect(url_for('auth.login'))

    except Exception as e:
        conn.rollback()
        logger.error(f"Claim seat error: {e}")
        flash("An error occurred. Please try again.", "danger")
        return redirect(url_for('public.home'))
    finally:
        cur.close()
        return_db_connection(conn)


# ── Update Permissions ────────────────────────────────────────────────────────

@team_bp.route('/api/team/permissions', methods=['POST'])
@login_required
def update_permissions():
    """Update a seat user's permissions."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json()
    member_id = data.get('member_id')
    permissions = data.get('permissions')

    if not member_id or permissions is None:
        return jsonify({"error": "member_id and permissions required"}), 400

    # Validate permission keys
    valid_keys = set(DEFAULT_PERMISSIONS.keys())
    filtered = {k: bool(v) for k, v in permissions.items() if k in valid_keys}

    # Seat users with invite permission cannot grant can_invite_users to others
    if getattr(current_user, 'is_seat_user', False):
        filtered.pop('can_invite_users', None)

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Get old permissions for audit
        cur.execute("SELECT email, permissions FROM location_users WHERE id = %s AND location_id = %s",
                    (member_id, current_user.location_id))
        old = cur.fetchone()
        if not old:
            return jsonify({"error": "Member not found"}), 404

        cur.execute("""
            UPDATE location_users
            SET permissions = %s, updated_at = NOW()
            WHERE id = %s AND location_id = %s
        """, (json.dumps(filtered), member_id, current_user.location_id))

        conn.commit()

        _audit_log(current_user.location_id, current_user.email,
                   'permissions_changed', old['email'],
                   {'old': old.get('permissions'), 'new': filtered})

        return jsonify({"status": "success", "permissions": filtered})

    except Exception as e:
        conn.rollback()
        logger.error(f"Update permissions error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Update Role ───────────────────────────────────────────────────────────────

@team_bp.route('/api/team/role', methods=['POST'])
@login_required
def update_role():
    """Update a seat user's role (admin/manager/agent). Auto-sets permissions."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    # Only non-seat users (location owners) can change roles
    if getattr(current_user, 'is_seat_user', False):
        return jsonify({"error": "Only the account owner can change roles"}), 403

    data = request.get_json()
    member_id = data.get('member_id')
    new_role = (data.get('role') or '').strip()

    if not member_id or new_role not in ROLES:
        return jsonify({"error": f"Valid role required: {', '.join(ROLES.keys())}"}), 400

    permissions = ROLE_DEFAULTS.get(new_role, DEFAULT_PERMISSIONS)

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT email, role FROM location_users WHERE id = %s AND location_id = %s",
                    (member_id, current_user.location_id))
        old = cur.fetchone()
        if not old:
            return jsonify({"error": "Member not found"}), 404

        cur.execute("""
            UPDATE location_users
            SET role = %s, permissions = %s, updated_at = NOW()
            WHERE id = %s AND location_id = %s
        """, (new_role, json.dumps(permissions), member_id, current_user.location_id))
        conn.commit()

        _audit_log(current_user.location_id, current_user.email,
                   'role_changed', old['email'],
                   {'old_role': old['role'], 'new_role': new_role})

        return jsonify({"status": "success", "role": new_role, "permissions": permissions})

    except Exception as e:
        conn.rollback()
        logger.error(f"Update role error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Deactivate / Reactivate Seat User ─────────────────────────────────────────

@team_bp.route('/api/team/toggle-active', methods=['POST'])
@login_required
def toggle_active():
    """Activate or deactivate a seat user. Deactivation revokes sessions."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json()
    member_id = data.get('member_id')
    is_active = data.get('is_active', True)

    if not member_id:
        return jsonify({"error": "member_id required"}), 400

    # Enforce seat limit on reactivation
    if is_active:
        active_count = _get_active_seat_count(current_user.location_id)
        if active_count >= MAX_SEATS:
            return jsonify({"error": f"Cannot reactivate — seat limit reached ({MAX_SEATS} max)"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT email FROM location_users WHERE id = %s AND location_id = %s",
                    (member_id, current_user.location_id))
        member = cur.fetchone()
        if not member:
            return jsonify({"error": "Member not found"}), 404

        cur.execute("""
            UPDATE location_users
            SET is_active = %s, updated_at = NOW()
            WHERE id = %s AND location_id = %s
        """, (bool(is_active), member_id, current_user.location_id))
        conn.commit()

        action = "activated" if is_active else "deactivated"

        # Revoke sessions on deactivation
        if not is_active:
            _revoke_seat_sessions(member['email'])

        _audit_log(current_user.location_id, current_user.email,
                   f'user_{action}', member['email'])

        return jsonify({"status": "success", "message": f"User {action}"})

    except Exception as e:
        conn.rollback()
        logger.error(f"Toggle active error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Seat User Voice Activation ────────────────────────────────────────────────

@team_bp.route('/api/team/activate-voice', methods=['POST'])
@login_required
def activate_seat_voice():
    """Activate voice for a seat user — creates their own Twilio sub-account."""
    data = request.get_json() or {}

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        if getattr(current_user, 'is_seat_user', False):
            member_id = current_user.seat_user_id
            cur.execute("SELECT * FROM location_users WHERE id = %s AND is_active = true",
                        (member_id,))
        else:
            if not _require_manager():
                return jsonify({"error": "Access denied"}), 403
            member_id = data.get('member_id')
            if not member_id:
                return jsonify({"error": "member_id required"}), 400
            cur.execute("SELECT * FROM location_users WHERE id = %s AND location_id = %s AND is_active = true",
                        (member_id, current_user.location_id))

        member = cur.fetchone()
        if not member:
            return jsonify({"error": "Member not found or inactive"}), 404

        vc = member.get('voice_config') or {}
        if vc.get('twilio_sub_account_sid'):
            return jsonify({
                "status": "success",
                "message": "Voice already activated",
                "twilio_phone_number": vc.get('twilio_phone_number', ''),
            })

        host = request.host
        webhook_base_url = f"https://{host}"

        result = twilio_provisioning.provision_subscriber(
            subscriber_email=member['email'],
            location_id=member['location_id'],
            webhook_base_url=webhook_base_url,
        )

        vc.update(result)
        vc['enabled'] = True

        cur.execute("""
            UPDATE location_users
            SET voice_config = %s, voice_activated = true, updated_at = NOW()
            WHERE id = %s
        """, (json.dumps(vc), member['id']))
        conn.commit()

        _audit_log(member['location_id'], current_user.email,
                   'voice_activated', member['email'],
                   {'sub_account_sid': result.get('twilio_sub_account_sid')})

        logger.info(f"Voice activated for seat user {member['email']}: sub={result.get('twilio_sub_account_sid')}")

        return jsonify({
            "status": "success",
            "message": "Voice account created! Now buy a phone number in the Numbers tab.",
            "twilio_phone_number": result.get('twilio_phone_number', ''),
        })

    except Exception as e:
        conn.rollback()
        logger.error(f"Seat voice activation error: {e}", exc_info=True)
        return jsonify({"error": f"Activation failed: {str(e)}"}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Per-Agent KPIs ────────────────────────────────────────────────────────────

@team_bp.route('/api/team/agent-kpis', methods=['GET'])
@login_required
def agent_kpis():
    """Per-agent KPIs: connect rate, talk time, dials/day, messages sent."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    period = request.args.get('period', 'week')
    period_map = {
        'today': "INTERVAL '1 day'",
        'week':  "INTERVAL '7 days'",
        'month': "INTERVAL '30 days'",
        'all':   "INTERVAL '365 days'",
    }
    interval = period_map.get(period, period_map['week'])

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Get all active seat users
        cur.execute("""
            SELECT id, email, full_name, role,
                   voice_config->>'twilio_phone_number' as phone_number,
                   voice_activated, created_at
            FROM location_users
            WHERE location_id = %s AND is_active = true
            ORDER BY full_name
        """, (current_user.location_id,))
        members = [dict(r) for r in cur.fetchall()]

        # Get call stats per member using their Twilio phone number
        for member in members:
            phone = member.get('phone_number', '')
            if phone:
                cur.execute(f"""
                    SELECT COUNT(*) as total_calls,
                           COUNT(*) FILTER (WHERE status = 'completed') as connected_calls,
                           COALESCE(SUM(duration), 0) as total_talk_time,
                           COALESCE(AVG(duration) FILTER (WHERE status = 'completed'), 0) as avg_talk_time,
                           COUNT(DISTINCT DATE(created_at)) as active_days,
                           MAX(created_at) as last_call
                    FROM call_history
                    WHERE location_id = %s
                      AND from_number = %s
                      AND created_at >= NOW() - {interval}
                """, (current_user.location_id, phone))
                stats = dict(cur.fetchone())

                member['total_calls'] = stats['total_calls']
                member['connected_calls'] = stats['connected_calls']
                member['connect_rate'] = round(
                    (stats['connected_calls'] / stats['total_calls'] * 100)
                    if stats['total_calls'] > 0 else 0, 1
                )
                member['total_talk_time'] = int(stats['total_talk_time'] or 0)
                member['avg_talk_time'] = round(float(stats['avg_talk_time'] or 0), 1)
                member['dials_per_day'] = round(
                    stats['total_calls'] / max(stats['active_days'], 1), 1
                )
                member['last_call'] = stats['last_call'].isoformat() if stats.get('last_call') else None
            else:
                member.update({
                    'total_calls': 0, 'connected_calls': 0, 'connect_rate': 0,
                    'total_talk_time': 0, 'avg_talk_time': 0, 'dials_per_day': 0,
                    'last_call': None,
                })

            if member.get('created_at'):
                member['created_at'] = member['created_at'].isoformat()

        return jsonify({"agents": members, "period": period})

    except Exception as e:
        logger.error(f"Agent KPIs error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Team Stats ────────────────────────────────────────────────────────────────

@team_bp.route('/api/team/stats', methods=['GET'])
@login_required
def team_stats():
    """Get aggregated stats across all seat users for the location."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE is_active = true) as active_seats,
                COUNT(*) FILTER (WHERE is_active = false) as inactive_seats,
                COUNT(*) FILTER (WHERE voice_activated = true) as voice_activated,
                COUNT(*) FILTER (WHERE onboarding_status = 'invited') as pending_invites,
                COUNT(*) FILTER (WHERE onboarding_status = 'claimed') as claimed
            FROM location_users
            WHERE location_id = %s
        """, (current_user.location_id,))
        counts = dict(cur.fetchone())
        counts['max_seats'] = MAX_SEATS

        return jsonify({"counts": counts})

    except Exception as e:
        logger.error(f"Team stats error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Audit Log Viewer ──────────────────────────────────────────────────────────

@team_bp.route('/api/team/audit-log', methods=['GET'])
@login_required
def get_audit_log():
    """Fetch team audit log entries. Manager only."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    limit = min(int(request.args.get('limit', 50)), 200)
    offset = int(request.args.get('offset', 0))

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT id, actor_email, action, target_email, details, created_at
            FROM team_audit_log
            WHERE location_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """, (current_user.location_id, limit, offset))
        entries = []
        for r in cur.fetchall():
            row = dict(r)
            row['created_at'] = row['created_at'].isoformat() if row.get('created_at') else None
            entries.append(row)

        return jsonify({"entries": entries})

    except Exception as e:
        logger.error(f"Audit log error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Seat Onboarding Status ───────────────────────────────────────────────────

@team_bp.route('/api/team/onboarding-status', methods=['GET'])
@login_required
def seat_onboarding_status():
    """Return onboarding checklist for the current seat user."""
    if not getattr(current_user, 'is_seat_user', False):
        return jsonify({"error": "Not a seat user"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT voice_activated, voice_config, full_name, onboarding_status,
                   invite_claimed_at
            FROM location_users WHERE id = %s
        """, (current_user.seat_user_id,))
        seat = cur.fetchone()

        if not seat:
            return jsonify({"error": "Account not found"}), 404

        vc = seat.get('voice_config') or {}

        checklist = [
            {
                "id": "account_claimed",
                "label": "Account activated",
                "done": seat['onboarding_status'] == 'claimed',
                "icon": "fa-user-check",
            },
            {
                "id": "voice_setup",
                "label": "Voice account created",
                "done": seat['voice_activated'],
                "icon": "fa-phone",
                "action": "Activate Voice" if not seat['voice_activated'] else None,
                "action_url": "/voice/automate-setup" if not seat['voice_activated'] else None,
            },
            {
                "id": "phone_number",
                "label": "Phone number purchased",
                "done": bool(vc.get('twilio_phone_number')),
                "icon": "fa-hashtag",
                "action": "Buy Number" if not vc.get('twilio_phone_number') else None,
                "action_tab": "numbers" if not vc.get('twilio_phone_number') else None,
            },
            {
                "id": "first_call",
                "label": "Made your first call",
                "done": False,  # Will check call_history
                "icon": "fa-phone-volume",
            },
        ]

        # Check if they've made a call
        if vc.get('twilio_phone_number'):
            cur.execute("""
                SELECT EXISTS(
                    SELECT 1 FROM call_history
                    WHERE location_id = %s AND from_number = %s
                    LIMIT 1
                )
            """, (seat.get('location_id', current_user.location_id),
                  vc['twilio_phone_number']))
            checklist[3]['done'] = cur.fetchone()[0]

        completed = sum(1 for c in checklist if c['done'])

        return jsonify({
            "checklist": checklist,
            "completed": completed,
            "total": len(checklist),
            "all_done": completed == len(checklist),
        })

    except Exception as e:
        logger.error(f"Onboarding status error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Remove Seat User ─────────────────────────────────────────────────────────

@team_bp.route('/api/team/remove', methods=['POST'])
@login_required
def remove_member():
    """Permanently remove a seat user (deactivates, doesn't delete for audit trail)."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    data = request.get_json()
    member_id = data.get('member_id')
    if not member_id:
        return jsonify({"error": "member_id required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT email FROM location_users WHERE id = %s AND location_id = %s",
                    (member_id, current_user.location_id))
        member = cur.fetchone()
        if not member:
            return jsonify({"error": "Member not found"}), 404

        cur.execute("""
            UPDATE location_users
            SET is_active = false, updated_at = NOW()
            WHERE id = %s AND location_id = %s
        """, (member_id, current_user.location_id))
        conn.commit()

        _revoke_seat_sessions(member['email'])
        _audit_log(current_user.location_id, current_user.email,
                   'user_removed', member['email'])

        return jsonify({"status": "success", "message": "Member removed"})

    except Exception as e:
        conn.rollback()
        logger.error(f"Remove member error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Seat Checkout (Stripe) ────────────────────────────────────────────────────

@team_bp.route('/api/team/checkout', methods=['POST'])
@login_required
def seat_checkout():
    """Create a Stripe checkout session for adding a seat ($49/month)."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    # Enforce seat limit
    active_count = _get_active_seat_count(current_user.location_id)
    if active_count >= MAX_SEATS:
        return jsonify({"error": f"Seat limit reached ({MAX_SEATS} max)"}), 403

    seat_price_id = os.getenv('STRIPE_SEAT_PRICE_ID', '')

    try:
        if seat_price_id:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                mode="subscription",
                allow_promotion_codes=True,
                line_items=[{"price": seat_price_id, "quantity": 1}],
                customer_email=current_user.email,
                metadata={
                    "user_email": current_user.email,
                    "location_id": current_user.location_id,
                    "purchase_type": "seat_user",
                },
                success_url=f"{YOUR_DOMAIN}/dashboard?tab=team&seat_added=1",
                cancel_url=f"{YOUR_DOMAIN}/dashboard?tab=team",
            )
        else:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                mode="subscription",
                allow_promotion_codes=True,
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": SEAT_PRICE_CENTS,
                        "recurring": {"interval": "month"},
                        "product_data": {
                            "name": "Additional Seat User",
                            "description": "Monthly seat for an additional team member on InsuranceGrokBot",
                        },
                    },
                    "quantity": 1,
                }],
                customer_email=current_user.email,
                metadata={
                    "user_email": current_user.email,
                    "location_id": current_user.location_id,
                    "purchase_type": "seat_user",
                },
                success_url=f"{YOUR_DOMAIN}/dashboard?tab=team&seat_added=1",
                cancel_url=f"{YOUR_DOMAIN}/dashboard?tab=team",
            )

        _audit_log(current_user.location_id, current_user.email,
                   'seat_checkout_started', details={'stripe_session': session.id})

        return jsonify({"checkout_url": session.url})

    except Exception as e:
        logger.error(f"Seat checkout error: {e}")
        return jsonify({"error": str(e)}), 500


# ── Billing Info ──────────────────────────────────────────────────────────────

@team_bp.route('/api/team/billing-info', methods=['GET'])
@login_required
def seat_billing_info():
    """Return seat count and billing info for the team tab."""
    if not _require_manager():
        return jsonify({"error": "Access denied"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database unavailable"}), 500

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT COUNT(*) as total_seats,
                   COUNT(*) FILTER (WHERE is_active = true) as active_seats,
                   COUNT(*) FILTER (WHERE stripe_seat_subscription_id IS NOT NULL) as paid_seats
            FROM location_users
            WHERE location_id = %s
        """, (current_user.location_id,))
        row = cur.fetchone()

        return jsonify({
            "total_seats": row['total_seats'],
            "active_seats": row['active_seats'],
            "paid_seats": row['paid_seats'],
            "max_seats": MAX_SEATS,
            "price_per_seat": SEAT_PRICE_CENTS / 100,
            "monthly_cost": (row['active_seats'] * SEAT_PRICE_CENTS) / 100,
        })

    except Exception as e:
        logger.error(f"Seat billing info error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cur.close()
        return_db_connection(conn)


# ── Available Roles List ──────────────────────────────────────────────────────

@team_bp.route('/api/team/roles', methods=['GET'])
@login_required
def get_roles():
    """Return available roles and their descriptions."""
    return jsonify({"roles": [
        {"key": k, "label": k.capitalize(), "description": v}
        for k, v in ROLES.items()
    ]})
