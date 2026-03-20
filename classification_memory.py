# classification_memory.py — Embedding-based classification memory
# Learns from every conversation to make objection/stage detection near-perfect over time.
#
# Architecture:
#   1. Seed: 320+ keyword phrases from conversation_engine.py as ground truth (confidence 1.0)
#   2. Learn: Every LLM classification is stored with its embedding
#   3. Lookup: Before LLM call, vector similarity search for near-matches
#   4. Self-correct: Keyword cross-validation prevents bad data from entering
#                    Contradictions are deleted, confirmations are promoted
#
# Three-source consensus: Keywords (ground truth) > LLM (contextual) > Embeddings (learned)

import os
import json
import logging
import hashlib
from datetime import datetime, timedelta

from openai import OpenAI

from db import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

XAI_API_KEY = os.getenv("XAI_API_KEY")
XAI_EMBEDDING_MODEL = os.getenv("XAI_EMBEDDING_MODEL", "grok-embedding-small")
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "0"))  # 0 = use model default
_detected_dimensions = None  # Auto-detected from first API response

# Thresholds
DIRECT_USE_THRESHOLD = 0.95      # ≥ 95% similarity + consensus → skip LLM
HINT_THRESHOLD = 0.85            # 85-95% → inject as hint into LLM prompt
MIN_CONFIDENCE_FOR_DIRECT = 0.85 # Only entries at this confidence can be used for direct match
CONSENSUS_REQUIRED = 3           # Top-N must agree on classification
MIN_TENANT_DIVERSITY = 2         # Matches must come from 2+ different locations

# Client (shared with conversation_engine.py pattern)
_client = None
_embeddings_available = False

if XAI_API_KEY:
    _client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")


# ═══════════════════════════════════════════════════════════════════════════════
# TABLE MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

_table_ready = False


def _ensure_table():
    """Create learned_classifications table if it doesn't exist."""
    global _table_ready
    if _table_ready:
        return True

    conn = get_db_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()

        # Try to enable pgvector — graceful if not available
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.commit()
        except Exception:
            conn.rollback()
            logger.warning("pgvector extension not available — classification memory will use JSON fallback")

        # Create table with vector column if pgvector is available, else JSONB
        cur.execute("""
            CREATE TABLE IF NOT EXISTS learned_classifications (
                id SERIAL PRIMARY KEY,
                message_hash TEXT NOT NULL,
                message_text TEXT NOT NULL,
                embedding JSONB,
                objection_type TEXT NOT NULL,
                objection_nature TEXT,
                stage TEXT,
                confidence FLOAT DEFAULT 0.7,
                location_id TEXT,
                source TEXT DEFAULT 'llm',
                confirmation_count INT DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Indexes
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_lc_hash ON learned_classifications (message_hash)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_lc_confidence ON learned_classifications (confidence)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_lc_type ON learned_classifications (objection_type)
        """)

        # pgvector column is created dynamically by _ensure_vector_column()
        # after the first embedding call reveals the actual dimensions

        conn.commit()
        _table_ready = True
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to create learned_classifications table: {e}")
        return False
    finally:
        return_db_connection(conn)


_vector_col_ready = False


def _ensure_vector_column(dimensions: int):
    """Add pgvector column with auto-detected dimensions. Called once after first embedding."""
    global _vector_col_ready
    if _vector_col_ready:
        return

    conn = get_db_connection()
    if not conn:
        return

    try:
        cur = conn.cursor()
        cur.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'learned_classifications' AND column_name = 'embedding_vec'
                ) THEN
                    ALTER TABLE learned_classifications ADD COLUMN embedding_vec vector({dimensions});
                END IF;
            END $$;
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_lc_embedding_hnsw
            ON learned_classifications USING hnsw (embedding_vec vector_cosine_ops)
        """)
        conn.commit()
        _vector_col_ready = True
        logger.info(f"pgvector column created: vector({dimensions})")
    except Exception as e:
        conn.rollback()
        logger.info(f"pgvector column not created (extension may not be available): {e}")
        _vector_col_ready = True  # Don't retry — use JSONB fallback
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# EMBEDDING GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def _get_embedding(text: str) -> list:
    """
    Generate embedding via xAI API. Returns list of floats or empty list on failure.
    Cost: ~$0.00002 per call.
    Auto-detects embedding dimensions on first successful call.
    """
    global _embeddings_available, _detected_dimensions
    if not _client:
        return []

    try:
        kwargs = {"model": XAI_EMBEDDING_MODEL, "input": text}
        if EMBEDDING_DIMENSIONS > 0:
            kwargs["dimensions"] = EMBEDDING_DIMENSIONS

        response = _client.embeddings.create(**kwargs)
        embedding = response.data[0].embedding

        if not _embeddings_available:
            _detected_dimensions = len(embedding)
            logger.info(
                f"Embedding API connected: model={XAI_EMBEDDING_MODEL}, "
                f"dimensions={_detected_dimensions}"
            )
            # Try to create/update pgvector column with actual dimensions
            _ensure_vector_column(_detected_dimensions)

        _embeddings_available = True
        return embedding
    except Exception as e:
        if _embeddings_available:
            logger.warning(f"Embedding generation failed (transient): {e}")
        else:
            logger.info(f"Embedding API not available ({XAI_EMBEDDING_MODEL}): {e}")
        return []


def _cosine_similarity(a: list, b: list) -> float:
    """Compute cosine similarity between two vectors. Pure Python fallback."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _message_hash(text: str) -> str:
    """Deterministic hash for deduplication."""
    normalized = text.lower().strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


