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
    Returns a dict with conversation, facts, pipeline, calls, tags,
    message timing signals, and response patterns.
    """
    ctx = {
        "messages": [],
        "messages_with_time": [],  # (role, text, created_at) for timing analysis
        "facts": [],
        "pipeline": None,
        "calls": {"total": 0, "agent_called": 0, "lead_called": 0, "answered": 0, "unanswered": 0, "long_calls": 0, "last_call": None},
        "call_transcripts": [],  # Recent call transcripts for content awareness
        "tags": [],
        "narrative": None,
        "last_message_at": None,
        "timing": {},  # Computed timing signals
    }

    conn = get_db_connection()
    if not conn:
        return ctx

    try:
        cur = conn.cursor()

        # Recent messages (last 30 for context) — WITH timestamps for timing
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
                    ctx["messages_with_time"].append({
                        "role": role,
                        "text": r['message_text'],
                        "at": r['created_at'],
                    })
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
                FROM crm_deals
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

        # Call transcripts — last 3 calls with transcripts for content awareness
        try:
            cur.execute("""
                SELECT direction, duration, transcript, created_at
                FROM call_history
                WHERE location_id = %s AND contact_id = %s
                  AND transcript IS NOT NULL AND transcript != ''
                ORDER BY created_at DESC
                LIMIT 3
            """, (location_id, contact_id))
            for r in cur.fetchall():
                # Truncate long transcripts to ~300 chars to stay within token budget
                transcript = (r.get('transcript') or '')[:300]
                if transcript:
                    direction = r.get('direction', 'unknown')
                    duration = r.get('duration', 0) or 0
                    ctx["call_transcripts"].append({
                        "direction": direction,
                        "duration": duration,
                        "transcript": transcript,
                        "at": str(r['created_at'])[:10] if r.get('created_at') else '',
                    })
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

    # ── Compute timing & behavioral signals from message timestamps ──
    ctx["timing"] = _compute_timing_signals(ctx["messages_with_time"])

    return ctx


def _compute_timing_signals(messages_with_time):
    """
    Analyze message timestamps to produce timing and behavioral signals.
    This gives the AI temporal awareness that raw message text cannot provide.

    Returns dict with:
      - who_spoke_last: "Lead" or "Bot"
      - consecutive_unanswered_bot: int (how many bot messages with no lead reply at end)
      - lead_last_message_age: str (human-readable like "3 hours ago", "5 days ago")
      - first_message_age: str (how long ago conversation started)
      - lead_response_times: list of response gaps in hours (lead replied to bot)
      - avg_lead_response_hours: float or None
      - lead_message_lengths: list of character counts (is lead writing more or less?)
      - velocity_trend: "warming" | "cooling" | "steady" | "unknown"
    """
    signals = {
        "who_spoke_last": "unknown",
        "consecutive_unanswered_bot": 0,
        "lead_last_message_age": "unknown",
        "first_message_age": "unknown",
        "avg_lead_response_hours": None,
        "velocity_trend": "unknown",
    }

    if not messages_with_time:
        return signals

    now = datetime.utcnow()

    # Who spoke last + consecutive unanswered bot messages
    signals["who_spoke_last"] = messages_with_time[-1]["role"]
    consecutive = 0
    for msg in reversed(messages_with_time):
        if msg["role"] == "Bot":
            consecutive += 1
        else:
            break
    signals["consecutive_unanswered_bot"] = consecutive

    # First message age
    first_at = messages_with_time[0].get("at")
    if first_at:
        if isinstance(first_at, str):
            try:
                first_at = datetime.fromisoformat(first_at.replace('Z', '+00:00')).replace(tzinfo=None)
            except Exception:
                first_at = None
        elif hasattr(first_at, 'tzinfo') and first_at.tzinfo:
            first_at = first_at.replace(tzinfo=None)
        if first_at:
            signals["first_message_age"] = _humanize_delta(now - first_at)

    # Lead's last message age
    for msg in reversed(messages_with_time):
        if msg["role"] == "Lead":
            lead_at = msg["at"]
            if isinstance(lead_at, str):
                try:
                    lead_at = datetime.fromisoformat(lead_at.replace('Z', '+00:00')).replace(tzinfo=None)
                except Exception:
                    lead_at = None
            elif hasattr(lead_at, 'tzinfo') and lead_at.tzinfo:
                lead_at = lead_at.replace(tzinfo=None)
            if lead_at:
                signals["lead_last_message_age"] = _humanize_delta(now - lead_at)
            break

    # Response gap analysis + velocity trend
    # Track: how fast does the lead reply to bot messages? Are replies getting faster/slower?
    lead_response_gaps = []
    lead_message_lengths = []
    last_bot_at = None
    for msg in messages_with_time:
        msg_at = msg["at"]
        if isinstance(msg_at, str):
            try:
                msg_at = datetime.fromisoformat(msg_at.replace('Z', '+00:00')).replace(tzinfo=None)
            except Exception:
                msg_at = None
        elif hasattr(msg_at, 'tzinfo') and msg_at.tzinfo:
            msg_at = msg_at.replace(tzinfo=None)

        if msg["role"] == "Bot":
            last_bot_at = msg_at
        elif msg["role"] == "Lead":
            lead_message_lengths.append(len(msg.get("text", "")))
            if last_bot_at and msg_at:
                gap_hours = (msg_at - last_bot_at).total_seconds() / 3600
                if gap_hours >= 0:
                    lead_response_gaps.append(gap_hours)
            last_bot_at = None  # Reset — only count first lead reply per bot message

    if lead_response_gaps:
        signals["avg_lead_response_hours"] = round(sum(lead_response_gaps) / len(lead_response_gaps), 1)

    # Velocity trend: compare first half vs second half of response times + message lengths
    if len(lead_response_gaps) >= 4:
        mid = len(lead_response_gaps) // 2
        first_half_avg = sum(lead_response_gaps[:mid]) / mid
        second_half_avg = sum(lead_response_gaps[mid:]) / (len(lead_response_gaps) - mid)
        # Also factor in message length trend
        if len(lead_message_lengths) >= 4:
            mid_l = len(lead_message_lengths) // 2
            first_len = sum(lead_message_lengths[:mid_l]) / mid_l
            second_len = sum(lead_message_lengths[mid_l:]) / (len(lead_message_lengths) - mid_l)
        else:
            first_len = second_len = 1

        # Warming = responding faster AND/OR writing more
        # Cooling = responding slower AND/OR writing less
        speed_ratio = first_half_avg / max(second_half_avg, 0.1)  # >1 = getting faster
        length_ratio = second_len / max(first_len, 1)  # >1 = writing more

        if speed_ratio > 1.5 or (speed_ratio > 1.2 and length_ratio > 1.3):
            signals["velocity_trend"] = "warming"
        elif speed_ratio < 0.6 or (speed_ratio < 0.8 and length_ratio < 0.7):
            signals["velocity_trend"] = "cooling"
        else:
            signals["velocity_trend"] = "steady"

    return signals


def _humanize_delta(delta):
    """Convert a timedelta to a human-readable string."""
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return "just now"
    if total_seconds < 60:
        return "just now"
    if total_seconds < 3600:
        mins = total_seconds // 60
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    if total_seconds < 86400:
        hours = total_seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = total_seconds // 86400
    if days == 1:
        return "1 day ago"
    if days < 30:
        return f"{days} days ago"
    months = days // 30
    return f"{months} month{'s' if months != 1 else ''} ago"


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ AI MICRO-PROMPT — Single call generates everything ═════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _build_timing_block(timing):
    """Build the timing/behavioral signals block for the AI prompt."""
    if not timing or timing.get("who_spoke_last") == "unknown":
        return ""

    lines = ["TIMING & BEHAVIORAL SIGNALS:"]

    who = timing.get("who_spoke_last", "unknown")
    lines.append(f"- Who spoke last: {who}")

    consec = timing.get("consecutive_unanswered_bot", 0)
    if consec > 0:
        lines.append(f"- Consecutive bot messages with no lead reply: {consec}")

    lead_age = timing.get("lead_last_message_age", "unknown")
    if lead_age != "unknown":
        lines.append(f"- Lead's last message: {lead_age}")

    first_age = timing.get("first_message_age", "unknown")
    if first_age != "unknown":
        lines.append(f"- Conversation started: {first_age}")

    avg_resp = timing.get("avg_lead_response_hours")
    if avg_resp is not None:
        if avg_resp < 1:
            lines.append(f"- Lead's avg response time: {int(avg_resp * 60)} minutes")
        else:
            lines.append(f"- Lead's avg response time: {avg_resp} hours")

    velocity = timing.get("velocity_trend", "unknown")
    if velocity != "unknown":
        label = {
            "warming": "WARMING (responding faster, writing more)",
            "cooling": "COOLING (responding slower, writing less)",
            "steady": "Steady (consistent response pattern)",
        }.get(velocity, velocity)
        lines.append(f"- Velocity trend: {label}")

    return "\n".join(lines)


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

    # Call transcripts — what was actually discussed on calls
    transcript_str = ""
    if ctx.get("call_transcripts"):
        transcript_lines = []
        for t in ctx["call_transcripts"]:
            direction = t.get("direction", "?")
            dur = t.get("duration", 0)
            date = t.get("at", "?")
            text = t.get("transcript", "")
            transcript_lines.append(f"[{date} {direction} {dur}s] {text}")
        transcript_str = "\nCALL TRANSCRIPTS (most recent calls with recordings):\n" + "\n".join(transcript_lines)

    narrative_str = ctx.get("narrative") or "No prior narrative."

    # Timing and behavioral signals
    timing = ctx.get("timing", {})
    timing_str = _build_timing_block(timing)

    prompt = f"""You are an AI sales coach analyzing an insurance lead for an agent. Read ALL the data below carefully and produce a JSON intelligence report. Your classification MUST be based on what the lead ACTUALLY SAID AND DID, not just whether they responded.

