# assistant_tools.py — Tool definitions and execution for the dashboard AI assistant
#
# Pattern follows support_tools.py but with key differences:
# - No consent gate (user is authenticated, acting on their own account)
# - Direct access to GHL APIs via user's tokens
# - Can execute write actions (book, send SMS) without asking permission
#
# Tools: search_contact, send_sms, check_calendar, book_appointment,
#         get_call_stats, navigate_dashboard, get_contact_intelligence

import json
import logging
import re
import requests

logger = logging.getLogger(__name__)

# ── Secret scrubber (reused from support_tools.py pattern) ────────────────────

_SECRET_PATTERNS = [
    (re.compile(r'AC[a-f0-9]{32}'), '[REDACTED]'),
    (re.compile(r'SK[a-f0-9]{32}'), '[REDACTED]'),
    (re.compile(r'BU[a-f0-9]{32}'), '[REDACTED]'),
    (re.compile(r'PN[a-f0-9]{32}'), '[REDACTED]'),
    (re.compile(r'auth_token[\s:="\']+[a-f0-9]{32}', re.IGNORECASE), 'auth_token=[REDACTED]'),
]


def _scrub(data):
    """Remove secrets from tool results."""
    text = json.dumps(data) if isinstance(data, (dict, list)) else str(data)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


# ── Tool definitions ──────────────────────────────────────────────────────────

def get_assistant_tool_definitions():
    """Return OpenAI-compatible tool schemas for the dashboard assistant."""
    return [
        {
            "type": "function",
            "function": {
                "name": "search_contact",
                "description": "Search for a contact in the user's CRM by name or phone number. Returns contact details including name, phone, email, tags, and pipeline stage. Always call this before sending SMS or booking — you need the contact_id.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Contact name (e.g. 'John Smith') or phone number (e.g. '+15551234567')"
                        },
                        "search_type": {
                            "type": "string",
                            "enum": ["name", "phone"],
                            "description": "Search by name or phone number. Default: name"
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "send_sms",
                "description": "Send an SMS text message to a contact. Requires the contact_id (use search_contact first if you only have a name).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {
                            "type": "string",
                            "description": "The GHL contact ID"
                        },
                        "message": {
                            "type": "string",
                            "description": "The text message to send"
                        }
                    },
                    "required": ["contact_id", "message"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "check_calendar",
                "description": "Check available appointment time slots on the user's calendar for the next few days.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days_ahead": {
                            "type": "integer",
                            "description": "Number of days to look ahead (default 3, max 14)"
                        }
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "book_appointment",
                "description": "Book an appointment for a contact at a specific date and time. Requires contact_id (use search_contact first) and a time expression.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {
                            "type": "string",
                            "description": "The GHL contact ID"
                        },
                        "selected_time": {
                            "type": "string",
                            "description": "The appointment time, e.g. 'Tuesday at 2:30 PM', 'tomorrow at 4pm', '2026-03-25 14:00'"
                        },
                        "first_name": {
                            "type": "string",
                            "description": "Contact's first name (for calendar event title)"
                        }
                    },
                    "required": ["contact_id", "selected_time"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_call_stats",
                "description": "Get the user's call statistics including total calls, connected calls, connect rate, and talk time.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "period": {
                            "type": "string",
                            "enum": ["today", "week", "month", "all"],
                            "description": "Time period for stats. Default: today"
                        }
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "navigate_dashboard",
                "description": "Navigate the user to a specific dashboard tab. Use when they ask to go somewhere or see a specific page.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "destination": {
                            "type": "string",
                            "description": "Where to navigate, e.g. 'voice settings', 'billing', 'dialer', 'team', 'workflows'"
                        }
                    },
                    "required": ["destination"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_contact_intelligence",
                "description": "Get AI-powered intelligence analysis for a contact including temperature (hot/warm/cool/cold), engagement score, summary, and recommended next actions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {
                            "type": "string",
                            "description": "The GHL contact ID"
                        }
                    },
                    "required": ["contact_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "make_call",
                "description": "Initiate an outbound phone call to a contact. Requires contact_id and phone number (use search_contact first). If the user hasn't specified whether AI or they should talk, call WITHOUT dial_mode to ask them first.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_id": {
                            "type": "string",
                            "description": "The GHL contact ID"
                        },
                        "phone": {
                            "type": "string",
                            "description": "The contact's phone number"
                        },
                        "first_name": {
                            "type": "string",
                            "description": "Contact's first name"
                        },
                        "dial_mode": {
                            "type": "string",
                            "enum": ["ai", "human"],
                            "description": "Who handles the call: 'ai' for AI voice agent, 'human' for the user to talk directly. ONLY set this if the user explicitly chose. Omit to ask them."
                        }
                    },
                    "required": ["contact_id", "phone"]
                }
            }
        },
    ]


