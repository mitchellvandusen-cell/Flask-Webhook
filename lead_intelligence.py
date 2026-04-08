# lead_intelligence.py - Rule-Based Contact Scoring + Fact Extraction
#
# Scores contacts using keyword detection + recency + engagement signals.
# Zero AI cost for scoring. Runs in milliseconds. Same output format as before.
# Results cached in contact_intelligence table with temperature-based TTLs.
#
# Fact extraction: lightweight LLM call extracts discrete personal facts
# (married, kids, job, etc.) from conversation history. Runs in background
# RQ only — zero impact on main SMS pipeline.

import json
import logging
import os
import re
from datetime import datetime
from db import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ KEYWORD LISTS ═══════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

# Contacts who have explicitly opted out → Cold, score 5, do not contact
_STOP_WORDS = [
    "stop", "unsubscribe", "cancel", "opt out", "remove me",
    "do not call", "do not text", "do not contact", "do not message",
    "take me off", "take me out", "never contact", "leave me alone",
    "lose my number", "delete my number",
]

# Lead is signaling intent to buy or schedule
_BUYING_WORDS = [
    "quote", "how much", "what's the price", "whats the price",
    "how much is it", "how much does", "price", "cost",
    "ready to", "sign me up", "sign up", "want to sign",
    "let's do it", "lets do it", "let's go", "lets go",
    "get started", "get me started", "i'm in", "im in",
    "enroll", "apply", "application",
    "sounds good", "i want",
    "book a", "schedule a call", "set up a call",
    "call me", "can you call", "give me a call",
    "when can we", "when can you",
    "coverage", "policy", "premium",
    "interested", "more info", "tell me more",
    "set up a meeting", "make an appointment",
]

# Lead is pushing back — objections across all 6 types
_OBJECTION_WORDS = [
    "not interested", "no thanks", "no thank you",
    "already have", "have insurance", "have a policy", "already covered",
    "i'm covered", "we're covered", "employer covers", "just renewed",
    "too expensive", "can't afford", "cannot afford", "no money",
    "not in my budget", "tight on money", "out of budget",
    "think about it", "let me think", "need to think",
    "not ready", "maybe later", "not right now", "not a good time",
    "call me back", "hit me up later", "not now",
    "busy", "in a meeting", "driving", "bad time",
    "ask my wife", "ask my husband", "check with my",
    "talk to my spouse", "check with my partner",
]


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ CONTEXT BUILDER — Gathers all data for scoring ═══════════════════════════
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
        "call_transcripts": [],
        "tags": [],
        "narrative": None,
        "last_message_at": None,
        "timing": {},
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

    # Compute timing & behavioral signals from message timestamps
    ctx["timing"] = _compute_timing_signals(ctx["messages_with_time"])

    return ctx