CRITICAL — READ CONVERSATIONS IN CONTEXT:
Messages are a back-and-forth thread between "Bot" (the agent's AI assistant) and "Lead" (the prospect). You MUST read each Lead reply as a RESPONSE TO THE PRECEDING Bot message. Short or single-word Lead replies ONLY make sense in context of what the Bot just said.
Examples of contextual reading:
- Bot: "Would you like a free quote?" → Lead: "sure" = INTERESTED (responding yes to the offer)
- Bot: "Reply STOP if you want to stop receiving messages" → Lead: "stop" = WANTS TO OPT OUT (not interested in anything)
- Bot: "Do you currently have life insurance?" → Lead: "no" = NO EXISTING COVERAGE (not rejecting the conversation)
- Bot: "Let me know if you want to stop talking" → Lead: "want" = WANTS TO STOP (completing the Bot's sentence)
- Bot: "I can help with term or whole life — which interests you?" → Lead: "term" = INTERESTED IN TERM LIFE (not a one-word dismissal)
- Bot: "Great! When works best for a call?" → Lead: "never" = REJECTING (not scheduling)
Never classify a message in isolation. A "yes", "no", "ok", "sure", "want", "stop", etc. can mean completely different things depending on what the Bot just said.

CONVERSATION HISTORY:
{convo}

KNOWN FACTS ABOUT LEAD: {facts}
TAGS: {tags}
{pipeline_str}
CALL HISTORY: {calls_str}{transcript_str}
PRIOR NARRATIVE: {narrative_str}
{timing_str}
CURRENT DATE: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}

Respond with ONLY valid JSON (no markdown, no code fences, no explanation). Use this exact structure:
{{{{
  "summary": "2-sentence situational snapshot. What does the agent need to know RIGHT NOW about this lead? Be specific — reference actual conversation details, names, products, objections. Include timing context (e.g. 'Last replied 3 days ago' or 'Responded within minutes').",
  "temperature": "hot | warm | cool | cold",
  "temperature_reason": "One sentence explaining why, referencing BOTH what they said AND their behavioral pattern (timing, responsiveness, message depth).",
  "score": 0-100,
  "should_respond": true or false,
  "should_respond_reason": "Why the agent should or should not respond right now.",
  "engagement_level": 0-3,
  "under_contract": true or false,
  "actions": [
    {{{{
      "action": "Short imperative action (e.g. 'Call back about the term quote')",
      "reason": "Why this action matters right now",
      "priority": "high | medium | low",
      "icon": "fa-solid fa-<appropriate-icon>"
    }}}}
  ]
}}}}

CRITICAL CLASSIFICATION RULES — READ THESE CAREFULLY:

"temperature" — Based on CONTENT, CONTEXT, AND BEHAVIOR:
- hot = Lead is actively buying: asking for quotes, requesting coverage, comparing products, ready to book, saying "let's do it." They are IN the buying process. ALSO hot if velocity_trend is "warming" AND engagement_level >= 2.
- warm = Lead is engaged and positive: responding to questions, sharing information about their situation, showing genuine interest. The conversation is progressing forward.
- cool = Lead went quiet (no response to last 2+ messages), giving slow/short replies, showing declining interest, soft objection ("let me think about it"), OR velocity_trend is "cooling." If bot sent 2+ messages with no lead reply, this is AT MOST cool — not warm or hot.
- cold = Lead explicitly said no: "not interested", "stop", "already covered", "don't contact me", OR has completely ghosted (no response to 3+ outreach attempts over multiple days). "no thanks" or "I'm good" = COLD, not hot.
IMPORTANT: If the Bot offered an opt-out and the lead's reply CONFIRMS they want to opt out (even with a single word that completes the Bot's sentence), that is COLD. Read the lead's reply in the context of what the Bot said immediately before it.
TIMING RULE: If the lead has not responded in 7+ days despite bot outreach, they cannot be "warm" — they are at best "cool" unless they have a scheduled callback.

