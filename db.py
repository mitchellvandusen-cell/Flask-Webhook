# db.py - PostgreSQL Database Utilities (Flawless 2026)
import os
import logging
import uuid
import gspread
import json
from oauth2client.service_account import ServiceAccountCredentials
from typing import Optional, Dict, Any
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

logger = logging.getLogger(__name__)


# Google Sheets Setup
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

def get_db_connection() -> Optional[psycopg2.extensions.connection]:
    """Get a new PostgreSQL connection with RealDictCursor."""
    if not DATABASE_URL:
        logger.critical("DATABASE_URL not set")
        return None
    try:
        return psycopg2.connect(
            DATABASE_URL,
            connect_timeout=10,
            cursor_factory=RealDictCursor,
        )
    except psycopg2.Error as e:
        logger.error(f"Database connection failed: {e}", exc_info=True)
        return None

def init_db() -> bool:
    """Initialize all required tables — idempotent and safe."""
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()

        # Users table (auth + Stripe)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                email TEXT PRIMARY KEY,
                password_hash TEXT,
                stripe_customer_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Subscribers table (per-location GHL config)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                location_id TEXT PRIMARY KEY,
                bot_first_name TEXT DEFAULT 'Grok',
                access_token TEXT,
                refresh_token TEXT,
                token_expires_at TIMESTAMP,
                token_type TEXT DEFAULT 'Bearer',
                timezone TEXT DEFAULT 'America/Chicago',
                crm_user_id TEXT,
                calendar_id TEXT,
                initial_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Add missing OAuth columns (safe ALTER)
        for col in ['access_token', 'refresh_token', 'token_expires_at', 'token_type']:
            cur.execute(f"ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS {col} TEXT;")

        # Legacy migration (one-time)
        try:
            cur.execute("UPDATE subscribers SET access_token = crm_api_key WHERE access_token IS NULL AND crm_api_key IS NOT NULL")
        except psycopg2.Error:
            pass  # Column may not exist

        # Messages (with type constraint)
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_messages_created ON contact_messages (contact_id, created_at DESC);")

        # Facts (unique per contact)
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

        # Narrative (one per contact)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contact_narratives (
                contact_id TEXT PRIMARY KEY,
                story_narrative TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Webhook deduplication
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_webhooks (
                webhook_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.commit()
        logger.info("All database tables initialized successfully")
        return True

    except psycopg2.Error as e:
        logger.critical(f"Database initialization failed: {e}", exc_info=True)
        conn.rollback()
        return False
    finally:
        if conn:
            cur.close()
            conn.close()

class User(UserMixin):
    def __init__(self, email: str, password_hash: Optional[str] = None, stripe_customer_id: Optional[str] = None):
        self.id = email
        self.email = email
        self.password_hash = password_hash
        self.stripe_customer_id = stripe_customer_id

    @staticmethod
    def get(email: str) -> Optional['User']:
        conn = get_db_connection()
        if not conn:
            return None
        try:
            cur = conn.cursor()
            cur.execute("SELECT email, password_hash, stripe_customer_id FROM users WHERE email = %s", (email,))
            row = cur.fetchone()
            if row:
                return User(row['email'], row['password_hash'], row['stripe_customer_id'])
            return None
        except psycopg2.Error as e:
            logger.error(f"User.get failed for {email}: {e}")
            return None
        finally:
            if conn:
                cur.close()
                conn.close()

    @staticmethod
    def create(email: str, password: Optional[str] = None, stripe_customer_id: Optional[str] = None) -> bool:
        password_hash = generate_password_hash(password) if password else None
        conn = get_db_connection()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO users (email, password_hash, stripe_customer_id) VALUES (%s, %s, %s)",
                (email, password_hash, stripe_customer_id)
            )
            conn.commit()
            return True
        except psycopg2.IntegrityError:
            logger.warning(f"User.create: duplicate email {email}")
            return False
        except psycopg2.Error as e:
            logger.error(f"User.create failed for {email}: {e}")
            conn.rollback()
            return False
        finally:
            if conn:
                cur.close()
                conn.close()


