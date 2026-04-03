# blueprints/oauth.py — GHL OAuth flow: initiate, callback, loading screen, subscriber refresh
#
# Routes:
#   GET  /oauth/initiate    — Start GHL OAuth (redirect to consent page)
#   GET  /oauth/callback    — GHL OAuth callback (token exchange, onboarding, DB write)
#   GET  /oauth/loading     — Post-OAuth loading screen (private app flow)
#   GET  /refresh           — Manually trigger subscriber sync
#
# Enterprise-grade rewrite (Apr 2026):
#   - Extracted helper functions for testability and clarity
#   - 13-step flow with explicit scenario handling (A-F)
#   - Location selection algorithm with DB-check for new vs existing
#   - Agency detection using role_type as primary signal
#   - No welcome email on reconnects
#   - Never overwrites existing subscriber's login email

import os
import json
import logging
import secrets
import requests
from urllib.parse import urlencode

from flask import (
    Blueprint, request, redirect, url_for, render_template,
    flash, jsonify as flask_jsonify, session
)
from flask_login import login_required, current_user, login_user

from db import (
    get_db_connection, return_db_connection, get_db_connection_with_retry,
    User, log_webhook_event, save_persistent_alert,
    mark_install_oauth_complete, find_marketplace_email,
    get_agency_by_company_id, update_subscriber_token,
)
from extensions import ADMIN_EMAILS, YOUR_DOMAIN
from email_templates import _email_wrapper, _build_welcome_email, _build_agency_owner_welcome_email
from send_email_api import send_email_via_api
from token_encryption import encrypt_token, decrypt_token
from ghl_api import fetch_all_ghl_items, ghl_api_call as _ghl_api_call, get_valid_token_with_status

logger = logging.getLogger(__name__)

oauth_bp = Blueprint('oauth', __name__)

# ── OAuth scopes ─────────────────────────────────────────────────────────────
# Synced exactly to the marketplace app install URL as of 2026-03-26.
# version_id=69baca8609da2bbc1060ae05
GHL_OAUTH_SCOPES = [
    "calendars.readonly",
    "calendars/events.readonly",
    "calendars/events.write",
    "calendars/groups.readonly",
    "contacts.readonly",
    "contacts.write",
    "conversations.readonly",
    "conversations.write",
    "conversations/message.readonly",
    "conversations/message.write",
    "locations.readonly",
    "locations/customFields.readonly",
    "locations/customFields.write",
    "locations/customValues.readonly",
    "locations/tags.readonly",
    "locations/tags.write",
    "numberpools.read",
    "oauth.readonly",
    "oauth.write",
    "opportunities.readonly",
    "opportunities.write",
    "phonenumbers.read",
    "users.readonly",
    "workflows.readonly",
    "twilioaccount.read",
]


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS — extracted for testability and clarity
# ═══════════════════════════════════════════════════════════════════════════════

def _get_existing_location_ids(conn, location_ids):
    """Check which location_ids already have subscriber rows. Returns a set."""
    if not location_ids:
        return set()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT location_id FROM subscribers WHERE location_id = ANY(%s)",
            (list(location_ids),)
        )
        result = {r['location_id'] for r in cur.fetchall()}
        cur.close()
        return result
    except Exception as e:
        logger.warning(f"_get_existing_location_ids failed: {e}")
        return set()


def _determine_primary_location(token_location_id, user_location_ids,
                                sub_accounts, company_id, db_conn):
    """
    Determine which GHL location the installing user belongs to.
    Uses only documented GHL API sources — no undocumented URL params.

    Returns (location_id, resolution_method) tuple.

    Priority:
      1. token_location_id (authoritative — from token exchange response)
      2. sub_accounts — find the NEW location not yet in DB
      3. user_location_ids from /users/{userId} roles — find NEW location
      4. None (failed — caller uses companyId fallback)
    """
    # Priority 1: token_location_id (authoritative — from token exchange)
    if token_location_id:
        return token_location_id, 'token_direct'

    # Priority 2: sub_accounts — find the NEW location (not in DB)
    if sub_accounts and db_conn:
        sub_ids = [s['id'] for s in sub_accounts]
        existing = _get_existing_location_ids(db_conn, sub_ids)
        new_locs = [lid for lid in sub_ids if lid not in existing]
        if len(new_locs) == 1:
            return new_locs[0], 'new_install_detected'
        elif len(new_locs) > 1:
            # Multiple new — use last (most recently added in GHL install order)
            return new_locs[-1], 'multiple_new_last'
        elif sub_ids:
            # All locations already registered — reconnect scenario
            return sub_ids[0], 'reconnect_first'

    # Priority 3: user_location_ids — find the NEW location
    if user_location_ids and db_conn:
        if len(user_location_ids) == 1:
            return user_location_ids[0], 'single_role_location'
        existing = _get_existing_location_ids(db_conn, user_location_ids)
        new_locs = [lid for lid in user_location_ids if lid not in existing]
        if len(new_locs) == 1:
            return new_locs[0], 'new_from_roles'
        elif len(new_locs) > 1:
            return new_locs[-1], 'multiple_new_roles'
        elif user_location_ids:
            return user_location_ids[0], 'reconnect_roles'

    return None, 'failed'


def _detect_agency_owner(user_role_type, company_id, user_location_ids,
                         token_user_type, user_email):
    """
    Determine if the installing user is an agency owner.

    Uses role_type as the STRONGEST signal:
      - 'account' = sub-account user → NEVER agency owner
      - 'agency' = agency-level user → agency owner (unless someone else owns it)
      - No role type → fall back to Company token + no location signals

    Returns bool.
    """
    # Role type is the STRONGEST signal
    if user_role_type == 'account':
        return False  # Sub-account user — NEVER agency owner

    if user_role_type == 'agency':
        # Check if agency already has a different owner
        if company_id:
            existing_agency = get_agency_by_company_id(company_id)
            if existing_agency and existing_agency.get('agency_email', '').lower() != user_email.lower():
                return False  # Already owned by someone else
        return True

    # No role type (fallback signals)
    if company_id and not user_location_ids and token_user_type == 'Company':
        # Company token with no location roles = likely agency owner
        if company_id:
            existing_agency = get_agency_by_company_id(company_id)
            if existing_agency and existing_agency.get('agency_email', '').lower() != user_email.lower():
                return False
        return True

    return False


def _fetch_installed_locations(headers, company_id, client_id):
    """
    Fetch installed locations from GHL OAuth API with pagination.
    Returns list of location dicts. Empty list on failure.
    """
    base_urls = [
        (
            f"https://services.leadconnectorhq.com/oauth/installedLocations"
            f"?companyId={company_id}&appId={client_id}"
        ),
        (
            f"https://services.leadconnectorhq.com/oauth/installedLocations"
            f"?companyId={company_id}"
        ),
    ]
    PAGE_LIMIT = 100

    for base_url in base_urls:
        all_locs = []
        skip = 0
        while True:
            sep = '&' if '?' in base_url else '?'
            url = f"{base_url}{sep}skip={skip}&limit={PAGE_LIMIT}"
            resp, err = _ghl_api_call(
                'GET', url, headers=headers, timeout=15,
                label="installedLocations"
            )
            if resp and resp.ok:
                try:
                    data = resp.json()
                    raw = (
                        data.get('locations')
                        or data.get('installedLocations')
                        or (data if isinstance(data, list) else [])
                    )
                    page_locs = [loc for loc in raw if isinstance(loc, dict)]
                    if not page_locs:
                        break  # No more results
                    all_locs.extend(page_locs)
                    if len(page_locs) < PAGE_LIMIT:
                        break  # Last page
                    skip += PAGE_LIMIT
                except Exception as e:
                    logger.warning(f"installedLocations parse error: {e}")
                    break
            else:
                status = resp.status_code if resp else None
                body = resp.text[:300] if resp else None
                logger.warning(
                    f"installedLocations {url}: status={status}, err={err}, body={body}"
                )
                break
        if all_locs:
            logger.info(f"installedLocations: {len(all_locs)} locations (paginated)")
            return all_locs
    return []


def _generate_location_tokens(headers, company_id, installed_locs):
    """
    Generate per-location tokens via POST /oauth/locationToken.
    Returns sub_accounts list with per-location tokens.
    """
    sub_accounts = []
    loc_token_url = "https://services.leadconnectorhq.com/oauth/locationToken"
    for loc in installed_locs:
        loc_id = loc.get('_id') or loc.get('id') or loc.get('locationId')
        if not loc_id:
            continue
        try:
            resp = requests.post(
                loc_token_url,
                json={'companyId': company_id, 'locationId': loc_id},
                headers={**headers, 'Content-Type': 'application/json'},
                timeout=10,
            )
            if resp.ok:
                data = resp.json()
                tok = data.get('access_token')
                if tok:
                    sub_accounts.append({
                        'id': loc_id,
                        'name': loc.get('name', 'Unknown Location'),
                        'timezone': loc.get('timezone'),
                        '_loc_token': tok,
                        '_loc_refresh': data.get('refresh_token'),
                        '_loc_expires': int(data.get('expires_in') or 86400),
                    })
                    logger.info(f"locationToken OK: {loc_id}")
                    continue
            logger.warning(
                f"locationToken {loc_id}: {resp.status_code} {resp.text[:200]}"
            )
        except Exception as e:
            logger.warning(f"locationToken exception {loc_id}: {e}")
    return sub_accounts


