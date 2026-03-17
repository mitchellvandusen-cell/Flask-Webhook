# crm_providers/hubspot/sync.py — HubSpot Data Sync Engine
#
# Incremental sync of HubSpot data into local Postgres for instant access.
# Mirrors the ghl_sync.py architecture: cursor-based pagination, retry with
# exponential backoff, UPSERT via ON CONFLICT, and sync state tracking.
#
# Syncs: Conversations (Communications), Deals, Contacts
#
# HubSpot rate limits: 100 req/10s (private app), 40 req/10s (OAuth)

import json
import logging
import time
from datetime import datetime, timezone

import requests

from crm_providers.base import SyncResult
from db import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)

HUBSPOT_BASE = "https://api.hubapi.com"
HUBSPOT_TIMEOUT = 20


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ API RETRY WRAPPER ═════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _api_get(url, headers, params=None, timeout=HUBSPOT_TIMEOUT):
    """
    Enterprise retry wrapper for HubSpot API calls.

    Handles:
        - Exponential backoff: 0s, 2s, 4s, 8s
        - 429 rate limiting: honors Retry-After header
        - 401 authentication errors: returns immediately (caller must refresh)
        - 404 not found: returns immediately (permanent failure)
        - Network errors: retries with backoff

    Returns: (response_json, None) on success, (None, error_reason) on failure
    """
    delays = [0, 2, 4, 8]
    last_error = None

    for attempt, delay in enumerate(delays):
        if delay > 0:
            time.sleep(delay)

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=timeout)

            if resp.status_code == 200:
                return (resp.json(), None)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "10"))
                logger.warning(f"HubSpot 429 rate limited — waiting {retry_after}s")
                time.sleep(min(retry_after, 60))
                continue

            if resp.status_code in (401, 403):
                return (None, "auth_error")

            if resp.status_code == 404:
                return (None, "not_found")

            logger.warning(f"HubSpot API {resp.status_code}: {resp.text[:200]} "
                         f"(attempt {attempt + 1}/{len(delays)})")
            last_error = f"http_{resp.status_code}"

        except requests.exceptions.Timeout:
            last_error = "timeout"
            logger.warning(f"HubSpot API timeout (attempt {attempt + 1}/{len(delays)})")
        except requests.exceptions.ConnectionError:
            last_error = "connection_error"
            logger.warning(f"HubSpot API connection error (attempt {attempt + 1}/{len(delays)})")
        except Exception as e:
            last_error = str(e)
            logger.error(f"HubSpot API unexpected error: {e}")
            break

    return (None, last_error or "max_retries")


def _api_post(url, headers, json_data=None, timeout=HUBSPOT_TIMEOUT):
    """POST variant of _api_get with same retry logic."""
    delays = [0, 2, 4, 8]
    last_error = None

    for attempt, delay in enumerate(delays):
        if delay > 0:
            time.sleep(delay)

        try:
            resp = requests.post(url, headers=headers, json=json_data, timeout=timeout)

            if resp.status_code in (200, 201):
                return (resp.json(), None)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "10"))
                time.sleep(min(retry_after, 60))
                continue

            if resp.status_code in (401, 403):
                return (None, "auth_error")

            last_error = f"http_{resp.status_code}"
            logger.warning(f"HubSpot POST {resp.status_code}: {resp.text[:200]}")

        except requests.exceptions.Timeout:
            last_error = "timeout"
        except requests.exceptions.ConnectionError:
            last_error = "connection_error"
        except Exception as e:
            last_error = str(e)
            break

    return (None, last_error or "max_retries")


def _hs_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ SYNC STATE MANAGEMENT ════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _get_sync_state(conn, location_id, resource_type):
    """Get current sync state for a resource type."""
    cur = conn.cursor()
    cur.execute("""
        SELECT last_sync_at, last_cursor, sync_status, total_synced
        FROM crm_sync_state
        WHERE location_id = %s AND resource_type = %s
    """, (location_id, resource_type))
    row = cur.fetchone()
    if row:
        return dict(row)
    return None


