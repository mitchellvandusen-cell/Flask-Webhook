# support_bot.py — AI support bot logic: diagnostics, ticket creation, sanitizer
#
# Provides:
#   run_support_diagnostics(identifier)  — READ-ONLY account health check
#   create_support_ticket(...)           — Inserts into support_tickets table
#   sanitize_support_reply(text)         — Strips forbidden terms from AI output
#   extract_email(text, history)         — Finds email in message or history
#   extract_ticket_tag(text)             — Parses [TICKET:...] from AI response
#   extract_options(text)                — Parses [OPTIONS:...] from AI response
#   extract_redirect(text)               — Parses [REDIRECT:...] from AI response
#   handle_quick_action(message)         — Handles button presses without AI
#   support_rate_limited(ip)             — Redis rate limiting per IP

import os
import re
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ── Forbidden terms sanitizer ────────────────────────────────────────────────

_FORBIDDEN_PATTERNS = [
    (re.compile(r'\bTwilio\b', re.IGNORECASE), 'our phone system'),
    (re.compile(r'\bsub[\-\s]?account\b', re.IGNORECASE), 'your account'),
    (re.compile(r'\bTrust\s*Hub\b', re.IGNORECASE), 'carrier registration'),
    (re.compile(r'\bTrust\s*Product\b', re.IGNORECASE), 'registration'),
    (re.compile(r'\bA2P\s*10DLC\b', re.IGNORECASE), 'text messaging registration'),
    (re.compile(r'\bA2P\b', re.IGNORECASE), 'text messaging'),
    (re.compile(r'\b10DLC\b', re.IGNORECASE), 'carrier registration'),
    (re.compile(r'\bVoice\s*Integrity\b', re.IGNORECASE), 'spam protection'),
    (re.compile(r'\bCNAM\b'), 'caller ID'),
    (re.compile(r'\bSecondary\s*Customer\s*Profile\b', re.IGNORECASE), 'business profile'),
    (re.compile(r'\bEndUser\b', re.IGNORECASE), 'business details'),
    (re.compile(r'\bEntityAssignment\b', re.IGNORECASE), 'connection'),
    (re.compile(r'\bChannelEndpoint\b', re.IGNORECASE), 'number assignment'),
    (re.compile(r'\bxAI\b', re.IGNORECASE), 'our AI engine'),
    (re.compile(r'\bGrok\b', re.IGNORECASE), 'our AI'),
    (re.compile(r'\bOpenAI\b', re.IGNORECASE), 'our AI engine'),
    (re.compile(r'\bRedis\b', re.IGNORECASE), 'our servers'),
    (re.compile(r'\bPostgreSQL\b', re.IGNORECASE), 'our database'),
    (re.compile(r'\bPostgres\b', re.IGNORECASE), 'our database'),
    (re.compile(r'\bFlask\b', re.IGNORECASE), 'our platform'),
    (re.compile(r'\bGunicorn\b', re.IGNORECASE), 'our servers'),
    (re.compile(r'\bRQ\b'), 'our processing system'),
    (re.compile(r'\bworker\b', re.IGNORECASE), 'our processing system'),
    (re.compile(r'\bwebhook\b', re.IGNORECASE), 'notification'),
    (re.compile(r'\bOAuth\b', re.IGNORECASE), 'CRM connection'),
    (re.compile(r'\bAPI endpoint\b', re.IGNORECASE), 'the connection'),
    (re.compile(r'\bJSON\b', re.IGNORECASE), 'data'),
    (re.compile(r'\blocation_id\b', re.IGNORECASE), 'account ID'),
    (re.compile(r'\bpsycopg2\b', re.IGNORECASE), 'our system'),
    (re.compile(r'\bAlembic\b', re.IGNORECASE), 'our system'),
    (re.compile(r'\bmigration\b', re.IGNORECASE), 'update'),
    (re.compile(r'\bconnection pool\b', re.IGNORECASE), 'our system'),
    (re.compile(r'\bpolicy_sid\b', re.IGNORECASE), 'policy'),
    (re.compile(r'\b[A-Z]{2}[a-f0-9]{32}\b'), ''),  # strip any Twilio SIDs
    (re.compile(r'\.py\b'), ''),  # strip any .py file references
]


