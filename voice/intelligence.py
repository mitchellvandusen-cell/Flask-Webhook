import json
import os
import logging

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from ghl_auth import jwt_or_session_required

from db import get_db_connection, return_db_connection
from ghl_api import get_valid_token
from ghl_sync import get_merged_call_count
from lead_intelligence import get_bulk_cached_intelligence, batch_analyze_contacts
from voice.helpers import _get_current_subscriber_voice

logger = logging.getLogger("voice_bridge.intelligence")

intelligence_bp = Blueprint('voice_intelligence', __name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_ids_param():
    """Extract 'ids' from GET query string or POST JSON body.
    Allows bulk endpoints to accept POST with JSON body to avoid URL length limits.
    """
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        ids = data.get('ids', '')
        if isinstance(ids, list):
            return ','.join(ids)
        return ids
    return request.args.get('ids', '')


# ── Routes ───────────────────────────────────────────────────────────────────

@intelligence_bp.route('/voice/contact-call-counts', methods=['GET', 'POST'])
@login_required
def get_contact_call_counts():
    """Batch local call counts for a list of contact IDs."""
    ids_param = _get_ids_param()
    if not ids_param:
        return jsonify({})
    contact_ids = [x.strip() for x in ids_param.split(',') if x.strip()][:300]
    conn = get_db_connection()
    if not conn:
        return jsonify({})
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify({})
        location_id = row['location_id']
        cur.execute("""
            SELECT contact_id, COUNT(*) AS cnt
            FROM call_history
            WHERE location_id = %s AND contact_id = ANY(%s)
            GROUP BY contact_id
        """, (location_id, contact_ids))
        result = {r['contact_id']: r['cnt'] for r in cur.fetchall()}
        cur.close()
        return jsonify(result)
    except Exception as e:
        logger.error(f"get_contact_call_counts failed: {e}")
        return jsonify({})
    finally:
        return_db_connection(conn)


@intelligence_bp.route('/voice/contact-engagement', methods=['GET', 'POST'])
@login_required
def get_contact_engagement_bulk():
    """Batch Omnisconn engagement data for contact list Smart Filters.
    Returns lightweight stats: message counts, call counts, last activity timestamps.
    """
    ids_param = _get_ids_param()
    if not ids_param:
        return jsonify({})
    contact_ids = [x.strip() for x in ids_param.split(',') if x.strip()][:300]
    conn = get_db_connection()
    if not conn:
        return jsonify({})
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify({})
        location_id = row['location_id']

        result = {}

        # Message stats from contact_messages
        cur.execute("""
            SELECT contact_id, message_type, COUNT(*) as cnt, MAX(created_at) as last_at
            FROM contact_messages
            WHERE contact_id = ANY(%s)
            GROUP BY contact_id, message_type
        """, (contact_ids,))
        for r in cur.fetchall():
            cid = r['contact_id']
            if cid not in result:
                result[cid] = {"messages": {"lead": 0, "assistant": 0, "last_message_at": None,
                                            "last_lead_at": None, "last_assistant_at": None},
                               "calls": {"total_calls": 0, "connected": 0, "total_duration": 0, "last_call_at": None, "recordings": 0}}
            result[cid]["messages"][r['message_type']] = r['cnt']
            ts = r['last_at'].isoformat() if r['last_at'] else None
            if ts:
                prev = result[cid]["messages"]["last_message_at"]
                if not prev or ts > prev:
                    result[cid]["messages"]["last_message_at"] = ts
                # Store per-type timestamps for Should Respond detection
                if r['message_type'] == 'lead':
                    result[cid]["messages"]["last_lead_at"] = ts
                elif r['message_type'] == 'assistant':
                    result[cid]["messages"]["last_assistant_at"] = ts

        # Call stats from call_history
        cur.execute("""
            SELECT contact_id,
                   COUNT(*) as total_calls,
                   COUNT(*) FILTER (WHERE status = 'completed') as connected,
                   COALESCE(SUM(duration), 0) as total_duration,
                   MAX(started_at) as last_call_at,
                   COUNT(*) FILTER (WHERE recording_url IS NOT NULL) as recordings
            FROM call_history
            WHERE location_id = %s AND contact_id = ANY(%s)
            GROUP BY contact_id
        """, (location_id, contact_ids))
        for r in cur.fetchall():
            cid = r['contact_id']
            if cid not in result:
                result[cid] = {"messages": {"lead": 0, "assistant": 0, "last_message_at": None,
                                            "last_lead_at": None, "last_assistant_at": None},
                               "calls": {"total_calls": 0, "connected": 0, "total_duration": 0, "last_call_at": None, "recordings": 0}}
            result[cid]["calls"] = {
                "total_calls": r['total_calls'],
                "connected": r['connected'],
                "total_duration": r['total_duration'],
                "last_call_at": r['last_call_at'].isoformat() if r['last_call_at'] else None,
                "recordings": r['recordings'],
            }

        # ── Opt-out detection: DnD from contact_cache ──
        cur.execute("""
            SELECT contact_id FROM contact_cache
            WHERE location_id = %s AND contact_id = ANY(%s) AND dnd = TRUE
        """, (location_id, contact_ids))
        for r in cur.fetchall():
            cid = r['contact_id']
            if cid not in result:
                result[cid] = {"messages": {"lead": 0, "assistant": 0, "last_message_at": None,
                                            "last_lead_at": None, "last_assistant_at": None},
                               "calls": {"total_calls": 0, "connected": 0, "total_duration": 0, "last_call_at": None, "recordings": 0}}
            result[cid]["opted_out"] = True

        # ── Last disposition + callback_at per contact (from most recent call with a disposition) ──
        cur.execute("""
            SELECT DISTINCT ON (contact_id) contact_id, disposition, callback_at
            FROM call_history
            WHERE location_id = %s AND contact_id = ANY(%s)
              AND disposition IS NOT NULL AND disposition != '' AND disposition != 'none'
            ORDER BY contact_id, created_at DESC
        """, (location_id, contact_ids))
        for r in cur.fetchall():
            cid = r['contact_id']
            if cid not in result:
                result[cid] = {"messages": {"lead": 0, "assistant": 0, "last_message_at": None,
                                            "last_lead_at": None, "last_assistant_at": None},
                               "calls": {"total_calls": 0, "connected": 0, "total_duration": 0, "last_call_at": None, "recordings": 0}}
            result[cid]["disposition"] = r['disposition']
            if r['callback_at']:
                result[cid]["callback_at"] = r['callback_at'].isoformat()

        # ── Opt-out detection: check last message from each lead for stop keywords ──
        import re as _re
        # TCPA-mandated stop words only — sales objections like "not interested"
        # are handled by the conversation engine, not flagged as opt-outs
        _stop_words = {'stop', 'unsubscribe', 'opt out', 'optout', 'remove me', 'do not contact', 'do not call', 'do not text', 'do not message', 'cancel', "don't contact", "don't call", "don't text", "don't message"}
        cur.execute("""
            SELECT DISTINCT ON (contact_id) contact_id, message_text
            FROM contact_messages
            WHERE contact_id = ANY(%s) AND message_type = 'lead'
            ORDER BY contact_id, created_at DESC
        """, (contact_ids,))
        for r in cur.fetchall():
            cid = r['contact_id']
            if cid in result and r['message_text']:
                msg_lower = r['message_text'].strip().lower()
                # Check exact match OR word-boundary match (not just startswith)
                if msg_lower in _stop_words or any(_re.search(r'\b' + _re.escape(w) + r'\b', msg_lower) for w in _stop_words):
                    result[cid]["opted_out"] = True

        cur.close()
        return jsonify(result)
    except Exception as e:
        logger.error(f"get_contact_engagement_bulk failed: {e}")
        return jsonify({})
    finally:
        return_db_connection(conn)


@intelligence_bp.route('/voice/contact-intelligence-bulk', methods=['GET', 'POST'])
@jwt_or_session_required
def get_contact_intelligence_bulk():
    """Bulk fetch cached AI intelligence for Smart Filters.
    Returns cached AI temperature/score for contacts that have fresh analysis.
    Zero AI cost — reads from contact_intelligence cache table only.
    """
    ids_param = _get_ids_param()
    if not ids_param:
        return jsonify({"cached": {}, "uncached": []})
    contact_ids = [x.strip() for x in ids_param.split(',') if x.strip()][:300]

    conn = get_db_connection()
    if not conn:
        return jsonify({"cached": {}, "uncached": contact_ids})
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"cached": {}, "uncached": contact_ids})
        location_id = row['location_id']
    except Exception:
        return jsonify({"cached": {}, "uncached": contact_ids})
    finally:
        return_db_connection(conn)

    cached = get_bulk_cached_intelligence(location_id, contact_ids)
    uncached = [cid for cid in contact_ids if cid not in cached]
    logger.info(f"[INTEL_BULK] {location_id}: requested={len(contact_ids)}, cached={len(cached)}, uncached={len(uncached)}")
    return jsonify({"cached": cached, "uncached": uncached})


