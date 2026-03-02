# blueprints/demo.py — Demo chatbot interface and session management
#
# Routes:
#   GET  /demo-chat                 — Demo chatbot page (runs janitor first)
#   POST /demo/init                 — Initialize or resume a demo session
#   POST /demo/reset                — Reset session to fresh opener
#   POST /api/demo/reset            — Simple reset (returns just the opener text)
#   POST /demo/chat                 — Send message → receive AI reply
#   GET  /get-logs                  — Fetch demo conversation logs + narrative
#   GET  /download-transcript       — Download demo conversation as .txt file
#   GET  /api/fetch-calendars       — Fetch GHL calendars for the current user

import uuid
import logging
import re
from datetime import datetime

import requests
from flask import Blueprint, request, render_template, make_response
from flask import jsonify as flask_jsonify
from flask_login import current_user
from psycopg2.extras import RealDictCursor

from extensions import YOUR_DOMAIN, safe_jsonify, get_client
from db import get_db_connection, return_db_connection
from memory import get_known_facts, get_narrative
from prompt import CORE_UNIFIED_MINDSET, DEMO_OPENER_ADDITIONAL_INSTRUCTIONS, build_system_prompt
from llm_caller import generate_clean_reply
from utils import clean_ai_reply
from individual_profile import build_comprehensive_profile

logger = logging.getLogger(__name__)

demo_bp = Blueprint('demo', __name__)


# ── Private helpers ───────────────────────────────────────────────────────────

def _generate_demo_opener() -> str:
    """Generate a unique demo opener via the LLM, with a deterministic fallback."""
    _fallback = (
        "Quick question are you still with that life insurance plan you mentioned before? "
        "There's some new living benefits people have been asking me about and I wanted to "
        "make sure yours doesnt just pay out when you're dead."
    )
    client = get_client()
    if not client:
        return _fallback
    try:
        system_content = (
            CORE_UNIFIED_MINDSET.format(bot_first_name="DEMOGROKBOT")
            + "\n\n"
            + DEMO_OPENER_ADDITIONAL_INSTRUCTIONS
        )
        cleaned = generate_clean_reply(
            client=client,
            system_prompt=system_content,
            user_message="Generate unique opener.",
            bot_name="DEMOGROKBOT",
            max_tokens=130,
            temperature=0.8,
        )
        if not cleaned:
            logger.error("OPENER: LLM could not produce clean reply. Using fallback.")
            return _fallback

        cleaned = cleaned.replace('"', '')
        cleaned = clean_ai_reply(cleaned)

        if len(cleaned) < 10 or not any(c.isalpha() for c in cleaned):
            logger.error(f"OPENER BLOCKED LOW-QUALITY: '{cleaned}' — using fallback")
            return _fallback

        return cleaned
    except Exception as e:
        logger.error(f"Demo opener failed: {e}")
        return _fallback