def sanitize_support_reply(text: str) -> str:
    """Strip forbidden technical terms from AI output."""
    if not text:
        return text
    for pattern, replacement in _FORBIDDEN_PATTERNS:
        text = pattern.sub(replacement, text)
    # Clean up double spaces from replacements
    text = re.sub(r'  +', ' ', text).strip()
    return text


# ── Email extraction ─────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')


def extract_email(text: str, history: list = None) -> str | None:
    """Find an email address in the current message or recent history."""
    # Check current message first
    match = _EMAIL_RE.search(text)
    if match:
        return match.group(0).lower()

    # Check recent history (last 5 messages from user)
    if history:
        for msg in reversed(history[-10:]):
            if msg.get("role") == "user":
                match = _EMAIL_RE.search(msg.get("content", ""))
                if match:
                    return match.group(0).lower()
    return None


# ── Consent detection ────────────────────────────────────────────────────────

def has_consent(history: list) -> bool:
    """Check if user has consented to account lookup in conversation history."""
    if not history:
        return False
    for msg in history:
        content = (msg.get("content") or "").strip().lower()
        if msg.get("role") == "user" and content in ("yes", "consent_yes", "yeah", "sure", "go ahead", "yes please"):
            return True
    return False


# ── Diagnostics ──────────────────────────────────────────────────────────────

