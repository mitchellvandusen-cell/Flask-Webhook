# blueprints/inbox.py — Inbox, SSE notifications, GHL sync, and related routes
#
# Extracted from main.py. Provides:
#   - /api/ghl-phone-numbers
#   - /api/inbox/conversations
#   - /api/inbox/thread/<contact_id>
#   - /api/stream/notifications (SSE)
#   - /api/contact/<contact_id>/intelligence
#   - /api/sync-status
#   - /api/sync/deep-pull
#   - /api/sync/deep-pull/status
#   - /api/sync/deep-pull/reset

import json
import logging
from datetime import datetime

from flask import Blueprint, Response, request
from flask_login import login_required, current_user

from db import get_db_connection, return_db_connection
from extensions import safe_jsonify, ensure_redis, q_website, redis_conn

logger = logging.getLogger(__name__)

inbox_bp = Blueprint('inbox', __name__)


# ── GHL Phone Numbers ────────────────────────────────────────────────────────

@inbox_bp.route("/api/ghl-phone-numbers", methods=["GET"])
@login_required
def api_ghl_phone_numbers():
    """Fetch GHL phone numbers for the current user's location.
    Returns both cached ghl_numbers from voice_config and can trigger a fresh sync."""
    location_id = current_user.location_id
    if not location_id:
        return safe_jsonify({"numbers": [], "error": "No location connected"})

    cached = (current_user.voice_config or {}).get("ghl_numbers", [])
    refresh = request.args.get("refresh", "false").lower() == "true"

    if cached and not refresh:
        return safe_jsonify({"numbers": cached, "source": "cache"})

    try:
        from ghl_sync import sync_ghl_phone_numbers
        result = sync_ghl_phone_numbers(location_id)
        return safe_jsonify({
            "numbers": result.get("numbers", []),
            "source": "live",
        })
    except Exception as e:
        logger.error(f"GHL phone number fetch failed: {e}")
        return safe_jsonify({"numbers": [], "source": "error"})


# ── Inbox Conversations ──────────────────────────────────────────────────────

@inbox_bp.route("/api/inbox/conversations", methods=["GET"])
@login_required
def api_inbox_conversations():
    """
    Get unified conversation list sorted by most recent message.
    Supports search by contact name/phone.
    Primary: synced GHL data (ghl_conversations).
    Fallback: local webhook messages (contact_messages via contact_cache).
    """
    location_id = current_user.location_id
    if not location_id:
        return safe_jsonify({"conversations": []})

    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    search = request.args.get("q", "").strip().lower()

    conn = get_db_connection()
    if not conn:
        return safe_jsonify({"conversations": [], "error": "DB unavailable"})

    try:
        cur = conn.cursor()

        # Primary: GHL synced conversations — get latest message per contact
        # Include all messaging types (sms, email, social) but exclude calls/voicemails
        # which have dedicated apps in the phone UI.
        if search:
            cur.execute("""
                SELECT DISTINCT ON (contact_id)
                    contact_id, contact_name, contact_phone,
                    body as last_message, direction as last_direction,
                    message_type, date_added, source
                FROM ghl_conversations
                WHERE location_id = %s
                  AND message_type NOT IN ('call', 'voicemail')
                  AND (lower(contact_name) LIKE %s OR contact_phone LIKE %s)
                ORDER BY contact_id, date_added DESC
            """, (location_id, f"%{search}%", f"%{search}%"))
        else:
            cur.execute("""
                SELECT DISTINCT ON (contact_id)
                    contact_id, contact_name, contact_phone,
                    body as last_message, direction as last_direction,
                    message_type, date_added, source
                FROM ghl_conversations
                WHERE location_id = %s
                  AND message_type NOT IN ('call', 'voicemail')
                ORDER BY contact_id, date_added DESC
            """, (location_id,))
        all_convos = cur.fetchall()

        source_label = "ghl_sync"

        # Fallback: local contact_messages if GHL sync hasn't populated yet
        if not all_convos:
            if search:
                cur.execute("""
                    SELECT DISTINCT ON (cm.contact_id)
                        cm.contact_id,
                        COALESCE(cc.name, cc.first_name, 'Unknown') as contact_name,
                        COALESCE(cc.phone, '') as contact_phone,
                        cm.message_text as last_message,
                        CASE WHEN cm.message_type = 'lead' THEN 'inbound' ELSE 'outbound' END as last_direction,
                        'sms' as message_type,
                        cm.created_at::text as date_added,
                        'local' as source
                    FROM contact_messages cm
                    JOIN contact_cache cc ON cm.contact_id = cc.contact_id AND cc.location_id = %s
                    WHERE (lower(cc.name) LIKE %s OR cc.phone LIKE %s)
                    ORDER BY cm.contact_id, cm.created_at DESC
                """, (location_id, f"%{search}%", f"%{search}%"))
            else:
                cur.execute("""
                    SELECT DISTINCT ON (cm.contact_id)
                        cm.contact_id,
                        COALESCE(cc.name, cc.first_name, 'Unknown') as contact_name,
                        COALESCE(cc.phone, '') as contact_phone,
                        cm.message_text as last_message,
                        CASE WHEN cm.message_type = 'lead' THEN 'inbound' ELSE 'outbound' END as last_direction,
                        'sms' as message_type,
                        cm.created_at::text as date_added,
                        'local' as source
                    FROM contact_messages cm
                    JOIN contact_cache cc ON cm.contact_id = cc.contact_id AND cc.location_id = %s
                    ORDER BY cm.contact_id, cm.created_at DESC
                """, (location_id,))
            all_convos = cur.fetchall()
            source_label = "local"

        cur.close()

        # Sort by most recent message and apply pagination
        all_convos.sort(key=lambda r: r.get('date_added') or '', reverse=True)
        total = len(all_convos)
        page = all_convos[offset:offset + limit]

        conversations = []
        for row in page:
            conversations.append({
                "contact_id": row['contact_id'],
                "contact_name": row['contact_name'] or "Unknown",
                "contact_phone": row['contact_phone'] or "",
                "last_message": (row['last_message'] or "")[:120],
                "last_direction": row['last_direction'],
                "message_type": row['message_type'],
                "date": row['date_added'],
                "source": row['source'],
            })

        return safe_jsonify({
            "conversations": conversations,
            "total": total,
            "has_more": offset + limit < total,
            "data_source": source_label,
        })

    except Exception as e:
        logger.error(f"Inbox conversations failed: {e}")
        # Table may not exist yet
        return safe_jsonify({"conversations": [], "error": "sync_pending"})
    finally:
        return_db_connection(conn)


