# blueprints/admin.py — Super-admin "God Mode" routes and admin API endpoints
#
# All routes require either ADMIN_EMAILS membership (logged-in) or a valid CRON_SECRET.
#
# Routes:
#   GET  /admin/god-mode                                        — All-users dashboard
#   POST /admin/impersonate/<email>                             — Log in as any user
#   POST /admin/revert                                          — Exit impersonation
#   GET  /admin/god-mode/logs/<location_id>                     — View any user's logs
#   GET  /admin/god-mode/subscriber/<location_id>               — Full subscriber details
#   GET|POST /api/admin/send-email                              — Send branded email
#   GET  /api/admin/marketplace-installs                        — View marketplace installs
#   POST /api/admin/marketplace-installs/<id>/send-setup-email  — Send setup email (single)
#   POST /api/admin/marketplace-installs/send-all-setup-emails  — Send setup emails (bulk)
#   GET|POST /api/admin/discover-installs                       — Discover via GHL API
#   GET|POST /api/admin/audit-ai-minutes                        — AI minutes receipt audit
#   GET  /admin/god-mode/tickets                                — Support ticket management
#   POST /admin/god-mode/tickets/<id>/status                    — Update ticket status

import os
import html as html_mod
import logging

import requests
from flask import Blueprint, request, render_template, redirect, url_for, flash, session
from flask import jsonify
from flask_login import login_required, login_user, current_user

from extensions import (ADMIN_EMAILS, YOUR_DOMAIN, safe_jsonify,
                        _is_admin_request, super_admin_required)
from db import (get_db_connection, return_db_connection, User,
                get_webhook_logs, get_all_marketplace_installs,
                get_incomplete_installs, mark_setup_email_sent,
                log_webhook_event, audit_ai_minutes, get_pool_stats)
from email_templates import _email_wrapper, _build_install_welcome_email

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__)


# ── God Mode dashboard ────────────────────────────────────────────────────────

