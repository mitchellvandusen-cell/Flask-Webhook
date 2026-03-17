import json
import os
import logging
import threading
import time
import asyncio
from datetime import datetime, timezone as dt_timezone
import httpx
import requests as http_requests
from flask import Blueprint, request, Response, jsonify
from flask_login import login_required, current_user
from ghl_auth import jwt_or_session_required

import twilio_provisioning
from db import get_db_connection, return_db_connection
from ghl_api import get_valid_token
from number_health import select_outbound_number
from voice.audio import XAI_API_KEY, VOICE_OPTIONS, DEFAULT_VOICE, _generate_voice_preview, _pcm16_to_wav
from voice.call_state import active_calls, transfer_requests, _twilio_hangup
from voice.helpers import _get_subscriber_by_location, _get_current_subscriber_voice
from voice.call_history_helpers import save_call_to_history, update_call_history_status
from blueprints.team import require_permission
from voice.predictive_engine import (
    calculate_optimal_dial_ratio, tcpa_tracker, agent_state_manager,
    callback_queue, check_recipient_timezone, is_two_party_consent_state,
    area_code_to_state, area_code_to_timezone, get_compliance_metrics,
    AgentState,
)
from extensions import ADMIN_EMAILS

logger = logging.getLogger("voice_bridge.dialer")

dialer_bp = Blueprint('voice_dialer', __name__)


# ═══ Multi-Line Dialer Enforcement Helpers ═══════════════════════════════════

def _check_calling_hours(voice_config, agent_tz_str=None):
    """Check if current time is within configured calling hours.
    Only enforced when quiet_hours_enabled is True (opt-in, default OFF).
    Uses agent's timezone since we don't reliably have contact timezone.
    Returns (allowed, reason) tuple."""
    # Quiet hours are opt-in — skip enforcement unless explicitly enabled
    if not voice_config.get('quiet_hours_enabled', False):
        return True, None
    start_str = voice_config.get('calling_hours_start', '08:00')
    end_str = voice_config.get('calling_hours_end', '21:00')
    if not start_str or not end_str:
        return True, None

    try:
        import pytz
    except ImportError:
        return True, None  # pytz not available, skip check

    tz_str = agent_tz_str or voice_config.get('timezone') or 'America/Chicago'
    try:
        tz = pytz.timezone(tz_str)
    except pytz.UnknownTimeZoneError:
        tz = pytz.timezone('America/Chicago')

    now = datetime.now(tz)
    try:
        start_h, start_m = map(int, start_str.split(':'))
        end_h, end_m = map(int, end_str.split(':'))
    except (ValueError, AttributeError):
        return True, None

    # Validate hour/minute ranges
    if not (0 <= start_h <= 23 and 0 <= start_m <= 59 and 0 <= end_h <= 23 and 0 <= end_m <= 59):
        return True, None

    current_minutes = now.hour * 60 + now.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    if start_minutes <= end_minutes:
        # Normal range (e.g., 08:00-21:00)
        if current_minutes < start_minutes or current_minutes >= end_minutes:
            return False, f"Outside calling hours ({start_str}-{end_str} {tz_str})"
    else:
        # Wraps midnight (e.g., 22:00-06:00): allowed if >= start OR < end
        if current_minutes < start_minutes and current_minutes >= end_minutes:
            return False, f"Outside calling hours ({start_str}-{end_str} {tz_str})"
    return True, None


def _check_cooldown_and_daily_max(location_id, phones, voice_config, conn):
    """Batch check cooldown and daily max for a list of phone numbers.
    Returns dict of phone -> reason string for phones that should be skipped."""
    try:
        cooldown_hours = int(voice_config.get('same_number_cooldown_hours', 0))
    except (ValueError, TypeError):
        cooldown_hours = 0
    try:
        daily_max = int(voice_config.get('same_contact_daily_max', 0))
    except (ValueError, TypeError):
        daily_max = 0
    if not cooldown_hours and not daily_max:
        return {}

    blocked = {}
    if not phones or not conn:
        return blocked

    try:
        cur = conn.cursor()

        if cooldown_hours > 0:
            cur.execute("""
                SELECT phone, MAX(created_at) AS last_called
                FROM call_history
                WHERE location_id = %s AND phone = ANY(%s)
                  AND created_at > NOW() - make_interval(hours => %s)
                GROUP BY phone
            """, (location_id, list(phones), cooldown_hours))
            for row in cur.fetchall():
                blocked[row['phone']] = f"Cooldown: called within {cooldown_hours}h"

        if daily_max > 0:
            cur.execute("""
                SELECT phone, COUNT(*) AS cnt
                FROM call_history
                WHERE location_id = %s AND phone = ANY(%s)
                  AND created_at >= CURRENT_DATE
                GROUP BY phone
                HAVING COUNT(*) >= %s
            """, (location_id, list(phones), daily_max))
            for row in cur.fetchall():
                if row['phone'] not in blocked:
                    blocked[row['phone']] = f"Daily max: {daily_max} calls/day reached"

        cur.close()
    except Exception as e:
        logger.warning(f"Cooldown/daily-max check failed: {e}")

    return blocked


@dialer_bp.route('/voice/test', methods=['POST'])
@login_required
def test_voice_connection():
    """Test that XAI and Voice credentials are valid."""
    data = request.json or {}
    location_id = data.get('location_id', '')

    results = {"xai": False, "voice_service": False, "errors": []}

    # Test XAI API key
    if XAI_API_KEY:
        try:
            resp = httpx.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {XAI_API_KEY}"},
                json={"model": "grok-4-1-fast-non-reasoning", "messages": [{"role": "user", "content": "test"}], "max_tokens": 5},
                timeout=10,
            )
            results["xai"] = resp.status_code == 200
            if resp.status_code != 200:
                results["errors"].append(f"XAI API returned {resp.status_code}")
        except Exception as e:
            results["errors"].append(f"XAI connection failed: {str(e)}")
    else:
        results["errors"].append("XAI_API_KEY not configured")

    # Test voice service via Twilio sub-account
    if location_id:
        subscriber = _get_subscriber_by_location(location_id)
        if subscriber:
            voice_config = subscriber.get("voice_config") or {}
            sub_sid = voice_config.get("twilio_sub_account_sid", "")
            if sub_sid:
                try:
                    client = twilio_provisioning.get_sub_account_client(sub_sid)
                    account = client.api.accounts(sub_sid).fetch()
                    results["voice_service"] = account.status == "active"
                    if account.status != "active":
                        results["errors"].append(f"Voice sub-account status: {account.status}")
                except Exception as e:
                    results["errors"].append(f"Voice service check failed: {str(e)}")
            else:
                results["errors"].append("Voice service not provisioned")

    return jsonify(results)


@dialer_bp.route('/voice/preview/<voice_name>', methods=['GET'])
@login_required
def preview_voice(voice_name):
    """Generate a short audio sample for the selected voice."""
    voice = VOICE_OPTIONS.get(voice_name.lower(), DEFAULT_VOICE)

    if not XAI_API_KEY:
        return jsonify({"error": "XAI API key not configured"}), 400

    loop = asyncio.new_event_loop()
    try:
        audio_data = loop.run_until_complete(_generate_voice_preview(voice))
    finally:
        loop.close()

    if not audio_data:
        return jsonify({"error": "Failed to generate preview"}), 500

    # Audio is now L16 PCM 16kHz (same as live calls) — wrap in WAV container
    wav_data = _pcm16_to_wav(audio_data, sample_rate=16000)
    return Response(wav_data, content_type='audio/wav',
                    headers={'Cache-Control': 'public, max-age=3600'})


# ──────────────────────────────────────────────────────────────
# CALL PANEL: Power dialer with GHL contact search + queue
# ──────────────────────────────────────────────────────────────

GHL_API_BASE = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "2021-07-28"