def get_subscriber_info_sql(location_id: str) -> Optional[Dict[str, Any]]:
    """
    Fast PostgreSQL lookup for subscriber info.
    """
    conn = get_db_connection()
    if not conn:
        return None

    try:
        # Note: We don't need to pass cursor_factory here if it's in the connection
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM subscribers 
            WHERE location_id = %s
            LIMIT 1
        """, (location_id,))
        
        row = cur.fetchone()
        return dict(row) if row else None

    except Exception as e:
        logger.error(f"SQL subscriber lookup failed for {location_id}: {e}")
        return None
    finally:
        if 'cur' in locals(): cur.close()
        if 'conn' in locals(): conn.close()

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
    # Lazy import to avoid circular dependency with main.py
    from main import gc, sheet_url 
    
    if not gc or not sheet_url:
        logger.warning("Sheets recovery unavailable: Credentials or URL missing.")
        return None

    try:
        logger.info(f"SQL miss for {location_id} — initiating Sheets recovery...")

        sh = gc.open_by_url(sheet_url)
        # Targeted specifically to the 'Subscribers' tab you created
        worksheet = sh.worksheet("Subscribers")
        
        # Pull all headers to create the map
        headers = [h.strip().lower() for h in worksheet.row_values(1)]
        
        # The exact headers we expect to find and return
        expected_headers = [
            "location_id", "calendar_id", "access_token", "refresh_token",
            "crm_user_id", "bot_first_name", "timezone", "email", "initial_message",
            "confirmation_code", "stripe_customer_id", "parent_agency_email", "role"
        ]

        # Map header names to their column index (0-based)
        col_map = {hdr: headers.index(hdr) for hdr in expected_headers if hdr in headers}
        
        if "location_id" not in col_map:
            logger.error("Critical: 'location_id' column not found in Subscribers sheet.")
            return None

        # Optimization: Use .find() for a targeted search
        # find() is 1-based, so we add 1 to the index
        cell = worksheet.find(location_id, in_column=col_map["location_id"] + 1)
        
        if cell:
            row_data = worksheet.row_values(cell.row)
            
            # Build the subscriber dictionary based on your specific columns
            subscriber = {}
            for hdr, idx in col_map.items():
                if idx < len(row_data):
                    subscriber[hdr] = row_data[idx]
            
            logger.info(f"Sheets recovery success for {location_id} (Relational Sync Active)")
            return subscriber

        logger.warning(f"Location {location_id} not found in SQL or Google Sheets.")
        return None

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
        return cur.fetchone()['count']
    except psycopg2.Error as e:
        logger.error(f"get_message_count failed for {contact_id}: {e}")
        return 0
    finally:
        if conn:
            cur.close()
            conn.close()

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
        if conn:
            cur.close()
            conn.close()

# Add these columns to your existing 'users' and 'subscribers' tables
def upgrade_db_for_agency():
    conn = get_db_connection()
    cur = conn.cursor()
    # Track who is an Agency Owner vs a Standard User
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user';") # 'user' or 'agency_owner'
    
    # Link subscribers to an Agency Owner
    cur.execute("ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS parent_agency_email TEXT;")
    cur.execute("ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS tier TEXT DEFAULT 'individual';") # 'individual', 'starter', 'pro'
    conn.commit()
    cur.close()

def update_subscriber_token(
    location_id: str,
    access_token: str,
    refresh_token: Optional[str] = None,
    expires_in: int = 86400
) -> bool:
    """Update OAuth tokens with expiry."""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE subscribers 
            SET access_token = %s,
                refresh_token = COALESCE(%s, refresh_token),
                token_expires_at = NOW() + interval '%s seconds',
                updated_at = NOW()
            WHERE location_id = %s
        """, (access_token, refresh_token, expires_in, location_id))
        conn.commit()
        return cur.rowcount > 0
    except psycopg2.Error as e:
        logger.error(f"update_subscriber_token failed for {location_id}: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            cur.close()
            conn.close()