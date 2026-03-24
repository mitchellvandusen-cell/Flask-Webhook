# blueprints/assistant.py — In-dashboard AI Agent Assistant
#
# Single endpoint: POST /api/assistant/chat
# Authenticated via @login_required, CSRF exempt.
# Multi-turn tool-calling loop with grok-3-mini-fast.
#
# The assistant can search contacts, book appointments, send SMS,
# check stats, navigate tabs, troubleshoot errors, AND provide
# support diagnostics (account lookup, registration checks, error
# logs, knowledge base search, ticket creation) — auto-detected
# from message intent.

import json
import os
import logging
import time

import redis
from flask import Blueprint, request, jsonify as flask_jsonify
from flask_login import login_required, current_user

logger = logging.getLogger(__name__)

assistant_bp = Blueprint('assistant', __name__)

_ASSISTANT_MODEL = "grok-3-mini-fast"
_ASSISTANT_MAX_TOOL_ROUNDS = 3
_ASSISTANT_MAX_TOKENS = 400
_RATE_LIMIT_RPM = 30

# ── Support intent detection ──────────────────────────────────────────────────
# Keywords that signal the user wants support/diagnostics rather than dashboard actions.
# When detected, support tools are merged into the tool set automatically.

_SUPPORT_KEYWORDS = {
    # Troubleshooting
    "not working", "broken", "issue", "problem", "error", "bug", "fix",
    "help me", "trouble", "wrong", "failing", "failed", "can't", "won't",
    "doesn't work", "stopped working", "down", "outage",
    # Registration / phone system
    "registration", "a2p", "spam likely", "spam protection", "caller id",
    "voice integrity", "cnam", "trust hub", "carrier registration",
    "text registration", "messages blocked", "texts not going",
    "calls showing spam", "spam label",
    # Account / setup
    "my account", "account status", "crm connection", "crm expired",
    "token expired", "reconnect", "onboarding",
    # Billing support (not billing nav)
    "billing issue", "charged", "refund", "overcharged", "payment failed",
    "invoice", "charge",
    # Support escalation
    "support ticket", "create ticket", "escalate", "speak to human",
    "talk to someone", "human support", "contact support",
    # Knowledge base
    "how do i", "how does", "what is", "documentation", "docs",
    "getting started", "setup guide", "tutorial",
    # Diagnostics
    "diagnose", "diagnostic", "check my account", "what's wrong",
    "why isn't", "why can't", "why won't", "look into",
    "check my registration", "check my numbers",
}


# ── Fast path: instant responses for common patterns (no LLM) ────────────────

_NAV_KEYWORDS = {
    "dialer": "voicedialer", "dial": "voicedialer", "phone": "voicedialer", "calls": "voicedialer",
    "bot config": "config", "sms settings": "config", "sms config": "config", "bot settings": "config",
    "voice config": "voice", "voice settings": "voice",
    "workflows": "workflows", "automations": "workflows", "automation": "workflows",
    "connect crm": "connect", "integrations": "connect", "connect": "connect", "crm": "connect",
    "carriers": "carriers",
    "advanced": "advanced", "advanced settings": "advanced",
    "ai minutes": "aiminutes", "minutes": "aiminutes",
    "billing": "billing", "subscription": "billing", "plan": "billing",
    "logs": "logs", "activity": "logs", "activity logs": "logs",
    "team": "team", "members": "team",
    "training": "training",
    "white label": "whitelabel", "whitelabel": "whitelabel",
    # Voice sub-panels (tab=voice, sub_panel=X)
    "business profile": "voice:spammonitoring", "spam monitoring": "voice:spammonitoring",
    "spam protection": "voice:spammonitoring", "trust hub": "voice:spammonitoring",
    "caller id": "voice:spammonitoring", "cnam": "voice:spammonitoring",
    "voice integrity": "voice:spammonitoring",
    "numbers": "voice:numbers", "phone numbers": "voice:numbers",
    "a2p": "voice:a2p", "10dlc": "voice:a2p", "a2p registration": "voice:a2p",
    "dialer settings": "voice:dialer",
    "voice activation": "voice:activation",
    # Config sub-panels (tab=config, sub_panel=X)
    "transfer number": "voice:settings", "transfer": "voice:settings",
}