@dialer_bp.route('/voice/call-panel')
@login_required
def call_panel():
    """Serve the call panel page (works standalone or in iframe)."""
    from flask import render_template
    conn = get_db_connection()
    if not conn:
        return "Database error", 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, voice_config, bot_first_name FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return "Account not found", 404
        location_id = row['location_id'] or ''
        voice_config = row['voice_config'] or {}
        bot_name = row['bot_first_name'] or 'AI Agent'
        return render_template('call_panel.html',
            location_id=location_id,
            voice_config=voice_config,
            bot_name=bot_name
        )
    finally:
        return_db_connection(conn)


GHL_429_MAX_RETRIES = 3


def _ghl_request_with_retry(url, headers, params, timeout=15):
    """Make a GHL API GET request with automatic retry + backoff on 429."""
    for attempt in range(GHL_429_MAX_RETRIES + 1):
        resp = http_requests.get(url, headers=headers, params=params, timeout=timeout)
        if resp.status_code != 429:
            return resp
        retry_after = int(resp.headers.get("Retry-After", 0))
        wait = max(retry_after, 2 ** (attempt + 1))  # 2s, 4s, 8s
        logger.warning(f"GHL 429 rate-limited on {url}, waiting {wait}s (attempt {attempt + 1}/{GHL_429_MAX_RETRIES + 1})")
        time.sleep(wait)
    return resp


# ── Helper: fetch all contacts from GHL API (paginated, with token refresh) ──
def _fetch_all_ghl_contacts(location_id, access_token, ghl_query=None):
    """Paginate through GHL contacts API. Returns (contacts_list, fetch_complete).
    fetch_complete=True means we got ALL contacts (pagination exhausted naturally).
    fetch_complete=False means we hit the max page cap (truncated — unsafe to prune stale).
    Handles 401 mid-pagination by refreshing the token and retrying."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": GHL_API_VERSION,
        "Content-Type": "application/json"
    }
    all_contacts = []
    page_limit = 100
    max_pages = 100  # 10,000 contacts max
    base_url = f"{GHL_API_BASE}/contacts/"
    url = base_url
    params = {"locationId": location_id, "limit": page_limit}
    if ghl_query:
        params["query"] = ghl_query
    token_refreshed = False
    fetch_complete = True  # assume complete unless we hit max_pages cap

    logger.info(f"[Contacts] {location_id} starting paginated fetch (page_limit={page_limit}, max_pages={max_pages}, query={ghl_query!r})")

    for page_num in range(max_pages):
        try:
            resp = _ghl_request_with_retry(url, headers=headers, params=params, timeout=20)
        except Exception as e:
            logger.error(f"[Contacts] Page {page_num+1} network error for {location_id}: {e}")
            fetch_complete = False
            break

        # Handle 401 — refresh token once and retry
        if resp.status_code in (401, 403) and not token_refreshed:
            logger.warning(f"[Contacts] Page {page_num+1} got {resp.status_code} — refreshing token for {location_id}")
            new_token = get_valid_token(location_id)
            if new_token and new_token != access_token:
                access_token = new_token
                headers["Authorization"] = f"Bearer {access_token}"
                token_refreshed = True
                # Retry the same page
                try:
                    resp = _ghl_request_with_retry(url, headers=headers, params=params, timeout=20)
                except Exception as e:
                    logger.error(f"[Contacts] Retry after token refresh failed: {e}")
                    fetch_complete = False
                    break

        if resp.status_code != 200:
            logger.error(f"[Contacts] Page {page_num+1} returned {resp.status_code} for {location_id}: {resp.text[:200]}")
            fetch_complete = False
            break

        data = resp.json()
        contacts = data.get("contacts", [])
        meta = data.get("meta", {})
        meta_total = meta.get("total")

        if not contacts:
            logger.info(f"[Contacts] Page {page_num+1} returned 0 contacts — pagination complete (total so far: {len(all_contacts)}, meta.total={meta_total})")
            break
        all_contacts.extend(contacts)

        logger.info(f"[Contacts] Page {page_num+1}: got {len(contacts)} contacts (running total: {len(all_contacts)}, meta.total={meta_total}, meta keys: {list(meta.keys())})")

        # Guard: stop if we've fetched more contacts than meta.total says exist
        if meta_total and len(all_contacts) >= meta_total:
            logger.info(f"[Contacts] Fetched {len(all_contacts)} >= meta.total {meta_total} — all contacts retrieved")
            break

        if len(contacts) < page_limit:
            logger.info(f"[Contacts] Page {page_num+1} returned {len(contacts)} < {page_limit} — last page reached naturally")
            break

        # GHL pagination: requires BOTH startAfterId AND startAfter from response meta
        # See: https://marketplace.gohighlevel.com/docs/ghl/contacts/get-contacts/index.html
        start_after_id = meta.get("startAfterId") or meta.get("nextPageStartAfterId")
        start_after_val = meta.get("startAfter")
        next_page_url = meta.get("nextPageUrl") or meta.get("nextPage")

        if start_after_id or start_after_val:
            # GHL requires both cursors together for proper pagination
            if start_after_id:
                params["startAfterId"] = start_after_id
            else:
                params.pop("startAfterId", None)
            if start_after_val is not None:
                params["startAfter"] = start_after_val
            else:
                params.pop("startAfter", None)
            # Reset URL in case a previous iteration used nextPageUrl
            url = base_url
            logger.info(f"[Contacts] Next page via cursor: startAfterId={start_after_id}, startAfter={start_after_val}")
        elif isinstance(next_page_url, str) and next_page_url.startswith("http"):
            # Full URL provided by GHL — use it but keep auth headers
            url = next_page_url
            params = {}
            logger.info(f"[Contacts] Next page via full URL: {next_page_url[:120]}")
        elif meta_total and len(all_contacts) < meta_total:
            # We know there are more contacts (meta.total tells us) but no cursor —
            # fall back to offset-based pagination using contact count so far
            params["startAfter"] = len(all_contacts)
            params.pop("startAfterId", None)
            url = base_url
            logger.info(f"[Contacts] No cursor but meta.total={meta_total} > {len(all_contacts)} — using offset fallback startAfter={len(all_contacts)}")
        else:
            # Last resort: if we got a full page (100) it's likely there are more contacts.
            # GHL sometimes returns no cursor and no meta.total — try offset-based anyway.
            if len(contacts) == page_limit:
                params["startAfter"] = len(all_contacts)
                params.pop("startAfterId", None)
                url = base_url
                logger.warning(f"[Contacts] No pagination cursor AND no meta.total after page {page_num+1} (meta={meta}) — "
                               f"got full page of {page_limit}, trying offset fallback startAfter={len(all_contacts)}")
            else:
                logger.warning(f"[Contacts] No pagination cursor after page {page_num+1} (meta={meta}) — stopping at {len(all_contacts)} contacts")
                break

        # Brief pause to be kind to GHL API
        time.sleep(0.3)
    else:
        # for-loop exhausted without break → hit max_pages cap (truncated)
        fetch_complete = False
        logger.warning(f"[Contacts] {location_id} hit max page cap ({max_pages}) — fetch truncated at {len(all_contacts)} contacts")

    logger.info(f"[Contacts] {location_id} total raw contacts fetched: {len(all_contacts)} (complete={fetch_complete})")

    # Build simplified list (phone required for dialer)
    result = []
    skipped_no_phone = 0
    for c in all_contacts:
        phone = c.get("phone", "")
        if not phone:
            skipped_no_phone += 1
            continue
        result.append({
            "id": c.get("id", ""),
            "name": f"{c.get('firstName', '')} {c.get('lastName', '')}".strip() or "Unknown",
            "firstName": c.get("firstName", ""),
            "lastName": c.get("lastName", ""),
            "phone": phone,
            "email": c.get("email", ""),
            "tags": c.get("tags", []),
            "dateAdded": c.get("dateAdded", ""),
            "dnd": c.get("dnd", False),
        })

    if skipped_no_phone:
        logger.info(f"[Contacts] {location_id} skipped {skipped_no_phone} contacts without phone numbers")
    logger.info(f"[Contacts] {location_id} returning {len(result)} contacts with phone numbers (raw={len(all_contacts)}, no_phone={skipped_no_phone})")
    return result, fetch_complete


# ── Helper: fetch pipeline/stage contacts from GHL opportunities API ──
def _fetch_pipeline_ghl_contacts(location_id, access_token, pipeline_id, stage_id):
    """Fetch contacts filtered by pipeline/stage via opportunities API."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": GHL_API_VERSION,
        "Content-Type": "application/json"
    }
    all_contacts = []
    page_limit = 100
    max_pages = 50
    page = 1
    seen_contact_ids = set()

    while page <= max_pages:
        opp_params = {
            "location_id": location_id,
            "pipeline_id": pipeline_id,
            "limit": page_limit,
            "page": page,
        }
        if stage_id:
            opp_params["pipeline_stage_id"] = stage_id

        resp = _ghl_request_with_retry(
            f"{GHL_API_BASE}/opportunities/search",
            headers=headers, params=opp_params, timeout=15,
        )
        if resp.status_code != 200:
            break

        data = resp.json()
        opportunities = data.get("opportunities", [])
        if not opportunities:
            break

        for opp in opportunities:
            contact = opp.get("contact", {})
            contact_id = contact.get("id", "")
            if contact_id and contact_id not in seen_contact_ids:
                seen_contact_ids.add(contact_id)
                all_contacts.append({
                    "id": contact_id,
                    "name": contact.get("name", "") or "Unknown",
                    "firstName": contact.get("name", "").split(" ")[0] if contact.get("name") else "",
                    "lastName": " ".join(contact.get("name", "").split(" ")[1:]) if contact.get("name") else "",
                    "phone": contact.get("phone", ""),
                    "email": contact.get("email", ""),
                    "tags": contact.get("tags", []),
                    "dateAdded": contact.get("dateAdded", ""),
                })

        meta = data.get("meta", {})
        if len(opportunities) < page_limit or not meta.get("nextPage"):
            break
        page += 1

    return [c for c in all_contacts if c.get("phone")]


