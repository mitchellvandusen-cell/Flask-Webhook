# lead_intelligence.py - AI-Powered Intelligence Layer
# Phase 5: AI summaries, next-best-actions, and lead temperature via xAI Grok.
#
# One micro-prompt per contact → cached in contact_intelligence table.
# Only regenerated when new messages arrive after the last analysis.
# Cost: ~$0.001-0.003 per analysis (grok-4-1-fast-non-reasoning, ~200 tokens out).

import json
import logging
import os
import re
from datetime import datetime, timedelta
from openai import OpenAI
from db import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)

XAI_API_KEY = os.getenv("XAI_API_KEY")
INTELLIGENCE_MODEL = "grok-4-1-fast-non-reasoning"

_client = None
if XAI_API_KEY:
    _client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ CONTEXT BUILDER — Gathers all data for the AI prompt ═══════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _gather_contact_context(location_id, contact_id):
    """
    Pull all available context for a contact from DB.
    Returns a dict with conversation, facts, pipeline, calls, tags.
    Also returns the timestamp of the most recent message (for cache freshness).
    """
    ctx = {
        "messages": [],
        "facts": [],
        "pipeline": None,
        "calls": {"total": 0, "connected": 0, "long_calls": 0, "last_call": None},
        "tags": [],
        "narrative": None,
        "last_message_at": None,
    }

    conn = get_db_connection()
    if not conn:
        return ctx

    try:
        cur = conn.cursor()

        # Recent messages (last 30 for context)
        try:
            cur.execute("""
                SELECT message_type, message_text, created_at
                FROM contact_messages
                WHERE contact_id = %s
                ORDER BY created_at DESC
                LIMIT 30
            """, (contact_id,))
            rows = cur.fetchall()
            if rows:
                ctx["last_message_at"] = rows[0].get('created_at')
                for r in reversed(rows):
                    role = "Lead" if r['message_type'] == 'lead' else "Bot"
                    ctx["messages"].append(f"{role}: {r['message_text']}")
        except Exception:
            pass

        # Known facts
        try:
            cur.execute("""
                SELECT fact_text FROM contact_facts
                WHERE contact_id = %s
                ORDER BY created_at ASC
            """, (contact_id,))
            ctx["facts"] = [r['fact_text'] for r in cur.fetchall()]
        except Exception:
            pass

        # Pipeline / opportunity
        try:
            cur.execute("""
                SELECT pipeline_name, stage_name, status, monetary_value
                FROM ghl_opportunities
                WHERE location_id = %s AND contact_id = %s
                ORDER BY updated_at_ghl DESC NULLS LAST
                LIMIT 1
            """, (location_id, contact_id))
            opp = cur.fetchone()
            if opp:
                ctx["pipeline"] = dict(opp)
        except Exception:
            pass

        # Call history stats
        try:
            cur.execute("""
                SELECT COUNT(*) as total,
                       COUNT(*) FILTER (WHERE status = 'completed' AND duration > 120) as long_calls,
                       COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) as connected,
                       MAX(created_at) as last_call
                FROM call_history
                WHERE location_id = %s AND contact_id = %s
            """, (location_id, contact_id))
            calls = cur.fetchone()
            if calls:
                ctx["calls"] = {
                    "total": calls.get('total', 0) or 0,
                    "connected": calls.get('connected', 0) or 0,
                    "long_calls": calls.get('long_calls', 0) or 0,
                    "last_call": str(calls['last_call']) if calls.get('last_call') else None,
                }
        except Exception:
            pass

        # Tags
        try:
            cur.execute("""
                SELECT tags FROM contact_cache
                WHERE location_id = %s AND contact_id = %s
            """, (location_id, contact_id))
            tag_row = cur.fetchone()
            if tag_row and tag_row.get('tags'):
                tags = tag_row['tags']
                if isinstance(tags, str):
                    tags = json.loads(tags)
                ctx["tags"] = [
                    t if isinstance(t, str) else (t.get('name', '') if isinstance(t, dict) else '')
                    for t in tags
                ]
        except Exception:
            pass

        # Existing narrative from memory.py
        try:
            cur.execute("""
                SELECT story_narrative FROM contact_narratives
                WHERE contact_id = %s
            """, (contact_id,))
            row = cur.fetchone()
            if row:
                ctx["narrative"] = row.get('story_narrative') or row.get('narrative')
        except Exception:
            pass

        cur.close()

    except Exception as e:
        logger.error(f"Failed to gather contact context: {e}")
    finally:
        return_db_connection(conn)

    return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ AI MICRO-PROMPT — Single call generates everything ═════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _run_ai_analysis(location_id, contact_id, ctx):
    """
    Send a single micro-prompt to xAI Grok with all contact context.
    Returns parsed JSON with summary, temperature, score, and actions.
    Cost: ~$0.001-0.003 per call.
    """
    if not _client:
        logger.warning("No XAI_API_KEY — AI intelligence unavailable")
        return None

    # Build the context block
    convo = "\n".join(ctx["messages"][-20:]) if ctx["messages"] else "No conversation yet."
    facts = ", ".join(ctx["facts"]) if ctx["facts"] else "None known."
    tags = ", ".join(ctx["tags"]) if ctx["tags"] else "None."

    pipeline_str = "No pipeline data."
    if ctx["pipeline"]:
        p = ctx["pipeline"]
        pipeline_str = f"Pipeline: {p.get('pipeline_name', '?')} | Stage: {p.get('stage_name', '?')} | Status: {p.get('status', '?')}"
        if p.get('monetary_value'):
            pipeline_str += f" | Value: ${p['monetary_value']:,.0f}"

    calls_str = f"{ctx['calls']['total']} calls total, {ctx['calls']['connected']} connected, {ctx['calls']['long_calls']} calls >2min"
    if ctx['calls']['last_call']:
        calls_str += f", last call: {ctx['calls']['last_call']}"

    narrative_str = ctx.get("narrative") or "No prior narrative."

    prompt = f"""You are an AI sales coach analyzing an insurance lead for an agent. Read all the data below and produce a JSON intelligence report.

CONVERSATION HISTORY:
{convo}

KNOWN FACTS ABOUT LEAD: {facts}
TAGS: {tags}
{pipeline_str}
CALL HISTORY: {calls_str}
PRIOR NARRATIVE: {narrative_str}
CURRENT DATE: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}

Respond with ONLY valid JSON (no markdown, no code fences, no explanation). Use this exact structure:
{{
  "summary": "2-sentence situational snapshot. What does the agent need to know RIGHT NOW about this lead? Be specific — reference actual conversation details.",
  "temperature": "hot | warm | cool | cold",
  "temperature_reason": "One sentence explaining why this temperature rating.",
  "score": 0-100,
  "actions": [
    {{
      "action": "Short imperative action (e.g. 'Call back about the term quote')",
      "reason": "Why this action matters right now",
      "priority": "high | medium | low",
      "icon": "fa-solid fa-<appropriate-icon>"
    }}
  ]
}}

Rules:
- "summary" must be specific to THIS lead. Reference names, products, objections from the conversation. No generic advice.
- "temperature": hot = actively buying/quoting, warm = engaged and responding, cool = went quiet or slow replies, cold = no engagement or ghosting.
- "score": 0-100 based on likelihood to convert. Consider engagement, pipeline stage, recency, call history.
- "actions": 2-4 specific next steps. Use icons: fa-phone for calls, fa-paper-plane for SMS, fa-file-invoice-dollar for quotes, fa-calendar for appointments, fa-fire for urgency, fa-clock for timing, fa-reply for responding, fa-bolt for quick action.
- If no conversation exists, focus actions on initial outreach.
- Be direct and actionable. No fluff."""

    try:
        response = _client.chat.completions.create(
            model=INTELLIGENCE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=400,
            timeout=12.0,
        )
        raw = (response.choices[0].message.content or "").strip()

        # Strip markdown code fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)

        result = json.loads(raw)

        # Validate required fields
        if not isinstance(result.get('actions'), list):
            result['actions'] = []
        if result.get('temperature') not in ('hot', 'warm', 'cool', 'cold'):
            result['temperature'] = 'warm'
        if not isinstance(result.get('score'), (int, float)):
            result['score'] = 50
        result['score'] = max(0, min(100, int(result['score'])))

        return result

    except json.JSONDecodeError as e:
        logger.error(f"AI intelligence JSON parse failed for {contact_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"AI intelligence call failed for {contact_id}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ CACHE LAYER — Store + retrieve AI analysis results ═════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_intelligence_table():
    """Create the contact_intelligence cache table if it doesn't exist."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contact_intelligence (
                contact_id TEXT PRIMARY KEY,
                location_id TEXT,
                analysis JSONB NOT NULL,
                analyzed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                context_hash TEXT
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ci_location ON contact_intelligence (location_id);")
        conn.commit()
        cur.close()
    except Exception as e:
        logger.debug(f"Intelligence table check: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


def _get_cached_analysis(contact_id, last_message_at):
    """
    Return cached AI analysis if still fresh.
    Cache is invalidated when new messages arrive after the analysis was generated.
    """
    conn = get_db_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT analysis, analyzed_at FROM contact_intelligence
            WHERE contact_id = %s
        """, (contact_id,))
        row = cur.fetchone()
        cur.close()

        if not row:
            return None

        analyzed_at = row.get('analyzed_at')
        if not analyzed_at:
            return None

        # Normalize timestamps for comparison
        if isinstance(analyzed_at, str):
            analyzed_at = datetime.fromisoformat(analyzed_at.replace('Z', '+00:00'))
        if hasattr(analyzed_at, 'tzinfo') and analyzed_at.tzinfo:
            analyzed_at = analyzed_at.replace(tzinfo=None)

        if last_message_at:
            lm = last_message_at
            if isinstance(lm, str):
                lm = datetime.fromisoformat(lm.replace('Z', '+00:00'))
            if hasattr(lm, 'tzinfo') and lm.tzinfo:
                lm = lm.replace(tzinfo=None)

            # If new messages arrived after analysis, cache is stale
            if lm > analyzed_at:
                return None

        # Also expire after 6 hours regardless
        if isinstance(analyzed_at, datetime):
            if datetime.utcnow() - analyzed_at > timedelta(hours=6):
                return None

        analysis = row.get('analysis')
        if isinstance(analysis, str):
            analysis = json.loads(analysis)
        return analysis

    except Exception as e:
        logger.debug(f"Cache read failed: {e}")
        return None
    finally:
        return_db_connection(conn)


