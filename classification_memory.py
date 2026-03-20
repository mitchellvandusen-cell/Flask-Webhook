# classification_memory.py — TF-IDF classification memory
# Learns from every conversation to make objection/stage detection near-perfect over time.
#
# Architecture:
#   1. Seed: 320+ keyword phrases from conversation_engine.py as ground truth (confidence 1.0)
#   2. Learn: Every LLM classification is stored (text + metadata)
#   3. Lookup: TF-IDF similarity search for near-matches BEFORE calling the LLM
#   4. Self-correct: Keyword cross-validation prevents bad data from entering
#                    Contradictions are deleted, confirmations are promoted
#
# Embedding engine: scikit-learn TF-IDF with character n-grams (3-5)
#   - Zero API cost, zero external dependency, works offline
#   - Excellent for short-phrase similarity in constrained domains
#   - Vectorizer fits on entire corpus in-memory, rebuilt when corpus changes
#   - Optional: xAI/OpenAI embeddings as upgrade if Management Keys available
#
# Three-source consensus: Keywords (ground truth) > LLM (contextual) > TF-IDF memory (learned)

import os
import json
import logging
import hashlib
import threading

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from db import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

# Thresholds
DIRECT_USE_THRESHOLD = 0.95      # >= 95% similarity + consensus -> skip LLM
HINT_THRESHOLD = 0.85            # 85-95% -> inject as hint into LLM prompt
MIN_CONFIDENCE_FOR_DIRECT = 0.85 # Only entries at this confidence can be used for direct match
CONSENSUS_REQUIRED = 3           # Top-N must agree on classification
MIN_TENANT_DIVERSITY = 2         # Matches must come from 2+ different locations

# TF-IDF vectorizer config
TFIDF_ANALYZER = "char_wb"      # Character n-grams with word boundaries
TFIDF_NGRAM_RANGE = (3, 5)      # 3-5 character n-grams — catches typos, slang, abbreviations
TFIDF_MAX_FEATURES = 50000      # Cap vocabulary size for memory
TFIDF_REBUILD_THRESHOLD = 50    # Rebuild vectorizer after this many new entries

# ═══════════════════════════════════════════════════════════════════════════════
# TF-IDF VECTORIZER STATE (in-memory, rebuilt from DB as needed)
# ═══════════════════════════════════════════════════════════════════════════════

_vectorizer = None       # Fitted TfidfVectorizer
_corpus_matrix = None    # TF-IDF matrix of all stored texts (sparse)
_corpus_meta = None      # List of dicts: [{objection_type, objection_nature, confidence, location_id}, ...]
_corpus_count = 0        # Number of entries at last vectorizer build
_entries_since_build = 0 # New entries since last vectorizer build
_vectorizer_lock = threading.Lock()
_vectorizer_ready = False