@admin_bp.route("/admin/god-mode")
@login_required
@super_admin_required
def god_mode_dashboard():
    """Super Admin dashboard: view all users on the platform."""
    conn = get_db_connection()
    if not conn:
        return "Database error", 500
    try:
        cur = conn.cursor()

        def _table_cols(table_name):
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = %s
            """, (table_name,))
            return {r['column_name'] for r in cur.fetchall()}

        sub_cols = _table_cols('subscribers')
        ab_cols  = _table_cols('agency_billing')

        sub_oauth = ("oauth_app_type" if "oauth_app_type" in sub_cols
                     else "'marketplace' AS oauth_app_type")
        cur.execute(f"""
            SELECT email, full_name, role, subscription_tier, stripe_status,
                   location_id, created_at, onboarding_status, {sub_oauth},
                   'subscriber' AS source
            FROM subscribers
            ORDER BY created_at DESC
        """)
        subscribers = [dict(r) for r in cur.fetchall()]

        ab_role   = "role"           if "role"           in ab_cols else "'agency_owner' AS role"
        ab_stripe = "stripe_status"  if "stripe_status"  in ab_cols else "NULL AS stripe_status"
        ab_oauth  = ("oauth_app_type" if "oauth_app_type" in ab_cols
                     else "'marketplace' AS oauth_app_type")
        cur.execute(f"""
            SELECT agency_email AS email, full_name, {ab_role}, subscription_tier,
                   {ab_stripe}, location_id, created_at,
                   'active' AS onboarding_status, {ab_oauth},
                   'agency_billing' AS source
            FROM agency_billing
            ORDER BY created_at DESC
        """)
        agency_owners = [dict(r) for r in cur.fetchall()]
        cur.close()

        all_users = sorted(
            agency_owners + subscribers,
            key=lambda u: u.get('created_at') or '',
            reverse=True
        )

        return render_template(
            'god_mode.html',
            all_users=all_users,
            impersonating=session.get('impersonating_as'),
            mail_sender=os.getenv('MAIL_DEFAULT_SENDER', ''),
        )
    finally:
        return_db_connection(conn)


@admin_bp.route("/admin/impersonate/<path:target_email>", methods=["POST"])
@login_required
@super_admin_required
def impersonate_user(target_email):
    """
    Ghost Mode: log in as any user without their password.
    Saves original admin email to session so we can revert later.

    This is TRUE ghost mode — after login_user(target), current_user IS the
    target user with their own sub_account_sid, role, and subscription tier.
    All Twilio API calls, sub-account lookups, and billing checks use the target
    user's credentials — NOT the master account.
    """
    target = User.get(target_email)
    if not target:
        flash(f"User not found: {target_email}", "danger")
        return redirect(url_for('admin.god_mode_dashboard'))

    admin_email = current_user.email
    session['original_admin_email'] = admin_email
    session['impersonating_as']     = target.email

    # Run stale-data cleanup for this specific user before viewing their account
    # so the admin sees the exact same thing the customer would see
    try:
        from db import clean_subaccount_contamination
        result = clean_subaccount_contamination()
        if result.get('cleaned', 0) > 0:
            logger.info(f"[GOD MODE] Pre-impersonation cleanup fixed {result['cleaned']} records")
    except Exception as e:
        logger.warning(f"[GOD MODE] Pre-impersonation cleanup failed (non-fatal): {e}")

    login_user(target)
    logger.info(f"[GOD MODE] {admin_email} ghosting as {target.email} "
                f"(sub_sid={getattr(target, 'voice_config', {}).get('twilio_sub_account_sid', 'N/A') if hasattr(target, 'voice_config') else 'N/A'})")

    if target.is_agency_owner:
        return redirect(url_for('agency.agency_dashboard'))
    return redirect(url_for('dashboard.dashboard'))


@admin_bp.route("/admin/revert", methods=["POST"])
@login_required
def revert_impersonation():
    """Exit impersonation and return to the super admin account."""
    original_email = session.pop('original_admin_email', None)
    session.pop('impersonating_as', None)

    if original_email:
        admin_user = User.get(original_email)
        if admin_user:
            login_user(admin_user)
            logger.info(f"[GOD MODE] Reverted to {original_email}")
            return redirect(url_for('admin.god_mode_dashboard'))

    return redirect(url_for('dashboard.dashboard'))


@admin_bp.route("/admin/god-mode/logs/<path:location_id>")
@login_required
@super_admin_required
def god_mode_logs(location_id):
    """God Mode: fetch webhook logs for any location."""
    limit        = min(int(request.args.get("limit", 100)), 500)
    offset       = int(request.args.get("offset", 0))
    event_type   = request.args.get("event_type", "").strip() or None
    status_filter= request.args.get("status", "").strip() or None

    logs = get_webhook_logs(location_id, limit=limit, offset=offset,
                            event_type=event_type, status=status_filter)
    for log in logs:
        if log.get("created_at"):
            log["created_at"] = log["created_at"].isoformat() + "Z"

    return safe_jsonify({"logs": logs, "total": len(logs), "location_id": location_id})


@admin_bp.route("/admin/god-mode/subscriber/<path:location_id>")
@login_required
@super_admin_required
def god_mode_subscriber_detail(location_id):
    """God Mode: view full subscriber details including OAuth token status."""
    conn = get_db_connection()
    if not conn:
        return safe_jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT location_id, email, full_name, role, subscription_tier,
                   stripe_status, oauth_app_type,
                   access_token  IS NOT NULL AS has_access_token,
                   refresh_token IS NOT NULL AS has_refresh_token,
                   token_expires_at, onboarding_status, created_at, updated_at
            FROM subscribers WHERE location_id = %s
        """, (location_id,))
        row = cur.fetchone()
        if not row:
            return safe_jsonify({"error": "Subscriber not found"}), 404
        data = dict(row)
        for k in ('token_expires_at', 'created_at', 'updated_at'):
            if data.get(k):
                data[k] = data[k].isoformat() + "Z"
        return safe_jsonify(data)
    finally:
        cur.close()
        return_db_connection(conn)