# ── Inbox Thread ─────────────────────────────────────────────────────────────

@inbox_bp.route("/api/inbox/thread/<contact_id>", methods=["GET"])
@login_required
def api_inbox_thread(contact_id):
    """Get full conversation thread for a contact from synced or local data."""
    location_id = current_user.location_id
    if not location_id:
        return safe_jsonify({"messages": []})

    limit = min(int(request.args.get("limit", 100)), 500)

    conn = get_db_connection()
    if not conn:
        return safe_jsonify({"messages": []})

    try:
        cur = conn.cursor()

        # Primary: GHL synced messages
        cur.execute("""
            SELECT body, direction, message_type, source, date_added
            FROM ghl_conversations
            WHERE location_id = %s AND contact_id = %s
            ORDER BY date_added ASC
            LIMIT %s
        """, (location_id, contact_id, limit))
        ghl_messages = []
        for row in cur.fetchall():
            ghl_messages.append({
                "body": row['body'] or "",
                "direction": row['direction'],
                "type": row['message_type'],
                "source": row['source'],
                "date": row['date_added'],
            })

        # Always supplement with local contact_messages (real-time webhook data).
        # This ensures inbound replies from contacts appear even when the GHL deep
        # sync hasn't run yet or hasn't captured them.
        cur.execute("""
            SELECT message_text as body,
                   CASE WHEN message_type = 'lead' THEN 'inbound' ELSE 'outbound' END as direction,
                   'sms' as message_type,
                   'local' as source,
                   created_at::text as date_added
            FROM contact_messages
            WHERE contact_id = %s
            ORDER BY created_at ASC
            LIMIT %s
        """, (contact_id, limit))
        local_messages = []
        for row in cur.fetchall():
            local_messages.append({
                "body": row['body'] or "",
                "direction": row['direction'],
                "type": row['message_type'],
                "source": row['source'],
                "date": row['date_added'],
            })

        # Merge: GHL data is primary; add local messages not already present in GHL.
        # Dedup by (first 80 chars of body, direction) to avoid showing duplicates.
        ghl_fingerprints = {(m['body'][:80], m['direction']) for m in ghl_messages}
        messages = list(ghl_messages)
        for lm in local_messages:
            fp = (lm['body'][:80], lm['direction'])
            if fp not in ghl_fingerprints:
                messages.append(lm)

        # Sort merged result by date
        messages.sort(key=lambda m: str(m.get('date') or ''))

        cur.close()

        # Also get pipeline stage for this contact
        pipeline = None
        try:
            from ghl_sync import get_contact_pipeline_stage
            pipeline = get_contact_pipeline_stage(location_id, contact_id)
        except Exception:
            pass

        return safe_jsonify({
            "messages": messages,
            "pipeline": pipeline,
        })
    except Exception as e:
        logger.error(f"Inbox thread failed for {contact_id}: {e}")
        return safe_jsonify({"messages": []})
    finally:
        return_db_connection(conn)


