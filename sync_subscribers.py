import os
import requests
import csv
import io
import psycopg2
from psycopg2.extras import execute_values
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SHEET_CSV_URL = os.getenv("SUBSCRIBER_SHEET_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

def sync_subscribers():
    if not SHEET_CSV_URL:
        logger.error("SUBSCRIBER_SHEET_URL not set")
        return False
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set")
        return False

    conn = None
    try:
        # Fetch published CSV
        response = requests.get(SHEET_CSV_URL, timeout=10)
        response.raise_for_status()

        f = io.StringIO(response.text)
        reader = csv.DictReader(f)

        subscribers_to_sync = []
        for row_num, row in enumerate(reader, start=2):
            location_id = (row.get('location_id') or '').strip()
            bot_name = (row.get('bot_first_name') or 'Mitchell').strip()
            crm_api_key = (row.get('crm_api_key') or '').strip()
            timezone = (row.get('timezone') or 'America/Chicago').strip()
            crm_user_id = (row.get('crm_user_id') or '').strip()
            calendar_id = (row.get('calendar_id') or '').strip()
            initial_message = (row.get('initial_message') or '').strip()

            if not crm_user_id or not crm_api_key:
                logger.warning(f"Row {row_num} skipped: missing crm_user_id or crm_api_key")
                continue

            subscribers_to_sync.append((
                crm_user_id,
                location_id,
                bot_name,
                crm_api_key,
                timezone,
                calendar_id,
                initial_message
            ))

        if not subscribers_to_sync:
            logger.info("No valid subscribers to sync")
            return True

        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # === FIX OLD COLUMN NAMES (PostgreSQL-safe) ===
        try:
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'subscribers' AND column_name = 'ghl_location_id' LIMIT 1;")
            if cur.fetchone():
                cur.execute("ALTER TABLE subscribers RENAME COLUMN ghl_location_id TO location_id;")

            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'subscribers' AND column_name = 'ghl_user_id' LIMIT 1;")
            if cur.fetchone():
                cur.execute("ALTER TABLE subscribers RENAME COLUMN ghl_user_id TO crm_user_id;")

            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'subscribers' AND column_name = 'ghl_api_key' LIMIT 1;")
            if cur.fetchone():
                cur.execute("ALTER TABLE subscribers RENAME COLUMN ghl_api_key TO crm_api_key;")

            cur.execute("ALTER TABLE subscribers DROP CONSTRAINT IF EXISTS subscribers_pkey;")
            cur.execute("ALTER TABLE subscribers ADD CONSTRAINT subscribers_pkey PRIMARY KEY (crm_user_id);")

            conn.commit()
            logger.info("Old column names and primary key fixed")
        except Exception as e:
            logger.warning(f"Column rename skipped (likely already done): {e}")
            conn.rollback()

        # Create table with current schema
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                location_id TEXT PRIMARY KEY,
                bot_first_name TEXT,
                crm_api_key TEXT,
                timezone TEXT,
                crm_user_id TEXT,
                calendar_id TEXT,
                initial_message TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Add any missing columns
        cur.execute("""
            ALTER TABLE subscribers
            ADD COLUMN IF NOT EXISTS crm_user_id TEXT,
            ADD COLUMN IF NOT EXISTS calendar_id TEXT,
            ADD COLUMN IF NOT EXISTS initial_message TEXT;
        """)

        # UPSERT
        upsert_query = """
            INSERT INTO subscribers (
                location_id, bot_first_name, crm_api_key, timezone,
                crm_user_id, calendar_id, initial_message
            ) VALUES %s
            ON CONFLICT (location_id) DO UPDATE SET
                bot_first_name = EXCLUDED.bot_first_name,
                crm_api_key = EXCLUDED.crm_api_key,
                timezone = EXCLUDED.timezone,
                crm_user_id = EXCLUDED.crm_user_id,
                calendar_id = EXCLUDED.calendar_id,
                initial_message = EXCLUDED.initial_message,
                updated_at = CURRENT_TIMESTAMP;
        """

        execute_values(cur, upsert_query, subscribers_to_sync)
        conn.commit()
        logger.info(f"Synced {len(subscribers_to_sync)} subscribers successfully")

        return True

    except Exception as e:
        logger.error(f"Sync failed: {e}")
        return False

    finally:
        if conn:
            try:
                cur.close()
            except:
                pass
            conn.close()

if __name__ == "__main__":
    sync_subscribers()