# ── Admin email ───────────────────────────────────────────────────────────────

@admin_bp.route("/api/admin/send-email", methods=["GET", "POST"])
def api_admin_send_email():
    """Send a branded email to any address. Auth: admin login or CRON_SECRET."""
    if not _is_admin_request():
        return safe_jsonify({"error": "Admin access required. Use ?key=YOUR_CRON_SECRET"}), 403

    to_email = request.args.get("to") or (request.get_json(silent=True) or {}).get("to")
    subject  = request.args.get("subject", "Update from InsuranceGrokBot")
    message  = request.args.get("message", "")

    if not to_email:
        return safe_jsonify({"error": "Missing 'to' parameter"}), 400
    if not message:
        return safe_jsonify({"error": "Missing 'message' parameter"}), 400

    from send_email_api import send_email_via_api
    domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")

    inner = f"""
<tr>
<td style="padding: 0 40px 30px;">
  <h1 style="margin:0 0 16px;font-size:24px;font-weight:800;color:#ffffff;line-height:1.3;">
    {html_mod.escape(subject)}
  </h1>
  <div style="font-size:15px;color:#ccc;line-height:1.7;">
    {html_mod.escape(message).replace(chr(10), "<br>")}
  </div>
</td>
</tr>
<tr>
<td align="center" style="padding: 0 40px 30px;">
  <table cellpadding="0" cellspacing="0">
  <tr>
  <td style="background:linear-gradient(135deg,#00c853 0%,#00e676 100%);
             border-radius:12px;padding:16px 48px;">
    <a href="{domain_url}/login"
       style="color:#000;font-size:17px;font-weight:800;text-decoration:none;">
      Go to Dashboard &rarr;
    </a>
  </td>
  </tr>
  </table>
</td>
</tr>
"""
    html_body = _email_wrapper(inner, domain_url)
    text_body = f"{subject}\n\n{message}\n\nDashboard: {domain_url}/login"

    sent = send_email_via_api(to_email=to_email, subject=subject,
                              html_body=html_body, text_body=text_body)
    if sent:
        return safe_jsonify({"success": True, "message": f"Email sent to {to_email}"})
    return safe_jsonify({"error": f"Failed to send email to {to_email}"}), 500


@admin_bp.route("/api/admin/send-email-all", methods=["POST"])
def api_admin_send_email_all():
    """Blast a branded update email to every subscriber. Auth: admin login or CRON_SECRET."""
    if not _is_admin_request():
        return safe_jsonify({"error": "Admin access required"}), 403

    body = request.get_json(silent=True) or {}
    subject      = body.get("subject", "New Update from InsuranceGrokBot")
    update_notes = body.get("update_notes", "")  # optional green callout block

    if not subject:
        return safe_jsonify({"error": "Missing 'subject'"}), 400

    from send_email_api import send_email_via_api
    from email_templates import build_app_update_email
    from db import get_db_connection, return_db_connection

    domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
    html_body, text_body = build_app_update_email(domain_url=domain_url, update_notes=update_notes)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT email FROM subscribers WHERE email IS NOT NULL AND email <> '' ORDER BY email"
            )
            rows = cur.fetchall()
    finally:
        return_db_connection(conn)

    emails = [r["email"] if isinstance(r, dict) else r[0] for r in rows]
    if not emails:
        return safe_jsonify({"error": "No subscriber emails found"}), 404

    sent_count = 0
    failed = []
    for addr in emails:
        ok = send_email_via_api(to_email=addr, subject=subject,
                                html_body=html_body, text_body=text_body)
        if ok:
            sent_count += 1
        else:
            failed.append(addr)

    return safe_jsonify({
        "success": True,
        "total": len(emails),
        "sent": sent_count,
        "failed": len(failed),
        "failed_addresses": failed,
    })


