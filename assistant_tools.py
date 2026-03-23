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
        {
            "type": "function",
            "function": {
                "name": "queue_dial_session",
                "description": "Queue contacts from a pipeline stage for a power dial session. Fetches all contacts in the specified pipeline/stage and starts dialing. Use when the user says things like 'call everyone in New Leads' or 'dial my hot leads' or 'power dial the Qualified stage'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pipeline_name": {
                            "type": "string",
                            "description": "Name of the pipeline (e.g. 'Sales Pipeline', 'New Leads')"
                        },
                        "stage_name": {
                            "type": "string",
                            "description": "Name of the stage within the pipeline (e.g. 'Qualified', 'Hot Leads', 'Follow Up'). If not specified, queues ALL contacts in the pipeline."
                        },
                        "dial_mode": {
                            "type": "string",
                            "enum": ["ai", "human"],
                            "description": "Who handles connected calls: 'ai' for AI voice agent, 'human' for the user. Default: ai"
                        }
                    },
                    "required": ["pipeline_name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "query_call_history",
                "description": "Search call history with filters. Use for: 'show calls over 5 minutes', 'did anyone call me today?', 'missed calls', 'who did I talk to longest?', 'show my recordings', 'inbound calls today'. Returns call records with contact name, duration, direction, and status.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "period": {"type": "string", "enum": ["today", "yesterday", "week", "month", "all"], "description": "Time period. Default: today"},
                        "direction": {"type": "string", "enum": ["inbound", "outbound", "all"], "description": "Call direction filter. Default: all"},
                        "min_duration_seconds": {"type": "integer", "description": "Minimum call duration in seconds (e.g. 300 for 5 minutes)"},
                        "status": {"type": "string", "enum": ["completed", "no-answer", "busy", "failed", "all"], "description": "Call status filter. Use 'no-answer' for missed calls. Default: all"},
                        "has_recording": {"type": "boolean", "description": "Only show calls with recordings"},
                        "sort": {"type": "string", "enum": ["duration_desc", "duration_asc", "recent", "oldest"], "description": "Sort order. Default: recent"},
                        "limit": {"type": "integer", "description": "Max results (default 10, max 50)"}
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_hot_leads",
                "description": "Get the user's hottest leads ranked by AI intelligence score. Use for: 'who should I call first?', 'show my hottest leads', 'who's ready to buy?', 'priority leads', 'best leads to call'. Returns contacts sorted by temperature and score.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "temperature": {"type": "string", "enum": ["hot", "warm", "cool", "cold", "all"], "description": "Filter by AI temperature. Default: hot"},
                        "limit": {"type": "integer", "description": "Max results (default 10, max 30)"},
                        "should_respond_only": {"type": "boolean", "description": "Only show leads where AI says you should respond now. Default: false"}
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_upcoming_appointments",
                "description": "Check upcoming appointments on the user's calendar. Use for: 'what's on my calendar today?', 'do I have appointments tomorrow?', 'when's my next call?', 'show my schedule'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days_ahead": {"type": "integer", "description": "Days to look ahead (default 1, max 7)"}
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_recent_messages",
                "description": "Get recent SMS messages. Use for: 'who texted me?', 'any new messages?', 'show texts from today', 'who responded?', 'unread messages', 'did anyone reply?'. Returns recent inbound messages with contact info.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["inbound", "outbound", "all"], "description": "Message direction. Use 'inbound' for 'who texted me'. Default: inbound"},
                        "period": {"type": "string", "enum": ["today", "yesterday", "week"], "description": "Time period. Default: today"},
                        "limit": {"type": "integer", "description": "Max results (default 10, max 30)"}
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_stale_leads",
                "description": "Find leads that haven't been contacted recently. Use for: 'who hasn't been contacted in a week?', 'stale leads', 'leads going cold', 'who needs follow up?', 'neglected contacts', 'who should I follow up with?'.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "days_since_contact": {"type": "integer", "description": "Minimum days since last contact (default 7)"},
                        "limit": {"type": "integer", "description": "Max results (default 15, max 50)"}
                    },
                    "required": []
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


def _handle_queue_dial_session(args, ctx):
    """Fetch contacts by pipeline/stage and return them for the dialer queue."""
    pipeline_name = (args.get("pipeline_name") or "").strip()
    stage_name = (args.get("stage_name") or "").strip()
    dial_mode = args.get("dial_mode", "ai")

    if not pipeline_name:
        return {"error": "Pipeline name is required. Ask which pipeline to dial."}

    location_id = ctx["location_id"]
    access_token = ctx["access_token"]

    if not access_token:
        return {"error": "No CRM connection. Connect your CRM first."}

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-07-28",
    }

    # Step 1: Fetch pipelines to find matching pipeline_id and stage_id
    try:
        resp = requests.get(
            "https://services.leadconnectorhq.com/opportunities/pipelines",
            headers=headers,
            params={"locationId": location_id},
            timeout=10,
        )
        if resp.status_code != 200:
            return {"error": "Could not fetch pipelines from your CRM."}

        pipelines = resp.json().get("pipelines", [])
    except Exception as e:
        logger.error(f"Pipeline fetch failed: {e}")
        return {"error": "Could not connect to your CRM."}

    # Match pipeline by name (case-insensitive fuzzy)
    pipeline = None
    pl_lower = pipeline_name.lower()
    for p in pipelines:
        if p.get("name", "").lower() == pl_lower:
            pipeline = p
            break
    if not pipeline:
        for p in pipelines:
            if pl_lower in p.get("name", "").lower() or p.get("name", "").lower() in pl_lower:
                pipeline = p
                break
    if not pipeline:
        available = [p.get("name") for p in pipelines]
        return {"error": f"No pipeline matching '{pipeline_name}'. Available: {', '.join(available)}"}

    pipeline_id = pipeline["id"]

    # Match stage if specified
    stage_id = None
    if stage_name:
        st_lower = stage_name.lower()
        for s in pipeline.get("stages", []):
            if s.get("name", "").lower() == st_lower:
                stage_id = s["id"]
                break
        if not stage_id:
            for s in pipeline.get("stages", []):
                if st_lower in s.get("name", "").lower() or s.get("name", "").lower() in st_lower:
                    stage_id = s["id"]
                    break
        if not stage_id:
            available = [s.get("name") for s in pipeline.get("stages", [])]
            return {"error": f"No stage matching '{stage_name}' in {pipeline.get('name')}. Available: {', '.join(available)}"}

    # Step 2: Fetch contacts in this pipeline/stage via opportunities API
    try:
        opp_params = {"location_id": location_id, "pipeline_id": pipeline_id, "limit": "100"}
        if stage_id:
            opp_params["pipeline_stage_id"] = stage_id

        opp_resp = requests.get(
            "https://services.leadconnectorhq.com/opportunities/search",
            headers=headers,
            params=opp_params,
            timeout=15,
        )
        if opp_resp.status_code != 200:
            return {"error": "Could not fetch contacts from pipeline."}

        opportunities = opp_resp.json().get("opportunities", [])
    except Exception as e:
        logger.error(f"Opportunities fetch failed: {e}")
        return {"error": "Could not fetch pipeline contacts."}

    if not opportunities:
        stage_label = f" / {stage_name}" if stage_name else ""
        return {"error": f"No contacts found in {pipeline.get('name')}{stage_label}."}

    # Build contact list for the queue
    contacts = []
    for opp in opportunities:
        contact = opp.get("contact", {})
        contact_id = contact.get("id") or opp.get("contactId", "")
        name = f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip()
        phone = contact.get("phone", "")
        first_name = contact.get("firstName", "")

        if contact_id and phone:
            contacts.append({
                "id": contact_id,
                "name": name or "Unknown",
                "firstName": first_name or name.split()[0] if name else "there",
                "phone": phone,
            })

    if not contacts:
        return {"error": "Found opportunities but none have phone numbers."}

    stage_label = f" / {stage_name}" if stage_name else ""
    return {
        "action": "dial_queue",
        "contacts": contacts,
        "dial_mode": dial_mode,
        "pipeline_name": pipeline.get("name"),
        "stage_name": stage_name,
        "message": f"Queued {len(contacts)} contacts from {pipeline.get('name')}{stage_label}. Ready to start dialing.",
    }