@intelligence_bp.route('/voice/contact-intelligence-analyze', methods=['POST'])
@jwt_or_session_required
def post_contact_intelligence_analyze():
    """Score contacts inline using rule-based intelligence (zero AI cost, instant).
    Previously queued to RQ workers when scoring used AI calls, but rule-based
    scoring runs in milliseconds so we do it directly in the web request.
    Frontend polls the bulk endpoint for results — they'll be available immediately.
    """
    data = request.get_json(silent=True) or {}
    contact_ids = data.get('contact_ids', [])
    if not contact_ids or not isinstance(contact_ids, list):
        return jsonify({"queued": 0, "error": "contact_ids required"})

    conn = get_db_connection()
    if not conn:
        return jsonify({"queued": 0})
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return jsonify({"queued": 0})
        location_id = row['location_id']
    except Exception:
        return jsonify({"queued": 0})
    finally:
        return_db_connection(conn)

    # Rule-based scoring: no AI, no network — runs inline in milliseconds
    from lead_intelligence import bulk_analyze_and_cache, get_bulk_cached_intelligence

    already_cached = get_bulk_cached_intelligence(location_id, contact_ids)
    need_analysis = [cid for cid in contact_ids if cid not in already_cached]

    if not need_analysis:
        logger.info(f"[INTEL_ANALYZE] {location_id}: all {len(contact_ids)} contacts already cached")
        return jsonify({"queued": len(contact_ids), "analyzed": 0, "cached": len(already_cached)})

    analyzed = bulk_analyze_and_cache(location_id, need_analysis)

    logger.info(f"[INTEL_ANALYZE] {location_id}: scored {analyzed}/{len(need_analysis)} inline (rule-based, {len(already_cached)} already cached)")
    return jsonify({"queued": len(contact_ids), "analyzed": analyzed, "cached": len(already_cached)})