# ── Marketplace installs ───────────────────────────────────────────────────────

@admin_bp.route("/api/admin/marketplace-installs", methods=["GET"])
def api_marketplace_installs():
    """View all marketplace installs and their OAuth status."""
    if not _is_admin_request():
        return safe_jsonify({"error": "Admin access required. Use ?key=YOUR_CRON_SECRET"}), 403

    show     = request.args.get("show", "all")
    installs = (get_incomplete_installs() if show == "incomplete"
                else get_all_marketplace_installs())
    if show == "complete":
        installs = [i for i in installs if i.get("oauth_completed")]

    for inst in installs:
        for key in ["created_at", "oauth_completed_at", "setup_email_sent_at"]:
            if inst.get(key):
                inst[key] = inst[key].isoformat() + "Z"

    return safe_jsonify({"success": True, "count": len(installs),
                         "filter": show, "installs": installs})


@admin_bp.route("/api/admin/marketplace-installs/<int:install_id>/send-setup-email",
                methods=["POST"])
def api_send_install_setup_email(install_id):
    """Manually send setup email to a specific marketplace installer."""
    if not _is_admin_request():
        return safe_jsonify({"error": "Admin access required. Use ?key=YOUR_CRON_SECRET"}), 403

    installs = get_all_marketplace_installs()
    target   = next((i for i in installs if i["id"] == install_id), None)
    if not target:
        return safe_jsonify({"error": "Install not found"}), 404

    email = target.get("user_email")
    if not email:
        return safe_jsonify({"error": "No email address for this install"}), 400

    from send_email_api import send_email_via_api
    domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
    name       = target.get("user_name") or "there"
    subject    = "Complete Your InsuranceGrokBot Setup"
    html_body  = _build_install_welcome_email(name, domain_url, recipient_email=email)
    text_body  = (
        f"Hi {name}, complete your InsuranceGrokBot setup to start converting leads: "
        f"{domain_url}/oauth/initiate"
    )

    sent = send_email_via_api(to_email=email, subject=subject,
                              html_body=html_body, text_body=text_body)
    if sent:
        mark_setup_email_sent(install_id)
        log_webhook_event("marketplace", "admin_setup_email", "success",
                          f"Admin sent setup email to {email} for install #{install_id}")
        return safe_jsonify({"success": True, "message": f"Setup email sent to {email}"})
    return safe_jsonify({"error": f"Failed to send email to {email}"}), 500


@admin_bp.route("/api/admin/marketplace-installs/send-all-setup-emails", methods=["POST"])
def api_send_all_setup_emails():
    """Send setup emails to all incomplete installs that have an email address."""
    if not _is_admin_request():
        return safe_jsonify({"error": "Admin access required. Use ?key=YOUR_CRON_SECRET"}), 403

    from send_email_api import send_email_via_api
    domain_url  = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
    incomplete  = get_incomplete_installs()
    sent_count  = 0
    errors      = []

    for inst in incomplete:
        email = inst.get("user_email")
        if not email or inst.get("setup_email_sent"):
            continue

        name      = inst.get("user_name") or "there"
        subject   = "Complete Your InsuranceGrokBot Setup"
        html_body = _build_install_welcome_email(name, domain_url, recipient_email=email)
        text_body = (
            f"Hi {name}, complete your InsuranceGrokBot setup to start converting leads: "
            f"{domain_url}/oauth/initiate"
        )
        try:
            sent = send_email_via_api(to_email=email, subject=subject,
                                      html_body=html_body, text_body=text_body)
            if sent:
                mark_setup_email_sent(inst["id"])
                sent_count += 1
                logger.info(f"Setup email sent to {email} for install #{inst['id']}")
            else:
                errors.append(f"{email}: send failed")
        except Exception as e:
            errors.append(f"{email}: {str(e)}")

    return safe_jsonify({
        "success": True,
        "sent":    sent_count,
        "skipped": len(incomplete) - sent_count - len(errors),
        "errors":  errors,
    })


