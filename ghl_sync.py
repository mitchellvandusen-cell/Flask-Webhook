# ghl_sync.py - GHL Data Sync Engine (Enterprise-Grade)
# Background sync engine that pulls GHL data into local Postgres for instant access.
# Runs on RQ workers, triggered by cron every 5-10 minutes.

import os
import logging
import time as _time
import json
import requests
from datetime import datetime, timezone, timedelta

from db import get_db_connection, return_db_connection
from ghl_api import get_valid_token, GHL_HEADERS
from token_encryption import decrypt_token

logger = logging.getLogger(__name__)

GHL_BASE = "https://services.leadconnectorhq.com"

# Rate limit safety: pause between paginated API calls
_PAGE_DELAY = 0.3  # seconds between pages
_RETRY_DELAYS = [2, 4, 8]  # exponential backoff on failure


# ─── Retry Wrapper ────────────────────────────────────────────────────────────

def _api_get(url, headers, params=None, timeout=15):
    """GHL API GET with retry + exponential backoff.
    Returns (response_json, None) on success or (None, error_reason) on failure."""
    last_err = None
    for attempt, delay in enumerate([0] + _RETRY_DELAYS):
        if delay:
            _time.sleep(delay)
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 10))
                logger.warning(f"[GHL_SYNC] Rate limited, waiting {retry_after}s")
                _time.sleep(retry_after)
                continue

            if resp.status_code in (401, 403):
                return None, "auth_error"

            resp.raise_for_status()
            return resp.json(), None

        except requests.HTTPError as e:
            status = e.response.status_code if e.response else 0
            last_err = f"http_{status}"
            logger.warning(f"[GHL_SYNC] HTTP {status} attempt {attempt+1}: {url}")
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = "network_error"
            logger.warning(f"[GHL_SYNC] Network error attempt {attempt+1}: {e}")
        except Exception as e:
            last_err = "unexpected"
            logger.error(f"[GHL_SYNC] Unexpected error: {e}", exc_info=True)
            break

    return None, last_err


def _get_headers(access_token):
    """Build GHL API headers with auth."""
    return {**GHL_HEADERS, "Authorization": f"Bearer {access_token}"}


# ─── Sync State Management ───────────────────────────────────────────────────

def _get_sync_state(conn, location_id, resource_type):
    """Get sync state for a location/resource combo."""
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT last_sync_at, last_cursor, sync_status, total_synced
            FROM ghl_sync_state
            WHERE location_id = %s AND resource_type = %s
        """, (location_id, resource_type))
        row = cur.fetchone()
        cur.close()
        return row
    except Exception:
        return None


def _update_sync_state(conn, location_id, resource_type, status,
                       cursor=None, total=None, error=None):
    """Upsert sync state."""
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ghl_sync_state
                (location_id, resource_type, sync_status, last_cursor,
                 total_synced, error_message, last_sync_at, created_at)
            VALUES (%s, %s, %s, %s, COALESCE(%s, 0), %s, NOW(), NOW())
            ON CONFLICT (location_id, resource_type) DO UPDATE SET
                sync_status = EXCLUDED.sync_status,
                last_cursor = COALESCE(EXCLUDED.last_cursor, ghl_sync_state.last_cursor),
                total_synced = COALESCE(EXCLUDED.total_synced, ghl_sync_state.total_synced),
                error_message = EXCLUDED.error_message,
                last_sync_at = NOW()
        """, (location_id, resource_type, status, cursor, total, error))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"[GHL_SYNC] Failed to update sync state: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


# ─── Conversation Sync ────────────────────────────────────────────────────────

def sync_ghl_conversations(location_id, access_token=None):
    """
    Sync ALL conversation messages from GHL into ghl_conversations table.
    Incremental: only fetches messages newer than last_sync_at.

    Flow:
    1. List contacts for location (paginated)
    2. For each contact, search for conversation
    3. Fetch messages from conversation (paginated)
    4. Upsert into ghl_conversations with message type classification
    """
    if not access_token:
        access_token = get_valid_token(location_id)
    if not access_token or access_token == 'DEMO':
        logger.warning(f"[GHL_SYNC] No valid token for {location_id}, skipping conversation sync")
        return {"synced": 0, "error": "no_token"}

    conn = get_db_connection()
    if not conn:
        return {"synced": 0, "error": "no_db"}

    headers = _get_headers(access_token)
    total_synced = 0

    try:
        # Get last sync time for incremental sync
        state = _get_sync_state(conn, location_id, 'conversations')
        last_sync = None
        if state and state.get('last_sync_at'):
            last_sync = state['last_sync_at']

        _update_sync_state(conn, location_id, 'conversations', 'running')

        # Step 1: Get contacts (paginated) — use local contact_cache if available
        contact_ids = _get_contact_ids_for_sync(conn, location_id, headers)

        if not contact_ids:
            logger.info(f"[GHL_SYNC] {location_id} | No contacts to sync")
            _update_sync_state(conn, location_id, 'conversations', 'idle', total=0)
            return {"synced": 0}

        logger.info(f"[GHL_SYNC] {location_id} | Syncing conversations for {len(contact_ids)} contacts")

        # Step 2: For each contact, fetch conversation messages
        for i, contact_id in enumerate(contact_ids):
            try:
                count = _sync_contact_conversations(
                    conn, location_id, contact_id, headers, last_sync
                )
                total_synced += count

                # Rate limit safety
                if (i + 1) % 10 == 0:
                    _time.sleep(_PAGE_DELAY)
                    # Update progress
                    _update_sync_state(conn, location_id, 'conversations', 'running',
                                       total=total_synced)

            except Exception as e:
                logger.warning(f"[GHL_SYNC] {location_id} | Error syncing contact {contact_id}: {e}")
                continue

        _update_sync_state(conn, location_id, 'conversations', 'idle', total=total_synced)
        logger.info(f"[GHL_SYNC] {location_id} | conversations | synced {total_synced} records")
        return {"synced": total_synced}

    except Exception as e:
        logger.error(f"[GHL_SYNC] {location_id} | conversations | FAILED: {e}", exc_info=True)
        _update_sync_state(conn, location_id, 'conversations', 'failed', error=str(e)[:500])
        return {"synced": total_synced, "error": str(e)}
    finally:
        return_db_connection(conn)