def _update_sync_state(conn, location_id, resource_type, **kwargs):
    """Update or insert sync state."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO crm_sync_state
            (location_id, resource_type, crm_source, last_sync_at, last_cursor,
             sync_status, error_message, total_synced)
        VALUES (%s, %s, 'hubspot', %s, %s, %s, %s, %s)
        ON CONFLICT (location_id, resource_type)
        DO UPDATE SET
            last_sync_at = COALESCE(EXCLUDED.last_sync_at, crm_sync_state.last_sync_at),
            last_cursor = COALESCE(EXCLUDED.last_cursor, crm_sync_state.last_cursor),
            sync_status = EXCLUDED.sync_status,
            error_message = EXCLUDED.error_message,
            total_synced = COALESCE(EXCLUDED.total_synced, crm_sync_state.total_synced),
            crm_source = 'hubspot'
    """, (
        location_id,
        resource_type,
        kwargs.get("last_sync_at"),
        kwargs.get("last_cursor"),
        kwargs.get("sync_status", "idle"),
        kwargs.get("error_message"),
        kwargs.get("total_synced"),
    ))
    conn.commit()


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ CONVERSATION SYNC ═════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def sync_hubspot_conversations(location_id, access_token):
    """
    Sync HubSpot Communications (SMS, email, calls) into crm_conversations.

    Uses the CRM v3 Objects API to list Communications with associated contacts.
    Paginated with cursor-based 'after' parameter.
    """
    conn = get_db_connection()
    try:
        _update_sync_state(conn, location_id, "hubspot_conversations",
                          sync_status="syncing")

        headers = _hs_headers(access_token)
        synced = 0
        errors = 0
        after = None

        while True:
            params = {
                "limit": 100,
                "properties": "hs_communication_channel_type,hs_communication_body,"
                             "hs_communication_logged_from,hs_timestamp",
                "associations": "contacts",
            }
            if after:
                params["after"] = after

            url = f"{HUBSPOT_BASE}/crm/v3/objects/communications"
            data, err = _api_get(url, headers, params=params)

            if err:
                logger.error(f"HubSpot conversations sync error: {err}")
                _update_sync_state(conn, location_id, "hubspot_conversations",
                                  sync_status="error", error_message=err)
                return SyncResult(synced=synced, errors=errors + 1, error_message=err)

            results = data.get("results", [])
            if not results:
                break

            cur = conn.cursor()
            for comm in results:
                try:
                    comm_id = str(comm.get("id", ""))
                    props = comm.get("properties", {})
                    channel = props.get("hs_communication_channel_type", "OTHER")
                    body = props.get("hs_communication_body", "")
                    timestamp = props.get("hs_timestamp", "")
                    logged_from = props.get("hs_communication_logged_from", "")

                    # Map channel type to our message_type
                    msg_type = _map_channel_type(channel)

                    # Get associated contact ID
                    contact_id = _extract_contact_association(comm)
                    if not contact_id:
                        continue

                    # Determine direction based on logged_from
                    direction = "outbound" if logged_from == "CRM" else "inbound"

                    cur.execute("""
                        INSERT INTO crm_conversations
                            (location_id, contact_id, conversation_id, message_type,
                             direction, body, source, external_message_id, crm_source,
                             date_added, synced_at)
                        VALUES (%s, %s, %s, %s, %s, %s, 'hubspot', %s, 'hubspot', %s, NOW())
                        ON CONFLICT (external_message_id) DO UPDATE SET
                            body = EXCLUDED.body,
                            synced_at = NOW()
                    """, (
                        location_id, contact_id, comm_id, msg_type,
                        direction, body, f"hs_{comm_id}", timestamp,
                    ))
                    synced += 1

                except Exception as e:
                    errors += 1
                    logger.error(f"HubSpot conversation sync row error: {e}")

            conn.commit()

            # Pagination
            paging = data.get("paging", {})
            next_page = paging.get("next", {})
            after = next_page.get("after")
            if not after:
                break

        _update_sync_state(conn, location_id, "hubspot_conversations",
                          sync_status="completed",
                          last_sync_at=datetime.now(timezone.utc),
                          total_synced=synced)

        logger.info(f"HubSpot conversations synced: {synced} messages, {errors} errors "
                   f"| location={location_id}")
        return SyncResult(synced=synced, errors=errors)

    except Exception as e:
        logger.error(f"HubSpot conversations sync failed: {e}", exc_info=True)
        return SyncResult(errors=1, error_message=str(e))
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ DEAL SYNC ═════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def sync_hubspot_deals(location_id, access_token):
    """
    Sync HubSpot Deals into crm_deals.

    Uses the CRM v3 Objects API with associated contacts and pipeline info.
    """
    conn = get_db_connection()
    try:
        _update_sync_state(conn, location_id, "hubspot_deals",
                          sync_status="syncing")

        headers = _hs_headers(access_token)
        synced = 0
        errors = 0
        after = None

        while True:
            params = {
                "limit": 100,
                "properties": "dealname,dealstage,pipeline,amount,closedate,"
                             "hubspot_owner_id,createdate,hs_lastmodifieddate",
                "associations": "contacts",
            }
            if after:
                params["after"] = after

            url = f"{HUBSPOT_BASE}/crm/v3/objects/deals"
            data, err = _api_get(url, headers, params=params)

            if err:
                _update_sync_state(conn, location_id, "hubspot_deals",
                                  sync_status="error", error_message=err)
                return SyncResult(synced=synced, errors=errors + 1, error_message=err)

            results = data.get("results", [])
            if not results:
                break

            # Fetch pipeline stages for name resolution
            pipelines = _fetch_pipeline_stages(access_token)

            cur = conn.cursor()
            for deal in results:
                try:
                    deal_id = str(deal.get("id", ""))
                    props = deal.get("properties", {})
                    pipeline_id = props.get("pipeline", "")
                    stage_id = props.get("dealstage", "")
                    amount = props.get("amount")
                    contact_id = _extract_contact_association(deal)

                    # Resolve pipeline/stage names
                    pipeline_name = pipelines.get(pipeline_id, {}).get("label", pipeline_id)
                    stage_name = pipelines.get(pipeline_id, {}).get(
                        "stages", {}).get(stage_id, stage_id)

                    cur.execute("""
                        INSERT INTO crm_deals
                            (location_id, contact_id, pipeline_id, pipeline_name,
                             stage_id, stage_name, status, monetary_value, source,
                             assigned_to, external_deal_id, crm_source,
                             created_at_ghl, updated_at_ghl, synced_at)
                        VALUES (%s, %s, %s, %s, %s, %s, 'open', %s, 'hubspot',
                                %s, %s, 'hubspot', %s, %s, NOW())
                        ON CONFLICT (external_deal_id) DO UPDATE SET
                            stage_id = EXCLUDED.stage_id,
                            stage_name = EXCLUDED.stage_name,
                            monetary_value = EXCLUDED.monetary_value,
                            updated_at_ghl = EXCLUDED.updated_at_ghl,
                            synced_at = NOW()
                    """, (
                        location_id, contact_id or "", pipeline_id, pipeline_name,
                        stage_id, stage_name,
                        float(amount) if amount else 0,
                        props.get("hubspot_owner_id", ""),
                        f"hs_{deal_id}",
                        props.get("createdate", ""),
                        props.get("hs_lastmodifieddate", ""),
                    ))
                    synced += 1

                except Exception as e:
                    errors += 1
                    logger.error(f"HubSpot deal sync row error: {e}")

            conn.commit()

            # Pagination
            paging = data.get("paging", {})
            after = paging.get("next", {}).get("after")
            if not after:
                break

        _update_sync_state(conn, location_id, "hubspot_deals",
                          sync_status="completed",
                          last_sync_at=datetime.now(timezone.utc),
                          total_synced=synced)

        logger.info(f"HubSpot deals synced: {synced} deals, {errors} errors "
                   f"| location={location_id}")
        return SyncResult(synced=synced, errors=errors)

    except Exception as e:
        logger.error(f"HubSpot deals sync failed: {e}", exc_info=True)
        return SyncResult(errors=1, error_message=str(e))
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ CONTACT SYNC ══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def sync_hubspot_contacts(location_id, access_token):
    """
    Sync HubSpot contacts into the universal contacts table.
    """
    conn = get_db_connection()
    try:
        _update_sync_state(conn, location_id, "hubspot_contacts",
                          sync_status="syncing")

        headers = _hs_headers(access_token)
        synced = 0
        errors = 0
        after = None

        while True:
            params = {
                "limit": 100,
                "properties": "firstname,lastname,email,phone,address,city,"
                             "state,zip,company,lifecyclestage,hs_lead_status",
            }
            if after:
                params["after"] = after

            url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
            data, err = _api_get(url, headers, params=params)

            if err:
                _update_sync_state(conn, location_id, "hubspot_contacts",
                                  sync_status="error", error_message=err)
                return SyncResult(synced=synced, errors=errors + 1, error_message=err)

            results = data.get("results", [])
            if not results:
                break

            cur = conn.cursor()
            for contact in results:
                try:
                    hs_id = str(contact.get("id", ""))
                    props = contact.get("properties", {})

                    cur.execute("""
                        INSERT INTO contacts
                            (location_id, external_id, crm_source,
                             first_name, last_name, email, phone,
                             address, city, state, zip,
                             pipeline_stage, date_added, last_activity_at)
                        VALUES (%s, %s, 'hubspot', %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                NOW(), NOW())
                        ON CONFLICT (location_id, external_id, crm_source) DO UPDATE SET
                            first_name = EXCLUDED.first_name,
                            last_name = EXCLUDED.last_name,
                            email = EXCLUDED.email,
                            phone = EXCLUDED.phone,
                            address = EXCLUDED.address,
                            city = EXCLUDED.city,
                            state = EXCLUDED.state,
                            zip = EXCLUDED.zip,
                            pipeline_stage = EXCLUDED.pipeline_stage,
                            last_activity_at = NOW()
                    """, (
                        location_id, hs_id,
                        props.get("firstname", ""),
                        props.get("lastname", ""),
                        props.get("email", ""),
                        props.get("phone", ""),
                        props.get("address", ""),
                        props.get("city", ""),
                        props.get("state", ""),
                        props.get("zip", ""),
                        props.get("lifecyclestage", ""),
                    ))
                    synced += 1

                except Exception as e:
                    errors += 1
                    logger.error(f"HubSpot contact sync row error: {e}")

            conn.commit()

            # Pagination
            paging = data.get("paging", {})
            after = paging.get("next", {}).get("after")
            if not after:
                break

        _update_sync_state(conn, location_id, "hubspot_contacts",
                          sync_status="completed",
                          last_sync_at=datetime.now(timezone.utc),
                          total_synced=synced)

        logger.info(f"HubSpot contacts synced: {synced} contacts, {errors} errors "
                   f"| location={location_id}")
        return SyncResult(synced=synced, errors=errors)

    except Exception as e:
        logger.error(f"HubSpot contacts sync failed: {e}", exc_info=True)
        return SyncResult(errors=1, error_message=str(e))
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ FULL SYNC ORCHESTRATOR ════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def sync_all_hubspot(location_id=None, access_token=None):
    """
    Run all HubSpot sync operations.

    If location_id + access_token provided: sync that specific location.
    If called with no args: find all HubSpot subscribers and sync each.
    Called from cron job via RQ worker.
    """
    if location_id and access_token:
        return _sync_single_location(location_id, access_token)

    # Find all active HubSpot subscribers and sync each
    conn = get_db_connection()
    if not conn:
        logger.error("HubSpot sync: no DB connection")
        return {}

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT location_id, crm_config FROM subscribers
            WHERE crm_type = 'hubspot'
              AND crm_config IS NOT NULL
              AND crm_config->>'access_token' IS NOT NULL
        """)
        rows = cur.fetchall()
        logger.info(f"HubSpot sync: found {len(rows)} subscribers to sync")

        all_results = {}
        for row in rows:
            loc_id = row["location_id"]
            crm_config = row["crm_config"] if isinstance(row["crm_config"], dict) else {}
            token = crm_config.get("access_token", "")
            if not token:
                continue
            try:
                all_results[loc_id] = _sync_single_location(loc_id, token)
            except Exception as e:
                logger.error(f"HubSpot sync failed for {loc_id}: {e}")

        return all_results
    except Exception as e:
        logger.error(f"HubSpot sync scan failed: {e}")
        return {}
    finally:
        return_db_connection(conn)


def _sync_single_location(location_id, access_token):
    """Run all sync operations for a single HubSpot location."""
    results = {}

    results["conversations"] = sync_hubspot_conversations(location_id, access_token)
    results["deals"] = sync_hubspot_deals(location_id, access_token)
    results["contacts"] = sync_hubspot_contacts(location_id, access_token)

    total_synced = sum(r.synced for r in results.values())
    total_errors = sum(r.errors for r in results.values())

    logger.info(f"HubSpot full sync complete: {total_synced} synced, "
               f"{total_errors} errors | location={location_id}")

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ HELPERS ═══════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _map_channel_type(channel):
    """Map HubSpot communication channel type to IGB message_type."""
    mapping = {
        "SMS": "sms",
        "EMAIL": "email",
        "CALL": "call",
        "WHATSAPP": "social",
        "FACEBOOK_MESSENGER": "social",
        "LIVE_CHAT": "social",
        "OTHER": "other",
    }
    return mapping.get(channel, "other")


def _extract_contact_association(obj):
    """Extract the first associated contact ID from a HubSpot object."""
    associations = obj.get("associations", {})

    # v3 format: associations.contacts.results[0].id
    contacts_assoc = associations.get("contacts", {})
    results = contacts_assoc.get("results", [])
    if results:
        return str(results[0].get("id", ""))

    return ""


_pipeline_cache = {}  # Simple in-memory cache per sync run


def _fetch_pipeline_stages(access_token):
    """
    Fetch HubSpot deal pipeline definitions for stage name resolution.
    Cached per process lifecycle (sync worker).
    """
    if _pipeline_cache:
        return _pipeline_cache

    headers = _hs_headers(access_token)
    url = f"{HUBSPOT_BASE}/crm/v3/pipelines/deals"
    data, err = _api_get(url, headers)

    if err or not data:
        return {}

    for pipeline in data.get("results", []):
        pid = pipeline.get("id", "")
        _pipeline_cache[pid] = {
            "label": pipeline.get("label", pid),
            "stages": {
                s.get("id", ""): s.get("label", "")
                for s in pipeline.get("stages", [])
            }
        }

    return _pipeline_cache