def _handle_query_call_history(args, ctx):
    """Query call history with flexible filters."""
    from db import get_db_connection, return_db_connection

    period = args.get("period", "today")
    direction = args.get("direction", "all")
    min_dur = args.get("min_duration_seconds", 0)
    status = args.get("status", "all")
    has_rec = args.get("has_recording", False)
    sort = args.get("sort", "recent")
    limit = min(args.get("limit", 10), 50)

    location_id = ctx["location_id"]

    # Build date filter
    date_filters = {
        "today": "AND ch.created_at >= CURRENT_DATE",
        "yesterday": "AND ch.created_at >= CURRENT_DATE - INTERVAL '1 day' AND ch.created_at < CURRENT_DATE",
        "week": "AND ch.created_at >= CURRENT_DATE - INTERVAL '7 days'",
        "month": "AND ch.created_at >= CURRENT_DATE - INTERVAL '30 days'",
        "all": "",
    }
    date_sql = date_filters.get(period, "")

    wheres = [f"ch.location_id = %s {date_sql}"]
    params = [location_id]

    if direction != "all":
        wheres.append("ch.direction = %s")
        params.append(direction)
    if min_dur > 0:
        wheres.append("ch.duration >= %s")
        params.append(min_dur)
    if status != "all":
        wheres.append("ch.status = %s")
        params.append(status)
    if has_rec:
        wheres.append("ch.recording_url IS NOT NULL AND ch.recording_url != ''")

    sort_map = {
        "duration_desc": "ch.duration DESC NULLS LAST",
        "duration_asc": "ch.duration ASC NULLS LAST",
        "recent": "ch.created_at DESC",
        "oldest": "ch.created_at ASC",
    }
    order = sort_map.get(sort, "ch.created_at DESC")

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT ch.contact_name, ch.phone_number, ch.direction, ch.status,
                   ch.duration, ch.created_at, ch.contact_id,
                   CASE WHEN ch.recording_url IS NOT NULL AND ch.recording_url != '' THEN true ELSE false END as has_recording,
                   CASE WHEN ch.transcript IS NOT NULL AND ch.transcript != '' THEN true ELSE false END as has_transcript
            FROM call_history ch
            WHERE {' AND '.join(wheres)}
            ORDER BY {order}
            LIMIT %s
        """, params + [limit])

        rows = cur.fetchall()
        cur.close()

        calls = []
        for r in rows:
            dur = r[4] or 0
            mins = dur // 60
            secs = dur % 60
            calls.append({
                "name": r[0] or "Unknown",
                "phone": r[1] or "",
                "direction": r[2] or "outbound",
                "status": r[3] or "unknown",
                "duration": f"{mins}m {secs}s" if mins > 0 else f"{secs}s",
                "duration_seconds": dur,
                "time": str(r[5])[:16] if r[5] else "",
                "contact_id": r[6] or "",
                "has_recording": r[7],
                "has_transcript": r[8],
            })

        if not calls:
            return {"count": 0, "message": "No calls found matching your criteria."}

        return {"count": len(calls), "calls": calls}

    except Exception as e:
        logger.error(f"Call history query failed: {e}")
        return {"error": "Could not query call history."}
    finally:
        if conn:
            return_db_connection(conn)


def _handle_get_hot_leads(args, ctx):
    """Get leads ranked by AI intelligence score."""
    from db import get_db_connection, return_db_connection

    temperature = args.get("temperature", "hot")
    limit = min(args.get("limit", 10), 30)
    respond_only = args.get("should_respond_only", False)

    location_id = ctx["location_id"]

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Join contact_intelligence with contact_cache for names/phones
        wheres = ["cc.location_id = %s", "ci.analysis IS NOT NULL"]
        params = [location_id]

        if temperature != "all":
            wheres.append("ci.analysis->>'temperature' = %s")
            params.append(temperature)

        if respond_only:
            wheres.append("(ci.analysis->>'should_respond')::boolean = true")

        cur.execute(f"""
            SELECT cc.contact_id, cc.first_name, cc.last_name, cc.phone, cc.email,
                   ci.analysis->>'temperature' as temperature,
                   (ci.analysis->>'score')::int as score,
                   ci.analysis->>'summary' as summary,
                   (ci.analysis->>'should_respond')::boolean as should_respond,
                   ci.analysis->>'should_respond_reason' as respond_reason
            FROM contact_cache cc
            JOIN contact_intelligence ci ON ci.contact_id = cc.contact_id
            WHERE {' AND '.join(wheres)}
            ORDER BY (ci.analysis->>'score')::int DESC NULLS LAST
            LIMIT %s
        """, params + [limit])

        rows = cur.fetchall()
        cur.close()

        leads = []
        for r in rows:
            leads.append({
                "contact_id": r[0],
                "name": f"{r[1] or ''} {r[2] or ''}".strip() or "Unknown",
                "phone": r[3] or "",
                "email": r[4] or "",
                "temperature": r[5] or "unknown",
                "score": r[6] or 0,
                "summary": r[7] or "",
                "should_respond": r[8],
                "respond_reason": r[9] or "",
            })

        if not leads:
            return {"count": 0, "message": f"No {temperature} leads found."}

        return {"count": len(leads), "leads": leads}

    except Exception as e:
        logger.error(f"Hot leads query failed: {e}")
        return {"error": "Could not fetch leads."}
    finally:
        if conn:
            return_db_connection(conn)


def _handle_get_upcoming_appointments(args, ctx):
    """Fetch upcoming appointments from GHL calendar."""
    days_ahead = min(args.get("days_ahead", 1), 7)
    access_token = ctx["access_token"]
    location_id = ctx["location_id"]
    cal_id = ctx.get("calendar_id")
    tz_str = ctx.get("timezone", "America/Chicago")

    if not cal_id:
        return {"error": "No calendar configured. Set one up in Bot Config."}
    if not access_token:
        return {"error": "No CRM connection."}

    from datetime import datetime, timedelta, timezone
    now_utc = datetime.now(timezone.utc)
    start_ts = int(now_utc.timestamp() * 1000)
    end_ts = int((now_utc + timedelta(days=days_ahead)).timestamp() * 1000)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": "2021-07-28",
    }

    try:
        resp = requests.get(
            f"https://services.leadconnectorhq.com/calendars/events",
            headers=headers,
            params={"locationId": location_id, "calendarId": cal_id, "startTime": start_ts, "endTime": end_ts},
            timeout=10,
        )
        if resp.status_code != 200:
            return {"error": "Could not fetch calendar events."}

        events = resp.json().get("events", [])
        if not events:
            return {"count": 0, "message": f"No appointments in the next {days_ahead} day{'s' if days_ahead > 1 else ''}."}

        appointments = []
        for ev in events[:20]:
            appointments.append({
                "title": ev.get("title") or ev.get("name") or "Appointment",
                "start": ev.get("startTime", ""),
                "end": ev.get("endTime", ""),
                "contact_name": ev.get("contact", {}).get("name") or ev.get("contactName") or "",
                "status": ev.get("appointmentStatus") or ev.get("status") or "",
            })

        return {"count": len(appointments), "appointments": appointments}

    except Exception as e:
        logger.error(f"Calendar events fetch failed: {e}")
        return {"error": "Could not fetch appointments."}


def _handle_get_recent_messages(args, ctx):
    """Get recent SMS messages."""
    from db import get_db_connection, return_db_connection

    direction = args.get("direction", "inbound")
    period = args.get("period", "today")
    limit = min(args.get("limit", 10), 30)
    location_id = ctx["location_id"]

    date_filters = {
        "today": "AND cm.created_at >= CURRENT_DATE",
        "yesterday": "AND cm.created_at >= CURRENT_DATE - INTERVAL '1 day' AND cm.created_at < CURRENT_DATE",
        "week": "AND cm.created_at >= CURRENT_DATE - INTERVAL '7 days'",
    }
    date_sql = date_filters.get(period, "AND cm.created_at >= CURRENT_DATE")

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        dir_filter = ""
        if direction == "inbound":
            dir_filter = "AND cm.direction = 'inbound'"
        elif direction == "outbound":
            dir_filter = "AND cm.direction = 'outbound'"

        cur.execute(f"""
            SELECT cm.contact_id, cm.message, cm.direction, cm.created_at,
                   cc.first_name, cc.last_name, cc.phone
            FROM contact_messages cm
            LEFT JOIN contact_cache cc ON cc.contact_id = cm.contact_id AND cc.location_id = %s
            WHERE cm.location_id = %s {date_sql} {dir_filter}
            ORDER BY cm.created_at DESC
            LIMIT %s
        """, (location_id, location_id, limit))

        rows = cur.fetchall()
        cur.close()

        messages = []
        for r in rows:
            messages.append({
                "contact_id": r[0] or "",
                "message": (r[1] or "")[:150],
                "direction": r[2] or "inbound",
                "time": str(r[3])[:16] if r[3] else "",
                "name": f"{r[4] or ''} {r[5] or ''}".strip() or "Unknown",
                "phone": r[6] or "",
            })

        if not messages:
            dir_label = {"inbound": "inbound", "outbound": "outbound", "all": ""}
            return {"count": 0, "message": f"No {dir_label.get(direction, '')} messages found for {period}."}

        return {"count": len(messages), "messages": messages}

    except Exception as e:
        logger.error(f"Messages query failed: {e}")
        return {"error": "Could not fetch messages."}
    finally:
        if conn:
            return_db_connection(conn)


def _handle_get_stale_leads(args, ctx):
    """Find leads that haven't been contacted recently."""
    from db import get_db_connection, return_db_connection

    days = args.get("days_since_contact", 7)
    limit = min(args.get("limit", 15), 50)
    location_id = ctx["location_id"]

    conn = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Find contacts in cache that have NO recent call or message
        cur.execute("""
            SELECT cc.contact_id, cc.first_name, cc.last_name, cc.phone, cc.email,
                   ci.analysis->>'temperature' as temperature,
                   (ci.analysis->>'score')::int as score,
                   ci.analysis->>'summary' as summary,
                   GREATEST(
                       (SELECT MAX(created_at) FROM call_history WHERE contact_id = cc.contact_id),
                       (SELECT MAX(created_at) FROM contact_messages WHERE contact_id = cc.contact_id)
                   ) as last_contact
            FROM contact_cache cc
            LEFT JOIN contact_intelligence ci ON ci.contact_id = cc.contact_id
            WHERE cc.location_id = %s
              AND cc.phone IS NOT NULL AND cc.phone != ''
              AND NOT EXISTS (
                  SELECT 1 FROM call_history ch
                  WHERE ch.contact_id = cc.contact_id
                    AND ch.created_at >= CURRENT_DATE - INTERVAL '%s days'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM contact_messages cm
                  WHERE cm.contact_id = cc.contact_id
                    AND cm.created_at >= CURRENT_DATE - INTERVAL '%s days'
              )
            ORDER BY last_contact ASC NULLS FIRST
            LIMIT %s
        """, (location_id, days, days, limit))

        rows = cur.fetchall()
        cur.close()

        leads = []
        for r in rows:
            leads.append({
                "contact_id": r[0],
                "name": f"{r[1] or ''} {r[2] or ''}".strip() or "Unknown",
                "phone": r[3] or "",
                "email": r[4] or "",
                "temperature": r[5] or "unknown",
                "score": r[6] or 0,
                "summary": r[7] or "",
                "last_contact": str(r[8])[:10] if r[8] else "Never",
            })

        if not leads:
            return {"count": 0, "message": f"All your leads have been contacted within the last {days} days. Nice work!"}

        return {"count": len(leads), "leads": leads}

    except Exception as e:
        logger.error(f"Stale leads query failed: {e}")
        return {"error": "Could not fetch stale leads."}
    finally:
        if conn:
            return_db_connection(conn)


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
    "queue_dial_session": _handle_queue_dial_session,
    "query_call_history": _handle_query_call_history,
    "get_hot_leads": _handle_get_hot_leads,
    "get_upcoming_appointments": _handle_get_upcoming_appointments,
    "get_recent_messages": _handle_get_recent_messages,
    "get_stale_leads": _handle_get_stale_leads,
}