# ── Background contact cache sync (runs in thread) ──
_contact_sync_lock = threading.Lock()
_contact_sync_active = set()

def _background_contact_sync(location_id):
    """Sync contacts from GHL to DB cache in background thread.
    Thread-safe: only one sync per location at a time."""
    with _contact_sync_lock:
        if location_id in _contact_sync_active:
            logger.info(f"[BgSync] {location_id} already syncing — skipping duplicate request")
            return
        _contact_sync_active.add(location_id)

    logger.info(f"[BgSync] {location_id} starting background contact cache sync")

    def _do_sync():
        try:
            access_token = get_valid_token(location_id)
            if not access_token:
                logger.warning(f"[BgSync] {location_id} no valid token — cannot sync contacts")
                return
            logger.info(f"[BgSync] {location_id} fetching all contacts from GHL...")
            contacts, fetch_complete = _fetch_all_ghl_contacts(location_id, access_token)
            if contacts:
                from db import upsert_contact_cache
                upsert_contact_cache(location_id, contacts, prune_stale=fetch_complete)
                logger.info(f"[BgSync] {location_id} DONE — cached {len(contacts)} contacts (fetch_complete={fetch_complete})")
            else:
                logger.warning(f"[BgSync] {location_id} GHL returned 0 contacts — cache NOT updated")
        except Exception as e:
            logger.error(f"[BgSync] {location_id} FAILED: {e}", exc_info=True)
        finally:
            with _contact_sync_lock:
                _contact_sync_active.discard(location_id)

    t = threading.Thread(target=_do_sync, daemon=True)
    t.start()


_CACHE_FRESH_SECS = 3600    # 1 hour — serve without background refresh
                              # Beyond 1 hour — serve + trigger background refresh


@dialer_bp.route('/voice/contacts', methods=['GET'])
@login_required
def fetch_contacts():
    """
    Fetch contacts with persistent DB cache for fast loading.
    Cache strategy:
    - DB cache < 15 min → serve instantly
    - DB cache 15min–6hr → serve instantly + background refresh
    - DB cache empty or > 6hr → synchronous GHL fetch + cache
    - force_refresh=1 → synchronous GHL fetch + cache
    Pipeline/stage filtering uses Redis cache (5 min) + GHL opportunities API.
    """
    query = request.args.get('q', '').strip()
    pipeline_id = request.args.get('pipeline', '').strip()
    stage_id = request.args.get('stage', '').strip()
    force_refresh = request.args.get('refresh', '').strip() == '1'

    # Resolve location_id (seat users use location_users table)
    _assigned_to_filter = None
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        if getattr(current_user, 'is_seat_user', False) and getattr(current_user, 'seat_user_id', None):
            cur.execute("""
                SELECT lu.location_id, lu.ghl_user_id, lu.permissions
                FROM location_users lu WHERE lu.id = %s
            """, (current_user.seat_user_id,))
            row = cur.fetchone()
            if row and row.get('location_id'):
                location_id = row['location_id']
                perms = row.get('permissions') or {}
                if not perms.get('can_view_all_leads', False) and row.get('ghl_user_id'):
                    _assigned_to_filter = row['ghl_user_id']
            else:
                cur.close()
                return jsonify({"error": "No location configured"}), 400
        else:
            cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
            row = cur.fetchone()
            if not row or not row['location_id']:
                cur.close()
                return jsonify({"error": "No location configured"}), 400
            location_id = row['location_id']
        cur.close()
    finally:
        return_db_connection(conn)

    # ── Pipeline/stage filter: use Redis cache + GHL opportunities API ──
    if pipeline_id or stage_id:
        cache_key = f"contacts:{location_id}:{pipeline_id or '_'}:{stage_id or '_'}"
        if not force_refresh:
            try:
                from main import redis_conn
                if redis_conn:
                    cached = redis_conn.get(cache_key)
                    if cached:
                        result = json.loads(cached)
                        if query:
                            q_lower = query.lower()
                            result = [c for c in result if
                                      q_lower in c.get("name", "").lower() or
                                      q_lower in c.get("phone", "").lower() or
                                      q_lower in c.get("email", "").lower() or
                                      any(q_lower in t.lower() for t in (c.get("tags") or []))]
                        return jsonify({"contacts": result, "total": len(result), "cached": True})
            except Exception:
                pass

        access_token = get_valid_token(location_id)
        if not access_token:
            return jsonify({"error": "No valid auth token. Reconnect your CRM."}), 401
        try:
            result = _fetch_pipeline_ghl_contacts(location_id, access_token, pipeline_id, stage_id)
            # Cache pipeline results in Redis (5 min)
            try:
                from main import redis_conn
                if redis_conn and result:
                    redis_conn.setex(cache_key, 300, json.dumps(result))
            except Exception:
                pass
            if query:
                q_lower = query.lower()
                result = [c for c in result if
                          q_lower in c.get("name", "").lower() or
                          q_lower in c.get("phone", "").lower() or
                          q_lower in c.get("email", "").lower() or
                          any(q_lower in t.lower() for t in (c.get("tags") or []))]
            return jsonify({"contacts": result, "total": len(result), "cached": False})
        except Exception as e:
            logger.error(f"Failed to fetch pipeline contacts: {e}")
            return jsonify({"error": str(e)}), 500

    # ── All contacts: persistent DB cache first ──
    import datetime
    from db import get_cached_contacts, get_contact_cache_age, upsert_contact_cache

    cache_age = get_contact_cache_age(location_id)
    has_cache = cache_age is not None

    # If cache exists and not force-refresh → serve instantly, refresh in bg if stale
    if has_cache and not force_refresh:
        age_secs = (datetime.datetime.utcnow() - cache_age).total_seconds()
        result = get_cached_contacts(location_id, query, assigned_to=_assigned_to_filter)
        resp_data = {
            "contacts": result,
            "total": len(result),
            "cached": True,
            "cached_at": cache_age.isoformat(),
        }

        # Trigger background refresh if stale (>1 hour)
        if age_secs > _CACHE_FRESH_SECS:
            resp_data["refreshing"] = True
            _background_contact_sync(location_id)

        return jsonify(resp_data)

    # If cache exists but force_refresh → serve stale cache NOW, refresh in bg
    if has_cache and force_refresh:
        result = get_cached_contacts(location_id, query, assigned_to=_assigned_to_filter)
        _background_contact_sync(location_id)
        return jsonify({
            "contacts": result,
            "total": len(result),
            "cached": True,
            "cached_at": cache_age.isoformat(),
            "refreshing": True,
        })

    # ── No cache at all (first time): synchronous GHL fetch ──
    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid auth token. Reconnect your CRM."}), 401

    try:
        result, fetch_complete = _fetch_all_ghl_contacts(location_id, access_token)
        logger.info(f"Fetched {len(result)} contacts from GHL for {location_id} (complete={fetch_complete})")

        # Persist to DB cache — only prune stale contacts if fetch was complete
        if result:
            upsert_contact_cache(location_id, result, prune_stale=fetch_complete)

        # Filter by assigned_to for seat users
        if _assigned_to_filter:
            result = [c for c in result if c.get("assignedTo") == _assigned_to_filter]

        # Apply search filter
        if query:
            q_lower = query.lower()
            result = [c for c in result if
                      q_lower in c.get("name", "").lower() or
                      q_lower in c.get("phone", "").lower() or
                      q_lower in c.get("email", "").lower() or
                      any(q_lower in t.lower() for t in (c.get("tags") or []))]

        return jsonify({"contacts": result, "total": len(result), "cached": False})

    except Exception as e:
        logger.error(f"Failed to fetch contacts: {e}")
        return jsonify({"error": str(e)}), 500


