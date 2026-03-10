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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
        "calls": {"total": 0, "agent_called": 0, "lead_called": 0, "answered": 0, "unanswered": 0, "long_calls": 0, "last_call": None},
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

        # Call history stats — direction-aware
        try:
            cur.execute("""
                SELECT COUNT(*) as total,
                       COUNT(*) FILTER (WHERE direction = 'outbound') as agent_called,
                       COUNT(*) FILTER (WHERE direction = 'inbound') as lead_called,
                       COUNT(*) FILTER (WHERE status = 'completed' AND duration > 30) as answered,
                       COUNT(*) FILTER (WHERE status = 'completed' AND duration > 120) as long_calls,
                       COUNT(*) FILTER (WHERE status IN ('no-answer','busy','canceled','failed')
                                              OR (status = 'completed' AND duration <= 30)) as unanswered,
                       MAX(created_at) as last_call
                FROM call_history
                WHERE location_id = %s AND contact_id = %s
            """, (location_id, contact_id))
            calls = cur.fetchone()
            if calls:
                ctx["calls"] = {
                    "total": calls.get('total', 0) or 0,
                    "agent_called": calls.get('agent_called', 0) or 0,
                    "lead_called": calls.get('lead_called', 0) or 0,
                    "answered": calls.get('answered', 0) or 0,
                    "unanswered": calls.get('unanswered', 0) or 0,
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

    c = ctx['calls']
    calls_str = f"{c['total']} calls total ({c['agent_called']} outbound by agent, {c['lead_called']} inbound from lead), {c['answered']} answered (>30s), {c['unanswered']} unanswered/no-answer, {c['long_calls']} calls >2min"
    if c['last_call']:
        calls_str += f", last call: {c['last_call']}"

    narrative_str = ctx.get("narrative") or "No prior narrative."

    prompt = f"""You are an AI sales coach analyzing an insurance lead for an agent. Read ALL the data below carefully and produce a JSON intelligence report. Your classification MUST be based on what the lead ACTUALLY SAID, not just whether they responded.

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
  "summary": "2-sentence situational snapshot. What does the agent need to know RIGHT NOW about this lead? Be specific — reference actual conversation details, names, products, objections.",
  "temperature": "hot | warm | cool | cold",
  "temperature_reason": "One sentence explaining why this temperature rating based on what the lead said.",
  "score": 0-100,
  "should_respond": true or false,
  "should_respond_reason": "Why the agent should or should not respond right now.",
  "engagement_level": 0-3,
  "actions": [
    {{
      "action": "Short imperative action (e.g. 'Call back about the term quote')",
      "reason": "Why this action matters right now",
      "priority": "high | medium | low",
      "icon": "fa-solid fa-<appropriate-icon>"
    }}
  ]
}}

CRITICAL CLASSIFICATION RULES — READ THESE CAREFULLY:

"temperature" — Based on the CONTENT of what the lead said, NOT just whether they replied:
- hot = Lead is actively buying: asking for quotes, requesting coverage, comparing products, ready to book, saying "let's do it." They are IN the buying process.
- warm = Lead is engaged and positive: responding to questions, sharing information about their situation, showing genuine interest. The conversation is progressing forward.
- cool = Lead went quiet (no response to last 2+ messages), giving slow/short replies, showing declining interest, OR gave a soft objection like "let me think about it."
- cold = Lead explicitly said no: "not interested", "stop", "already covered", "don't contact me", OR has completely ghosted (no response to 3+ outreach attempts). A lead who REPLIED with "no thanks" or "I'm good" is COLD, not hot.

"score" — 0-100 likelihood to convert to a paying client. Consider:
- What the lead actually said (buying signals vs objections vs silence)
- Pipeline stage and deal value if present
- Call history: "outbound by agent" = the AGENT called the lead (NOT lead-initiated). "inbound from lead" = the LEAD called in. "answered (>30s)" = actually spoke. "unanswered" = no answer, busy, or call under 30s (likely voicemail/no pickup). Do NOT say "lead initiated a call" when the call was outbound by agent.
- Recency of engagement
- A lead who said "not interested" gets 5-15, NOT 50+

"should_respond" — true if the agent needs to take action NOW:
- true: Lead asked a question that hasn't been answered, expressed interest that wasn't followed up, or sent a message that deserves a reply.
- false: Bot already replied to the lead's last message, lead said stop/not interested, lead hasn't sent any messages, or there is genuinely nothing to respond to.
- This is about whether the lead is WAITING for a response, not just whether they're a good lead.

"engagement_level" — How deep the interaction has gone (0-3):
- 0 = No meaningful interaction yet (new lead, only outbound attempts)
- 1 = Surface contact (lead acknowledged but no real conversation — one-word replies, "who is this", etc.)
- 2 = Real conversation (lead is sharing information, asking questions, back-and-forth dialogue)
- 3 = Deep engagement (multiple exchanges, discussing specifics, answered calls >30s where they actually spoke, approaching a decision). Unanswered outbound calls do NOT count as engagement.

"actions" — 2-4 specific next steps. Use icons: fa-phone (calls), fa-paper-plane (SMS), fa-file-invoice-dollar (quotes), fa-calendar (appointments), fa-fire (urgency), fa-clock (timing), fa-reply (responding), fa-bolt (quick action).
- If no conversation exists, focus on initial outreach.
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
        # Validate new fields (graceful defaults for backward compat)
        if not isinstance(result.get('should_respond'), bool):
            result['should_respond'] = False
        if not isinstance(result.get('should_respond_reason'), str):
            result['should_respond_reason'] = ''
        if not isinstance(result.get('engagement_level'), (int, float)):
            result['engagement_level'] = 0
        result['engagement_level'] = max(0, min(3, int(result['engagement_level'])))

        return result

    except json.JSONDecodeError as e:
        logger.error(f"AI intelligence JSON parse failed for {contact_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"AI intelligence call failed for {contact_id}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ BULK AI ANALYSIS — Analyze many contacts in a single LLM call ═════════
# ═══════════════════════════════════════════════════════════════════════════════

def _gather_bulk_contexts(location_id, contact_ids):
    """
    Gather context for many contacts in batched DB queries (not 1 query per contact).
    Returns {contact_id: ctx_dict}.
    """
    if not contact_ids:
        return {}

    results = {cid: {
        "messages": [], "facts": [], "pipeline": None,
        "calls": {"total": 0, "agent_called": 0, "lead_called": 0, "answered": 0, "unanswered": 0, "long_calls": 0, "last_call": None},
        "tags": [], "narrative": None, "last_message_at": None,
    } for cid in contact_ids}

    conn = get_db_connection()
    if not conn:
        return results

    try:
        cur = conn.cursor()

        # ── Messages (last 30 per contact) ──
        try:
            cur.execute("""
                SELECT contact_id, message_type, message_text, created_at,
                       ROW_NUMBER() OVER (PARTITION BY contact_id ORDER BY created_at DESC) AS rn
                FROM contact_messages
                WHERE contact_id = ANY(%s)
            """, (contact_ids,))
            rows = cur.fetchall()
            # Group and filter to last 30 per contact
            for r in rows:
                cid = r['contact_id']
                if cid in results and r['rn'] <= 30:
                    if r['rn'] == 1:
                        results[cid]['last_message_at'] = r['created_at']
                    role = "Lead" if r['message_type'] == 'lead' else "Bot"
                    results[cid]['messages'].append((r['created_at'], f"{role}: {r['message_text']}"))
            # Sort messages chronologically
            for cid in results:
                results[cid]['messages'] = [m[1] for m in sorted(results[cid]['messages'], key=lambda x: x[0])]
        except Exception:
            pass

        # ── Facts ──
        try:
            cur.execute("""
                SELECT contact_id, fact_text FROM contact_facts
                WHERE contact_id = ANY(%s)
                ORDER BY created_at ASC
            """, (contact_ids,))
            for r in cur.fetchall():
                cid = r['contact_id']
                if cid in results:
                    results[cid]['facts'].append(r['fact_text'])
        except Exception:
            pass

        # ── Pipeline / opportunities (latest per contact) ──
        try:
            cur.execute("""
                SELECT DISTINCT ON (contact_id)
                       contact_id, pipeline_name, stage_name, status, monetary_value
                FROM ghl_opportunities
                WHERE location_id = %s AND contact_id = ANY(%s)
                ORDER BY contact_id, updated_at_ghl DESC NULLS LAST
            """, (location_id, contact_ids))
            for r in cur.fetchall():
                cid = r['contact_id']
                if cid in results:
                    results[cid]['pipeline'] = dict(r)
        except Exception:
            pass

        # ── Call history stats — direction-aware ──
        try:
            cur.execute("""
                SELECT contact_id,
                       COUNT(*) as total,
                       COUNT(*) FILTER (WHERE direction = 'outbound') as agent_called,
                       COUNT(*) FILTER (WHERE direction = 'inbound') as lead_called,
                       COUNT(*) FILTER (WHERE status = 'completed' AND duration > 30) as answered,
                       COUNT(*) FILTER (WHERE status = 'completed' AND duration > 120) as long_calls,
                       COUNT(*) FILTER (WHERE status IN ('no-answer','busy','canceled','failed')
                                              OR (status = 'completed' AND duration <= 30)) as unanswered,
                       MAX(created_at) as last_call
                FROM call_history
                WHERE location_id = %s AND contact_id = ANY(%s)
                GROUP BY contact_id
            """, (location_id, contact_ids))
            for r in cur.fetchall():
                cid = r['contact_id']
                if cid in results:
                    results[cid]['calls'] = {
                        "total": r.get('total', 0) or 0,
                        "agent_called": r.get('agent_called', 0) or 0,
                        "lead_called": r.get('lead_called', 0) or 0,
                        "answered": r.get('answered', 0) or 0,
                        "unanswered": r.get('unanswered', 0) or 0,
                        "long_calls": r.get('long_calls', 0) or 0,
                        "last_call": str(r['last_call']) if r.get('last_call') else None,
                    }
        except Exception:
            pass

        # ── Tags ──
        try:
            cur.execute("""
                SELECT contact_id, tags FROM contact_cache
                WHERE location_id = %s AND contact_id = ANY(%s)
            """, (location_id, contact_ids))
            for r in cur.fetchall():
                cid = r['contact_id']
                if cid in results and r.get('tags'):
                    tags = r['tags']
                    if isinstance(tags, str):
                        tags = json.loads(tags)
                    results[cid]['tags'] = [
                        t if isinstance(t, str) else (t.get('name', '') if isinstance(t, dict) else '')
                        for t in tags
                    ]
        except Exception:
            pass

        # ── Narratives ──
        try:
            cur.execute("""
                SELECT contact_id, story_narrative FROM contact_narratives
                WHERE contact_id = ANY(%s)
            """, (contact_ids,))
            for r in cur.fetchall():
                cid = r['contact_id']
                if cid in results:
                    results[cid]['narrative'] = r.get('story_narrative')
        except Exception:
            pass

        cur.close()
    except Exception as e:
        logger.error(f"Bulk context gather failed: {e}")
    finally:
        return_db_connection(conn)

    return results


def _build_contact_block(contact_id, ctx):
    """Build a compact text block summarizing one contact's context for the bulk prompt."""
    convo = "\n".join(ctx["messages"][-15:]) if ctx["messages"] else "No conversation."
    facts = ", ".join(ctx["facts"][:8]) if ctx["facts"] else "None."
    tags = ", ".join(ctx["tags"][:6]) if ctx["tags"] else "None."

    pipeline_str = "None."
    if ctx["pipeline"]:
        p = ctx["pipeline"]
        pipeline_str = f"{p.get('pipeline_name', '?')} / {p.get('stage_name', '?')} / {p.get('status', '?')}"
        if p.get('monetary_value'):
            pipeline_str += f" ${p['monetary_value']:,.0f}"

    calls = ctx['calls']
    calls_str = f"{calls['total']}t/{calls['agent_called']}out/{calls['lead_called']}in/{calls['answered']}ans/{calls['unanswered']}unans/{calls['long_calls']}>2m"
    if calls.get('last_call'):
        calls_str += f" last:{calls['last_call'][:10]}"

    return f"""[{contact_id}]
CONVO:
{convo}
FACTS: {facts}
TAGS: {tags}
PIPELINE: {pipeline_str}
CALLS: {calls_str}"""


def _run_bulk_ai_analysis(location_id, contact_blocks):
    """
    Analyze multiple contacts in a single LLM call.
    contact_blocks: list of (contact_id, text_block) tuples.
    Returns: {contact_id: analysis_dict} for successfully parsed contacts.
    """
    if not _client or not contact_blocks:
        return {}

    contacts_text = "\n\n---\n\n".join(block for _, block in contact_blocks)
    id_list = ", ".join(cid for cid, _ in contact_blocks)

    prompt = f"""You are an AI sales coach bulk-analyzing insurance leads. Below are {len(contact_blocks)} contacts separated by "---". For EACH contact, produce a JSON analysis object.

{contacts_text}

---

CURRENT DATE: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}

Respond with ONLY a valid JSON array (no markdown, no code fences). Each element MUST include the contact's ID from the [brackets] above. Use this exact structure:

[
  {{
    "contact_id": "<id from brackets>",
    "summary": "2-sentence snapshot. Be specific — reference actual conversation details.",
    "temperature": "hot | warm | cool | cold",
    "temperature_reason": "One sentence why.",
    "score": 0-100,
    "should_respond": true or false,
    "should_respond_reason": "Why the agent should or should not respond now.",
    "engagement_level": 0-3,
    "actions": [
      {{
        "action": "Short imperative action",
        "reason": "Why this matters",
        "priority": "high | medium | low",
        "icon": "fa-solid fa-<icon>"
      }}
    ]
  }}
]

CLASSIFICATION RULES:
- temperature: hot=actively buying/quoting/ready. warm=engaged/sharing info/positive. cool=went quiet/short replies/soft objections. cold=said no/stop/not interested/ghosted 3+ attempts. "no thanks"=COLD not hot.
- score: 0-100 conversion likelihood. "not interested"=5-15, NOT 50+.
- should_respond: true only if the lead is WAITING for a reply (unanswered question, unaddressed interest). false if bot already replied, lead said stop, or nothing to respond to.
- engagement_level: 0=no contact, 1=surface (one-word replies), 2=real conversation, 3=deep (specifics, calls, near decision).
- actions: 2-4 per contact. Icons: fa-phone, fa-paper-plane, fa-file-invoice-dollar, fa-calendar, fa-fire, fa-clock, fa-reply, fa-bolt.

You MUST return exactly {len(contact_blocks)} objects, one per contact. Contact IDs: {id_list}"""

    # Scale max tokens: ~150 tokens per contact (compact output) + buffer
    max_tokens = min(32000, len(contact_blocks) * 200 + 200)
    # Scale timeout: ~0.8s per contact + base
    timeout = min(180.0, len(contact_blocks) * 1.2 + 15)

    try:
        response = _client.chat.completions.create(
            model=INTELLIGENCE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        raw = (response.choices[0].message.content or "").strip()

        # Strip markdown code fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)

        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            logger.error("Bulk AI response was not a JSON array")
            return {}

        results = {}
        for item in parsed:
            cid = item.get("contact_id", "")
            if not cid:
                continue

            # Validate fields (same as single analysis)
            if not isinstance(item.get('actions'), list):
                item['actions'] = []
            if item.get('temperature') not in ('hot', 'warm', 'cool', 'cold'):
                item['temperature'] = 'warm'
            if not isinstance(item.get('score'), (int, float)):
                item['score'] = 50
            item['score'] = max(0, min(100, int(item['score'])))
            if not isinstance(item.get('should_respond'), bool):
                item['should_respond'] = False
            if not isinstance(item.get('should_respond_reason'), str):
                item['should_respond_reason'] = ''
            if not isinstance(item.get('engagement_level'), (int, float)):
                item['engagement_level'] = 0
            item['engagement_level'] = max(0, min(3, int(item['engagement_level'])))

            results[cid] = item

        logger.info(f"Bulk AI analysis returned {len(results)}/{len(contact_blocks)} contacts")
        return results

    except json.JSONDecodeError as e:
        logger.warning(f"Bulk AI JSON parse failed, attempting repair: {e}")
        # Common LLM issues: trailing commas, unescaped quotes in strings
        try:
            fixed = raw
            # Remove trailing commas before } or ]
            fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
            # Try parsing the repaired JSON
            parsed = json.loads(fixed)
            if isinstance(parsed, list):
                results = {}
                for item in parsed:
                    cid = item.get("contact_id", "")
                    if not cid:
                        continue
                    if not isinstance(item.get('actions'), list):
                        item['actions'] = []
                    if item.get('temperature') not in ('hot', 'warm', 'cool', 'cold'):
                        item['temperature'] = 'warm'
                    if not isinstance(item.get('score'), (int, float)):
                        item['score'] = 50
                    item['score'] = max(0, min(100, int(item['score'])))
                    if not isinstance(item.get('should_respond'), bool):
                        item['should_respond'] = False
                    if not isinstance(item.get('should_respond_reason'), str):
                        item['should_respond_reason'] = ''
                    if not isinstance(item.get('engagement_level'), (int, float)):
                        item['engagement_level'] = 0
                    item['engagement_level'] = max(0, min(3, int(item['engagement_level'])))
                    results[cid] = item
                logger.info(f"Bulk AI JSON repair succeeded: {len(results)}/{len(contact_blocks)} contacts")
                return results
        except Exception:
            pass
        logger.error(f"Bulk AI JSON repair also failed, dropping batch")
        return {}
    except Exception as e:
        logger.error(f"Bulk AI call failed: {e}")
        return {}


def bulk_analyze_and_cache(location_id, contact_ids):
    """
    Analyze many contacts via bulk AI prompt and cache all results.
    Splits into sub-batches of 25 per LLM call to stay within token limits.
    Returns count of successfully analyzed contacts.
    """
    if not contact_ids or not _client:
        return 0

    # Gather context for all contacts in batched DB queries
    all_contexts = _gather_bulk_contexts(location_id, contact_ids)

    # Build compact text blocks for each contact
    contact_blocks = []
    for cid in contact_ids:
        ctx = all_contexts.get(cid)
        if ctx:
            block = _build_contact_block(cid, ctx)
            contact_blocks.append((cid, block))

    if not contact_blocks:
        return 0

    # Process in sub-batches of 25 per LLM call, run up to 10 concurrently
    # 1000 contacts = 40 sub-batches / 10 concurrent = 4 rounds * ~8s = ~32s total
    SUB_BATCH = 25
    MAX_CONCURRENT = 10
    total_analyzed = 0

    chunks = []
    for i in range(0, len(contact_blocks), SUB_BATCH):
        chunks.append(contact_blocks[i:i + SUB_BATCH])

    def _process_chunk(chunk):
        """Process a single chunk: call LLM, cache results, return count."""
        analyzed = 0
        ai_results = _run_bulk_ai_analysis(location_id, chunk)

        for cid, _ in chunk:
            result = ai_results.get(cid)
            if result:
                _save_analysis_cache(cid, location_id, {
                    "summary": result.get("summary"),
                    "score": result.get("score", 50),
                    "temperature": result.get("temperature", "warm"),
                    "temperature_reason": result.get("temperature_reason", ""),
                    "actions": result.get("actions", []),
                    "should_respond": result.get("should_respond", False),
                    "should_respond_reason": result.get("should_respond_reason", ""),
                    "engagement_level": result.get("engagement_level", 0),
                })
                analyzed += 1
            else:
                # Fallback: analyze individually if bulk missed this contact
                try:
                    ctx = all_contexts.get(cid)
                    if ctx:
                        single = _run_ai_analysis(location_id, cid, ctx)
                        if single:
                            _save_analysis_cache(cid, location_id, {
                                "summary": single.get("summary"),
                                "score": single.get("score", 50),
                                "temperature": single.get("temperature", "warm"),
                                "temperature_reason": single.get("temperature_reason", ""),
                                "actions": single.get("actions", []),
                                "should_respond": single.get("should_respond", False),
                                "should_respond_reason": single.get("should_respond_reason", ""),
                                "engagement_level": single.get("engagement_level", 0),
                            })
                            analyzed += 1
                except Exception as e:
                    logger.error(f"Fallback single analysis failed for {cid}: {e}")
        return analyzed

    # Run chunks concurrently with ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT, len(chunks))) as executor:
        futures = {executor.submit(_process_chunk, chunk): chunk for chunk in chunks}
        for future in as_completed(futures):
            try:
                total_analyzed += future.result()
            except Exception as e:
                logger.error(f"Chunk analysis failed: {e}")

    logger.info(f"Bulk analysis complete: {total_analyzed}/{len(contact_ids)} contacts for {location_id}")
    return total_analyzed


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

        # No time-based expiry — cache persists until new messages arrive.
        # This matches CLAUDE.md: "no time-based expiry."

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
# ═══ BULK API — Fetch cached AI classifications for Smart Filters ════════════
# ═══════════════════════════════════════════════════════════════════════════════

def get_bulk_cached_intelligence(location_id, contact_ids):
    """
    Fetch cached AI intelligence for multiple contacts in one DB query.
    Returns only contacts with fresh cache (< 6 hours old AND no new messages
    since analysis). This is the zero-cost path used by Smart Filters on every
    dialer load — no AI calls, just a single SQL query.

    Returns: {contact_id: {temperature, score, summary, temperature_reason}}
    """
    if not contact_ids:
        return {}

    conn = get_db_connection()
    if not conn:
        return {}

    try:
        cur = conn.cursor()
        # LEFT JOIN is faster than correlated subquery at scale (300+ contacts).
        # No time-based expiry — cache persists until new messages invalidate it.
        cur.execute("""
            SELECT ci.contact_id, ci.analysis, ci.analyzed_at, lm.last_msg_at
            FROM contact_intelligence ci
            LEFT JOIN LATERAL (
                SELECT MAX(cm.created_at) AS last_msg_at
                FROM contact_messages cm
                WHERE cm.contact_id = ci.contact_id
            ) lm ON TRUE
            WHERE ci.location_id = %s
              AND ci.contact_id = ANY(%s)
        """, (location_id, contact_ids))

        results = {}
        for row in cur.fetchall():
            cid = row['contact_id']
            analyzed_at = row['analyzed_at']
            last_msg_at = row.get('last_msg_at')

            # Skip stale cache — new messages arrived after analysis
            if last_msg_at and analyzed_at:
                a = analyzed_at
                m = last_msg_at
                if hasattr(a, 'tzinfo') and a.tzinfo:
                    a = a.replace(tzinfo=None)
                if hasattr(m, 'tzinfo') and m.tzinfo:
                    m = m.replace(tzinfo=None)
                if m > a:
                    continue

            analysis = row['analysis']
            if isinstance(analysis, str):
                analysis = json.loads(analysis)

            results[cid] = {
                "temperature": analysis.get("temperature", "warm"),
                "score": analysis.get("score", 50),
                "summary": analysis.get("summary", ""),
                "temperature_reason": analysis.get("temperature_reason", ""),
                "should_respond": analysis.get("should_respond", False),
                "engagement_level": analysis.get("engagement_level", 0),
            }

        cur.close()
        return results

    except Exception as e:
        logger.error(f"Bulk intelligence fetch failed: {e}")
        return {}
    finally:
        return_db_connection(conn)


def batch_analyze_contacts(location_id, contact_ids, limit=5):
    """
    Run AI analysis for a batch of contacts that don't have fresh cache.
    Processes up to `limit` contacts synchronously (each takes ~2-3s).
    Returns list of {contact_id, temperature, score} for successfully analyzed contacts.
    """
    if not contact_ids or not _client:
        return []

    # Filter to only contacts that actually need analysis
    already_cached = get_bulk_cached_intelligence(location_id, contact_ids)
    need_analysis = [cid for cid in contact_ids if cid not in already_cached][:limit]

    if not need_analysis:
        return []

    results = []
    for cid in need_analysis:
        try:
            intel = get_contact_intelligence(location_id, cid)
            if intel and intel.get("temperature") != "unknown":
                score = intel.get("score", 50)
                if isinstance(score, dict):
                    score = score.get("score", 50)
                results.append({
                    "contact_id": cid,
                    "temperature": intel.get("temperature", "warm"),
                    "score": score,
                    "summary": intel.get("summary", ""),
                    "temperature_reason": intel.get("temperature_reason", ""),
                })
        except Exception as e:
            logger.error(f"Batch analysis failed for {cid}: {e}")

    logger.info(f"Batch analyzed {len(results)}/{len(need_analysis)} contacts for {location_id}")
    return results


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
        # Normalize score format: cache stores int, API returns {"score": int, "label": str}
        raw_score = cached.get("score", 50)
        if isinstance(raw_score, (int, float)):
            cached["score"] = {"score": int(raw_score), "label": cached.get("temperature", "warm")}
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
            "should_respond": ai_result.get("should_respond", False),
            "should_respond_reason": ai_result.get("should_respond_reason", ""),
            "engagement_level": ai_result.get("engagement_level", 0),
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