@admin_bp.route("/api/admin/discover-installs", methods=["GET", "POST"])
def api_discover_installs():
    """
    Query GHL API to discover all locations that have the marketplace app installed,
    even if their OAuth callback never completed. Useful for finding lost installers.
    """
    if not _is_admin_request():
        return safe_jsonify({"error": "Admin access required"}), 403

    client_id     = os.getenv("GHL_CLIENT_ID")
    client_secret = os.getenv("GHL_CLIENT_SECRET")

    if not client_id or not client_secret:
        return safe_jsonify({"error": "GHL_CLIENT_ID and GHL_CLIENT_SECRET must be set"}), 500

    app_id  = request.args.get("appId", client_id)
    results = {
        "app_id": app_id,
        "installed_locations": [],
        "errors":              [],
        "cross_reference":     [],
    }

    # Method 1: GHL installedLocations API
    try:
        token_url = "https://services.leadconnectorhq.com/oauth/token"
        token_resp = requests.post(token_url, data={
            "client_id":     client_id,
            "client_secret": client_secret,
            "grant_type":    "client_credentials",
        }, timeout=15)

        if token_resp.ok:
            app_token = token_resp.json().get("access_token")
            if app_token:
                headers = {
                    "Authorization": f"Bearer {app_token}",
                    "Version":       "2021-07-28",
                    "Accept":        "application/json",
                }
                all_locations = []
                skip = 0
                while True:
                    page_resp = requests.get(
                        "https://services.leadconnectorhq.com/oauth/installedLocations",
                        headers=headers,
                        params={"appId": app_id, "limit": 100, "skip": skip},
                        timeout=15,
                    )
                    if page_resp.ok:
                        page_data      = page_resp.json()
                        page_locations = page_data.get("locations", page_data.get("data", []))
                        if not page_locations:
                            break
                        all_locations.extend(page_locations)
                        skip += len(page_locations)
                        if len(page_locations) < 100:
                            break
                    else:
                        results["errors"].append(
                            f"installedLocations API: {page_resp.status_code} — {page_resp.text[:300]}"
                        )
                        break
                if all_locations:
                    results["installed_locations"] = all_locations
                    results["method"] = "installedLocations_api"
                    logger.info(f"Discovered {len(all_locations)} installed locations via GHL API")
            else:
                results["errors"].append("client_credentials token had no access_token")
        else:
            results["errors"].append(
                f"client_credentials grant: {token_resp.status_code} — {token_resp.text[:300]}"
            )
    except Exception as e:
        results["errors"].append(f"API discovery error: {str(e)}")

    # Method 2: Cross-reference with our database
    conn = None
    try:
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT location_id, email, full_name,
                       access_token IS NOT NULL  AS has_token,
                       calendar_id  IS NOT NULL  AS has_calendar,
                       stripe_customer_id IS NOT NULL AS has_stripe,
                       created_at, install_completed_at
                FROM subscribers
                WHERE location_id NOT LIKE 'temp_%%'
                ORDER BY created_at DESC
            """)
            db_users = [dict(r) for r in cur.fetchall()]
            for u in db_users:
                for key in ["created_at", "install_completed_at"]:
                    if u.get(key):
                        u[key] = u[key].isoformat() + "Z"
            results["db_subscribers"] = db_users

            cur.execute("""
                SELECT agency_email, company_id,
                       access_token IS NOT NULL AS has_token,
                       created_at
                FROM agency_billing
                ORDER BY created_at DESC
            """)
            db_agencies = [dict(r) for r in cur.fetchall()]
            for a in db_agencies:
                if a.get("created_at"):
                    a["created_at"] = a["created_at"].isoformat() + "Z"
            results["db_agencies"] = db_agencies

            cur.execute("SELECT * FROM marketplace_installs ORDER BY created_at DESC")
            mkt_installs = [dict(r) for r in cur.fetchall()]
            for m in mkt_installs:
                for key in ["created_at", "oauth_completed_at", "setup_email_sent_at"]:
                    if m.get(key):
                        m[key] = m[key].isoformat() + "Z"
            results["marketplace_installs"] = mkt_installs

            cur.close()
    except Exception as e:
        results["errors"].append(f"DB cross-reference error: {str(e)}")
    finally:
        if conn:
            return_db_connection(conn)

    db_users = results.get("db_subscribers", [])
    if db_users:
        db_location_ids = {u["location_id"] for u in db_users if u.get("location_id")}
        for loc in results.get("installed_locations", []):
            loc_id = loc.get("locationId") or loc.get("location_id") or loc.get("_id")
            in_db  = loc_id in db_location_ids if loc_id else False
            results["cross_reference"].append({
                "location_id":   loc_id,
                "name":          loc.get("name") or loc.get("locationName", "Unknown"),
                "email":         loc.get("email", ""),
                "company_id":    loc.get("companyId", ""),
                "in_our_database": in_db,
                "status":        "connected" if in_db else "LOST — needs OAuth",
            })

    log_webhook_event(
        "admin", "discover_installs", "info",
        f"Admin discovered {len(results.get('installed_locations', []))} installs, "
        f"{len(results.get('cross_reference', []))} cross-referenced",
        details={"errors": results["errors"]}
    )

    return safe_jsonify(results)


# ── AI Minutes audit ──────────────────────────────────────────────────────────

@admin_bp.route("/api/admin/audit-ai-minutes", methods=["GET", "POST"])
@login_required
def api_admin_audit_ai_minutes():
    """
    Run the AI minutes receipt audit for one user or all users.

    GET  ?email=user@example.com   — audit single account
    POST body {"email": "..."}     — audit single account
    POST body {"email": "ALL"}     — audit all accounts with a balance
    """
    if not hasattr(current_user, 'email') or current_user.email not in ADMIN_EMAILS:
        return jsonify({"error": "Unauthorized"}), 403

    if request.method == "GET":
        email = request.args.get("email", "").strip().lower()
    else:
        data  = request.get_json(silent=True) or {}
        email = data.get("email", "").strip().lower()

    if not email:
        return jsonify({"error": "email param required"}), 400

    if email == "all":
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "db_unavailable"}), 503
        try:
            cur = conn.cursor()
            cur.execute("SELECT email FROM ai_minute_balances ORDER BY email")
            all_emails = [r[0] for r in cur.fetchall()]
            cur.close()
        except Exception as e:
            return jsonify({"error": "Internal server error"}), 500
        finally:
            return_db_connection(conn)

        results         = []
        corrected_count = 0
        for em in all_emails:
            res = audit_ai_minutes(em)
            results.append(res)
            if res.get("corrected"):
                corrected_count += 1

        return jsonify({"audited": len(results), "corrected": corrected_count, "results": results})

    result = audit_ai_minutes(email)
    return jsonify(result)


# ── Pool stats (admin monitoring) ──────────────────────────────────────────

@admin_bp.route("/api/admin/pool-stats")
@login_required
@super_admin_required
def pool_stats():
    """Return DB connection pool utilization stats."""
    return jsonify(get_pool_stats())


# ── Sub-account data isolation cleanup ───────────────────────────────────────

@admin_bp.route("/api/admin/clear-subaccount-contamination", methods=["POST"])
@login_required
@super_admin_required
def clear_subaccount_contamination():
    """
    One-shot cleanup: wipe trust_hub and A2P SID fields from ALL sub-account
    voice_configs that don't have a _sub_sid ownership tag.

    This fixes contamination from the old auto-discovery code that copied the
    master account's Trust Hub profiles / A2P brands into sub-account records.

    Safe to run multiple times — only touches rows where sub_sid != master_sid
    and where the SID keys exist without proper _sub_sid tagging.
    """
    import json as _json
    import os as _os

    master_sid = _os.getenv("TWILIO_ACCOUNT_SID", "")
    if not master_sid:
        return jsonify({"error": "TWILIO_ACCOUNT_SID not set"}), 500

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500

    cleaned_trust_hub = 0
    cleaned_a2p = 0
    skipped = 0
    errors = 0

    try:
        cur = conn.cursor()
        # Fetch all subscribers with non-null voice_config and a sub-account (not master)
        cur.execute("""
            SELECT email, voice_config
            FROM subscribers
            WHERE voice_config IS NOT NULL
              AND voice_config->>'twilio_sub_account_sid' IS NOT NULL
              AND voice_config->>'twilio_sub_account_sid' != %s
        """, (master_sid,))
        rows = cur.fetchall()
        cur.close()

        for row in rows:
            email = row['email']
            vc = row['voice_config'] or {}
            if not isinstance(vc, dict):
                try:
                    vc = _json.loads(vc)
                except Exception:
                    continue

            sub_sid = vc.get('twilio_sub_account_sid', '')
            changed = False

            # Clean trust_hub: remove protection fields unless _sub_sid matches this sub-account
            trust_hub = vc.get('trust_hub', {})
            if isinstance(trust_hub, dict):
                th_sub = trust_hub.get('_sub_sid', '')
                has_stale = any(trust_hub.get(k) for k in ('protection_active', 'profile_sid', 'business_name'))
                if has_stale and th_sub != sub_sid:
                    for k in ('protection_active', 'profile_sid', 'business_name', 'registered_at', '_sub_sid'):
                        trust_hub.pop(k, None)
                    vc['trust_hub'] = trust_hub
                    changed = True
                    cleaned_trust_hub += 1

            # Clean a2p: remove Twilio SID fields unless _sub_sid matches this sub-account
            a2p = vc.get('a2p', {})
            if isinstance(a2p, dict):
                a2p_sub = a2p.get('_sub_sid', '')
                has_stale = any(a2p.get(k) for k in ('brand_sid', 'campaign_sid', 'messaging_service_sid'))
                if has_stale and a2p_sub != sub_sid:
                    for k in ('brand_sid', 'brand_status', 'campaign_sid', 'campaign_status',
                              'messaging_service_sid', 'use_case', 'registered', '_sub_sid'):
                        a2p.pop(k, None)
                    vc['a2p'] = a2p
                    changed = True
                    cleaned_a2p += 1

            if changed:
                try:
                    upd_cur = conn.cursor()
                    upd_cur.execute(
                        "UPDATE subscribers SET voice_config = %s WHERE email = %s",
                        (_json.dumps(vc), email)
                    )
                    upd_cur.close()
                    conn.commit()
                    logger.info(f"[cleanup] Cleaned contaminated voice_config for {email}")
                except Exception as e:
                    logger.error(f"[cleanup] Failed to update {email}: {e}")
                    try: conn.rollback()
                    except Exception: pass
                    errors += 1
            else:
                skipped += 1

        return jsonify({
            "status": "ok",
            "subscribers_checked": len(rows),
            "trust_hub_cleaned": cleaned_trust_hub,
            "a2p_cleaned": cleaned_a2p,
            "skipped_clean": skipped,
            "errors": errors,
        })

    except Exception as e:
        logger.error(f"[cleanup] Sub-account contamination cleanup failed: {e}", exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        return_db_connection(conn)


# ── Support Tickets ──────────────────────────────────────────────────────────

@admin_bp.route("/admin/god-mode/tickets")
@login_required
@super_admin_required
def god_mode_tickets():
    """View and manage support tickets."""
    from support_bot import get_support_tickets

    status_filter = request.args.get("status", "")
    severity_filter = request.args.get("severity", "")
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))

    tickets = get_support_tickets(
        status=status_filter or None,
        severity=severity_filter or None,
        limit=limit,
        offset=offset,
    )

    # Convert to safe JSON (datetime handling)
    for t in tickets:
        for key in ("created_at", "reviewed_at", "resolved_at"):
            if t.get(key):
                t[key] = t[key].isoformat() if hasattr(t[key], "isoformat") else str(t[key])

    return safe_jsonify({
        "tickets": tickets,
        "filters": {"status": status_filter, "severity": severity_filter},
        "count": len(tickets),
    })


@admin_bp.route("/admin/god-mode/tickets/<int:ticket_id>/status", methods=["POST"])
@login_required
@super_admin_required
def update_ticket(ticket_id):
    """Update a support ticket's status and optional admin notes."""
    from support_bot import update_ticket_status

    data = request.get_json(silent=True) or {}
    new_status = data.get("status", "")
    admin_notes = data.get("admin_notes", "")

    if new_status not in ("open", "reviewed", "resolved"):
        return jsonify({"error": "Invalid status"}), 400

    success = update_ticket_status(ticket_id, new_status, admin_notes or None)
    if success:
        return jsonify({"success": True, "ticket_id": ticket_id, "status": new_status})
    return jsonify({"error": "Failed to update ticket"}), 500