"score" — 0-100 likelihood to convert to a paying client:
HARD CALIBRATION RULES (these override your intuition):
  - Lead said "not interested" / "stop" / "no" / "I'm good" → score MUST be 5-15
  - No conversation exists (only bot outreach, no lead replies) → score MUST be 10-25
  - Lead replied but only surface-level (one-word, "who is this") → score 15-30
  - Lead engaged but has soft objections ("too expensive", "let me think") → score 25-45
  - Lead actively discussing coverage, answering questions, sharing details → score 45-70
  - Lead asking for quotes, comparing products, scheduling calls → score 65-85
  - Lead ready to buy, discussing specific policies/terms/amounts → score 80-95
  TIMING ADJUSTMENTS: Subtract 10-20 points if lead hasn't responded in 7+ days. Add 5-10 points if lead responded within minutes/hours.
  Call history: "outbound by agent" = the AGENT called the lead (NOT lead-initiated). "inbound from lead" = the LEAD called in. "answered (>30s)" = actually spoke. "unanswered" = no answer, busy, or call under 30s (likely voicemail/no pickup). Do NOT say "lead initiated a call" when the call was outbound by agent.

"should_respond" — true if the agent needs to take action NOW:
- true: Lead asked a question that hasn't been answered, expressed interest that wasn't followed up, or sent a message that deserves a reply. ALSO true if lead responded recently and the last message is from the Lead.
- false: Bot already replied to the lead's last message, lead said stop/not interested, lead hasn't sent any messages, or there is genuinely nothing to respond to.
- This is about whether the lead is WAITING for a response, not just whether they're a good lead.
- TIMING: If who_spoke_last is "Lead", should_respond is almost always true (lead is waiting).