def _fetch_location_users(loc_id, loc_token):
    """
    Fetch users at a location via GET /users/?locationId={id}.
    Uses the per-location token (users.readonly scope).
    Returns list of {email, name, id} dicts. Empty list on any failure.
    """
    if not loc_token:
        return []
    headers = {'Authorization': f'Bearer {loc_token}', 'Version': '2021-07-28'}
    resp, err = _ghl_api_call(
        'GET', f"https://services.leadconnectorhq.com/users/?locationId={loc_id}",
        headers=headers, timeout=10, label=f"/users/?locationId={loc_id}"
    )
    if not resp or not resp.ok:
        logger.info(f"_fetch_location_users {loc_id}: {resp.status_code if resp else err}")
        return []
    try:
        data = resp.json()
        users = data.get('users') or (data if isinstance(data, list) else [])
        result = []
        for u in users:
            email = u.get('email', '').strip().lower()
            if email:
                result.append({
                    'email': email,
                    'name': u.get('name') or f"{u.get('firstName','')} {u.get('lastName','')}".strip(),
                    'id': u.get('id'),
                    'role': u.get('roles', {}).get('type', 'account') if isinstance(u.get('roles'), dict) else 'account',
                })
        logger.info(f"_fetch_location_users {loc_id}: {len(result)} users")
        return result
    except Exception as e:
        logger.warning(f"_fetch_location_users parse error {loc_id}: {e}")
        return []


def _resolve_user_email(token_data, primary_location_id, company_id):
    """
    5-source email recovery chain for marketplace installs (user not logged in).
    Returns (email, name, source) tuple. Never returns None for email.
    """
    user_email = None
    user_name = None

    # Source 1: token_data (marketplace installs)
    user_email = token_data.get('userEmail') or token_data.get('email')
    if user_email:
        return user_email, None, 'token_data'

    # Source 2: marketplace_installs table bridge
    market_data = find_marketplace_email(
        location_id=primary_location_id, company_id=company_id
    )
    if market_data:
        user_email = market_data.get('user_email')
        user_name = market_data.get('user_name')
        if user_email:
            return user_email, user_name, 'marketplace_installs'

    # Source 3: look up by GHL userId in subscribers table
    ghl_user_id = token_data.get('userId')
    if ghl_user_id:
        conn_lookup = None
        try:
            conn_lookup = get_db_connection()
            if conn_lookup:
                cur_lookup = conn_lookup.cursor()
                cur_lookup.execute(
                    "SELECT email FROM subscribers WHERE crm_user_id = %s LIMIT 1",
                    (ghl_user_id,)
                )
                found = cur_lookup.fetchone()
                if found:
                    cur_lookup.close()
                    return found['email'], None, 'userId_lookup'
                cur_lookup.close()
        except Exception:
            pass
        finally:
            if conn_lookup:
                return_db_connection(conn_lookup)

    # Source 4: placeholder — onboarding completes, admin gets notified
    ghl_user_id = token_data.get('userId') or 'unknown'
    user_email = f"install_{ghl_user_id}@placeholder.grokbot"
    user_name = "New User (Update Email)"
    return user_email, user_name, 'placeholder'


def _send_ghost_install_alert(user_email, ghl_user_id, primary_location_id, company_id):
    """Send admin notification for ghost installs (placeholder email)."""
    logger.warning(f"ALL email sources exhausted. Using placeholder: {user_email}")

    try:
        log_webhook_event(
            primary_location_id or "unknown", "oauth_placeholder_account", "warning",
            f"Placeholder account created: {user_email} — userId={ghl_user_id}, "
            f"locationId={primary_location_id}, companyId={company_id}",
            details={
                "userId": ghl_user_id,
                "locationId": primary_location_id,
                "companyId": company_id,
            }
        )
    except Exception:
        pass

    try:
        admin_target = ADMIN_EMAILS[0] if ADMIN_EMAILS else "mitchell_vandusen@hotmail.com"
        domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
        alert_inner = f'''
<tr><td style="padding: 20px 40px 30px;">
    <h1 style="margin: 0 0 16px; font-size: 22px; font-weight: 800; color: #ff6b35;">Ghost Install Detected</h1>
    <p style="font-size: 15px; color: #ccc; line-height: 1.6;">
        A user installed the app but Lead Connector permissions blocked their email.
        A placeholder account was created so they can access the dashboard.
    </p>
    <table cellpadding="0" cellspacing="0" width="100%" style="background: rgba(255,255,255,0.04); border-radius: 10px; border: 1px solid rgba(255,255,255,0.08); margin: 20px 0;">
        <tr><td style="padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #888; width: 130px;">LC User ID</td>
            <td style="padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #fff; font-family: monospace;">{ghl_user_id}</td></tr>
        <tr><td style="padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #888;">Location ID</td>
            <td style="padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.06); color: #fff; font-family: monospace;">{primary_location_id or 'N/A'}</td></tr>
        <tr><td style="padding: 12px 16px; color: #888;">Company ID</td>
            <td style="padding: 12px 16px; color: #fff; font-family: monospace;">{company_id or 'N/A'}</td></tr>
    </table>
    <p style="font-size: 14px; color: #aaa;">Search this Location ID in your Lead Connector Agency View to find the user\'s real email, then update their record in the database.</p>
</td></tr>'''
        send_email_via_api(
            to_email=admin_target,
            subject="Ghost Install — Action Required",
            html_body=_email_wrapper(alert_inner, domain_url),
            text_body=f"Ghost install: userId={ghl_user_id}, "
                      f"locationId={primary_location_id}, companyId={company_id}"
        )
        logger.info(f"Admin ghost-install alert sent to {admin_target}")
    except Exception as e:
        logger.error(f"Failed to send admin ghost-install alert: {e}")

    try:
        save_persistent_alert(
            email=ADMIN_EMAILS[0] if ADMIN_EMAILS else "admin",
            alert_type="ghost_install",
            title="Ghost Install — Email Unknown",
            message=(
                f"User installed app but email couldn't be retrieved. "
                f"userId={ghl_user_id}, locationId={primary_location_id}, companyId={company_id}. "
                f"Placeholder account: {user_email}"
            ),
            severity="warning",
            location_id=primary_location_id or "unknown",
        )
    except Exception:
        pass


def _build_company_metadata(me_data, company_id):
    """Extract company metadata from user info response."""
    metadata = {
        'company_id': company_id,
        'company_name': None,
        'company_owner_name': me_data.get('name') or me_data.get('firstName', ''),
        'company_owner_email': me_data.get('email'),
        'company_owner_phone': me_data.get('phone'),
    }
    if me_data.get('companyName'):
        metadata['company_name'] = me_data['companyName']
    elif isinstance(me_data.get('company'), dict) and me_data['company'].get('name'):
        metadata['company_name'] = me_data['company']['name']

    if not metadata['company_owner_name']:
        first = me_data.get('firstName', '')
        last = me_data.get('lastName', '')
        metadata['company_owner_name'] = f"{first} {last}".strip() or None

    return metadata


# ═══════════════════════════════════════════════════════════════════════════════
# DB OPERATIONS — extracted for clarity
# ═══════════════════════════════════════════════════════════════════════════════

def _upsert_agency_owner(cur, user_email, agency_location_id, primary_name,
                         enc_access_token, enc_refresh_token, expires_in,
                         primary_timezone, me_data, crm_email_resolved,
                         app_type, company_id, company_metadata,
                         token_user_type_used):
    """Insert or update subscribers + agency_billing for agency owner."""
    _company_token = enc_access_token if token_user_type_used == 'Company' else None
    _company_refresh = enc_refresh_token if token_user_type_used == 'Company' else None

    cur.execute("""
        INSERT INTO subscribers (
            email, location_id, full_name, role,
            subscription_tier, access_token, refresh_token,
            token_expires_at, timezone, crm_user_id, crm_email,
            oauth_app_type, company_id,
            company_access_token, company_refresh_token, company_token_expires_at,
            created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s,
            NOW() + interval '%s seconds', %s, %s, %s, %s, %s,
            %s, %s, CASE WHEN %s IS NOT NULL THEN NOW() + interval '%s seconds' END,
            NOW(), NOW()
        )
        ON CONFLICT (email) DO UPDATE SET
            location_id = COALESCE(EXCLUDED.location_id, subscribers.location_id),
            role = 'agency_owner',
            access_token = EXCLUDED.access_token,
            refresh_token = EXCLUDED.refresh_token,
            token_expires_at = EXCLUDED.token_expires_at,
            crm_user_id = COALESCE(EXCLUDED.crm_user_id, subscribers.crm_user_id),
            crm_email = EXCLUDED.crm_email,
            oauth_app_type = EXCLUDED.oauth_app_type,
            company_id = COALESCE(EXCLUDED.company_id, subscribers.company_id),
            company_access_token = COALESCE(EXCLUDED.company_access_token, subscribers.company_access_token),
            company_refresh_token = COALESCE(EXCLUDED.company_refresh_token, subscribers.company_refresh_token),
            company_token_expires_at = COALESCE(EXCLUDED.company_token_expires_at, subscribers.company_token_expires_at),
            updated_at = NOW()
    """, (
        user_email, agency_location_id, primary_name, 'agency_owner',
        'agency_owner', enc_access_token, enc_refresh_token,
        expires_in, primary_timezone or 'America/Chicago', me_data.get('id'),
        crm_email_resolved, app_type,
        company_metadata.get('company_id') or company_id,
        _company_token, _company_refresh, _company_token, expires_in,
    ))

    # THIN agency_billing: only agency-specific metadata
    cur.execute("""
        INSERT INTO agency_billing (
            agency_email, company_id, company_name,
            company_owner_name, company_owner_email, company_owner_phone,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (agency_email) DO UPDATE SET
            company_id = COALESCE(EXCLUDED.company_id, agency_billing.company_id),
            company_name = COALESCE(EXCLUDED.company_name, agency_billing.company_name),
            company_owner_name = COALESCE(EXCLUDED.company_owner_name, agency_billing.company_owner_name),
            company_owner_email = COALESCE(EXCLUDED.company_owner_email, agency_billing.company_owner_email),
            company_owner_phone = COALESCE(EXCLUDED.company_owner_phone, agency_billing.company_owner_phone),
            updated_at = NOW()
    """, (
        user_email,
        company_metadata.get('company_id') or company_id,
        company_metadata.get('company_name'),
        company_metadata.get('company_owner_name'),
        company_metadata.get('company_owner_email'),
        company_metadata.get('company_owner_phone'),
    ))

    # Auto-populate white-label branding from GHL company name
    _wl_company = company_metadata.get('company_name')
    if _wl_company:
        cur.execute("""
            UPDATE agency_billing
            SET whitelabel_config = COALESCE(whitelabel_config, '{}'::jsonb) || %s::jsonb
            WHERE agency_email = %s
              AND (whitelabel_config IS NULL OR NOT (whitelabel_config ? 'company_name'))
        """, (json.dumps({'company_name': _wl_company}), user_email))

    logger.info(f"Agency owner {user_email}: subscribers + agency_billing upserted")


