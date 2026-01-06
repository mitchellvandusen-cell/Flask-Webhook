import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor

# Initialize logger at the top so it's available for all functions
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_db_connection():
    """Get database connection"""
    try:
        url = os.environ.get("DATABASE_URL")
        if not url:
            logger.error("DATABASE_URL environment variable is not set")
            return None
        return psycopg2.connect(url)
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return None

def init_subscriber_table():
    """Initialize the subscribers table for multi-tenancy"""
    conn = get_db_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                ghl_location_id TEXT PRIMARY KEY,
                ghl_api_key TEXT NOT NULL,
                ghl_calendar_id TEXT,
                ghl_user_id TEXT,
                bot_first_name TEXT DEFAULT 'Mitchell',
                timezone TEXT DEFAULT 'America/Chicago',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        logger.info("Subscriber table initialized")
        return True
    except Exception as e:
        logger.error(f"Error initializing subscriber table: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            cur.close()
            conn.close()

def init_nlp_tables():
    """Initialize NLP memory and qualification tables"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cur = conn.cursor()
        
        # 1. Message Logs
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contact_messages (
                id SERIAL PRIMARY KEY,
                contact_id TEXT NOT NULL,
                message_type TEXT NOT NULL,
                message_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                parsed_at TIMESTAMP,
                UNIQUE(contact_id, message_text, message_type, created_at)
            )
        """)
        
        cur.execute("CREATE INDEX IF NOT EXISTS idx_contact_messages_contact_id ON contact_messages(contact_id)")
        
        # 2. Short-term Memory
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nlp_memory (
                id SERIAL PRIMARY KEY,
                contact_id TEXT NOT NULL,
                message TEXT NOT NULL,
                role TEXT NOT NULL, -- 'lead' or 'assistant'
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 3. Qualification Data (Combined and Flawless)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contact_qualification (
                contact_id TEXT PRIMARY KEY,
                topics_asked TEXT[] DEFAULT ARRAY[]::TEXT[],
                key_quotes TEXT[] DEFAULT ARRAY[]::TEXT[],
                blockers TEXT[] DEFAULT ARRAY[]::TEXT[],
                health_conditions TEXT[] DEFAULT ARRAY[]::TEXT[],
                health_details TEXT[] DEFAULT ARRAY[]::TEXT[],
                total_exchanges INTEGER DEFAULT 0,
                dismissive_count INTEGER DEFAULT 0,
                has_policy BOOLEAN,
                is_personal_policy BOOLEAN,
                is_employer_based BOOLEAN,
                is_term BOOLEAN,
                is_whole_life BOOLEAN,
                is_iul BOOLEAN,
                is_guaranteed_issue BOOLEAN,
                term_length INTEGER,
                face_amount TEXT,
                carrier TEXT,
                has_spouse BOOLEAN,
                num_kids INTEGER,
                tobacco_user BOOLEAN,
                age INTEGER,
                retiring_soon BOOLEAN,
                motivating_goal TEXT,
                has_other_policies BOOLEAN,
                medications TEXT,
                is_booked BOOLEAN DEFAULT FALSE,
                is_qualified BOOLEAN DEFAULT FALSE,
                appointment_time TEXT,
                already_handled BOOLEAN DEFAULT FALSE,
                objection_path TEXT,
                waiting_for_health BOOLEAN DEFAULT FALSE,
                waiting_for_other_policies BOOLEAN DEFAULT FALSE,
                waiting_for_goal BOOLEAN DEFAULT FALSE,
                carrier_gap_found BOOLEAN DEFAULT FALSE,
                appointment_declined BOOLEAN DEFAULT FALSE,
                waiting_for_medications BOOLEAN DEFAULT FALSE,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 4. Webhook Deduplication
        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_webhooks (
                webhook_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # 5. Entity Parsing
        cur.execute("""
            CREATE TABLE IF NOT EXISTS parsed_entities (
                id SERIAL PRIMARY KEY,
                contact_id TEXT NOT NULL,
                message_id INTEGER REFERENCES contact_messages(id) ON DELETE CASCADE,
                entity_type TEXT NOT NULL,
                entity_text TEXT NOT NULL,
                entity_label TEXT,
                confidence FLOAT DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cur.execute("CREATE INDEX IF NOT EXISTS idx_parsed_entities_contact_id ON parsed_entities(contact_id)")
        
        # 6. Topic Tracking
        cur.execute("""
            CREATE TABLE IF NOT EXISTS topic_breakdown (
                id SERIAL PRIMARY KEY,
                contact_id TEXT NOT NULL,
                topic_name TEXT NOT NULL,
                topic_value TEXT,
                source_message_id INTEGER REFERENCES contact_messages(id) ON DELETE CASCADE,
                confidence FLOAT DEFAULT 1.0,
                times_mentioned INTEGER DEFAULT 1,
                first_mentioned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_mentioned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(contact_id, topic_name)
            )
        """)
        
        cur.execute("CREATE INDEX IF NOT EXISTS idx_topic_breakdown_contact_id ON topic_breakdown(contact_id)")

        # Ensure all columns exist (Safety for existing databases)
        columns_to_add = [
            ("has_policy", "BOOLEAN"), ("is_personal_policy", "BOOLEAN"), 
            ("is_employer_based", "BOOLEAN"), ("is_term", "BOOLEAN"),
            ("is_whole_life", "BOOLEAN"), ("is_iul", "BOOLEAN"),
            ("is_guaranteed_issue", "BOOLEAN"), ("term_length", "INTEGER"),
            ("face_amount", "TEXT"), ("carrier", "TEXT"),
            ("has_spouse", "BOOLEAN"), ("num_kids", "INTEGER"),
            ("tobacco_user", "BOOLEAN"), ("age", "INTEGER"),
            ("retiring_soon", "BOOLEAN"), ("motivating_goal", "TEXT"),
            ("has_other_policies", "BOOLEAN"), ("medications", "TEXT"),
            ("is_booked", "BOOLEAN DEFAULT FALSE"), ("is_qualified", "BOOLEAN DEFAULT FALSE"),
            ("appointment_time", "TEXT"), ("already_handled", "BOOLEAN DEFAULT FALSE"),
            ("objection_path", "TEXT"), ("waiting_for_health", "BOOLEAN DEFAULT FALSE"),
            ("waiting_for_other_policies", "BOOLEAN DEFAULT FALSE"), ("waiting_for_goal", "BOOLEAN DEFAULT FALSE"),
            ("carrier_gap_found", "BOOLEAN DEFAULT FALSE"), ("appointment_declined", "BOOLEAN DEFAULT FALSE"),
            ("waiting_for_medications", "BOOLEAN DEFAULT FALSE"), ("notes", "TEXT"),
            ("total_exchanges", "INTEGER DEFAULT 0"), ("dismissive_count", "INTEGER DEFAULT 0")
        ]
        
        for col_name, col_type in columns_to_add:
            cur.execute(f"ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS {col_name} {col_type};")

        conn.commit()
        logger.info("All database tables and columns verified")
        return True

    except Exception as e:
        logger.error(f"Error initializing NLP tables: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            cur.close()
            conn.close()

def get_subscriber_info(location_id):
    """Retrieves the marketer's specific config from the database."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM subscribers WHERE ghl_location_id = %s", (location_id,))
        subscriber = cur.fetchone()
        return subscriber
    except Exception as e:
        logger.error(f"Error fetching subscriber info for {location_id}: {e}")
        return None
    finally:
        if conn:
            cur.close()
            conn.close()