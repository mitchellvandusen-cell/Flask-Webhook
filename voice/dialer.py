import json
import os
import logging
import threading
import time
import asyncio
import re

import httpx
import requests as http_requests
from flask import Blueprint, request, Response, jsonify
from flask_login import login_required, current_user

import twilio_provisioning
from db import get_db_connection, return_db_connection, log_webhook_event
from ghl_api import get_valid_token
from number_health import select_outbound_number
from voice.audio import XAI_API_KEY, VOICE_OPTIONS, DEFAULT_VOICE, _generate_voice_preview, _pcm16_to_wav
from voice.call_state import active_calls, transfer_requests, _twilio_hangup
from voice.helpers import _get_subscriber_by_location, _get_current_subscriber_voice
from voice.call_history_helpers import save_call_to_history, update_call_history_status
from extensions import ADMIN_EMAILS

logger = logging.getLogger("voice_bridge.dialer")

dialer_bp = Blueprint('voice_dialer', __name__)


def _check_multi_line_access(subscriber):
    """Check if subscriber has multi-line dialer access."""
    tier = subscriber.get('subscription_tier', 'individual')
    return tier == 'pro_dialer'


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

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
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
        result = get_cached_contacts(location_id, query)
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
        result = get_cached_contacts(location_id, query)
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


@dialer_bp.route('/voice/dial', methods=['POST'])
@login_required
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
    dial_attempt  = int(data.get('dial_attempt', 1))

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

    # Enforce max dial attempts server-side
    max_attempts = int(voice_config.get('dial_attempts', 2))
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

    if dial_mode == 'ai' and not voice_config.get('enabled'):
        return jsonify({"error": "Voice AI is not enabled. Enable it in the Voice tab."}), 400

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

        ring_timeout = voice_config.get('ring_timeout', 45)
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
        }

        save_call_to_history(
            location_id=location_id,
            call_sid=call_sid,
            phone=phone,
            contact_id=contact_id,
            contact_name=first_name,
            direction='outbound',
            status='initiated'
        )

        logger.info(f"Dialer call [{dial_mode}]: {from_number} -> {phone} ({first_name}) attempt={dial_attempt} sid={call_sid}")
        return jsonify({"status": "calling", "call_sid": call_sid, "dial_mode": dial_mode})

    except Exception as e:
        logger.error(f"Dialer call failed: {e}")
        return jsonify({"error": str(e)}), 500