def _try_fast_path(msg: str):
    """Try to handle common requests instantly without LLM. Returns response dict or None."""
    lower = msg.lower().strip()

    # INIT_CHAT greeting
    if lower == "init_chat":
        return {"text": "What can I help you with?"}

    # Navigation: "go to X", "take me to X", "open X", "show X"
    for prefix in ("go to ", "take me to ", "open ", "show ", "show me ", "navigate to ", "switch to "):
        if lower.startswith(prefix):
            dest = lower[len(prefix):].strip()
            nav = _match_nav(dest)
            if nav:
                return _build_nav_response(dest, nav)

    # Direct tab name match (user just types "billing", "dialer", etc.)
    nav = _match_nav(lower)
    if nav and len(lower.split()) <= 3:
        return _build_nav_response(lower, nav)

    # Stats — execute directly without LLM
    if lower in ("stats", "my stats", "call stats", "calls today", "how many calls", "how many calls today",
                  "my calls", "daily stats", "today stats"):
        from assistant_tools import _handle_get_call_stats
        return {"text": _format_stats(_handle_get_call_stats({"period": "today"}, _quick_ctx()))}

    if lower in ("daily summary", "my daily summary", "give me my daily summary", "end of day report",
                  "how did today go", "how did today go?", "daily recap", "recap"):
        from assistant_tools import _handle_get_daily_summary
        return {"text": _format_daily_summary(_handle_get_daily_summary({"period": "today"}, _quick_ctx()))}

    if lower in ("my reminders", "reminders", "show reminders", "show my reminders", "what reminders"):
        from assistant_tools import _handle_list_reminders
        result = _handle_list_reminders({}, _quick_ctx())
        if result.get("count", 0) == 0:
            return {"text": "No pending reminders."}
        lines = [f"- {r.get('message', '')} (at {r.get('remind_at', '')[:16]})" for r in result.get("reminders", [])[:5]]
        return {"text": f"**{result['count']} reminders:**\n" + "\n".join(lines)}

    if lower in ("my plan", "what plan am i on", "what plan am i on?", "subscription", "subscription info"):
        from assistant_tools import _handle_get_subscription_info
        result = _handle_get_subscription_info({}, _quick_ctx())
        return {"text": f"You're on the **{result.get('plan', 'unknown')}** plan."}

    if lower in ("ai minutes", "how many minutes", "minutes balance", "ai minutes balance",
                  "how many ai minutes", "how many ai minutes?"):
        from assistant_tools import _handle_get_ai_minutes_balance
        result = _handle_get_ai_minutes_balance({}, _quick_ctx())
        return {"text": f"**{result.get('balance_minutes', 0)} AI minutes** remaining ({result.get('level', 'ok')})."}

    if lower in ("my carriers", "carriers", "what carriers", "what carriers do i have", "show carriers"):
        from assistant_tools import _handle_list_carriers
        result = _handle_list_carriers({}, _quick_ctx())
        if result.get("count", 0) == 0:
            return {"text": "No carriers selected. Go to the Carriers tab to set them up.", "navigate": "carriers"}
        return {"text": f"**{result['count']} carriers:** " + ", ".join(result.get("carriers", []))}

    if lower in ("my workflows", "workflows", "show workflows", "list workflows"):
        from assistant_tools import _handle_list_workflows
        result = _handle_list_workflows({}, _quick_ctx())
        if result.get("count", 0) == 0:
            return {"text": "No workflows yet. Create one in the Workflows tab.", "navigate": "workflows"}
        lines = [f"- **{w['name']}** ({w['status']})" for w in result.get("workflows", [])[:10]]
        return {"text": f"**{result['count']} workflows:**\n" + "\n".join(lines)}

    if lower in ("my team", "team", "show team", "team members", "who is on my team"):
        from assistant_tools import _handle_list_team_members
        result = _handle_list_team_members({}, _quick_ctx())
        if result.get("count", 0) == 0:
            return {"text": "No team members yet."}
        lines = [f"- **{m['name'] or m['email']}** ({m['role']}, {m['status']})" for m in result.get("members", [])[:10]]
        return {"text": f"**{result['count']} members:**\n" + "\n".join(lines)}

    return None