# ── Tool execution ────────────────────────────────────────────────────────────

def execute_assistant_tool(tool_name: str, tool_args: dict, user_ctx: dict) -> dict:
    """Execute a tool and return the result dict."""
    try:
        handler = _TOOL_HANDLERS.get(tool_name)
        if not handler:
            return {"error": f"Unknown tool: {tool_name}"}
        result = handler(tool_args, user_ctx)
        return _scrub(result)
    except Exception as e:
        logger.error(f"Assistant tool {tool_name} failed: {e}", exc_info=True)
        return {"error": f"Tool failed: {str(e)[:200]}"}


# ── Individual tool handlers ──────────────────────────────────────────────────

def _handle_search_contact(args, ctx):
    """Search for a contact by name or phone."""
    from contact_validator import search_contact_by_name, search_contact_by_phone
    from db import get_db_connection, return_db_connection

    query = (args.get("query") or "").strip()
    search_type = args.get("search_type", "name")
    location_id = ctx["location_id"]
    access_token = ctx["access_token"]

    if not query:
        return {"error": "No search query provided"}

    contact_id = None

    if search_type == "phone":
        result = search_contact_by_phone(location_id, query)
        if result:
            contact_id = result.get("contact_id") or result.get("id")
    else:
        contact_id = search_contact_by_name(location_id, query)

    if not contact_id:
        return {"found": False, "message": f"No contact found matching '{query}'"}

    # Fetch full contact details from GHL
    try:
        resp = requests.get(
            f"https://services.leadconnectorhq.com/contacts/{contact_id}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Version": "2021-07-28",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            contact = resp.json().get("contact", {})
            result = {
                "found": True,
                "contact_id": contact_id,
                "name": f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip(),
                "first_name": contact.get("firstName", ""),
                "phone": contact.get("phone", ""),
                "email": contact.get("email", ""),
                "tags": contact.get("tags", []),
            }

            # Check for cached AI intelligence
            conn = None
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT analysis FROM contact_intelligence WHERE contact_id = %s",
                    (contact_id,)
                )
                row = cur.fetchone()
                if row and row[0]:
                    intel = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                    result["temperature"] = intel.get("temperature", "")
                    result["score"] = intel.get("score", "")
                    result["summary"] = intel.get("summary", "")
                cur.close()
            except Exception:
                pass
            finally:
                if conn:
                    return_db_connection(conn)

            return result
        else:
            return {"found": True, "contact_id": contact_id, "name": query, "note": "Found ID but couldn't fetch full details"}
    except Exception as e:
        logger.warning(f"Contact detail fetch failed: {e}")
        return {"found": True, "contact_id": contact_id, "name": query}


def _handle_send_sms(args, ctx):
    """Send an SMS message to a contact."""
    from ghl_message import send_sms_via_ghl

    contact_id = args.get("contact_id", "").strip()
    message = args.get("message", "").strip()

    if not contact_id or not message:
        return {"error": "Both contact_id and message are required"}

    success, fail_reason, http_detail = send_sms_via_ghl(
        contact_id=contact_id,
        message=message,
        access_token=ctx["access_token"],
        location_id=ctx["location_id"],
    )

    if success:
        return {"sent": True, "message": f"SMS sent successfully"}
    else:
        return {"sent": False, "error": fail_reason or "Failed to send SMS"}