def _update_existing_location(cur, primary_location_id, crm_email_resolved,
                              enc_access_token, enc_refresh_token, expires_in,
                              me_data, app_type, sync_role, user_email,
                              use_agency_flow, company_id, token_user_type_used):
    """
    Update tokens on an existing subscriber row (reconnect scenario).
    Returns the corrected user_email (may differ from CRM email).
    """
    # Look up the existing row's login email
    cur.execute(
        "SELECT location_id, email FROM subscribers WHERE location_id = %s",
        (primary_location_id,)
    )
    existing_by_location = cur.fetchone()
    if not existing_by_location:
        return None, None  # No existing row

    # CRITICAL: Correct user_email to the subscriber's actual login email.
    # CRM email (from /users/ API) may differ from login email.
    existing_login_email = existing_by_location['email']
    corrected_email = existing_login_email if existing_login_email else user_email
    if existing_login_email and existing_login_email != user_email:
        logger.info(
            f"Correcting user_email from CRM email {user_email!r} to "
            f"subscriber's login email {existing_login_email!r} "
            f"(existing subscriber found by location_id={primary_location_id})"
        )

    _company_token = enc_access_token if token_user_type_used == 'Company' else None
    _company_refresh = enc_refresh_token if token_user_type_used == 'Company' else None
    cur.execute("""
        UPDATE subscribers
        SET crm_email = %s,
            access_token = %s,
            refresh_token = %s,
            token_expires_at = NOW() + interval '%s seconds',
            crm_user_id = COALESCE(%s, crm_user_id),
            oauth_app_type = %s,
            role = COALESCE(%s, role),
            parent_agency_email = COALESCE(%s, parent_agency_email),
            company_id = COALESCE(%s, company_id),
            company_access_token = COALESCE(%s, company_access_token),
            company_refresh_token = COALESCE(%s, company_refresh_token),
            company_token_expires_at = CASE WHEN %s IS NOT NULL THEN NOW() + interval '%s seconds' ELSE company_token_expires_at END,
            onboarding_status = CASE
                WHEN onboarding_status IN ('pending', 'invited') THEN 'claimed'
                ELSE onboarding_status
            END,
            updated_at = NOW()
        WHERE location_id = %s
    """, (
        crm_email_resolved, enc_access_token, enc_refresh_token,
        expires_in, me_data.get('id'), app_type,
        sync_role, corrected_email if use_agency_flow else None,
        company_id,
        _company_token, _company_refresh,
        _company_token, expires_in,
        primary_location_id
    ))
    logger.info(
        f"Synced OAuth tokens for existing location {primary_location_id} "
        f"(login: {corrected_email}, crm_email: {crm_email_resolved}, app_type={app_type})"
    )

    # Clean up orphaned temp_* rows that share the CRM email
    if crm_email_resolved and crm_email_resolved != corrected_email:
        cur.execute(
            "DELETE FROM subscribers WHERE email = %s AND location_id LIKE 'temp_%%'",
            (crm_email_resolved,)
        )
        deleted = cur.rowcount
        if deleted:
            logger.info(
                f"Cleaned up {deleted} orphaned temp account(s) for CRM email "
                f"{crm_email_resolved!r} (real subscriber: {corrected_email!r})"
            )

    return corrected_email, existing_by_location


def _provision_new_subscriber(cur, sub, user_email, crm_email_resolved,
                              enc_access_token, enc_refresh_token, expires_in,
                              me_data, app_type, use_agency_flow,
                              auto_linked_agency_email, company_id,
                              primary_location_id, token_user_type_used):
    """
    Insert a new subscriber row for a location.
    Handles location_id conflict (upsert) and email conflict (savepoint + update).
    Returns True if the location was owned by another user (skipped), False otherwise.
    """
    sub_id = sub['id']
    sub_name = sub.get('name', 'Unknown Location')
    sub_timezone = sub.get('timezone')

    is_owner_location = (sub_id == primary_location_id)
    if use_agency_flow and is_owner_location:
        role = 'agency_owner'
    elif use_agency_flow:
        # Agent locations pre-provisioned by agency install get 'individual'
        # role — they're regular agents, just pre-populated by agency owner
        role = 'individual'
    else:
        role = 'individual'
    parent_agency_email = user_email if use_agency_flow else auto_linked_agency_email
    plan_tier = 'agency_owner' if (use_agency_flow and is_owner_location) else 'individual'

    # For agency sub-account locations (not the owner's primary):
    # Use the real agent email if we discovered it via users.readonly scope.
    # Fall back to placeholder if unknown — real email gets set when agent logs in.
    if use_agency_flow and not is_owner_location:
        effective_email = sub.get('_primary_email') or f"install_{sub_id}@pending.grokbot"
        if sub.get('_primary_name') and not sub_name:
            sub_name = sub['_primary_name']
    else:
        effective_email = user_email

    # CRITICAL: Never overwrite another user's subscriber row
    cur.execute(
        "SELECT email FROM subscribers WHERE location_id = %s",
        (sub_id,)
    )
    existing_owner = cur.fetchone()
    if existing_owner and existing_owner['email'].lower() != effective_email.lower():
        logger.warning(
            f"Location {sub_id} already owned by {existing_owner['email']} — "
            f"not overwriting with {effective_email}'s data."
        )
        # Refresh tokens for existing agent via company install
        _loc_tok = sub.get('_loc_token')
        _loc_ref = sub.get('_loc_refresh')
        _loc_exp = sub.get('_loc_expires') or expires_in
        _agency_email = user_email if use_agency_flow else auto_linked_agency_email
        if _loc_tok and use_agency_flow:
            cur.execute("""
                UPDATE subscribers
                SET parent_agency_email = COALESCE(%s, parent_agency_email),
                    company_id = COALESCE(%s, company_id),
                    access_token = %s,
                    refresh_token = %s,
                    token_expires_at = NOW() + interval '%s seconds',
                    updated_at = NOW()
                WHERE location_id = %s
            """, (
                _agency_email, company_id,
                encrypt_token(_loc_tok),
                encrypt_token(_loc_ref) if _loc_ref else None,
                _loc_exp,
                sub_id,
            ))
            logger.info(
                f"Refreshed tokens for agent location {sub_id} "
                f"({existing_owner['email']}) via company install"
            )
        elif use_agency_flow or auto_linked_agency_email:
            cur.execute("""
                UPDATE subscribers
                SET parent_agency_email = COALESCE(%s, parent_agency_email),
                    company_id = COALESCE(%s, company_id),
                    updated_at = NOW()
                WHERE location_id = %s
            """, (_agency_email, company_id, sub_id))
        return True  # Skipped — owned by another user

    # Determine effective tokens (per-location vs company)
    _loc_tok_new = sub.get('_loc_token')
    _loc_ref_new = sub.get('_loc_refresh')
    _loc_exp_new = sub.get('_loc_expires') or expires_in

    if _loc_tok_new:
        _eff_access = encrypt_token(_loc_tok_new)
        _eff_refresh = encrypt_token(_loc_ref_new) if _loc_ref_new else None
        _eff_expires = _loc_exp_new
        _ct_prov = enc_access_token
        _cr_prov = enc_refresh_token
    else:
        _eff_access = enc_access_token
        _eff_refresh = enc_refresh_token
        _eff_expires = expires_in
        _ct_prov = enc_access_token if token_user_type_used == 'Company' else None
        _cr_prov = enc_refresh_token if token_user_type_used == 'Company' else None

    _sub_params = (
        sub_id, effective_email, crm_email_resolved, sub_name, role,
        plan_tier, parent_agency_email, company_id,
        _eff_access, _eff_refresh,
        _eff_expires,
        sub_timezone or 'America/Chicago', me_data.get('id'),
        'pending', app_type,
        _ct_prov, _cr_prov, _ct_prov, _eff_expires,
    )

    # Use savepoint so rollback on email conflict doesn't kill prior work
    cur.execute("SAVEPOINT sub_upsert")
    try:
        cur.execute("""
            INSERT INTO subscribers (
                location_id, email, crm_email, full_name, role,
                subscription_tier, parent_agency_email, company_id,
                access_token, refresh_token,
                token_expires_at, timezone, crm_user_id,
                onboarding_status, oauth_app_type,
                company_access_token, company_refresh_token, company_token_expires_at,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                NOW() + interval '%s seconds',
                %s, %s, %s, %s,
                %s, %s, CASE WHEN %s IS NOT NULL THEN NOW() + interval '%s seconds' END,
                NOW(), NOW()
            )
            ON CONFLICT (location_id) DO UPDATE SET
                crm_email = EXCLUDED.crm_email,
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                token_expires_at = EXCLUDED.token_expires_at,
                crm_user_id = COALESCE(EXCLUDED.crm_user_id, subscribers.crm_user_id),
                oauth_app_type = EXCLUDED.oauth_app_type,
                company_id = COALESCE(EXCLUDED.company_id, subscribers.company_id),
                parent_agency_email = COALESCE(EXCLUDED.parent_agency_email, subscribers.parent_agency_email),
                company_access_token = COALESCE(EXCLUDED.company_access_token, subscribers.company_access_token),
                company_refresh_token = COALESCE(EXCLUDED.company_refresh_token, subscribers.company_refresh_token),
                company_token_expires_at = COALESCE(EXCLUDED.company_token_expires_at, subscribers.company_token_expires_at),
                updated_at = NOW()
        """, _sub_params)
        cur.execute("RELEASE SAVEPOINT sub_upsert")
    except Exception as _upsert_err:
        cur.execute("ROLLBACK TO SAVEPOINT sub_upsert")
        if 'idx_subscribers_email' in str(_upsert_err) or 'unique' in str(_upsert_err).lower():
            logger.warning(
                f"Email {effective_email} already in subscribers with different location_id. "
                f"Updating tokens and location to {sub_id}."
            )
            cur.execute("""
                UPDATE subscribers SET
                    location_id = %s,
                    crm_email = %s,
                    access_token = %s,
                    refresh_token = %s,
                    token_expires_at = NOW() + interval '%s seconds',
                    crm_user_id = COALESCE(%s, crm_user_id),
                    oauth_app_type = %s,
                    company_id = COALESCE(%s, company_id),
                    parent_agency_email = COALESCE(%s, parent_agency_email),
                    company_access_token = COALESCE(%s, company_access_token),
                    company_refresh_token = COALESCE(%s, company_refresh_token),
                    company_token_expires_at = CASE WHEN %s IS NOT NULL THEN NOW() + interval '%s seconds' ELSE company_token_expires_at END,
                    updated_at = NOW()
                WHERE email = %s
            """, (
                sub_id, crm_email_resolved,
                enc_access_token, enc_refresh_token,
                expires_in, me_data.get('id'), app_type,
                company_id, parent_agency_email,
                _ct_prov, _cr_prov, _ct_prov, expires_in,
                effective_email
            ))
        else:
            raise

    return False  # Not skipped


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@oauth_bp.route("/refresh")
@login_required
def refresh_subscribers():
    """Manual OAuth token refresh for current user."""
    try:
        token, refreshed, err = get_valid_token_with_status(
            current_user.location_id, force_refresh=True
        )
        if token and refreshed:
            flash("OAuth token refreshed successfully.", "success")
        elif err:
            flash(f"Token refresh failed: {err}", "danger")
        else:
            flash("Token is still valid.", "info")
    except Exception as e:
        flash(f"Refresh failed: {e}", "danger")
    return redirect(url_for('dashboard.dashboard'))