@dialer_bp.route('/voice/contacts/sync', methods=['POST'])
@login_required
def sync_contacts_cache():
    """Manually trigger a full contact cache sync from GHL."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        # Support seat users
        if getattr(current_user, 'is_seat_user', False) and getattr(current_user, 'seat_user_id', None):
            cur.execute("SELECT location_id FROM location_users WHERE id = %s", (current_user.seat_user_id,))
        else:
            cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
    finally:
        return_db_connection(conn)

    _background_contact_sync(location_id)
    return jsonify({"status": "sync_started"})


@dialer_bp.route('/voice/contacts/tags', methods=['GET'])
@login_required
def get_contact_tags():
    """Return all unique tags across contacts for export filter UI."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        if getattr(current_user, 'is_seat_user', False) and getattr(current_user, 'seat_user_id', None):
            cur.execute("SELECT location_id FROM location_users WHERE id = %s", (current_user.seat_user_id,))
        else:
            cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row or not row.get('location_id'):
            cur.close()
            return jsonify({"tags": []})
        location_id = row['location_id']
        cur.execute("""
            SELECT DISTINCT tag FROM contact_cache, jsonb_array_elements_text(tags) AS tag
            WHERE location_id = %s ORDER BY tag
        """, (location_id,))
        tags = [r['tag'] for r in cur.fetchall()]
        cur.close()
        return jsonify({"tags": tags})
    except Exception as e:
        logger.error(f"get_contact_tags error: {e}")
        return jsonify({"tags": []})
    finally:
        return_db_connection(conn)


@dialer_bp.route('/voice/contacts/export', methods=['POST'])
@login_required
def export_contacts():
    """
    Export contacts from cache as CSV with optional filters.
    Body: { tags: [], date_from: "YYYY-MM-DD", date_to: "YYYY-MM-DD",
            search: "", dnd: null|true|false, include_fields: [] }
    """
    import csv
    import io

    data = request.get_json(silent=True) or {}
    filter_tags = data.get('tags') or []
    date_from = (data.get('date_from') or '').strip()
    date_to = (data.get('date_to') or '').strip()
    search = (data.get('search') or '').strip()
    dnd_filter = data.get('dnd')  # null=all, true=only DND, false=exclude DND

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        # Resolve location_id + seat user filtering
        _assigned_to = None
        if getattr(current_user, 'is_seat_user', False) and getattr(current_user, 'seat_user_id', None):
            cur.execute("""
                SELECT lu.location_id, lu.ghl_user_id, lu.permissions
                FROM location_users lu WHERE lu.id = %s
            """, (current_user.seat_user_id,))
            row = cur.fetchone()
            if not row or not row.get('location_id'):
                return jsonify({"error": "No location configured"}), 400
            location_id = row['location_id']
            perms = row.get('permissions') or {}
            if not perms.get('can_view_all_leads', False) and row.get('ghl_user_id'):
                _assigned_to = row['ghl_user_id']
        else:
            cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
            row = cur.fetchone()
            if not row or not row.get('location_id'):
                return jsonify({"error": "No location configured"}), 400
            location_id = row['location_id']

        # Build query with filters
        where = ["location_id = %s"]
        params = [location_id]

        if _assigned_to:
            where.append("assigned_to = %s")
            params.append(_assigned_to)

        if search:
            where.append("(LOWER(name) LIKE %s OR LOWER(phone) LIKE %s OR LOWER(email) LIKE %s)")
            s = f"%{search.lower()}%"
            params.extend([s, s, s])

        if dnd_filter is True:
            where.append("dnd = true")
        elif dnd_filter is False:
            where.append("(dnd = false OR dnd IS NULL)")

        if date_from:
            where.append("date_added >= %s")
            params.append(date_from)
        if date_to:
            where.append("date_added <= %s")
            params.append(date_to + "T23:59:59")

        if filter_tags:
            where.append("tags ?| %s")
            params.append(filter_tags)

        sql = f"""
            SELECT contact_id, name, first_name, last_name, phone, email,
                   tags, date_added, dnd, assigned_to
            FROM contact_cache
            WHERE {' AND '.join(where)}
            ORDER BY name ASC
        """
        cur.execute(sql, params)
        rows = cur.fetchall()
        cur.close()

        # Generate CSV
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            'Contact ID', 'Full Name', 'First Name', 'Last Name',
            'Phone', 'Email', 'Tags', 'Date Added', 'Do Not Disturb', 'Assigned To'
        ])
        for r in rows:
            tags_val = r.get('tags') or []
            if isinstance(tags_val, str):
                try:
                    tags_val = json.loads(tags_val)
                except Exception:
                    tags_val = []
            writer.writerow([
                r.get('contact_id', ''),
                r.get('name', ''),
                r.get('first_name', ''),
                r.get('last_name', ''),
                r.get('phone', ''),
                r.get('email', ''),
                '; '.join(tags_val) if isinstance(tags_val, list) else str(tags_val),
                r.get('date_added', ''),
                'Yes' if r.get('dnd') else 'No',
                r.get('assigned_to', ''),
            ])

        csv_content = output.getvalue()
        output.close()

        return Response(
            csv_content,
            mimetype='text/csv',
            headers={
                'Content-Disposition': f'attachment; filename="contacts-export-{datetime.now().strftime("%Y%m%d-%H%M%S")}.csv"'
            }
        )

    except Exception as e:
        logger.error(f"export_contacts error: {e}")
        return jsonify({"error": f"Export failed: {str(e)}"}), 500
    finally:
        return_db_connection(conn)


