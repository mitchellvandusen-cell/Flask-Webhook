# db.py - PostgreSQL Database Utilities (Production 2026) - FIXED VERSION
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
                initial_message TEXT,
                subscription_tier TEXT DEFAULT 'agency_starter',
                max_seats INTEGER DEFAULT 10,
                active_seats INTEGER DEFAULT 0,
                stripe_customer_id TEXT,
               
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
        
        conn.commit()
        logger.info("Database initialized: All tables ready (including contact_narratives).")
        return True
    except psycopg2.Error as e:
        logger.critical(f"Database initialization failed: {e}", exc_info=True)
        if conn: conn.rollback()
        return False
    finally:
        if conn:
            cur.close()
            conn.close()


class User(UserMixin):
    def __init__(self, data: dict):
        # Core identification (works for both agency_billing and subscribers)
        self.email = data.get('agency_email') or data.get('email')
        self.id = self.email  # Flask-Login requires this

        self.password_hash = data.get('password_hash')

        # Location & GHL identifiers
        self.location_id = data.get('location_id')
        self.ghl_calendar_id = data.get('ghl_calendar_id')
        self.crm_api_key = data.get('crm_api_key')
        self.crm_user_id = data.get('crm_user_id')
        self.calendar_id = data.get('calendar_id')

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

        # Agency-specific billing fields
        self.max_seats = data.get('max_seats')
        self.active_seats = data.get('active_seats')
        self.next_billing_date = data.get('next_billing_date')

        # Sub-user onboarding system fields
        self.agent_email = data.get('agent_email')
        self.invite_token = data.get('invite_token')
        self.invite_sent_at = data.get('invite_sent_at')
        self.invite_claimed_at = data.get('invite_claimed_at')
        self.onboarding_status = data.get('onboarding_status', 'pending')

        # Timestamps
        self.created_at = data.get('created_at')
        self.updated_at = data.get('updated_at')
   
    @property
    def is_agency_owner(self) -> bool:
        return self.role == 'agency_owner'
   
    @staticmethod
    def get(email: str) -> Optional['User']:
        """
        Fetch user from BOTH tables - subscribers first, then agency_billing.
        Returns User object or None if no match.
        
        FIXED: Now checks both tables so agency owners can log in.
        """
        if not email:
            return None
            
        logger.debug(f"User.get called for email: '{email}'")
       
        conn = get_db_connection()
        if not conn:
            logger.debug("DB connection failed")
            return None
       
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            
            # 1. Check subscribers table first (most users)
            cur.execute("""
                SELECT * FROM subscribers WHERE email = %s LIMIT 1
            """, (email,))
            row = cur.fetchone()
            
            if row:
                logger.debug(f"Found user in subscribers table")
                return User(row)
            
            # 2. Check agency_billing table (agency owners)
            cur.execute("""
                SELECT * FROM agency_billing WHERE agency_email = %s LIMIT 1
            """, (email,))
            row = cur.fetchone()
            
            if row:
                logger.debug(f"Found user in agency_billing table")
                return User(row)
            
            logger.debug(f"No match found for '{email}' in either table")
            return None
       
        except psycopg2.Error as e:
            logger.error(f"DB error in User.get: {e}")
            return None
       
        finally:
            if 'cur' in locals():
                cur.close()
            if conn:
                conn.close()
   
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
                conn.close()
   
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
                conn.close()


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
            conn.close()


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
        from main import gc, sheet_url
    except ImportError:
        logger.warning("Sheets recovery unavailable: Credentials or URL missing.")
        return None
   
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
        if 'cur' in locals():
            cur.close()
        if conn:
            conn.close()


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
        if 'cur' in locals():
            cur.close()
        if conn:
            conn.close()