def _quick_ctx():
    """Build minimal user context for fast-path tool calls."""
    from token_encryption import decrypt_token
    raw = getattr(current_user, "access_token", "") or ""
    try:
        tok = decrypt_token(raw) if raw else ""
    except Exception:
        tok = raw
    return {
        "location_id": current_user.location_id,
        "email": current_user.email,
        "access_token": tok,
        "calendar_id": getattr(current_user, "calendar_id", "") or "",
        "timezone": getattr(current_user, "timezone", "America/Chicago") or "America/Chicago",
        "subscription_tier": getattr(current_user, "subscription_tier", "individual") or "individual",
        "bot_first_name": getattr(current_user, "bot_first_name", "") or "",
        "crm_user_id": getattr(current_user, "crm_user_id", "") or "",
    }


def _format_stats(result):
    if "error" in result: return result["error"]
    return (f"**Today:** {result.get('total_calls', 0)} calls, {result.get('connected', 0)} connected "
            f"({result.get('connect_rate', '0%')}), {result.get('talk_time', '0m')} talk time.")


def _format_daily_summary(result):
    if "error" in result: return result["error"]
    return (f"**Daily Summary:**\n"
            f"- Calls: {result.get('calls_made', 0)} made, {result.get('calls_connected', 0)} connected ({result.get('connect_rate', '0%')})\n"
            f"- Talk time: {result.get('talk_time', '0m')}\n"
            f"- Missed calls: {result.get('missed_calls', 0)}\n"
            f"- Texts sent: {result.get('texts_sent', 0)}, received: {result.get('texts_received', 0)}\n"
            f"- Hot leads: {result.get('hot_leads', 0)}")


def _is_support_intent(message: str) -> bool:
    """Detect whether the user's message is a support/troubleshooting question."""
    lower = message.lower()
    for keyword in _SUPPORT_KEYWORDS:
        if keyword in lower:
            return True
    return False


def _get_support_tools_for_assistant():
    """Get support tool definitions adapted for authenticated dashboard use.

    Modifies the support tools to NOT require an email parameter (we inject
    it automatically from the authenticated session).
    """
    from support_tools import get_support_tool_definitions

    adapted = []
    for tool in get_support_tool_definitions():
        func = tool.get("function", {})
        name = func.get("name", "")

        # Prefix support tool names to avoid collisions
        adapted_tool = {
            "type": "function",
            "function": {
                "name": f"support_{name}",
                "description": func.get("description", ""),
                "parameters": dict(func.get("parameters", {})),
            }
        }

        # Remove email from required params — we inject it from session
        params = adapted_tool["function"]["parameters"]
        props = params.get("properties", {})
        if "email" in props:
            props["email"]["description"] = "(Auto-filled from your account — leave empty)"

        req = params.get("required", [])
        if "email" in req:
            params["required"] = [r for r in req if r != "email"]

        adapted.append(adapted_tool)

    return adapted