def _handle_check_calendar(args, ctx):
    """Check available calendar slots."""
    from ghl_calendar import consolidated_calendar_op

    if not ctx.get("calendar_id"):
        return {"error": "No calendar configured. Go to Bot Config to set one up."}

    subscriber_data = {
        "location_id": ctx["location_id"],
        "calendar_id": ctx["calendar_id"],
        "crm_user_id": ctx.get("crm_user_id"),
        "timezone": ctx.get("timezone", "America/Chicago"),
        "access_token": ctx["access_token"],
    }

    result = consolidated_calendar_op(
        operation="fetch_slots",
        subscriber_data=subscriber_data,
    )

    if isinstance(result, str):
        return {"slots": result}
    else:
        return {"error": "Could not fetch calendar slots"}


def _handle_book_appointment(args, ctx):
    """Book an appointment for a contact."""
    from ghl_calendar import consolidated_calendar_op

    contact_id = args.get("contact_id", "").strip()
    selected_time = args.get("selected_time", "").strip()
    first_name = args.get("first_name", "").strip()

    if not contact_id or not selected_time:
        return {"error": "Both contact_id and selected_time are required"}

    if not ctx.get("calendar_id"):
        return {"error": "No calendar configured. Go to Bot Config to set one up."}

    subscriber_data = {
        "location_id": ctx["location_id"],
        "calendar_id": ctx["calendar_id"],
        "crm_user_id": ctx.get("crm_user_id"),
        "timezone": ctx.get("timezone", "America/Chicago"),
        "access_token": ctx["access_token"],
    }

    result = consolidated_calendar_op(
        operation="book",
        subscriber_data=subscriber_data,
        contact_id=contact_id,
        first_name=first_name,
        selected_time=selected_time,
    )

    if result is True or (isinstance(result, dict) and result.get("success")):
        return {"booked": True, "time": selected_time, "contact_id": contact_id}
    elif isinstance(result, str) and "booked" in result.lower():
        return {"booked": True, "time": selected_time, "details": result}
    else:
        return {"booked": False, "error": str(result) if result else "Booking failed"}


def _handle_get_call_stats(args, ctx):
    """Get call statistics for the user."""
    from db import get_db_connection, return_db_connection

    period = args.get("period", "today")
    location_id = ctx["location_id"]

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Build date filter
        if period == "today":
            date_filter = "AND created_at >= CURRENT_DATE"
        elif period == "week":
            date_filter = "AND created_at >= CURRENT_DATE - INTERVAL '7 days'"
        elif period == "month":
            date_filter = "AND created_at >= CURRENT_DATE - INTERVAL '30 days'"
        else:
            date_filter = ""

        cur.execute(f"""
            SELECT
                COUNT(*) as total_calls,
                COUNT(*) FILTER (WHERE status = 'completed' AND duration > 0) as connected,
                COALESCE(SUM(duration) FILTER (WHERE status = 'completed'), 0) as total_seconds
            FROM call_history
            WHERE location_id = %s {date_filter}
        """, (location_id,))

        row = cur.fetchone()
        cur.close()

        total = row[0] or 0
        connected = row[1] or 0
        total_seconds = row[2] or 0

        rate = round((connected / total * 100), 1) if total > 0 else 0
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        return {
            "period": period,
            "total_calls": total,
            "connected": connected,
            "connect_rate": f"{rate}%",
            "talk_time": f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m",
        }

    except Exception as e:
        logger.error(f"Call stats query failed: {e}")
        return {"error": "Could not fetch call statistics"}
    finally:
        if conn:
            return_db_connection(conn)