# ═══════════════════════════════════════════════════════════════════════════════
# LOOKUP — Search for similar prior classifications
# ═══════════════════════════════════════════════════════════════════════════════

def lookup_classification(message_text: str):
    """
    Search for similar prior classifications using embedding similarity.

    Returns:
        dict with keys:
            - match_type: "direct" (≥95%, use it), "hint" (85-95%, suggest to LLM), or None
            - objection_type: str or None
            - objection_nature: str or None
            - confidence: float (average similarity of top matches)
            - hint_text: str (for injection into LLM prompt when match_type="hint")
        Returns None if no match found or embeddings unavailable.
    """
    if not _embeddings_available:
        return None

    if not _ensure_table():
        return None

    embedding = _get_embedding(message_text)
    if not embedding:
        return None

    conn = get_db_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()

        # Try pgvector similarity search first (fast, indexed)
        try:
            cur.execute("""
                SELECT objection_type, objection_nature, confidence, location_id,
                       1 - (embedding_vec <=> %s::vector) AS similarity
                FROM learned_classifications
                WHERE embedding_vec IS NOT NULL
                  AND confidence >= %s
                ORDER BY embedding_vec <=> %s::vector
                LIMIT %s
            """, (str(embedding), MIN_CONFIDENCE_FOR_DIRECT, str(embedding), CONSENSUS_REQUIRED + 2))
            rows = cur.fetchall()
        except Exception:
            # pgvector not available — fall back to JSONB + Python cosine
            rows = _fallback_similarity_search(cur, embedding)

        if not rows or len(rows) < CONSENSUS_REQUIRED:
            return None

        # Check consensus: top-N must agree on objection_type
        top_n = rows[:CONSENSUS_REQUIRED]
        types = [r['objection_type'] for r in top_n]
        if len(set(types)) != 1:
            # No consensus — disagreement among top matches
            return None

        # Check multi-tenant diversity
        locations = set(r['location_id'] for r in top_n if r.get('location_id'))
        # For seed data (location_id = '__seed__'), count as one universal location
        real_locations = locations - {'__seed__'}
        has_seed = '__seed__' in locations
        # Seed + 1 real location counts as diversity, or 2+ real locations
        if len(real_locations) < MIN_TENANT_DIVERSITY and not (has_seed and len(real_locations) >= 1):
            # Not enough tenant diversity yet — only seed data or single-tenant
            # Still useful as hint but not for direct use
            avg_sim = sum(r['similarity'] for r in top_n) / len(top_n)
            if avg_sim >= HINT_THRESHOLD:
                return {
                    "match_type": "hint",
                    "objection_type": types[0],
                    "objection_nature": top_n[0].get('objection_nature'),
                    "confidence": round(avg_sim, 4),
                    "hint_text": f"Similar messages were previously classified as '{types[0]}' (confidence: {avg_sim:.0%})",
                }
            return None

        avg_sim = sum(r['similarity'] for r in top_n) / len(top_n)

        if avg_sim >= DIRECT_USE_THRESHOLD:
            logger.info(
                f"MEMORY DIRECT: '{message_text[:60]}' → {types[0]} "
                f"(similarity: {avg_sim:.4f}, {len(real_locations)} tenants)"
            )
            return {
                "match_type": "direct",
                "objection_type": types[0],
                "objection_nature": top_n[0].get('objection_nature'),
                "confidence": round(avg_sim, 4),
                "hint_text": None,
            }
        elif avg_sim >= HINT_THRESHOLD:
            return {
                "match_type": "hint",
                "objection_type": types[0],
                "objection_nature": top_n[0].get('objection_nature'),
                "confidence": round(avg_sim, 4),
                "hint_text": f"Similar messages were previously classified as '{types[0]}' (confidence: {avg_sim:.0%})",
            }

        return None

    except Exception as e:
        logger.error(f"Classification memory lookup failed: {e}")
        return None
    finally:
        return_db_connection(conn)