def run_support_diagnostics(identifier: str) -> dict:
    """Run READ-ONLY diagnostics on a customer account.

    Args:
        identifier: Email address or location ID.

    Returns:
        Plain-English diagnostic context dict for system prompt injection.
    """
    from db_legacy import get_db_connection, return_db_connection, get_webhook_logs

    result = {"not_found": False}
    conn = None

    try:
        conn = get_db_connection()
        if not conn:
            return {"not_found": True, "recommendation": "Our system is temporarily busy. Try again in a moment."}

        cur = conn.cursor()

        # Determine if identifier is email or location_id
        is_email = "@" in identifier
        if is_email:
            cur.execute(
                "SELECT location_id, email, subscription_tier, stripe_customer_id, "
                "access_token, token_expires_at, bot_first_name, calendar_id, "
                "timezone, password_hash, voice_config, sms_send_via "
                "FROM subscribers WHERE LOWER(email) = %s",
                (identifier.lower(),)
            )
        else:
            cur.execute(
                "SELECT location_id, email, subscription_tier, stripe_customer_id, "
                "access_token, token_expires_at, bot_first_name, calendar_id, "
                "timezone, password_hash, voice_config, sms_send_via "
                "FROM subscribers WHERE location_id = %s",
                (identifier,)
            )

        row = cur.fetchone()
        if not row:
            return {"not_found": True}

        location_id = row["location_id"] or ""
        email = row["email"] or ""
        tier = row["subscription_tier"]
        stripe_id = row["stripe_customer_id"]
        has_token = bool(row["access_token"])
        token_expires = row.get("token_expires_at")
        bot_name = row["bot_first_name"]
        calendar_id = row["calendar_id"]
        has_password = bool(row["password_hash"])
        tz = row.get("timezone", "Not set")

        result["email"] = email
        result["location_id"] = location_id

        # Subscription status
        tier_names = {
            "sms_bot": "SMS Bot ($99.98/mo)",
            "individual": "Power Dialer ($149.98/mo)",
            "pro_dialer": "Pro Dialer ($224.98/mo)",
            "solo_predictive": "Predictive Dialer ($349.98/mo)",
        }
        if stripe_id and tier:
            result["subscription"] = f"Active — {tier_names.get(tier, tier)}"
        elif stripe_id and not tier:
            result["subscription"] = "Active but plan tier not set (unusual)"
        else:
            result["subscription"] = "NO ACTIVE SUBSCRIPTION — they need to subscribe"

        # CRM connection
        if has_token:
            if token_expires:
                try:
                    exp_dt = token_expires if hasattr(token_expires, 'timestamp') else datetime.fromisoformat(str(token_expires))
                    now = datetime.now(timezone.utc)
                    if hasattr(exp_dt, 'tzinfo') and exp_dt.tzinfo is None:
                        from datetime import timezone as tz_mod
                        exp_dt = exp_dt.replace(tzinfo=tz_mod.utc)
                    if exp_dt < now:
                        result["crm_status"] = "EXPIRED — they need to reconnect from the dashboard"
                    else:
                        result["crm_status"] = "Connected and active"
                except Exception:
                    result["crm_status"] = "Connected (couldn't verify expiry)"
            else:
                result["crm_status"] = "Connected (no expiry info)"
        else:
            result["crm_status"] = "NOT CONNECTED — they need to connect their CRM from the dashboard"

        # Onboarding completeness
        issues = []
        if not location_id or location_id.startswith("temp_"):
            issues.append("account ID not set (CRM not connected)")
        if not calendar_id:
            issues.append("calendar not selected")
        if not bot_name:
            issues.append("bot name not configured")
        if not has_password:
            issues.append("password not set")

        if issues:
            result["onboarding"] = f"INCOMPLETE — missing: {', '.join(issues)}"
        else:
            result["onboarding"] = "Complete — all setup steps done"

        # Bot status
        if bot_name and calendar_id and has_token:
            result["bot_status"] = f"Active — named \"{bot_name}\", timezone {tz}"
        elif bot_name:
            result["bot_status"] = f"Configured as \"{bot_name}\" but may not be fully operational (check CRM connection and calendar)"
        else:
            result["bot_status"] = "Not configured yet"

        # Recent errors
        if location_id and not location_id.startswith("temp_"):
            try:
                error_logs = get_webhook_logs(location_id, limit=10, status="error")
                if error_logs:
                    error_count = len(error_logs)
                    # Summarize error types
                    error_types = set()
                    for log in error_logs:
                        details = log.get("details") or {}
                        if isinstance(details, str):
                            try:
                                details = json.loads(details)
                            except Exception:
                                details = {}
                        reason = details.get("failure_reason", "")
                        if reason == "auth":
                            error_types.add("CRM connection issues")
                        elif "sms" in (log.get("event_type") or "").lower():
                            error_types.add("text message delivery failures")
                        else:
                            error_types.add("processing errors")

                    result["recent_errors"] = f"{error_count} errors in recent activity: {', '.join(error_types)}"
                else:
                    result["recent_errors"] = "No recent errors — things look clean"
            except Exception:
                result["recent_errors"] = "Couldn't check recent activity"

        # Recommendation
        if result.get("crm_status", "").startswith("EXPIRED") or result.get("crm_status", "").startswith("NOT CONNECTED"):
            result["recommendation"] = "Walk them through reconnecting their CRM: Dashboard → click 'Connect Lead Connector' → approve the connection"
        elif issues:
            result["recommendation"] = f"Help them finish setup: {', '.join(issues)}"
        elif result.get("recent_errors", "").startswith("No"):
            result["recommendation"] = "Account looks healthy. Ask what specific issue they're experiencing."
        else:
            result["recommendation"] = "Check the recent errors above and help them troubleshoot the specific failure type."

        cur.close()

    except Exception as e:
        logger.error(f"Support diagnostics failed: {e}")
        result = {"not_found": True, "recommendation": "We had trouble looking up the account. Let's try again."}
    finally:
        if conn:
            return_db_connection(conn)

    return result


# ── Ticket creation ──────────────────────────────────────────────────────────

