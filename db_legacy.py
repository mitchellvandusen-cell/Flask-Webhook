# db.py - PostgreSQL Database Utilities (Production 2026) - FIXED VERSION
import os
import logging
import uuid
import threading
import gspread
import json
from oauth2client.service_account import ServiceAccountCredentials
import time
import math
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from psycopg2 import pool
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)

# --- Google Sheets Setup (Legacy / Backup) ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(os.getenv("GOOGLE_CREDENTIALS", "{}"))
worksheet = None
if creds_dict:
    try:
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
        sheet_url = os.getenv("SUBSCRIBER_SHEET_EDIT_URL")
        if sheet_url:
            sh = gc.open_by_url(sheet_url)
            worksheet = sh.sheet1
            logger.info("Google Sheet connected")
    except Exception as e:
        logger.error(f"Google Sheet connection failed: {e}")

DATABASE_URL = os.getenv("DATABASE_URL")

# --- Connection Pool with Semaphore Queue ---
# At 500 concurrent webhooks, we can't open 500 DB connections (Postgres caps out).
# Instead we keep a bounded pool and use a semaphore so overflow requests WAIT
# for a free connection rather than opening unbounded direct connections.
_connection_pool = None
_pool_semaphore = None
_pool_lock = threading.Lock()

# Pool sizing: match pool to thread count. Gunicorn runs 40 threads, so we need
# at least 40 pool slots to avoid threads blocking each other waiting for DB.
# Workers (4x production + 1x demo) each get their own pool via fork.
_POOL_MIN = int(os.getenv("DB_POOL_MIN", "5"))
_POOL_MAX = int(os.getenv("DB_POOL_MAX", "40"))
# How many requests can wait for a connection before we reject/fallback
_POOL_WAITERS_MAX = int(os.getenv("DB_POOL_WAITERS", "500"))
# Seconds to wait before falling back to direct connection. Keep short — it's
# better to open a direct connection fast than to block a thread for 10s.
_POOL_WAIT_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "3"))

# Pool utilization tracking (lightweight counters for monitoring)
_pool_stats_lock = threading.Lock()
_pool_stats = {
    "acquired": 0,
    "released": 0,
    "timeouts": 0,
    "direct_fallbacks": 0,
    "stale_replaced": 0,
}

def get_pool_stats() -> dict:
    """Return pool utilization stats for monitoring."""
    with _pool_stats_lock:
        stats = dict(_pool_stats)
    stats["in_use"] = stats["acquired"] - stats["released"]
    stats["pool_max"] = _POOL_MAX
    return stats

def _bump_stat(key: str):
    with _pool_stats_lock:
        _pool_stats[key] += 1

def _get_pool():
    """Lazy-init a threaded connection pool with semaphore queuing."""
    global _connection_pool, _pool_semaphore
    if _connection_pool is None and DATABASE_URL:
        with _pool_lock:
            if _connection_pool is None:
                try:
                    _connection_pool = pool.ThreadedConnectionPool(
                        minconn=_POOL_MIN,
                        maxconn=_POOL_MAX,
                        dsn=DATABASE_URL,
                        connect_timeout=10,
                        cursor_factory=RealDictCursor,
                    )
                    _pool_semaphore = threading.Semaphore(_POOL_MAX)
                    logger.info(f"Database connection pool initialized ({_POOL_MIN}-{_POOL_MAX} connections, "
                                f"{_POOL_WAITERS_MAX} max waiters, {_POOL_WAIT_TIMEOUT}s timeout)")
                except psycopg2.Error as e:
                    logger.error(f"Connection pool creation failed: {e}", exc_info=True)
    return _connection_pool

def get_db_connection() -> Optional[psycopg2.extensions.connection]:
    """
    Get a connection from the pool. Up to _POOL_MAX concurrent connections;
    additional callers wait up to _POOL_WAIT_TIMEOUT seconds for a free slot.
    Falls back to a direct connection only if the pool itself is broken.
    """
    if not DATABASE_URL:
        logger.critical("DATABASE_URL not set")
        return None
    p = _get_pool()
    if p and _pool_semaphore:
        # Wait for a pool slot (blocks if all connections are checked out)
        acquired = _pool_semaphore.acquire(timeout=_POOL_WAIT_TIMEOUT)
        if not acquired:
            _bump_stat("timeouts")
            logger.warning(f"Connection pool wait timed out after {_POOL_WAIT_TIMEOUT}s, "
                           "using direct connection")
            return _direct_connect()
        conn = None
        try:
            conn = p.getconn()
            # Validate the connection is alive (catches stale SSL drops)
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.close()
                conn.rollback()
            except Exception:
                _bump_stat("stale_replaced")
                logger.warning("Stale pooled connection detected, replacing")
                try:
                    p.putconn(conn, close=True)
                except Exception:
                    pass
                # Create a fresh direct connection to use for this request.
                # Tag it so return_db_connection releases the semaphore slot
                # even though putconn will fail for this non-pooled connection.
                conn = psycopg2.connect(
                    DATABASE_URL, connect_timeout=10, cursor_factory=RealDictCursor)
                conn._pool_semaphore_acquired = True
                conn.autocommit = False
                _bump_stat("acquired")
                return conn
            _bump_stat("acquired")
            conn.autocommit = False
            return conn
        except Exception as e:
            # Return semaphore slot since we failed to get a usable connection
            _pool_semaphore.release()
            if conn is not None:
                try:
                    p.putconn(conn)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
            _bump_stat("direct_fallbacks")
            logger.warning(f"Pool getconn failed, falling back to direct: {e}")
            return _direct_connect()
    _bump_stat("direct_fallbacks")
    return _direct_connect()

def _direct_connect() -> Optional[psycopg2.extensions.connection]:
    """Fallback: open a direct connection outside the pool."""
    try:
        conn = psycopg2.connect(
            DATABASE_URL,
            connect_timeout=10,
            cursor_factory=RealDictCursor,
        )
        conn.autocommit = False
        return conn
    except psycopg2.Error as e:
        logger.error(f"Database connection failed: {e}", exc_info=True)
        return None

def return_db_connection(conn):
    """Return a connection to the pool (or close if not pooled)."""
    if conn is None:
        return
    _bump_stat("released")
    # Check if this connection owns a semaphore slot (set on stale-replacement path)
    owns_semaphore = getattr(conn, '_pool_semaphore_acquired', False)
    # Always rollback any uncommitted transaction before returning to pool
    try:
        conn.rollback()
    except Exception:
        pass
    p = _get_pool()
    if p:
        try:
            p.putconn(conn)
            # Release semaphore slot so a waiting request can proceed
            if _pool_semaphore:
                _pool_semaphore.release()
            return
        except Exception:
            # putconn failed — connection wasn't from pool (stale replacement)
            try:
                conn.close()
            except Exception:
                pass
            # Still release the semaphore if this conn acquired a slot
            if owns_semaphore and _pool_semaphore:
                _pool_semaphore.release()
            return
    try:
        conn.close()
    except Exception:
        pass


def save_persistent_alert(email: str, alert_type: str, title: str, message: str,
                          severity: str = "warning", location_id: str = None):
    """Save a persistent dashboard alert that stays until dismissed."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        # Prevent duplicate alerts of the same type for the same email
        cur.execute("""
            DELETE FROM persistent_alerts
            WHERE email = %s AND alert_type = %s AND dismissed = FALSE
        """, (email, alert_type))
        cur.execute("""
            INSERT INTO persistent_alerts (email, location_id, alert_type, severity, title, message)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (email, location_id, alert_type, severity, title, message))
        conn.commit()
    except Exception as e:
        logger.debug(f"Failed to save persistent alert: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            return_db_connection(conn)


def get_persistent_alerts(email: str) -> list:
    """Fetch undismissed persistent alerts for a user."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM persistent_alerts
            WHERE email = %s AND dismissed = FALSE
            ORDER BY created_at DESC
        """, (email,))
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        if conn:
            return_db_connection(conn)


def dismiss_persistent_alert(alert_id: int, email: str):
    """Dismiss a persistent alert."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE persistent_alerts SET dismissed = TRUE
            WHERE id = %s AND email = %s
        """, (alert_id, email))
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
    finally:
        if conn:
            return_db_connection(conn)


def get_db_connection_with_retry(max_attempts: int = 3) -> Optional:
    """Get a DB connection with retry. For critical paths like OAuth."""
    for attempt in range(max_attempts):
        conn = get_db_connection()
        if conn:
            return conn
        logger.warning(f"DB connection attempt {attempt+1}/{max_attempts} failed")
        if attempt < max_attempts - 1:
            time.sleep(2 ** attempt)  # 1s, 2s backoff
    logger.error(f"DB connection failed after {max_attempts} attempts")
    return None