def _fallback_similarity_search(cur, query_embedding):
    """
    When pgvector isn't available, fetch recent high-confidence entries
    and compute cosine similarity in Python. Limited to last 5000 entries
    for performance.
    """
    try:
        cur.execute("""
            SELECT objection_type, objection_nature, confidence, location_id, embedding
            FROM learned_classifications
            WHERE embedding IS NOT NULL
              AND confidence >= %s
            ORDER BY created_at DESC
            LIMIT 5000
        """, (MIN_CONFIDENCE_FOR_DIRECT,))
        rows = cur.fetchall()
    except Exception:
        return []

    if not rows:
        return []

    scored = []
    for r in rows:
        stored_emb = r['embedding'] if isinstance(r['embedding'], list) else json.loads(r['embedding'])
        sim = _cosine_similarity(query_embedding, stored_emb)
        scored.append({
            'objection_type': r['objection_type'],
            'objection_nature': r['objection_nature'],
            'confidence': r['confidence'],
            'location_id': r['location_id'],
            'similarity': sim,
        })

    scored.sort(key=lambda x: x['similarity'], reverse=True)
    return scored[:CONSENSUS_REQUIRED + 2]


# ═══════════════════════════════════════════════════════════════════════════════
# STORE — Save classification with embedding for future lookups
# ═══════════════════════════════════════════════════════════════════════════════

def store_classification(
    message_text: str,
    objection_type: str,
    objection_nature: str = "none",
    stage: str = None,
    confidence: float = 0.7,
    location_id: str = None,
    source: str = "llm",
    keyword_validated: bool = False,
):
    """
    Store a classified message with its embedding for future similarity lookups.

    Confidence levels:
        1.0  — keyword seed data (ground truth)
        0.95 — keyword-confirmed LLM result
        0.9  — multi-LLM-confirmed (2+ independent LLM calls agree)
        0.7  — single LLM classification (provisional, not used for direct match)

    Self-correction rules:
        - If keywords validate the LLM result → store at 0.95
        - If keywords DISAGREE → store the KEYWORD result, not the LLM result
        - If an existing entry at 0.7 gets confirmed → promote to 0.9
        - If an existing entry gets contradicted → delete it
    """
    if not _ensure_table():
        return

    # Boost confidence if keyword-validated
    if keyword_validated and confidence < 0.95:
        confidence = 0.95

    msg_hash = _message_hash(message_text)

    conn = get_db_connection()
    if not conn:
        return

    try:
        cur = conn.cursor()

        # Check for existing entry with same hash
        cur.execute("""
            SELECT id, objection_type, confidence, confirmation_count
            FROM learned_classifications
            WHERE message_hash = %s
            LIMIT 1
        """, (msg_hash,))
        existing = cur.fetchone()

        if existing:
            if existing['objection_type'] == objection_type:
                # CONFIRMATION — same classification seen again → promote confidence
                new_count = (existing['confirmation_count'] or 0) + 1
                new_confidence = min(0.95, max(existing['confidence'], 0.9 if new_count >= 2 else 0.8))
                cur.execute("""
                    UPDATE learned_classifications
                    SET confirmation_count = %s, confidence = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (new_count, new_confidence, existing['id']))
                conn.commit()
                return
            else:
                # CONTRADICTION — different classification for same message
                if confidence > existing['confidence']:
                    # New classification is higher confidence → replace
                    logger.info(
                        f"MEMORY CORRECTION: '{message_text[:60]}' was {existing['objection_type']} "
                        f"(conf {existing['confidence']:.2f}), now {objection_type} (conf {confidence:.2f})"
                    )
                    cur.execute("DELETE FROM learned_classifications WHERE id = %s", (existing['id'],))
                    # Fall through to insert new
                elif existing['confidence'] <= 0.7:
                    # Existing was provisional and now contradicted → delete
                    logger.info(
                        f"MEMORY DELETE: provisional '{message_text[:60]}' was {existing['objection_type']} "
                        f"but new classification says {objection_type} — removing bad data"
                    )
                    cur.execute("DELETE FROM learned_classifications WHERE id = %s", (existing['id'],))
                    conn.commit()
                    return  # Don't store the new one either — let it be reclassified next time
                else:
                    # Existing has higher confidence — keep it, discard new
                    conn.commit()
                    return

        # Generate embedding (async-safe: this is a quick API call)
        embedding = _get_embedding(message_text)

        # Insert new classification
        cur.execute("""
            INSERT INTO learned_classifications
                (message_hash, message_text, embedding, objection_type, objection_nature,
                 stage, confidence, location_id, source, confirmation_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            msg_hash,
            message_text[:500],  # Truncate long messages
            json.dumps(embedding) if embedding else None,
            objection_type,
            objection_nature,
            stage,
            confidence,
            location_id or '__unknown__',
            source,
            0,
        ))

        # Also write to pgvector column if available
        if embedding:
            try:
                cur.execute("""
                    UPDATE learned_classifications
                    SET embedding_vec = %s::vector
                    WHERE message_hash = %s AND embedding_vec IS NULL
                """, (str(embedding), msg_hash))
            except Exception:
                pass  # pgvector not available

        conn.commit()

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to store classification: {e}")
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# SELF-CORRECTION — Handle contradictions and confirmations
# ═══════════════════════════════════════════════════════════════════════════════