@oauth_bp.route("/oauth/initiate")
@login_required
def oauth_initiate():
    """
    Initiates OAuth flow with Lead Connector.
    Always uses the public marketplace app. The marketplace URL includes
    version_id to pin to the exact approved version with correct scopes.

    User clicks "Connect CRM" -> Redirected to GHL consent page -> Back to /oauth/callback

    SECURITY: Requires active login. Agency owners are FREE (no subscription check).
    """
    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
    is_agency = (current_user.role or '').lower() == 'agency_owner'
    needs_subscription = not current_user.stripe_customer_id and not is_admin and not is_agency

    if needs_subscription:
        flash("You must have an active subscription to connect Lead Connector. Please subscribe first.", "error")
        logger.warning(f"OAuth initiate blocked for {current_user.email} - no active subscription")
        return redirect(url_for('dashboard.dashboard'))

    client_id = os.getenv("GHL_CLIENT_ID")
    domain = os.getenv("YOUR_DOMAIN")
    if not client_id or not domain:
        logger.error(
            f"OAuth initiate failed: GHL_CLIENT_ID={'set' if client_id else 'MISSING'}, "
            f"YOUR_DOMAIN={'set' if domain else 'MISSING'}"
        )
        flash("OAuth is not configured. Please contact support.", "error")
        return redirect(url_for('dashboard.dashboard'))

    redirect_uri = f"{domain}/oauth/callback"
    scope_string = " ".join(GHL_OAUTH_SCOPES)

    # CSRF protection: cryptographic state nonce
    nonce = secrets.token_urlsafe(32)
    state = f"website_user:{nonce}"
    session["ghl_oauth_state"] = state

    oauth_params_dict = {
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'client_id': client_id,
        'scope': scope_string,
        'state': state,
    }

    oauth_params = urlencode(oauth_params_dict)
    oauth_url = f"https://marketplace.gohighlevel.com/oauth/chooselocation?{oauth_params}"

    logger.info(
        f"Initiating OAuth flow for {current_user.email}. "
        f"Redirecting to GHL marketplace consent page."
    )
    return redirect(oauth_url)