def _filter_tools(all_tools, message):
    """Select relevant tools based on message keywords to reduce token count.
    Returns max ~15 tools instead of all 50."""
    lower = message.lower()

    # Always include these core tools
    core = {"search_contact", "navigate_dashboard", "get_call_stats"}

    # Keyword → tool name mappings
    _KEYWORD_TOOLS = {
        "call": {"make_call", "query_call_history", "queue_dial_session", "search_contact"},
        "dial": {"make_call", "queue_dial_session", "search_contact"},
        "text": {"send_sms", "send_bulk_sms", "get_recent_messages", "search_contact"},
        "sms": {"send_sms", "send_bulk_sms", "get_recent_messages", "search_contact"},
        "message": {"send_sms", "get_recent_messages", "get_inbox_conversations", "search_contact"},
        "book": {"book_appointment", "check_calendar", "get_upcoming_appointments", "search_contact"},
        "appointment": {"book_appointment", "check_calendar", "get_upcoming_appointments"},
        "calendar": {"check_calendar", "get_upcoming_appointments", "book_appointment"},
        "schedule": {"get_upcoming_appointments", "book_appointment", "set_reminder"},
        "remind": {"set_reminder", "list_reminders"},
        "workflow": {"list_workflows", "assign_contact_to_workflow", "assign_bulk_to_workflow", "toggle_workflow", "create_workflow_ai"},
        "automat": {"list_workflows", "toggle_workflow", "create_workflow_ai"},
        "contact": {"search_contact", "edit_contact", "add_contact_tag", "add_contact_note", "create_contact", "get_contact_history"},
        "tag": {"add_contact_tag", "remove_contact_tag", "search_contact"},
        "note": {"add_contact_note", "search_contact"},
        "pipeline": {"move_contact_pipeline", "get_pipeline_summary", "queue_dial_session"},
        "stage": {"move_contact_pipeline", "get_pipeline_summary"},
        "carrier": {"set_carriers", "list_carriers"},
        "hot": {"get_hot_leads"},
        "warm": {"get_hot_leads"},
        "cold": {"get_hot_leads", "get_stale_leads"},
        "stale": {"get_stale_leads"},
        "follow up": {"get_stale_leads", "set_reminder"},
        "team": {"list_team_members", "invite_team_member", "get_agent_performance"},
        "agent": {"get_agent_performance", "get_agency_kpis", "list_agency_members"},
        "agency": {"get_agency_kpis", "get_agent_performance", "get_agency_call_log", "get_agency_leaderboard", "list_agency_members"},
        "leaderboard": {"get_agency_leaderboard"},
        "billing": {"get_subscription_info"},
        "plan": {"get_subscription_info"},
        "minute": {"get_ai_minutes_balance"},
        "number": {"list_phone_numbers"},
        "phone": {"list_phone_numbers", "make_call", "search_contact"},
        "record": {"search_recordings", "query_call_history", "get_recording_transcript", "transcribe_recording", "get_agent_recordings"},
        "inbox": {"get_inbox_conversations", "get_recent_messages"},
        "log": {"get_activity_logs"},
        "summary": {"get_daily_summary", "get_call_stats"},
        "stat": {"get_call_stats", "get_daily_summary"},
        "dnc": {"mark_do_not_contact", "search_contact"},
        "do not": {"mark_do_not_contact", "search_contact"},
        "setting": {"get_bot_config", "update_bot_config"},
        "config": {"get_bot_config", "update_bot_config"},
        "transcript": {"get_recording_transcript", "transcribe_recording"},
        "cancel": {"cancel_appointment"},
        "reschedule": {"reschedule_appointment"},
        "deal": {"get_contact_deals"},
        "opportunit": {"get_contact_deals", "get_pipeline_summary"},
        "sync": {"trigger_crm_sync", "get_sync_status"},
        "report": {"generate_weekly_report", "get_daily_summary"},
        "compar": {"compare_periods", "compare_agents"},
        "funnel": {"get_conversion_funnel"},
        "conversion": {"get_conversion_funnel"},
        "coach": {"generate_agent_coaching"},
        "improve": {"generate_agent_coaching"},
        "registr": {"check_registration_status"},
        "spam": {"check_number_health", "check_registration_status"},
        "health": {"check_number_health"},
        "onboard": {"get_onboarding_status"},
        "setup": {"get_onboarding_status"},
        "search": {"search_everything", "search_contact"},
        "find": {"search_everything", "search_contact"},
        "integrat": {"check_integrations_status"},
        "discord": {"check_integrations_status"},
        "slack": {"check_integrations_status"},
        "train": {"get_training_status"},
        "analyz": {"analyze_contact_now", "get_intelligence_summary"},
        "reanalyz": {"analyze_contact_now"},
        "overview": {"get_intelligence_summary", "get_pipeline_summary"},
        "usage": {"get_ai_minutes_usage"},
        "who": {"get_hot_leads", "get_stale_leads", "get_recent_messages", "query_call_history", "search_contact"},
    }

    matched = set(core)
    for keyword, tools in _KEYWORD_TOOLS.items():
        if keyword in lower:
            matched.update(tools)

    # If nothing matched beyond core, send all tools (fallback for unusual queries)
    if len(matched) <= len(core):
        return all_tools

    # Filter tool list
    filtered = [t for t in all_tools if t["function"]["name"] in matched]
    return filtered if filtered else all_tools


def _match_nav(dest: str):
    """Match a destination string to a tab ID (or tab:subpanel)."""
    # Exact match
    if dest in _NAV_KEYWORDS:
        return _NAV_KEYWORDS[dest]
    # Substring match
    for key, tid in _NAV_KEYWORDS.items():
        if key in dest or dest in key:
            return tid
    return None


def _build_nav_response(dest: str, nav: str):
    """Build a navigation response dict, handling tab:subpanel format."""
    if ":" in nav:
        tab_id, sub_panel = nav.split(":", 1)
        return {"text": f"Opening {dest}.", "navigate": tab_id, "sub_panel": sub_panel}
    return {"text": f"Opening {dest}.", "navigate": nav}


