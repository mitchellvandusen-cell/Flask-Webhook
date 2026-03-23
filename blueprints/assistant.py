# blueprints/assistant.py — In-dashboard AI Agent Assistant
#
# Single endpoint: POST /api/assistant/chat
# Authenticated via @login_required, CSRF exempt.
# Multi-turn tool-calling loop with grok-3-mini-fast.
#
# The assistant can search contacts, book appointments, send SMS,
# check stats, navigate tabs, and troubleshoot errors.

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
            tab_id = _match_nav(dest)
            if tab_id:
                return {"text": f"Opening {dest}.", "navigate": tab_id}

    # Direct tab name match (user just types "billing", "dialer", etc.)
    tab_id = _match_nav(lower)
    if tab_id and len(lower.split()) <= 3:
        return {"text": f"Opening {lower}.", "navigate": tab_id}

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


def _match_nav(dest: str):
    """Match a destination string to a tab ID."""
    # Exact match
    if dest in _NAV_KEYWORDS:
        return _NAV_KEYWORDS[dest]
    # Substring match
    for key, tid in _NAV_KEYWORDS.items():
        if key in dest or dest in key:
            return tid
    return None


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

    # Build system prompt
    system_prompt = build_assistant_prompt(user_ctx, error_context)

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

    # Tool definitions
    tools = get_assistant_tool_definitions()

    # LLM client
    xai_key = os.getenv("XAI_API_KEY")
    if not xai_key:
        logger.error("XAI_API_KEY not set — assistant cannot respond")
        return flask_jsonify({"text": "Assistant is temporarily unavailable. Please try again shortly."})

    client = OpenAI(api_key=xai_key, base_url="https://api.x.ai/v1")
    reply = ""
    navigate_tab = None

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

                    tool_result = execute_assistant_tool(tool_name, tool_args, user_ctx)

                    # Check for frontend actions
                    if isinstance(tool_result, dict):
                        action = tool_result.get("action")
                        if action == "navigate":
                            navigate_tab = tool_result.get("tab_id")
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

    # Extract [NAVIGATE:tab_id] from reply text
    nav_match = _extract_navigate(reply)
    if nav_match:
        navigate_tab = nav_match
        reply = reply.replace(f"[NAVIGATE:{nav_match}]", "").strip()

    result = {"text": reply}
    if navigate_tab:
        result["navigate"] = navigate_tab

    return flask_jsonify(result)


def _extract_navigate(text: str):
    """Extract [NAVIGATE:tab_id] tag from text."""
    import re
    match = re.search(r'\[NAVIGATE:(\w+)\]', text)
    return match.group(1) if match else None