"engagement_level" — How deep the interaction has gone (0-3):
- 0 = No meaningful interaction yet (new lead, only outbound attempts, no lead replies at all)
- 1 = Surface contact (lead acknowledged but no real conversation — one-word replies, "who is this", etc.)
- 2 = Real conversation (lead is sharing information, asking questions, back-and-forth dialogue)
- 3 = Deep engagement (multiple exchanges, discussing specifics, answered calls >30s where they actually spoke, approaching a decision). Unanswered outbound calls do NOT count as engagement.

"under_contract" — true if this contact appears to be an EXISTING CLIENT who already bought a policy, NOT an unsold lead:
- Look at the PIPELINE data. If the contact is in a pipeline/stage that signals a completed sale (e.g. "sold", "closed won", "active client", "policy issued", "customer", "onboarding", "retention", "renewal", "in force", "bound"), set true.
- Also look at conversation content: if the lead clearly purchased, has policy numbers, or is discussing servicing an existing policy, set true.
- If the contact is in a sales/prospecting pipeline (e.g. "new leads", "follow up", "quoted", "nurture", "cold leads"), set false.
- When in doubt (no pipeline, unclear stage), set false. Most contacts are unsold leads.

"actions" — 2-4 specific next steps. Use icons: fa-phone (calls), fa-paper-plane (SMS), fa-file-invoice-dollar (quotes), fa-calendar (appointments), fa-fire (urgency), fa-clock (timing), fa-reply (responding), fa-bolt (quick action).
- Factor in TIMING: if lead hasn't replied in days, don't suggest "follow up on the conversation" — suggest a new angle or different channel (call instead of text).
- If call transcripts show specific topics discussed, reference them in actions.
- Be direct and actionable. No fluff."""

    try:
        response = _client.chat.completions.create(
            model=INTELLIGENCE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
            timeout=15.0,
        )
        raw = (response.choices[0].message.content or "").strip()

        # Strip markdown code fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)

        result = json.loads(raw)

        # Validate and calibrate
        result = _validate_and_calibrate(result, ctx)

        return result

    except json.JSONDecodeError as e:
        logger.error(f"AI intelligence JSON parse failed for {contact_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"AI intelligence call failed for {contact_id}: {e}")
        return None


def _validate_and_calibrate(result, ctx):
    """
    Validate required fields AND apply hard score/temperature calibration.
    The AI is instructed to follow the calibration rules, but this function
    enforces them structurally as a safety net. If the AI's output violates
    a hard rule, we correct it here.
    """
    # --- Basic field validation ---
    if not isinstance(result.get('actions'), list):
        result['actions'] = []
    if result.get('temperature') not in ('hot', 'warm', 'cool', 'cold'):
        result['temperature'] = 'warm'
    if not isinstance(result.get('score'), (int, float)):
        result['score'] = 50
    result['score'] = max(0, min(100, int(result['score'])))
    if not isinstance(result.get('should_respond'), bool):
        result['should_respond'] = False
    if not isinstance(result.get('should_respond_reason'), str):
        result['should_respond_reason'] = ''
    if not isinstance(result.get('engagement_level'), (int, float)):
        result['engagement_level'] = 0
    result['engagement_level'] = max(0, min(3, int(result['engagement_level'])))
    if not isinstance(result.get('under_contract'), bool):
        result['under_contract'] = False
    if not isinstance(result.get('summary'), str) or len(result.get('summary', '')) < 5:
        result['summary'] = 'Analysis incomplete.'
    if not isinstance(result.get('temperature_reason'), str):
        result['temperature_reason'] = ''

    # --- Hard calibration rules (structural enforcement) ---
    timing = ctx.get("timing", {})
    messages = ctx.get("messages", [])
    consec_unanswered = timing.get("consecutive_unanswered_bot", 0)

    # Shared stop/opt-out word set (matches voice/intelligence.py _stop_words)
    _opt_out_phrases = {
        "stop", "unsubscribe", "opt out", "optout", "remove me",
        "do not contact", "do not call", "do not text", "do not message",
        "cancel", "quit", "leave me alone", "not interested",
        "lose my number", "delete my number", "take me off",
        "don't contact", "don't call", "don't text",
        "im good", "i'm good", "no thanks", "no thank you",
    }

    # Rule 0: Stop / opt-out detection → force cold, score 0-5, should_respond=false
    # This catches cases where the AI misreads a "stop" reply as something else.
    lead_messages = [m for m in messages if m.startswith("Lead:")]
    if lead_messages:
        last_lead_msg = ""
        for m in reversed(messages):
            if m.startswith("Lead:"):
                last_lead_msg = m[5:].strip().lower()
                break
        if last_lead_msg and any(phrase in last_lead_msg for phrase in _opt_out_phrases):
            # Check if the entire message is basically just the opt-out phrase
            # (not "I'm not interested in whole life but term sounds good")
            clean = last_lead_msg.strip().rstrip('.!?')
            is_pure_rejection = len(clean) < 40 or clean in _opt_out_phrases
            if is_pure_rejection:
                result['temperature'] = 'cold'
                result['score'] = min(result['score'], 5)
                result['should_respond'] = False
                result['should_respond_reason'] = 'Lead opted out or expressed clear disinterest.'
                result['engagement_level'] = min(result['engagement_level'], 1)

    # Rule 1: No lead messages at all → can't be warm/hot, score capped at 25
    if not lead_messages:
        if result['temperature'] in ('hot', 'warm'):
            result['temperature'] = 'cool'
        result['score'] = min(result['score'], 25)
        result['engagement_level'] = 0
        if result['should_respond']:
            result['should_respond'] = False
            result['should_respond_reason'] = 'No lead messages to respond to.'

    # Rule 2: 3+ consecutive bot messages unanswered → at most "cool"
    if consec_unanswered >= 3:
        if result['temperature'] in ('hot', 'warm'):
            result['temperature'] = 'cool'
        result['score'] = min(result['score'], 30)

    # Rule 3: 2+ consecutive bot messages unanswered → cannot be "hot"
    if consec_unanswered >= 2:
        if result['temperature'] == 'hot':
            result['temperature'] = 'warm'

    # Rule 4: If who_spoke_last is Lead, should_respond is almost always true
    # (unless Rule 0 already set should_respond=false for opt-out)
    if timing.get("who_spoke_last") == "Lead" and not result['should_respond']:
        last_lead_msg = ""
        for m in reversed(messages):
            if m.startswith("Lead:"):
                last_lead_msg = m[5:].strip().lower()
                break
        if not any(sw in last_lead_msg for sw in _opt_out_phrases):
            result['should_respond'] = True
            if not result['should_respond_reason']:
                result['should_respond_reason'] = 'Lead spoke last — they may be waiting for a reply.'

    # Rule 5: Temperature/score coherence
    if result['temperature'] == 'cold' and result['score'] > 20:
        result['score'] = min(result['score'], 20)
    if result['temperature'] == 'hot' and result['score'] < 55:
        result['score'] = max(result['score'], 55)

    # Rule 6: Under contract → neutralize temperature for pipeline purposes
    # The frontend correctly groups under_contract contacts into their own group,
    # but the cached temperature can still leak into the SMS bot pipeline via
    # get_cached_temperature(). Set temperature to None so the bot pipeline
    # doesn't inject misleading "LEAD TEMPERATURE: HOT" for existing clients.
    if result.get('under_contract'):
        result['temperature'] = 'neutral'
        result['should_respond'] = False
        result['should_respond_reason'] = 'Existing client under contract — not an active sales lead.'

    return result


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
                FROM crm_deals
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


def _run_bulk_ai_analysis(location_id, contact_blocks, all_contexts=None):
    """
    Analyze multiple contacts in a single LLM call.
    contact_blocks: list of (contact_id, text_block) tuples.
    all_contexts: optional dict of {contact_id: ctx_dict} for calibration.
    Returns: {contact_id: analysis_dict} for successfully parsed contacts.
    """
    if not _client or not contact_blocks:
        return {}
    all_contexts = all_contexts or {}

    contacts_text = "\n\n---\n\n".join(block for _, block in contact_blocks)
    id_list = ", ".join(cid for cid, _ in contact_blocks)

    prompt = f"""You are an AI sales coach bulk-analyzing insurance leads. Below are {len(contact_blocks)} contacts separated by "---". For EACH contact, produce a JSON analysis object.