# ── SSE Notifications ────────────────────────────────────────────────────────

@inbox_bp.route("/api/stream/notifications")
@login_required
def api_stream_notifications():
    """
    Server-Sent Events stream for real-time dashboard notifications.
    Pushes new webhook events to the connected client.
    """
    location_id = current_user.location_id
    if not location_id:
        return Response("data: {}\n\n", mimetype='text/event-stream')

    def event_stream():
        import time as _t
        last_check = datetime.utcnow()
        yield f"data: {json.dumps({'type': 'connected', 'location_id': location_id})}\n\n"

        while True:
            _t.sleep(5)  # Check every 5 seconds
            try:
                conn = get_db_connection()
                if not conn:
                    continue
                try:
                    cur = conn.cursor()
                    cur.execute("""
                        SELECT id, event_type, details, contact_id, created_at
                        FROM webhook_logs
                        WHERE location_id = %s
                          AND created_at > %s
                          AND event_type IN ('message_sent', 'webhook_received', 'ghl_sync_complete')
                        ORDER BY created_at ASC
                        LIMIT 10
                    """, (location_id, last_check))
                    rows = cur.fetchall()
                    cur.close()

                    for row in rows:
                        event = {
                            "type": row['event_type'],
                            "details": row['details'],
                            "contact_id": row.get('contact_id'),
                            "time": row['created_at'].isoformat() if row.get('created_at') else None,
                        }
                        yield f"data: {json.dumps(event)}\n\n"
                        last_check = row['created_at']

                    if not rows:
                        yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

                finally:
                    return_db_connection(conn)

            except GeneratorExit:
                break
            except Exception as e:
                logger.debug(f"SSE stream error: {e}")
                yield f"data: {json.dumps({'type': 'error'})}\n\n"

    return Response(
        event_stream(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        }
    )


# ── Contact Intelligence ─────────────────────────────────────────────────────

@inbox_bp.route("/api/contact/<contact_id>/intelligence", methods=["GET"])
@login_required
def api_contact_intelligence(contact_id):
    """
    Get AI-powered intelligence dossier for a contact.
    Returns: summary, temperature, score, next-best-actions, facts, pipeline stage.
    AI analysis is cached and only regenerated when new messages arrive.
    """
    location_id = current_user.location_id
    if not location_id:
        return safe_jsonify({"error": "No location connected"})

    try:
        from lead_intelligence import get_contact_intelligence
        intel = get_contact_intelligence(location_id, contact_id)
        return safe_jsonify(intel)
    except Exception as e:
        logger.error(f"Contact intelligence failed: {e}")
        return safe_jsonify({"score": {"score": 50, "label": "unknown"}, "actions": []})


# ── Sync Status ──────────────────────────────────────────────────────────────

@inbox_bp.route("/api/sync-status", methods=["GET"])
@login_required
def api_sync_status():
    """Get GHL sync status for dashboard display."""
    location_id = current_user.location_id
    if not location_id:
        return safe_jsonify({})

    try:
        from ghl_sync import get_sync_stats_for_dashboard
        stats = get_sync_stats_for_dashboard(location_id)
        return safe_jsonify(stats)
    except Exception as e:
        logger.error(f"Sync status fetch failed: {e}")
        return safe_jsonify({})


# ── Deep Pull ────────────────────────────────────────────────────────────────