# ── Reset Stale Voice Configs ─────────────────────────────────────────────────

@admin_bp.route("/api/admin/reset-stale-voice-configs", methods=["POST"])
@super_admin_required
def reset_stale_voice_configs():
    """
    Validate all subscribers' Twilio sub-accounts against the current master.
    If a sub-account doesn't exist under the current master (stale from old account),
    wipe the Twilio provisioning fields but preserve user settings.
    """
    import json
    import twilio_provisioning

    TWILIO_FIELDS_TO_CLEAR = [
        'twilio_sub_account_sid', 'twilio_sub_account_auth_token',
        'twilio_twiml_app_sid', 'twilio_phone_number', 'twilio_number_sid',
        'twilio_api_key_sid', 'twilio_api_key_secret',
    ]

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500

    report = {"checked": 0, "cleared": [], "valid": [], "errors": []}

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT email, voice_config FROM subscribers
            WHERE voice_config IS NOT NULL
              AND voice_config::text != '{}'
              AND voice_config::text != 'null'
              AND voice_config->>'twilio_sub_account_sid' IS NOT NULL
              AND voice_config->>'twilio_sub_account_sid' != ''
        """)
        rows = cur.fetchall()
        cur.close()

        master_client = twilio_provisioning.get_master_client()

        for row in rows:
            email = row['email']
            vc = row['voice_config'] if isinstance(row['voice_config'], dict) else json.loads(row['voice_config'])
            sub_sid = vc.get('twilio_sub_account_sid', '')
            if not sub_sid:
                continue

            report["checked"] += 1

            # Verify sub-account exists under current master
            try:
                account = master_client.api.accounts(sub_sid).fetch()
                if account.status == 'active':
                    report["valid"].append({"email": email, "sub_sid": sub_sid})
                    continue
                else:
                    logger.info(f"[stale-reset] {email}: sub-account {sub_sid} status={account.status} — clearing")
            except Exception as e:
                logger.info(f"[stale-reset] {email}: sub-account {sub_sid} not found on current master — clearing ({e})")

            # Stale — clear Twilio fields, preserve everything else
            old_phone = vc.get('twilio_phone_number', '')
            for field in TWILIO_FIELDS_TO_CLEAR:
                vc.pop(field, None)
            vc['enabled'] = False

            cur2 = conn.cursor()
            cur2.execute(
                "UPDATE subscribers SET voice_config = %s WHERE email = %s",
                (json.dumps(vc), email)
            )
            cur2.close()
            conn.commit()

            report["cleared"].append({"email": email, "old_sub_sid": sub_sid, "old_phone": old_phone})
            logger.info(f"[stale-reset] Cleared stale voice config for {email} (old sub={sub_sid}, old phone={old_phone})")

    except Exception as e:
        logger.error(f"[stale-reset] Error: {e}", exc_info=True)
        report["errors"].append(str(e))
    finally:
        return_db_connection(conn)

    return jsonify(report)