@intelligence_bp.route('/voice/contact-call-counts/merged', methods=['GET', 'POST'])
@login_required
def get_contact_call_counts_merged():
    """Batch merged (local DB + synced GHL) call counts for up to 300 contact IDs.
    Uses local crm_conversations table instead of live API — instant, no rate limits."""

    ids_param = _get_ids_param()
    if not ids_param:
        return jsonify({})
    contact_ids = [x.strip() for x in ids_param.split(',') if x.strip()][:300]

    conn = get_db_connection()
    if not conn:
        return jsonify({cid: 0 for cid in contact_ids})

    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        if not row:
            return jsonify({})
        location_id = row['location_id']

        # Local dialer calls (call_history table)
        cur.execute("""
            SELECT contact_id, COUNT(*) AS cnt
            FROM call_history
            WHERE location_id = %s AND contact_id = ANY(%s)
            GROUP BY contact_id
        """, (location_id, contact_ids))
        local_counts = {r['contact_id']: r['cnt'] for r in cur.fetchall()}

        # Synced GHL/WAVV calls (crm_conversations table, exclude dialer to avoid dupes)
        ghl_counts = {}
        try:
            cur.execute("""
                SELECT contact_id, COUNT(*) AS cnt
                FROM crm_conversations
                WHERE location_id = %s AND contact_id = ANY(%s)
                  AND message_type IN ('call', 'voicemail')
                  AND source != 'dialer'
                GROUP BY contact_id
            """, (location_id, contact_ids))
            ghl_counts = {r['contact_id']: r['cnt'] for r in cur.fetchall()}
        except Exception:
            # Table may not exist yet — graceful fallback
            try:
                conn.rollback()
            except Exception:
                pass

        cur.close()

        result = {cid: (local_counts.get(cid, 0) or 0) + (ghl_counts.get(cid, 0) or 0)
                  for cid in contact_ids}
        return jsonify(result)

    except Exception as e:
        logger.error(f"merged call counts failed: {e}")
        return jsonify({cid: 0 for cid in contact_ids})
    finally:
        return_db_connection(conn)


@intelligence_bp.route('/voice/contact/<contact_id>/ghl-call-count')
@login_required
def get_contact_ghl_call_count(contact_id):
    """Return merged call count: local dialer DB + synced GHL conversations.
    Phase 2: Uses local Postgres instead of live GHL API calls — instant, no rate limits."""
    location_id = current_user.location_id
    if not location_id:
        return jsonify({"local": 0, "ghl": 0, "total": 0})

    conn = get_db_connection()
    if not conn:
        return jsonify({"local": 0, "ghl": 0, "total": 0})

    try:
        cur = conn.cursor()

        # Local dialer calls (our call_history table)
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM call_history WHERE location_id = %s AND contact_id = %s",
            (location_id, contact_id)
        )
        local_count = cur.fetchone()['cnt'] or 0

        # Synced GHL calls (from crm_conversations, excluding our dialer to avoid dupes)
        ghl_count = 0
        try:
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM crm_conversations
                WHERE location_id = %s AND contact_id = %s
                  AND message_type IN ('call', 'voicemail')
                  AND source != 'dialer'
            """, (location_id, contact_id))
            ghl_count = cur.fetchone()['cnt'] or 0
        except Exception:
            # Table may not exist yet if migration hasn't run — graceful fallback
            try:
                conn.rollback()
            except Exception:
                pass

        cur.close()

        return jsonify({
            "local": local_count,
            "ghl": ghl_count,
            "total": local_count + ghl_count,
            "source": "synced_db",
        })

    except Exception as e:
        logger.error(f"Merged call count failed for {contact_id}: {e}")
        return jsonify({"local": 0, "ghl": 0, "total": 0})
    finally:
        return_db_connection(conn)