@dialer_bp.route('/voice/dial', methods=['POST'])
@jwt_or_session_required
@require_permission('can_dial')
def dial_contact():
    """
    Initiate an outbound call to a specific contact via Twilio.
    Used by the call panel. Returns call_sid for status tracking.
    """
    data = request.json or {}
    contact_id    = data.get('contact_id', '')
    phone         = data.get('phone', '')
    first_name    = data.get('first_name', 'there')
    dial_mode     = data.get('dial_mode', 'ai')
    try:
        dial_attempt = int(data.get('dial_attempt', 1))
    except (ValueError, TypeError):
        dial_attempt = 1

    if not phone:
        return jsonify({"error": "Phone number is required"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        subscriber = dict(row)
    finally:
        return_db_connection(conn)

    location_id  = subscriber.get('location_id', '')
    voice_config = subscriber.get('voice_config') or {}
    tier = subscriber.get('subscription_tier', 'individual')
    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]

    # Manual dial: resolve contact name + ID by phone number lookup in GHL
    if first_name in ('Manual', 'there', '') and phone and location_id:
        try:
            access_token = get_valid_token(location_id)
            if access_token and access_token != 'DEMO':
                resp = http_requests.get(
                    f"{GHL_API_BASE}/contacts/",
                    headers={"Authorization": f"Bearer {access_token}", "Version": "2021-07-28"},
                    params={"query": phone, "locationId": location_id, "limit": 1},
                    timeout=8
                )
                if resp.status_code == 200:
                    contacts = resp.json().get("contacts", [])
                    if contacts:
                        c = contacts[0]
                        resolved_name = c.get("firstName", "").strip()
                        if resolved_name:
                            first_name = resolved_name
                            logger.info(f"Manual dial: resolved {phone} -> {first_name} (contact {c.get('id','')})")
                        if not contact_id and c.get("id"):
                            contact_id = c["id"]
        except Exception as e:
            logger.debug(f"Manual dial contact lookup failed (non-fatal): {e}")

    # Fallback: treat placeholder names as "there" so greeting skips the name
    if first_name in ('Manual', ''):
        first_name = 'there'

    # ── Calling hours enforcement ──
    bypass_hours = voice_config.get('bypass_calling_hours', False)
    hours_ok, hours_reason = _check_calling_hours(voice_config, subscriber.get('timezone'))
    if not hours_ok and not bypass_hours:
        return jsonify({"error": hours_reason, "calling_hours_blocked": True}), 400

    # ── Recipient timezone enforcement (compliance — pro_dialer+ only) ──
    if tier in ('pro_dialer', 'predictive_dialer'):
        tz_ok, tz_reason, recip_tz, recip_time = check_recipient_timezone(
            phone,
            voice_config.get('calling_hours_start', '08:00'),
            voice_config.get('calling_hours_end', '21:00'),
        )
        if not tz_ok and not bypass_hours:
            return jsonify({"error": tz_reason, "recipient_tz_blocked": True,
                            "recipient_timezone": recip_tz, "recipient_local_time": recip_time}), 400

    # Enforce max dial attempts server-side
    try:
        max_attempts = int(voice_config.get('dial_attempts', 2))
    except (ValueError, TypeError):
        max_attempts = 2
    if dial_attempt > max_attempts:
        logger.warning(f"Blocked dial attempt {dial_attempt} > max {max_attempts} for {current_user.email}")
        return jsonify({"error": f"Max dial attempts ({max_attempts}) exceeded"}), 400

    # ── DnD / opt-out guard: never dial contacts flagged Do Not Contact ──
    if contact_id and location_id:
        _dnd_conn = get_db_connection()
        if _dnd_conn:
            try:
                _dnd_cur = _dnd_conn.cursor()
                _dnd_cur.execute(
                    "SELECT dnd FROM contact_cache WHERE location_id = %s AND contact_id = %s",
                    (location_id, contact_id)
                )
                _dnd_row = _dnd_cur.fetchone()
                _dnd_cur.close()
                if _dnd_row and _dnd_row.get("dnd"):
                    logger.warning(f"Blocked dial to DnD contact {contact_id} ({phone}) for {current_user.email}")
                    return jsonify({"error": "Contact is marked Do Not Contact"}), 403
            except Exception as _dnd_e:
                logger.debug(f"DnD check failed (non-fatal): {_dnd_e}")
            finally:
                return_db_connection(_dnd_conn)

    # ── Cooldown / daily max enforcement (same as multi_dial) ──
    if phone and location_id:
        _cd_conn = get_db_connection()
        if _cd_conn:
            try:
                blocked = _check_cooldown_and_daily_max(location_id, [phone], voice_config, _cd_conn)
                if phone in blocked:
                    return jsonify({"error": blocked[phone], "cooldown_blocked": True}), 400
            except Exception as _cd_e:
                logger.warning(f"Cooldown check failed (non-fatal): {_cd_e}")
            finally:
                return_db_connection(_cd_conn)

    if dial_mode == 'ai' and not voice_config.get('enabled'):
        return jsonify({"error": "Voice AI is not enabled. Enable it in the Voice tab."}), 400

    # ── AI Minutes balance check: block AI calls when balance is 0 ──
    if dial_mode == 'ai' and not is_admin:
        try:
            from db import get_ai_minute_balance
            bal = get_ai_minute_balance(current_user.email)
            if bal.get('total_purchased', 0) > 0 and bal.get('balance_minutes', 0) <= 0:
                return jsonify({"error": "You're out of AI minutes. Purchase more to continue making AI calls.",
                                "minutes_required": True}), 402
        except Exception as e:
            logger.warning(f"AI minutes check failed (non-fatal): {e}")

    sub_sid      = voice_config.get('twilio_sub_account_sid', '')
    from_number  = voice_config.get('twilio_phone_number', '')

    # Smart number rotation: if enabled, let the health engine pick the best number
    rotation_result = select_outbound_number(location_id, voice_config, dest_phone=phone)
    if rotation_result:
        from_number = rotation_result["phone"]
        logger.info(f"Smart rotation selected {from_number} (reason={rotation_result['reason']}, health={rotation_result.get('health_score', '?')}, daily={rotation_result.get('daily_calls', '?')}/{rotation_result.get('daily_cap', '?')})")
    else:
        # Rotation disabled — use legacy local presence logic
        local_presence_enabled = voice_config.get('local_presence', False)
        if local_presence_enabled:
            dest_area = phone.lstrip('+').lstrip('1')[:3] if phone else ''
            local_pool = voice_config.get('local_presence_numbers', [])
            for lp_num in local_pool:
                lp_area = lp_num.lstrip('+').lstrip('1')[:3]
                if lp_area == dest_area:
                    from_number = lp_num
                    break

    if not sub_sid or not from_number:
        return jsonify({"error": "Voice service not fully provisioned"}), 400

    # AMD: always on for AI mode (needs to detect voicemail); for human mode, respect user setting
    use_amd = True if dial_mode == 'ai' else voice_config.get('use_amd', False)

    # Idempotency guard: prevent double-dial to the same phone number.
    # If a non-terminal call to this phone already exists for this location, return it.
    for sid, info in list(active_calls.items()):
        if (info.get('phone') == phone
                and info.get('_location_id') == location_id
                and info.get('status') not in ('completed', 'busy', 'no-answer', 'failed', 'canceled')):
            logger.warning(f"Double-dial blocked: {phone} already has active call {sid[:16]} (status={info.get('status')})")
            return jsonify({"status": "calling", "call_sid": sid, "dial_mode": dial_mode})

    try:
        host = request.host
        webhook_base_url = f"https://{host}"

        custom_params = {
            'location_id':  location_id,
            'caller':       from_number,
            'called':       phone,
            'direction':    'outbound',
            'contact_id':   contact_id,
            'contact_name': first_name,
            'dial_mode':    dial_mode,
        }

        try:
            ring_timeout = int(voice_config.get('ring_timeout', 45))
        except (ValueError, TypeError):
            ring_timeout = 45
        result = twilio_provisioning.create_outbound_call(
            sub_account_sid=sub_sid,
            to=phone,
            from_number=from_number,
            webhook_base_url=webhook_base_url,
            machine_detection='DetectMessageEnd' if use_amd else None,
            custom_params=custom_params,
            ring_timeout=ring_timeout,
        )
        call_sid = result.get('call_sid', '')

        active_calls[call_sid] = {
            "status":     "initiated",
            "duration":   0,
            "contact_id": contact_id,
            "phone":      phone,
            "name":       first_name,
            "dial_mode":  dial_mode,
            "attempt":    dial_attempt,
            "_location_id": location_id,
            "_sub_sid":     sub_sid,
            "_host":        request.host,
            "_from_number": from_number,
            "_agent_email": current_user.email,
            "_wrap_up_time": int(voice_config.get('wrap_up_time', 15)),
        }

        save_call_to_history(
            location_id=location_id,
            call_sid=call_sid,
            phone=phone,
            contact_id=contact_id,
            contact_name=first_name,
            direction='outbound',
            status='initiated',
            from_number=from_number,
        )

        logger.info(f"Dialer call [{dial_mode}]: {from_number} -> {phone} ({first_name}) attempt={dial_attempt} sid={call_sid}")
        return jsonify({"status": "calling", "call_sid": call_sid, "dial_mode": dial_mode})

    except Exception as e:
        logger.error(f"Dialer call failed: {e}")
        return jsonify({"error": str(e)}), 500


@dialer_bp.route('/voice/multi-dial', methods=['POST'])
@jwt_or_session_required
@require_permission('can_dial')
def multi_dial():
    """
    Multi-line dialer: initiate up to N concurrent calls.
    Requires 'pro_dialer' subscription tier.
    Returns list of {contact_id, call_sid, status, error} per line.
    """
    data = request.json or {}
    contacts = data.get('contacts', [])  # [{contact_id, phone, first_name}]
    dial_mode = data.get('dial_mode', 'ai')
    try:
        max_lines = int(data.get('max_lines', 3))
    except (ValueError, TypeError):
        max_lines = 3

    if not contacts:
        return jsonify({"error": "No contacts provided"}), 400
    # Cap max_lines to server-side limit (1-4)
    if max_lines < 1:
        max_lines = 1
    if max_lines > 4:
        max_lines = 4

    # ── Subscription tier gate ──
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        subscriber = dict(row)
    finally:
        return_db_connection(conn)

    tier = subscriber.get('subscription_tier', 'individual')
    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
    if tier not in ('pro_dialer', 'predictive_dialer'):
        return jsonify({"error": "Multi-line dialer requires Pro Dialer subscription", "upgrade_required": True}), 403

    location_id = subscriber.get('location_id', '')
    voice_config = subscriber.get('voice_config') or {}

    if dial_mode == 'ai' and not voice_config.get('enabled'):
        return jsonify({"error": "Voice AI is not enabled"}), 400

    sub_sid = voice_config.get('twilio_sub_account_sid', '')
    from_number = voice_config.get('twilio_phone_number', '')

    if not sub_sid or not from_number:
        return jsonify({"error": "Voice service not fully provisioned"}), 400

    # ── Calling hours enforcement ──
    bypass_hours = voice_config.get('bypass_calling_hours', False)
    hours_ok, hours_reason = _check_calling_hours(voice_config, subscriber.get('timezone'))
    if not hours_ok and not bypass_hours:
        return jsonify({"error": hours_reason, "calling_hours_blocked": True}), 400

    # Enforce max concurrent lines already active for this location
    active_for_location = sum(
        1 for sid, info in list(active_calls.items())
        if info.get('_location_id') == location_id
        and info.get('status') not in ('completed', 'busy', 'no-answer', 'failed', 'canceled')
    )
    available_lines = max(0, max_lines - active_for_location)
    if available_lines == 0:
        return jsonify({"error": f"All {max_lines} lines are busy", "active_lines": active_for_location}), 429

    # Limit contacts to available lines
    contacts_to_dial = contacts[:available_lines]
    results = []

    # Batch DnD check — single query for all contacts
    dnd_contact_ids = set()
    batch_cids = [c.get('contact_id', '') for c in contacts_to_dial if c.get('contact_id')]
    if batch_cids and location_id:
        _dnd_conn = get_db_connection()
        if _dnd_conn:
            try:
                _dnd_cur = _dnd_conn.cursor()
                _dnd_cur.execute(
                    "SELECT contact_id FROM contact_cache WHERE location_id = %s AND contact_id = ANY(%s) AND dnd = true",
                    (location_id, batch_cids)
                )
                dnd_contact_ids = {r['contact_id'] for r in _dnd_cur.fetchall()}
                _dnd_cur.close()
            except Exception as e:
                logger.warning(f"Multi-dial DnD batch check failed: {e}")
            finally:
                return_db_connection(_dnd_conn)

    # Batch cooldown + daily max check — single query for all phones
    blocked_phones = {}
    batch_phones = [c.get('phone', '') for c in contacts_to_dial if c.get('phone')]
    if batch_phones and location_id:
        _cd_conn = get_db_connection()
        if _cd_conn:
            try:
                blocked_phones = _check_cooldown_and_daily_max(location_id, batch_phones, voice_config, _cd_conn)
            except Exception as e:
                logger.warning(f"Multi-dial cooldown batch check failed: {e}")
            finally:
                return_db_connection(_cd_conn)

    # ── Per-contact timezone enforcement (compliance — pro_dialer+ only) ──
    tz_blocked_phones = set()
    if tier in ('pro_dialer', 'predictive_dialer'):
        calling_start = voice_config.get('calling_hours_start', '08:00')
        calling_end = voice_config.get('calling_hours_end', '21:00')
        for contact in contacts_to_dial:
            c_phone = contact.get('phone', '')
            if c_phone:
                tz_ok, tz_reason, _, _ = check_recipient_timezone(c_phone, calling_start, calling_end)
                if not tz_ok:
                    tz_blocked_phones.add(c_phone)

    for contact in contacts_to_dial:
        c_phone = contact.get('phone', '')
        c_name = contact.get('first_name', 'there')
        c_id = contact.get('contact_id', '')
        try:
            c_attempt = int(contact.get('dial_attempt', 1))
        except (ValueError, TypeError):
            c_attempt = 1

        if not c_phone:
            results.append({"contact_id": c_id, "call_sid": None, "status": "error", "error": "No phone number"})
            continue

        # Recipient timezone check
        if c_phone in tz_blocked_phones:
            state = area_code_to_state(c_phone) or "?"
            results.append({"contact_id": c_id, "call_sid": None, "status": "skipped",
                            "error": f"Outside calling hours in recipient's timezone ({state})",
                            "recipient_tz_blocked": True})
            continue

        # DnD check (from batch query above)
        if c_id in dnd_contact_ids:
            results.append({"contact_id": c_id, "call_sid": None, "status": "skipped", "error": "Do Not Contact"})
            continue

        # Cooldown / daily max check (from batch query above)
        if c_phone in blocked_phones:
            results.append({"contact_id": c_id, "call_sid": None, "status": "skipped", "error": blocked_phones[c_phone]})
            continue

        # Max attempts guard
        try:
            max_attempts = int(voice_config.get('dial_attempts', 2))
        except (ValueError, TypeError):
            max_attempts = 2
        if c_attempt > max_attempts:
            results.append({"contact_id": c_id, "call_sid": None, "status": "skipped", "error": "Max attempts exceeded"})
            continue

        # Double-dial guard per phone
        existing_sid = None
        for sid, info in list(active_calls.items()):
            if (info.get('phone') == c_phone
                    and info.get('_location_id') == location_id
                    and info.get('status') not in ('completed', 'busy', 'no-answer', 'failed', 'canceled')):
                existing_sid = sid
                break
        if existing_sid:
            results.append({"contact_id": c_id, "call_sid": existing_sid, "status": "already_active", "error": None})
            continue

        # Smart number rotation
        rotation_result = select_outbound_number(location_id, voice_config, dest_phone=c_phone)
        call_from = rotation_result["phone"] if rotation_result else from_number

        use_amd = True if dial_mode == 'ai' else voice_config.get('use_amd', False)

        try:
            host = request.host
            webhook_base_url = f"https://{host}"
            custom_params = {
                'location_id': location_id,
                'caller': call_from,
                'called': c_phone,
                'direction': 'outbound',
                'contact_id': c_id,
                'contact_name': c_name,
                'dial_mode': dial_mode,
            }
            try:
                ring_timeout = int(voice_config.get('ring_timeout', 45))
            except (ValueError, TypeError):
                ring_timeout = 45
            result = twilio_provisioning.create_outbound_call(
                sub_account_sid=sub_sid,
                to=c_phone,
                from_number=call_from,
                webhook_base_url=webhook_base_url,
                machine_detection='DetectMessageEnd' if use_amd else None,
                custom_params=custom_params,
                ring_timeout=ring_timeout,
            )
            call_sid = result.get('call_sid', '')

            active_calls[call_sid] = {
                "status": "initiated",
                "duration": 0,
                "contact_id": c_id,
                "phone": c_phone,
                "name": c_name,
                "dial_mode": dial_mode,
                "attempt": c_attempt,
                "_location_id": location_id,
                "_sub_sid": sub_sid,
                "_host": request.host,
                "_from_number": call_from,
                "_multi_line": True,
                "_agent_email": current_user.email,
                "_wrap_up_time": int(voice_config.get('wrap_up_time', 15)),
            }

            save_call_to_history(
                location_id=location_id,
                call_sid=call_sid,
                phone=c_phone,
                contact_id=c_id,
                contact_name=c_name,
                direction='outbound',
                status='initiated',
                from_number=call_from,
            )

            logger.info(f"Multi-dial [{dial_mode}]: {call_from} -> {c_phone} ({c_name}) attempt={c_attempt} sid={call_sid}")
            results.append({"contact_id": c_id, "call_sid": call_sid, "status": "initiated", "error": None})

        except Exception as e:
            logger.error(f"Multi-dial failed for {c_phone}: {e}")
            results.append({"contact_id": c_id, "call_sid": None, "status": "error", "error": str(e)})

    return jsonify({
        "results": results,
        "active_lines": active_for_location + sum(1 for r in results if r["status"] == "initiated"),
        "max_lines": max_lines
    })


@dialer_bp.route('/voice/active-lines', methods=['GET'])
@jwt_or_session_required
def get_active_lines():
    """Return count and details of currently active call lines for this user."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, subscription_tier FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        location_id = row['location_id']
        tier = row['subscription_tier']
    finally:
        return_db_connection(conn)

    max_lines = 4 if tier in ('pro_dialer', 'predictive_dialer') else 1

    lines = []
    for sid, info in list(active_calls.items()):
        if (info.get('_location_id') == location_id
                and info.get('status') not in ('completed', 'busy', 'no-answer', 'failed', 'canceled')):
            lines.append({
                "call_sid": sid,
                "contact_id": info.get("contact_id"),
                "phone": info.get("phone"),
                "name": info.get("name"),
                "status": info.get("status"),
                "duration": info.get("duration", 0),
                "dial_mode": info.get("dial_mode"),
                "ring_confirmed": info.get("_ring_confirmed", False),
            })

    return jsonify({
        "lines": lines,
        "active_count": len(lines),
        "max_lines": max_lines,
        "tier": tier
    })


@dialer_bp.route('/voice/multi-hangup', methods=['POST'])
@jwt_or_session_required
def multi_hangup():
    """Hang up multiple active calls at once."""
    data = request.json or {}
    call_sids = data.get('call_sids', [])
    if not call_sids:
        return jsonify({"error": "No call_sids provided"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        location_id = row['location_id']
        sub_sid = (row['voice_config'] or {}).get('twilio_sub_account_sid', '')
    finally:
        return_db_connection(conn)

    if not sub_sid:
        return jsonify({"error": "Voice not provisioned"}), 400

    results = []
    for sid in call_sids:
        try:
            # Ownership check: only allow hanging up calls belonging to this location
            call_info = active_calls.get(sid)
            if call_info and call_info.get('_location_id') and call_info['_location_id'] != location_id:
                results.append({"call_sid": sid, "success": False, "error": "Not your call"})
                continue

            success = _twilio_hangup(sid, sub_sid)
            if sid in active_calls:
                if success:
                    active_calls[sid]['status'] = 'completed'
                else:
                    active_calls[sid]['status'] = 'hangup-failed'
            transfer_requests.pop(sid, None)
            if success:
                try:
                    update_call_history_status(sid, 'completed', 0)
                except Exception:
                    pass
            results.append({"call_sid": sid, "success": success})
        except Exception as e:
            logger.error(f"Multi-hangup failed for {sid}: {e}")
            results.append({"call_sid": sid, "success": False, "error": str(e)})

    return jsonify({"results": results})


@dialer_bp.route('/voice/multi-status', methods=['POST'])
@jwt_or_session_required
def multi_call_status():
    """Poll status of multiple calls at once (reduces HTTP overhead for multi-line)."""
    data = request.json or {}
    call_sids = data.get('call_sids', [])
    if not call_sids:
        return jsonify({"error": "No call_sids provided"}), 400

    # Resolve caller's location_id for ownership filtering
    _owner_location = None
    _ms_conn = get_db_connection()
    if _ms_conn:
        try:
            _ms_cur = _ms_conn.cursor()
            _ms_cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
            _ms_row = _ms_cur.fetchone()
            _ms_cur.close()
            if _ms_row:
                _owner_location = _ms_row['location_id']
        except Exception:
            pass
        finally:
            return_db_connection(_ms_conn)

    statuses = {}
    for sid in call_sids:
        if sid in active_calls:
            info = active_calls[sid]
            # Ownership check: skip calls belonging to other locations
            if _owner_location and info.get('_location_id') and info['_location_id'] != _owner_location:
                statuses[sid] = {"status": "unknown"}
                continue
            # Terminal state cleanup (same logic as single poll)
            if info["status"] in ("completed", "busy", "no-answer", "failed", "canceled", "transferred"):
                poll_count = info.get('_terminal_polls', 0) + 1
                info['_terminal_polls'] = poll_count
                if poll_count >= 20:
                    status_copy = dict(info)
                    del active_calls[sid]
                    statuses[sid] = status_copy
                    continue
            entry = dict(info)
            # Normalize internal keys for client consumption
            if '_amd_result' in entry:
                entry['amd_result'] = entry['_amd_result']
            entry['ring_confirmed'] = entry.get('_ring_confirmed', False)
            statuses[sid] = entry
        else:
            statuses[sid] = {"status": "unknown"}

    return jsonify({"statuses": statuses})


@dialer_bp.route('/voice/predictive-stats', methods=['GET'])
@login_required
def predictive_stats():
    """
    Return predictive dialing statistics using Erlang-C pacing algorithm.
    Includes TCPA compliance status and recommended dial ratio.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, subscription_tier, voice_config FROM subscribers WHERE email = %s",
                    (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        location_id = row['location_id']
        tier = row['subscription_tier'] or 'individual'
        voice_config = row['voice_config'] or {}

        # Get recent call stats for predictive calculations
        cur.execute("""
            SELECT
                COUNT(*) as total_calls,
                COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) as connected_calls,
                COUNT(*) FILTER (WHERE status IN ('no-answer', 'busy', 'failed', 'canceled')) as failed_calls,
                AVG(duration) FILTER (WHERE status = 'completed' AND duration > 0) as avg_duration,
                AVG(duration) FILTER (WHERE status = 'completed' AND duration > 30) as avg_talk_time
            FROM call_history
            WHERE location_id = %s
            AND created_at > NOW() - INTERVAL '7 days'
        """, (location_id,))
        stats_row = cur.fetchone()
        cur.close()

        total = stats_row['total_calls'] or 0
        connected = stats_row['connected_calls'] or 0
        failed = stats_row['failed_calls'] or 0
        avg_duration = float(stats_row['avg_duration'] or 0)
        avg_talk_time = float(stats_row['avg_talk_time'] or 0)

        connect_rate = (connected / total * 100) if total > 0 else 0
        wrap_up_time = int(voice_config.get('wrap_up_time', 15))

        # Use Erlang-C for predictive_dialer tier, simple formula for pro_dialer
        if tier == 'predictive_dialer':
            # Bootstrap TCPA tracker from DB if this location has no in-memory data yet
            if tcpa_tracker.get_abandon_rate(location_id) == 0.0:
                try:
                    tcpa_tracker.load_from_db(location_id)
                except Exception as e:
                    logger.warning(f"TCPA bootstrap failed for {location_id}: {e}")
            current_abandon = tcpa_tracker.get_abandon_rate(location_id)
            available = len(agent_state_manager.get_available_agents(location_id)) or 1

            erlang_result = calculate_optimal_dial_ratio(
                available_agents=available,
                avg_handle_time_sec=avg_duration or 180,
                avg_ring_time_sec=15,
                answer_rate_pct=connect_rate or 25,
                current_abandon_rate_pct=current_abandon,
                target_abandon_rate_pct=float(voice_config.get('max_abandon_rate_pct', 3.0)),
                wrap_up_time_sec=wrap_up_time,
                max_ratio=4.0,
            )

            return jsonify({
                "total_calls_7d": total,
                "connected_calls_7d": connected,
                "failed_calls_7d": failed,
                "connect_rate": round(connect_rate, 1),
                "avg_duration_sec": round(avg_duration, 1),
                "avg_talk_time_sec": round(avg_talk_time, 1),
                "recommended_lines": erlang_result["recommended_lines"],
                "dial_ratio": erlang_result["dial_ratio"],
                "erlang_c_probability": erlang_result["erlang_c_probability"],
                "predicted_abandon_rate": erlang_result["predicted_abandon_rate"],
                "current_abandon_rate": round(current_abandon, 2),
                "throttled": erlang_result["throttled"],
                "algorithm": "erlang_c",
                "available_agents": erlang_result["inputs"]["available_agents"],
            })
        else:
            # Simple formula for pro_dialer tier
            if connect_rate > 0:
                raw_ratio = min(4.0, max(1.0, 100 / connect_rate))
            else:
                raw_ratio = 3.0
            recommended_lines = min(4, max(1, round(raw_ratio)))

            return jsonify({
                "total_calls_7d": total,
                "connected_calls_7d": connected,
                "failed_calls_7d": failed,
                "connect_rate": round(connect_rate, 1),
                "avg_duration_sec": round(avg_duration, 1),
                "avg_talk_time_sec": round(avg_talk_time, 1),
                "recommended_lines": recommended_lines,
                "dial_ratio": round(raw_ratio, 2),
                "algorithm": "simple",
            })
    except Exception as e:
        logger.error(f"Predictive stats error: {e}")
        return jsonify({"error": "Failed to calculate stats"}), 500
    finally:
        return_db_connection(conn)


# ═══ Predictive Dialer Tier Routes ═══════════════════════════════════════════

@dialer_bp.route('/voice/compliance', methods=['GET'])
@login_required
def compliance_dashboard():
    """Get compliance metrics for the TCPA compliance dashboard."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, subscription_tier FROM subscribers WHERE email = %s",
                    (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404

        tier = row['subscription_tier'] or 'individual'
        if tier != 'predictive_dialer':
            return jsonify({"error": "Compliance dashboard requires Predictive Dialer tier",
                            "upgrade_required": True}), 403

        location_id = row['location_id']
    except Exception as e:
        logger.error(f"Compliance dashboard DB error: {e}")
        return jsonify({"error": "Database error"}), 500
    finally:
        return_db_connection(conn)

    # Bootstrap TCPA tracker from DB if not yet loaded for this location
    if tcpa_tracker.get_abandon_rate(location_id) == 0.0:
        try:
            tcpa_tracker.load_from_db(location_id)
        except Exception as e:
            logger.warning(f"TCPA bootstrap failed for {location_id}: {e}")

    try:
        period = int(request.args.get('period', 30))
    except (ValueError, TypeError):
        period = 30
    period = max(1, min(90, period))

    metrics = get_compliance_metrics(location_id, period_days=period)
    return jsonify(metrics)


@dialer_bp.route('/voice/agent-state', methods=['GET', 'POST'])
@login_required
def agent_state():
    """Get or set agent state for the predictive dialer state machine."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, subscription_tier FROM subscribers WHERE email = %s",
                    (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        location_id = row['location_id']
        tier = row['subscription_tier'] or 'individual'
    except Exception as e:
        logger.error(f"Agent state DB error: {e}")
        return jsonify({"error": "Database error"}), 500
    finally:
        return_db_connection(conn)

    if tier != 'predictive_dialer':
        return jsonify({"error": "Agent state requires Predictive Dialer tier",
                        "upgrade_required": True}), 403

    if request.method == 'GET':
        state = agent_state_manager.get_agent_state(location_id, current_user.email)
        all_agents = agent_state_manager.get_all_agents(location_id)
        available = agent_state_manager.get_available_agents(location_id)
        predicted = agent_state_manager.get_predicted_available(location_id, horizon_sec=30)
        return jsonify({
            "my_state": state,
            "all_agents": all_agents,
            "available_count": len(available),
            "predicted_available_30s": predicted,
        })

    # POST: set state (user-settable states only; ON_CALL and WRAP_UP are server-controlled)
    data = request.json or {}
    new_state = data.get('state', '')
    reason = data.get('reason')

    valid = {AgentState.READY, AgentState.NOT_READY, AgentState.BREAK,
             AgentState.EXTENDED_AWAY, AgentState.LOGGED_OUT}
    if new_state not in valid:
        return jsonify({"error": f"Invalid state: {new_state}. Valid: {sorted(valid)}"}), 400

    agent_state_manager.set_state(location_id, current_user.email, new_state, reason=reason)
    return jsonify({"success": True, "state": new_state})


@dialer_bp.route('/voice/callback-queue', methods=['GET', 'POST', 'DELETE'])
@login_required
def callback_queue_route():
    """Manage the callback/re-dial queue."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, subscription_tier FROM subscribers WHERE email = %s",
                    (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        location_id = row['location_id']
        tier = row['subscription_tier'] or 'individual'
    except Exception as e:
        logger.error(f"Callback queue DB error: {e}")
        return jsonify({"error": "Database error"}), 500
    finally:
        return_db_connection(conn)

    if tier != 'predictive_dialer':
        return jsonify({"error": "Callback queue requires Predictive Dialer tier",
                        "upgrade_required": True}), 403

    if request.method == 'GET':
        queue = callback_queue.get_queue(location_id)
        due = callback_queue.get_due_callbacks(location_id)
        return jsonify({
            "queue": queue,
            "queue_size": len(queue),
            "due_count": len(due),
            "due": due,
        })

    if request.method == 'POST':
        data = request.json or {}
        contact_id = data.get('contact_id', '')
        phone = data.get('phone', '')
        name = data.get('name', 'Unknown')
        try:
            delay_minutes = int(data.get('delay_minutes', 30))
        except (ValueError, TypeError):
            delay_minutes = 30
        delay_minutes = max(1, min(10080, delay_minutes))  # 1 min to 7 days
        reason = data.get('reason', 'no_answer')

        if not phone:
            return jsonify({"error": "Phone number required"}), 400

        scheduled_at = time.time() + (delay_minutes * 60)
        added = callback_queue.schedule_callback(
            location_id, contact_id, phone, name, scheduled_at, reason
        )
        return jsonify({"success": added, "scheduled_in_minutes": delay_minutes})

    if request.method == 'DELETE':
        data = request.json or {}
        phone = data.get('phone', '')
        if data.get('clear_all'):
            count = callback_queue.clear_queue(location_id)
            return jsonify({"success": True, "cleared": count})
        if phone:
            cancelled = callback_queue.cancel_callback(location_id, phone)
            return jsonify({"success": cancelled})
        return jsonify({"error": "Provide 'phone' or 'clear_all'"}), 400


@dialer_bp.route('/voice/recording-consent', methods=['GET'])
@login_required
def recording_consent_check():
    """Check recording consent requirements for a phone number."""
    phone = request.args.get('phone', '').strip()
    if not phone:
        return jsonify({"error": "Phone number required"}), 400

    state = area_code_to_state(phone)
    two_party = is_two_party_consent_state(phone)
    tz = area_code_to_timezone(phone)

    return jsonify({
        "phone": phone,
        "state": state,
        "timezone": tz,
        "two_party_consent": two_party,
        "consent_required": two_party,
        "disclosure_text": (
            "This call may be recorded for quality and training purposes."
            if two_party else None
        ),
    })


@dialer_bp.route('/voice/recording-consent/batch', methods=['POST'])
@login_required
def recording_consent_batch():
    """Batch check recording consent for multiple phone numbers."""
    data = request.json or {}
    phones = data.get('phones', [])
    if not phones or not isinstance(phones, list):
        return jsonify({"error": "No phone numbers provided (expected array)"}), 400

    results = {}
    for phone in phones[:300]:
        state = area_code_to_state(phone)
        results[phone] = {
            "state": state,
            "two_party_consent": is_two_party_consent_state(phone),
        }

    two_party_count = sum(1 for r in results.values() if r["two_party_consent"])
    return jsonify({
        "results": results,
        "total": len(results),
        "two_party_count": two_party_count,
    })
