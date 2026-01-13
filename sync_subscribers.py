# sync_subscribers.py - Google Sheet → PostgreSQL Sync (OAuth-Aligned, Production Hardened)
import os
import requests
import csv
import io
import logging
import psycopg2
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SHEET_CSV_URL = os.getenv("SUBSCRIBER_SHEET_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

def sync_subscribers() -> bool:
    """
    Syncs subscribers from public Google Sheet CSV to PostgreSQL.
    Fully dynamic headers, OAuth tokens (access_token + refresh_token), skips invalid rows.
    Returns True on success.
    """
    if not SHEET_CSV_URL:
        logger.critical("SUBSCRIBER_SHEET_URL not set")
        return False
    if not DATABASE_URL:
        logger.critical("DATABASE_URL not set")
        return False

    # ─── Fetch CSV ───
    try:
        response = requests.get(SHEET_CSV_URL, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch sheet CSV: {e}")
        return False

    # Parse CSV
    f = io.StringIO(response.text)
    reader = csv.DictReader(f)
    subscribers_to_sync = []

    for row_num, row in enumerate(reader, start=2):
        location_id = (row.get('location_id') or '').strip()
        access_token = (row.get('access_token') or '').strip()
        refresh_token = (row.get('refresh_token') or '').strip()
        bot_name = (row.get('bot_first_name') or 'Mitchell').strip()
        timezone = (row.get('timezone') or 'America/Chicago').strip()
        crm_user_id = (row.get('crm_user_id') or '').strip()
        calendar_id = (row.get('calendar_id') or '').strip()
        initial_message = (row.get('initial_message') or '').strip()

        # Require location + at least one usable token (OAuth reality)
        if not location_id or (not access_token and not refresh_token):
            logger.warning(f"Row {row_num} skipped: missing location_id or any OAuth token")
            continue

        subscribers_to_sync.append((
            location_id,
            bot_name,
            access_token,
            refresh_token,
            timezone,
            crm_user_id,
            calendar_id,
            initial_message
        ))

    if not subscribers_to_sync:
        logger.info("No valid subscribers to sync")
        return True

    # ─── Database Connection & Migration ───
    conn = None
    try:
        conn = psycopg2.connect(
            DATABASE_URL,
            connect_timeout=10,
        )
        conn.autocommit = False
        cur = conn.cursor()

        # Fix legacy columns (safe, idempotent)
        legacy_fixes = [
            ("ghl_location_id", "location_id"),
            ("ghl_user_id", "crm_user_id"),
            ("ghl_api_key", "access_token"),  # Map old API key → access_token if present
        ]
        for old, new in legacy_fixes:
            cur.execute("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'subscribers' AND column_name = %s
            """, (old,))
            if cur.fetchone():
                cur.execute(f"ALTER TABLE subscribers RENAME COLUMN {old} TO {new};")
                logger.info(f"Renamed legacy column: {old} → {new}")

        # Drop/re-add PK if needed
        cur.execute("ALTER TABLE subscribers DROP CONSTRAINT IF EXISTS subscribers_pkey;")
        cur.execute("ALTER TABLE subscribers ADD CONSTRAINT subscribers_pkey PRIMARY KEY (location_id);")

        # Create table with OAuth fields
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                location_id TEXT PRIMARY KEY,
                bot_first_name TEXT,
                access_token TEXT,
                refresh_token TEXT,
                timezone TEXT,
                crm_user_id TEXT,
                calendar_id TEXT,
                initial_message TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Add optional columns if missing
        cur.execute("""
            ALTER TABLE subscribers
            ADD COLUMN IF NOT EXISTS access_token TEXT,
            ADD COLUMN IF NOT EXISTS refresh_token TEXT,
            ADD COLUMN IF NOT EXISTS crm_user_id TEXT,
            ADD COLUMN IF NOT EXISTS calendar_id TEXT,
            ADD COLUMN IF NOT EXISTS initial_message TEXT;
        """)

        conn.commit()
        logger.info("Schema migrations complete")

        # ─── UPSERT Subscribers ───
        upsert_query = """
            INSERT INTO subscribers (
                location_id, bot_first_name, access_token, refresh_token,
                timezone, crm_user_id, calendar_id, initial_message
            ) VALUES %s
            ON CONFLICT (location_id) DO UPDATE SET
                bot_first_name = EXCLUDED.bot_first_name,
                access_token = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                timezone = EXCLUDED.timezone,
                crm_user_id = EXCLUDED.crm_user_id,
                calendar_id = EXCLUDED.calendar_id,
                initial_message = EXCLUDED.initial_message,
                updated_at = CURRENT_TIMESTAMP;
        """

        execute_values(cur, upsert_query, subscribers_to_sync)
        conn.commit()
        logger.info(f"Successfully synced {len(subscribers_to_sync)} subscribers")

        return True

    except psycopg2.Error as e:
        logger.critical(f"PostgreSQL sync failed: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return False
    except Exception as e:
        logger.critical(f"Unexpected sync error: {e}", exc_info=True)
        return False
    finally:
        if conn:
            try:
                if 'cur' in locals():
                    cur.close()
            except:
                pass
            conn.close()

if __name__ == "__main__":
    sync_subscribers()