def _save_analysis_cache(contact_id, location_id, analysis):
    """Save AI analysis results to cache."""
    conn = get_db_connection()
    if not conn:
        return

    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO contact_intelligence (contact_id, location_id, analysis, analyzed_at)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (contact_id)
            DO UPDATE SET analysis = EXCLUDED.analysis,
                          location_id = EXCLUDED.location_id,
                          analyzed_at = CURRENT_TIMESTAMP
        """, (contact_id, location_id, json.dumps(analysis)))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.error(f"Cache write failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ PUBLIC API — Called by /api/contact/<id>/intelligence ═══════════════════
# ═══════════════════════════════════════════════════════════════════════════════

# Ensure table exists on module load
_ensure_intelligence_table()


def get_contact_intelligence(location_id, contact_id):
    """
    Get AI-powered intelligence dossier for a contact.

    Flow:
    1. Gather all context (messages, facts, pipeline, calls, tags)
    2. Check cache — if analysis exists and no new messages since, return cached
    3. If cache miss, fire one AI micro-prompt to Grok
    4. Cache the result, return it

    Returns dict with: summary, score, temperature, temperature_reason, actions,
                       facts, pipeline, narrative.
    """
    # Gather all context from DB
    ctx = _gather_contact_context(location_id, contact_id)

    # Check cache first
    cached = _get_cached_analysis(contact_id, ctx.get("last_message_at"))
    if cached:
        logger.debug(f"Intelligence cache HIT for {contact_id}")
        # Merge in live DB data that doesn't need AI
        cached["facts"] = ctx["facts"]
        cached["pipeline"] = ctx["pipeline"]
        cached["narrative"] = ctx.get("narrative")
        return cached

    logger.info(f"Intelligence cache MISS for {contact_id} — running AI analysis")

    # Run AI analysis
    ai_result = _run_ai_analysis(location_id, contact_id, ctx)

    if ai_result:
        # Cache the AI-generated fields
        _save_analysis_cache(contact_id, location_id, {
            "summary": ai_result.get("summary"),
            "score": ai_result.get("score", 50),
            "temperature": ai_result.get("temperature", "warm"),
            "temperature_reason": ai_result.get("temperature_reason", ""),
            "actions": ai_result.get("actions", []),
        })

        # Build full response
        return {
            "summary": ai_result.get("summary"),
            "score": {"score": ai_result.get("score", 50), "label": ai_result.get("temperature", "warm")},
            "temperature": ai_result.get("temperature", "warm"),
            "temperature_reason": ai_result.get("temperature_reason", ""),
            "actions": ai_result.get("actions", []),
            "facts": ctx["facts"],
            "pipeline": ctx["pipeline"],
            "narrative": ctx.get("narrative"),
        }

    # Fallback if AI call fails — return basic data without AI analysis
    logger.warning(f"AI analysis failed for {contact_id} — returning basic data")
    return {
        "summary": ctx.get("narrative") or "No analysis available yet.",
        "score": {"score": 50, "label": "unknown"},
        "temperature": "unknown",
        "temperature_reason": "",
        "actions": [],
        "facts": ctx["facts"],
        "pipeline": ctx["pipeline"],
        "narrative": ctx.get("narrative"),
    }