def _get_contact_ids_for_sync(conn, location_id, headers):
    """Get contact IDs to sync — prefer local cache, fallback to GHL API."""
    # Try local contact_cache first (fast)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT contact_id FROM contact_cache
            WHERE location_id = %s
            ORDER BY synced_at DESC
            LIMIT 5000
        """, (location_id,))
        rows = cur.fetchall()
        cur.close()
        if rows:
            return [r['contact_id'] for r in rows]
    except Exception:
        pass

    # Fallback: fetch from GHL API
    contact_ids = []
    url = f"{GHL_BASE}/contacts/"
    params = {"locationId": location_id, "limit": 100}
    pages_fetched = 0
    max_pages = 50  # cap at 5000 contacts

    while pages_fetched < max_pages:
        data, err = _api_get(url, headers, params=params)
        if err or not data:
            break

        contacts = data.get("contacts", [])
        for c in contacts:
            cid = c.get("id")
            if cid:
                contact_ids.append(cid)

        # Check for next page
        meta = data.get("meta", {})
        next_page_url = meta.get("nextPageUrl") or meta.get("nextPage")
        if not next_page_url or not contacts:
            break

        # GHL returns full URL for next page or uses startAfterId
        start_after = meta.get("startAfterId") or meta.get("nextPageStartAfterId")
        if start_after:
            params["startAfterId"] = start_after
        elif isinstance(next_page_url, str) and next_page_url.startswith("http"):
            url = next_page_url
            params = {}
        else:
            break

        pages_fetched += 1
        _time.sleep(_PAGE_DELAY)

    return contact_ids


def _classify_message_type(msg):
    """Classify a GHL message into type and source.
    Returns (message_type, source)."""
    msg_type = (msg.get("type") or msg.get("messageType") or "").lower()
    content_type = (msg.get("contentType") or "").lower()
    source_field = (msg.get("source") or "").lower()

    # GHL message types:
    # TYPE_SMS = 1, TYPE_EMAIL = 2, TYPE_CALL = 3, TYPE_VOICEMAIL = 4
    # TYPE_FACEBOOK = 5, TYPE_GMB = 7, TYPE_LIVE_CHAT = 8, TYPE_INSTAGRAM = 9
    # TYPE_WHATSAPP = 10, TYPE_CUSTOM_SMS = 11, TYPE_CUSTOM_EMAIL = 12

    # Numeric type mapping
    type_num = None
    try:
        type_num = int(msg_type) if msg_type.isdigit() else None
    except (ValueError, AttributeError):
        pass

    if type_num == 3 or msg_type == 'call' or content_type == 'call':
        # Determine call source
        body = (msg.get("body") or "").lower()
        if "wavv" in body or "wavv" in source_field:
            return "call", "wavv"
        elif "dialer" in source_field or "insurancegrokbot" in source_field:
            return "call", "dialer"
        return "call", "ghl_native"

    elif type_num == 4 or msg_type == 'voicemail' or content_type == 'voicemail':
        return "voicemail", "ghl_native"

    elif type_num == 2 or msg_type == 'email' or content_type == 'email':
        return "email", "ghl_native"

    elif type_num in (1, 11) or msg_type in ('sms', 'custom_sms') or content_type == 'sms':
        return "sms", "ghl_native"

    elif type_num in (5, 7, 8, 9, 10):
        return "social", "ghl_native"

    # Default: SMS
    return "sms", "ghl_native"


def _sync_contact_conversations(conn, location_id, contact_id, headers, last_sync=None):
    """Sync messages for a single contact's conversation. Returns count of new messages."""
    # Step 1: Find conversation ID
    search_url = f"{GHL_BASE}/conversations/search"
    data, err = _api_get(search_url, headers,
                         params={"locationId": location_id, "contactId": contact_id})
    if err or not data:
        return 0

    convos = data.get("conversations", [])
    if not convos:
        return 0

    convo_id = convos[0].get("id")
    if not convo_id:
        return 0

    contact_name = convos[0].get("contactName") or convos[0].get("fullName") or ""
    contact_phone = convos[0].get("phone") or ""

    # Step 2: Fetch messages (paginated)
    msg_url = f"{GHL_BASE}/conversations/{convo_id}/messages"
    messages_to_insert = []
    page = 0
    max_pages = 10  # cap at ~1000 messages per contact

    while page < max_pages:
        params = {"limit": 100}
        data, err = _api_get(msg_url, headers, params=params)
        if err or not data:
            break

        messages_payload = data.get("messages", [])
        if isinstance(messages_payload, dict):
            raw_messages = messages_payload.get("messages", [])
            next_page = messages_payload.get("nextPage")
            last_msg_id = messages_payload.get("lastMessageId")
        elif isinstance(messages_payload, list):
            raw_messages = messages_payload
            next_page = None
            last_msg_id = None
        else:
            break

        if not raw_messages:
            break

        for m in raw_messages:
            if not isinstance(m, dict):
                continue

            ghl_msg_id = m.get("id") or m.get("messageId")
            if not ghl_msg_id:
                continue

            date_added = m.get("dateAdded") or m.get("createdAt") or m.get("created_at")

            # Incremental: skip messages older than last sync
            if last_sync and date_added:
                try:
                    msg_dt = datetime.fromisoformat(date_added.replace('Z', '+00:00'))
                    if msg_dt.tzinfo:
                        msg_dt = msg_dt.replace(tzinfo=None)
                    if isinstance(last_sync, datetime) and msg_dt < last_sync:
                        continue
                except (ValueError, TypeError):
                    pass

            direction = m.get("direction", "inbound")
            body = m.get("body") or m.get("text") or ""
            msg_type, source = _classify_message_type(m)

            messages_to_insert.append((
                location_id, contact_id, contact_name, contact_phone,
                convo_id, msg_type, direction, body[:5000], source,
                ghl_msg_id, date_added
            ))

        # Pagination
        if not next_page and not last_msg_id:
            break
        if last_msg_id:
            msg_url = f"{GHL_BASE}/conversations/{convo_id}/messages?lastMessageId={last_msg_id}"
        page += 1
        _time.sleep(_PAGE_DELAY)

    # Bulk upsert
    if messages_to_insert:
        try:
            cur = conn.cursor()
            from psycopg2.extras import execute_values
            execute_values(cur, """
                INSERT INTO ghl_conversations
                    (location_id, contact_id, contact_name, contact_phone,
                     conversation_id, message_type, direction, body, source,
                     ghl_message_id, date_added, synced_at)
                VALUES %s
                ON CONFLICT (ghl_message_id) DO UPDATE SET
                    body = EXCLUDED.body,
                    synced_at = NOW()
            """, [(
                *row, datetime.utcnow()
            ) for row in messages_to_insert],
            template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
            conn.commit()
            cur.close()
        except Exception as e:
            conn.rollback()
            logger.error(f"[GHL_SYNC] Bulk insert failed for {contact_id}: {e}")
            return 0

    return len(messages_to_insert)


# ─── Opportunity Sync ─────────────────────────────────────────────────────────

def sync_ghl_opportunities(location_id, access_token=None):
    """
    Sync ALL pipeline opportunities from GHL into ghl_opportunities table.
    Pulls pipeline stages, deal values, and assignment info.
    """
    if not access_token:
        access_token = get_valid_token(location_id)
    if not access_token or access_token == 'DEMO':
        return {"synced": 0, "error": "no_token"}

    conn = get_db_connection()
    if not conn:
        return {"synced": 0, "error": "no_db"}

    headers = _get_headers(access_token)
    total_synced = 0

    try:
        _update_sync_state(conn, location_id, 'opportunities', 'running')

        # Step 1: Get pipelines
        pipelines = _fetch_pipelines(location_id, headers)
        if not pipelines:
            _update_sync_state(conn, location_id, 'opportunities', 'idle', total=0)
            return {"synced": 0}

        # Build stage name lookup
        stage_map = {}
        for p in pipelines:
            for stage in p.get("stages", []):
                stage_map[stage.get("id")] = {
                    "name": stage.get("name", ""),
                    "pipeline_id": p.get("id"),
                    "pipeline_name": p.get("name", "")
                }

        # Step 2: Fetch opportunities per pipeline (paginated)
        for pipeline in pipelines:
            pid = pipeline.get("id")
            pname = pipeline.get("name", "")
            if not pid:
                continue

            url = f"{GHL_BASE}/opportunities/search"
            params = {
                "location_id": location_id,
                "pipeline_id": pid,
                "limit": 100
            }
            page = 0
            max_pages = 20

            while page < max_pages:
                data, err = _api_get(url, headers, params=params)
                if err or not data:
                    break

                opps = data.get("opportunities", [])
                if not opps:
                    break

                rows = []
                for opp in opps:
                    opp_id = opp.get("id")
                    if not opp_id:
                        continue

                    stage_id = opp.get("pipelineStageId") or opp.get("stageId") or ""
                    stage_info = stage_map.get(stage_id, {})

                    rows.append((
                        location_id,
                        opp.get("contactId") or opp.get("contact", {}).get("id") or "",
                        pid,
                        pname,
                        stage_id,
                        stage_info.get("name", ""),
                        opp.get("status", "open"),
                        opp.get("monetaryValue") or 0,
                        opp.get("source") or "",
                        opp.get("assignedTo") or "",
                        opp_id,
                        opp.get("createdAt") or opp.get("dateAdded"),
                        opp.get("updatedAt") or opp.get("lastUpdatedAt"),
                    ))

                # Bulk upsert
                if rows:
                    try:
                        cur = conn.cursor()
                        from psycopg2.extras import execute_values
                        execute_values(cur, """
                            INSERT INTO ghl_opportunities
                                (location_id, contact_id, pipeline_id, pipeline_name,
                                 stage_id, stage_name, status, monetary_value, source,
                                 assigned_to, ghl_opportunity_id, created_at_ghl,
                                 updated_at_ghl, synced_at)
                            VALUES %s
                            ON CONFLICT (ghl_opportunity_id) DO UPDATE SET
                                stage_id = EXCLUDED.stage_id,
                                stage_name = EXCLUDED.stage_name,
                                status = EXCLUDED.status,
                                monetary_value = EXCLUDED.monetary_value,
                                assigned_to = EXCLUDED.assigned_to,
                                updated_at_ghl = EXCLUDED.updated_at_ghl,
                                synced_at = NOW()
                        """, [(
                            *row, datetime.utcnow()
                        ) for row in rows],
                        template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
                        conn.commit()
                        cur.close()
                        total_synced += len(rows)
                    except Exception as e:
                        conn.rollback()
                        logger.error(f"[GHL_SYNC] Opportunity insert failed: {e}")

                # Pagination
                meta = data.get("meta", {})
                next_page = meta.get("nextPage") or meta.get("nextPageUrl")
                start_after = meta.get("startAfterId")
                if not next_page and not start_after:
                    break
                if start_after:
                    params["startAfterId"] = start_after
                page += 1
                _time.sleep(_PAGE_DELAY)

        _update_sync_state(conn, location_id, 'opportunities', 'idle', total=total_synced)
        logger.info(f"[GHL_SYNC] {location_id} | opportunities | synced {total_synced} records")
        return {"synced": total_synced}

    except Exception as e:
        logger.error(f"[GHL_SYNC] {location_id} | opportunities | FAILED: {e}", exc_info=True)
        _update_sync_state(conn, location_id, 'opportunities', 'failed', error=str(e)[:500])
        return {"synced": total_synced, "error": str(e)}
    finally:
        return_db_connection(conn)


def _fetch_pipelines(location_id, headers):
    """Fetch all pipelines for a location."""
    url = f"{GHL_BASE}/opportunities/pipelines"
    data, err = _api_get(url, headers, params={"locationId": location_id})
    if err or not data:
        return []
    return data.get("pipelines", [])


# ─── Phone Number Sync ────────────────────────────────────────────────────────

def sync_ghl_phone_numbers(location_id, access_token=None):
    """
    Fetch phone numbers from GHL location (via twilioaccount.read scope).
    Stores in subscribers.voice_config['ghl_numbers'] for display alongside
    IGB-purchased Twilio numbers.
    """
    if not access_token:
        access_token = get_valid_token(location_id)
    if not access_token or access_token == 'DEMO':
        return {"numbers": [], "error": "no_token"}

    headers = _get_headers(access_token)
    numbers = []

    # Try the GHL phone numbers endpoint
    # GHL stores phone numbers at the location level
    endpoints = [
        f"{GHL_BASE}/locations/{location_id}/phone-numbers",
        f"{GHL_BASE}/phone-numbers/?locationId={location_id}",
    ]

    for url in endpoints:
        data, err = _api_get(url, headers)
        if err:
            continue
        if data:
            raw_numbers = data.get("phoneNumbers", data.get("numbers", []))
            if isinstance(raw_numbers, list):
                for n in raw_numbers:
                    if not isinstance(n, dict):
                        continue
                    numbers.append({
                        "number": n.get("phoneNumber") or n.get("number") or n.get("phone", ""),
                        "ghl_id": n.get("id", ""),
                        "name": n.get("name") or n.get("friendlyName", ""),
                        "capabilities": {
                            "sms": n.get("smsEnabled", True),
                            "voice": n.get("voiceEnabled", True),
                            "mms": n.get("mmsEnabled", False),
                        },
                        "source": "ghl",
                    })
                if numbers:
                    break

    # Store in voice_config['ghl_numbers']
    if numbers:
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE subscribers
                    SET voice_config = voice_config || %s::jsonb,
                        updated_at = NOW()
                    WHERE location_id = %s
                """, (json.dumps({"ghl_numbers": numbers}), location_id))

                # Also update agency_billing if exists
                cur.execute("""
                    UPDATE agency_billing
                    SET voice_config = voice_config || %s::jsonb
                    WHERE location_id = %s
                """, (json.dumps({"ghl_numbers": numbers}), location_id))

                conn.commit()
                cur.close()
                logger.info(f"[GHL_SYNC] {location_id} | phone_numbers | stored {len(numbers)} numbers")
            except Exception as e:
                conn.rollback()
                logger.error(f"[GHL_SYNC] Failed to store GHL numbers: {e}")
            finally:
                return_db_connection(conn)

    return {"numbers": numbers}


# ─── Location Data Sync ───────────────────────────────────────────────────────

def sync_ghl_location_data(location_id, access_token=None):
    """
    Pull location timezone, business name, address from GHL.
    Auto-populates subscriber timezone and business context.
    """
    if not access_token:
        access_token = get_valid_token(location_id)
    if not access_token or access_token == 'DEMO':
        return {}

    headers = _get_headers(access_token)
    url = f"{GHL_BASE}/locations/{location_id}"
    data, err = _api_get(url, headers)
    if err or not data:
        return {}

    loc = data.get("location", data)
    result = {
        "business_name": loc.get("name") or loc.get("businessName", ""),
        "timezone": loc.get("timezone", ""),
        "address": loc.get("address") or loc.get("street", ""),
        "city": loc.get("city", ""),
        "state": loc.get("state", ""),
        "zip": loc.get("postalCode") or loc.get("zip", ""),
        "country": loc.get("country", "US"),
        "website": loc.get("website", ""),
        "phone": loc.get("phone", ""),
    }

    # Auto-set timezone if found
    if result["timezone"]:
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE subscribers
                    SET timezone = COALESCE(NULLIF(timezone, ''), %s),
                        updated_at = NOW()
                    WHERE location_id = %s
                """, (result["timezone"], location_id))
                conn.commit()
                cur.close()
                logger.info(f"[GHL_SYNC] {location_id} | location | timezone={result['timezone']}")
            except Exception as e:
                conn.rollback()
                logger.error(f"[GHL_SYNC] Failed to update timezone: {e}")
            finally:
                return_db_connection(conn)

    return result


# ─── Users / Agents Sync ─────────────────────────────────────────────────────

def sync_ghl_users(location_id, access_token=None):
    """Sync GHL users/agents for the location. Returns list of user dicts."""
    if not access_token:
        access_token = get_valid_token(location_id)
    if not access_token or access_token == 'DEMO':
        return []

    headers = _get_headers(access_token)
    url = f"{GHL_BASE}/users/search"
    data, err = _api_get(url, headers, params={"locationId": location_id, "limit": 100})
    if err or not data:
        # Fallback endpoint
        data, err = _api_get(f"{GHL_BASE}/users/", headers,
                             params={"locationId": location_id})
        if err or not data:
            return []

    users = data.get("users", [])
    result = []
    for u in users:
        if not isinstance(u, dict):
            continue
        result.append({
            "id": u.get("id", ""),
            "name": u.get("name") or f"{u.get('firstName', '')} {u.get('lastName', '')}".strip(),
            "email": u.get("email", ""),
            "role": u.get("role", ""),
        })

    return result


# ─── Tags Sync ────────────────────────────────────────────────────────────────

def sync_ghl_tags(location_id, access_token=None):
    """Fetch all tag definitions for a location."""
    if not access_token:
        access_token = get_valid_token(location_id)
    if not access_token or access_token == 'DEMO':
        return []

    headers = _get_headers(access_token)
    url = f"{GHL_BASE}/locations/{location_id}/tags"
    data, err = _api_get(url, headers)
    if err or not data:
        return []

    tags = data.get("tags", [])
    return [{"id": t.get("id", ""), "name": t.get("name", "")} for t in tags if isinstance(t, dict)]


# ─── Custom Fields Sync ──────────────────────────────────────────────────────

def sync_ghl_custom_fields(location_id, access_token=None):
    """Fetch custom field definitions for a location."""
    if not access_token:
        access_token = get_valid_token(location_id)
    if not access_token or access_token == 'DEMO':
        return []

    headers = _get_headers(access_token)
    url = f"{GHL_BASE}/locations/{location_id}/customFields"
    data, err = _api_get(url, headers)
    if err or not data:
        return []

    fields = data.get("customFields", [])
    return [{
        "id": f.get("id", ""),
        "name": f.get("name", ""),
        "field_key": f.get("fieldKey", ""),
        "data_type": f.get("dataType", ""),
    } for f in fields if isinstance(f, dict)]


# ─── Master Sync Orchestrator ────────────────────────────────────────────────

def sync_all_for_location(location_id, access_token=None):
    """
    Run all sync operations for a single location.
    Called by cron job for each active subscriber.
    Returns summary dict.
    """
    if not access_token:
        access_token = get_valid_token(location_id)

    results = {}
    start = _time.time()

    # 1. Conversations (largest sync, most valuable)
    results["conversations"] = sync_ghl_conversations(location_id, access_token)

    # 2. Opportunities
    results["opportunities"] = sync_ghl_opportunities(location_id, access_token)

    # 3. Phone numbers (small, fast)
    results["phone_numbers"] = sync_ghl_phone_numbers(location_id, access_token)

    # 4. Location data (small, fast)
    results["location"] = sync_ghl_location_data(location_id, access_token)

    elapsed = round(_time.time() - start, 1)
    logger.info(f"[GHL_SYNC] {location_id} | full sync complete in {elapsed}s | {results}")

    # Log to webhook_logs for dashboard visibility
    _log_sync_event(location_id, results, elapsed)

    return results


def _log_sync_event(location_id, results, elapsed):
    """Log sync completion to webhook_logs for dashboard visibility."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        conv_count = results.get("conversations", {}).get("synced", 0)
        opp_count = results.get("opportunities", {}).get("synced", 0)
        num_count = len(results.get("phone_numbers", {}).get("numbers", []))
        details = (f"Conversations: {conv_count}, Opportunities: {opp_count}, "
                   f"Phone Numbers: {num_count}, Duration: {elapsed}s")
        cur.execute("""
            INSERT INTO webhook_logs
                (location_id, event_type, details, created_at)
            VALUES (%s, 'ghl_sync_complete', %s, NOW())
        """, (location_id, details))
        conn.commit()
        cur.close()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug(f"[GHL_SYNC] Log event failed: {e}")
    finally:
        return_db_connection(conn)


# ─── Cron Entry Point ────────────────────────────────────────────────────────

def run_incremental_sync_all():
    """
    Cron entry point: sync GHL data for ALL active subscribers.
    Designed to run every 5-10 minutes.
    Returns summary stats.
    """
    conn = get_db_connection()
    if not conn:
        return {"error": "no_db", "synced": 0}

    stats = {"total_locations": 0, "synced": 0, "failed": 0, "skipped": 0}

    try:
        cur = conn.cursor()
        # Get all active subscribers with OAuth tokens
        cur.execute("""
            SELECT location_id, access_token, refresh_token, token_expires_at
            FROM subscribers
            WHERE location_id IS NOT NULL
              AND access_token IS NOT NULL
              AND refresh_token IS NOT NULL
              AND stripe_status IN ('active', 'trialing')
            UNION ALL
            SELECT location_id, access_token, refresh_token, token_expires_at
            FROM agency_billing
            WHERE location_id IS NOT NULL
              AND access_token IS NOT NULL
              AND refresh_token IS NOT NULL
              AND stripe_status IN ('active', 'trialing')
        """)
        rows = cur.fetchall()
        cur.close()
    except Exception as e:
        logger.error(f"[GHL_SYNC] Failed to query subscribers: {e}")
        return_db_connection(conn)
        return {"error": str(e)}
    finally:
        return_db_connection(conn)

    stats["total_locations"] = len(rows)
    logger.info(f"[GHL_SYNC] Starting incremental sync for {len(rows)} locations")

    for row in rows:
        loc_id = row.get("location_id")
        if not loc_id:
            stats["skipped"] += 1
            continue

        try:
            # Check if sync is already running (prevent overlap)
            conn2 = get_db_connection()
            if conn2:
                state = _get_sync_state(conn2, loc_id, 'conversations')
                return_db_connection(conn2)
                if state and state.get('sync_status') == 'running':
                    # Check if it's been running too long (stuck)
                    last_sync = state.get('last_sync_at')
                    if last_sync:
                        if isinstance(last_sync, str):
                            last_sync = datetime.fromisoformat(last_sync.replace('Z', '+00:00'))
                        age = datetime.utcnow() - (last_sync.replace(tzinfo=None) if hasattr(last_sync, 'tzinfo') and last_sync.tzinfo else last_sync)
                        if age < timedelta(minutes=30):
                            logger.info(f"[GHL_SYNC] {loc_id} sync already running, skipping")
                            stats["skipped"] += 1
                            continue

            # Get fresh token
            token = get_valid_token(loc_id, row)
            if not token or token == 'DEMO':
                stats["skipped"] += 1
                continue

            result = sync_all_for_location(loc_id, token)
            stats["synced"] += 1

            # Brief pause between locations to be kind to GHL API
            _time.sleep(1)

        except Exception as e:
            logger.error(f"[GHL_SYNC] {loc_id} sync failed: {e}", exc_info=True)
            stats["failed"] += 1

    logger.info(f"[GHL_SYNC] Incremental sync complete: {stats}")
    return stats


# ─── Deep Initial Sync (One-Time Historical Pull) ────────────────────────────

# GHL API rate limit: ~100 requests/minute for most endpoints.
# We pace ourselves just below that and wait when hit.
_DEEP_SYNC_PACE = 0.7  # seconds between API calls (~85 req/min, safely under 100)
_DEEP_SYNC_RATE_LIMIT_WAIT = 65  # seconds to wait after a 429


def _api_get_paced(url, headers, params=None, timeout=15):
    """GHL API GET with unlimited retry on rate limits.
    Unlike _api_get which retries 3 times and gives up, this function waits
    as long as needed for rate limits to clear. For deep pulls that must
    finish eventually."""
    max_retries = 100  # effectively unlimited — 100 rate limit waits = ~100 min
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", _DEEP_SYNC_RATE_LIMIT_WAIT))
                logger.info(f"[DEEP_SYNC] Rate limited, waiting {retry_after}s (attempt {attempt+1})")
                _time.sleep(retry_after)
                continue

            if resp.status_code in (401, 403):
                return None, "auth_error"

            resp.raise_for_status()
            return resp.json(), None

        except requests.HTTPError as e:
            status = e.response.status_code if e.response else 0
            logger.warning(f"[DEEP_SYNC] HTTP {status} attempt {attempt+1}: {url}")
            _time.sleep(4)
        except (requests.Timeout, requests.ConnectionError) as e:
            logger.warning(f"[DEEP_SYNC] Network error attempt {attempt+1}: {e}")
            _time.sleep(4)
        except Exception as e:
            logger.error(f"[DEEP_SYNC] Unexpected error: {e}", exc_info=True)
            return None, "unexpected"

    return None, "max_retries"


def deep_sync_conversations(location_id, access_token=None):
    """One-time deep historical pull of ALL conversation data from GHL.

    Differences from sync_ghl_conversations:
    - No page cap — pulls every message for every contact
    - Paced to stay just under GHL rate limit (85 req/min)
    - Unlimited retry on 429s — waits and continues, never gives up
    - Tracks progress in ghl_sync_state as 'conversations_deep'
    - Only runs once — checks for prior completion before starting
    - Can resume from where it left off if interrupted (uses cursor)

    After this completes, all new calls go through our dialer (call_history)
    and the regular incremental sync handles any GHL-side activity.
    """
    if not access_token:
        access_token = get_valid_token(location_id)
    if not access_token or access_token == 'DEMO':
        return {"synced": 0, "error": "no_token"}

    conn = get_db_connection()
    if not conn:
        return {"synced": 0, "error": "no_db"}

    headers = _get_headers(access_token)
    total_synced = 0
    contacts_processed = 0

    try:
        # Check if deep sync already completed
        state = _get_sync_state(conn, location_id, 'conversations_deep')
        if state and state.get('sync_status') == 'completed':
            logger.info(f"[DEEP_SYNC] {location_id} | Already completed, skipping")
            return {"synced": state.get('total_synced', 0), "status": "already_completed"}

        # Check if already running (prevent overlap)
        if state and state.get('sync_status') == 'running':
            last = state.get('last_sync_at')
            if last:
                if isinstance(last, str):
                    last = datetime.fromisoformat(last.replace('Z', '+00:00'))
                age = datetime.utcnow() - (last.replace(tzinfo=None) if hasattr(last, 'tzinfo') and last.tzinfo else last)
                if age < timedelta(hours=2):
                    return {"synced": 0, "status": "already_running"}

        _update_sync_state(conn, location_id, 'conversations_deep', 'running')

        # Get resume cursor (contact index) if resuming from interruption
        resume_index = 0
        if state and state.get('last_cursor'):
            try:
                resume_index = int(state['last_cursor'])
                total_synced = state.get('total_synced', 0) or 0
            except (ValueError, TypeError):
                pass

        # Step 1: Get ALL contacts (no cap)
        contact_ids = _deep_get_all_contacts(location_id, headers)
        total_contacts = len(contact_ids)

        if not contact_ids:
            _update_sync_state(conn, location_id, 'conversations_deep', 'completed', total=0)
            return {"synced": 0, "contacts": 0}

        logger.info(f"[DEEP_SYNC] {location_id} | Starting deep pull for {total_contacts} contacts"
                     f" (resuming from #{resume_index})" if resume_index else "")

        # Step 2: For each contact, pull ALL messages (no page cap)
        for i, contact_id in enumerate(contact_ids):
            if i < resume_index:
                continue  # Skip already-processed contacts on resume

            try:
                count = _deep_sync_contact(conn, location_id, contact_id, headers)
                total_synced += count
                contacts_processed += 1

                # Pace ourselves: ~85 requests/min
                _time.sleep(_DEEP_SYNC_PACE)

                # Update progress every 5 contacts
                if contacts_processed % 5 == 0:
                    _update_sync_state(conn, location_id, 'conversations_deep', 'running',
                                       cursor=str(i + 1), total=total_synced)
                    logger.info(f"[DEEP_SYNC] {location_id} | Progress: {i+1}/{total_contacts} contacts, "
                                f"{total_synced} messages synced")

            except Exception as e:
                logger.warning(f"[DEEP_SYNC] {location_id} | Error on contact {contact_id}: {e}")
                # Save progress so we can resume
                _update_sync_state(conn, location_id, 'conversations_deep', 'running',
                                   cursor=str(i), total=total_synced)
                continue

        # Mark as completed — this sync never needs to run again
        _update_sync_state(conn, location_id, 'conversations_deep', 'completed',
                           cursor=str(total_contacts), total=total_synced)
        logger.info(f"[DEEP_SYNC] {location_id} | COMPLETE: {total_contacts} contacts, "
                    f"{total_synced} messages synced")

        return {
            "synced": total_synced,
            "contacts": total_contacts,
            "status": "completed",
        }

    except Exception as e:
        logger.error(f"[DEEP_SYNC] {location_id} | FAILED: {e}", exc_info=True)
        _update_sync_state(conn, location_id, 'conversations_deep', 'failed',
                           cursor=str(resume_index + contacts_processed),
                           total=total_synced, error=str(e)[:500])
        return {"synced": total_synced, "error": str(e), "status": "failed"}
    finally:
        return_db_connection(conn)


def _deep_get_all_contacts(location_id, headers):
    """Fetch ALL contact IDs from GHL with no cap. Paced for rate limits."""
    contact_ids = []
    url = f"{GHL_BASE}/contacts/"
    params = {"locationId": location_id, "limit": 100}
    max_pages = 200  # 200 pages × 100 = 20,000 contacts (safety)

    for page in range(max_pages):
        data, err = _api_get_paced(url, headers, params=params)
        if err or not data:
            break

        contacts = data.get("contacts", [])
        for c in contacts:
            cid = c.get("id")
            if cid:
                contact_ids.append(cid)

        if not contacts:
            break

        # Check for next page
        meta = data.get("meta", {})
        start_after = meta.get("startAfterId") or meta.get("nextPageStartAfterId")
        next_page_url = meta.get("nextPageUrl") or meta.get("nextPage")

        if start_after:
            params["startAfterId"] = start_after
        elif isinstance(next_page_url, str) and next_page_url.startswith("http"):
            url = next_page_url
            params = {}
        else:
            break

        _time.sleep(_DEEP_SYNC_PACE)

    logger.info(f"[DEEP_SYNC] {location_id} | Found {len(contact_ids)} contacts")
    return contact_ids


def _deep_sync_contact(conn, location_id, contact_id, headers):
    """Pull ALL messages for a single contact. No page cap. Returns count."""
    # Find conversation
    search_url = f"{GHL_BASE}/conversations/search"
    data, err = _api_get_paced(search_url, headers,
                                params={"locationId": location_id, "contactId": contact_id})
    if err or not data:
        return 0

    convos = data.get("conversations", [])
    if not convos:
        return 0

    convo_id = convos[0].get("id")
    if not convo_id:
        return 0

    contact_name = convos[0].get("contactName") or convos[0].get("fullName") or ""
    contact_phone = convos[0].get("phone") or ""

    # Fetch ALL messages — no page cap
    msg_url = f"{GHL_BASE}/conversations/{convo_id}/messages"
    messages_to_insert = []
    max_pages = 100  # 100 pages × 100 msgs = 10,000 per contact (safety)

    for page in range(max_pages):
        params = {"limit": 100}
        data, err = _api_get_paced(msg_url, headers, params=params)
        if err or not data:
            break

        messages_payload = data.get("messages", [])
        if isinstance(messages_payload, dict):
            raw_messages = messages_payload.get("messages", [])
            next_page = messages_payload.get("nextPage")
            last_msg_id = messages_payload.get("lastMessageId")
        elif isinstance(messages_payload, list):
            raw_messages = messages_payload
            next_page = None
            last_msg_id = None
        else:
            break

        if not raw_messages:
            break

        for m in raw_messages:
            if not isinstance(m, dict):
                continue

            ghl_msg_id = m.get("id") or m.get("messageId")
            if not ghl_msg_id:
                continue

            date_added = m.get("dateAdded") or m.get("createdAt") or m.get("created_at")
            direction = m.get("direction", "inbound")
            body = m.get("body") or m.get("text") or ""
            msg_type, source = _classify_message_type(m)

            messages_to_insert.append((
                location_id, contact_id, contact_name, contact_phone,
                convo_id, msg_type, direction, body[:5000], source,
                ghl_msg_id, date_added
            ))

        # Pagination
        if not next_page and not last_msg_id:
            break
        if last_msg_id:
            msg_url = f"{GHL_BASE}/conversations/{convo_id}/messages?lastMessageId={last_msg_id}"

        _time.sleep(_DEEP_SYNC_PACE)

    # Bulk upsert
    if messages_to_insert:
        try:
            cur = conn.cursor()
            from psycopg2.extras import execute_values
            execute_values(cur, """
                INSERT INTO ghl_conversations
                    (location_id, contact_id, contact_name, contact_phone,
                     conversation_id, message_type, direction, body, source,
                     ghl_message_id, date_added, synced_at)
                VALUES %s
                ON CONFLICT (ghl_message_id) DO UPDATE SET
                    body = EXCLUDED.body,
                    synced_at = NOW()
            """, [(
                *row, datetime.utcnow()
            ) for row in messages_to_insert],
            template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")
            conn.commit()
            cur.close()
        except Exception as e:
            conn.rollback()
            logger.error(f"[DEEP_SYNC] Bulk insert failed for {contact_id}: {e}")
            return 0

    return len(messages_to_insert)


def get_deep_sync_status(location_id):
    """Get deep sync status for frontend progress display.
    Returns dict with status, progress %, contacts done/total, messages synced."""
    conn = get_db_connection()
    if not conn:
        return {"status": "unknown"}
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT sync_status, last_cursor, total_synced, error_message, last_sync_at
            FROM ghl_sync_state
            WHERE location_id = %s AND resource_type = 'conversations_deep'
        """, (location_id,))
        row = cur.fetchone()
        cur.close()

        if not row:
            return {"status": "not_started"}

        status = row['sync_status']
        cursor = int(row['last_cursor'] or 0)
        total_synced = row['total_synced'] or 0

        result = {
            "status": status,
            "contacts_processed": cursor,
            "messages_synced": total_synced,
            "last_update": row['last_sync_at'].isoformat() if row['last_sync_at'] else None,
        }

        if status == 'failed':
            result["error"] = row['error_message']

        return result
    except Exception:
        return {"status": "unknown"}
    finally:
        return_db_connection(conn)