@inbox_bp.route("/api/sync/deep-pull", methods=["POST"])
@login_required
def api_sync_deep_pull():
    """Trigger a one-time deep historical pull of all GHL conversation data.
    Runs as background RQ job. Only fires once — subsequent calls return current status."""
    location_id = current_user.location_id
    if not location_id:
        return safe_jsonify({"error": "No location connected"}), 400

    try:
        from ghl_sync import get_deep_sync_status, deep_sync_conversations

        # Check current status — don't re-trigger if already done or running
        status = get_deep_sync_status(location_id)
        if status.get("status") == "completed" and status.get("messages_synced", 0) > 0:
            return safe_jsonify({"status": "already_completed", **status})
        if status.get("status") == "running":
            return safe_jsonify({"status": "already_running", **status})

        # If previous run completed with 0 records (bug), reset so it runs again
        if status.get("status") == "completed" and status.get("messages_synced", 0) == 0:
            from ghl_sync import _update_sync_state
            rc = get_db_connection()
            if rc:
                try:
                    _update_sync_state(rc, location_id, 'conversations_deep', 'pending')
                finally:
                    return_db_connection(rc)

        # Queue background job (long-running, up to 2 hours)
        ensure_redis()
        job = q_website.enqueue(
            deep_sync_conversations,
            location_id,
            job_timeout=7200,  # 2 hour max
            result_ttl=86400,
            job_id=f"deep-sync-{location_id}",
        )

        # Also trigger a contact cache refresh so the dialer contact count updates
        try:
            from voice.dialer import _background_contact_sync
            _background_contact_sync(location_id)
        except Exception as ce:
            logger.warning(f"Contact cache refresh trigger failed (non-fatal): {ce}")

        return safe_jsonify({"status": "started", "job_id": job.id})

    except Exception as e:
        logger.error(f"Deep sync trigger failed: {e}", exc_info=True)
        return safe_jsonify({"error": str(e)}), 500


@inbox_bp.route("/api/sync/deep-pull/status", methods=["GET"])
@login_required
def api_sync_deep_pull_status():
    """Poll deep sync progress."""
    location_id = current_user.location_id
    if not location_id:
        return safe_jsonify({"status": "not_started"})

    try:
        from ghl_sync import get_deep_sync_status
        return safe_jsonify(get_deep_sync_status(location_id))
    except Exception as e:
        logger.error(f"Deep sync status failed: {e}")
        return safe_jsonify({"status": "unknown"})


@inbox_bp.route("/api/sync/deep-pull/reset", methods=["POST"])
@login_required
def api_sync_deep_pull_reset():
    """Reset deep sync state so it can be re-triggered for a full historical re-pull.
    Resets the sync state to 'pending' and re-queues the deep sync job."""
    location_id = current_user.location_id
    if not location_id:
        return safe_jsonify({"error": "No location connected"}), 400

    try:
        from ghl_sync import _update_sync_state, deep_sync_conversations
        conn = get_db_connection()
        if not conn:
            return safe_jsonify({"error": "Database error"}), 500
        try:
            cur = conn.cursor()
            # Reset sync state to pending with zeroed counters
            cur.execute("""
                UPDATE ghl_sync_state
                SET sync_status = 'pending', last_cursor = '0', total_synced = 0,
                    error_message = NULL, last_sync_at = NOW()
                WHERE location_id = %s AND resource_type = 'conversations_deep'
            """, (location_id,))
            conn.commit()
            cur.close()
        finally:
            return_db_connection(conn)

        # Queue the deep sync job
        ensure_redis()
        job_id = f"deep-sync-{location_id}"
        # Cancel any existing job first
        try:
            from rq.job import Job as RQJob
            existing = RQJob.fetch(job_id, connection=redis_conn)
            if existing and existing.get_status() in ('queued', 'started'):
                existing.cancel()
        except Exception:
            pass

        job = q_website.enqueue(
            deep_sync_conversations,
            location_id,
            job_timeout=7200,
            result_ttl=86400,
            job_id=job_id,
        )
        logger.info(f"Deep sync reset and re-queued for {location_id}")

        # Also trigger a contact cache refresh so the dialer contact count updates
        try:
            from voice.dialer import _background_contact_sync
            _background_contact_sync(location_id)
            logger.info(f"Contact cache refresh triggered alongside deep sync for {location_id}")
        except Exception as ce:
            logger.warning(f"Contact cache refresh trigger failed (non-fatal): {ce}")

        return safe_jsonify({"status": "reset_and_started", "job_id": job.id})

    except Exception as e:
        logger.error(f"Deep sync reset failed: {e}", exc_info=True)
        return safe_jsonify({"error": str(e)}), 500
