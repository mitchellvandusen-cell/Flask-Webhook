import os
import logger
import psycopg2

def get_db_connection():
    """Get database connection"""
    try:
        return psycopg2.connect(os.environ.get("DATABASE_URL"))
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return None

def init_nlp_tables():
    """Initialize NLP memory tables"""
    conn = get_db_connection()
    if not conn:
        return False
    
    try:
        cur = conn.cursor()
        
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
        
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_contact_messages_contact_id 
            ON contact_messages(contact_id)
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nlp_memory (
                id SERIAL PRIMARY KEY,
                contact_id TEXT NOT NULL,
                message TEXT NOT NULL,
                role TEXT NOT NULL, -- 'lead' or 'assistant'
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS processed_webhooks (
                webhook_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

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
        
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_parsed_entities_contact_id 
            ON parsed_entities(contact_id)
        """)
        
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
        
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_topic_breakdown_contact_id 
            ON topic_breakdown(contact_id)
        """)
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS has_policy BOOLEAN;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_personal_policy BOOLEAN;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_employer_based BOOLEAN;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_term BOOLEAN;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_whole_life BOOLEAN;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_iul BOOLEAN;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_guaranteed_issue BOOLEAN;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS term_length INTEGER;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS face_amount TEXT;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS carrier TEXT;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS has_spouse BOOLEAN;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS num_kids INTEGER;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS tobacco_user BOOLEAN;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS age INTEGER;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS retiring_soon BOOLEAN;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS motivating_goal TEXT;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS has_other_policies BOOLEAN;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS medications TEXT;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_booked BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS is_qualified BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS appointment_time TEXT;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS already_handled BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS objection_path TEXT;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS waiting_for_health BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS waiting_for_other_policies BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS waiting_for_goal BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS carrier_gap_found BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS appointment_declined BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS waiting_for_medications BOOLEAN DEFAULT FALSE;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS blockers TEXT[] DEFAULT ARRAY[]::TEXT[];")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS notes TEXT;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS total_exchanges INTEGER DEFAULT 0;")
        cur.execute("ALTER TABLE contact_qualification ADD COLUMN IF NOT EXISTS dismissive_count INTEGER DEFAULT 0;")
        conn.commit()
        logger.info("NLP memory tables initialized")
        return True
        
    except Exception as e:
        logger.error(f"Error initializing NLP tables: {e}")
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()