def log_webhook_event(location_id: str, event_type: str, status: str = "info",
                      summary: str = "", contact_id: str = None, details: dict = None):
    """Log a webhook/system event for the subscriber's activity log.

    event_type: webhook_received, booking_attempt, booking_success, booking_failed,
                message_sent, message_failed, slot_fetch, error, crm_action
    status: success, error, warning, info
    """
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO webhook_logs (location_id, contact_id, event_type, status, summary, details)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (location_id, contact_id, event_type, status,
              (summary or "")[:500], json.dumps(details or {})))
        conn.commit()
    except Exception as e:
        logger.debug(f"Failed to log webhook event: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            return_db_connection(conn)


def get_auth_failed_messages(location_id: str, max_age_minutes: int = 60,
                             limit: int = 50) -> list:
    """Fetch recent message_failed webhook logs caused by auth errors, that haven't
    been retried yet. Used by the token recovery audit system.

    Returns list of dicts with: id, contact_id, details (contains reply text).
    """
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, contact_id, details, created_at
            FROM webhook_logs
            WHERE location_id = %s
              AND event_type = 'message_failed'
              AND status = 'error'
              AND details->>'failure_reason' = 'auth'
              AND (details->>'retried') IS NULL
              AND created_at > NOW() - INTERVAL '%s minutes'
            ORDER BY created_at ASC
            LIMIT %s
        """, (location_id, max_age_minutes, limit))
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_auth_failed_messages failed: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def mark_webhook_log_retried(log_id: int, success: bool = True) -> bool:
    """Mark a webhook_log entry as retried (prevents re-processing by audit).

    Updates the JSONB details field to add retried=true and retry_result.
    """
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE webhook_logs
            SET details = details || %s::jsonb
            WHERE id = %s
        """, (json.dumps({"retried": True,
                          "retry_result": "success" if success else "failed"}), log_id))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        logger.error(f"mark_webhook_log_retried failed for log {log_id}: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)


def mark_webhook_log_backfill_retried(log_id: int, success: bool = True) -> bool:
    """Mark a webhook_received log entry as backfill-retried.

    Uses the 'backfill_retried' key (distinct from 'retried' used by the
    scourer/audit) so the dropped-webhook query can filter them out.
    """
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE webhook_logs
            SET details = details || %s::jsonb
            WHERE id = %s
        """, (json.dumps({"backfill_retried": True,
                          "backfill_result": "success" if success else "failed"}), log_id))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        logger.error(f"mark_webhook_log_backfill_retried failed for log {log_id}: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_webhook_logs(location_id: str, limit: int = 100, offset: int = 0,
                     event_type: str = None, status: str = None) -> list:
    """Fetch webhook logs for a subscriber, newest first."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        query = "SELECT * FROM webhook_logs WHERE location_id = %s"
        params = [location_id]
        if event_type:
            query += " AND event_type = %s"
            params.append(event_type)
        if status:
            query += " AND status = %s"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        cur.execute(query, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Failed to fetch webhook logs: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_token_failed_webhook_logs(max_age_hours: int = 48,
                                  limit: int = 500) -> list:
    """Find dropped webhooks and failed SMS sends from webhook_logs.

    Works BACKWARDS from outcomes — does NOT rely on error entries existing.

    Catches TWO types of failures:

      1. 'dropped' — A 'webhook_received' entry exists but there is NO
         matching 'message_sent', 'api_webhook_sent', or 'audit_retry_sent'
         within 10 minutes after it.  These are messages that were received
         but never replied to (token refresh failed, task crashed, etc.).
         Needs a full pipeline re-queue.

      2. 'sms_http_fail' — event_type='message_failed'. The AI reply was
         generated but the SMS HTTP call failed.  The reply text is saved
         in details->>'reply' so we only need to re-send the SMS.

    Returns list of dicts with: id, location_id, contact_id, details,
    created_at, message_preview, first_name, entry_type.
    """
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            (
                SELECT wr.id, wr.location_id, wr.contact_id, wr.details,
                       wr.event_type, wr.summary, wr.created_at,
                       'dropped' AS entry_type,
                       wr.details->>'message_preview' AS message_preview,
                       wr.summary AS received_summary
                FROM webhook_logs wr
                WHERE wr.event_type = 'webhook_received'
                  AND wr.created_at > NOW() - INTERVAL '%s hours'
                  AND (wr.details->>'backfill_retried') IS NULL
                  AND wr.contact_id IS NOT NULL
                  -- No successful reply within 10 min
                  AND NOT EXISTS (
                      SELECT 1 FROM webhook_logs ms
                      WHERE ms.location_id = wr.location_id
                        AND ms.contact_id  = wr.contact_id
                        AND ms.event_type IN (
                            'message_sent', 'api_webhook_sent', 'audit_retry_sent'
                        )
                        AND ms.created_at BETWEEN wr.created_at
                                              AND wr.created_at + INTERVAL '10 minutes'
                  )
                  -- Exclude if there's a message_failed (handled separately below)
                  AND NOT EXISTS (
                      SELECT 1 FROM webhook_logs mf
                      WHERE mf.location_id = wr.location_id
                        AND mf.contact_id  = wr.contact_id
                        AND mf.event_type  = 'message_failed'
                        AND mf.created_at BETWEEN wr.created_at
                                              AND wr.created_at + INTERVAL '10 minutes'
                  )
            )
            UNION ALL
            (
                SELECT e.id, e.location_id, e.contact_id, e.details,
                       e.event_type, e.summary, e.created_at,
                       'sms_http_fail' AS entry_type,
                       NULL AS message_preview,
                       NULL AS received_summary
                FROM webhook_logs e
                WHERE e.event_type = 'message_failed'
                  AND e.status = 'error'
                  AND (e.details->>'retried') IS NULL
                  AND e.created_at > NOW() - INTERVAL '%s hours'
            )
            ORDER BY created_at ASC
            LIMIT %s
        """, (max_age_hours, max_age_hours, limit))
        rows = cur.fetchall()
        results = []
        for r in rows:
            d = dict(r)
            # Extract first_name from "Webhook from FirstName" summary
            summary = d.get('received_summary') or ''
            if summary.startswith('Webhook from '):
                d['first_name'] = summary.replace('Webhook from ', '').strip()
            else:
                d['first_name'] = ''
            results.append(d)
        return results
    except Exception as e:
        logger.error(f"get_token_failed_webhook_logs failed: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def save_failed_webhook_payload(location_id: str, contact_id: str,
                                payload: dict, failure_reason: str) -> bool:
    """Persist a failed webhook payload for later recovery by the scourer.

    Called when process_webhook_task aborts due to a token error. The full
    payload is saved so the scourer can re-queue it once a valid token is obtained.
    """
    conn = get_db_connection()
    if not conn:
        return False
    try:
        # Strip _original_payload to avoid double-nesting and keep size manageable
        clean_payload = {k: v for k, v in payload.items() if k != '_original_payload'}
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO failed_webhook_payloads
                (location_id, contact_id, payload, failure_reason)
            VALUES (%s, %s, %s, %s)
        """, (location_id, contact_id, json.dumps(clean_payload), failure_reason))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"save_failed_webhook_payload failed: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_unretried_failed_webhooks(location_id: str = None,
                                  max_age_hours: int = 24,
                                  limit: int = 200) -> list:
    """Fetch failed webhook payloads that haven't been retried yet.

    If location_id is provided, only fetch for that location.
    Otherwise, fetch across all locations (for the scourer cron).
    """
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        if location_id:
            cur.execute("""
                SELECT id, location_id, contact_id, payload, failure_reason, created_at
                FROM failed_webhook_payloads
                WHERE location_id = %s
                  AND retried = FALSE
                  AND created_at > NOW() - INTERVAL '%s hours'
                ORDER BY created_at ASC
                LIMIT %s
            """, (location_id, max_age_hours, limit))
        else:
            cur.execute("""
                SELECT id, location_id, contact_id, payload, failure_reason, created_at
                FROM failed_webhook_payloads
                WHERE retried = FALSE
                  AND created_at > NOW() - INTERVAL '%s hours'
                ORDER BY created_at ASC
                LIMIT %s
            """, (max_age_hours, limit))
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_unretried_failed_webhooks failed: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def mark_failed_webhook_retried(payload_id: int, success: bool,
                                result: str = None) -> bool:
    """Mark a failed webhook payload as retried."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE failed_webhook_payloads
            SET retried = TRUE,
                retry_result = %s,
                retried_at = NOW()
            WHERE id = %s
        """, (result or ("success" if success else "failed"), payload_id))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        logger.error(f"mark_failed_webhook_retried failed for id {payload_id}: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            return_db_connection(conn)


def init_db() -> bool:
    """Initialize the MASTER subscribers table and all supporting tables."""
    conn = get_db_connection()
    if not conn:
        logger.critical("Cannot initialize DB: connection failed")
        return False
    try:
        cur = conn.cursor()
        
        # 1. THE MASTER TABLE (Merged Users + Subscribers)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                location_id TEXT PRIMARY KEY,
                email TEXT UNIQUE,
                password_hash TEXT,
                full_name TEXT,
                phone TEXT,
                bio TEXT,
                role TEXT DEFAULT 'individual',

                bot_first_name TEXT DEFAULT 'Grok',
                access_token TEXT,
                refresh_token TEXT,
                token_expires_at TIMESTAMP,
                token_type TEXT DEFAULT 'Bearer',
                timezone TEXT DEFAULT 'America/Chicago',
                crm_user_id TEXT,
                calendar_id TEXT,
                calendar_name TEXT,
                initial_message TEXT,
                parent_agency_email TEXT,
                subscription_tier TEXT DEFAULT 'individual',
                confirmation_code TEXT,
                stripe_customer_id TEXT,

                agent_email TEXT,
                invite_token TEXT,
                invite_sent_at TIMESTAMP,
                invite_claimed_at TIMESTAMP,
                onboarding_status TEXT DEFAULT 'pending',
                oauth_app_type TEXT DEFAULT 'marketplace',
                personal_website TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 1b. Agency Billing/Owners Table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agency_billing (
                agency_email TEXT PRIMARY KEY,
                location_id TEXT UNIQUE,
                password_hash TEXT,
                full_name TEXT,
                phone TEXT,
                bio TEXT,
                role TEXT DEFAULT 'agency_owner',
               
                bot_first_name TEXT DEFAULT 'Grok',
                access_token TEXT,
                refresh_token TEXT,
                token_expires_at TIMESTAMP,
                token_type TEXT DEFAULT 'Bearer',
                timezone TEXT DEFAULT 'America/Chicago',
                crm_user_id TEXT,
                calendar_id TEXT,
                calendar_name TEXT,
                initial_message TEXT,
                subscription_tier TEXT DEFAULT 'agency_starter',
                max_seats INTEGER DEFAULT 10,
                active_seats INTEGER DEFAULT 0,
                stripe_customer_id TEXT,
                oauth_app_type TEXT DEFAULT 'marketplace',
                personal_website TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 2. Messages Table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contact_messages (
                id SERIAL PRIMARY KEY,
                contact_id TEXT NOT NULL,
                message_type TEXT NOT NULL CHECK (message_type IN ('lead', 'assistant')),
                message_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_messages_contact_id ON contact_messages (contact_id);")
        
        # 3. Facts Table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contact_facts (
                id SERIAL PRIMARY KEY,
                contact_id TEXT NOT NULL,
                fact_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(contact_id, fact_text)
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_facts_contact_id ON contact_facts (contact_id);")

        # 4. Webhook Deduplication
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_webhooks (
                webhook_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 5. CRITICAL FIX: Contact Narratives Table (was missing!)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contact_narratives (
                contact_id TEXT PRIMARY KEY,
                story_narrative TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_narratives_updated ON contact_narratives (updated_at);")

        # 6. MIGRATION: Add oauth_app_type column to existing tables
        try:
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS oauth_app_type TEXT DEFAULT 'marketplace'
            """)
            logger.info("✅ Migration: Added oauth_app_type to subscribers")
        except Exception as e:
            logger.debug(f"oauth_app_type column may already exist in subscribers: {e}")

        try:
            cur.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS oauth_app_type TEXT DEFAULT 'marketplace'
            """)
            logger.info("✅ Migration: Added oauth_app_type to agency_billing")
        except Exception as e:
            logger.debug(f"oauth_app_type column may already exist in agency_billing: {e}")

        # 6b. MIGRATION: Ensure agency_billing has role and stripe_status columns
        for col_name, col_def in [
            ("role", "TEXT DEFAULT 'agency_owner'"),
            ("stripe_status", "TEXT"),
        ]:
            try:
                cur.execute(f"ALTER TABLE agency_billing ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
            except Exception:
                conn.rollback()

        # 7. MIGRATION: Add calendar_name column to both tables
        try:
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS calendar_name TEXT
            """)
            logger.info("✅ Migration: Added calendar_name to subscribers")
        except Exception as e:
            logger.debug(f"calendar_name column may already exist in subscribers: {e}")

        try:
            cur.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS calendar_name TEXT
            """)
            logger.info("✅ Migration: Added calendar_name to agency_billing")
        except Exception as e:
            logger.debug(f"calendar_name column may already exist in agency_billing: {e}")

        # 8. MIGRATION: Replace unique_msg_content constraint
        # Old constraint blocked BOTH lead and assistant duplicates.
        # Leads CAN and DO repeat themselves (e.g. "I'm not interested" sent multiple times).
        # But assistant duplicates should still be blocked to prevent the bot from looping.
        try:
            # Drop the old constraint that blocked everything
            cur.execute("""
                ALTER TABLE contact_messages
                DROP CONSTRAINT IF EXISTS unique_msg_content
            """)
            # Drop old partial index if it exists from a previous migration
            cur.execute("DROP INDEX IF EXISTS unique_assistant_msg")
            # Add a partial unique index ONLY for assistant messages (anti-looping)
            cur.execute("""
                CREATE UNIQUE INDEX unique_assistant_msg
                ON contact_messages (contact_id, message_text, message_type)
                WHERE message_type = 'assistant'
            """)
            logger.info("✅ Migration: Replaced unique_msg_content with assistant-only constraint")
        except Exception as e:
            logger.debug(f"unique_msg_content migration note: {e}")

        # MIGRATION: Add stage column to contact_messages for stage history tracking
        try:
            cur.execute("""
                ALTER TABLE contact_messages ADD COLUMN IF NOT EXISTS stage TEXT
            """)
            logger.info("✅ Migration: Added stage column to contact_messages")
        except Exception as e:
            logger.debug(f"stage column migration note: {e}")

        conn.commit()
        logger.info("Database initialized: All tables ready (including contact_narratives).")

        # 9. MIGRATION: Drop any NOT NULL constraints on columns that should allow NULL
        # These columns are populated later during OAuth/config, not at account creation.
        try:
            cur2 = conn.cursor()
            for col in ['crm_user_id', 'calendar_id', 'bot_first_name', 'timezone',
                        'access_token', 'refresh_token', 'initial_message', 'calendar_name']:
                try:
                    cur2.execute(f"ALTER TABLE subscribers ALTER COLUMN {col} DROP NOT NULL")
                except Exception:
                    conn.rollback()  # Each ALTER is its own mini-transaction
            conn.commit()
            cur2.close()
            logger.info("✅ Migration: Ensured nullable columns on subscribers")
        except Exception as e:
            logger.debug(f"Nullable column migration note: {e}")

        # 10. MIGRATION: Add onboarding_status column if missing
        # The CREATE TABLE schema includes it, but tables created before it was added won't have it.
        try:
            cur3 = conn.cursor()
            cur3.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS onboarding_status TEXT DEFAULT 'pending'
            """)
            conn.commit()
            cur3.close()
            logger.info("✅ Migration: Ensured onboarding_status column on subscribers")
        except Exception as e:
            logger.debug(f"onboarding_status migration note: {e}")

        # 11. MIGRATION: Add crm_type and crm_config columns for multi-CRM support
        # crm_type: "ghl" (default), "zapier", "salesforce", "hubspot", "pipedrive", "zoho", "insureio"
        # crm_config: JSON blob with CRM-specific settings (webhook URLs, API keys, etc.)
        try:
            cur4 = conn.cursor()
            cur4.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS crm_type TEXT DEFAULT 'ghl'
            """)
            cur4.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS crm_config JSONB DEFAULT '{}'::jsonb
            """)
            cur4.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS crm_type TEXT DEFAULT 'ghl'
            """)
            cur4.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS crm_config JSONB DEFAULT '{}'::jsonb
            """)
            conn.commit()
            cur4.close()
            logger.info("✅ Migration: Added crm_type and crm_config columns for multi-CRM support")
        except Exception as e:
            logger.debug(f"crm_type/crm_config migration note: {e}")

        # 12. MIGRATION: Add personal_website column for agent website sharing
        try:
            cur_pw = conn.cursor()
            cur_pw.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS personal_website TEXT
            """)
            cur_pw.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS personal_website TEXT
            """)
            conn.commit()
            cur_pw.close()
            logger.info("✅ Migration: Added personal_website column to subscribers and agency_billing")
        except Exception as e:
            logger.debug(f"personal_website migration note: {e}")

        # 13. MIGRATION: Create webhook_logs table for per-subscriber activity logging
        try:
            cur5 = conn.cursor()
            cur5.execute("""
                CREATE TABLE IF NOT EXISTS webhook_logs (
                    id SERIAL PRIMARY KEY,
                    location_id TEXT NOT NULL,
                    contact_id TEXT,
                    event_type TEXT NOT NULL,
                    status TEXT DEFAULT 'info',
                    summary TEXT,
                    details JSONB DEFAULT '{}'::jsonb,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur5.execute("CREATE INDEX IF NOT EXISTS idx_webhook_logs_location ON webhook_logs(location_id)")
            cur5.execute("CREATE INDEX IF NOT EXISTS idx_webhook_logs_created ON webhook_logs(created_at DESC)")
            cur5.execute("CREATE INDEX IF NOT EXISTS idx_webhook_logs_event ON webhook_logs(event_type)")
            conn.commit()
            cur5.close()
            logger.info("✅ Migration: Created webhook_logs table")
        except Exception as e:
            logger.debug(f"webhook_logs migration note: {e}")

        # 14. MIGRATION: Create persistent_alerts table for dashboard notifications
        try:
            cur6 = conn.cursor()
            cur6.execute("""
                CREATE TABLE IF NOT EXISTS persistent_alerts (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    location_id TEXT,
                    alert_type TEXT NOT NULL,
                    severity TEXT DEFAULT 'warning',
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    dismissed BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur6.execute("""
                CREATE INDEX IF NOT EXISTS idx_persistent_alerts_email
                ON persistent_alerts(email, dismissed)
            """)
            conn.commit()
            cur6.close()
            logger.info("✅ Migration: Created persistent_alerts table")
        except Exception as e:
            logger.debug(f"persistent_alerts migration note: {e}")

        # 15. MIGRATION: Add install tracking + reminder columns for email sequences
        try:
            cur7 = conn.cursor()
            for col, default in [
                ("install_completed_at", "NULL"),
                ("reminder_24h_sent", "FALSE"),
                ("reminder_72h_sent", "FALSE"),
            ]:
                for table in ["subscribers", "agency_billing"]:
                    try:
                        if default == "FALSE":
                            cur7.execute(f"ALTER TABLE {table} ADD COLUMN {col} BOOLEAN DEFAULT FALSE")
                        else:
                            cur7.execute(f"ALTER TABLE {table} ADD COLUMN {col} TIMESTAMP")
                        conn.commit()
                    except psycopg2.Error:
                        conn.rollback()
            # Backfill: set install_completed_at = created_at for existing users
            # so they get picked up by the reminder system
            for table, email_col in [("subscribers", "email"), ("agency_billing", "agency_email")]:
                try:
                    cur7.execute(f"""
                        UPDATE {table}
                        SET install_completed_at = created_at
                        WHERE install_completed_at IS NULL
                          AND access_token IS NOT NULL
                    """)
                    conn.commit()
                    if cur7.rowcount > 0:
                        logger.info(f"✅ Backfilled install_completed_at for {cur7.rowcount} rows in {table}")
                except psycopg2.Error:
                    conn.rollback()

            cur7.close()
            logger.info("✅ Migration: Added install tracking + reminder columns")
        except Exception as e:
            logger.debug(f"reminder columns migration note: {e}")

        # 16. MIGRATION: Create marketplace_installs table for capturing GHL installs
        # This captures install events even if OAuth callback never fires (e.g., broken redirect URL)
        try:
            cur8 = conn.cursor()
            cur8.execute("""
                CREATE TABLE IF NOT EXISTS marketplace_installs (
                    id SERIAL PRIMARY KEY,
                    app_id TEXT,
                    company_id TEXT,
                    location_id TEXT,
                    user_id TEXT,
                    user_email TEXT,
                    user_name TEXT,
                    plan_id TEXT,
                    install_type TEXT,
                    raw_payload JSONB DEFAULT '{}'::jsonb,
                    oauth_completed BOOLEAN DEFAULT FALSE,
                    oauth_completed_at TIMESTAMP,
                    setup_email_sent BOOLEAN DEFAULT FALSE,
                    setup_email_sent_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur8.execute("CREATE INDEX IF NOT EXISTS idx_mkt_installs_company ON marketplace_installs(company_id)")
            cur8.execute("CREATE INDEX IF NOT EXISTS idx_mkt_installs_location ON marketplace_installs(location_id)")
            cur8.execute("CREATE INDEX IF NOT EXISTS idx_mkt_installs_email ON marketplace_installs(user_email)")
            cur8.execute("CREATE INDEX IF NOT EXISTS idx_mkt_installs_created ON marketplace_installs(created_at DESC)")
            conn.commit()
            cur8.close()
            logger.info("✅ Migration: Created marketplace_installs table")
        except Exception as e:
            logger.debug(f"marketplace_installs migration note: {e}")

        # 17. MIGRATION: Add contracted_carriers column for per-agent carrier selection
        try:
            cur9 = conn.cursor()
            cur9.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS contracted_carriers JSONB DEFAULT '[]'
            """)
            cur9.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS contracted_carriers JSONB DEFAULT '[]'
            """)
            conn.commit()
            cur9.close()
            logger.info("✅ Migration: Added contracted_carriers column to subscribers and agency_billing")
        except Exception as e:
            logger.debug(f"contracted_carriers migration note: {e}")

        # 18. MIGRATION: Add bot_settings JSONB column for advanced settings
        try:
            cur10 = conn.cursor()
            cur10.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS bot_settings JSONB DEFAULT '{}'::jsonb
            """)
            cur10.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS bot_settings JSONB DEFAULT '{}'::jsonb
            """)
            conn.commit()
            cur10.close()
            logger.info("✅ Migration: Added bot_settings column to subscribers and agency_billing")
        except Exception as e:
            logger.debug(f"bot_settings migration note: {e}")

        # 19. MIGRATION: API Platform — api_key, webhook, usage logging
        try:
            cur11 = conn.cursor()
            # Add API columns to subscribers
            cur11.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS api_key TEXT UNIQUE,
                ADD COLUMN IF NOT EXISTS api_key_created_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS outbound_webhook_url TEXT,
                ADD COLUMN IF NOT EXISTS webhook_secret TEXT
            """)
            # Add API columns to agency_billing too
            cur11.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS api_key TEXT UNIQUE,
                ADD COLUMN IF NOT EXISTS api_key_created_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS outbound_webhook_url TEXT,
                ADD COLUMN IF NOT EXISTS webhook_secret TEXT
            """)
            # API usage log table for rate limiting and analytics
            cur11.execute("""
                CREATE TABLE IF NOT EXISTS api_usage_logs (
                    id SERIAL PRIMARY KEY,
                    api_key_prefix TEXT NOT NULL,
                    location_id TEXT,
                    endpoint TEXT NOT NULL,
                    method TEXT DEFAULT 'POST',
                    status_code INTEGER,
                    response_time_ms INTEGER,
                    contact_id TEXT,
                    ip_address TEXT,
                    user_agent TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur11.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_usage_created
                ON api_usage_logs (created_at DESC)
            """)
            cur11.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_usage_key_prefix
                ON api_usage_logs (api_key_prefix, created_at DESC)
            """)
            conn.commit()
            cur11.close()
            logger.info("✅ Migration: Added API platform columns and api_usage_logs table")
        except Exception as e:
            logger.debug(f"API platform migration note: {e}")

        # 20. MIGRATION: Add voice_config JSONB column for AI Voice calling
        try:
            cur_voice = conn.cursor()
            cur_voice.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS voice_config JSONB DEFAULT '{}'::jsonb
            """)
            cur_voice.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS voice_config JSONB DEFAULT '{}'::jsonb
            """)
            conn.commit()
            cur_voice.close()
            logger.info("✅ Migration: Added voice_config column to subscribers and agency_billing")
        except Exception as e:
            logger.debug(f"voice_config migration note: {e}")

        # 21. MIGRATION: Create call_history table for AI Voice call tracking
        try:
            cur_calls = conn.cursor()
            cur_calls.execute("""
                CREATE TABLE IF NOT EXISTS call_history (
                    id SERIAL PRIMARY KEY,
                    location_id TEXT NOT NULL,
                    contact_id TEXT,
                    contact_name TEXT,
                    phone TEXT NOT NULL,
                    direction TEXT DEFAULT 'outbound',
                    call_sid TEXT UNIQUE,
                    status TEXT DEFAULT 'initiated',
                    duration INTEGER DEFAULT 0,
                    recording_url TEXT,
                    recording_sid TEXT,
                    transcript JSONB DEFAULT '[]'::jsonb,
                    started_at TIMESTAMP DEFAULT NOW(),
                    ended_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur_calls.execute("CREATE INDEX IF NOT EXISTS idx_call_history_location ON call_history(location_id)")
            cur_calls.execute("CREATE INDEX IF NOT EXISTS idx_call_history_call_sid ON call_history(call_sid)")
            cur_calls.execute("CREATE INDEX IF NOT EXISTS idx_call_history_location_contact ON call_history(location_id, contact_id)")
            # Add disposition column (migration for existing tables)
            cur_calls.execute("ALTER TABLE call_history ADD COLUMN IF NOT EXISTS disposition TEXT DEFAULT NULL")
            # Add callback_at for AI auto-callback scheduling
            cur_calls.execute("ALTER TABLE call_history ADD COLUMN IF NOT EXISTS callback_at TIMESTAMP DEFAULT NULL")
            # Add STIR/SHAKEN attestation tracking
            cur_calls.execute("ALTER TABLE call_history ADD COLUMN IF NOT EXISTS stir_status TEXT DEFAULT NULL")
            conn.commit()
            cur_calls.close()
            logger.info("✅ Migration: Created call_history table")
        except Exception as e:
            logger.debug(f"call_history migration note: {e}")

        # 22. MIGRATION: AI Minutes marketplace — balances, purchases, usage logs
        try:
            cur_aim = conn.cursor()
            cur_aim.execute("""
                CREATE TABLE IF NOT EXISTS ai_minute_balances (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    balance_minutes INTEGER NOT NULL DEFAULT 0,
                    total_purchased INTEGER NOT NULL DEFAULT 0,
                    total_used INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur_aim.execute("""
                CREATE TABLE IF NOT EXISTS ai_minute_purchases (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    stripe_session_id TEXT UNIQUE,
                    stripe_payment_intent TEXT,
                    package_minutes INTEGER NOT NULL,
                    package_label TEXT,
                    amount_cents INTEGER,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP
                )
            """)
            cur_aim.execute("""
                CREATE TABLE IF NOT EXISTS ai_minute_usage_logs (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    call_sid TEXT,
                    phone TEXT,
                    direction TEXT DEFAULT 'outbound',
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    minutes_deducted INTEGER NOT NULL DEFAULT 0,
                    balance_after INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur_aim.execute("CREATE INDEX IF NOT EXISTS idx_aim_balances_email ON ai_minute_balances(email)")
            cur_aim.execute("CREATE INDEX IF NOT EXISTS idx_aim_purchases_email ON ai_minute_purchases(email)")
            cur_aim.execute("CREATE INDEX IF NOT EXISTS idx_aim_purchases_session ON ai_minute_purchases(stripe_session_id)")
            cur_aim.execute("CREATE INDEX IF NOT EXISTS idx_aim_usage_email ON ai_minute_usage_logs(email)")
            cur_aim.execute("CREATE INDEX IF NOT EXISTS idx_aim_usage_created ON ai_minute_usage_logs(created_at DESC)")
            conn.commit()
            cur_aim.close()
            logger.info("✅ Migration: Created AI minutes tables (balances, purchases, usage_logs)")
        except Exception as e:
            logger.debug(f"AI minutes migration note: {e}")

        # ── Discord Integration tables ──────────────────────────────────────
        try:
            cur_discord = conn.cursor()
            cur_discord.execute("""
                CREATE TABLE IF NOT EXISTS discord_connections (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    discord_user_id TEXT NOT NULL,
                    username TEXT,
                    global_name TEXT,
                    avatar TEXT,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT,
                    token_expires_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur_discord.execute("""
                CREATE TABLE IF NOT EXISTS discord_servers (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    guild_name TEXT NOT NULL,
                    guild_icon TEXT,
                    position INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(email, guild_id)
                )
            """)
            cur_discord.execute("CREATE INDEX IF NOT EXISTS idx_discord_conn_email ON discord_connections(email)")
            cur_discord.execute("CREATE INDEX IF NOT EXISTS idx_discord_servers_email ON discord_servers(email)")
            # Webhook-based channel connections (no bot required)
            cur_discord.execute("""
                CREATE TABLE IF NOT EXISTS discord_webhook_channels (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    guild_name TEXT NOT NULL,
                    guild_icon TEXT,
                    channel_id TEXT NOT NULL,
                    channel_name TEXT NOT NULL,
                    webhook_id TEXT,
                    webhook_token TEXT,
                    webhook_url TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(email, channel_id)
                )
            """)
            cur_discord.execute("CREATE INDEX IF NOT EXISTS idx_discord_wh_email ON discord_webhook_channels(email)")
            conn.commit()
            cur_discord.close()
            logger.info("✅ Migration: Discord integration tables ready")
        except Exception as e:
            logger.debug(f"Discord migration note: {e}")

        # 23. MIGRATION: Create failed_webhook_payloads table for token-failure recovery
        try:
            cur_fwp = conn.cursor()
            cur_fwp.execute("""
                CREATE TABLE IF NOT EXISTS failed_webhook_payloads (
                    id SERIAL PRIMARY KEY,
                    location_id TEXT NOT NULL,
                    contact_id TEXT,
                    payload JSONB NOT NULL,
                    failure_reason TEXT NOT NULL,
                    retried BOOLEAN DEFAULT FALSE,
                    retry_result TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    retried_at TIMESTAMP
                )
            """)
            cur_fwp.execute("CREATE INDEX IF NOT EXISTS idx_fwp_location_retried ON failed_webhook_payloads(location_id, retried)")
            cur_fwp.execute("CREATE INDEX IF NOT EXISTS idx_fwp_created ON failed_webhook_payloads(created_at)")
            conn.commit()
            cur_fwp.close()
            logger.info("✅ Migration: Created failed_webhook_payloads table")
        except Exception as e:
            logger.debug(f"failed_webhook_payloads migration note: {e}")

        # ── App settings table (encryption keys, etc.) ────────────────────────
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            logger.info("✅ Migration: app_settings table ready")
        except Exception as e:
            logger.debug(f"app_settings migration note: {e}")

        # ── Super Admin role migration ──────────────────────────────────────
        # Ensures the platform owner always has super_admin role on every deploy.
        try:
            cur.execute("""
                UPDATE subscribers
                SET role = 'super_admin'
                WHERE email = 'mitchell_vandusen@hotmail.com'
                  AND role != 'super_admin'
            """)
            cur.execute("""
                UPDATE agency_billing
                SET role = 'super_admin'
                WHERE agency_email = 'mitchell_vandusen@hotmail.com'
                  AND role != 'super_admin'
            """)
            conn.commit()
            logger.info("✅ Migration: super_admin role ensured for platform owner")
        except Exception as e:
            logger.debug(f"super_admin migration note: {e}")

        # ── Contact cache table for fast dialer loading ─────────────────────
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contact_cache (
                    location_id TEXT NOT NULL,
                    contact_id TEXT NOT NULL,
                    name TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    phone TEXT,
                    email TEXT,
                    tags JSONB DEFAULT '[]',
                    date_added TEXT,
                    synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (location_id, contact_id)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_cache_loc ON contact_cache (location_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_cache_search ON contact_cache (location_id, lower(name) text_pattern_ops)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_cache_phone ON contact_cache (location_id, phone)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_cache_synced ON contact_cache (location_id, synced_at DESC)")
            conn.commit()
            logger.info("✅ Migration: contact_cache table ready")
        except Exception as e:
            conn.rollback()
            logger.debug(f"contact_cache migration note: {e}")

        # MIGRATION: Add dnd column to contact_cache
        try:
            cur.execute("ALTER TABLE contact_cache ADD COLUMN IF NOT EXISTS dnd BOOLEAN DEFAULT FALSE")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.debug(f"contact_cache dnd migration note: {e}")

        # MIGRATION: Add assigned_to column to contact_cache for seat user filtering
        try:
            cur.execute("ALTER TABLE contact_cache ADD COLUMN IF NOT EXISTS assigned_to TEXT DEFAULT NULL")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_cache_assigned ON contact_cache (location_id, assigned_to)")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.debug(f"contact_cache assigned_to migration note: {e}")

        # ── CRM Sync Engine tables (renamed from ghl_* for multi-CRM support) ──
        # Phase 1: crm_conversations — stores ALL CRM conversation messages locally
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_conversations (
                    id SERIAL PRIMARY KEY,
                    location_id TEXT NOT NULL,
                    contact_id TEXT NOT NULL,
                    contact_name TEXT,
                    contact_phone TEXT,
                    conversation_id TEXT,
                    message_type TEXT DEFAULT 'sms',
                    direction TEXT DEFAULT 'inbound',
                    body TEXT,
                    source TEXT DEFAULT 'ghl_native',
                    external_message_id TEXT UNIQUE NOT NULL,
                    crm_source TEXT DEFAULT 'ghl',
                    date_added TEXT,
                    synced_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_conv_location ON crm_conversations(location_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_conv_contact ON crm_conversations(location_id, contact_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_conv_type ON crm_conversations(location_id, message_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_conv_source ON crm_conversations(location_id, source)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_conv_date ON crm_conversations(location_id, date_added DESC)")
            conn.commit()
            logger.info("✅ Migration: Created crm_conversations table")
        except Exception as e:
            conn.rollback()
            logger.debug(f"crm_conversations migration note: {e}")

        # Rename legacy ghl_conversations → crm_conversations if it exists
        try:
            cur.execute("""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ghl_conversations')
                       AND NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'crm_conversations') THEN
                        ALTER TABLE ghl_conversations RENAME TO crm_conversations;
                    END IF;
                    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'crm_conversations' AND column_name = 'ghl_message_id') THEN
                        ALTER TABLE crm_conversations RENAME COLUMN ghl_message_id TO external_message_id;
                    END IF;
                END $$;
            """)
            cur.execute("ALTER TABLE crm_conversations ADD COLUMN IF NOT EXISTS crm_source TEXT DEFAULT 'ghl'")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.debug(f"crm_conversations rename migration note: {e}")

        # Phase 1: crm_deals — synced pipeline deals from any CRM
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_deals (
                    id SERIAL PRIMARY KEY,
                    location_id TEXT NOT NULL,
                    contact_id TEXT,
                    pipeline_id TEXT,
                    pipeline_name TEXT,
                    stage_id TEXT,
                    stage_name TEXT,
                    status TEXT DEFAULT 'open',
                    monetary_value NUMERIC DEFAULT 0,
                    source TEXT,
                    assigned_to TEXT,
                    external_deal_id TEXT UNIQUE NOT NULL,
                    crm_source TEXT DEFAULT 'ghl',
                    created_at_ghl TEXT,
                    updated_at_ghl TEXT,
                    synced_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_deals_location ON crm_deals(location_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_deals_contact ON crm_deals(location_id, contact_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_deals_pipeline ON crm_deals(location_id, pipeline_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_deals_status ON crm_deals(location_id, status)")
            conn.commit()
            logger.info("✅ Migration: Created crm_deals table")
        except Exception as e:
            conn.rollback()
            logger.debug(f"crm_deals migration note: {e}")

        # Rename legacy ghl_opportunities → crm_deals if it exists
        try:
            cur.execute("""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ghl_opportunities')
                       AND NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'crm_deals') THEN
                        ALTER TABLE ghl_opportunities RENAME TO crm_deals;
                    END IF;
                    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'crm_deals' AND column_name = 'ghl_opportunity_id') THEN
                        ALTER TABLE crm_deals RENAME COLUMN ghl_opportunity_id TO external_deal_id;
                    END IF;
                END $$;
            """)
            cur.execute("ALTER TABLE crm_deals ADD COLUMN IF NOT EXISTS crm_source TEXT DEFAULT 'ghl'")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.debug(f"crm_deals rename migration note: {e}")

        # Phase 1: crm_sync_state — tracks incremental sync progress per CRM
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS crm_sync_state (
                    id SERIAL PRIMARY KEY,
                    location_id TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    crm_source TEXT DEFAULT 'ghl',
                    last_sync_at TIMESTAMP,
                    last_cursor TEXT,
                    sync_status TEXT DEFAULT 'idle',
                    error_message TEXT,
                    total_synced INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE (location_id, resource_type)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_sync_location ON crm_sync_state(location_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crm_sync_status ON crm_sync_state(sync_status)")
            conn.commit()
            logger.info("✅ Migration: Created crm_sync_state table")
        except Exception as e:
            conn.rollback()
            logger.debug(f"crm_sync_state migration note: {e}")

        # Rename legacy ghl_sync_state → crm_sync_state if it exists
        try:
            cur.execute("""
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ghl_sync_state')
                       AND NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'crm_sync_state') THEN
                        ALTER TABLE ghl_sync_state RENAME TO crm_sync_state;
                    END IF;
                END $$;
            """)
            cur.execute("ALTER TABLE crm_sync_state ADD COLUMN IF NOT EXISTS crm_source TEXT DEFAULT 'ghl'")
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.debug(f"crm_sync_state rename migration note: {e}")

        # ── Universal contacts table (multi-CRM + standalone mode) ────────
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contacts (
                    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    location_id TEXT NOT NULL,
                    external_id TEXT,
                    crm_source TEXT DEFAULT 'standalone',
                    first_name TEXT,
                    last_name TEXT,
                    email TEXT,
                    phone TEXT,
                    address TEXT,
                    city TEXT,
                    state TEXT,
                    zip TEXT,
                    tags JSONB DEFAULT '[]',
                    custom_fields JSONB DEFAULT '{}',
                    pipeline_stage TEXT,
                    date_added TIMESTAMP DEFAULT NOW(),
                    last_activity_at TIMESTAMP,
                    do_not_contact BOOLEAN DEFAULT FALSE,
                    UNIQUE(location_id, external_id, crm_source)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_contacts_location ON contacts(location_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_contacts_phone ON contacts(phone)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_contacts_external ON contacts(external_id, crm_source)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_contacts_crm ON contacts(location_id, crm_source)")
            conn.commit()
            logger.info("✅ Migration: Created contacts table")
        except Exception as e:
            conn.rollback()
            logger.debug(f"contacts migration note: {e}")

        # Phase 3: SMS channel selection — sms_send_via column
        try:
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS sms_send_via TEXT DEFAULT 'ghl'
            """)
            cur.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS sms_send_via TEXT DEFAULT 'ghl'
            """)
            conn.commit()
            logger.info("✅ Migration: Added sms_send_via column")
        except Exception as e:
            conn.rollback()
            logger.debug(f"sms_send_via migration note: {e}")

        # 24. MIGRATION: Create number_health table for smart number rotation
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS number_health (
                    id SERIAL PRIMARY KEY,
                    location_id TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    status TEXT DEFAULT 'active',
                    warmup_stage INTEGER DEFAULT 0,
                    health_score NUMERIC(5,1) DEFAULT 75.0,
                    daily_calls_today INTEGER DEFAULT 0,
                    daily_connected INTEGER DEFAULT 0,
                    daily_no_answer INTEGER DEFAULT 0,
                    daily_failed INTEGER DEFAULT 0,
                    daily_busy INTEGER DEFAULT 0,
                    daily_duration_secs INTEGER DEFAULT 0,
                    total_calls INTEGER DEFAULT 0,
                    total_connected INTEGER DEFAULT 0,
                    total_no_answer INTEGER DEFAULT 0,
                    total_failed INTEGER DEFAULT 0,
                    total_busy INTEGER DEFAULT 0,
                    total_duration_secs INTEGER DEFAULT 0,
                    rest_until TIMESTAMP,
                    last_used_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE (location_id, phone)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_number_health_location ON number_health(location_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_number_health_status ON number_health(location_id, status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_number_health_score ON number_health(location_id, health_score DESC)")
            conn.commit()
            logger.info("✅ Migration: Created number_health table")
        except Exception as e:
            conn.rollback()
            logger.debug(f"number_health migration note: {e}")

        # Phase 4: CRM email — separate from login email
        try:
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS crm_email TEXT
            """)
            cur.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS crm_email TEXT
            """)
            conn.commit()
            logger.info("✅ Migration: Added crm_email column")
        except Exception as e:
            conn.rollback()
            logger.debug(f"crm_email migration note: {e}")

        # Phase 5: Carrier blocked tracking for number health
        try:
            cur.execute("""
                ALTER TABLE number_health
                ADD COLUMN IF NOT EXISTS daily_carrier_blocked INTEGER DEFAULT 0
            """)
            cur.execute("""
                ALTER TABLE number_health
                ADD COLUMN IF NOT EXISTS total_carrier_blocked INTEGER DEFAULT 0
            """)
            conn.commit()
            logger.info("✅ Migration: Added carrier_blocked columns to number_health")
        except Exception as e:
            conn.rollback()
            logger.debug(f"carrier_blocked migration note: {e}")

        # Phase 6a: SIP 180 ring confirmation + Voice Insights Advanced
        try:
            cur.execute("ALTER TABLE call_history ADD COLUMN IF NOT EXISTS ring_confirmed BOOLEAN DEFAULT FALSE")
            cur.execute("ALTER TABLE call_history ADD COLUMN IF NOT EXISTS insights JSONB DEFAULT NULL")
            cur.execute("ALTER TABLE call_history ADD COLUMN IF NOT EXISTS pdd_ms INTEGER DEFAULT NULL")
            cur.execute("ALTER TABLE call_history ADD COLUMN IF NOT EXISTS quality_tags TEXT[] DEFAULT NULL")
            cur.execute("ALTER TABLE number_health ADD COLUMN IF NOT EXISTS daily_ring_confirmed INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE number_health ADD COLUMN IF NOT EXISTS total_ring_confirmed INTEGER DEFAULT 0")
            conn.commit()
            logger.info("✅ Migration: Added ring_confirmed + Voice Insights columns")
        except Exception as e:
            conn.rollback()
            logger.debug(f"ring_confirmed/insights migration note: {e}")

        # Phase 6b: from_number tracking + advanced number health signals
        try:
            # Track which caller ID was used for each outbound call
            cur.execute("ALTER TABLE call_history ADD COLUMN IF NOT EXISTS from_number TEXT DEFAULT NULL")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_call_history_from_number ON call_history(from_number) WHERE from_number IS NOT NULL")
            # Voice Insights aggregate signals on number_health
            cur.execute("ALTER TABLE number_health ADD COLUMN IF NOT EXISTS avg_pdd_ms NUMERIC(8,1) DEFAULT NULL")
            cur.execute("ALTER TABLE number_health ADD COLUMN IF NOT EXISTS recent_pdd_trend NUMERIC(8,1) DEFAULT NULL")
            cur.execute("ALTER TABLE number_health ADD COLUMN IF NOT EXISTS insights_quality_issues INTEGER DEFAULT 0")
            cur.execute("ALTER TABLE number_health ADD COLUMN IF NOT EXISTS stir_a_rate NUMERIC(5,2) DEFAULT NULL")
            cur.execute("ALTER TABLE number_health ADD COLUMN IF NOT EXISTS carrier_block_velocity NUMERIC(5,2) DEFAULT 0")
            conn.commit()
            logger.info("✅ Migration: Added from_number + advanced health signal columns")
        except Exception as e:
            conn.rollback()
            logger.debug(f"from_number/advanced health migration note: {e}")

        # Phase 6: Google Calendar integration config
        try:
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS google_calendar_config JSONB DEFAULT '{}'::jsonb
            """)
            cur.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS google_calendar_config JSONB DEFAULT '{}'::jsonb
            """)
            conn.commit()
            logger.info("✅ Migration: Added google_calendar_config column")
        except Exception as e:
            conn.rollback()
            logger.debug(f"google_calendar_config migration note: {e}")

        # ── Slack Integration tables ──────────────────────────────────────
        try:
            cur_slack = conn.cursor()
            cur_slack.execute("""
                CREATE TABLE IF NOT EXISTS slack_connections (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    slack_team_id TEXT NOT NULL,
                    slack_team_name TEXT,
                    slack_user_id TEXT,
                    bot_token TEXT NOT NULL,
                    user_token TEXT,
                    authed_user_name TEXT,
                    bot_user_id TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur_slack.execute("""
                CREATE TABLE IF NOT EXISTS slack_workspaces (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    team_id TEXT NOT NULL,
                    team_name TEXT NOT NULL,
                    team_icon TEXT,
                    position INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(email, team_id)
                )
            """)
            cur_slack.execute("CREATE INDEX IF NOT EXISTS idx_slack_conn_email ON slack_connections(email)")
            cur_slack.execute("CREATE INDEX IF NOT EXISTS idx_slack_ws_email ON slack_workspaces(email)")
            conn.commit()
            cur_slack.close()
            logger.info("✅ Migration: Slack integration tables ready")
        except Exception as e:
            logger.debug(f"Slack migration note: {e}")

        # 25. MIGRATION: Create uninstall_feedback table for app uninstall feedback tracking
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS uninstall_feedback (
                    id SERIAL PRIMARY KEY,
                    location_id TEXT,
                    company_id TEXT,
                    user_email TEXT,
                    user_name TEXT,
                    reason TEXT,
                    other_text TEXT,
                    raw_webhook_payload JSONB DEFAULT '{}'::jsonb,
                    feedback_submitted_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_uninstall_fb_location ON uninstall_feedback(location_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_uninstall_fb_created ON uninstall_feedback(created_at DESC)")
            conn.commit()
            logger.info("✅ Migration: Created uninstall_feedback table")
        except Exception as e:
            conn.rollback()
            logger.debug(f"uninstall_feedback migration note: {e}")

        # 26. MIGRATION: Add preferred_language column for auto-detect i18n
        try:
            cur.execute("""
                ALTER TABLE subscribers
                ADD COLUMN IF NOT EXISTS preferred_language TEXT DEFAULT 'en'
            """)
            cur.execute("""
                ALTER TABLE agency_billing
                ADD COLUMN IF NOT EXISTS preferred_language TEXT DEFAULT 'en'
            """)
            conn.commit()
            logger.info("✅ Migration: Added preferred_language column")
        except Exception as e:
            conn.rollback()
            logger.debug(f"preferred_language migration note: {e}")

        # 27. MIGRATION: Create location_users table for multi-user seat management
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS location_users (
                    id SERIAL PRIMARY KEY,
                    location_id TEXT NOT NULL REFERENCES subscribers(location_id),
                    ghl_user_id TEXT,
                    email TEXT UNIQUE,
                    password_hash TEXT,
                    full_name TEXT,
                    role TEXT DEFAULT 'agent',
                    permissions JSONB DEFAULT '{
                        "can_dial": true,
                        "can_text": true,
                        "can_view_all_leads": false,
                        "can_import_leads": false,
                        "can_change_bot_config": false,
                        "can_view_call_recordings": true,
                        "can_manage_numbers": false,
                        "can_view_billing": false,
                        "can_invite_users": false
                    }'::jsonb,
                    voice_config JSONB DEFAULT '{}'::jsonb,
                    voice_activated BOOLEAN DEFAULT false,
                    invite_token TEXT,
                    invite_sent_at TIMESTAMP,
                    invite_claimed_at TIMESTAMP,
                    onboarding_status TEXT DEFAULT 'pending',
                    is_active BOOLEAN DEFAULT true,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_location_users_location ON location_users(location_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_location_users_email ON location_users(email)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_location_users_ghl ON location_users(ghl_user_id)")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_location_users_loc_ghl ON location_users(location_id, ghl_user_id)")
            conn.commit()
            logger.info("✅ Migration: Created location_users table for multi-user seats")
        except Exception as e:
            conn.rollback()
            logger.debug(f"location_users migration note: {e}")

        # 28. MIGRATION: Add session_revoked_at and stripe_seat_subscription_id to location_users
        try:
            cur.execute("ALTER TABLE location_users ADD COLUMN IF NOT EXISTS session_revoked_at TIMESTAMP")
            cur.execute("ALTER TABLE location_users ADD COLUMN IF NOT EXISTS stripe_seat_subscription_id TEXT")
            conn.commit()
            logger.info("✅ Migration: Added session_revoked_at + stripe columns to location_users")
        except Exception as e:
            conn.rollback()
            logger.debug(f"location_users session columns migration note: {e}")

        # 29. MIGRATION: Create team_audit_log table
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS team_audit_log (
                    id SERIAL PRIMARY KEY,
                    location_id TEXT NOT NULL,
                    actor_email TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_email TEXT,
                    details JSONB,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_team_audit_location ON team_audit_log(location_id, created_at DESC)")
            conn.commit()
            logger.info("✅ Migration: Created team_audit_log table")
        except Exception as e:
            conn.rollback()
            logger.debug(f"team_audit_log migration note: {e}")

        # 30. MIGRATION: Contact imports table for CSV/Excel/TXT import tracking
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS contact_imports (
                    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    location_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    total_rows INTEGER DEFAULT 0,
                    imported INTEGER DEFAULT 0,
                    updated INTEGER DEFAULT 0,
                    skipped INTEGER DEFAULT 0,
                    failed INTEGER DEFAULT 0,
                    duplicate_strategy TEXT DEFAULT 'skip',
                    apply_tags JSONB DEFAULT '[]',
                    column_mapping JSONB DEFAULT '{}',
                    preview_data JSONB DEFAULT '[]',
                    error_log JSONB DEFAULT '[]',
                    file_data JSONB DEFAULT '[]',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    created_by TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_imports_loc ON contact_imports (location_id, created_at DESC)")
            conn.commit()
            logger.info("✅ Migration: Created contact_imports table")
        except Exception as e:
            conn.rollback()
            logger.debug(f"contact_imports migration note: {e}")

        # 31. MIGRATION: Workflow builder tables
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    location_id TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT 'Untitled Workflow',
                    description TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    trigger_type TEXT NOT NULL DEFAULT 'manual',
                    trigger_config JSONB DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_by TEXT,
                    stats JSONB DEFAULT '{"runs": 0, "completed": 0, "failed": 0}'
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_workflows_loc ON workflows (location_id, status)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
                    step_type TEXT NOT NULL,
                    step_subtype TEXT NOT NULL,
                    config JSONB DEFAULT '{}',
                    position_x FLOAT DEFAULT 0,
                    position_y FLOAT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wf_steps_wf ON workflow_steps (workflow_id)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS workflow_connections (
                    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
                    from_step_id TEXT NOT NULL,
                    to_step_id TEXT NOT NULL,
                    branch_key TEXT DEFAULT 'default',
                    sort_order INTEGER DEFAULT 0
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wf_conn_wf ON workflow_connections (workflow_id)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
                    contact_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    current_step_id TEXT,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    next_execute_at TIMESTAMP,
                    error TEXT,
                    context JSONB DEFAULT '{}'
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wf_runs_wf ON workflow_runs (workflow_id, status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wf_runs_contact ON workflow_runs (contact_id, status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wf_runs_next ON workflow_runs (next_execute_at) WHERE status = 'running'")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS workflow_step_logs (
                    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    run_id TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result JSONB DEFAULT '{}',
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wf_logs_run ON workflow_step_logs (run_id, executed_at)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS workflow_custom_actions (
                    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    location_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    step_type TEXT NOT NULL DEFAULT 'action',
                    icon TEXT DEFAULT 'fa-solid fa-puzzle-piece',
                    color TEXT DEFAULT '#00b4ff',
                    config_template JSONB DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_wf_custom_loc ON workflow_custom_actions (location_id)")

            conn.commit()
            logger.info("✅ Migration: Created workflow builder tables (workflows, steps, connections, runs, logs, custom_actions)")
        except Exception as e:
            conn.rollback()
            logger.debug(f"workflow tables migration note: {e}")

        # ── GHL trigger subscriptions ──
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ghl_trigger_subscriptions (
                    id SERIAL PRIMARY KEY,
                    location_id TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    webhook_url TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(location_id, trigger_type)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ghl_trig_loc ON ghl_trigger_subscriptions (location_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ghl_trig_type ON ghl_trigger_subscriptions (trigger_type)")
            conn.commit()
            logger.info("✅ Migration: ghl_trigger_subscriptions table ready")
        except Exception as e:
            conn.rollback()
            logger.debug(f"ghl_trigger_subscriptions migration note: {e}")

        # ── GHL action loops (custom action loop/repeat) ──
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ghl_action_loops (
                    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
                    location_id TEXT NOT NULL,
                    contact_id TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    loop_action TEXT NOT NULL DEFAULT 'ai_sms',
                    message_template TEXT DEFAULT '',
                    duration_days INTEGER NOT NULL DEFAULT 3,
                    interval_hours INTEGER NOT NULL DEFAULT 24,
                    iteration INTEGER NOT NULL DEFAULT 0,
                    max_iterations INTEGER NOT NULL DEFAULT 3,
                    status TEXT NOT NULL DEFAULT 'active',
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    next_execute_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    config JSONB DEFAULT '{}'
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ghl_loops_active ON ghl_action_loops (status, next_execute_at) WHERE status = 'active'")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ghl_loops_loc ON ghl_action_loops (location_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ghl_loops_contact ON ghl_action_loops (location_id, contact_id)")
            conn.commit()
            logger.info("✅ Migration: ghl_action_loops table ready")
        except Exception as e:
            conn.rollback()
            logger.debug(f"ghl_action_loops migration note: {e}")

        # 32. MIGRATION: Add company_id + company metadata to agency_billing and subscribers
        # Supports companyId-based agency detection and auto-linking
        try:
            # agency_billing: company_id + metadata + whitelabel_config
            for col_name, col_def in [
                ("company_id", "TEXT"),
                ("company_name", "TEXT"),
                ("company_owner_name", "TEXT"),
                ("company_owner_email", "TEXT"),
                ("company_owner_phone", "TEXT"),
                ("whitelabel_config", "JSONB DEFAULT '{}'::jsonb"),
            ]:
                cur.execute(f"ALTER TABLE agency_billing ADD COLUMN IF NOT EXISTS {col_name} {col_def}")

            cur.execute("CREATE INDEX IF NOT EXISTS idx_agency_billing_company_id ON agency_billing (company_id)")

            # subscribers: company_id for auto-linking to agency
            cur.execute("ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS company_id TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_subscribers_company_id ON subscribers (company_id)")

            conn.commit()
            logger.info("✅ Migration 32: Added company_id + metadata columns to agency_billing and subscribers")
        except Exception as e:
            conn.rollback()
            logger.debug(f"company_id migration note: {e}")

        return True
    except psycopg2.Error as e:
        logger.critical(f"Database initialization failed: {e}", exc_info=True)
        if conn: conn.rollback()
        return False
    finally:
        if conn:
            cur.close()
            return_db_connection(conn)


class User(UserMixin):
    def __init__(self, data: dict):
        # Core identification (works for both agency_billing and subscribers)
        self.email = data.get('agency_email') or data.get('email')
        self.id = self.email  # Flask-Login requires this

        self.password_hash = data.get('password_hash')

        # Location & GHL identifiers
        self.location_id = data.get('location_id')
        self.crm_email = data.get('crm_email')
        self.ghl_calendar_id = data.get('ghl_calendar_id')
        self.crm_api_key = data.get('crm_api_key')
        self.crm_user_id = data.get('crm_user_id')
        self.calendar_id = data.get('calendar_id')
        self.calendar_name = data.get('calendar_name')

        # Bot configuration
        self.bot_first_name = data.get('bot_first_name', 'Grok')
        self.timezone = data.get('timezone', 'America/Chicago')
        self.initial_message = data.get('initial_message', '')
        self.bot_active = data.get('bot_active')

        # OAuth / Token fields
        self.access_token = data.get('access_token')
        self.refresh_token = data.get('refresh_token')
        self.token_expires_at = data.get('token_expires_at')
        self.token_type = data.get('token_type', 'Bearer')

        # Profile & Misc
        self.full_name = data.get('full_name')
        self.phone = data.get('phone')
        self.bio = data.get('bio')
        self.confirmation_code = data.get('confirmation_code')
        self.role = data.get('role', 'individual')

        # Billing & Subscription
        self.subscription_tier = data.get('subscription_tier', 'individual')
        self.tier = data.get('tier')
        self.stripe_customer_id = data.get('stripe_customer_id')
        self.stripe_status = data.get('stripe_status')

        # Agency linkage
        self.parent_agency_email = data.get('parent_agency_email')
        self.company_id = data.get('company_id')

        # Agency-specific billing/metadata fields
        self.max_seats = data.get('max_seats')
        self.active_seats = data.get('active_seats')
        self.next_billing_date = data.get('next_billing_date')
        self.company_name = data.get('company_name')
        self.company_owner_name = data.get('company_owner_name')
        self.company_owner_email = data.get('company_owner_email')
        self.company_owner_phone = data.get('company_owner_phone')
        self.whitelabel_config = data.get('whitelabel_config') or {}

        # Sub-user onboarding system fields
        self.agent_email = data.get('agent_email')
        self.invite_token = data.get('invite_token')
        self.invite_sent_at = data.get('invite_sent_at')
        self.invite_claimed_at = data.get('invite_claimed_at')
        self.onboarding_status = data.get('onboarding_status', 'pending')

        # Multi-CRM integration fields
        self.crm_type = data.get('crm_type', 'ghl')
        self.crm_config = data.get('crm_config') or {}

        # Agent personal website (shared by bot when lead asks)
        self.personal_website = data.get('personal_website')

        # Voice AI configuration
        self.voice_config = data.get('voice_config') or {}

        # SMS channel selection: 'ghl' (default) or '+1234567890' (Twilio number)
        self.sms_send_via = data.get('sms_send_via', 'ghl')

        # Google Calendar integration
        self.google_calendar_config = data.get('google_calendar_config') or {}

        # i18n — auto-detected from browser Accept-Language or manually overridden
        self.preferred_language = data.get('preferred_language', 'en')

        # Seat user fields (location_users table)
        self.is_seat_user = data.get('_is_seat_user', False)
        self.seat_user_id = data.get('_seat_user_id')
        self.permissions = data.get('permissions') or {}

        # Timestamps
        self.created_at = data.get('created_at')
        self.updated_at = data.get('updated_at')
   
    @property
    def is_agency_owner(self) -> bool:
        return self.role == 'agency_owner'

    @property
    def is_super_admin(self) -> bool:
        return self.role == 'super_admin'

    def has_permission(self, perm: str) -> bool:
        """Check if seat user has a specific permission. Managers/owners always have all permissions."""
        if not self.is_seat_user:
            return True  # Non-seat users (managers/owners) have all permissions
        return bool(self.permissions.get(perm, False))
   
    @staticmethod
    def get(email: str) -> Optional['User']:
        """
        Fetch user from BOTH tables - subscribers first, then agency_billing.
        Returns User object or None if no match.
        
        FIXED: Now checks both tables so agency owners can log in.
        """
        if not email:
            return None

        # Normalize email to lowercase for case-insensitive matching.
        # GHL and Outlook can return mixed-case emails (e.g. "Meghan@Outlook.com")
        # which must match the DB regardless of stored casing.
        email_lookup = email.strip().lower()
        logger.debug(f"User.get called for email: '{email_lookup}'")

        conn = get_db_connection()
        if not conn:
            logger.debug("DB connection failed")
            return None

        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)

            # 1. Check subscribers table first (most users)
            cur.execute("""
                SELECT * FROM subscribers WHERE LOWER(email) = %s LIMIT 1
            """, (email_lookup,))
            row = cur.fetchone()

            if row:
                logger.debug(f"Found user in subscribers table")
                return User(row)

            # 2. Check agency_billing table (agency owners)
            cur.execute("""
                SELECT * FROM agency_billing WHERE LOWER(agency_email) = %s LIMIT 1
            """, (email_lookup,))
            row = cur.fetchone()

            if row:
                logger.debug(f"Found user in agency_billing table")
                return User(row)

            # 3. Check location_users table (seat users)
            try:
                cur.execute("""
                    SELECT lu.*, s.location_id as parent_location_id,
                           s.access_token, s.refresh_token, s.token_expires_at,
                           s.token_type, s.bot_first_name, s.timezone,
                           s.initial_message, s.contracted_carriers,
                           s.bot_settings, s.crm_type, s.crm_config,
                           s.crm_user_id as parent_crm_user_id,
                           s.stripe_customer_id as parent_stripe_customer_id,
                           s.subscription_tier as parent_subscription_tier,
                           s.sms_send_via, s.calendar_id, s.calendar_name
                    FROM location_users lu
                    JOIN subscribers s ON s.location_id = lu.location_id
                    WHERE LOWER(lu.email) = %s AND lu.is_active = true
                    LIMIT 1
                """, (email_lookup,))
                seat_row = cur.fetchone()
                if seat_row:
                    # Check session revocation — if session was created before revocation, deny
                    if seat_row.get('session_revoked_at'):
                        from flask import session as flask_session
                        login_time = flask_session.get('_seat_login_at')
                        if login_time:
                            from datetime import datetime as _dt
                            try:
                                lt = _dt.fromisoformat(login_time) if isinstance(login_time, str) else login_time
                                if lt < seat_row['session_revoked_at']:
                                    logger.info(f"Seat user {email} session revoked")
                                    return None
                            except Exception:
                                pass

                    logger.debug(f"Found seat user in location_users table")
                    data = dict(seat_row)
                    data['_is_seat_user'] = True
                    data['_seat_user_id'] = data.get('id')
                    data['location_id'] = data.get('location_id') or data.get('parent_location_id')
                    data['role'] = data.get('role', 'agent')  # Preserve seat user role
                    data['subscription_tier'] = data.get('parent_subscription_tier', 'individual')
                    data['stripe_customer_id'] = data.get('parent_stripe_customer_id')
                    return User(data)
            except Exception as seat_err:
                logger.debug(f"location_users check note: {seat_err}")

            logger.debug(f"No match found for '{email}' in any table")
            return None
       
        except psycopg2.Error as e:
            logger.error(f"DB error in User.get: {e}")
            return None
       
        finally:
            if 'cur' in locals():
                cur.close()
            if conn:
                return_db_connection(conn)
   
    @staticmethod
    def get_from_agency(email: str) -> Optional['User']:
        """
        Fetch user from the 'agency_billing' table only.
        Returns User object or None if no match.
        """
        if not email:
            return None
            
        logger.debug(f"User.get_from_agency called for email: '{email}'")
       
        conn = get_db_connection()
        if not conn:
            logger.debug("DB connection failed")
            return None
       
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT * FROM agency_billing WHERE agency_email = %s LIMIT 1
            """, (email,))
           
            row = cur.fetchone()
            if row:
                logger.debug(f"Found user in agency_billing")
                return User(row)
            else:
                logger.debug(f"No match in agency_billing for '{email}'")
            return None
       
        except psycopg2.Error as e:
            logger.error(f"DB error in User.get_from_agency: {e}")
            return None
       
        finally:
            if 'cur' in locals():
                cur.close()
            if conn:
                return_db_connection(conn)
   
    @staticmethod
    def create(
        email: str,
        password: Optional[str] = None,
        stripe_customer_id: Optional[str] = None,
        role: str = 'individual',
        location_id: Optional[str] = None
    ) -> bool:
        """
        Creates a new user in the appropriate table.
        For 'agency_owner', use agency_billing; else subscribers.
        """
        password_hash = generate_password_hash(password) if password else None
       
        # If no location_id is provided, generate a temporary one
        if not location_id:
            location_id = f"temp_{uuid.uuid4().hex[:8]}"
            
        conn = get_db_connection()
        if not conn: 
            return False
            
        try:
            cur = conn.cursor()
            if role == 'agency_owner':
                cur.execute(
                    """
                    INSERT INTO agency_billing (agency_email, password_hash, stripe_customer_id, role, location_id)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (email, password_hash, stripe_customer_id, role, location_id)
                )
            else:
                cur.execute(
                    """
                    INSERT INTO subscribers (email, password_hash, stripe_customer_id, role, location_id)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (email, password_hash, stripe_customer_id, role, location_id)
                )
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            logger.warning(f"User.create duplicate email/location for {email}")
            conn.rollback()
            return False
        except psycopg2.Error as e:
            logger.error(f"User.create failed for {email}: {e}")
            conn.rollback()
            return False
        finally:
            if conn:
                cur.close()
                return_db_connection(conn)


# --- Sub-account isolation cleanup ---

def clean_subaccount_contamination() -> dict:
    """
    One-shot cleanup: clear trust_hub protection fields and A2P SID fields
    from ALL sub-account voice_configs that lack a _sub_sid ownership tag.

    This fixes contamination from the old auto-discovery that copied the master
    account's Trust Hub / A2P data into sub-account records.  Safe to call at
    every startup — it's a no-op once data is already clean.
    """
    import json as _json
    master_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    if not master_sid:
        return {"skipped": True, "reason": "TWILIO_ACCOUNT_SID not set"}

    conn = get_db_connection()
    if not conn:
        return {"skipped": True, "reason": "No DB connection"}

    cleaned = 0
    checked = 0
    try:
        cur = conn.cursor()
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
            checked += 1
            email = row['email']
            vc = row['voice_config'] or {}
            if not isinstance(vc, dict):
                try:
                    vc = _json.loads(vc)
                except Exception:
                    continue

            sub_sid = vc.get('twilio_sub_account_sid', '')
            changed = False

            trust_hub = vc.get('trust_hub', {})
            if isinstance(trust_hub, dict):
                th_sub = trust_hub.get('_sub_sid', '')
                if any(trust_hub.get(k) for k in ('protection_active', 'profile_sid', 'business_name')) \
                        and th_sub != sub_sid:
                    for k in ('protection_active', 'profile_sid', 'business_name', 'registered_at', '_sub_sid'):
                        trust_hub.pop(k, None)
                    vc['trust_hub'] = trust_hub
                    changed = True

            a2p = vc.get('a2p', {})
            if isinstance(a2p, dict):
                a2p_sub = a2p.get('_sub_sid', '')
                if any(a2p.get(k) for k in ('brand_sid', 'campaign_sid', 'messaging_service_sid')) \
                        and a2p_sub != sub_sid:
                    for k in ('brand_sid', 'brand_status', 'campaign_sid', 'campaign_status',
                              'messaging_service_sid', 'use_case', 'registered', '_sub_sid'):
                        a2p.pop(k, None)
                    vc['a2p'] = a2p
                    changed = True

            if changed:
                try:
                    upd = conn.cursor()
                    upd.execute(
                        "UPDATE subscribers SET voice_config = %s WHERE email = %s",
                        (_json.dumps(vc), email)
                    )
                    upd.close()
                    conn.commit()
                    cleaned += 1
                    logger.info(f"[startup-cleanup] Cleared contaminated voice_config for {email}")
                except Exception as exc:
                    logger.error(f"[startup-cleanup] Failed to clean {email}: {exc}")
                    try: conn.rollback()
                    except Exception: pass

        result = {"checked": checked, "cleaned": cleaned}
        if cleaned:
            logger.warning(f"[startup-cleanup] Fixed {cleaned}/{checked} contaminated sub-account voice_configs")
        return result
    except Exception as e:
        logger.error(f"[startup-cleanup] Contamination cleanup failed: {e}")
        return {"error": str(e)}
    finally:
        return_db_connection(conn)


# --- Helper Functions ---

def get_subscriber_info_sql(location_id: str) -> Optional[Dict[str, Any]]:
    """Direct SQL lookup for subscriber by location_id."""
    conn = get_db_connection()
    if not conn: 
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscribers WHERE location_id = %s LIMIT 1", (location_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"SQL lookup failed: {e}")
        return None
    finally:
        if 'cur' in locals():
            cur.close()
        if conn:
            return_db_connection(conn)


def get_subscriber_info_hybrid(location_id: str) -> Optional[Dict[str, Any]]:
    """
    Hybrid Fetcher:
    1. SQL (Fastest)
    2. Google Sheets "Subscribers" Tab (Recovery)
    """
    # 1. Primary path: PostgreSQL
    sql_data = get_subscriber_info_sql(location_id)
    if sql_data:
        return sql_data
        
    # 2. Fallback path: Google Sheets
    try:
        import extensions as _ext_gs
        gc = _ext_gs.gc
        sheet_url = _ext_gs.sheet_url
    except Exception:
        gc = None
        sheet_url = None
   
    if not gc or not sheet_url:
        logger.warning("Sheets recovery unavailable: Credentials or URL missing.")
        return None
        
    try:
        logger.info(f"SQL miss for {location_id} — initiating Sheets recovery...")
        sh = gc.open_by_url(sheet_url)
        worksheet = sh.worksheet("Subscribers")
       
        headers = [h.strip().lower() for h in worksheet.row_values(1)]
       
        expected_headers = [
            "location_id", "calendar_id", "access_token", "refresh_token",
            "crm_user_id", "bot_first_name", "timezone", "email", "initial_message",
            "confirmation_code", "stripe_customer_id", "parent_agency_email", "subscription_tier"
        ]
        
        col_map = {}
        for hdr in expected_headers:
            try:
                col_map[hdr] = headers.index(hdr)
            except ValueError:
                if hdr != "subscription_tier":
                    logger.warning(f"Expected header '{hdr}' not found in Subscribers sheet.")
       
        if "location_id" not in col_map:
            logger.error("Critical: 'location_id' column not found in Subscribers sheet.")
            return None
            
        cell = worksheet.find(location_id, in_column=col_map["location_id"] + 1)
        if not cell:
            logger.warning(f"Location {location_id} not found in Google Sheets.")
            return None
       
        row_data = worksheet.row_values(cell.row)
        subscriber = {}
        for hdr, col_idx in col_map.items():
            if col_idx < len(row_data):
                value = row_data[col_idx]
                subscriber[hdr] = None if value == "" else value
       
        logger.info(f"Sheets recovery success for {location_id}")
        return subscriber
    except Exception as e:
        logger.error(f"Sheets recovery failed for {location_id}: {e}", exc_info=True)
        return None

   
def get_message_count(contact_id: str) -> int:
    """Count messages for a contact (detect empty/wiped DB)."""
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM contact_messages WHERE contact_id = %s", (contact_id,))
        result = cur.fetchone()
        # Handle both dict and tuple results
        if isinstance(result, dict):
            return result.get('count', 0)
        return result[0] if result else 0
    except psycopg2.Error as e:
        logger.error(f"get_message_count failed for {contact_id}: {e}")
        return 0
    finally:
        if 'cur' in locals():
            cur.close()
        if conn:
            return_db_connection(conn)


def sync_messages_to_db(contact_id: str, location_id: str, fetched_messages: list) -> int:
    """Bulk sync GHL messages to DB with deduplication."""
    if not contact_id or not fetched_messages:
        return 0
    conn = get_db_connection()
    if not conn:
        return 0
    inserted = 0
    try:
        cur = conn.cursor()
        values = [
            (contact_id, msg['role'], msg['text'].strip())
            for msg in fetched_messages
            if msg.get('text') and msg.get('text').strip()
        ]
        if values:
            execute_values(cur, """
                INSERT INTO contact_messages (contact_id, message_type, message_text)
                VALUES %s
                ON CONFLICT DO NOTHING
            """, values)
            inserted = cur.rowcount
        conn.commit()
        if inserted > 0:
            logger.info(f"Synced {inserted} messages for {contact_id}")
        return inserted
    except psycopg2.Error as e:
        logger.error(f"sync_messages_to_db failed for {contact_id}: {e}")
        conn.rollback()
        return 0
    finally:
        if 'cur' in locals():
            cur.close()
        if conn:
            return_db_connection(conn)


def update_subscriber_token(
    location_id: str,
    access_token: str,
    refresh_token: Optional[str] = None,
    expires_in: int = 86400,
    oauth_app_type: Optional[str] = None
) -> bool:
    """Update OAuth tokens with expiry. Optionally fix oauth_app_type if credential
    auto-detection found the stored type was wrong.

    CRITICAL: GHL refresh tokens are single-use. If this write fails after a
    successful token refresh, the old refresh_token is already invalidated by GHL
    and the new one is lost — causing permanent lockout. This function retries
    up to 3 times on failure to prevent that scenario.
    """
    import time as _t

    for attempt in range(3):
        conn = get_db_connection()
        if not conn:
            if attempt < 2:
                logger.warning(f"update_subscriber_token: no DB connection "
                              f"(attempt {attempt+1}/3), retrying...")
                _t.sleep(1)
                continue
            logger.error(f"update_subscriber_token: no DB connection after 3 attempts "
                        f"for {location_id} — TOKENS MAY BE LOST")
            return False
        try:
            cur = conn.cursor()
            # Acquire advisory lock scoped to this transaction to prevent multiple
            # workers from racing on the same location_id's token refresh.
            # hashtext() maps location_id string → bigint deterministically.
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (location_id,))
            # COALESCE keeps old refresh_token only if GHL didn't return a new one.
            # This is a safety net — GHL almost always returns a new refresh_token.
            if oauth_app_type:
                cur.execute("""
                    UPDATE subscribers
                    SET access_token = %s,
                        refresh_token = COALESCE(%s, refresh_token),
                        token_expires_at = NOW() + interval '%s seconds',
                        oauth_app_type = %s,
                        updated_at = NOW()
                    WHERE location_id = %s
                """, (access_token, refresh_token, expires_in, oauth_app_type, location_id))
            else:
                cur.execute("""
                    UPDATE subscribers
                    SET access_token = %s,
                        refresh_token = COALESCE(%s, refresh_token),
                        token_expires_at = NOW() + interval '%s seconds',
                        updated_at = NOW()
                    WHERE location_id = %s
                """, (access_token, refresh_token, expires_in, location_id))

            updated = cur.rowcount > 0

            # Fallback: agency owners have tokens in agency_billing, not subscribers
            if not updated:
                if oauth_app_type:
                    cur.execute("""
                        UPDATE agency_billing
                        SET access_token = %s,
                            refresh_token = COALESCE(%s, refresh_token),
                            token_expires_at = NOW() + interval '%s seconds',
                            oauth_app_type = %s,
                            updated_at = NOW()
                        WHERE location_id = %s
                    """, (access_token, refresh_token, expires_in, oauth_app_type, location_id))
                else:
                    cur.execute("""
                        UPDATE agency_billing
                        SET access_token = %s,
                            refresh_token = COALESCE(%s, refresh_token),
                            token_expires_at = NOW() + interval '%s seconds',
                            updated_at = NOW()
                        WHERE location_id = %s
                    """, (access_token, refresh_token, expires_in, location_id))
                updated = cur.rowcount > 0

            conn.commit()
            if updated:
                logger.info(f"✅ DB token persisted for {location_id} "
                           f"(refresh_token={'new' if refresh_token else 'kept'})")
            else:
                logger.warning(f"⚠️ DB token update matched 0 rows for {location_id} "
                              f"— location_id not found in subscribers or agency_billing")
            return updated
        except psycopg2.Error as e:
            logger.error(f"update_subscriber_token FAILED for {location_id} "
                        f"(attempt {attempt+1}/3): {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            if attempt < 2:
                _t.sleep(1)
                continue
            logger.error(f"🚨 CRITICAL: Token DB write failed after 3 attempts for {location_id} "
                        f"— refresh_token may be permanently lost")
            return False
        finally:
            if 'cur' in locals():
                cur.close()
            if conn:
                return_db_connection(conn)
    return False


def update_crm_config_token(location_id: str, access_token: str) -> bool:
    """
    Persist a refreshed CRM adapter access_token into crm_config JSONB.
    Used by HubSpot/Salesforce/Zoho/Pipedrive adapters after a successful token refresh.
    Updates only the access_token key — leaves all other crm_config keys intact.
    Uses advisory lock to prevent race conditions across parallel workers.
    """
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (location_id,))
        cur.execute("""
            UPDATE subscribers
            SET crm_config = crm_config || jsonb_build_object('access_token', %s::text),
                updated_at = NOW()
            WHERE location_id = %s
        """, (access_token, location_id))
        conn.commit()
        updated = cur.rowcount > 0
        if updated:
            logger.info(f"CRM config token persisted for {location_id}")
        else:
            logger.warning(f"update_crm_config_token: 0 rows updated for {location_id}")
        return updated
    except psycopg2.Error as e:
        logger.error(f"update_crm_config_token failed for {location_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        if 'cur' in locals():
            cur.close()
        return_db_connection(conn)


def get_subscribers_needing_token_refresh() -> list:
    """
    Get all subscribers whose OAuth tokens expire within the next 2 hours.
    Only returns rows that have a refresh_token (i.e., OAuth-connected users).
    """
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT location_id, access_token, refresh_token, token_expires_at, oauth_app_type
            FROM subscribers
            WHERE refresh_token IS NOT NULL
              AND refresh_token != ''
              AND token_expires_at IS NOT NULL
              AND token_expires_at < NOW() + interval '2 hours'
              AND token_expires_at > NOW() - interval '30 days'
            ORDER BY token_expires_at ASC
        """)
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_subscribers_needing_token_refresh failed: {e}")
        return []
    finally:
        return_db_connection(conn)


def get_users_needing_reminders() -> list:
    """
    Find users who need a reminder email. Catches TWO scenarios:
      A) User has account but never completed OAuth — missing access_token
         or location_id starts with 'temp_' (incomplete setup)
      B) User completed OAuth but hasn't subscribed — no stripe_customer_id

    Both scenarios trigger 24h and 72h reminders based on created_at.
    Each user gets a 'missing_fields' list so the email can say exactly what's needed.
    """
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        # Subscribers: anyone with an account that's either unsubscribed or incomplete
        cur.execute("""
            SELECT email, full_name, location_id, access_token,
                   stripe_customer_id, calendar_id, crm_user_id,
                   reminder_24h_sent, reminder_72h_sent,
                   COALESCE(install_completed_at, created_at) as ref_time,
                   'individual' as user_type
            FROM subscribers
            WHERE (
                  stripe_customer_id IS NULL
                  OR access_token IS NULL
                  OR location_id LIKE 'temp_%%'
                  OR calendar_id IS NULL
                  )
              AND email IS NOT NULL
              AND (
                  (reminder_24h_sent = FALSE AND COALESCE(install_completed_at, created_at) <= NOW() - INTERVAL '24 hours')
                  OR
                  (reminder_72h_sent = FALSE AND COALESCE(install_completed_at, created_at) <= NOW() - INTERVAL '72 hours')
              )
        """)
        individual_rows = cur.fetchall()

        # Agency owners
        cur.execute("""
            SELECT agency_email as email, full_name, location_id, access_token,
                   stripe_customer_id, calendar_id, crm_user_id,
                   reminder_24h_sent, reminder_72h_sent,
                   COALESCE(install_completed_at, created_at) as ref_time,
                   'agency_owner' as user_type
            FROM agency_billing
            WHERE (
                  stripe_customer_id IS NULL
                  OR access_token IS NULL
                  OR location_id LIKE 'temp_%%'
                  OR calendar_id IS NULL
                  )
              AND agency_email IS NOT NULL
              AND (
                  (reminder_24h_sent = FALSE AND COALESCE(install_completed_at, created_at) <= NOW() - INTERVAL '24 hours')
                  OR
                  (reminder_72h_sent = FALSE AND COALESCE(install_completed_at, created_at) <= NOW() - INTERVAL '72 hours')
              )
        """)
        agency_rows = cur.fetchall()

        results = []
        for row in list(individual_rows) + list(agency_rows):
            r = dict(row)
            ref_time = r.get("ref_time")
            if not ref_time:
                continue
            if hasattr(ref_time, 'tzinfo') and ref_time.tzinfo is None:
                ref_time = ref_time.replace(tzinfo=timezone.utc)
            hours_since = (datetime.now(timezone.utc) - ref_time).total_seconds() / 3600

            if hours_since >= 72 and not r.get("reminder_72h_sent"):
                r["reminder_type"] = "72h"
            elif hours_since >= 24 and not r.get("reminder_24h_sent"):
                r["reminder_type"] = "24h"
            else:
                continue

            # Build list of what's missing so email can be specific
            missing = []
            loc = r.get("location_id") or ""
            if not r.get("access_token"):
                missing.append("crm_connection")
            if not loc or loc.startswith("temp_"):
                missing.append("location_id")
            if not r.get("stripe_customer_id"):
                missing.append("subscription")
            if not r.get("calendar_id"):
                missing.append("calendar")
            r["missing_fields"] = missing
            results.append(r)

        cur.close()
        return results
    except psycopg2.Error as e:
        logger.error(f"get_users_needing_reminders failed: {e}")
        return []
    finally:
        return_db_connection(conn)


def mark_reminder_sent(email: str, reminder_type: str, user_type: str = "individual") -> bool:
    """Mark a reminder email as sent for a user."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        col = "reminder_24h_sent" if reminder_type == "24h" else "reminder_72h_sent"
        if user_type == "agency_owner":
            cur.execute(f"UPDATE agency_billing SET {col} = TRUE WHERE agency_email = %s", (email,))
        else:
            cur.execute(f"UPDATE subscribers SET {col} = TRUE WHERE email = %s", (email,))
        conn.commit()
        success = cur.rowcount > 0
        cur.close()
        return success
    except psycopg2.Error as e:
        logger.error(f"mark_reminder_sent failed for {email}: {e}")
        conn.rollback()
        return False
    finally:
        return_db_connection(conn)


def save_marketplace_install(payload: dict) -> Optional[int]:
    """Save a GHL marketplace install event. Returns the row ID or None on failure."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        # Extract fields from GHL app.installed webhook payload
        data = payload.get("data", payload)
        app_id = data.get("appId") or data.get("app_id") or payload.get("appId")
        company_id = data.get("companyId") or data.get("company_id") or payload.get("companyId")
        location_id = data.get("locationId") or data.get("location_id") or payload.get("locationId")
        user_id = data.get("userId") or data.get("user_id")
        user_email = data.get("email") or data.get("userEmail")
        user_name = data.get("name") or data.get("userName") or data.get("firstName", "")
        plan_id = data.get("planId") or data.get("plan_id")
        install_type = data.get("installType") or data.get("install_type") or "unknown"

        cur.execute("""
            INSERT INTO marketplace_installs
                (app_id, company_id, location_id, user_id, user_email, user_name,
                 plan_id, install_type, raw_payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (app_id, company_id, location_id, user_id, user_email,
              user_name, plan_id, install_type, json.dumps(payload)))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        install_id = row["id"] if row else None
        logger.info(f"Saved marketplace install: id={install_id}, company={company_id}, "
                     f"location={location_id}, email={user_email}")
        return install_id
    except psycopg2.Error as e:
        logger.error(f"save_marketplace_install failed: {e}")
        conn.rollback()
        return None
    finally:
        return_db_connection(conn)


def mark_install_oauth_complete(company_id: str = None, location_id: str = None):
    """Mark a marketplace install as having completed OAuth."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        if location_id:
            cur.execute("""
                UPDATE marketplace_installs
                SET oauth_completed = TRUE, oauth_completed_at = NOW()
                WHERE location_id = %s AND oauth_completed = FALSE
            """, (location_id,))
        elif company_id:
            cur.execute("""
                UPDATE marketplace_installs
                SET oauth_completed = TRUE, oauth_completed_at = NOW()
                WHERE company_id = %s AND oauth_completed = FALSE
            """, (company_id,))
        conn.commit()
        cur.close()
    except psycopg2.Error as e:
        logger.error(f"mark_install_oauth_complete failed: {e}")
        conn.rollback()
    finally:
        return_db_connection(conn)


def get_incomplete_installs() -> list:
    """Get marketplace installs that never completed OAuth — these are the 'lost' users."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, app_id, company_id, location_id, user_id, user_email, user_name,
                   plan_id, install_type, oauth_completed, setup_email_sent,
                   setup_email_sent_at, created_at
            FROM marketplace_installs
            WHERE oauth_completed = FALSE
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    except psycopg2.Error as e:
        logger.error(f"get_incomplete_installs failed: {e}")
        return []
    finally:
        return_db_connection(conn)


def get_all_marketplace_installs() -> list:
    """Get all marketplace installs for admin view."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, app_id, company_id, location_id, user_id, user_email, user_name,
                   plan_id, install_type, oauth_completed, oauth_completed_at,
                   setup_email_sent, setup_email_sent_at, created_at
            FROM marketplace_installs
            ORDER BY created_at DESC
            LIMIT 200
        """)
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    except psycopg2.Error as e:
        logger.error(f"get_all_marketplace_installs failed: {e}")
        return []
    finally:
        return_db_connection(conn)


def mark_setup_email_sent(install_id: int) -> bool:
    """Mark that a setup email was sent for a specific install."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE marketplace_installs
            SET setup_email_sent = TRUE, setup_email_sent_at = NOW()
            WHERE id = %s
        """, (install_id,))
        conn.commit()
        success = cur.rowcount > 0
        cur.close()
        return success
    except psycopg2.Error as e:
        logger.error(f"mark_setup_email_sent failed for install {install_id}: {e}")
        conn.rollback()
        return False
    finally:
        return_db_connection(conn)


def find_marketplace_email(location_id: str = None, company_id: str = None) -> Optional[dict]:
    """
    Bridge function: search marketplace_installs for a matching user email.
    When OAuth scopes block /users/me, this recovers the email from the
    install webhook data we already captured.
    """
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        # 1. Strongest match: Location ID (specific sub-account)
        if location_id:
            cur.execute("""
                SELECT user_email, user_name
                FROM marketplace_installs
                WHERE location_id = %s AND user_email IS NOT NULL AND user_email != ''
                ORDER BY created_at DESC LIMIT 1
            """, (location_id,))
            row = cur.fetchone()
            if row:
                cur.close()
                return dict(row)

        # 2. Fallback: Company ID (agency-level install)
        if company_id:
            cur.execute("""
                SELECT user_email, user_name
                FROM marketplace_installs
                WHERE company_id = %s AND user_email IS NOT NULL AND user_email != ''
                ORDER BY created_at DESC LIMIT 1
            """, (company_id,))
            row = cur.fetchone()
            if row:
                cur.close()
                return dict(row)

        cur.close()
        return None
    except psycopg2.Error as e:
        logger.error(f"find_marketplace_email failed: {e}")
        return None
    finally:
        return_db_connection(conn)


def save_uninstall_record(payload: dict, location_id: str, company_id: str,
                          user_email: str = "", user_name: str = "") -> Optional[int]:
    """Save an uninstall event from the GHL webhook. Returns the record ID."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO uninstall_feedback
                (location_id, company_id, user_email, user_name, raw_webhook_payload)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (location_id, company_id, user_email, user_name, json.dumps(payload)))
        row = cur.fetchone()
        conn.commit()
        record_id = row["id"] if row else None
        cur.close()
        return record_id
    except psycopg2.Error as e:
        logger.error(f"save_uninstall_record failed: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        return_db_connection(conn)


def delete_subscriber_data(location_id: str) -> bool:
    """Delete the subscriber row and all related data for a location_id on app uninstall."""
    if not location_id:
        logger.warning("delete_subscriber_data called with empty location_id")
        return False
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()

        # Look up the subscriber's email first (needed for email-keyed tables)
        cur.execute("SELECT email FROM subscribers WHERE location_id = %s", (location_id,))
        row = cur.fetchone()
        subscriber_email = (row[0] if isinstance(row, tuple) else row.get('email')) if row else None

        # Delete from child/related tables first, then the subscriber row.
        # Tables that reference location_id directly:
        location_tables = [
            "webhook_logs",
            "persistent_alerts",
            "call_history",
            "failed_webhook_payloads",
            "crm_conversations",
            "crm_deals",
            "crm_sync_state",
            "contacts",
            "number_health",
            "contact_cache",
        ]
        for table in location_tables:
            cur.execute(f"DELETE FROM {table} WHERE location_id = %s", (location_id,))

        # Tables keyed by email (not location_id)
        if subscriber_email:
            email_tables = [
                "ai_minute_balances",
                "ai_minute_purchases",
                "ai_minute_usage_logs",
                "discord_connections",
                "discord_servers",
                "discord_webhook_channels",
            ]
            for table in email_tables:
                cur.execute(f"DELETE FROM {table} WHERE email = %s", (subscriber_email,))

        # Delete the subscriber row itself
        cur.execute("DELETE FROM subscribers WHERE location_id = %s", (location_id,))
        deleted = cur.rowcount

        conn.commit()
        cur.close()
        logger.info(f"delete_subscriber_data: removed location_id={location_id} "
                     f"(subscriber row deleted: {deleted > 0})")
        return deleted > 0
    except psycopg2.Error as e:
        logger.error(f"delete_subscriber_data failed for {location_id}: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        return_db_connection(conn)


def save_uninstall_feedback(record_id: int, reason: str, other_text: str = "") -> bool:
    """Save user-submitted feedback for an uninstall record."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE uninstall_feedback
            SET reason = %s, other_text = %s, feedback_submitted_at = NOW()
            WHERE id = %s
        """, (reason, other_text, record_id))
        conn.commit()
        cur.close()
        return True
    except psycopg2.Error as e:
        logger.error(f"save_uninstall_feedback failed: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        return_db_connection(conn)


def get_uninstall_feedback(record_id: int) -> Optional[dict]:
    """Retrieve an uninstall feedback record by ID."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM uninstall_feedback WHERE id = %s", (record_id,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except psycopg2.Error as e:
        logger.error(f"get_uninstall_feedback failed: {e}")
        return None
    finally:
        return_db_connection(conn)


def save_contracted_carriers(email: str, carriers: list) -> bool:
    """Save an agent's contracted carrier selections."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers SET contracted_carriers = %s, updated_at = NOW()
            WHERE email = %s
        """, (json.dumps(carriers), email))
        if cur.rowcount == 0:
            # Try agency_billing too
            cur.execute("""
                UPDATE agency_billing SET contracted_carriers = %s, updated_at = NOW()
                WHERE agency_email = %s
            """, (json.dumps(carriers), email))
        conn.commit()
        cur.close()
        return True
    except psycopg2.Error as e:
        logger.error(f"save_contracted_carriers failed for {email}: {e}")
        conn.rollback()
        return False
    finally:
        return_db_connection(conn)


def get_contracted_carriers(email: str) -> list:
    """Load an agent's contracted carrier selections."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT contracted_carriers FROM subscribers WHERE email = %s LIMIT 1", (email,))
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT contracted_carriers FROM agency_billing WHERE agency_email = %s LIMIT 1", (email,))
            row = cur.fetchone()
        cur.close()
        if row and row.get('contracted_carriers'):
            carriers = row['contracted_carriers']
            return carriers if isinstance(carriers, list) else json.loads(carriers)
        return []
    except Exception as e:
        logger.error(f"get_contracted_carriers failed for {email}: {e}")
        return []
    finally:
        return_db_connection(conn)



# ===================================================
# BOT SETTINGS HELPERS
# ===================================================

# Default settings — any key missing from a subscriber's stored settings
# falls back to these values. This keeps the system backwards-compatible:
# existing subscribers with no bot_settings get the original behavior.
BOT_SETTINGS_DEFAULTS = {
    "humor_enabled": True,
    "professionalism_level": 0,       # 0 = current casual style, 5 = ultra-professional
    "custom_behavior": "",             # Free-text behavior instructions
    "outbound_messages": [],           # Custom text drip messages
    "auto_emoji": True,                # Whether bot uses emojis
    "after_hours_enabled": False,      # After-hours auto-reply mode
    "after_hours_start": "18:00",      # When after-hours kicks in (HH:MM)
    "after_hours_end": "09:00",        # When after-hours ends
    "response_length": "balanced",     # "short", "balanced", or "detailed"
    "booking_confirmation": True,      # Double-confirm before booking
    "objection_persistence": 3,        # How many angles before graceful exit (1-5)
    "lead_reengagement": True,         # Whether to do follow-up sequences
    "conversation_memory": True,       # Whether bot references past conversation details
    "speed_to_lead": True,             # Respond immediately to new leads
    "multi_language": False,           # Detect and respond in lead's language
}


def get_bot_settings(email: str) -> dict:
    """Load a subscriber's bot_settings, merged with defaults for any missing keys."""
    conn = get_db_connection()
    if not conn:
        return dict(BOT_SETTINGS_DEFAULTS)
    try:
        cur = conn.cursor()
        cur.execute("SELECT bot_settings FROM subscribers WHERE email = %s LIMIT 1", (email,))
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT bot_settings FROM agency_billing WHERE agency_email = %s LIMIT 1", (email,))
            row = cur.fetchone()
        cur.close()
        stored = {}
        if row and row.get('bot_settings'):
            stored = row['bot_settings']
            if isinstance(stored, str):
                stored = json.loads(stored)
        # Merge: stored values override defaults
        merged = dict(BOT_SETTINGS_DEFAULTS)
        merged.update(stored)
        return merged
    except Exception as e:
        logger.error(f"get_bot_settings failed for {email}: {e}")
        return dict(BOT_SETTINGS_DEFAULTS)
    finally:
        return_db_connection(conn)


def get_bot_settings_by_location(location_id: str) -> dict:
    """Load bot_settings by location_id (used in webhook/task context)."""
    conn = get_db_connection()
    if not conn:
        return dict(BOT_SETTINGS_DEFAULTS)
    try:
        cur = conn.cursor()
        cur.execute("SELECT bot_settings FROM subscribers WHERE location_id = %s LIMIT 1", (location_id,))
        row = cur.fetchone()
        cur.close()
        stored = {}
        if row and row.get('bot_settings'):
            stored = row['bot_settings']
            if isinstance(stored, str):
                stored = json.loads(stored)
        merged = dict(BOT_SETTINGS_DEFAULTS)
        merged.update(stored)
        return merged
    except Exception as e:
        logger.error(f"get_bot_settings_by_location failed: {e}")
        return dict(BOT_SETTINGS_DEFAULTS)
    finally:
        return_db_connection(conn)


def save_bot_settings(email: str, settings: dict) -> bool:
    """Save a subscriber's bot_settings."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers SET bot_settings = %s, updated_at = NOW()
            WHERE email = %s
        """, (json.dumps(settings), email))
        if cur.rowcount == 0:
            cur.execute("""
                UPDATE agency_billing SET bot_settings = %s, updated_at = NOW()
                WHERE agency_email = %s
            """, (json.dumps(settings), email))
        conn.commit()
        cur.close()
        return True
    except psycopg2.Error as e:
        logger.error(f"save_bot_settings failed for {email}: {e}")
        conn.rollback()
        return False
    finally:
        return_db_connection(conn)


# ===================================================
# API KEY HELPERS
# ===================================================

import secrets
import hmac as _hmac

def generate_api_key() -> str:
    """Generate a secure API key with sk_live_ prefix."""
    return f"sk_live_{secrets.token_urlsafe(32)}"


def generate_webhook_secret() -> str:
    """Generate a secure webhook signing secret."""
    return f"whsec_{secrets.token_urlsafe(32)}"


def api_key_prefix(key: str) -> str:
    """Get a safe prefix for logging (first 12 chars)."""
    return key[:12] + "..." if key and len(key) > 12 else key or ""


def create_api_key_for_user(email: str) -> dict:
    """Generate and save a new API key + webhook secret for a subscriber."""
    api_key = generate_api_key()
    webhook_secret = generate_webhook_secret()
    conn = get_db_connection()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        # Try subscribers first
        cur.execute("""
            UPDATE subscribers
            SET api_key = %s, webhook_secret = %s, api_key_created_at = NOW(), updated_at = NOW()
            WHERE email = %s
        """, (api_key, webhook_secret, email))
        if cur.rowcount == 0:
            cur.execute("""
                UPDATE agency_billing
                SET api_key = %s, webhook_secret = %s, api_key_created_at = NOW(), updated_at = NOW()
                WHERE agency_email = %s
            """, (api_key, webhook_secret, email))
        conn.commit()
        cur.close()
        return {"api_key": api_key, "webhook_secret": webhook_secret}
    except psycopg2.Error as e:
        logger.error(f"create_api_key_for_user failed for {email}: {e}")
        conn.rollback()
        return {"error": str(e)}
    finally:
        return_db_connection(conn)


def revoke_api_key(email: str) -> bool:
    """Revoke a user's API key."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers SET api_key = NULL, webhook_secret = NULL, updated_at = NOW()
            WHERE email = %s
        """, (email,))
        if cur.rowcount == 0:
            cur.execute("""
                UPDATE agency_billing SET api_key = NULL, webhook_secret = NULL, updated_at = NOW()
                WHERE agency_email = %s
            """, (email,))
        conn.commit()
        cur.close()
        return True
    except psycopg2.Error as e:
        logger.error(f"revoke_api_key failed for {email}: {e}")
        conn.rollback()
        return False
    finally:
        return_db_connection(conn)


# ===================================================
# TRAINING TOKEN HELPERS
# ===================================================

def generate_training_token() -> str:
    """Generate a secure training integration token with trn_ prefix."""
    return f"trn_{secrets.token_urlsafe(32)}"


def create_training_token_for_user(email: str) -> dict:
    """Generate and store a training token in the user's voice_config JSONB."""
    token = generate_training_token()
    conn = get_db_connection()
    if not conn:
        return {"error": "Database unavailable"}
    try:
        cur = conn.cursor()
        # Read existing voice_config, merge in training_token fields
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (email,))
        row = cur.fetchone()
        vc = (row['voice_config'] if row and row['voice_config'] else {}) if row else {}
        if not isinstance(vc, dict):
            vc = {}
        vc["training_token"] = token
        vc["training_token_created_at"] = datetime.utcnow().isoformat() + "Z"
        cur.execute(
            "UPDATE subscribers SET voice_config = %s::jsonb, updated_at = NOW() WHERE email = %s",
            (json.dumps(vc), email),
        )
        if cur.rowcount == 0:
            conn.rollback()
            cur.close()
            return {"error": "Account not found"}
        conn.commit()
        cur.close()
        return {"training_token": token}
    except Exception as e:
        logger.error(f"create_training_token_for_user failed for {email}: {e}")
        conn.rollback()
        return {"error": str(e)}
    finally:
        return_db_connection(conn)


def revoke_training_token(email: str) -> bool:
    """Remove the training token from a user's voice_config."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT voice_config FROM subscribers WHERE email = %s", (email,))
        row = cur.fetchone()
        vc = (row['voice_config'] if row and row['voice_config'] else {}) if row else {}
        if not isinstance(vc, dict):
            vc = {}
        vc.pop("training_token", None)
        vc.pop("training_token_created_at", None)
        cur.execute(
            "UPDATE subscribers SET voice_config = %s::jsonb, updated_at = NOW() WHERE email = %s",
            (json.dumps(vc), email),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"revoke_training_token failed for {email}: {e}")
        conn.rollback()
        return False
    finally:
        return_db_connection(conn)


def get_subscriber_by_training_token(token: str) -> dict:
    """Look up a subscriber by their training integration token. Returns subscriber dict or None."""
    if not token or not token.startswith("trn_"):
        return None
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM subscribers WHERE voice_config->>'training_token' = %s LIMIT 1",
            (token,),
        )
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_subscriber_by_training_token failed: {e}")
        return None
    finally:
        return_db_connection(conn)


def save_outbound_webhook_url(email: str, url: str) -> bool:
    """Save a subscriber's outbound webhook URL for API reply delivery."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers SET outbound_webhook_url = %s, updated_at = NOW()
            WHERE email = %s
        """, (url, email))
        if cur.rowcount == 0:
            cur.execute("""
                UPDATE agency_billing SET outbound_webhook_url = %s, updated_at = NOW()
                WHERE agency_email = %s
            """, (url, email))
        conn.commit()
        cur.close()
        return True
    except psycopg2.Error as e:
        logger.error(f"save_outbound_webhook_url failed for {email}: {e}")
        conn.rollback()
        return False
    finally:
        return_db_connection(conn)


def get_subscriber_by_api_key(api_key: str) -> dict:
    """Look up a subscriber by their API key. Returns full subscriber dict or None."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM subscribers WHERE api_key = %s LIMIT 1", (api_key,))
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT * FROM agency_billing WHERE api_key = %s LIMIT 1", (api_key,))
            row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_subscriber_by_api_key failed: {e}")
        return None
    finally:
        return_db_connection(conn)


def log_api_usage(api_key_pfx: str, location_id: str, endpoint: str,
                  status_code: int, response_time_ms: int = 0,
                  contact_id: str = None, ip_address: str = None,
                  user_agent: str = None, error_message: str = None):
    """Log an API request for analytics and rate limiting."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO api_usage_logs
                (api_key_prefix, location_id, endpoint, status_code, response_time_ms,
                 contact_id, ip_address, user_agent, error_message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (api_key_pfx, location_id, endpoint, status_code, response_time_ms,
              contact_id, ip_address, user_agent, error_message))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.debug(f"log_api_usage failed: {e}")
    finally:
        return_db_connection(conn)


def get_api_request_count(api_key_pfx: str, window_seconds: int = 60) -> int:
    """Count API requests in the last N seconds for rate limiting."""
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) as cnt FROM api_usage_logs
            WHERE api_key_prefix = %s
            AND created_at > NOW() - INTERVAL '%s seconds'
        """, (api_key_pfx, window_seconds))
        row = cur.fetchone()
        cur.close()
        return row['cnt'] if row else 0
    except Exception as e:
        logger.debug(f"get_api_request_count failed: {e}")
        return 0
    finally:
        return_db_connection(conn)


# ── AI Minutes helpers ──────────────────────────────────────────────────

def get_ai_minute_balance(email: str) -> dict:
    """Get AI minute balance for a subscriber."""
    conn = get_db_connection()
    if not conn:
        return {"balance_minutes": 0, "total_purchased": 0, "total_used": 0}
    try:
        cur = conn.cursor()
        cur.execute("SELECT balance_minutes, total_purchased, total_used FROM ai_minute_balances WHERE email = %s", (email,))
        row = cur.fetchone()
        cur.close()
        if row:
            return dict(row)
        return {"balance_minutes": 0, "total_purchased": 0, "total_used": 0}
    except Exception as e:
        logger.error(f"get_ai_minute_balance failed: {e}")
        return {"balance_minutes": 0, "total_purchased": 0, "total_used": 0}
    finally:
        return_db_connection(conn)


def credit_ai_minutes(email: str, minutes: int, stripe_session_id: str = None,
                      stripe_payment_intent: str = None, package_label: str = None,
                      amount_cents: int = 0) -> bool:
    """Credit AI minutes to a subscriber after purchase.

    Idempotent: if stripe_session_id has already been marked 'completed' in
    ai_minute_purchases, the balance is NOT touched again.  This prevents
    double-crediting from Stripe webhook retries or any other duplicate calls.
    """
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()

        # ── Idempotency guard ──────────────────────────────────────────────────
        # Check BEFORE touching the balance so a duplicate webhook never
        # increments the counter a second time.
        if stripe_session_id:
            cur.execute(
                "SELECT 1 FROM ai_minute_purchases "
                "WHERE stripe_session_id = %s AND status = 'completed'",
                (stripe_session_id,)
            )
            if cur.fetchone():
                logger.warning(
                    f"⚠️  Duplicate AI minutes credit blocked — "
                    f"session {stripe_session_id} already processed for {email}"
                )
                cur.close()
                return True  # idempotent; treat as success

        # ── Credit balance (only reached once per session) ────────────────────
        cur.execute("""
            INSERT INTO ai_minute_balances (email, balance_minutes, total_purchased, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (email) DO UPDATE SET
                balance_minutes  = ai_minute_balances.balance_minutes  + EXCLUDED.balance_minutes,
                total_purchased  = ai_minute_balances.total_purchased  + EXCLUDED.total_purchased,
                updated_at       = NOW()
        """, (email, minutes, minutes))

        # ── Record purchase (DO NOTHING on conflict = safe re-entry) ──────────
        if stripe_session_id:
            cur.execute("""
                INSERT INTO ai_minute_purchases
                    (email, stripe_session_id, stripe_payment_intent, package_minutes,
                     package_label, amount_cents, status, completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'completed', NOW())
                ON CONFLICT (stripe_session_id) DO NOTHING
            """, (email, stripe_session_id, stripe_payment_intent, minutes,
                  package_label, amount_cents))

        conn.commit()
        cur.close()
        logger.info(f"✅ Credited {minutes} AI minutes to {email} (session={stripe_session_id})")
        return True
    except Exception as e:
        logger.error(f"credit_ai_minutes failed: {e}")
        conn.rollback()
        return False
    finally:
        return_db_connection(conn)


def deduct_ai_minutes(email: str, duration_seconds: int, call_sid: str = None,
                      phone: str = None, direction: str = 'outbound') -> dict:
    """Deduct AI minutes after a call. Returns deduction info."""
    # Round up to nearest minute
    minutes_used = max(1, math.ceil(duration_seconds / 60))

    conn = get_db_connection()
    if not conn:
        return {"success": False, "error": "db_unavailable"}
    try:
        cur = conn.cursor()
        # Get current balance
        cur.execute("SELECT balance_minutes FROM ai_minute_balances WHERE email = %s FOR UPDATE", (email,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return {"success": False, "error": "no_balance", "minutes_used": minutes_used}

        current = row['balance_minutes']
        actual_deduction = min(minutes_used, current)
        new_balance = max(0, current - minutes_used)

        # Update balance
        cur.execute("""
            UPDATE ai_minute_balances
            SET balance_minutes = %s, total_used = total_used + %s, updated_at = NOW()
            WHERE email = %s
        """, (new_balance, actual_deduction, email))

        # Log usage
        cur.execute("""
            INSERT INTO ai_minute_usage_logs
                (email, call_sid, phone, direction, duration_seconds, minutes_deducted, balance_after)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (email, call_sid, phone, direction, duration_seconds, actual_deduction, new_balance))

        conn.commit()
        cur.close()
        return {
            "success": True,
            "minutes_deducted": actual_deduction,
            "balance_after": new_balance,
            "duration_seconds": duration_seconds,
        }
    except Exception as e:
        logger.error(f"deduct_ai_minutes failed: {e}")
        conn.rollback()
        return {"success": False, "error": str(e)}
    finally:
        return_db_connection(conn)


def get_ai_minute_purchases(email: str, limit: int = 20) -> list:
    """Get purchase history for a subscriber."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT package_minutes, package_label, amount_cents, status, created_at, completed_at
            FROM ai_minute_purchases
            WHERE email = %s ORDER BY created_at DESC LIMIT %s
        """, (email, limit))
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_ai_minute_purchases failed: {e}")
        return []
    finally:
        return_db_connection(conn)


def get_ai_minute_usage(email: str, limit: int = 50) -> list:
    """Get usage history for a subscriber."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT call_sid, phone, direction, duration_seconds, minutes_deducted,
                   balance_after, created_at
            FROM ai_minute_usage_logs
            WHERE email = %s ORDER BY created_at DESC LIMIT %s
        """, (email, limit))
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_ai_minute_usage failed: {e}")
        return []
    finally:
        return_db_connection(conn)

def audit_ai_minutes(email: str) -> dict:
    """Reconcile ai_minute_balances against the immutable source-of-truth tables.

    Source of truth:
      - Total purchased  = SUM(package_minutes) from ai_minute_purchases WHERE status='completed'
      - Total used       = SUM(minutes_deducted) from ai_minute_usage_logs
      - Correct balance  = total_purchased - total_used  (floor 0)

    If the live balance_minutes differs from that calculation the row is
    corrected in place and the drift is logged as a warning.

    Returns a dict:
      {
        "email":            str,
        "purchased":        int,   # sum of completed purchase records
        "used":             int,   # sum of usage log entries
        "expected_balance": int,
        "actual_balance":   int,
        "drift":            int,   # actual - expected (positive = over-credited)
        "corrected":        bool,
      }
    """
    conn = get_db_connection()
    if not conn:
        return {"error": "db_unavailable"}
    try:
        cur = conn.cursor()

        # Sum every completed purchase receipt
        cur.execute(
            "SELECT COALESCE(SUM(package_minutes), 0) AS total "
            "FROM ai_minute_purchases WHERE email = %s AND status = 'completed'",
            (email,)
        )
        total_purchased = int(cur.fetchone()['total'] or 0)

        # Sum every usage entry
        cur.execute(
            "SELECT COALESCE(SUM(minutes_deducted), 0) AS total "
            "FROM ai_minute_usage_logs WHERE email = %s",
            (email,)
        )
        total_used = int(cur.fetchone()['total'] or 0)

        expected_balance = max(0, total_purchased - total_used)

        # Current live balance
        cur.execute(
            "SELECT balance_minutes FROM ai_minute_balances WHERE email = %s",
            (email,)
        )
        row = cur.fetchone()
        actual_balance = int(row['balance_minutes'] if row else 0)

        drift = actual_balance - expected_balance  # +ve = over-credited, -ve = under-credited
        corrected = False

        if drift != 0:
            logger.warning(
                f"⚠️  AI minutes audit drift for {email}: "
                f"actual={actual_balance}, expected={expected_balance}, drift={drift:+d} min — correcting"
            )
            # Write the corrected values derived purely from receipts
            cur.execute("""
                INSERT INTO ai_minute_balances
                    (email, balance_minutes, total_purchased, total_used, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (email) DO UPDATE SET
                    balance_minutes = EXCLUDED.balance_minutes,
                    total_purchased = EXCLUDED.total_purchased,
                    total_used      = EXCLUDED.total_used,
                    updated_at      = NOW()
            """, (email, expected_balance, total_purchased, total_used))
            conn.commit()
            corrected = True
            logger.info(
                f"✅ AI minutes audit corrected {email}: "
                f"{actual_balance} → {expected_balance} (purchased={total_purchased}, used={total_used})"
            )

        cur.close()
        return {
            "email":            email,
            "purchased":        total_purchased,
            "used":             total_used,
            "expected_balance": expected_balance,
            "actual_balance":   actual_balance,
            "drift":            drift,
            "corrected":        corrected,
        }
    except Exception as e:
        logger.error(f"audit_ai_minutes failed for {email}: {e}")
        conn.rollback()
        return {"error": str(e)}
    finally:
        return_db_connection(conn)


# ════════════════════════════════════════════════════════════════
# DISCORD INTEGRATION HELPERS
# ════════════════════════════════════════════════════════════════

def save_discord_connection(email: str, discord_user_id: str, username: str,
                             global_name: str, avatar: str, access_token: str,
                             refresh_token: str, token_expires_at) -> bool:
    """Upsert a Discord OAuth connection for a user."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO discord_connections
                (email, discord_user_id, username, global_name, avatar,
                 access_token, refresh_token, token_expires_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (email) DO UPDATE SET
                discord_user_id = EXCLUDED.discord_user_id,
                username        = EXCLUDED.username,
                global_name     = EXCLUDED.global_name,
                avatar          = EXCLUDED.avatar,
                access_token    = EXCLUDED.access_token,
                refresh_token   = EXCLUDED.refresh_token,
                token_expires_at= EXCLUDED.token_expires_at,
                updated_at      = NOW()
        """, (email, discord_user_id, username, global_name, avatar,
              access_token, refresh_token, token_expires_at))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"save_discord_connection failed: {e}")
        return False
    finally:
        return_db_connection(conn)


def get_discord_connection(email: str) -> Optional[dict]:
    """Return the Discord connection row for a user, or None."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT * FROM discord_connections WHERE email = %s LIMIT 1
        """, (email,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_discord_connection failed: {e}")
        return None
    finally:
        return_db_connection(conn)


def delete_discord_connection(email: str) -> bool:
    """Remove Discord connection (disconnect)."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM discord_connections WHERE email = %s", (email,))
        cur.execute("DELETE FROM discord_servers WHERE email = %s", (email,))
        cur.execute("DELETE FROM discord_webhook_channels WHERE email = %s", (email,))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"delete_discord_connection failed: {e}")
        return False
    finally:
        return_db_connection(conn)


def save_discord_servers(email: str, servers: list) -> bool:
    """Replace the user's saved Discord servers (max 3)."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM discord_servers WHERE email = %s", (email,))
        for i, srv in enumerate(servers[:3]):
            cur.execute("""
                INSERT INTO discord_servers (email, guild_id, guild_name, guild_icon, position)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (email, guild_id) DO UPDATE SET
                    guild_name = EXCLUDED.guild_name,
                    guild_icon = EXCLUDED.guild_icon,
                    position   = EXCLUDED.position
            """, (email, srv['guild_id'], srv['name'], srv.get('icon'), i))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"save_discord_servers failed: {e}")
        return False
    finally:
        return_db_connection(conn)


def get_discord_servers(email: str) -> list:
    """Return a user's saved Discord servers."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT guild_id, guild_name AS name, guild_icon AS icon
            FROM discord_servers WHERE email = %s ORDER BY position
        """, (email,))
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_discord_servers failed: {e}")
        return []
    finally:
        return_db_connection(conn)


def save_discord_webhook_channel(email: str, guild_id: str, guild_name: str,
                                  guild_icon: str, channel_id: str, channel_name: str,
                                  webhook_id: str, webhook_token: str, webhook_url: str) -> bool:
    """Upsert a webhook-connected Discord channel for a user."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO discord_webhook_channels
                (email, guild_id, guild_name, guild_icon, channel_id, channel_name,
                 webhook_id, webhook_token, webhook_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (email, channel_id) DO UPDATE SET
                guild_name    = EXCLUDED.guild_name,
                guild_icon    = EXCLUDED.guild_icon,
                channel_name  = EXCLUDED.channel_name,
                webhook_id    = EXCLUDED.webhook_id,
                webhook_token = EXCLUDED.webhook_token,
                webhook_url   = EXCLUDED.webhook_url
        """, (email, guild_id, guild_name, guild_icon, channel_id, channel_name,
              webhook_id, webhook_token, webhook_url))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"save_discord_webhook_channel failed: {e}")
        return False
    finally:
        return_db_connection(conn)


def get_discord_webhook_channels(email: str) -> list:
    """Return all webhook-connected channels for a user, grouped by guild."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT guild_id, guild_name, guild_icon, channel_id, channel_name,
                   webhook_id, webhook_url
            FROM discord_webhook_channels
            WHERE email = %s
            ORDER BY guild_name, channel_name
        """, (email,))
        rows = cur.fetchall()
        cur.close()
        # Group by guild
        guilds = {}
        for r in rows:
            gid = r["guild_id"]
            if gid not in guilds:
                guilds[gid] = {
                    "guild_id": gid,
                    "name": r["guild_name"],
                    "icon": r["guild_icon"],
                    "channels": [],
                }
            guilds[gid]["channels"].append({
                "id": r["channel_id"],
                "name": r["channel_name"],
                "webhook_id": r["webhook_id"],
                "webhook_url": r["webhook_url"],
            })
        return list(guilds.values())
    except Exception as e:
        logger.error(f"get_discord_webhook_channels failed: {e}")
        return []
    finally:
        return_db_connection(conn)


def delete_discord_webhook_channel(email: str, channel_id: str) -> bool:
    """Remove a single webhook-connected channel."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM discord_webhook_channels WHERE email = %s AND channel_id = %s",
            (email, channel_id)
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"delete_discord_webhook_channel failed: {e}")
        return False
    finally:
        return_db_connection(conn)


# ════════════════════════════════════════════════════════════════
# SLACK INTEGRATION HELPERS
# ════════════════════════════════════════════════════════════════

def save_slack_connection(email: str, slack_team_id: str, slack_team_name: str,
                           slack_user_id: str, bot_token: str, user_token: str,
                           authed_user_name: str, bot_user_id: str) -> bool:
    """Upsert a Slack OAuth connection for a user."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO slack_connections
                (email, slack_team_id, slack_team_name, slack_user_id, bot_token,
                 user_token, authed_user_name, bot_user_id, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (email) DO UPDATE SET
                slack_team_id    = EXCLUDED.slack_team_id,
                slack_team_name  = EXCLUDED.slack_team_name,
                slack_user_id    = EXCLUDED.slack_user_id,
                bot_token        = EXCLUDED.bot_token,
                user_token       = EXCLUDED.user_token,
                authed_user_name = EXCLUDED.authed_user_name,
                bot_user_id      = EXCLUDED.bot_user_id,
                updated_at       = NOW()
        """, (email, slack_team_id, slack_team_name, slack_user_id, bot_token,
              user_token, authed_user_name, bot_user_id))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"save_slack_connection failed: {e}")
        return False
    finally:
        return_db_connection(conn)


def get_slack_connection(email: str) -> Optional[dict]:
    """Return the Slack connection row for a user, or None."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT * FROM slack_connections WHERE email = %s LIMIT 1
        """, (email,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_slack_connection failed: {e}")
        return None
    finally:
        return_db_connection(conn)


def delete_slack_connection(email: str) -> bool:
    """Remove Slack connection (disconnect)."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM slack_connections WHERE email = %s", (email,))
        cur.execute("DELETE FROM slack_workspaces WHERE email = %s", (email,))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"delete_slack_connection failed: {e}")
        return False
    finally:
        return_db_connection(conn)


def save_slack_workspaces(email: str, workspaces: list) -> bool:
    """Replace the user's saved Slack workspaces (max 3)."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM slack_workspaces WHERE email = %s", (email,))
        for i, ws in enumerate(workspaces[:3]):
            cur.execute("""
                INSERT INTO slack_workspaces (email, team_id, team_name, team_icon, position)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (email, team_id) DO UPDATE SET
                    team_name = EXCLUDED.team_name,
                    team_icon = EXCLUDED.team_icon,
                    position  = EXCLUDED.position
            """, (email, ws['team_id'], ws['name'], ws.get('icon'), i))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"save_slack_workspaces failed: {e}")
        return False
    finally:
        return_db_connection(conn)


def get_slack_workspaces(email: str) -> list:
    """Return a user's saved Slack workspaces."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT team_id, team_name AS name, team_icon AS icon
            FROM slack_workspaces WHERE email = %s ORDER BY position
        """, (email,))
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_slack_workspaces failed: {e}")
        return []
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════
#  GOOGLE CALENDAR — OAuth token persistence via JSONB column
# ═══════════════════════════════════════════════════════════════

def save_google_calendar_config(location_id: str, config: dict) -> bool:
    """Save Google Calendar OAuth config to subscribers.google_calendar_config JSONB."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        import json as _json
        cur.execute("""
            UPDATE subscribers
            SET google_calendar_config = %s::jsonb,
                updated_at = NOW()
            WHERE location_id = %s
        """, (_json.dumps(config), location_id))
        if cur.rowcount == 0:
            cur.execute("""
                UPDATE agency_billing
                SET google_calendar_config = %s::jsonb,
                    updated_at = NOW()
                WHERE location_id = %s
            """, (_json.dumps(config), location_id))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"save_google_calendar_config failed for {location_id}: {e}")
        conn.rollback()
        return False
    finally:
        return_db_connection(conn)


def get_google_calendar_config(location_id: str) -> dict:
    """Get Google Calendar config from subscribers.google_calendar_config JSONB."""
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT google_calendar_config FROM subscribers WHERE location_id = %s
        """, (location_id,))
        row = cur.fetchone()
        if not row:
            cur.execute("""
                SELECT google_calendar_config FROM agency_billing WHERE location_id = %s
            """, (location_id,))
            row = cur.fetchone()
        cur.close()
        return dict(row['google_calendar_config']) if row and row.get('google_calendar_config') else {}
    except Exception as e:
        logger.error(f"get_google_calendar_config failed for {location_id}: {e}")
        return {}
    finally:
        return_db_connection(conn)


def delete_google_calendar_config(location_id: str) -> bool:
    """Clear Google Calendar config (disconnect)."""
    return save_google_calendar_config(location_id, {})


# ═══════════════════════════════════════════════════════════════
#  CONTACT CACHE — persistent per-subscriber cache for fast dialer
# ═══════════════════════════════════════════════════════════════

def get_cached_contacts(location_id: str, query: str = None, limit: int = 5000,
                        assigned_to: str = None) -> list:
    """Fetch contacts from the local cache. Optional search filter.
    assigned_to: if set, only return contacts assigned to this GHL user ID.
    Returns list of contact dicts or empty list if no cache."""
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        conditions = ["location_id = %s"]
        params = [location_id]

        if assigned_to:
            conditions.append("assigned_to = %s")
            params.append(assigned_to)

        if query and query.strip():
            q = f"%{query.strip().lower()}%"
            conditions.append(
                "(lower(name) LIKE %s OR phone LIKE %s OR lower(email) LIKE %s"
                " OR EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(tags, '[]'::jsonb)) AS t WHERE lower(t) LIKE %s))"
            )
            params.extend([q, q, q, q])

        where_clause = " AND ".join(conditions)
        params.append(limit)
        cur.execute(f"""
            SELECT contact_id, name, first_name, last_name, phone, email, tags, date_added, dnd, assigned_to
            FROM contact_cache
            WHERE {where_clause}
            ORDER BY name
            LIMIT %s
        """, params)
        rows = cur.fetchall()
        cur.close()
        return [{
            "id": r["contact_id"],
            "name": r["name"] or "Unknown",
            "firstName": r["first_name"] or "",
            "lastName": r["last_name"] or "",
            "phone": r["phone"] or "",
            "email": r["email"] or "",
            "tags": r["tags"] if r["tags"] else [],
            "dateAdded": r["date_added"] or "",
            "dnd": bool(r.get("dnd", False)),
            "assignedTo": r.get("assigned_to") or "",
        } for r in rows]
    except Exception as e:
        logger.error(f"get_cached_contacts failed for {location_id}: {e}")
        return []
    finally:
        return_db_connection(conn)


def get_contact_cache_age(location_id: str):
    """Return the max synced_at timestamp for a location's cache, or None if empty."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(synced_at) as latest FROM contact_cache WHERE location_id = %s",
            (location_id,)
        )
        row = cur.fetchone()
        cur.close()
        return row["latest"] if row and row["latest"] else None
    except Exception as e:
        logger.error(f"get_contact_cache_age failed: {e}")
        return None
    finally:
        return_db_connection(conn)


def get_contact_cache_count(location_id: str) -> int:
    """Return the number of cached contacts for a location."""
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM contact_cache WHERE location_id = %s", (location_id,))
        row = cur.fetchone()
        cur.close()
        return row["cnt"] if row else 0
    except Exception as e:
        logger.error(f"get_contact_cache_count failed: {e}")
        return 0
    finally:
        return_db_connection(conn)


def upsert_contact_cache(location_id: str, contacts: list, prune_stale: bool = False) -> int:
    """Bulk upsert contacts into cache. Returns number of rows affected.
    prune_stale=True removes contacts not in the incoming list (safe only when
    the fetch was complete — i.e. pagination exhausted naturally, not truncated
    by max-page cap or API errors)."""
    if not location_id or not contacts:
        return 0
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        cur = conn.cursor()

        # Build values for bulk upsert
        from psycopg2.extras import execute_values
        import json as _json

        values = []
        incoming_ids = set()
        for c in contacts:
            cid = c.get("id", "")
            if not cid:
                continue
            incoming_ids.add(cid)
            values.append((
                location_id,
                cid,
                c.get("name") or f"{c.get('firstName', '')} {c.get('lastName', '')}".strip() or "Unknown",
                c.get("firstName", ""),
                c.get("lastName", ""),
                c.get("phone", ""),
                c.get("email", ""),
                _json.dumps(c.get("tags", [])),
                c.get("dateAdded", ""),
                bool(c.get("dnd", False)),
                c.get("assignedTo") or None,
            ))

        if not values:
            return 0

        # Bulk upsert
        execute_values(cur, """
            INSERT INTO contact_cache (location_id, contact_id, name, first_name, last_name, phone, email, tags, date_added, dnd, assigned_to, synced_at)
            VALUES %s
            ON CONFLICT (location_id, contact_id)
            DO UPDATE SET
                name = EXCLUDED.name,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                phone = EXCLUDED.phone,
                email = EXCLUDED.email,
                tags = EXCLUDED.tags,
                date_added = EXCLUDED.date_added,
                dnd = EXCLUDED.dnd,
                assigned_to = EXCLUDED.assigned_to,
                synced_at = CURRENT_TIMESTAMP
        """, values, template="(%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, CURRENT_TIMESTAMP)")

        total_upserted = len(values)  # cur.rowcount only reflects last execute_values batch

        # Only remove stale contacts when fetch was complete (all pages fetched
        # without error or truncation). Otherwise we'd delete contacts that
        # simply weren't returned because the fetch was cut short.
        if prune_stale and incoming_ids:
            cur.execute(
                "DELETE FROM contact_cache WHERE location_id = %s AND contact_id != ALL(%s)",
                (location_id, list(incoming_ids))
            )
            stale_removed = cur.rowcount
            if stale_removed > 0:
                logger.info(f"Removed {stale_removed} stale cached contacts for {location_id}")

        conn.commit()
        cur.close()
        logger.info(f"Contact cache upserted: {total_upserted} contacts for {location_id} (pruned={prune_stale})")
        return total_upserted
    except Exception as e:
        logger.error(f"upsert_contact_cache failed for {location_id}: {e}")
        if conn:
            conn.rollback()
        return 0
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════
#  AGENCY / WHITE-LABEL — company metadata and branding helpers
# ═══════════════════════════════════════════════════════════════

def get_agency_by_company_id(company_id: str) -> Optional[Dict[str, Any]]:
    """Look up agency_billing row by GHL companyId. Returns dict or None."""
    if not company_id:
        return None
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM agency_billing WHERE company_id = %s LIMIT 1", (company_id,))
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    except Exception as e:
        logger.error(f"get_agency_by_company_id failed for {company_id}: {e}")
        return None
    finally:
        return_db_connection(conn)


def get_agency_members_by_company_id(company_id: str) -> list:
    """Get all subscribers linked to an agency via company_id."""
    if not company_id:
        return []
    conn = get_db_connection()
    if not conn:
        return []
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT location_id, email, full_name, phone, role, subscription_tier,
                   bot_first_name, timezone, access_token, token_expires_at,
                   onboarding_status, agent_email, invite_sent_at, invite_claimed_at,
                   created_at, voice_config, sms_send_via
            FROM subscribers
            WHERE company_id = %s
            ORDER BY created_at DESC
        """, (company_id,))
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_agency_members_by_company_id failed for {company_id}: {e}")
        return []
    finally:
        return_db_connection(conn)


def save_whitelabel_config(agency_email: str, config: dict) -> bool:
    """Save white-label branding config for an agency owner."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE agency_billing
            SET whitelabel_config = %s, updated_at = NOW()
            WHERE agency_email = %s
        """, (json.dumps(config), agency_email))
        conn.commit()
        cur.close()
        return cur.rowcount > 0
    except Exception as e:
        logger.error(f"save_whitelabel_config failed for {agency_email}: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        return_db_connection(conn)


def get_whitelabel_config(agency_email: str) -> dict:
    """Get white-label branding config for an agency owner."""
    if not agency_email:
        return {}
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT whitelabel_config FROM agency_billing WHERE agency_email = %s LIMIT 1",
            (agency_email,)
        )
        row = cur.fetchone()
        cur.close()
        return dict(row['whitelabel_config']) if row and row.get('whitelabel_config') else {}
    except Exception as e:
        logger.error(f"get_whitelabel_config failed for {agency_email}: {e}")
        return {}
    finally:
        return_db_connection(conn)


def get_whitelabel_for_user(user) -> dict:
    """Resolve white-label config for any user — agency owners get their own,
    sub-users get their agency's config via company_id lookup."""
    if not user:
        return {}
    # Agency owners: return their own config
    if getattr(user, 'role', None) == 'agency_owner':
        return getattr(user, 'whitelabel_config', None) or {}
    # Sub-users: look up by company_id
    company_id = getattr(user, 'company_id', None)
    if not company_id:
        # Fallback: try parent_agency_email
        parent = getattr(user, 'parent_agency_email', None)
        if parent:
            return get_whitelabel_config(parent)
        return {}
    conn = get_db_connection()
    if not conn:
        return {}
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT whitelabel_config FROM agency_billing WHERE company_id = %s LIMIT 1",
            (company_id,)
        )
        row = cur.fetchone()
        cur.close()
        return dict(row['whitelabel_config']) if row and row.get('whitelabel_config') else {}
    except Exception as e:
        logger.error(f"get_whitelabel_for_user failed for company_id={company_id}: {e}")
        return {}
    finally:
        return_db_connection(conn)


def update_agency_company_metadata(agency_email: str, company_id: str,
                                    company_name: str = None,
                                    company_owner_name: str = None,
                                    company_owner_email: str = None,
                                    company_owner_phone: str = None) -> bool:
    """Save company metadata to agency_billing. Called during OAuth."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE agency_billing
            SET company_id = %s,
                company_name = COALESCE(%s, company_name),
                company_owner_name = COALESCE(%s, company_owner_name),
                company_owner_email = COALESCE(%s, company_owner_email),
                company_owner_phone = COALESCE(%s, company_owner_phone),
                updated_at = NOW()
            WHERE agency_email = %s
        """, (company_id, company_name, company_owner_name,
              company_owner_email, company_owner_phone, agency_email))
        conn.commit()
        cur.close()
        return cur.rowcount > 0
    except Exception as e:
        logger.error(f"update_agency_company_metadata failed for {agency_email}: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        return_db_connection(conn)


def link_subscriber_to_agency(location_id: str, company_id: str,
                               parent_agency_email: str = None) -> bool:
    """Link a subscriber to an agency by company_id. Optionally set parent_agency_email."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        if parent_agency_email:
            cur.execute("""
                UPDATE subscribers
                SET company_id = %s, parent_agency_email = COALESCE(%s, parent_agency_email),
                    updated_at = NOW()
                WHERE location_id = %s
            """, (company_id, parent_agency_email, location_id))
        else:
            cur.execute("""
                UPDATE subscribers
                SET company_id = %s, updated_at = NOW()
                WHERE location_id = %s
            """, (company_id, location_id))
        conn.commit()
        cur.close()
        return cur.rowcount > 0
    except Exception as e:
        logger.error(f"link_subscriber_to_agency failed for {location_id}: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        return_db_connection(conn)