def _compute_timing_signals(messages_with_time):
    """
    Analyze message timestamps to produce timing and behavioral signals.
    Returns dict with who_spoke_last, consecutive_unanswered_bot,
    lead_last_message_age, avg_lead_response_hours, velocity_trend.
    """
    signals = {
        "who_spoke_last": "unknown",
        "consecutive_unanswered_bot": 0,
        "first_message_age": None,
        "lead_last_message_age": None,
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
            last_bot_at = None

    if lead_response_gaps:
        signals["avg_lead_response_hours"] = round(sum(lead_response_gaps) / len(lead_response_gaps), 1)

    if len(lead_response_gaps) >= 4:
        mid = len(lead_response_gaps) // 2
        first_half_avg = sum(lead_response_gaps[:mid]) / mid
        second_half_avg = sum(lead_response_gaps[mid:]) / (len(lead_response_gaps) - mid)
        if len(lead_message_lengths) >= 4:
            mid_l = len(lead_message_lengths) // 2
            first_len = sum(lead_message_lengths[:mid_l]) / mid_l
            second_len = sum(lead_message_lengths[mid_l:]) / (len(lead_message_lengths) - mid_l)
        else:
            first_len = second_len = 1

        speed_ratio = first_half_avg / max(second_half_avg, 0.1)
        length_ratio = second_len / max(first_len, 1)

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
# ═══ RULES SCORER — keyword + recency + engagement, zero cost ════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _contact_summary(n_inbound, hours_since, connected, long_calls, pipeline, situation):
    """Build a brief human-readable summary from available contact data."""
    parts = []
    if n_inbound == 0:
        parts.append("No text replies yet")
    elif n_inbound == 1:
        parts.append("1 inbound message")
    else:
        parts.append(f"{n_inbound} inbound messages")
    if hours_since < 9999:
        if hours_since < 1:
            parts.append("last reply < 1h ago")
        elif hours_since < 24:
            parts.append(f"last reply {int(hours_since)}h ago")
        else:
            parts.append(f"last reply {int(hours_since / 24)}d ago")
    if connected > 0:
        parts.append(f"{connected} connected call{'s' if connected != 1 else ''}")
    if pipeline and pipeline.get("stage_name"):
        parts.append(f"pipeline: {pipeline['stage_name']}")
    base = "; ".join(parts)
    if situation:
        return f"{base}. {situation}" if base else situation
    return base


def _make_result(temperature, score, reason, should_respond, eng, summary, actions):
    """Build a standardized scoring result dict."""
    return {
        "temperature": temperature,
        "score": score,
        "temperature_reason": reason,
        "should_respond": should_respond,
        "should_respond_reason": (
            "Recent inbound message awaiting reply." if should_respond
            else "No immediate response needed."
        ),
        "engagement_level": eng,
        "summary": summary,
        "actions": actions,
        "rule_based": True,
    }


def _rules_score_contact(ctx):
    """
    Score a contact using keyword detection + recency + engagement rules.
    No AI. No network calls. Returns analysis dict matching the cached format.
    """
    msgs = ctx.get("messages_with_time", [])
    tags = [t.lower().strip() for t in (ctx.get("tags") or [])]
    calls = ctx.get("calls", {})
    pipeline = ctx.get("pipeline")
    timing = ctx.get("timing", {})
    now = datetime.utcnow()

    inbound = [m for m in msgs if m.get("role") == "Lead"]

    # --- Recency (hours since last inbound message) ---
    hours_since = 9999
    if inbound:
        last_at = inbound[-1].get("at")
        if last_at:
            if isinstance(last_at, str):
                try:
                    last_at = datetime.fromisoformat(last_at.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    last_at = None
            elif hasattr(last_at, "tzinfo") and last_at.tzinfo:
                last_at = last_at.replace(tzinfo=None)
            if last_at:
                hours_since = max(0, (now - last_at).total_seconds() / 3600)

    # --- Text analysis on last 5 inbound messages ---
    recent_text = " " + " ".join(m.get("text", "").lower() for m in inbound[-5:]) + " "

    # Use word-boundary matching for stop words to avoid false positives like
    # "nonstop", "I can't stop thinking", etc. matching bare "stop".
    def _word_match(keyword: str, text: str) -> bool:
        return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text))

    has_stop = any(_word_match(k, recent_text) for k in _STOP_WORDS)
    has_buying = any(k in recent_text for k in _BUYING_WORDS)
    has_objection = any(k in recent_text for k in _OBJECTION_WORDS)
    has_dnc_tag = any(t in ("dnc", "do not call", "do not contact", "unsubscribed", "opted out") for t in tags)

    # --- Call signals ---
    connected = calls.get("answered", 0) or 0
    long_calls = calls.get("long_calls", 0) or 0
    total_calls = calls.get("total", 0) or 0

    # --- Velocity from timing signals ---
    velocity = timing.get("velocity_trend", "")
    consecutive_unanswered = timing.get("consecutive_unanswered_bot", 0) or 0

    # --- Engagement depth ---
    n_inbound = len(inbound)
    if n_inbound == 0:
        eng = 0
    elif n_inbound <= 2:
        eng = 1
    elif n_inbound <= 8:
        eng = 2
    else:
        eng = 3
    if long_calls > 0:
        eng = max(eng, 2)

    # --- Should respond: last message from lead AND recent AND not opted out ---
    last_msg_role = msgs[-1].get("role") if msgs else None
    should_respond = bool(
        last_msg_role == "Lead"
        and hours_since < 48
        and not has_stop
        and not has_dnc_tag
    )

    # ── Classification ──────────────────────────────────────────────────────

    if has_stop or has_dnc_tag:
        return _make_result(
            "cold", 5,
            "Opted out or marked Do Not Contact.",
            should_respond=False, eng=0,
            summary=f"Contact has requested no further contact. {n_inbound} inbound messages on record.",
            actions=[{"action": "Verify DNC compliance — do not contact", "priority": "high", "icon": "fa-ban"}],
        )

    if has_buying and hours_since < 96:
        score = min(92, 70 + min(15, connected * 5) + min(7, long_calls * 3))
        if velocity == "warming":
            score = min(96, score + 5)
        return _make_result(
            "hot", score,
            f"Expressed buying intent recently ({int(hours_since)}h ago).",
            should_respond=should_respond, eng=eng,
            summary=_contact_summary(n_inbound, hours_since, connected, long_calls, pipeline, "Buying intent expressed."),
            actions=[
                {"action": "Call now — buying signals detected", "priority": "high", "icon": "fa-phone"},
                {"action": "Send quote or pricing if not already sent", "priority": "high", "icon": "fa-file-text"},
            ],
        )

    if not has_objection and hours_since < 24 and n_inbound >= 1:
        score = min(65, 40 + min(15, n_inbound * 2) + min(10, connected * 3))
        if velocity == "warming":
            score = min(70, score + 5)
        return _make_result(
            "warm", score,
            f"Active today with {n_inbound} text exchange{'s' if n_inbound != 1 else ''}.",
            should_respond=should_respond, eng=eng,
            summary=_contact_summary(n_inbound, hours_since, connected, long_calls, pipeline, "Active today, no objections."),
            actions=[
                {"action": "Keep momentum — follow up today", "priority": "high" if should_respond else "medium", "icon": "fa-comment"},
                {"action": "Ask a qualifying question to advance conversation", "priority": "medium", "icon": "fa-question-circle"},
            ],
        )

    if not has_objection and hours_since < 72 and n_inbound >= 2:
        score = min(58, 35 + min(12, n_inbound * 2) + min(10, connected * 3))
        if velocity == "warming":
            score = min(65, score + 5)
        elif velocity == "cooling":
            score = max(25, score - 5)
        return _make_result(
            "warm", score,
            f"Engaged in last {int(hours_since / 24) + 1} day(s), no objections raised.",
            should_respond=should_respond, eng=eng,
            summary=_contact_summary(n_inbound, hours_since, connected, long_calls, pipeline, "Engaged, no objections."),
            actions=[
                {"action": "Follow up — last reply was recent", "priority": "medium", "icon": "fa-comment"},
                {"action": "Try a call if multiple text exchanges", "priority": "medium" if (connected == 0 and n_inbound > 3) else "low", "icon": "fa-phone"},
            ],
        )

    if has_objection and hours_since < 168:
        score = min(30, 15 + min(10, connected * 3))
        return _make_result(
            "cool", score,
            "Has raised objections or expressed hesitation.",
            should_respond=False, eng=eng,
            summary=_contact_summary(n_inbound, hours_since, connected, long_calls, pipeline, "Objections raised — needs re-engagement."),
            actions=[
                {"action": "Re-engage with a different angle in 2–3 days", "priority": "low", "icon": "fa-refresh"},
                {"action": "Address specific objection with empathy", "priority": "medium", "icon": "fa-comments"},
            ],
        )

    if consecutive_unanswered >= 3 or (72 < hours_since < 720 and n_inbound >= 1):
        score = max(10, 20 + min(10, connected * 3))
        if velocity == "cooling":
            score = max(8, score - 5)
        days_inactive = int(hours_since / 24) if hours_since < 9999 else None
        return _make_result(
            "cool", score,
            f"Not replied in {days_inactive} day(s)." if days_inactive else "No recent engagement.",
            should_respond=False, eng=eng,
            summary=_contact_summary(n_inbound, hours_since, connected, long_calls, pipeline, "Gone quiet — follow-up needed."),
            actions=[
                {"action": f"Send check-in ({days_inactive}d since last reply)" if days_inactive else "Re-engage with check-in", "priority": "medium", "icon": "fa-envelope"},
            ],
        )

    if n_inbound == 0 and total_calls > 0:
        score = 18 + min(15, connected * 5)
        return _make_result(
            "cool", score,
            f"{total_calls} call attempt{'s' if total_calls != 1 else ''}, no text replies.",
            should_respond=False, eng=eng,
            summary=f"{total_calls} call attempt{'s' if total_calls != 1 else ''}, {connected} connected. No text engagement.",
            actions=[
                {"action": "Try intro SMS if not already sent", "priority": "medium", "icon": "fa-comment"},
                {"action": "Vary call timing if multiple unanswered", "priority": "low", "icon": "fa-phone"},
            ],
        )

    # Default: no engagement or inactive >30 days
    days_inactive = int(hours_since / 24) if hours_since < 9999 else None
    reason = f"No engagement in {days_inactive} days." if days_inactive else "No engagement recorded."
    return _make_result(
        "cold", 8,
        reason,
        should_respond=False, eng=eng,
        summary=_contact_summary(n_inbound, hours_since, connected, long_calls, pipeline, reason),
        actions=[
            {"action": "Re-engage with fresh intro if inactive 30+ days", "priority": "low", "icon": "fa-refresh"},
        ],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ BULK CONTEXT BUILDER — batch DB queries for many contacts ════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _gather_bulk_contexts(location_id, contact_ids):
    """
    Gather context for many contacts in batched DB queries (not 1 query per contact).
    Returns {contact_id: ctx_dict}.
    """
    if not contact_ids:
        return {}

    results = {cid: {
        "messages": [], "messages_with_time": [], "facts": [], "pipeline": None,
        "calls": {"total": 0, "agent_called": 0, "lead_called": 0, "answered": 0, "unanswered": 0, "long_calls": 0, "last_call": None},
        "tags": [], "narrative": None, "last_message_at": None, "timing": {},
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
            for r in rows:
                cid = r['contact_id']
                if cid in results and r['rn'] <= 30:
                    if r['rn'] == 1:
                        results[cid]['last_message_at'] = r['created_at']
                    role = "Lead" if r['message_type'] == 'lead' else "Bot"
                    results[cid]['messages'].append((r['created_at'], f"{role}: {r['message_text']}"))
            for cid in results:
                sorted_msgs = sorted(results[cid]['messages'], key=lambda x: x[0])
                results[cid]['messages_with_time'] = [
                    {"role": m[1].split(":")[0], "text": m[1].split(": ", 1)[1] if ": " in m[1] else m[1], "at": m[0]}
                    for m in sorted_msgs
                ]
                results[cid]['messages'] = [m[1] for m in sorted_msgs]
            for cid in results:
                if results[cid]['messages_with_time']:
                    results[cid]['timing'] = _compute_timing_signals(results[cid]['messages_with_time'])
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

        # ── Call history stats ──
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


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ BULK ANALYSIS — rules-based, no AI, runs in milliseconds ════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def bulk_analyze_and_cache(location_id, contact_ids):
    """
    Score many contacts using rule-based analysis and cache all results.
    No AI, no network calls. Runs in milliseconds. Zero cost.
    Returns count of successfully scored contacts.
    """
    if not contact_ids:
        return 0

    all_contexts = _gather_bulk_contexts(location_id, contact_ids)

    conn = get_db_connection()
    if not conn:
        return 0

    scored = 0
    now = datetime.utcnow()

    # Phase 1: Score all contacts in pure Python (no DB, no exceptions expected)
    rows_to_insert = []
    for cid in contact_ids:
        ctx = all_contexts.get(cid)
        if not ctx:
            continue
        try:
            result = _rules_score_contact(ctx)
            rows_to_insert.append((cid, location_id, json.dumps(result), now))
        except Exception as e:
            logger.error(f"[INTEL_RULES] Scoring failed for {cid}: {e}")

    if not rows_to_insert:
        return_db_connection(conn)
        return 0

    # Phase 2: Bulk UPSERT all scored results in one statement
    try:
        cur = conn.cursor()
        from psycopg2.extras import execute_values
        execute_values(
            cur,
            """INSERT INTO contact_intelligence (contact_id, location_id, analysis, analyzed_at)
               VALUES %s
               ON CONFLICT (contact_id) DO UPDATE SET
                   analysis = EXCLUDED.analysis,
                   location_id = EXCLUDED.location_id,
                   analyzed_at = EXCLUDED.analyzed_at""",
            rows_to_insert,
            template="(%s, %s, %s::jsonb, %s)",
            page_size=500,
        )
        conn.commit()
        scored = len(rows_to_insert)
        cur.close()
        logger.info(f"[INTEL_RULES] Scored {scored}/{len(contact_ids)} contacts for {location_id}")
    except Exception as e:
        logger.error(f"[INTEL_RULES] Bulk cache write failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)

    return scored


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ CACHE LAYER — Store + retrieve scoring results ══════════════════════════
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
                location_id TEXT NOT NULL,
                analysis JSONB NOT NULL,
                analyzed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    Return cached analysis if still fresh.

    TTL rules:
      hot   →  4 hours
      warm  → 12 hours
      cool  → 48 hours
      cold  → 168 hours (7 days)

    Message-invalidation: only when the LEAD sent a new message after the last
    analysis AND the cache is >1 hour old (bot outbound does not invalidate).
    """
    conn = get_db_connection()
    if not conn:
        return None

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ci.analysis, ci.analyzed_at,
                   lm.last_type
            FROM contact_intelligence ci
            LEFT JOIN LATERAL (
                SELECT message_type AS last_type
                FROM contact_messages
                WHERE contact_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            ) lm ON TRUE
            WHERE ci.contact_id = %s
        """, (contact_id, contact_id))
        row = cur.fetchone()
        cur.close()

        if not row:
            return None

        analyzed_at = row.get('analyzed_at')
        if not analyzed_at:
            return None

        if isinstance(analyzed_at, str):
            analyzed_at = datetime.fromisoformat(analyzed_at.replace('Z', '+00:00'))
        if hasattr(analyzed_at, 'tzinfo') and analyzed_at.tzinfo:
            analyzed_at = analyzed_at.replace(tzinfo=None)

        analysis = row.get('analysis')
        if isinstance(analysis, str):
            analysis = json.loads(analysis)

        age_hours = (datetime.utcnow() - analyzed_at).total_seconds() / 3600

        if last_message_at:
            lm = last_message_at
            if isinstance(lm, str):
                lm = datetime.fromisoformat(lm.replace('Z', '+00:00'))
            if hasattr(lm, 'tzinfo') and lm.tzinfo:
                lm = lm.replace(tzinfo=None)

            last_type = (row.get('last_type') or '').lower()
            lead_spoke = last_type in ('user', 'inbound', 'lead')

            if lm > analyzed_at and lead_spoke and age_hours >= 1.0:
                return None

        temperature = (analysis.get('temperature') or 'unknown').lower()
        ttl_hours = {'hot': 4, 'warm': 12, 'cool': 48, 'cold': 168}.get(temperature, 168)

        if age_hours > ttl_hours:
            return None

        return analysis

    except Exception as e:
        logger.debug(f"Cache read failed: {e}")
        return None
    finally:
        return_db_connection(conn)


def _save_analysis_cache(contact_id, location_id, analysis):
    """Save scoring results to cache."""
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
    Zero cost — reads DB only.

    Returns None for:
      - No cached data
      - Under-contract contacts (existing clients)
      - Neutral temperature

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

        if analysis.get("under_contract"):
            return None

        temp = analysis.get("temperature", "")
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


def get_bulk_cached_intelligence(location_id, contact_ids):
    """
    Fetch cached intelligence for multiple contacts in one DB query.
    Returns only contacts with fresh cache. Stale contacts appear as
    'uncached' so they get re-scored on next dialer load. Zero cost.

    Returns: {contact_id: {temperature, score, summary, temperature_reason}}
    """
    if not contact_ids:
        return {}

    conn = get_db_connection()
    if not conn:
        return {}

    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT ci.contact_id, ci.analysis, ci.analyzed_at,
                   lm.last_msg_at, lm.last_type
            FROM contact_intelligence ci
            LEFT JOIN LATERAL (
                SELECT MAX(cm.created_at) AS last_msg_at,
                       (SELECT message_type FROM contact_messages
                        WHERE contact_id = ci.contact_id
                        ORDER BY created_at DESC LIMIT 1) AS last_type
                FROM contact_messages cm
                WHERE cm.contact_id = ci.contact_id
            ) lm ON TRUE
            WHERE ci.location_id = %s
              AND ci.contact_id = ANY(%s)
              AND ci.analyzed_at > NOW() - INTERVAL '7 days'
        """, (location_id, contact_ids))

        _TTL = {'hot': 4, 'warm': 12, 'cool': 48, 'cold': 168}

        results = {}
        for row in cur.fetchall():
            cid = row['contact_id']
            analyzed_at = row['analyzed_at']
            last_msg_at = row.get('last_msg_at')
            last_type = (row.get('last_type') or '').lower()

            if hasattr(analyzed_at, 'tzinfo') and analyzed_at.tzinfo:
                analyzed_at = analyzed_at.replace(tzinfo=None)

            age_hours = (datetime.utcnow() - analyzed_at).total_seconds() / 3600

            analysis_raw = row['analysis']
            if isinstance(analysis_raw, str):
                try:
                    analysis_raw = json.loads(analysis_raw)
                except Exception:
                    continue

            temperature = (analysis_raw.get('temperature') or 'unknown').lower()
            ttl_hours = _TTL.get(temperature, 168)

            if age_hours > ttl_hours:
                continue

            if last_msg_at and analyzed_at:
                m = last_msg_at
                if hasattr(m, 'tzinfo') and m.tzinfo:
                    m = m.replace(tzinfo=None)
                lead_spoke = last_type in ('user', 'inbound', 'lead')
                if m > analyzed_at and lead_spoke and age_hours >= 1.0:
                    continue

            results[cid] = {
                "temperature": analysis_raw.get("temperature", "warm"),
                "score": analysis_raw.get("score", 50),
                "summary": analysis_raw.get("summary", ""),
                "temperature_reason": analysis_raw.get("temperature_reason", ""),
                "should_respond": analysis_raw.get("should_respond", False),
                "engagement_level": analysis_raw.get("engagement_level", 0),
                "under_contract": analysis_raw.get("under_contract", False),
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
    Score a batch of contacts that don't have fresh cache.
    Rules-based — instant, no AI cost.
    Returns list of {contact_id, temperature, score} for scored contacts.
    """
    if not contact_ids:
        return []

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

    logger.info(f"Batch scored {len(results)}/{len(need_analysis)} contacts for {location_id}")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ PUBLIC API — Called by /api/contact/<id>/intelligence ═══════════════════
# ═══════════════════════════════════════════════════════════════════════════════

# Ensure table exists on module load
_ensure_intelligence_table()


def _filter_real_facts(facts):
    """
    Filter out legacy message snippets from contact_facts.
    Real facts are short fragments like "Married, wife named Sarah".
    Message snippets are long, start with greetings, or contain ellipsis.
    """
    if not facts:
        return []
    clean = []
    for f in facts:
        if not f or len(f) < 4:
            continue
        # Skip obvious message snippets (outreach messages, bot text)
        if re.match(r'^(Hey |Hi |Hello |Mitch |Start with|No lead resp)', f, re.IGNORECASE):
            continue
        # Skip truncated messages (contain "...")
        if '...' in f:
            continue
        # Skip if too many words (real facts are short fragments)
        if len(f.split()) > 15:
            continue
        # Skip if it looks like a full outreach message (contains common bot patterns)
        lower = f.lower()
        if any(p in lower for p in ['autocorrect just', 'my dog just', 'my phone just',
                                     'i promise this is', 'quick question',
                                     'most folks', 'most people']):
            continue
        clean.append(f)
    return clean


def get_contact_intelligence(location_id, contact_id):
    """
    Get contact intelligence dossier.

    Flow:
    1. Gather all context (messages, facts, pipeline, calls, tags)
    2. Check cache — if fresh, return cached
    3. Cache miss → run rules scorer → cache result → return

    Returns dict with: summary, score, temperature, temperature_reason, actions,
                       facts, pipeline, narrative.
    """
    ctx = _gather_contact_context(location_id, contact_id)

    cached = _get_cached_analysis(contact_id, ctx.get("last_message_at"))
    if cached:
        logger.debug(f"Intelligence cache HIT for {contact_id}")
        cached["facts"] = _filter_real_facts(ctx["facts"])
        cached["pipeline"] = ctx["pipeline"]
        cached["narrative"] = ctx.get("narrative")
        raw_score = cached.get("score", 50)
        if isinstance(raw_score, (int, float)):
            cached["score"] = {"score": int(raw_score), "label": cached.get("temperature", "warm")}
        return cached

    result = _rules_score_contact(ctx)
    _save_analysis_cache(contact_id, location_id, result)

    # Extract real facts from conversation in background (no main pipeline impact)
    _extract_and_save_facts(contact_id, ctx)

    # Re-read facts after extraction (may have new ones)
    fresh_facts = ctx["facts"]
    try:
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT fact_text FROM contact_facts
                    WHERE contact_id = %s
                    ORDER BY created_at ASC
                """, (contact_id,))
                fresh_facts = [r['fact_text'] for r in cur.fetchall()]
                cur.close()
            finally:
                return_db_connection(conn)
    except Exception:
        pass

    return {
        "summary": result.get("summary"),
        "score": {"score": result.get("score", 50), "label": result.get("temperature", "warm")},
        "temperature": result.get("temperature", "warm"),
        "temperature_reason": result.get("temperature_reason", ""),
        "actions": result.get("actions", []),
        "facts": _filter_real_facts(fresh_facts),
        "pipeline": ctx["pipeline"],
        "narrative": ctx.get("narrative"),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ FACT EXTRACTION — Lightweight LLM extraction from conversation ══════════
# ═══════════════════════════════════════════════════════════════════════════════

_fact_client = None

def _get_fact_client():
    """Lazy-init xAI client for fact extraction."""
    global _fact_client
    if _fact_client is None:
        api_key = os.getenv("XAI_API_KEY")
        if api_key:
            from openai import OpenAI
            _fact_client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    return _fact_client


def _extract_and_save_facts(contact_id, ctx):
    """
    Extract discrete personal facts from conversation messages via LLM.
    Runs in background RQ — zero impact on main SMS pipeline latency.

    Extracts facts like: "Married, wife's name is Sarah", "Has 2 kids",
    "Works at FedEx", "Age 45", "Lives in Dallas TX", "Has term life through employer".

    Only extracts facts the LEAD said about themselves — never bot statements.
    Skips if no lead messages exist.
    """
    messages = ctx.get("messages", [])
    if not messages:
        return

    # Only proceed if there are lead messages (not just bot outreach)
    lead_msgs = [m for m in messages if m.startswith("Lead:")]
    if not lead_msgs:
        return

    # Check what facts already exist to avoid redundant LLM calls
    existing_facts = ctx.get("facts", [])

    # Build conversation text for LLM
    conversation_text = "\n".join(messages[-20:])  # Last 20 messages

    existing_str = "\n".join(f"- {f}" for f in existing_facts) if existing_facts else "None yet."

    prompt = f"""Read this insurance sales conversation and extract ONLY concrete personal facts that the LEAD revealed about themselves. Return one fact per line, short fragments only (max 10 words each).

ALREADY KNOWN FACTS (do NOT repeat these):
{existing_str}

CONVERSATION:
{conversation_text}

RULES:
- Only facts the LEAD said about themselves (never bot statements or questions)
- Personal details: name, age, marital status, spouse name, kids, job, employer, city/state
- Insurance details: current coverage, carrier, policy type, health conditions, budget
- Life details: hobbies, pets, retirement plans, mortgage, dependents
- Each fact must be a short fragment like: "Married, wife named Sarah" or "Works at FedEx" or "Has 2 kids ages 5 and 8"
- Do NOT include: street addresses, zip codes, phone numbers, email addresses
- Do NOT include messages, questions, or conversation snippets
- Do NOT include bot/agent statements
- If no personal facts were revealed by the lead, write ONLY the word: NONE

FACTS:"""

    client = _get_fact_client()
    if not client:
        return

    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast-non-reasoning",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.2,
            max_tokens=300,
            timeout=10.0,
        )
        raw = response.choices[0].message.content.strip()

        # Strip reasoning artifacts
        if "<thinking>" in raw:
            raw = re.sub(r'<thinking>.*?</thinking>', '', raw, flags=re.DOTALL).strip()

        if not raw or raw.upper().strip() == "NONE":
            return

        new_facts = []
        for line in raw.split("\n"):
            line = line.strip()
            # Remove bullet/numbering prefixes
            line = re.sub(r'^[-•*]\s*', '', line)
            line = re.sub(r'^\d+[.)]\s*', '', line)
            line = line.strip()
            if line and len(line) > 3 and line.upper() != "NONE" and len(line) < 120:
                new_facts.append(line)

        if new_facts:
            from memory import save_new_facts
            saved = save_new_facts(contact_id, new_facts)
            if saved > 0:
                logger.info(f"[FACTS] Extracted {saved} facts for {contact_id}: {new_facts}")

    except Exception as e:
        logger.warning(f"[FACTS] Extraction failed for {contact_id}: {e}")