def handle_contradiction(message_text: str, new_type: str, new_confidence: float):
    """
    Called when LLM classification disagrees with an embedding lookup result.
    The LLM has full conversational context, so it wins — but we update the memory.
    """
    msg_hash = _message_hash(message_text)

    conn = get_db_connection()
    if not conn:
        return

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, objection_type, confidence FROM learned_classifications
            WHERE message_hash = %s
            LIMIT 1
        """, (msg_hash,))
        existing = cur.fetchone()

        if not existing:
            return

        if existing['confidence'] <= 0.7:
            # Provisional entry contradicted → delete
            cur.execute("DELETE FROM learned_classifications WHERE id = %s", (existing['id'],))
            logger.info(f"MEMORY SELF-CORRECT: deleted provisional '{message_text[:60]}' ({existing['objection_type']})")
        elif new_confidence >= existing['confidence']:
            # Higher confidence new result → replace
            cur.execute("""
                UPDATE learned_classifications
                SET objection_type = %s, confidence = %s, confirmation_count = 0,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (new_type, new_confidence, existing['id']))
            logger.info(
                f"MEMORY SELF-CORRECT: updated '{message_text[:60]}' "
                f"from {existing['objection_type']} to {new_type}"
            )

        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Contradiction handling failed: {e}")
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# SEEDING — Initialize from keyword phrases
# ═══════════════════════════════════════════════════════════════════════════════

_seed_done = False