def _run_demo_janitor():
    """Delete demo conversation data older than 30 minutes to keep the DB light."""
    conn = get_db_connection()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            DELETE FROM contact_messages
            WHERE contact_id LIKE 'demo_%%'
              AND created_at < NOW() - INTERVAL '30 minutes'
        """)
        cur.execute("""
            DELETE FROM contact_facts
            WHERE contact_id LIKE 'demo_%%'
              AND created_at < NOW() - INTERVAL '30 minutes'
        """)
        cur.execute("""
            DELETE FROM contact_narratives
            WHERE contact_id LIKE 'demo_%%'
              AND updated_at < NOW() - INTERVAL '30 minutes'
        """)
        conn.commit()
    except Exception as e:
        logger.error(f"Janitor cleanup failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        try:
            cur.close()
        except Exception:
            pass
        return_db_connection(conn)


# ── Routes ────────────────────────────────────────────────────────────────────

@demo_bp.route("/demo-chat")
def demo_chat():
    try:
        _run_demo_janitor()
    except Exception as e:
        logger.error(f"Demo janitor failed: {e}")
    return render_template('demo.html')


@demo_bp.route("/demo/init", methods=["POST"])
def demo_init_api():
    """Initialize or resume a demo session."""
    data       = request.get_json() or {}
    session_id = data.get("session_id") or str(uuid.uuid4())
    contact_id = f"demo_{session_id}"

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 503

    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) as cnt FROM contact_messages WHERE contact_id = %s",
            (contact_id,)
        )
        count = cur.fetchone()['cnt']

        if count == 0:
            opener = _generate_demo_opener()
            cur.execute("""
                INSERT INTO contact_messages (contact_id, message_type, message_text)
                VALUES (%s, 'assistant', %s)
            """, (contact_id, opener))
            conn.commit()
            cur.close()
            return_db_connection(conn)
            return flask_jsonify({"contact_id": contact_id, "opener": opener, "status": "new"})

        cur.execute("""
            SELECT message_type, message_text
            FROM contact_messages
            WHERE contact_id = %s
            ORDER BY created_at ASC
        """, (contact_id,))

        history = [
            {
                "role": "bot" if r['message_type'] == 'assistant' else "user",
                "content": r['message_text']
            }
            for r in cur.fetchall()
        ]
        cur.close()
        return_db_connection(conn)
        return flask_jsonify({"contact_id": contact_id, "history": history, "status": "existing"})

    except Exception as e:
        logger.error(f"Demo init error: {e}")
        return flask_jsonify({"error": str(e)}), 500


@demo_bp.route("/demo/reset", methods=["POST"])
def demo_reset_api():
    """Clear the current demo session and start fresh with a new opener."""
    data   = request.get_json() or {}
    old_id = data.get("contact_id")

    conn = get_db_connection()
    if conn and old_id and old_id.startswith("demo_"):
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM contact_messages  WHERE contact_id = %s", (old_id,))
            cur.execute("DELETE FROM contact_facts     WHERE contact_id = %s", (old_id,))
            cur.execute("DELETE FROM contact_narratives WHERE contact_id = %s", (old_id,))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.error(f"Demo reset DELETE failed for {old_id}: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                return_db_connection(conn)

    new_id = f"demo_{uuid.uuid4()}"
    opener = _generate_demo_opener()

    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO contact_messages (contact_id, message_type, message_text)
            VALUES (%s, 'assistant', %s)
        """, (new_id, opener))
        conn.commit()
        cur.close()
        return_db_connection(conn)

    return flask_jsonify({"contact_id": new_id, "opener": opener})


@demo_bp.route("/api/demo/reset", methods=["POST"])
def api_demo_reset():
    """Simple legacy reset endpoint — returns just the opener message."""
    opener = _generate_demo_opener()
    return flask_jsonify({"message": opener})