CRITICAL — READ CONVERSATIONS IN CONTEXT:
Each contact has a "CONVO" section showing Bot/Lead message exchanges. You MUST read each Lead reply as a RESPONSE TO THE PRECEDING Bot message. Short replies ("yes", "no", "ok", "stop", "want") mean completely different things depending on what the Bot just asked. For example: Bot asks "want to stop?" + Lead says "want" = WANTS TO STOP. Bot asks "want a quote?" + Lead says "want" = WANTS A QUOTE. Never classify messages in isolation — always read the thread as a conversation.

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
    "under_contract": true or false,
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
- temperature: hot=actively buying/quoting/ready. warm=engaged/sharing info/positive. cool=went quiet/short replies/soft objections/2+ bot messages unanswered. cold=said no/stop/not interested/ghosted 3+ attempts. "no thanks"=COLD not hot. TIMING: if no lead reply in 7+ days despite outreach, AT MOST "cool."
- score CALIBRATION (mandatory ranges): not interested/stop=5-15. No lead replies at all=10-25. Surface one-word replies=15-30. Soft objections=25-45. Discussing coverage/sharing details=45-70. Asking for quotes/scheduling=65-85. Ready to buy=80-95.
- should_respond: true only if the lead is WAITING for a reply (unanswered question, unaddressed interest, lead spoke last). false if bot already replied, lead said stop, or nothing to respond to.
- engagement_level: 0=no lead replies at all, 1=surface (one-word replies), 2=real conversation, 3=deep (specifics, calls >30s, near decision). Unanswered outbound calls do NOT count.
- under_contract: true if contact is an EXISTING CLIENT (sold). Look at PIPELINE — stages like "sold", "closed won", "active client", "policy issued", "customer", "in force", "bound", "retention", "renewal" = true. Sales/prospecting pipelines ("new leads", "follow up", "quoted", "nurture") = false. Also check conversation for policy numbers or servicing existing coverage. When in doubt, false.
- actions: 2-4 per contact. Factor in TIMING — if lead hasn't replied in days, suggest a new angle or different channel, not "follow up." Icons: fa-phone, fa-paper-plane, fa-file-invoice-dollar, fa-calendar, fa-fire, fa-clock, fa-reply, fa-bolt.

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

            # Validate + calibrate using same rules as single analysis
            ctx = all_contexts.get(cid, {"messages": [], "timing": {}})
            item = _validate_and_calibrate(item, ctx)
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
                    ctx = all_contexts.get(cid, {"messages": [], "timing": {}})
                    item = _validate_and_calibrate(item, ctx)
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
        ai_results = _run_bulk_ai_analysis(location_id, chunk, all_contexts=all_contexts)

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

        # 24-hour TTL — cache expires after 24 hours regardless of messages.
        # The bulk analysis engine can process 1000 contacts in ~30s, so
        # keeping stale classifications indefinitely is unnecessary.
        from datetime import timedelta
        age = datetime.utcnow() - analyzed_at
        if age > timedelta(hours=24):
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