@dialer_bp.route('/voice/multi-dial', methods=['POST'])
@login_required
def multi_dial():
    """
    Multi-line dialer: initiate up to N concurrent calls.
    Requires 'pro_dialer' subscription tier.
    Returns list of {contact_id, call_sid, status, error} per line.
    """
    data = request.json or {}
    contacts = data.get('contacts', [])  # [{contact_id, phone, first_name}]
    dial_mode = data.get('dial_mode', 'ai')
    max_lines = int(data.get('max_lines', 3))

    if not contacts:
        return jsonify({"error": "No contacts provided"}), 400
    if max_lines < 1 or max_lines > 4:
        return jsonify({"error": "max_lines must be 1-4"}), 400

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
    if tier != 'pro_dialer' and not is_admin:
        return jsonify({"error": "Multi-line dialer requires Pro Dialer subscription", "upgrade_required": True}), 403

    location_id = subscriber.get('location_id', '')
    voice_config = subscriber.get('voice_config') or {}

    if dial_mode == 'ai' and not voice_config.get('enabled'):
        return jsonify({"error": "Voice AI is not enabled"}), 400

    sub_sid = voice_config.get('twilio_sub_account_sid', '')
    from_number = voice_config.get('twilio_phone_number', '')

    if not sub_sid or not from_number:
        return jsonify({"error": "Voice service not fully provisioned"}), 400

    # Enforce max concurrent lines already active for this location
    active_for_location = sum(
        1 for sid, info in active_calls.items()
        if info.get('_location_id') == location_id
        and info.get('status') not in ('completed', 'busy', 'no-answer', 'failed', 'canceled')
    )
    available_lines = max(0, max_lines - active_for_location)
    if available_lines == 0:
        return jsonify({"error": f"All {max_lines} lines are busy", "active_lines": active_for_location}), 429

    # Limit contacts to available lines
    contacts_to_dial = contacts[:available_lines]
    results = []

    for contact in contacts_to_dial:
        c_phone = contact.get('phone', '')
        c_name = contact.get('first_name', 'there')
        c_id = contact.get('contact_id', '')
        c_attempt = int(contact.get('dial_attempt', 1))

        if not c_phone:
            results.append({"contact_id": c_id, "call_sid": None, "status": "error", "error": "No phone number"})
            continue

        # DnD check
        if c_id and location_id:
            _dnd_conn = get_db_connection()
            if _dnd_conn:
                try:
                    _dnd_cur = _dnd_conn.cursor()
                    _dnd_cur.execute(
                        "SELECT dnd FROM contact_cache WHERE location_id = %s AND contact_id = %s",
                        (location_id, c_id)
                    )
                    _dnd_row = _dnd_cur.fetchone()
                    _dnd_cur.close()
                    if _dnd_row and _dnd_row.get("dnd"):
                        results.append({"contact_id": c_id, "call_sid": None, "status": "skipped", "error": "Do Not Contact"})
                        continue
                except Exception:
                    pass
                finally:
                    return_db_connection(_dnd_conn)

        # Max attempts guard
        max_attempts = int(voice_config.get('dial_attempts', 2))
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
            ring_timeout = voice_config.get('ring_timeout', 45)
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
            }

            save_call_to_history(
                location_id=location_id,
                call_sid=call_sid,
                phone=c_phone,
                contact_id=c_id,
                contact_name=c_name,
                direction='outbound',
                status='initiated'
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
@login_required
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

    is_admin = current_user.email.lower() in [e.lower() for e in ADMIN_EMAILS]
    max_lines = 4 if (tier == 'pro_dialer' or is_admin) else 1

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
            })

    return jsonify({
        "lines": lines,
        "active_count": len(lines),
        "max_lines": max_lines,
        "tier": tier
    })


@dialer_bp.route('/voice/multi-hangup', methods=['POST'])
@login_required
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
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        sub_sid = (row['voice_config'] or {}).get('twilio_sub_account_sid', '')
    finally:
        return_db_connection(conn)

    if not sub_sid:
        return jsonify({"error": "Voice not provisioned"}), 400

    results = []
    for sid in call_sids:
        try:
            success = _twilio_hangup(sid, sub_sid)
            if sid in active_calls:
                active_calls[sid]['status'] = 'completed'
            transfer_requests.pop(sid, None)
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
@login_required
def multi_call_status():
    """Poll status of multiple calls at once (reduces HTTP overhead for multi-line)."""
    data = request.json or {}
    call_sids = data.get('call_sids', [])
    if not call_sids:
        return jsonify({"error": "No call_sids provided"}), 400

    statuses = {}
    for sid in call_sids:
        if sid in active_calls:
            info = active_calls[sid]
            # Terminal state cleanup (same logic as single poll)
            if info["status"] in ("completed", "busy", "no-answer", "failed", "canceled", "transferred"):
                poll_count = info.get('_terminal_polls', 0) + 1
                info['_terminal_polls'] = poll_count
                if poll_count >= 20:
                    status_copy = dict(info)
                    del active_calls[sid]
                    statuses[sid] = status_copy
                    continue
            statuses[sid] = dict(info)
        else:
            statuses[sid] = {"status": "unknown"}

    return jsonify({"statuses": statuses})


@dialer_bp.route('/voice/predictive-stats', methods=['GET'])
@login_required
def predictive_stats():
    """
    Return predictive dialing statistics for the current user.
    Used to calculate optimal dial ratio and pacing.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Account not found"}), 404
        location_id = row['location_id']

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

        # Calculate connect rate and recommended dial ratio
        connect_rate = (connected / total * 100) if total > 0 else 0
        # Predictive ratio: inverse of connect rate, capped 1.0-4.0
        # Lower connect rate = dial more lines simultaneously
        if connect_rate > 0:
            raw_ratio = min(4.0, max(1.0, 100 / connect_rate))
        else:
            raw_ratio = 3.0  # Default when no data

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
        })
    except Exception as e:
        logger.error(f"Predictive stats error: {e}")
        return jsonify({"error": "Failed to calculate stats"}), 500
    finally:
        return_db_connection(conn)