def seed_from_keywords():
    """
    Seed the classification memory with keyword phrases from conversation_engine.py.
    These are ground truth at confidence 1.0.

    Run once on first use. Subsequent calls are no-ops.
    This is idempotent — existing entries won't be duplicated (message_hash dedup).
    """
    global _seed_done
    if _seed_done:
        return

    if not _embeddings_available:
        # Try one test embedding to see if API is available
        test = _get_embedding("test")
        if not test:
            logger.info("Embedding API not available — skipping keyword seeding")
            _seed_done = True
            return

    if not _ensure_table():
        return

    # Check if we already have seed data
    conn = get_db_connection()
    if not conn:
        return

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) as cnt FROM learned_classifications WHERE source = 'keyword_seed'
        """)
        count = cur.fetchone()['cnt']
        if count > 50:
            # Already seeded
            _seed_done = True
            return
    except Exception:
        pass
    finally:
        return_db_connection(conn)

    # Build seed phrases from conversation_engine keyword lists
    seed_phrases = _get_seed_phrases()
    logger.info(f"Seeding classification memory with {len(seed_phrases)} keyword phrases...")

    seeded = 0
    for phrase, obj_type, obj_nature in seed_phrases:
        try:
            store_classification(
                message_text=phrase,
                objection_type=obj_type,
                objection_nature=obj_nature,
                confidence=1.0,
                location_id="__seed__",
                source="keyword_seed",
                keyword_validated=True,
            )
            seeded += 1
        except Exception as e:
            logger.warning(f"Failed to seed phrase '{phrase[:40]}': {e}")
            continue

    logger.info(f"Seeded {seeded}/{len(seed_phrases)} keyword phrases into classification memory")
    _seed_done = True


def _get_seed_phrases():
    """
    Extract representative keyword phrases for seeding.
    Returns list of (phrase, objection_type, objection_nature) tuples.

    We don't seed ALL 320+ phrases (too many API calls on first boot).
    Instead, seed the most representative ~80 phrases across all types.
    """
    seeds = []

    # NOT_INTERESTED — the broadest bucket
    for phrase in [
        "not interested", "no thanks", "no thank you", "don't want",
        "not for me", "hard pass", "nah", "nope", "leave me alone",
        "i'm good", "i'm done", "over it", "don't care", "stop texting",
        "stop calling", "go away", "take me off your list",
        "not looking", "i'll pass", "count me out", "absolutely not",
    ]:
        seeds.append((phrase, "not_interested", "logistical"))

    # SPOUSE / PARTNER
    for phrase in [
        "talk to my wife", "ask my husband", "check with my partner",
        "need to discuss with my wife", "my wife handles that",
        "let me talk to my family", "ask my accountant",
        "need to check with my financial advisor", "run it by my spouse",
        "need approval", "get their opinion",
    ]:
        seeds.append((phrase, "spouse_partner", "logistical"))

    # PRICE / MONEY
    for phrase in [
        "too expensive", "can't afford it", "not in my budget",
        "on a fixed income", "money is tight", "waste of money",
        "costs too much", "more than i expected", "anything cheaper",
        "not worth it", "can't justify the cost",
    ]:
        seeds.append((phrase, "price_money", "logistical"))

    # ALREADY COVERED
    for phrase in [
        "already have insurance", "i'm covered", "have a policy",
        "through my employer", "already have an agent", "all set",
        "just renewed my policy", "happy with what i have",
        "went with another company", "already working with someone",
    ]:
        seeds.append((phrase, "already_covered", "logistical"))

    # THINK ABOUT IT
    for phrase in [
        "let me think about it", "need some time", "sleep on it",
        "not ready yet", "get back to you", "send me an email",
        "let me do some research", "maybe down the road",
        "big decision", "let me shop around", "we'll see",
    ]:
        seeds.append((phrase, "think_about_it", "logistical"))

    # BUSY / TIMING
    for phrase in [
        "busy right now", "in a meeting", "at work", "driving",
        "call back later", "not a good time", "can't talk right now",
        "heading out", "maybe tomorrow", "try me later",
    ]:
        seeds.append((phrase, "busy_timing", "logistical"))

    # HEALTH CONCERN
    for phrase in [
        "i have diabetes", "high blood pressure", "heart condition",
        "too old for insurance", "pre-existing condition",
        "will i even qualify", "health issues", "on medication",
    ]:
        seeds.append((phrase, "health_concern", "fear_based"))

    # TRUST ISSUE
    for phrase in [
        "insurance is a scam", "don't trust insurance companies",
        "got burned before", "bad experience with insurance",
        "my nephew sells insurance", "waste of money just like last time",
    ]:
        seeds.append((phrase, "trust_issue", "fear_based"))

    # NON-OBJECTIONS (critical — prevents false positives)
    for phrase in [
        "yes", "sure", "sounds good", "tell me more", "how much",
        "what coverage do you recommend", "i'm interested",
        "let's do it", "when can we meet", "book me in",
        "that works for me", "what's the next step",
    ]:
        seeds.append((phrase, "none", "none"))

    return seeds


# ═══════════════════════════════════════════════════════════════════════════════
# STATS — How well is the memory performing?
# ═══════════════════════════════════════════════════════════════════════════════

def get_memory_stats():
    """Return stats about the classification memory for monitoring."""
    conn = get_db_connection()
    if not conn:
        return {}

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) as total_entries,
                COUNT(*) FILTER (WHERE source = 'keyword_seed') as seed_entries,
                COUNT(*) FILTER (WHERE source = 'llm') as llm_entries,
                COUNT(*) FILTER (WHERE confidence >= 0.85) as high_confidence,
                COUNT(*) FILTER (WHERE confidence < 0.85) as provisional,
                COUNT(DISTINCT location_id) FILTER (WHERE location_id != '__seed__') as tenant_count,
                COUNT(DISTINCT objection_type) as type_count
            FROM learned_classifications
        """)
        row = cur.fetchone()
        return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Failed to get memory stats: {e}")
        return {}
    finally:
        return_db_connection(conn)