def get_cached_temperature(contact_id: str) -> dict:
    """
    Lightweight cache read for the live SMS pipeline.
    Returns cached temperature, score, and engagement_level if available.
    Zero AI cost — reads DB only.

    Returns None for:
      - No cached data
      - Under-contract contacts (existing clients, not sales leads)
      - Neutral temperature (set by calibration for sold clients)

    Returns: {"temperature": str, "score": int, "engagement_level": int} or None
    """
    conn = get_db_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT analysis FROM contact_intelligence
            WHERE contact_id = %s
        """, (contact_id,))
        row = cur.fetchone()
        cur.close()

        if not row:
            return None

        analysis = row.get('analysis') if isinstance(row, dict) else row[0]
        if isinstance(analysis, str):
            analysis = json.loads(analysis)

        if not analysis:
            return None

        # Don't inject temperature context for existing clients —
        # they're not sales leads and the bot pipeline shouldn't
        # adjust its approach based on a sold client's temperature.
        if analysis.get("under_contract"):
            return None

        temp = analysis.get("temperature", "")
        # "neutral" is set by calibration for under_contract contacts
        if not temp or temp == "neutral":
            return None

        return {
            "temperature": temp,
            "score": analysis.get("score", 0),
            "engagement_level": analysis.get("engagement_level", 0),
        }
    except Exception:
        return None
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ BULK API — Fetch cached AI classifications for Smart Filters ════════════
# ═══════════════════════════════════════════════════════════════════════════════

def get_bulk_cached_intelligence(location_id, contact_ids):
    """
    Fetch cached AI intelligence for multiple contacts in one DB query.
    Returns only contacts with fresh cache (<24h old AND no new messages
    since analysis). Stale contacts appear as 'uncached' so they get
    re-analyzed on next dialer load. Zero AI cost — single SQL query.

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
        # 24-hour TTL — stale cache is excluded so uncached contacts get re-analyzed.
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
              AND ci.analyzed_at > NOW() - INTERVAL '24 hours'
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
                "under_contract": analysis.get("under_contract", False),
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