# ── Rate limiter (per-user, Redis sliding window) ────────────────────────────

def _is_rate_limited(email: str) -> bool:
    """Check if user has exceeded 30 requests/minute."""
    try:
        from extensions import ensure_redis
        r = ensure_redis()
        if not r:
            return False
        key = f"assistant_rate:{email}:min"
        count = r.incr(key)
        if count == 1:
            r.expire(key, 60)
        return count > _RATE_LIMIT_RPM
    except Exception:
        return False


# ── Main chat endpoint ───────────────────────────────────────────────────────

@assistant_bp.route("/api/assistant/chat", methods=["POST"])
@login_required
def assistant_chat():
    """
    Dashboard AI assistant with tool-calling capabilities.
    Receives: {message: str, history: [{role, content}], error_context?: {url, status, body}}
    Returns: {text: str, navigate?: str}
    """
    from openai import OpenAI
    from assistant_prompt import build_assistant_prompt
    from assistant_tools import get_assistant_tool_definitions, execute_assistant_tool
    from token_encryption import decrypt_token

    payload = request.get_json(silent=True) or {}
    user_message = (payload.get("message") or "").strip()
    history = payload.get("history", [])
    error_context = payload.get("error_context")

    if not user_message:
        return flask_jsonify({"error": "No message provided"}), 400

    # Rate limit
    if _is_rate_limited(current_user.email):
        return flask_jsonify({"text": "You're sending messages too quickly. Give it a moment."}), 429

    # ── Fast path: handle common requests without LLM ─────────────
    fast = _try_fast_path(user_message)
    if fast is not None:
        return flask_jsonify(fast)

    # Handle consent-approved support write actions (e.g. fix registration)
    if user_message.startswith("APPROVE_ACTION:"):
        from support_tools import execute_approved_action
        action_id = user_message.split(":", 1)[1].strip()
        result = execute_approved_action(action_id, {
            "email": current_user.email,
            "location_id": current_user.location_id,
            "conversation_log": history,
        })
        if result.get("success"):
            return flask_jsonify({"text": result.get("message", "Done!")})
        else:
            return flask_jsonify({"text": result.get("error", "Something went wrong. Please try again.")})

    # Build user context from authenticated session
    raw_token = getattr(current_user, "access_token", "") or ""
    try:
        access_token = decrypt_token(raw_token) if raw_token else ""
    except Exception:
        access_token = raw_token

    user_ctx = {
        "location_id": current_user.location_id,
        "email": current_user.email,
        "access_token": access_token,
        "calendar_id": getattr(current_user, "calendar_id", "") or "",
        "timezone": getattr(current_user, "timezone", "America/Chicago") or "America/Chicago",
        "subscription_tier": getattr(current_user, "subscription_tier", "individual") or "individual",
        "bot_first_name": getattr(current_user, "bot_first_name", "") or getattr(current_user, "full_name", "") or "",
        "crm_user_id": getattr(current_user, "crm_user_id", "") or "",
    }

    # Detect support intent from message (or recent history)
    support_mode = _is_support_intent(user_message)
    if not support_mode:
        # Also check last 3 history messages for ongoing support conversation
        for msg in history[-3:]:
            if msg.get("role") == "user" and _is_support_intent(msg.get("content", "")):
                support_mode = True
                break

    # Build system prompt
    system_prompt = build_assistant_prompt(user_ctx, error_context, support_mode=support_mode)

    # Build messages array
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history[-20:]:
        role = msg.get("role", "user")
        if role not in ("user", "assistant"):
            role = "user"
        content = msg.get("content", "")
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    # Tool definitions — filter to relevant subset for faster processing
    tools = _filter_tools(get_assistant_tool_definitions(), user_message)

    # Merge support tools when support intent is detected
    if support_mode:
        tools = tools + _get_support_tools_for_assistant()

    # LLM client
    xai_key = os.getenv("XAI_API_KEY")
    if not xai_key:
        logger.error("XAI_API_KEY not set — assistant cannot respond")
        return flask_jsonify({"text": "Assistant is temporarily unavailable. Please try again shortly."})

    client = OpenAI(api_key=xai_key, base_url="https://api.x.ai/v1")
    reply = ""
    navigate_tab = None
    navigate_sub_panel = None

    # ── Multi-turn tool-calling loop ──────────────────────────────
    try:
        for _round in range(_ASSISTANT_MAX_TOOL_ROUNDS):
            response = client.chat.completions.create(
                model=_ASSISTANT_MODEL,
                messages=messages,
                tools=tools,
                temperature=0.3,
                max_tokens=300,
            )

            msg = response.choices[0].message

            if msg.tool_calls:
                messages.append(msg)

                for tool_call in msg.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        tool_args = json.loads(tool_call.function.arguments)
                    except (json.JSONDecodeError, TypeError):
                        tool_args = {}

                    logger.info(f"Assistant tool: {tool_name}({list(tool_args.keys())})")

                    # Route support_* tools to support tool executor
                    if tool_name.startswith("support_"):
                        from support_tools import execute_support_tool
                        from support_bot import sanitize_support_reply
                        real_name = tool_name[len("support_"):]
                        # Auto-inject email from authenticated session
                        if "email" not in tool_args or not tool_args["email"]:
                            tool_args["email"] = current_user.email
                        support_ctx = {
                            "email": current_user.email,
                            "location_id": current_user.location_id,
                            "has_consent": True,  # Authenticated user = implicit consent
                            "conversation_log": history,
                        }
                        tool_result = execute_support_tool(real_name, tool_args, support_ctx)
                    else:
                        tool_result = execute_assistant_tool(tool_name, tool_args, user_ctx)

                    # Check for frontend actions
                    if isinstance(tool_result, dict):
                        action = tool_result.get("action")
                        if action == "navigate":
                            navigate_tab = tool_result.get("tab_id")
                            navigate_sub_panel = tool_result.get("sub_panel")
                        elif action == "ask_call_mode":
                            # Return choice buttons — frontend renders them
                            return flask_jsonify({
                                "text": tool_result.get("message"),
                                "call_choice": {
                                    "contact_id": tool_result.get("contact_id"),
                                    "phone": tool_result.get("phone"),
                                    "first_name": tool_result.get("first_name"),
                                },
                            })
                        elif action == "call":
                            return flask_jsonify({
                                "text": tool_result.get("message", "Calling now..."),
                                "call": {
                                    "contact_id": tool_result.get("contact_id"),
                                    "phone": tool_result.get("phone"),
                                    "first_name": tool_result.get("first_name"),
                                    "dial_mode": tool_result.get("dial_mode", "ai"),
                                },
                            })
                        elif action == "dial_queue":
                            return flask_jsonify({
                                "text": tool_result.get("message"),
                                "dial_queue": {
                                    "contacts": tool_result.get("contacts", []),
                                    "dial_mode": tool_result.get("dial_mode", "ai"),
                                    "pipeline_name": tool_result.get("pipeline_name"),
                                    "stage_name": tool_result.get("stage_name"),
                                },
                            })

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(tool_result, default=str),
                    })
            else:
                reply = (msg.content or "").strip()
                break

    except Exception as e:
        logger.error(f"Assistant loop failed: {type(e).__name__}: {e}", exc_info=True)
        reply = ""

    if not reply:
        reply = "Something went wrong on my end. Try again in a moment."

    # Sanitize support replies to strip forbidden technical terms
    if support_mode:
        from support_bot import sanitize_support_reply, extract_options, extract_redirect
        reply = sanitize_support_reply(reply)

        # Parse [OPTIONS:...] from support-style responses
        options, reply = extract_options(reply)

        # Parse [REDIRECT:...] from support-style responses
        redirect_url, reply = extract_redirect(reply)

    # Extract [NAVIGATE:tab_id] from reply text
    nav_match = _extract_navigate(reply)
    if nav_match:
        navigate_tab = nav_match
        reply = reply.replace(f"[NAVIGATE:{nav_match}]", "").strip()

    result = {"text": reply}
    if navigate_tab:
        result["navigate"] = navigate_tab
    if navigate_sub_panel:
        result["sub_panel"] = navigate_sub_panel
    if support_mode:
        if options:
            result["options"] = options
        if redirect_url:
            result["redirect"] = redirect_url

    return flask_jsonify(result)


def _extract_navigate(text: str):
    """Extract [NAVIGATE:tab_id] tag from text."""
    import re
    match = re.search(r'\[NAVIGATE:(\w+)\]', text)
    return match.group(1) if match else None
