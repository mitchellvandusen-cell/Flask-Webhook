import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_db_connection():
    """Get database connection from DATABASE_URL"""
    try:
        url = os.environ.get("DATABASE_URL")
        if not url:
            logger.error("DATABASE_URL environment variable is not set")
            return None
        return psycopg2.connect(url)
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return None

# ===================================
# INITIALIZATION
# ===================================

def init_db():
    """Initialize all required tables"""
    conn = get_db_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()

        # 1. Subscribers (multi-tenancy)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                ghl_location_id TEXT PRIMARY KEY,
                ghl_api_key TEXT NOT NULL,
                ghl_calendar_id TEXT,
                ghl_user_id TEXT,
                bot_first_name TEXT DEFAULT 'Grok',
                timezone TEXT DEFAULT 'America/Chicago',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 2. Raw message log (for conversation history)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contact_messages (
                id SERIAL PRIMARY KEY,
                contact_id TEXT NOT NULL,
                message_type TEXT NOT NULL CHECK (message_type IN ('lead', 'assistant')),
                message_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_contact_messages_contact_id (contact_id),
                INDEX idx_contact_messages_created (contact_id, created_at DESC)
            );
        """)

        # 3. Grok-extracted facts (core memory)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contact_facts (
                id SERIAL PRIMARY KEY,
                contact_id TEXT NOT NULL,
                fact_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(contact_id, fact_text),
                INDEX idx_contact_facts_contact_id (contact_id)
            );
        """)

        # 4. Webhook deduplication
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_webhooks (
                webhook_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        conn.commit()
        logger.info("Database tables initialized successfully")
        return True

    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        conn.rollback()
        return False
    finally:
        if conn:
            cur.close()
            conn.close()

# ===================================
# SUBSCRIBER LOOKUP
# ===================================

def get_subscriber_info(location_id: str) -> dict | None:
    """Get subscriber config by GoHighLevel location ID"""
    conn = get_db_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM subscribers WHERE ghl_location_id = %s",
            (location_id,)
        )
        subscriber = cur.fetchone()
        return dict(subscriber) if subscriber else None
    except Exception as e:
        logger.error(f"Error fetching subscriber {location_id}: {e}")
        return None
    finally:
        if conn:
            cur.close()
            conn.close()