def _handle_navigate_dashboard(args, ctx):
    """Map destination to tab ID."""
    dest = (args.get("destination") or "").lower().strip()

    _NAV_MAP = {
        "dialer": "voicedialer", "phone": "voicedialer", "calls": "voicedialer", "dial": "voicedialer",
        "bot config": "config", "sms settings": "config", "sms config": "config", "bot settings": "config", "sms": "config",
        "voice config": "voice", "voice settings": "voice", "voice": "voice",
        "workflows": "workflows", "automations": "workflows", "automation": "workflows",
        "connect crm": "connect", "integrations": "connect", "connect": "connect", "crm": "connect",
        "carriers": "carriers", "carrier": "carriers",
        "advanced": "advanced", "advanced settings": "advanced",
        "ai minutes": "aiminutes", "minutes": "aiminutes",
        "billing": "billing", "subscription": "billing", "plan": "billing", "payment": "billing",
        "logs": "logs", "activity": "logs", "activity logs": "logs",
        "team": "team", "members": "team",
        "training": "training",
        "white label": "whitelabel", "whitelabel": "whitelabel", "branding": "whitelabel",
    }

    tab_id = _NAV_MAP.get(dest)
    if not tab_id:
        # Fuzzy match: check if destination contains any key
        for key, tid in _NAV_MAP.items():
            if key in dest or dest in key:
                tab_id = tid
                break

    if tab_id:
        return {"action": "navigate", "tab_id": tab_id, "message": f"Navigating to {dest}"}
    else:
        return {"error": f"I don't recognize '{dest}'. Try: dialer, voice settings, billing, workflows, team, logs, or carriers."}


def _handle_get_contact_intelligence(args, ctx):
    """Get cached AI intelligence for a contact."""
    from db import get_db_connection, return_db_connection

    contact_id = args.get("contact_id", "").strip()
    if not contact_id:
        return {"error": "contact_id is required"}

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT analysis, analyzed_at FROM contact_intelligence WHERE contact_id = %s",
            (contact_id,)
        )
        row = cur.fetchone()
        cur.close()

        if not row or not row[0]:
            return {"has_intelligence": False, "message": "No AI analysis available yet for this contact. They'll be analyzed automatically on the next dialer load."}

        intel = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return {
            "has_intelligence": True,
            "temperature": intel.get("temperature", "unknown"),
            "temperature_reason": intel.get("temperature_reason", ""),
            "score": intel.get("score", 0),
            "summary": intel.get("summary", ""),
            "should_respond": intel.get("should_respond", False),
            "should_respond_reason": intel.get("should_respond_reason", ""),
            "actions": intel.get("actions", []),
            "analyzed_at": str(row[1]) if row[1] else "",
        }

    except Exception as e:
        logger.error(f"Intelligence lookup failed: {e}")
        return {"error": "Could not fetch intelligence data"}
    finally:
        if conn:
            return_db_connection(conn)


def _handle_make_call(args, ctx):
    """Initiate an outbound call — returns action for frontend to show call mode choice."""
    contact_id = args.get("contact_id", "").strip()
    phone = args.get("phone", "").strip()
    first_name = args.get("first_name", "").strip() or "there"
    dial_mode = args.get("dial_mode", "").strip().lower()

    if not contact_id or not phone:
        return {"error": "Both contact_id and phone are required. Search for the contact first."}

    # If no dial_mode specified, ask the user
    if not dial_mode:
        return {
            "action": "ask_call_mode",
            "contact_id": contact_id,
            "phone": phone,
            "first_name": first_name,
            "message": f"Do you want AI to call {first_name}, or do you want to talk to them yourself?",
        }

    return {
        "action": "call",
        "contact_id": contact_id,
        "phone": phone,
        "first_name": first_name,
        "dial_mode": dial_mode,
        "message": f"Calling {first_name} now...",
    }


# ── Tool handler registry ────────────────────────────────────────────────────

_TOOL_HANDLERS = {
    "search_contact": _handle_search_contact,
    "send_sms": _handle_send_sms,
    "check_calendar": _handle_check_calendar,
    "book_appointment": _handle_book_appointment,
    "get_call_stats": _handle_get_call_stats,
    "navigate_dashboard": _handle_navigate_dashboard,
    "get_contact_intelligence": _handle_get_contact_intelligence,
    "make_call": _handle_make_call,
}