@oauth_bp.route("/oauth/callback")
def oauth_callback():
    """
    GHL OAuth callback — enterprise-grade 13-step flow.

    Handles 6 installation scenarios:
      A. New individual agent (Company token, role_type=account)
      B. Agency owner first install (Company token, role_type=agency)
      C. Individual agent reconnects (location already in subscribers)
      D. Agency owner reconnects (location + agency_billing update)
      E. Location-scoped token install (individual agent always)
      F. All API calls fail, no locationId anywhere (companyId fallback)
    """
    code = request.args.get("code")
    raw_state = request.args.get("state")

    logger.info(
        f"=== OAUTH CALLBACK START === state={'present' if raw_state else 'MISSING'}, "
        f"code={'present' if code else 'MISSING'}"
    )

    try:
        log_webhook_event("oauth_global", "oauth_callback_hit", "info",
                          f"OAuth callback received: state={'yes' if raw_state else 'NO'}, "
                          f"code={'yes' if code else 'NO'}",
                          details={"has_state": bool(raw_state), "has_code": bool(code)})
    except Exception:
        pass

    if not code:
        logger.warning("OAuth callback: No authorization code in request params")
        try:
            log_webhook_event("oauth_global", "oauth_callback_error", "error",
                              "No authorization code in callback params")
        except Exception:
            pass
        flash("No authorization code received.", "danger")
        return redirect(url_for('public.home'))

    try:
        # ══════════════════════════════════════════════════════════════════════
        # Step 1: Validate state parameter (CSRF protection)
        # ══════════════════════════════════════════════════════════════════════
        flow_type = None

        if raw_state and ":" in raw_state:
            stored_state = session.pop("ghl_oauth_state", None)
            if not stored_state or not secrets.compare_digest(raw_state, stored_state):
                logger.warning(
                    f"OAuth CSRF validation failed: state mismatch "
                    f"(received={'present' if raw_state else 'NONE'}, "
                    f"stored={'present' if stored_state else 'NONE'})"
                )
                flash("OAuth session expired or invalid. Please try connecting again.", "danger")
                if raw_state and raw_state.startswith("ghl_sso:"):
                    return redirect(url_for('auth.login'))
                return redirect(url_for('dashboard.dashboard'))
            flow_type = raw_state.split(":", 1)[0]
            session.pop("ghl_pkce_verifier", None)
            logger.info(f"OAuth state validated (flow_type={flow_type})")
        elif raw_state in ("website_user", "private_app"):
            if not current_user.is_authenticated:
                logger.warning(f"OAuth callback: Legacy static state ({raw_state}) rejected — user not authenticated")
                flash("Session expired. Please log in and try connecting again.", "error")
                return redirect(url_for('auth.login'))
            flow_type = raw_state
            logger.info(f"OAuth callback: Legacy static state ({raw_state}) — authenticated user")
        else:
            logger.info("OAuth callback: Marketplace installation flow (no state)")

        is_website_user = flow_type in ("website_user", "private_app")
        is_ghl_sso = flow_type == "ghl_sso"

        if is_website_user:
            if not current_user.is_authenticated:
                flash("You must be logged in to connect Lead Connector.", "error")
                logger.warning("OAuth callback blocked - user not authenticated")
                return redirect(url_for('auth.login'))

            is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
            is_agency = (current_user.role or '').lower() == 'agency_owner'
            needs_subscription = not current_user.stripe_customer_id and not is_admin and not is_agency

            if needs_subscription:
                flash("Active subscription required to connect Lead Connector. Please subscribe first.", "error")
                logger.warning(f"OAuth callback blocked for {current_user.email} - no active subscription")
                if current_user.role == 'agency_owner':
                    return redirect(url_for('agency.agency_dashboard'))
                return redirect(url_for('dashboard.dashboard'))

            logger.info(f"OAuth callback: Website user flow for {current_user.email}")
        else:
            logger.info("OAuth callback: Marketplace installation flow")

        # ══════════════════════════════════════════════════════════════════════
        # Step 2: Token exchange (Company user_type first, Location fallback)
        # ══════════════════════════════════════════════════════════════════════
        is_private_app = False
        client_id = os.getenv("GHL_CLIENT_ID")
        client_secret = os.getenv("GHL_CLIENT_SECRET")
        cred_label = "GHL (marketplace)"

        domain = os.getenv("YOUR_DOMAIN")
        if not client_id or not client_secret or not domain:
            logger.error(
                f"OAuth env vars missing: "
                f"GHL_CLIENT_ID={'set' if client_id else 'MISSING'}, "
                f"GHL_CLIENT_SECRET={'set' if client_secret else 'MISSING'}, "
                f"YOUR_DOMAIN={'set' if domain else 'MISSING'}"
            )
            flash("OAuth is not configured. Please contact support.", "danger")
            return redirect(url_for('public.home'))

        token_url = "https://services.leadconnectorhq.com/oauth/token"
        token_data = None
        token_user_type_used = None

        base_payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{domain}/oauth/callback",
        }

        for user_type in ["Company", "Location"]:
            payload = {**base_payload, "user_type": user_type}
            logger.info(f"Token exchange attempt with user_type={user_type}")

            token_resp, token_err = _ghl_api_call(
                'POST', token_url, data=payload, timeout=15,
                label=f"Token exchange ({user_type})"
            )

            if token_resp is None:
                logger.warning(f"Token exchange ({user_type}) unreachable: {token_err}")
                continue

            if token_resp.ok:
                try:
                    token_data = token_resp.json()
                    token_user_type_used = user_type
                    logger.info(f"Token exchange SUCCESS with user_type={user_type}")
                    break
                except ValueError:
                    logger.error(
                        f"Token exchange ({user_type}) returned non-JSON: {token_resp.text[:500]}"
                    )
                    continue
            elif token_resp.status_code == 400:
                logger.warning(
                    f"Token exchange ({user_type}) got 400: {token_resp.text[:300]} — trying next"
                )
                continue
            else:
                logger.error(
                    f"Token exchange ({user_type}) rejected: "
                    f"{token_resp.status_code} {token_resp.text[:500]}"
                )
                continue

        if not token_data:
            logger.error("Token exchange failed for all user_types")
            try:
                log_webhook_event("oauth_global", "oauth_token_exchange_failed", "error",
                                  "Token exchange failed for all user_types",
                                  details={"flow_type": flow_type, "code_present": bool(code)})
            except Exception:
                pass
            flash("Failed to connect to Lead Connector. Please try again.", "danger")
            return redirect(url_for('public.home'))

        # ══════════════════════════════════════════════════════════════════════
        # Step 3: Extract core fields
        # ══════════════════════════════════════════════════════════════════════
        access_token = token_data.get('access_token')
        if not access_token:
            logger.error("Token exchange returned no access_token")
            try:
                log_webhook_event("oauth_global", "oauth_no_access_token", "error",
                                  "Token exchange missing access_token")
            except Exception:
                pass
            flash("Authorization failed — no access token received. Please try again.", "danger")
            return redirect(url_for('public.home'))

        token_location_id = token_data.get('locationId')
        company_id = token_data.get('companyId')
        refresh_token_raw = token_data.get('refresh_token')
        expires_in = int(token_data.get('expires_in') or 86400)

        # ══════════════════════════════════════════════════════════════════════
        # Step 4: Validate critical scopes
        # ══════════════════════════════════════════════════════════════════════
        granted_scope_str = token_data.get('scope', '')
        if granted_scope_str:
            granted_scopes = set(granted_scope_str.split())
            critical_scopes = {
                "contacts.readonly", "conversations/message.write",
                "conversations/message.readonly", "oauth.readonly",
            }
            missing_critical = critical_scopes - granted_scopes
            if missing_critical:
                logger.error(f"Critical scopes MISSING from grant: {missing_critical}")
                try:
                    log_webhook_event(
                        token_location_id or "unknown",
                        "oauth_scope_mismatch", "error",
                        f"Critical scopes missing: {missing_critical}",
                        details={"granted": list(granted_scopes), "missing": list(missing_critical)})
                except Exception:
                    pass
                flash("Some required permissions were not granted. Please reconnect and accept all permissions.", "danger")
                return redirect(url_for('dashboard.dashboard'))

            missing_optional = set(GHL_OAUTH_SCOPES) - granted_scopes
            if missing_optional:
                logger.warning(f"Optional scopes not granted: {missing_optional}")

        # Encrypt tokens before any DB storage
        enc_access_token = encrypt_token(access_token)
        enc_refresh_token = encrypt_token(refresh_token_raw) if refresh_token_raw else None

        logger.info(
            f"Step 3-4 complete: Token OK via user_type={token_user_type_used}. "
            f"locationId={token_location_id}, companyId={company_id}, expires_in={expires_in}"
        )

        headers_ghl = {'Authorization': f'Bearer {access_token}', 'Version': '2021-07-28'}

        # ══════════════════════════════════════════════════════════════════════
        # Step 5: Get user info via GET /users/{userId}
        # ══════════════════════════════════════════════════════════════════════
        me_data = {}
        user_email_from_api = None
        user_name = None

        ghl_user_id = token_data.get('userId')
        if ghl_user_id:
            me_data['id'] = ghl_user_id
            me_resp, me_err = _ghl_api_call(
                'GET', f"https://services.leadconnectorhq.com/users/{ghl_user_id}",
                headers=headers_ghl, timeout=10, label=f"/users/{ghl_user_id}"
            )

            if me_resp and me_resp.ok:
                try:
                    me_data = me_resp.json()
                    user_email_from_api = me_data.get('email')
                    user_name = me_data.get('name') or me_data.get('firstName')
                except ValueError:
                    logger.warning(f"/users/{ghl_user_id} returned non-JSON: {me_resp.text[:300]}")
            elif me_resp and me_resp.status_code in (401, 403):
                logger.info(
                    f"/users/{ghl_user_id} returned {me_resp.status_code} — "
                    f"expected with Location-scoped tokens. Using fallback chain."
                )
            elif me_resp:
                logger.warning(f"/users/{ghl_user_id} failed: {me_resp.status_code} {me_resp.text[:300]}")
            else:
                logger.warning(f"/users/{ghl_user_id} unreachable: {me_err}")
        else:
            logger.info("No userId in token_data — skipping /users/ call")

        # Extract role info
        user_roles = me_data.get('roles', {}) if isinstance(me_data.get('roles'), dict) else {}
        user_role_type = user_roles.get('type', '')
        user_location_ids = user_roles.get('locationIds', []) or []

        # CRM email (stored separately, never overwrites login email)
        crm_email_resolved = user_email_from_api
        if not crm_email_resolved:
            crm_email_resolved = token_data.get('userEmail') or token_data.get('email')

        logger.info(
            f"Step 5 complete: User info retrieved. "
            f"role_type={user_role_type}, locationIds={user_location_ids}"
        )

        # ══════════════════════════════════════════════════════════════════════
        # Step 6: Location discovery — scoped by token type
        # ══════════════════════════════════════════════════════════════════════
        #
        # Company token (agency owner):
        #   oauth.readonly  → GET /oauth/installedLocations — list all locations
        #   oauth.write     → POST /oauth/locationToken — per-location credentials
        #   users.readonly  → GET /users/?locationId={id} — pre-populate real emails
        #
        # Location token (individual agent):
        #   Source: token_location_id from the token exchange response (authoritative)
        #   locations.readonly → GET /locations/{id} — enrich with name/timezone
        #   On failure: use token data directly, silently — no alert, no URL param fallback
        # ──────────────────────────────────────────────────────────────────────
        sub_accounts = []
        using_location_fallback = False

        if token_user_type_used == 'Company' and company_id:
            logger.info(f"Company token: discovering installed locations for companyId={company_id}...")

            # oauth.readonly: get all installed locations
            installed_locs = _fetch_installed_locations(headers_ghl, company_id, client_id)
            if installed_locs:
                # oauth.write: generate per-location tokens
                sub_accounts_raw = _generate_location_tokens(headers_ghl, company_id, installed_locs)
                for entry in sub_accounts_raw:
                    loc_tok = entry.get('_loc_token')
                    # users.readonly: get real user emails for this location
                    loc_users = _fetch_location_users(entry['id'], loc_tok) if loc_tok else []
                    entry['_loc_users'] = loc_users
                    # Identify primary agent email: account-role users only (not agency-level)
                    account_users = [u for u in loc_users if u.get('role') == 'account']
                    candidates = account_users if account_users else loc_users
                    if len(candidates) == 1:
                        entry['_primary_email'] = candidates[0]['email']
                        entry['_primary_name'] = candidates[0]['name']
                    # Multiple users: can't determine primary — use placeholder at provisioning
                sub_accounts = sub_accounts_raw

            if not sub_accounts and installed_locs:
                # locationToken failed — use locations without per-location tokens
                for loc in installed_locs:
                    loc_id = loc.get('_id') or loc.get('id') or loc.get('locationId')
                    if loc_id:
                        sub_accounts.append({
                            'id': loc_id,
                            'name': loc.get('name', 'Unknown Location'),
                            'timezone': loc.get('timezone'),
                            '_loc_users': [],
                        })

            if not sub_accounts:
                # installedLocations returned nothing — genuine discovery failure.
                # Do not fall back to undocumented heuristics.
                # using_location_fallback will be set below and the calm info banner fires.
                logger.warning(
                    f"Company token: installedLocations returned 0 locations for "
                    f"companyId={company_id}. Marking as location fallback."
                )

        else:
            # Location token (individual agent).
            # Source: token_location_id from token exchange — this is authoritative.
            # Use locations.readonly to enrich with name/timezone.
            # If the enrichment call fails, fall back to token data silently — no banner.
            loc_id = token_location_id  # authoritative — from token exchange response
            if loc_id:
                loc_name = user_name or 'Primary Location'
                loc_timezone = None
                loc_resp, _ = _ghl_api_call(
                    'GET', f"https://services.leadconnectorhq.com/locations/{loc_id}",
                    headers=headers_ghl, timeout=10, label=f"/locations/{loc_id}"
                )
                if loc_resp and loc_resp.ok:
                    try:
                        loc_data = loc_resp.json().get('location', loc_resp.json())
                        loc_name = loc_data.get('name', loc_name)
                        loc_timezone = loc_data.get('timezone')
                    except (ValueError, KeyError):
                        pass  # Use token defaults
                sub_accounts = [{'id': loc_id, 'name': loc_name, 'timezone': loc_timezone}]
                logger.info(f"Location token: loc_id={loc_id}, name={loc_name!r}")
            else:
                logger.warning("Location token: no locationId in token exchange response")

        logger.info(f"Step 6 complete: {len(sub_accounts)} locations, token_type={token_user_type_used}")

        # ══════════════════════════════════════════════════════════════════════
        # Step 7: Determine primary_location_id
        # ══════════════════════════════════════════════════════════════════════
        # For Company token reconnects (logged-in user reauthorizing), use
        # their existing location_id as token_location_id if the token
        # exchange didn't provide one. Skip temp_ placeholders from
        # Stripe-first provisioning.
        _effective_token_loc = token_location_id
        if not _effective_token_loc and token_user_type_used == 'Company' and current_user.is_authenticated:
            _session_loc = getattr(current_user, 'location_id', None)
            if _session_loc and not (
                str(_session_loc).startswith('temp_') or
                str(_session_loc).startswith('install_')
            ):
                _effective_token_loc = _session_loc
                logger.info(
                    f"Step 7: Company token reconnect — using session "
                    f"location_id={_session_loc} as effective token_location_id"
                )

        # Use a temporary DB connection for the location check
        _loc_conn = get_db_connection()
        try:
            primary_location_id, resolution_method = _determine_primary_location(
                _effective_token_loc, user_location_ids,
                sub_accounts, company_id, _loc_conn
            )
        finally:
            if _loc_conn:
                return_db_connection(_loc_conn)

        logger.info(
            f"Step 7 complete: primary_location_id={primary_location_id} "
            f"(method={resolution_method})"
        )

        # Synthesize sub_account entry if we have a location but no sub_accounts.
        # For location-scoped tokens, empty sub_accounts is NORMAL — we got the
        # location from the token itself, not from the locations discovery API.
        # Only flag as a real fallback for company-token flows where we expected
        # to discover locations but couldn't.
        if not sub_accounts and primary_location_id:
            _company_token_flow = (token_user_type_used == 'Company')
            using_location_fallback = _company_token_flow
            sub_accounts = [{'id': primary_location_id,
                             'name': user_name or 'Primary Location',
                             'timezone': None}]
            if _company_token_flow:
                logger.warning(
                    f"No locations discovered (company token) but have "
                    f"locationId={primary_location_id}. Using token-based fallback."
                )
            else:
                logger.info(
                    f"Location-scoped token: synthesizing sub_accounts from "
                    f"locationId={primary_location_id}. Normal operation — user is connected."
                )

        # Hard fail if no location ID could be determined through documented GHL APIs.
        # companyId is NOT a locationId — using it as one corrupts subscriber data.
        # Enterprise behavior: surface a clear error and ask the user to reconnect.
        if not primary_location_id:
            logger.error(
                f"OAuth hard fail: no locationId determined. "
                f"token_type={token_user_type_used}, companyId={company_id}, "
                f"sub_accounts={len(sub_accounts)}, user_location_ids={user_location_ids}"
            )
            try:
                save_persistent_alert(
                    email=ADMIN_EMAILS[0] if ADMIN_EMAILS else "admin",
                    alert_type="oauth_no_location",
                    title="OAuth: No Location ID Returned",
                    message=(
                        f"GHL OAuth completed but returned no locationId. "
                        f"token_type={token_user_type_used}, companyId={company_id}. "
                        f"User must reconnect."
                    ),
                    severity="warning",
                    location_id=company_id or "unknown",
                )
            except Exception:
                pass
            flash(
                "Connection incomplete — Lead Connector did not return a location ID. "
                "Please try connecting again from the Connect tab.",
                "danger"
            )
            return redirect(url_for('dashboard.dashboard'))

        # ══════════════════════════════════════════════════════════════════════
        # Step 8: Detect agency owner
        # ══════════════════════════════════════════════════════════════════════
        is_agency_owner = _detect_agency_owner(
            user_role_type, company_id, user_location_ids,
            token_user_type_used, crm_email_resolved or ''
        )

        company_metadata = {}
        if company_id:
            company_metadata = _build_company_metadata(me_data, company_id)
            logger.info(f"Company metadata: {company_metadata}")

        use_agency_flow = is_agency_owner

        logger.info(
            f"Step 8 complete: is_agency_owner={is_agency_owner}, companyId={company_id}, "
            f"role_type={user_role_type}"
        )

        # ══════════════════════════════════════════════════════════════════════
        # Step 9: Email recovery chain
        # ══════════════════════════════════════════════════════════════════════
        # Priority 1: logged-in user (canonical login email)
        if current_user.is_authenticated:
            user_email = current_user.email
            user_name = current_user.full_name or user_name
            email_source = 'logged_in_user'
            logger.info(f"Using logged-in user's email: {user_email} (CRM email: {crm_email_resolved})")
        else:
            # Use /users/ API email first
            if user_email_from_api:
                user_email = user_email_from_api
                email_source = 'users_api'
                logger.info(f"Using email from /users/ API: {user_email}")

                # Stripe-first cross-reference: user may have subscribed with a
                # different email (e.g. hotmail) than their GHL account (gmail).
                # If we're about to create a gmail row but there's already a
                # Stripe-provisioned temp_ row for this location's company, use
                # the Stripe email as the canonical identity so they don't end up
                # with two orphaned rows.
                try:
                    _xref_conn = get_db_connection()
                    if _xref_conn:
                        _xref_cur = _xref_conn.cursor()
                        # Check if GHL userId already maps to a different IGB email
                        ghl_user_id_xref = token_data.get('userId') or me_data.get('id')
                        if ghl_user_id_xref:
                            _xref_cur.execute(
                                "SELECT email FROM subscribers "
                                "WHERE crm_user_id = %s AND email != %s LIMIT 1",
                                (ghl_user_id_xref, user_email_from_api)
                            )
                            _xref_row = _xref_cur.fetchone()
                            if _xref_row:
                                logger.info(
                                    f"Email mismatch: GHL userId {ghl_user_id_xref} already "
                                    f"maps to IGB email {_xref_row['email']!r}. "
                                    f"Using existing IGB email instead of GHL API email "
                                    f"{user_email_from_api!r}."
                                )
                                user_email = _xref_row['email']
                                email_source = 'crm_user_id_xref'
                        # If no crm_user_id match, check for a Stripe-provisioned temp_ row
                        # that hasn't been GHL-connected yet.
                        # NOTE: Stripe rows have company_id=NULL (we don't know the GHL
                        # company at Stripe time), so we cannot filter by company_id.
                        # Instead, filter by the specific location_ids GHL just reported
                        # for this install — if a temp_ row exists for one of those
                        # locations' emails it must be this user.
                        # Fallback: if no location ids known yet, look for ANY temp_+Stripe
                        # row with the same normalized email prefix (catches hotmail/gmail
                        # same-person different-domain — only safe when EXACTLY ONE match).
                        if email_source == 'users_api':
                            _known_loc_ids = (
                                [s['id'] for s in sub_accounts if s.get('id')]
                                + (user_location_ids or [])
                            )
                            _known_loc_ids = list(set(_known_loc_ids))
                            _stripe_row = None
                            if _known_loc_ids:
                                # Exact: Stripe row already has this location_id stamped
                                # (shouldn't happen, but handles partial-provision retries)
                                _xref_cur.execute(
                                    "SELECT email FROM subscribers "
                                    "WHERE location_id = ANY(%s) "
                                    "  AND stripe_customer_id IS NOT NULL "
                                    "  AND email != %s LIMIT 1",
                                    (_known_loc_ids, user_email_from_api)
                                )
                                _stripe_row = _xref_cur.fetchone()
                            if not _stripe_row:
                                # Broad: one temp_ Stripe row exists — this user subscribed
                                # first, then installed from GHL marketplace. Only safe when
                                # exactly one such row exists (prevents false positives in
                                # multi-tenant scenarios).
                                _xref_cur.execute(
                                    "SELECT email FROM subscribers "
                                    "WHERE location_id LIKE 'temp_%%' "
                                    "  AND stripe_customer_id IS NOT NULL "
                                    "  AND email != %s",
                                    (user_email_from_api,)
                                )
                                _candidates = _xref_cur.fetchall()
                                if len(_candidates) == 1:
                                    _stripe_row = _candidates[0]
                            if _stripe_row:
                                logger.info(
                                    f"Found Stripe-first row: "
                                    f"{_stripe_row['email']!r} — using as canonical email "
                                    f"instead of GHL API email {user_email_from_api!r}."
                                )
                                user_email = _stripe_row['email']
                                email_source = 'stripe_xref'
                        _xref_cur.close()
                except Exception as _xref_err:
                    logger.warning(f"Email cross-reference check failed (non-fatal): {_xref_err}")
                finally:
                    if _xref_conn:
                        return_db_connection(_xref_conn)
            else:
                # Full fallback chain for marketplace installs
                user_email, resolved_name, email_source = _resolve_user_email(
                    token_data, primary_location_id, company_id
                )
                if resolved_name:
                    user_name = resolved_name
                logger.info(f"Email resolved via {email_source}: {user_email}")

        if email_source == 'placeholder':
            _send_ghost_install_alert(
                user_email,
                token_data.get('userId') or 'unknown',
                primary_location_id, company_id
            )

        # For agency detection, re-check with resolved email if we used crm_email before
        if is_agency_owner and user_email:
            is_agency_owner = _detect_agency_owner(
                user_role_type, company_id, user_location_ids,
                token_user_type_used, user_email
            )
            use_agency_flow = is_agency_owner

        logger.info(f"Step 9 complete: user_email={user_email}, source={email_source}")

        # ══════════════════════════════════════════════════════════════════════
        # Step 10: Database operations (single connection, transaction)
        # ══════════════════════════════════════════════════════════════════════
        conn = get_db_connection_with_retry(max_attempts=3)
        if not conn:
            logger.error("OAuth callback: DB connection failed after 3 retries")
            flash("Database temporarily unavailable. Please try connecting again in a few minutes.", "danger")
            return redirect(url_for('public.home'))

        is_new_install = False
        app_type = 'private' if is_private_app else ('website' if is_website_user else 'marketplace')

        try:
            cur = conn.cursor()

            # 10a: Agency owner: upsert subscribers + agency_billing
            if use_agency_flow:
                agency_location_id = primary_location_id or company_id

                _upsert_agency_owner(
                    cur, user_email, agency_location_id,
                    user_name or 'Agency Owner',
                    enc_access_token, enc_refresh_token, expires_in,
                    (next((s.get('timezone') for s in sub_accounts if s['id'] == agency_location_id), None)),
                    me_data, crm_email_resolved,
                    app_type, company_id, company_metadata,
                    token_user_type_used
                )
                logger.info(f"Step 10a: Agency owner upserted: {user_email}")

            # 10b: Check for existing location row (reconnect scenario)
            # Skip for agency owners — Step 10a already handled their
            # primary location via INSERT...ON CONFLICT(email). Running
            # both would double-write the same tokens to the same row.
            existing_row = None
            if primary_location_id and not use_agency_flow:
                sync_role = None
                corrected_email, existing_row = _update_existing_location(
                    cur, primary_location_id, crm_email_resolved,
                    enc_access_token, enc_refresh_token, expires_in,
                    me_data, app_type, sync_role, user_email,
                    use_agency_flow, company_id, token_user_type_used
                )
                # Only apply email correction for unauthenticated flows (marketplace
                # installs). For logged-in reauthorize flows, current_user.email is the
                # canonical IGB identity — never override it with a GHL-sourced email
                # from an existing row (which could be a split-row scenario where the
                # GHL location maps to a different email than this user's IGB account).
                if corrected_email and not current_user.is_authenticated:
                    user_email = corrected_email
                elif corrected_email and corrected_email.lower() == user_email.lower():
                    pass  # Same email, no change needed

            # Also check if email owns a different location (update tokens there too)
            if not existing_row and primary_location_id and not use_agency_flow:
                cur.execute(
                    "SELECT location_id FROM subscribers WHERE email = %s",
                    (user_email,)
                )
                existing_by_email = cur.fetchone()
                if existing_by_email:
                    _ct = enc_access_token if token_user_type_used == 'Company' else None
                    _cr = enc_refresh_token if token_user_type_used == 'Company' else None
                    cur.execute("""
                        UPDATE subscribers
                        SET crm_email = %s,
                            access_token = %s,
                            refresh_token = %s,
                            token_expires_at = NOW() + interval '%s seconds',
                            crm_user_id = COALESCE(%s, crm_user_id),
                            oauth_app_type = %s,
                            company_access_token = COALESCE(%s, company_access_token),
                            company_refresh_token = COALESCE(%s, company_refresh_token),
                            company_token_expires_at = CASE WHEN %s IS NOT NULL THEN NOW() + interval '%s seconds' ELSE company_token_expires_at END,
                            updated_at = NOW()
                        WHERE email = %s
                    """, (
                        crm_email_resolved, enc_access_token, enc_refresh_token,
                        expires_in, me_data.get('id'), app_type,
                        _ct, _cr, _ct, expires_in,
                        user_email
                    ))
                    logger.info(
                        f"Updated tokens for {user_email}'s existing location {existing_by_email['location_id']}."
                    )

            # 10c: Provision subscriber rows
            # CRITICAL FIX (BUG 0): Agency owners provision ALL installed
            # locations, not just their primary. This pre-populates agent
            # rows with tokens + parent_agency_email so agents don't need
            # to individually install from the marketplace.
            # Individual agents get EXACTLY ONE location (their primary).
            if use_agency_flow:
                # Agency: provision ALL sub_accounts EXCEPT the owner's
                # primary (already handled by _upsert_agency_owner in 10a).
                # Existing agents get token refresh + parent_agency_email;
                # new agent locations get pre-populated rows.
                locations_to_provision = [s for s in sub_accounts
                                          if s['id'] != primary_location_id]
            else:
                # Individual: only primary location
                locations_to_provision = [s for s in sub_accounts if s['id'] == primary_location_id]
                if existing_row and primary_location_id:
                    locations_to_provision = [s for s in locations_to_provision
                                              if s['id'] != primary_location_id]

            # Auto-link to agency if individual with matching companyId
            auto_linked_agency_email = None
            if not use_agency_flow and company_id:
                agency_row = get_agency_by_company_id(company_id)
                if agency_row:
                    auto_linked_agency_email = agency_row.get('agency_email')
                    logger.info(
                        f"Auto-linking {user_email} to agency "
                        f"{auto_linked_agency_email} via companyId={company_id}"
                    )

            # Individual: enforce single location
            if not use_agency_flow:
                if len(locations_to_provision) > 1:
                    if primary_location_id:
                        match = [s for s in locations_to_provision if s['id'] == primary_location_id]
                        locations_to_provision = match if match else locations_to_provision[:1]
                    else:
                        locations_to_provision = locations_to_provision[:1]
                elif len(locations_to_provision) == 0 and primary_location_id and not existing_row:
                    locations_to_provision = [{'id': primary_location_id, 'name': user_name or 'Primary', 'timezone': None}]

            logger.info(
                f"Step 10c: Provisioning {len(locations_to_provision)} subscriber rows "
                f"(agency_flow={use_agency_flow}, total_sub_accounts={len(sub_accounts)})"
            )

            for sub in locations_to_provision:
                skipped = _provision_new_subscriber(
                    cur, sub, user_email, crm_email_resolved,
                    enc_access_token, enc_refresh_token, expires_in,
                    me_data, app_type, use_agency_flow,
                    auto_linked_agency_email, company_id,
                    primary_location_id, token_user_type_used
                )
                if not skipped:
                    is_new_install = True

            # Refresh tokens for agent locations NOT in locations_to_provision
            # (already-provisioned agents whose location was in sub_accounts
            # but already handled by _provision_new_subscriber's "owned by
            # another user" path — this catches any stragglers)
            if use_agency_flow and sub_accounts:
                provisioned_ids = {s['id'] for s in locations_to_provision}
                for sub in sub_accounts:
                    if sub['id'] not in provisioned_ids and sub['id'] != primary_location_id and sub.get('_loc_token'):
                        cur.execute(
                            "SELECT email FROM subscribers WHERE location_id = %s",
                            (sub['id'],)
                        )
                        agent_row = cur.fetchone()
                        if agent_row:
                            _loc_tok = sub['_loc_token']
                            _loc_ref = sub.get('_loc_refresh')
                            _loc_exp = sub.get('_loc_expires') or expires_in
                            cur.execute("""
                                UPDATE subscribers
                                SET parent_agency_email = COALESCE(%s, parent_agency_email),
                                    company_id = COALESCE(%s, company_id),
                                    access_token = %s,
                                    refresh_token = %s,
                                    token_expires_at = NOW() + interval '%s seconds',
                                    updated_at = NOW()
                                WHERE location_id = %s
                            """, (
                                user_email, company_id,
                                encrypt_token(_loc_tok),
                                encrypt_token(_loc_ref) if _loc_ref else None,
                                _loc_exp, sub['id'],
                            ))
                            logger.info(
                                f"Refreshed tokens for agent {agent_row['email']} "
                                f"at location {sub['id']} via agency install"
                            )

            conn.commit()
            logger.info(
                f"Step 10 complete: Onboarded {user_email} "
                f"(agency_flow={use_agency_flow}, new_install={is_new_install})"
            )

        except Exception as e:
            conn.rollback()
            logger.error(f"Database onboarding error for {user_email}: {e}", exc_info=True)
            flash("Error completing setup. Please contact support.", "danger")
            return redirect(url_for('public.home'))
        finally:
            cur.close()
            return_db_connection(conn)

        # ══════════════════════════════════════════════════════════════════════
        # Step 11: Post-onboarding (logging, alerts, install timestamp)
        # ══════════════════════════════════════════════════════════════════════
        try:
            log_webhook_event(
                location_id=primary_location_id,
                event_type="oauth_onboarding",
                status="success",
                summary=f"OAuth onboarding complete for {user_email}",
                details={
                    "email": user_email,
                    "agency_flow": use_agency_flow,
                    "new_install": is_new_install,
                    "resolution_method": resolution_method,
                    "locations_provisioned": len(locations_to_provision),
                    "total_ghl_locations": len(sub_accounts),
                    "fallback_mode": using_location_fallback,
                    "is_website_user": is_website_user,
                }
            )
        except Exception as e:
            logger.warning(f"Failed to log onboarding event: {e}")

        # Stamp install_completed_at
        _conn = None
        try:
            _conn = get_db_connection_with_retry(2)
            if _conn:
                _cur = _conn.cursor()
                _cur.execute(
                    "UPDATE subscribers SET install_completed_at = NOW() "
                    "WHERE LOWER(email) = LOWER(%s) AND install_completed_at IS NULL",
                    (user_email,)
                )
                _conn.commit()
                _cur.close()
        except Exception as e:
            logger.warning(f"Failed to set install_completed_at: {e}")
        finally:
            if _conn:
                return_db_connection(_conn)

        # Mark marketplace install as OAuth-complete
        try:
            if primary_location_id:
                mark_install_oauth_complete(location_id=primary_location_id)
            if company_id:
                mark_install_oauth_complete(company_id=company_id)
        except Exception as e:
            logger.debug(f"mark_install_oauth_complete note: {e}")

        # Persistent alert only for true company-token discovery failures
        # (location-scoped tokens never trigger this — their fallback is normal operation)
        if using_location_fallback:
            try:
                if use_agency_flow:
                    alert_msg = (
                        "Your account is connected and operational. However, some of your "
                        "sub-account locations couldn't be loaded automatically — this is "
                        "usually a temporary GHL API delay. Try reconnecting via the Connect "
                        "tab if any locations are missing."
                    )
                    alert_severity = "info"
                else:
                    alert_msg = (
                        "Your account connected successfully and the bot is active. "
                        "One detail couldn't be loaded automatically — reconnect via the "
                        "Connect tab if anything looks off."
                    )
                    alert_severity = "info"

                save_persistent_alert(
                    email=user_email,
                    alert_type="scope_locations_readonly",
                    title="Account Connected",
                    message=alert_msg,
                    severity=alert_severity,
                    location_id=primary_location_id,
                )
                log_webhook_event(
                    location_id=primary_location_id,
                    event_type="scope_issue",
                    status="warning",
                    summary="locations API returned 0 results — using fallback",
                    details={"fallback": True, "agency_flow": use_agency_flow},
                )
            except Exception as e:
                logger.warning(f"Failed to save scope alert: {e}")

        # ══════════════════════════════════════════════════════════════════════
        # Step 12: Welcome email (ONLY for new installs, not reconnects)
        # ══════════════════════════════════════════════════════════════════════
        # Welcome email: only for NEW installs, never reconnects.
        # is_new_install is True when _provision_new_subscriber created a row.
        # For agency flow, existing_row is always None (we skip 10b), so also
        # check resolution_method to detect reconnects.
        _is_reconnect = resolution_method in ('reconnect_first', 'reconnect_roles')
        _send_welcome = (is_new_install or (not existing_row and not _is_reconnect)) and not _is_reconnect
        if _send_welcome:
            try:
                domain_url = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
                dashboard_link = (
                    f"{domain_url}/agency-dashboard" if use_agency_flow
                    else f"{domain_url}/dashboard"
                )
                if use_agency_flow:
                    welcome_html = _build_agency_owner_welcome_email(user_name, dashboard_link, domain_url, recipient_email=user_email)
                else:
                    welcome_html = _build_welcome_email(user_name, dashboard_link, domain_url, recipient_email=user_email)
                email_subject = (
                    "Your Agency Dashboard is Ready — InsuranceGrokBot" if use_agency_flow
                    else "Welcome to InsuranceGrokBot — Your AI Assistant is Ready"
                )
                email_sent = send_email_via_api(
                    to_email=user_email,
                    subject=email_subject,
                    html_body=welcome_html,
                    text_body=(
                        f"Welcome to InsuranceGrokBot, {user_name}! "
                        f"Dashboard: {dashboard_link} | "
                        f"Support: {domain_url}/support"
                    )
                )
                if email_sent:
                    logger.info(f"Welcome email sent to {user_email}")
                else:
                    logger.warning(f"Welcome email failed for {user_email}")
            except Exception as e:
                logger.warning(f"Welcome email error for {user_email}: {e}")
        else:
            logger.info(f"Skipping welcome email for {user_email} — reconnect (method={resolution_method})")

        # ══════════════════════════════════════════════════════════════════════
        # Step 13: Login and redirect
        # ══════════════════════════════════════════════════════════════════════
        session.clear()

        user = User.get(user_email)
        if user:
            login_user(user)
            logger.info(f"Step 13 complete: Logged in {user_email}")
        else:
            logger.error(
                f"User.get({user_email}) returned None after successful DB commit — login failed"
            )
            if is_website_user:
                flash("Account created but login failed. Please log in manually.", "warning")
                return redirect(url_for('auth.login'))

        # Marketplace install redirect
        if not is_website_user and not is_ghl_sso:
            logger.info(f"=== MARKETPLACE INSTALL COMPLETE for {user_email} ===")
            try:
                log_webhook_event(
                    primary_location_id or "unknown", "oauth_complete", "success",
                    f"Marketplace install complete for {user_email} "
                    f"(user_type_used={token_user_type_used})"
                )
            except Exception:
                pass

            if user:
                if not getattr(user, 'password_hash', None):
                    flash("App installed! Set a password so you can log back in anytime.", "success")
                    return redirect(f"/set-password?type={'agency' if use_agency_flow else 'individual'}")
                flash("App installed successfully! Complete your dashboard setup to activate your bot.", "success")
                if use_agency_flow:
                    return redirect(url_for('agency.agency_dashboard'))
                return redirect(url_for('dashboard.dashboard'))
            else:
                flash("App installed! Please log in or create a password to access your dashboard.", "success")
                return redirect(url_for('auth.login'))

        # GHL SSO: auto-login after successful token exchange
        if is_ghl_sso:
            sso_user = User.get(user_email) if user_email else None

            if not sso_user:
                sso_conn = get_db_connection()
                if sso_conn:
                    try:
                        sso_cur = sso_conn.cursor()
                        if primary_location_id:
                            sso_cur.execute("SELECT email FROM subscribers WHERE location_id = %s", (primary_location_id,))
                            sso_row = sso_cur.fetchone()
                            if sso_row:
                                sso_user = User.get(sso_row['email'])
                        if not sso_user and company_id:
                            sso_cur.execute("SELECT agency_email FROM agency_billing WHERE company_id = %s LIMIT 1", (company_id,))
                            sso_row = sso_cur.fetchone()
                            if sso_row:
                                sso_user = User.get(sso_row['agency_email'])
                                logger.info(f"GHL SSO: Found agency owner by company_id={company_id}")
                        sso_cur.close()
                    finally:
                        return_db_connection(sso_conn)

            if sso_user:
                # Save Company token if this was a Company-scoped authorization
                if token_user_type_used == "Company" and company_id:
                    try:
                        ct_conn = get_db_connection()
                        if ct_conn:
                            try:
                                ct_cur = ct_conn.cursor()
                                ct_cur.execute("""
                                    UPDATE subscribers
                                    SET company_access_token = %s,
                                        company_refresh_token = %s,
                                        company_token_expires_at = NOW() + INTERVAL '%s seconds'
                                    WHERE LOWER(email) = LOWER(%s)
                                """, (enc_access_token, enc_refresh_token, expires_in, sso_user.email))
                                ct_conn.commit()
                                ct_cur.close()
                                logger.info(f"GHL SSO: Company token saved for {sso_user.email}")
                            finally:
                                return_db_connection(ct_conn)
                    except Exception as ct_err:
                        logger.warning(f"GHL SSO: Failed to save company token: {ct_err}")

                # Also save the regular Location token if we have a location
                if primary_location_id and access_token:
                    try:
                        update_subscriber_token(
                            primary_location_id, enc_access_token,
                            enc_refresh_token, expires_in
                        )
                    except Exception as tok_err:
                        logger.warning(f"GHL SSO: Failed to save location token: {tok_err}")

                login_user(sso_user, remember=True)
                logger.info(f"=== GHL SSO LOGIN: {sso_user.email} (location={primary_location_id}) ===")
                flash("Signed in with GoHighLevel!", "success")
                role = (sso_user.role or 'individual').lower()
                is_admin = sso_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
                if not is_admin and role == 'agency_owner':
                    return redirect(url_for('agency.agency_dashboard'))
                return redirect(url_for('dashboard.dashboard'))
            else:
                logger.warning(f"GHL SSO: No account found for email={user_email}, location={primary_location_id}")
                flash("No InsuranceGrokBot account found for this GoHighLevel location. Please register first or install from the GHL Marketplace.", "error")
                return redirect(url_for('auth.login'))

        # Website user flow redirect
        app_type_label = 'private' if is_private_app else 'website'
        logger.info(f"=== {app_type_label.upper()} APP OAUTH COMPLETE for {user_email} ===")
        try:
            log_webhook_event(
                primary_location_id or "unknown", "oauth_complete", "success",
                f"{app_type_label.capitalize()} app OAuth complete for {user_email}"
            )
        except Exception:
            pass
        if is_private_app:
            return redirect(url_for('oauth.oauth_loading'))
        flash("CRM connected successfully!", "success")
        if use_agency_flow:
            return redirect(url_for('agency.agency_dashboard'))
        return redirect(url_for('dashboard.dashboard'))

    except requests.RequestException as e:
        logger.error(f"OAuth network error: {e}", exc_info=True)
        try:
            log_webhook_event("oauth_global", "oauth_network_error", "error",
                              f"OAuth network error: {e}")
        except Exception:
            pass
        flash("Failed to connect to Lead Connector. Please try again.", "danger")
        return redirect(url_for('public.home'))
    except Exception as e:
        logger.error(f"Critical OAuth failure: {e}", exc_info=True)
        try:
            log_webhook_event("oauth_global", "oauth_critical_error", "error",
                              f"Critical OAuth failure: {e}")
        except Exception:
            pass
        flash("An unexpected error occurred. Please try again or contact support.", "danger")
        return redirect(url_for('public.home'))


@oauth_bp.route("/oauth/loading")
@login_required
def oauth_loading():
    """Loading screen shown after OAuth to visualize data gathering progress."""
    return render_template('oauth-loading.html')
