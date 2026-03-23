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
_ASSISTANT_MAX_TOOL_ROUNDS = 5
_ASSISTANT_MAX_TOKENS = 600
_RATE_LIMIT_RPM = 30


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
                temperature=0.5,
                max_tokens=_ASSISTANT_MAX_TOKENS,
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

                    # Check for navigation action
                    if isinstance(tool_result, dict) and tool_result.get("action") == "navigate":
                        navigate_tab = tool_result.get("tab_id")

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