# ─── Query Functions (used by other modules) ─────────────────────────────────

def get_merged_call_count(location_id, contact_id):
    """
    Get total dial count for a contact: local call_history + ghl_conversations calls.
    Deduplicates by timestamp window (60s) + phone number.
    """
    conn = get_db_connection()
    if not conn:
        return 0

    try:
        cur = conn.cursor()

        # Local dialer calls
        cur.execute("""
            SELECT COUNT(*) as cnt FROM call_history
            WHERE location_id = %s AND contact_id = %s
        """, (location_id, contact_id))
        local_count = cur.fetchone()['cnt']

        # GHL calls (excluding those already counted in local)
        cur.execute("""
            SELECT COUNT(*) as cnt FROM ghl_conversations
            WHERE location_id = %s AND contact_id = %s
              AND message_type IN ('call', 'voicemail')
              AND source != 'dialer'
        """, (location_id, contact_id))
        ghl_count = cur.fetchone()['cnt']

        cur.close()
        return local_count + ghl_count

    except Exception as e:
        logger.error(f"[GHL_SYNC] Merged call count error: {e}")
        return 0
    finally:
        return_db_connection(conn)


def get_merged_call_history(location_id, contact_id=None, limit=100, offset=0):
    """
    Get merged call history from both local and GHL sources.
    Returns list of call records ordered by timestamp DESC.
    """
    conn = get_db_connection()
    if not conn:
        return []

    try:
        cur = conn.cursor()
        contact_filter = "AND contact_id = %s" if contact_id else ""
        params_base = [location_id]
        if contact_id:
            params_base.append(contact_id)

        cur.execute(f"""
            SELECT * FROM (
                -- Local dialer calls
                SELECT
                    id, location_id, contact_id, contact_name, phone,
                    direction, status, duration, recording_url, call_sid,
                    transcript, started_at as call_time,
                    'dialer' as source, 'call' as message_type
                FROM call_history
                WHERE location_id = %s {contact_filter}

                UNION ALL

                -- GHL calls/voicemails (exclude dialer-sourced to avoid dupes)
                SELECT
                    id, location_id, contact_id, contact_name, contact_phone as phone,
                    direction, 'completed' as status, 0 as duration, NULL as recording_url,
                    NULL as call_sid, NULL as transcript,
                    COALESCE(date_added::timestamp, synced_at) as call_time,
                    source, message_type
                FROM ghl_conversations
                WHERE location_id = %s {contact_filter}
                  AND message_type IN ('call', 'voicemail')
                  AND source != 'dialer'
            ) merged
            ORDER BY call_time DESC
            LIMIT %s OFFSET %s
        """, params_base + params_base + [limit, offset])

        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]

    except Exception as e:
        logger.error(f"[GHL_SYNC] Merged history error: {e}")
        return []
    finally:
        return_db_connection(conn)


