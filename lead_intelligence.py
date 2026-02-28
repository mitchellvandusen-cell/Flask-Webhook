# lead_intelligence.py - AI Intelligence Layer
# Phase 5: Smart summaries, next-best-action, and lead priority scoring.
# Designed to be cost-effective: rule-based where possible, micro-prompts where needed.

import logging
from datetime import datetime, timedelta
from db import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ LEAD PRIORITY SCORING (Rule-Based — Zero AI Cost) ════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_lead_score(location_id, contact_id):
    """
    Score 0-100 based on weighted signals.
    Entirely rule-based — zero API calls, zero cost, instant.

    Signals:
    - Recency of last interaction (+20 today, +10 this week, +5 this month)
    - Engagement level (+15 if they replied, +5 per conversation turn)
    - Pipeline stage (+25 if Quoted/Application, +15 if engaged, +5 if New)
    - Call history (+10 if connected call >2min, +5 if any call)
    - Tags (+15 for Hot Lead, -50 for Do Not Contact)
    """
    conn = get_db_connection()
    if not conn:
        return {"score": 50, "label": "unknown", "signals": []}

    score = 0
    signals = []

    try:
        cur = conn.cursor()

        # 1. Recency — when was the last interaction?
        try:
            cur.execute("""
                SELECT MAX(created_at) as last_activity
                FROM contact_messages
                WHERE contact_id = %s
            """, (contact_id,))
            row = cur.fetchone()
            if row and row.get('last_activity'):
                last = row['last_activity']
                if isinstance(last, str):
                    last = datetime.fromisoformat(last.replace('Z', '+00:00'))
                if hasattr(last, 'tzinfo') and last.tzinfo:
                    last = last.replace(tzinfo=None)
                age = datetime.utcnow() - last
                if age < timedelta(hours=24):
                    score += 20
                    signals.append("Active today (+20)")
                elif age < timedelta(days=7):
                    score += 10
                    signals.append("Active this week (+10)")
                elif age < timedelta(days=30):
                    score += 5
                    signals.append("Active this month (+5)")
        except Exception:
            pass

        # 2. Engagement — how many back-and-forth exchanges?
        try:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE message_type = 'lead') as lead_msgs,
                    COUNT(*) FILTER (WHERE message_type = 'assistant') as bot_msgs,
                    COUNT(*) as total
                FROM contact_messages
                WHERE contact_id = %s
            """, (contact_id,))
            row = cur.fetchone()
            if row:
                lead_msgs = row.get('lead_msgs', 0) or 0
                total = row.get('total', 0) or 0
                if lead_msgs > 0:
                    score += 15
                    signals.append(f"Lead replied ({lead_msgs} messages, +15)")
                if total > 4:
                    score += min(total, 10)
                    signals.append(f"Active thread ({total} messages, +{min(total, 10)})")
        except Exception:
            pass

        # 3. Pipeline stage (from synced GHL data)
        try:
            cur.execute("""
                SELECT stage_name, status, monetary_value
                FROM ghl_opportunities
                WHERE location_id = %s AND contact_id = %s
                ORDER BY updated_at_ghl DESC NULLS LAST
                LIMIT 1
            """, (location_id, contact_id))
            opp = cur.fetchone()
            if opp:
                stage = (opp.get('stage_name') or '').lower()
                status = (opp.get('status') or '').lower()
                value = opp.get('monetary_value') or 0

                if status == 'won':
                    score += 10
                    signals.append(f"Deal won (+10)")
                elif 'quot' in stage or 'application' in stage or 'submitted' in stage:
                    score += 25
                    signals.append(f"Pipeline: {opp['stage_name']} (+25)")
                elif 'engaged' in stage or 'contacted' in stage or 'warm' in stage:
                    score += 15
                    signals.append(f"Pipeline: {opp['stage_name']} (+15)")
                elif 'new' in stage or 'lead' in stage:
                    score += 5
                    signals.append(f"Pipeline: {opp['stage_name']} (+5)")

                if value and value > 0:
                    score += 5
                    signals.append(f"Deal value: ${value:,.0f} (+5)")
        except Exception:
            pass

        # 4. Call history
        try:
            cur.execute("""
                SELECT COUNT(*) as total_calls,
                       COUNT(*) FILTER (WHERE status = 'completed' AND duration > 120) as long_calls,
                       COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) as connected_calls
                FROM call_history
                WHERE location_id = %s AND contact_id = %s
            """, (location_id, contact_id))
            calls = cur.fetchone()
            if calls:
                if (calls.get('long_calls') or 0) > 0:
                    score += 10
                    signals.append(f"Connected call >2min (+10)")
                elif (calls.get('connected_calls') or 0) > 0:
                    score += 5
                    signals.append(f"Connected call (+5)")
                elif (calls.get('total_calls') or 0) > 0:
                    score += 2
                    signals.append(f"Call attempted (+2)")
        except Exception:
            pass

        # 5. Tags (from contact_cache)
        try:
            cur.execute("""
                SELECT tags FROM contact_cache
                WHERE location_id = %s AND contact_id = %s
            """, (location_id, contact_id))
            tag_row = cur.fetchone()
            if tag_row and tag_row.get('tags'):
                tags = tag_row['tags']
                if isinstance(tags, str):
                    import json
                    tags = json.loads(tags)
                tag_names = [t.lower() if isinstance(t, str) else (t.get('name', '') if isinstance(t, dict) else '').lower() for t in tags]

                for t in tag_names:
                    if 'hot' in t:
                        score += 15
                        signals.append("Tag: Hot Lead (+15)")
                    elif 'do not' in t or 'dnc' in t or 'block' in t:
                        score -= 50
                        signals.append("Tag: Do Not Contact (-50)")
                    elif 'vip' in t or 'priority' in t:
                        score += 10
                        signals.append(f"Tag: {t} (+10)")
        except Exception:
            pass

        cur.close()

        # Clamp score 0-100
        score = max(0, min(100, score))

        # Label
        if score >= 75:
            label = "hot"
        elif score >= 50:
            label = "warm"
        elif score >= 25:
            label = "cool"
        else:
            label = "cold"

        return {"score": score, "label": label, "signals": signals}

    except Exception as e:
        logger.error(f"Lead score calculation failed: {e}")
        return {"score": 50, "label": "unknown", "signals": []}
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ NEXT-BEST-ACTION ENGINE (Rule-Based — Zero AI Cost) ═════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def get_next_best_actions(location_id, contact_id):
    """
    Rule-based next-best-action recommendations.
    Runs entirely in Python — zero API calls, zero cost, instant.

    Returns list of action dicts: [{action, reason, priority, icon}]
    """
    conn = get_db_connection()
    if not conn:
        return []

    actions = []

    try:
        cur = conn.cursor()

        # Get message history stats
        lead_msgs = 0
        bot_msgs = 0
        last_lead_at = None
        last_bot_at = None
        last_lead_text = ""

        try:
            cur.execute("""
                SELECT message_type, message_text, created_at
                FROM contact_messages
                WHERE contact_id = %s
                ORDER BY created_at DESC
                LIMIT 20
            """, (contact_id,))
            msgs = cur.fetchall()
            for m in msgs:
                mt = m.get('message_type', '')
                if mt == 'lead':
                    lead_msgs += 1
                    if not last_lead_at:
                        last_lead_at = m.get('created_at')
                        last_lead_text = m.get('message_text', '')
                elif mt == 'assistant':
                    bot_msgs += 1
                    if not last_bot_at:
                        last_bot_at = m.get('created_at')
        except Exception:
            pass

        now = datetime.utcnow()

        # Rule 1: Lead asked a question — reply ASAP
        if last_lead_text and last_lead_at:
            if isinstance(last_lead_at, str):
                try:
                    last_lead_at = datetime.fromisoformat(last_lead_at.replace('Z', '+00:00'))
                except Exception:
                    last_lead_at = None
            if last_lead_at:
                if hasattr(last_lead_at, 'tzinfo') and last_lead_at.tzinfo:
                    last_lead_at = last_lead_at.replace(tzinfo=None)
                if last_lead_at > (last_bot_at.replace(tzinfo=None) if last_bot_at and hasattr(last_bot_at, 'tzinfo') and last_bot_at.tzinfo else last_bot_at or datetime(2000, 1, 1)):
                    if '?' in last_lead_text:
                        actions.append({
                            "action": "Reply ASAP — lead asked a question",
                            "reason": f"Last message: \"{last_lead_text[:60]}...\"",
                            "priority": "high",
                            "icon": "fa-solid fa-reply",
                        })
                    else:
                        age = now - last_lead_at
                        if age < timedelta(hours=2):
                            actions.append({
                                "action": "Reply soon — lead is engaged",
                                "reason": f"Lead messaged {int(age.total_seconds() // 60)} minutes ago",
                                "priority": "high",
                                "icon": "fa-solid fa-bolt",
                            })

        # Rule 2: No response in 24h + new lead
        if last_bot_at and not last_lead_at:
            if isinstance(last_bot_at, str):
                try:
                    last_bot_at = datetime.fromisoformat(last_bot_at.replace('Z', '+00:00'))
                except Exception:
                    last_bot_at = None
            if last_bot_at:
                if hasattr(last_bot_at, 'tzinfo') and last_bot_at.tzinfo:
                    last_bot_at = last_bot_at.replace(tzinfo=None)
                age = now - last_bot_at
                if age > timedelta(hours=24) and bot_msgs <= 2:
                    actions.append({
                        "action": "Send follow-up SMS",
                        "reason": f"No response in {age.days} days — initial outreach may have been missed",
                        "priority": "medium",
                        "icon": "fa-solid fa-paper-plane",
                    })

        # Rule 3: Call stats analysis
        try:
            cur.execute("""
                SELECT COUNT(*) as total,
                       COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) as connected,
                       MAX(created_at) as last_call
                FROM call_history
                WHERE location_id = %s AND contact_id = %s
            """, (location_id, contact_id))
            calls = cur.fetchone()
            total_calls = calls.get('total', 0) or 0
            connected_calls = calls.get('connected', 0) or 0

            if total_calls >= 3 and connected_calls == 0:
                actions.append({
                    "action": "Try different time of day",
                    "reason": f"{total_calls} calls, 0 connects — try morning or evening",
                    "priority": "medium",
                    "icon": "fa-solid fa-clock",
                })

            if connected_calls > 0 and lead_msgs == 0:
                actions.append({
                    "action": "Send SMS after connected call",
                    "reason": "Connected by phone but no text conversation yet",
                    "priority": "medium",
                    "icon": "fa-solid fa-message",
                })
        except Exception:
            pass

        # Rule 4: Pipeline-based actions
        try:
            cur.execute("""
                SELECT stage_name, status, updated_at_ghl
                FROM ghl_opportunities
                WHERE location_id = %s AND contact_id = %s
                ORDER BY updated_at_ghl DESC NULLS LAST
                LIMIT 1
            """, (location_id, contact_id))
            opp = cur.fetchone()
            if opp:
                stage = (opp.get('stage_name') or '').lower()
                updated = opp.get('updated_at_ghl')

                if updated:
                    try:
                        if isinstance(updated, str):
                            updated = datetime.fromisoformat(updated.replace('Z', '+00:00'))
                        if hasattr(updated, 'tzinfo') and updated.tzinfo:
                            updated = updated.replace(tzinfo=None)
                        stale = now - updated
                    except Exception:
                        stale = timedelta(days=0)
                else:
                    stale = timedelta(days=0)

                if ('quot' in stage or 'proposal' in stage) and stale > timedelta(hours=48):
                    actions.append({
                        "action": "Follow up on quote",
                        "reason": f"Quote sent {stale.days} days ago — no update since",
                        "priority": "high",
                        "icon": "fa-solid fa-file-invoice-dollar",
                    })

                if ('application' in stage or 'submitted' in stage) and stale > timedelta(days=3):
                    actions.append({
                        "action": "Check application status",
                        "reason": f"Application submitted {stale.days} days ago",
                        "priority": "medium",
                        "icon": "fa-solid fa-clipboard-check",
                    })
        except Exception:
            pass

        # Rule 5: Tag-based actions
        try:
            cur.execute("""
                SELECT tags FROM contact_cache
                WHERE location_id = %s AND contact_id = %s
            """, (location_id, contact_id))
            tag_row = cur.fetchone()
            if tag_row and tag_row.get('tags'):
                tags = tag_row['tags']
                if isinstance(tags, str):
                    import json
                    tags = json.loads(tags)
                tag_names = [t.lower() if isinstance(t, str) else (t.get('name', '') if isinstance(t, dict) else '').lower() for t in tags]

                if any('hot' in t for t in tag_names):
                    # Check if we called today
                    cur.execute("""
                        SELECT COUNT(*) as cnt FROM call_history
                        WHERE location_id = %s AND contact_id = %s
                          AND created_at > NOW() - INTERVAL '24 hours'
                    """, (location_id, contact_id))
                    today_calls = (cur.fetchone() or {}).get('cnt', 0)
                    if not today_calls:
                        actions.append({
                            "action": "Priority dial — hot lead",
                            "reason": "Tagged as hot lead, no call today",
                            "priority": "high",
                            "icon": "fa-solid fa-fire",
                        })
        except Exception:
            pass

        cur.close()

        # Sort by priority
        priority_order = {"high": 0, "medium": 1, "low": 2}
        actions.sort(key=lambda a: priority_order.get(a.get('priority', 'low'), 3))

        return actions[:5]  # Max 5 actions

    except Exception as e:
        logger.error(f"Next-best-action failed: {e}")
        return []
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ SMART CONTACT SUMMARY (On-Demand, Cached via contact_narratives) ════════
# ═══════════════════════════════════════════════════════════════════════════════

def get_contact_intelligence(location_id, contact_id):
    """
    Get full intelligence dossier for a contact.
    Combines: lead score + next-best-actions + cached narrative + known facts + pipeline.
    All data is local or cached — extremely fast.
    """
    result = {
        "score": calculate_lead_score(location_id, contact_id),
        "actions": get_next_best_actions(location_id, contact_id),
        "narrative": None,
        "facts": [],
        "pipeline": None,
    }

    conn = get_db_connection()
    if not conn:
        return result

    try:
        cur = conn.cursor()

        # Narrative (from memory.py's contact_narratives)
        try:
            cur.execute("""
                SELECT narrative FROM contact_narratives
                WHERE contact_id = %s
                ORDER BY updated_at DESC LIMIT 1
            """, (contact_id,))
            row = cur.fetchone()
            if row:
                result["narrative"] = row.get('narrative')
        except Exception:
            pass

        # Known facts
        try:
            cur.execute("""
                SELECT fact_text FROM contact_facts
                WHERE contact_id = %s
                ORDER BY created_at ASC
            """, (contact_id,))
            result["facts"] = [r['fact_text'] for r in cur.fetchall()]
        except Exception:
            pass

        # Pipeline stage
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
                result["pipeline"] = dict(opp)
        except Exception:
            pass

        cur.close()

    except Exception as e:
        logger.error(f"Contact intelligence failed: {e}")
    finally:
        return_db_connection(conn)

    return result