@demo_bp.route("/demo/chat", methods=["POST"])
def demo_chat_api():
    """
    Send a user message to the demo bot and receive an AI reply.
    No polling — synchronous request/response like a standard chat API.
    """
    data       = request.get_json()
    contact_id = data.get("contact_id")
    message    = data.get("message", "").strip()

    if not contact_id or not contact_id.startswith("demo_"):
        return flask_jsonify({"error": "Invalid session"}), 400
    if not message:
        return flask_jsonify({"error": "Empty message"}), 400

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 503

    try:
        cur = conn.cursor()

        # Save user message — ON CONFLICT DO NOTHING prevents crash on duplicate text
        cur.execute("""
            INSERT INTO contact_messages (contact_id, message_type, message_text)
            VALUES (%s, 'lead', %s)
            ON CONFLICT DO NOTHING
        """, (contact_id, message))
        conn.commit()

        # Load conversation history (last 16 messages)
        cur.execute("""
            SELECT message_type, message_text
            FROM contact_messages
            WHERE contact_id = %s
            ORDER BY created_at DESC
            LIMIT 16
        """, (contact_id,))

        rows = cur.fetchall()
        recent_exchanges = [
            {"role": "lead" if r['message_type'] == 'lead' else "assistant",
             "text": r['message_text']}
            for r in reversed(rows)
        ]

        cur.close()
        return_db_connection(conn)

        # Strategic sales intelligence layer
        from sales_director import generate_strategic_directive
        director_output = generate_strategic_directive(
            contact_id=contact_id,
            message=message,
            first_name="Demo User",
            age=None,
            address=None
        )

        if "Silence required" in director_output["tactical_narrative"]:
            return flask_jsonify({"reply": "", "stage": "closed"})

        calendar_slots = ""
        if director_output["stage"] == "booking":
            calendar_slots = "Tomorrow at 2:00 PM, Tomorrow at 4:30 PM, Friday at 10:00 AM"

        system_prompt = build_system_prompt(
            bot_first_name="Grok",
            timezone="America/Chicago",
            profile_str=director_output["profile_str"],
            tactical_narrative=director_output["tactical_narrative"],
            known_facts=director_output["known_facts"],
            story_narrative=director_output["story_narrative"],
            stage=director_output["stage"],
            recent_exchanges=recent_exchanges[-8:],
            message=message,
            calendar_slots=calendar_slots,
            context_nudge="",
            lead_type="default"
        )

        system_prompt = system_prompt.replace("{bot_first_name}", "GrokBot")
        system_prompt += (
            "\n\nDEMO IDENTITY RULE: If someone asks who you are, who they are talking to, "
            "or what this is, you MUST respond with something like: "
            "'This is GrokBot. I'm an independent life insurance agent. I'm currently in "
            "demo mode, in production I'll identify as whatever name you assign me in your "
            "dashboard.' Then follow up with a question to keep the conversation going. "
            "Do not dodge identity questions. Do not deflect. Answer directly then ask a question."
        )

        grok_messages = [{"role": "system", "content": system_prompt}]
        for msg in recent_exchanges[-8:]:
            role = "user" if msg["role"] == "lead" else "assistant"
            grok_messages.append({"role": role, "content": msg["text"]})
        grok_messages.append({"role": "user", "content": message})

        client = get_client()
        reply  = generate_clean_reply(
            client=client,
            full_messages=grok_messages,
            bot_name="GrokBot",
        )

        if not reply:
            logger.error("DEMO: LLM could not produce clean reply. Using fallback.")
            reply = "What's your main concern about coverage right now?"

        reply = reply.replace("—", ",").replace("–", ",").strip()

        if len(reply) < 5 or not any(c.isalpha() for c in reply):
            logger.error(f"DEMO BLOCKED LOW-QUALITY MESSAGE: '{reply}' — using fallback")
            reply = "What's your main concern about coverage right now?"

        # Save bot reply — crash-proof
        conn = get_db_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO contact_messages (contact_id, message_type, message_text)
                VALUES (%s, 'assistant', %s)
                ON CONFLICT DO NOTHING
            """, (contact_id, reply))
            conn.commit()
            cur.close()
            return_db_connection(conn)

        return flask_jsonify({"reply": reply, "stage": director_output["stage"]})

    except Exception as e:
        logger.error(f"Demo chat error: {e}", exc_info=True)
        return flask_jsonify({
            "reply": "I hear you. Could you clarify that last part?",
            "error": str(e)
        }), 200


@demo_bp.route("/get-logs", methods=["GET"])
def get_logs():
    """Fetch demo conversation messages and AI-generated narrative."""
    contact_id = request.args.get("contact_id")
    if not contact_id:
        return flask_jsonify({"logs": []})

    if not contact_id.startswith(('demo_', 'test_')):
        return flask_jsonify({"logs": []})

    db_conn = get_db_connection()
    if not db_conn:
        return flask_jsonify({"logs": []})

    logs = []
    try:
        cur = db_conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
            SELECT message_type, message_text, created_at
            FROM contact_messages
            WHERE contact_id = %s
            ORDER BY created_at ASC
        """, (contact_id,))

        for r in cur.fetchall():
            ts   = r['created_at'].isoformat() if hasattr(r['created_at'], 'isoformat') else str(r['created_at'])
            role = "bot" if r['message_type'] in ['assistant', 'bot'] else "lead"
            logs.append({
                "role":      role,
                "type":      "Bot Message" if role == "bot" else "Lead Message",
                "content":   r['message_text'],
                "timestamp": ts,
            })

        facts     = get_known_facts(contact_id)
        narrative = get_narrative(contact_id)

        if not narrative and facts:
            try:
                facts_text   = " ".join(facts).lower()
                first_name   = None
                age          = None
                name_match   = re.search(r"first name: (\w+)", facts_text, re.IGNORECASE)
                if name_match:
                    first_name = name_match.group(1).capitalize()
                age_match = re.search(r"age: (\d+)", facts_text)
                if age_match:
                    age = age_match.group(1)

                rebuilt   = build_comprehensive_profile(
                    story_narrative="",
                    known_facts=facts,
                    first_name=first_name,
                    age=age
                )
                narrative = str(rebuilt[0]) if isinstance(rebuilt, tuple) else str(rebuilt)
            except Exception as e:
                logger.warning(f"Profile rebuild failed: {e}")

        if narrative:
            logs.append({
                "timestamp": datetime.now().isoformat(),
                "type":      "Full Human Identity Narrative",
                "content":   narrative,
            })

        return safe_jsonify({"logs": logs})

    except Exception as e:
        logger.error(f"get_logs error: {e}")
        return flask_jsonify({"logs": []})
    finally:
        cur.close()
        return_db_connection(db_conn)