def get_contact_pipeline_stage(location_id, contact_id):
    """Get the current pipeline stage for a contact (for AI prompt injection)."""
    conn = get_db_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT pipeline_name, stage_name, status, monetary_value
            FROM ghl_opportunities
            WHERE location_id = %s AND contact_id = %s
            ORDER BY updated_at_ghl DESC NULLS LAST
            LIMIT 1
        """, (location_id, contact_id))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception:
        return None
    finally:
        return_db_connection(conn)


def get_sync_stats_for_dashboard(location_id):
    """Get sync status for dashboard display."""
    conn = get_db_connection()
    if not conn:
        return {}

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT resource_type, sync_status, last_sync_at, total_synced, error_message
            FROM ghl_sync_state
            WHERE location_id = %s
        """, (location_id,))
        rows = cur.fetchall()
        cur.close()
        return {r['resource_type']: dict(r) for r in rows}
    except Exception:
        return {}
    finally:
        return_db_connection(conn)


def get_conversation_stats(location_id):
    """Get conversation statistics from synced data for dashboard KPIs."""
    conn = get_db_connection()
    if not conn:
        return {}

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE message_type = 'sms') as total_sms,
                COUNT(*) FILTER (WHERE message_type = 'call') as total_calls,
                COUNT(*) FILTER (WHERE message_type = 'voicemail') as total_voicemails,
                COUNT(*) FILTER (WHERE message_type = 'email') as total_emails,
                COUNT(DISTINCT contact_id) as unique_contacts,
                COUNT(*) FILTER (WHERE source = 'ghl_native') as ghl_native,
                COUNT(*) FILTER (WHERE source = 'wavv') as wavv,
                COUNT(*) FILTER (WHERE source = 'dialer') as dialer_calls
            FROM ghl_conversations
            WHERE location_id = %s
        """, (location_id,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else {}
    except Exception:
        return {}
    finally:
        return_db_connection(conn)