def create_support_ticket(email: str, location_id: str, conversation_log: list,
                          summary: str, category: str, severity: str) -> int | None:
    """Create a support ticket in the database.

    Returns the ticket ID or None on failure.
    """
    from db_legacy import get_db_connection, return_db_connection

    valid_categories = {"setup", "billing", "bot_behavior", "voice", "crm", "technical", "feature_request"}
    valid_severities = {"low", "medium", "high", "critical"}
    category = category if category in valid_categories else "technical"
    severity = severity if severity in valid_severities else "medium"

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return None

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO support_tickets (email, location_id, issue_summary, category, severity, conversation_log)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (email, location_id, summary[:500], category, severity,
              json.dumps(conversation_log[-30:] if conversation_log else [])))
        ticket_id = cur.fetchone()["id"]
        conn.commit()
        cur.close()

        logger.info(f"Support ticket #{ticket_id} created: [{severity}] {category} — {summary[:100]}")

        # Email notification for high/critical
        if severity in ("high", "critical"):
            _notify_admin_ticket(ticket_id, email, category, severity, summary)

        return ticket_id

    except Exception as e:
        logger.error(f"Failed to create support ticket: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return None
    finally:
        if conn:
            return_db_connection(conn)


def _notify_admin_ticket(ticket_id: int, email: str, category: str, severity: str, summary: str):
    """Send email notification to admins for high/critical tickets."""
    try:
        from send_email_api import send_email_via_api
        from extensions import ADMIN_EMAILS

        severity_colors = {"high": "#ff9800", "critical": "#f44336"}
        color = severity_colors.get(severity, "#666")

        for admin_email in ADMIN_EMAILS:
            send_email_via_api(
                to_email=admin_email,
                subject=f"[{severity.upper()}] Support Ticket #{ticket_id} — {category}",
                html_body=(
                    f"<p>A <strong style='color:{color}'>{severity.upper()}</strong> "
                    f"support ticket was auto-created.</p>"
                    f"<p><strong>Customer:</strong> {email or 'Anonymous'}<br>"
                    f"<strong>Category:</strong> {category}<br>"
                    f"<strong>Summary:</strong> {summary}</p>"
                    f"<p>View in God Mode to review the full conversation and respond.</p>"
                ),
            )
    except Exception as e:
        logger.warning(f"Failed to send ticket notification: {e}")


def get_support_tickets(status: str = None, severity: str = None,
                        limit: int = 50, offset: int = 0) -> list:
    """Fetch support tickets for admin view."""
    from db_legacy import get_db_connection, return_db_connection

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return []

        cur = conn.cursor()
        query = "SELECT * FROM support_tickets WHERE 1=1"
        params = []
        if status:
            query += " AND status = %s"
            params.append(status)
        if severity:
            query += " AND severity = %s"
            params.append(severity)
        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        cur.execute(query, params)
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]

    except Exception as e:
        logger.error(f"Failed to fetch support tickets: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def update_ticket_status(ticket_id: int, status: str, admin_notes: str = None):
    """Update a support ticket's status."""
    from db_legacy import get_db_connection, return_db_connection

    valid = {"open", "reviewed", "resolved"}
    if status not in valid:
        return False

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False

        cur = conn.cursor()
        ts_field = "reviewed_at" if status == "reviewed" else "resolved_at" if status == "resolved" else None
        if ts_field:
            cur.execute(
                f"UPDATE support_tickets SET status = %s, {ts_field} = NOW(), admin_notes = COALESCE(%s, admin_notes) WHERE id = %s",
                (status, admin_notes, ticket_id)
            )
        else:
            cur.execute(
                "UPDATE support_tickets SET status = %s, admin_notes = COALESCE(%s, admin_notes) WHERE id = %s",
                (status, admin_notes, ticket_id)
            )
        conn.commit()
        cur.close()
        return True

    except Exception as e:
        logger.error(f"Failed to update ticket #{ticket_id}: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn:
            return_db_connection(conn)


# ── Tag parsing helpers ──────────────────────────────────────────────────────

_TICKET_RE = re.compile(r'\[TICKET:(\w+):(\w+):([^\]]+)\]')
_OPTIONS_RE = re.compile(r'\[OPTIONS:([^\]]+)\]')
_REDIRECT_RE = re.compile(r'\[REDIRECT:([^\]]+)\]')


def extract_ticket_tag(text: str) -> dict | None:
    """Parse [TICKET:category:severity:summary] from AI response."""
    match = _TICKET_RE.search(text)
    if not match:
        return None
    return {
        "category": match.group(1),
        "severity": match.group(2),
        "summary": match.group(3).strip(),
        "raw_tag": match.group(0),
    }


def extract_options(text: str) -> tuple[list, str]:
    """Parse [OPTIONS:label1|value1,label2|value2] from AI response.

    Returns (options_list, cleaned_text).
    """
    match = _OPTIONS_RE.search(text)
    if not match:
        return [], text

    options = []
    raw = match.group(1)
    for pair in raw.split(","):
        parts = pair.strip().split("|", 1)
        if len(parts) == 2:
            options.append({"label": parts[0].strip(), "value": parts[1].strip()})

    cleaned = text[:match.start()].rstrip() + text[match.end():]
    return options, cleaned.strip()


def extract_redirect(text: str) -> tuple[str | None, str]:
    """Parse [REDIRECT:/path] from AI response.

    Returns (redirect_url_or_none, cleaned_text).
    """
    match = _REDIRECT_RE.search(text)
    if not match:
        return None, text

    url = match.group(1).strip()
    cleaned = text[:match.start()].rstrip() + text[match.end():]
    return url, cleaned.strip()


# ── Quick action handler ─────────────────────────────────────────────────────

def handle_quick_action(message: str) -> dict | None:
    """Handle quick-action button presses without AI call.

    Returns response dict or None if not a quick action.
    """
    actions = {
        "QUICK_DEMO": {
            "text": "Let's show you what the AI can do with a real insurance lead conversation.",
            "redirect": "/demo-chat"
        },
        "QUICK_PRICING": None,  # Let AI handle — it has full pricing knowledge
        "QUICK_SUPPORT": None,  # Let AI handle — needs to ask what's wrong
        "QUICK_SETUP": {
            "text": (
                "Getting started is quick — about 5 minutes:\n\n"
                "1. Connect your CRM (click 'Connect Lead Connector' on the dashboard)\n"
                "2. Pick a plan (Power Dialer is the most popular)\n"
                "3. Set your password\n"
                "4. Configure your bot (name, calendar, timezone)\n\n"
                "Want to jump right in?"
            ),
            "options": [
                {"label": "Start Free Trial", "value": "QUICK_TRIAL"},
                {"label": "See Pricing First", "value": "QUICK_PRICING"}
            ]
        },
        "QUICK_TRIAL": {
            "text": "Great choice! Every plan comes with a 7-day free trial. No charge until day 8.",
            "redirect": "/checkout?consent=1"
        },
        "CONSENT_YES": None,  # Let AI handle — it needs to ask for email
        "CONSENT_NO": None,   # Let AI handle — it should offer general help
    }

    if message in actions:
        return actions[message]
    return None


# ── Rate limiting ────────────────────────────────────────────────────────────

SUPPORT_RATE_PER_MIN = 20
SUPPORT_RATE_PER_HOUR = 100


def support_rate_limited(ip: str) -> bool:
    """Redis sliding window rate limit per IP."""
    try:
        from extensions import ensure_redis
        ensure_redis()
        from extensions import redis_conn
        if not redis_conn:
            return False

        # Per-minute
        min_key = f"support_rate:{ip}:min"
        count = redis_conn.incr(min_key)
        if count == 1:
            redis_conn.expire(min_key, 60)
        if count > SUPPORT_RATE_PER_MIN:
            return True

        # Per-hour
        hour_key = f"support_rate:{ip}:hour"
        hcount = redis_conn.incr(hour_key)
        if hcount == 1:
            redis_conn.expire(hour_key, 3600)
        if hcount > SUPPORT_RATE_PER_HOUR:
            return True

        return False
    except Exception:
        return False
