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
        # Fetch CSV
        response = requests.get(SHEET_CSV_URL)
        response.raise_for_status()

        f = io.StringIO(response.text)
        reader = csv.DictReader(f)

        subscribers_to_sync = []
        for row in reader:
            # Safely extract and strip — default to empty string if missing
            location_id = (row.get('location_id') or '').strip()
            bot_name = (row.get('bot_first_name') or 'Mitchell').strip()
            crm_api_key = (row.get('crm_api_key') or '').strip()
            timezone = (row.get('timezone') or 'America/Chicago').strip()
            crm_user_id = (row.get('crm_user_id') or '').strip()
            calendar_id = (row.get('calendar_id') or '').strip()
            initial_message = (row.get('initial_message') or '').strip()

            # Skip if required fields missing
            if not location_id or not crm_api_key:
                logger.warning(f"Skipping row (missing location_id or crm_api_key): {row}")
                continue

            subscribers_to_sync.append((
                location_id,
                bot_name,
                crm_api_key,
                timezone,
                crm_user_id,
                calendar_id,
                initial_message
            ))

        if not subscribers_to_sync:
            logger.info("No valid subscribers to sync")
            return True

        # Connect to DB
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # Create table (consistent column names)
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

        # Add missing columns if needed
        cur.execute("""
            ALTER TABLE subscribers
            ADD COLUMN IF NOT EXISTS crm_user_id TEXT,
            ADD COLUMN IF NOT EXISTS calendar_id TEXT,
            ADD COLUMN IF NOT EXISTS initial_message TEXT;
        """)

        # UPSERT — consistent column order
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