@demo_bp.route("/download-transcript", methods=["GET"])
def download_transcript():
    """Download a conversation transcript as a plain-text file."""
    contact_id = request.args.get("contact_id")
    if not contact_id:
        return flask_jsonify({"error": "Missing contact_id parameter"}), 400

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "Database unavailable"}), 500

    try:
        cur     = conn.cursor(cursor_factory=RealDictCursor)
        allowed = False
        location_id = None
        is_demo     = contact_id.startswith(('demo_', 'test_'))

        if is_demo:
            allowed     = True
            location_id = contact_id
        else:
            if not current_user.is_authenticated:
                return flask_jsonify({"error": "Please log in to download real transcripts"}), 401

            if current_user.role == 'agency_owner':
                cur.execute("""
                    SELECT location_id FROM subscribers
                    WHERE location_id = %s AND parent_agency_email = %s
                    LIMIT 1
                """, (contact_id, current_user.email))
                row = cur.fetchone()
                if row:
                    allowed     = True
                    location_id = row['location_id']
            else:
                if contact_id == current_user.location_id:
                    allowed     = True
                    location_id = current_user.location_id

        if not allowed:
            return flask_jsonify({"error": "You do not have permission to download this transcript"}), 403

        cur.execute("""
            SELECT message_type, message_text, created_at
            FROM contact_messages
            WHERE contact_id = %s
            ORDER BY created_at ASC
        """, (contact_id,))
        messages  = cur.fetchall()
        facts     = get_known_facts(contact_id)
        narrative = get_narrative(contact_id)

        lines = [
            "INSURANCEGROKBOT CONVERSATION TRANSCRIPT",
            "=" * 60,
            f"Contact ID:       {contact_id}",
            f"Downloaded by:    {'Anonymous (Demo)' if is_demo else current_user.email}",
            f"Date:             {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Location ID:      {location_id or '—'}",
            "",
        ]

        for msg in messages:
            role = "BOT" if msg['message_type'] in ['assistant', 'bot'] else "LEAD"
            ts   = msg['created_at'].strftime('%H:%M:%S') if hasattr(msg['created_at'], 'strftime') else ''
            lines.append(f"[{ts}] {role}: {msg['message_text']}")

        if narrative:
            lines += ["", "─" * 60, "AI NARRATIVE SUMMARY", "─" * 60, narrative]
        if facts:
            lines += ["", "─" * 60, "EXTRACTED FACTS", "─" * 60]
            lines.extend(f"• {f}" for f in facts)

        transcript = "\n".join(lines)
        filename   = (f"InsuranceGrokBot_transcript_{contact_id}_"
                      f"{datetime.now().strftime('%Y%m%d_%H%M')}.txt")
        response   = make_response(transcript)
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        response.headers["Content-Type"]        = "text/plain; charset=utf-8"
        return response

    except Exception as e:
        logger.error(f"Transcript download error for {contact_id}: {e}", exc_info=True)
        return flask_jsonify({"error": "Failed to generate transcript"}), 500
    finally:
        if 'cur' in locals():
            cur.close()
        if conn:
            return_db_connection(conn)