def _rebuild_vectorizer():
    """
    Fit TF-IDF vectorizer on all stored message texts from the DB.
    Called on first lookup and periodically when new entries accumulate.
    Thread-safe via lock.
    """
    global _vectorizer, _corpus_matrix, _corpus_meta, _corpus_count
    global _entries_since_build, _vectorizer_ready

    conn = get_db_connection()
    if not conn:
        return False

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT message_text, objection_type, objection_nature, confidence, location_id
            FROM learned_classifications
            WHERE confidence >= %s
            ORDER BY confidence DESC, created_at DESC
        """, (0.5,))
        rows = cur.fetchall()

        if not rows or len(rows) < 3:
            # Need at least a few entries to build meaningful TF-IDF
            return False

        texts = [r['message_text'].lower().strip() for r in rows]
        meta = [{
            'objection_type': r['objection_type'],
            'objection_nature': r['objection_nature'],
            'confidence': r['confidence'],
            'location_id': r['location_id'],
        } for r in rows]

        vectorizer = TfidfVectorizer(
            analyzer=TFIDF_ANALYZER,
            ngram_range=TFIDF_NGRAM_RANGE,
            max_features=TFIDF_MAX_FEATURES,
            lowercase=True,
            strip_accents='unicode',
            sublinear_tf=True,  # Logarithmic TF scaling — dampens common phrases
        )
        matrix = vectorizer.fit_transform(texts)

        with _vectorizer_lock:
            _vectorizer = vectorizer
            _corpus_matrix = matrix
            _corpus_meta = meta
            _corpus_count = len(rows)
            _entries_since_build = 0
            _vectorizer_ready = True

        logger.info(f"TF-IDF vectorizer rebuilt: {len(rows)} entries, {len(vectorizer.vocabulary_)} features")
        return True

    except Exception as e:
        logger.error(f"Failed to rebuild TF-IDF vectorizer: {e}")
        return False
    finally:
        return_db_connection(conn)


def _ensure_vectorizer():
    """Ensure vectorizer is ready. Rebuild if stale or not yet built."""
    global _entries_since_build
    if _vectorizer_ready and _entries_since_build < TFIDF_REBUILD_THRESHOLD:
        return True
    with _vectorizer_lock:
        # Double-check after acquiring lock
        if _vectorizer_ready and _entries_since_build < TFIDF_REBUILD_THRESHOLD:
            return True
    return _rebuild_vectorizer()


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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lc_hash ON learned_classifications (message_hash)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lc_confidence ON learned_classifications (confidence)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lc_type ON learned_classifications (objection_type)")

        conn.commit()
        _table_ready = True
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to create learned_classifications table: {e}")
        return False
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _message_hash(text: str) -> str:
    """Deterministic hash for deduplication."""
    normalized = text.lower().strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


# ═══════════════════════════════════════════════════════════════════════════════
# LOOKUP — Search for similar prior classifications
# ═══════════════════════════════════════════════════════════════════════════════

def lookup_classification(message_text: str):
    """
    Search for similar prior classifications using TF-IDF cosine similarity.

    Returns:
        dict with keys:
            - match_type: "direct" (>=95%, use it), "hint" (85-95%, suggest to LLM), or None
            - objection_type: str or None
            - objection_nature: str or None
            - confidence: float (average similarity of top matches)
            - hint_text: str (for injection into LLM prompt when match_type="hint")
        Returns None if no match found or vectorizer not ready.
    """
    if not _ensure_table():
        return None

    if not _ensure_vectorizer():
        return None

    with _vectorizer_lock:
        vectorizer = _vectorizer
        corpus_matrix = _corpus_matrix
        corpus_meta = _corpus_meta

    if vectorizer is None or corpus_matrix is None:
        return None

    try:
        # Transform query text using the fitted vectorizer
        query_vec = vectorizer.transform([message_text.lower().strip()])
        similarities = cosine_similarity(query_vec, corpus_matrix).flatten()

        # Get top-N indices sorted by similarity (descending)
        top_k = min(CONSENSUS_REQUIRED + 2, len(similarities))
        top_indices = similarities.argsort()[-top_k:][::-1]

        rows = []
        for idx in top_indices:
            sim = float(similarities[idx])
            if sim < HINT_THRESHOLD * 0.9:
                # Below even hint threshold — stop collecting
                break
            meta = corpus_meta[idx]
            if meta['confidence'] < MIN_CONFIDENCE_FOR_DIRECT:
                continue
            rows.append({
                'objection_type': meta['objection_type'],
                'objection_nature': meta['objection_nature'],
                'confidence': meta['confidence'],
                'location_id': meta['location_id'],
                'similarity': sim,
            })

        if len(rows) < CONSENSUS_REQUIRED:
            return None

        # Check consensus: top-N must agree on objection_type
        top_n = rows[:CONSENSUS_REQUIRED]
        types = [r['objection_type'] for r in top_n]
        if len(set(types)) != 1:
            return None

        # Check multi-tenant diversity
        locations = set(r['location_id'] for r in top_n if r.get('location_id'))
        real_locations = locations - {'__seed__', '__unknown__'}
        has_seed = '__seed__' in locations
        # Seed + 1 real location counts as diversity, or 2+ real locations
        if len(real_locations) < MIN_TENANT_DIVERSITY and not (has_seed and len(real_locations) >= 1):
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
                f"MEMORY DIRECT: '{message_text[:60]}' -> {types[0]} "
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


# ═══════════════════════════════════════════════════════════════════════════════
# STORE — Save classification for future lookups
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
    Store a classified message for future TF-IDF similarity lookups.

    Confidence levels:
        1.0  — keyword seed data (ground truth)
        0.95 — keyword-confirmed LLM result
        0.9  — multi-LLM-confirmed (2+ independent LLM calls agree)
        0.7  — single LLM classification (provisional, not used for direct match)

    Self-correction rules:
        - If keywords validate the LLM result -> store at 0.95
        - If keywords DISAGREE -> store the KEYWORD result, not the LLM result
        - If an existing entry at 0.7 gets confirmed -> promote to 0.9
        - If an existing entry gets contradicted -> delete it
    """
    global _entries_since_build

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
                # CONFIRMATION — same classification seen again -> promote confidence
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
                    logger.info(
                        f"MEMORY CORRECTION: '{message_text[:60]}' was {existing['objection_type']} "
                        f"(conf {existing['confidence']:.2f}), now {objection_type} (conf {confidence:.2f})"
                    )
                    cur.execute("DELETE FROM learned_classifications WHERE id = %s", (existing['id'],))
                    # Fall through to insert new
                elif existing['confidence'] <= 0.7:
                    logger.info(
                        f"MEMORY DELETE: provisional '{message_text[:60]}' was {existing['objection_type']} "
                        f"but new classification says {objection_type} — removing bad data"
                    )
                    cur.execute("DELETE FROM learned_classifications WHERE id = %s", (existing['id'],))
                    conn.commit()
                    _entries_since_build += 1  # Corpus changed
                    return  # Don't store the new one either — let it be reclassified next time
                else:
                    # Existing has higher confidence — keep it, discard new
                    conn.commit()
                    return

        # Insert new classification (no embedding needed — TF-IDF works from text)
        cur.execute("""
            INSERT INTO learned_classifications
                (message_hash, message_text, objection_type, objection_nature,
                 stage, confidence, location_id, source, confirmation_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            msg_hash,
            message_text[:500],  # Truncate long messages
            objection_type,
            objection_nature,
            stage,
            confidence,
            location_id or '__unknown__',
            source,
            0,
        ))

        conn.commit()
        _entries_since_build += 1

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
    Called when LLM classification disagrees with a memory lookup result.
    The LLM has full conversational context, so it wins — but we update the memory.
    """
    global _entries_since_build
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
            cur.execute("DELETE FROM learned_classifications WHERE id = %s", (existing['id'],))
            logger.info(f"MEMORY SELF-CORRECT: deleted provisional '{message_text[:60]}' ({existing['objection_type']})")
            _entries_since_build += 1
        elif new_confidence >= existing['confidence']:
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
            _entries_since_build += 1

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
    No API calls — TF-IDF works from raw text stored in the DB.
    """
    global _seed_done
    if _seed_done:
        return

    if not _ensure_table():
        return

    # Check if we already have seed data
    conn = get_db_connection()
    if not conn:
        return

    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as cnt FROM learned_classifications WHERE source = 'keyword_seed'")
        count = cur.fetchone()['cnt']
        if count > 50:
            _seed_done = True
            return
    except Exception:
        pass
    finally:
        return_db_connection(conn)

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

    # Force vectorizer rebuild now that corpus has seed data
    _rebuild_vectorizer()


def _get_seed_phrases():
    """
    Extract representative keyword phrases for seeding.
    Returns list of (phrase, objection_type, objection_nature) tuples.

    With TF-IDF we can seed ALL phrases — no API calls, zero cost.
    More seed data = better TF-IDF vocabulary from day one.
    """
    seeds = []

    # NOT_INTERESTED — the broadest bucket
    for phrase in [
        "not interested", "no thanks", "no thank you", "don't want",
        "not for me", "hard pass", "nah", "nope", "leave me alone",
        "i'm good", "i'm done", "over it", "don't care", "stop texting",
        "stop calling", "go away", "take me off your list",
        "not looking", "i'll pass", "count me out", "absolutely not",
        "whatever", "kick rocks", "hell no", "pass", "not gonna happen",
        "why would i", "lol no", "never", "don't bother", "waste of time",
        "not now", "i said no", "buzz off",
    ]:
        seeds.append((phrase, "not_interested", "logistical"))

    # SPOUSE / PARTNER
    for phrase in [
        "talk to my wife", "ask my husband", "check with my partner",
        "need to discuss with my wife", "my wife handles that",
        "let me talk to my family", "ask my accountant",
        "need to check with my financial advisor", "run it by my spouse",
        "need approval", "get their opinion", "my partner makes those decisions",
        "wife won't go for it", "husband handles the money",
        "gotta ask my better half", "need to run it by the boss at home",
    ]:
        seeds.append((phrase, "spouse_partner", "logistical"))

    # PRICE / MONEY
    for phrase in [
        "too expensive", "can't afford it", "not in my budget",
        "on a fixed income", "money is tight", "waste of money",
        "costs too much", "more than i expected", "anything cheaper",
        "not worth it", "can't justify the cost", "too much money",
        "don't have the money right now", "that's a lot",
        "social security doesn't cover that", "living paycheck to paycheck",
        "every penny counts", "can barely pay my bills",
    ]:
        seeds.append((phrase, "price_money", "logistical"))

    # ALREADY COVERED
    for phrase in [
        "already have insurance", "i'm covered", "have a policy",
        "through my employer", "already have an agent", "all set",
        "just renewed my policy", "happy with what i have",
        "went with another company", "already working with someone",
        "got coverage through work", "my job provides it",
        "state farm takes care of it", "allstate covers me",
        "just got a new policy last month", "satisfied with my coverage",
    ]:
        seeds.append((phrase, "already_covered", "logistical"))

    # THINK ABOUT IT
    for phrase in [
        "let me think about it", "need some time", "sleep on it",
        "not ready yet", "get back to you", "send me an email",
        "let me do some research", "maybe down the road",
        "big decision", "let me shop around", "we'll see",
        "i'll let you know", "give me a few days", "not rushing into anything",
        "need to weigh my options", "gotta think it over",
        "let me pray on it", "need to crunch the numbers",
    ]:
        seeds.append((phrase, "think_about_it", "logistical"))

    # BUSY / TIMING
    for phrase in [
        "busy right now", "in a meeting", "at work", "driving",
        "call back later", "not a good time", "can't talk right now",
        "heading out", "maybe tomorrow", "try me later",
        "swamped today", "hit me up next week", "in the middle of something",
        "picking up the kids", "at the doctor", "on my way somewhere",
        "crazy week", "slammed at work",
    ]:
        seeds.append((phrase, "busy_timing", "logistical"))

    # HEALTH CONCERN
    for phrase in [
        "i have diabetes", "high blood pressure", "heart condition",
        "too old for insurance", "pre-existing condition",
        "will i even qualify", "health issues", "on medication",
        "cancer survivor", "had a stroke", "copd", "asthma",
        "they always deny me", "no one will insure me",
    ]:
        seeds.append((phrase, "health_concern", "fear_based"))

    # TRUST ISSUE
    for phrase in [
        "insurance is a scam", "don't trust insurance companies",
        "got burned before", "bad experience with insurance",
        "my nephew sells insurance", "waste of money just like last time",
        "they never pay out", "just trying to take my money",
        "heard too many horror stories", "insurance companies are crooks",
    ]:
        seeds.append((phrase, "trust_issue", "fear_based"))

    # NON-OBJECTIONS (critical — prevents false positives)
    for phrase in [
        "yes", "sure", "sounds good", "tell me more", "how much",
        "what coverage do you recommend", "i'm interested",
        "let's do it", "when can we meet", "book me in",
        "that works for me", "what's the next step",
        "ok sounds interesting", "yeah i need coverage",
        "how does it work", "what are my options",
        "can you explain the difference", "i want to make sure my family is covered",
        "sign me up", "absolutely", "that makes sense",
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
                COUNT(DISTINCT location_id) FILTER (WHERE location_id NOT IN ('__seed__', '__unknown__')) as tenant_count,
                COUNT(DISTINCT objection_type) as type_count
            FROM learned_classifications
        """)
        row = cur.fetchone()

        stats = dict(row) if row else {}

        # Add vectorizer info
        stats['vectorizer_ready'] = _vectorizer_ready
        stats['entries_since_rebuild'] = _entries_since_build
        if _vectorizer:
            stats['vocabulary_size'] = len(_vectorizer.vocabulary_)

        return stats
    except Exception as e:
        logger.error(f"Failed to get memory stats: {e}")
        return {}
    finally:
        return_db_connection(conn)
