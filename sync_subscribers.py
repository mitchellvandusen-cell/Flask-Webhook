import os
import requests
import csv
import io
import psycopg2
from psycopg2.extras import execute_values
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# URL of your Google Sheet published as CSV
# Ensure your sheet is: File -> Share -> Publish to Web -> Link -> CSV
SHEET_CSV_URL = os.getenv("SUBSCRIBER_SHEET_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

def sync_subscribers():
    if not SHEET_CSV_URL or not DATABASE_URL:
        logger.error("Missing SHEET_CSV_URL or DATABASE_URL environment variables.")
        return

    try:
        # 1. Fetch the data from Google Sheets
        response = requests.get(SHEET_CSV_URL)
        response.raise_for_status()
        
        # 2. Parse CSV
        f = io.StringIO(response.text)
        reader = csv.DictReader(f)
        
        # Normalize headers to lowercase to match our logic
        subscribers_to_sync = []
        for row in reader:
            # Expected columns in Sheet: location_id, bot_first_name, ghl_api_key, timezone
            subscribers_to_sync.append((
                row.get('location_id').strip(),
                row.get('bot_first_name', 'Mitchell').strip(),
                row.get('api_key').strip(),
                row.get('timezone', 'America/Chicago').strip(),
                row.get('crm_user_id').strip(),
                row.get('calendar_id').strip(),
                row.get(('initial_message') or '').strip()
            ))

        if not subscribers_to_sync:
            logger.warning("No subscribers found in the sheet.")
            return

        # 3. Connect to Database and Upsert
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # Create table if it doesn't exist
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                crm_location_id TEXT PRIMARY KEY,
                bot_first_name TEXT,
                crm_user_id TEXT,
                calendar_id TEXT,
                initial_message TEXT,
                crm_api_key TEXT,
                timezone TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Perform the UPSERT (Update on conflict)
        upsert_query = """
            INSERT INTO subscribers (crm_location_id, bot_first_name, crm_api_key, timezone, initial_message, crm_user_id, calendar_id)
            VALUES %s
            ON CONFLICT (ghl_location_id) 
            DO UPDATE SET 
                bot_first_name = EXCLUDED.bot_first_name,
                crm_api_key = EXCLUDED.crm_api_key,
                timezone = EXCLUDED.timezone,
                crm_user_id = EXCLUDED.crm_user_id,
                initial_message = EXCLUDED.initial_message,
                calendar_id = EXCLUDED.calendar_id,
                updated_at = CURRENT_TIMESTAMP;
        """

        execute_values(cur, upsert_query, subscribers_to_sync)    
        conn.commit()
        logger.info(f"Successfully synced {len(subscribers_to_sync)} subscribers from Google Sheets.")

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