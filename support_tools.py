# support_tools.py — Grok function-calling tools for the autonomous support agent
#
# Provides:
#   get_support_tool_definitions()  — OpenAI-compatible tool schemas for Grok
#   execute_support_tool(name, args, user_context) — Runs a tool, returns result dict
#
# Tools are split into READ (no consent needed) and WRITE (consent required).
# Write actions use a whitelist — anything not on the list is denied.
# All write actions are logged to support_actions_log for audit.

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Knowledge library path ──────────────────────────────────────────────────
KNOWLEDGE_DIR = Path(__file__).parent / "support_knowledge"

# ── Write action whitelist ──────────────────────────────────────────────────
ALLOWED_WRITE_ACTIONS = {
    "resubmit_voice_integrity",
    "submit_voice_integrity",
    "update_cnam",
    "submit_cnam",
    "resubmit_a2p_brand",
    "update_profile_info",
}

DENIED_ACTIONS = {
    "buy_number", "release_number", "delete_number",
    "create_sub_account", "delete_sub_account", "suspend_account",
    "send_sms", "make_call",
    "modify_billing", "cancel_subscription", "change_plan",
    "delete_data", "modify_code", "expose_secrets",
}

# ── SID/secret scrubber ─────────────────────────────────────────────────────
_SECRET_PATTERNS = [
    (re.compile(r'AC[a-f0-9]{32}'), '[ACCOUNT_ID]'),
    (re.compile(r'SK[a-f0-9]{32}'), '[API_KEY]'),
    (re.compile(r'BU[a-f0-9]{32}'), '[PROFILE_ID]'),
    (re.compile(r'IT[a-f0-9]{32}'), '[ENTITY_ID]'),
    (re.compile(r'RN[a-f0-9]{32}'), '[POLICY_ID]'),
    (re.compile(r'BN[a-f0-9]{32}'), '[BRAND_ID]'),
    (re.compile(r'QE[a-f0-9]{32}'), '[CAMPAIGN_ID]'),
    (re.compile(r'MG[a-f0-9]{32}'), '[SERVICE_ID]'),
    (re.compile(r'PN[a-f0-9]{32}'), '[NUMBER_ID]'),
    (re.compile(r'auth_token[\s:="\']+[a-f0-9]{32}', re.IGNORECASE), 'auth_token=[REDACTED]'),
    (re.compile(r'[a-f0-9]{32}(?=[a-f0-9]{0,2}[^a-f0-9])'), '[REDACTED]'),
]


def _scrub_secrets(data):
    """Remove SIDs, auth tokens, and secrets from tool results before returning to Grok."""
    text = json.dumps(data) if isinstance(data, (dict, list)) else str(data)
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


# ── Tool definitions (OpenAI-compatible) ────────────────────────────────────

def get_support_tool_definitions():
    """Return tool definitions for the xAI Grok chat completions API."""
    return [
        {
            "type": "function",
            "function": {
                "name": "lookup_account",
                "description": "Look up a customer's account status including subscription, CRM connection, onboarding progress, bot configuration, and recent errors. Requires the customer's email address.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "description": "The customer's email address"
                        }
                    },
                    "required": ["email"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "check_registrations",
                "description": "Check the customer's phone system registration status: carrier text registration (A2P), spam protection (Voice Integrity), caller ID registration (CNAM), and business profile status. Call this when a customer reports issues with text messages not going through, calls showing as spam, or caller ID not displaying correctly.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "description": "The customer's email address"
                        }
                    },
                    "required": ["email"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_error_logs",
                "description": "Read recent error logs for a specific customer. Shows what went wrong with their webhooks, messages, or integrations in the last few days. Use this to diagnose specific failures.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "description": "The customer's email address"
                        },
                        "days": {
                            "type": "integer",
                            "description": "Number of days of logs to retrieve (max 7, default 3)",
                            "default": 3
                        }
                    },
                    "required": ["email"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_server_logs",
                "description": "Read recent platform server logs to diagnose system-wide issues. Use this when the customer's issue might be caused by a platform-level problem rather than their specific account configuration.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "enum": ["flask", "worker", "all"],
                            "description": "Which server logs to read: 'flask' for web server, 'worker' for background processing, 'all' for both",
                            "default": "all"
                        },
                        "lines": {
                            "type": "integer",
                            "description": "Number of recent log lines (max 200, default 100)",
                            "default": 100
                        }
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_knowledge",
                "description": "Search the platform knowledge base for information about features, troubleshooting steps, API documentation, error codes, or codebase details. Use this to find specific technical information to help the customer.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to search for, e.g. 'A2P registration rejected', 'calendar booking errors', 'CRM token refresh'"
                        },
                        "category": {
                            "type": "string",
                            "enum": ["troubleshooting", "product", "api_docs", "error_codes", "codebase", "all"],
                            "description": "Which knowledge category to search",
                            "default": "all"
                        }
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "fix_registration",
                "description": "Fix a customer's phone system registration issue. This is a WRITE action that requires customer consent. Supported actions: resubmit spam protection, update caller ID, resubmit text message registration, update business profile. ALWAYS explain the issue and proposed fix to the customer BEFORE calling this tool.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "description": "The customer's email address"
                        },
                        "action": {
                            "type": "string",
                            "enum": [
                                "resubmit_voice_integrity",
                                "submit_voice_integrity",
                                "update_cnam",
                                "submit_cnam",
                                "resubmit_a2p_brand",
                                "update_profile_info"
                            ],
                            "description": "The specific fix action to take"
                        },
                        "params": {
                            "type": "object",
                            "description": "Action-specific parameters (e.g. business_name for update_cnam, corrected fields for update_profile_info)",
                            "additionalProperties": True
                        }
                    },
                    "required": ["email", "action"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_ticket",
                "description": "Create a support ticket to escalate an issue to the human support team. Use this when: (1) the issue cannot be resolved autonomously, (2) it requires code changes, (3) it involves billing disputes, (4) it's a bug you've identified but cannot fix, or (5) the customer explicitly asks to speak to a human.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "description": "The customer's email address"
                        },
                        "summary": {
                            "type": "string",
                            "description": "Clear, concise summary of the issue"
                        },
                        "category": {
                            "type": "string",
                            "enum": ["setup", "billing", "bot_behavior", "voice", "crm", "technical", "feature_request"],
                            "description": "Issue category"
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["low", "medium", "high", "critical"],
                            "description": "Issue severity: low=minor inconvenience, medium=has workaround, high=feature broken, critical=platform unusable"
                        },
                        "technical_details": {
                            "type": "string",
                            "description": "Internal technical details for the engineering team (error codes, log excerpts, affected code paths). This is NOT shown to the customer."
                        }
                    },
                    "required": ["summary", "category", "severity"]
                }
            }
        },
    ]


# ── Tool executors ──────────────────────────────────────────────────────────

def execute_support_tool(name: str, args: dict, user_context: dict) -> dict:
    """Execute a support tool and return the result.

    Args:
        name: Tool name (must match a definition above).
        args: Arguments from the Grok tool call.
        user_context: Dict with 'email', 'location_id', 'sub_account_sid',
                      'sub_account_auth_token', 'has_consent' keys.

    Returns:
        Dict with tool results (scrubbed of secrets).
    """
    executors = {
        "lookup_account": _exec_lookup_account,
        "check_registrations": _exec_check_registrations,
        "read_error_logs": _exec_read_error_logs,
        "read_server_logs": _exec_read_server_logs,
        "search_knowledge": _exec_search_knowledge,
        "fix_registration": _exec_fix_registration,
        "create_ticket": _exec_create_ticket,
    }

    executor = executors.get(name)
    if not executor:
        return {"error": f"Unknown tool: {name}"}

    try:
        result = executor(args, user_context)
        return _scrub_secrets(result)
    except Exception as e:
        logger.error(f"Support tool '{name}' failed: {e}", exc_info=True)
        return {"error": "Tool execution failed. Please try again or create a ticket."}


# ── Individual tool implementations ─────────────────────────────────────────

def _exec_lookup_account(args: dict, ctx: dict) -> dict:
    """Full account diagnostics."""
    from support_bot import run_support_diagnostics

    email = args.get("email", "").strip().lower()
    if not email:
        return {"error": "Email address is required."}

    # Tenant isolation: if we already have a consented user, only allow their email
    if ctx.get("email") and ctx["email"].lower() != email:
        return {"error": "You can only look up the account of the customer you're chatting with."}

    result = run_support_diagnostics(email)

    # Enhance with voice config status
    if not result.get("not_found"):
        _enrich_with_voice_status(result, email)

    return result


def _enrich_with_voice_status(result: dict, email: str):
    """Add voice/dialer status to account diagnostics."""
    from db_legacy import get_db_connection, return_db_connection

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return
        cur = conn.cursor()
        cur.execute(
            "SELECT voice_config, subscription_tier FROM subscribers WHERE LOWER(email) = %s",
            (email.lower(),)
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return

        vc = row.get("voice_config") or {}
        tier = row.get("subscription_tier", "")

        # Phone numbers
        has_number = bool(vc.get("twilio_phone_number"))
        result["phone_numbers"] = "Has primary number configured" if has_number else "NO PHONE NUMBER — needs to buy one in the Numbers tab"

        # Sub-account
        has_sub = bool(vc.get("twilio_sub_account_sid"))
        result["phone_system"] = "Connected and active" if has_sub else "NOT SET UP — phone system not provisioned"

        # A2P status
        a2p = vc.get("a2p", {})
        if a2p.get("campaign_status", "").upper() in ("VERIFIED", "APPROVED"):
            result["text_registration"] = "Approved — text messages cleared for sending"
        elif a2p.get("brand_status", "").upper() == "APPROVED":
            result["text_registration"] = "Brand approved but campaign pending — not fully cleared yet"
        elif a2p.get("brand_sid"):
            result["text_registration"] = f"Brand submitted, status: {a2p.get('brand_status', 'unknown')}"
        else:
            result["text_registration"] = "NOT REGISTERED — needs to register in the Numbers tab under A2P Registration"

        # Voice Integrity
        ni = vc.get("number_integrity", {})
        if ni.get("status") == "twilio-approved":
            result["spam_protection"] = "Active — registered with carriers"
        elif ni.get("status") == "pending-review":
            result["spam_protection"] = "Pending review — submitted, waiting for carrier approval (24-48 hours)"
        elif ni.get("status") == "twilio-rejected":
            result["spam_protection"] = "REJECTED — registration was denied, needs to be resubmitted"
        elif ni.get("trust_product_sid"):
            result["spam_protection"] = f"In progress — status: {ni.get('status', 'unknown')}"
        else:
            result["spam_protection"] = "NOT REGISTERED — needs to set up in the Numbers tab under Spam Protection"

        # Tier
        tier_names = {
            "sms_bot": "SMS Bot ($99.98/mo)",
            "individual": "Power Dialer ($149.98/mo)",
            "pro_dialer": "Pro Dialer ($224.98/mo)",
            "solo_predictive": "Predictive Dialer ($349.98/mo)",
        }
        result["plan_tier"] = tier_names.get(tier, tier or "None")

    except Exception as e:
        logger.error(f"Voice status enrichment failed: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def _exec_check_registrations(args: dict, ctx: dict) -> dict:
    """Check Trust Hub, A2P, Voice Integrity, CNAM status."""
    email = args.get("email", "").strip().lower()
    if not email:
        return {"error": "Email address is required."}
    if ctx.get("email") and ctx["email"].lower() != email:
        return {"error": "You can only check registrations for the customer you're chatting with."}

    # Get Twilio credentials from subscriber record
    creds = _get_twilio_creds(email)
    if not creds:
        return {"error": "Customer does not have a phone system set up yet. They need to complete onboarding first."}

    sub_sid, auth_token, voice_config = creds
    result = {}

    # 1. Secondary Customer Profile
    try:
        from twilio_provisioning import check_secondary_profile_status
        profile = check_secondary_profile_status(sub_sid, auth_token)
        if profile.get("approved"):
            result["business_profile"] = "Approved and active"
        else:
            result["business_profile"] = f"Status: {profile.get('status', 'unknown')} — {profile.get('message', '')}"
        result["business_profile_details"] = profile.get("message", "")
    except Exception as e:
        result["business_profile"] = f"Could not check — {_safe_error(e)}"

    # 2. A2P 10DLC
    try:
        from twilio_provisioning import discover_full_a2p_status
        a2p = discover_full_a2p_status(sub_sid, auth_token)

        if a2p.get("best_brand"):
            brand = a2p["best_brand"]
            result["text_registration_brand"] = f"Status: {brand.get('status', 'unknown')}"
            if brand.get("status", "").upper() == "FAILED":
                result["text_registration_brand"] += " — FAILED. May need business name/EIN correction."
        else:
            result["text_registration_brand"] = "No brand registered — text messages may be filtered by carriers"

        if a2p.get("best_campaign"):
            campaign = a2p["best_campaign"]
            result["text_registration_campaign"] = f"Status: {campaign.get('campaign_status', 'unknown')}"
        else:
            result["text_registration_campaign"] = "No campaign registered"

        result["messaging_services_count"] = len(a2p.get("messaging_services", []))
    except Exception as e:
        result["text_registration"] = f"Could not check — {_safe_error(e)}"

    # 3. Voice Integrity
    ni = voice_config.get("number_integrity", {})
    tp_sid = ni.get("trust_product_sid")
    if tp_sid:
        try:
            from twilio_provisioning import get_voice_integrity_status
            vi = get_voice_integrity_status(sub_sid, tp_sid, auth_token)
            status = vi.get("status", "unknown")
            result["spam_protection_status"] = _translate_trust_status(status)
            result["spam_protection_numbers_count"] = vi.get("assigned_count", 0)
            if vi.get("failure_reasons"):
                result["spam_protection_rejection_reasons"] = vi["failure_reasons"]
        except Exception as e:
            result["spam_protection"] = f"Could not check — {_safe_error(e)}"
    else:
        result["spam_protection_status"] = "Not set up — no spam protection registration found"

    # 4. CNAM
    try:
        from twilio_provisioning import get_spam_protection_status
        spam = get_spam_protection_status(sub_sid)
        total = spam.get("numbers_total", 0)
        protected = spam.get("numbers_protected", 0)
        result["caller_id"] = f"{protected} of {total} numbers have caller ID name set"
        if total > 0 and protected == 0:
            result["caller_id"] += " — caller ID not configured, calls may show as unknown"
    except Exception as e:
        result["caller_id"] = f"Could not check — {_safe_error(e)}"

    # 5. Phone numbers
    try:
        from twilio_provisioning import list_phone_numbers
        numbers = list_phone_numbers(sub_sid)
        result["phone_numbers_count"] = len(numbers)
        if numbers:
            result["phone_numbers_list"] = [
                {"number": n.get("phone_number", ""), "friendly_name": n.get("friendly_name", "")}
                for n in numbers[:10]
            ]
    except Exception as e:
        result["phone_numbers"] = f"Could not list — {_safe_error(e)}"

    return result


def _exec_read_error_logs(args: dict, ctx: dict) -> dict:
    """Read recent error logs for a user."""
    email = args.get("email", "").strip().lower()
    if not email:
        return {"error": "Email address is required."}
    if ctx.get("email") and ctx["email"].lower() != email:
        return {"error": "You can only read logs for the customer you're chatting with."}

    days = min(args.get("days", 3), 7)
    location_id = _get_location_id(email)
    if not location_id:
        return {"error": "Customer account not found."}

    from db_legacy import get_db_connection, return_db_connection

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return {"error": "Could not connect to database."}

        cur = conn.cursor()
        cur.execute("""
            SELECT event_type, status, details, created_at
            FROM webhook_logs
            WHERE location_id = %s
              AND created_at > NOW() - make_interval(days => %s)
            ORDER BY created_at DESC
            LIMIT 50
        """, (location_id, days))
        rows = cur.fetchall()
        cur.close()

        if not rows:
            return {"message": f"No activity logs found in the last {days} days.", "log_count": 0}

        errors = []
        successes = 0
        for row in rows:
            status = row.get("status", "")
            if status == "error":
                details = row.get("details") or {}
                if isinstance(details, str):
                    try:
                        details = json.loads(details)
                    except Exception:
                        details = {"raw": details[:200]}

                errors.append({
                    "event": row.get("event_type", "unknown"),
                    "when": row["created_at"].isoformat() if row.get("created_at") else "",
                    "reason": details.get("failure_reason", "unknown"),
                    "detail": str(details.get("error", details.get("response_body", "")))[:200],
                })
            else:
                successes += 1

        return {
            "total_events": len(rows),
            "successful_events": successes,
            "error_count": len(errors),
            "errors": errors[:20],
            "period_days": days,
        }

    except Exception as e:
        logger.error(f"Error reading user logs: {e}")
        return {"error": "Could not read logs."}
    finally:
        if conn:
            return_db_connection(conn)


def _exec_read_server_logs(args: dict, ctx: dict) -> dict:
    """Read cached Railway server logs."""
    from db_legacy import get_db_connection, return_db_connection

    source = args.get("source", "all")
    lines = min(args.get("lines", 100), 200)

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return {"error": "Could not connect to database."}

        cur = conn.cursor()
        if source == "all":
            cur.execute("""
                SELECT source, log_content, captured_at
                FROM support_log_cache
                WHERE location_id IS NULL
                ORDER BY captured_at DESC
                LIMIT 5
            """)
        else:
            source_key = f"railway_{source}"
            cur.execute("""
                SELECT source, log_content, captured_at
                FROM support_log_cache
                WHERE source = %s AND location_id IS NULL
                ORDER BY captured_at DESC
                LIMIT 3
            """, (source_key,))

        rows = cur.fetchall()
        cur.close()

        if not rows:
            return {"message": "No server logs cached yet. Logs are refreshed periodically."}

        # Combine log content, limit to requested lines
        combined = []
        for row in rows:
            content = row.get("log_content", "")
            captured = row["captured_at"].isoformat() if row.get("captured_at") else ""
            combined.append(f"--- {row.get('source', '')} @ {captured} ---\n{content}")

        full_text = "\n".join(combined)
        log_lines = full_text.split("\n")
        truncated = "\n".join(log_lines[-lines:])

        return {
            "logs": truncated,
            "line_count": min(len(log_lines), lines),
            "sources": list(set(r.get("source", "") for r in rows)),
        }

    except Exception as e:
        logger.error(f"Error reading server logs: {e}")
        return {"error": "Could not read server logs."}
    finally:
        if conn:
            return_db_connection(conn)


def _exec_search_knowledge(args: dict, ctx: dict) -> dict:
    """Search the knowledge library files."""
    query = args.get("query", "").strip().lower()
    category = args.get("category", "all")
    if not query:
        return {"error": "Search query is required."}

    # Map categories to files
    category_files = {
        "troubleshooting": ["troubleshooting.md"],
        "product": ["product_guide.md"],
        "error_codes": ["error_codes.md"],
        "codebase": ["codebase_map.md"],
        "api_docs": [],  # all files in api_docs/
    }

    files_to_search = []
    if category == "all" or category == "api_docs":
        api_dir = KNOWLEDGE_DIR / "api_docs"
        if api_dir.exists():
            files_to_search.extend(api_dir.glob("*.md"))

    if category in ("all", "troubleshooting", "product", "error_codes", "codebase"):
        for cat, filenames in category_files.items():
            if category in ("all", cat):
                for fn in filenames:
                    fp = KNOWLEDGE_DIR / fn
                    if fp.exists():
                        files_to_search.append(fp)

    if not files_to_search:
        return {"message": "No knowledge base files found. The knowledge library may not be set up yet."}

    # Simple keyword search across files
    results = []
    query_words = query.split()

    for fpath in files_to_search:
        try:
            content = fpath.read_text(encoding="utf-8")
            lines = content.split("\n")

            # Find lines matching any query word
            matching_sections = []
            for i, line in enumerate(lines):
                line_lower = line.lower()
                if any(w in line_lower for w in query_words):
                    # Grab surrounding context (5 lines before/after)
                    start = max(0, i - 5)
                    end = min(len(lines), i + 6)
                    section = "\n".join(lines[start:end])
                    matching_sections.append(section)

            if matching_sections:
                # Deduplicate overlapping sections
                unique = []
                seen = set()
                for s in matching_sections[:5]:
                    key = s[:100]
                    if key not in seen:
                        seen.add(key)
                        unique.append(s)

                results.append({
                    "source": fpath.name,
                    "matches": unique[:3],
                })
        except Exception as e:
            logger.warning(f"Error reading knowledge file {fpath}: {e}")

    if not results:
        return {"message": f"No results found for '{query}' in the knowledge base."}

    return {"results": results, "query": query, "files_searched": len(files_to_search)}


def _exec_fix_registration(args: dict, ctx: dict) -> dict:
    """Execute a write action on Twilio Trust Hub (with consent gate)."""
    email = args.get("email", "").strip().lower()
    action = args.get("action", "")
    params = args.get("params", {})

    if not email:
        return {"error": "Email address is required."}
    if ctx.get("email") and ctx["email"].lower() != email:
        return {"error": "You can only fix registrations for the customer you're chatting with."}

    # Validate action against whitelist
    if action in DENIED_ACTIONS:
        return {"error": f"Action '{action}' is not permitted. This action is restricted for security."}
    if action not in ALLOWED_WRITE_ACTIONS:
        return {"error": f"Unknown action '{action}'. Supported actions: {', '.join(sorted(ALLOWED_WRITE_ACTIONS))}"}

    # Generate consent request — don't execute yet
    action_id = str(uuid.uuid4())[:8]
    action_descriptions = {
        "resubmit_voice_integrity": "Resubmit your spam protection registration for carrier review",
        "submit_voice_integrity": "Submit your spam protection registration for the first time",
        "update_cnam": "Update the caller ID display name for your phone number",
        "submit_cnam": "Submit your caller ID registration for review",
        "resubmit_a2p_brand": "Resubmit your business text messaging registration with corrected information",
        "update_profile_info": "Update your business profile information",
    }

    description = action_descriptions.get(action, action)

    # Store pending action in Redis
    try:
        from extensions import ensure_redis
        ensure_redis()
        from extensions import redis_conn
        if redis_conn:
            pending = {
                "action": action,
                "email": email,
                "params": params,
                "description": description,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            redis_conn.setex(
                f"support_action:{action_id}",
                300,  # 5-minute TTL
                json.dumps(pending),
            )
    except Exception as e:
        logger.error(f"Failed to store pending action in Redis: {e}")
        return {"error": "Could not prepare the action. Please try again."}

    return {
        "needs_consent": True,
        "action_id": action_id,
        "action_description": description,
        "message": f"I can {description.lower()}. Would you like me to go ahead?",
    }


def execute_approved_action(action_id: str, user_context: dict) -> dict:
    """Execute a previously consented write action.

    Called when user clicks 'Yes, Fix It' after seeing the consent prompt.
    """
    # Retrieve pending action from Redis
    try:
        from extensions import ensure_redis
        ensure_redis()
        from extensions import redis_conn
        if not redis_conn:
            return {"error": "Service temporarily unavailable."}

        raw = redis_conn.get(f"support_action:{action_id}")
        if not raw:
            return {"error": "This action has expired. Please describe the issue again and I'll set it up fresh."}

        pending = json.loads(raw)
        redis_conn.delete(f"support_action:{action_id}")
    except Exception as e:
        logger.error(f"Failed to retrieve pending action: {e}")
        return {"error": "Could not retrieve the action. Please try again."}

    action = pending["action"]
    email = pending["email"]
    params = pending.get("params", {})

    # Tenant isolation check
    if user_context.get("email") and user_context["email"].lower() != email.lower():
        return {"error": "Security check failed. Cannot execute action for a different account."}

    # Get Twilio credentials
    creds = _get_twilio_creds(email)
    if not creds:
        return {"error": "Could not find phone system credentials for this account."}

    sub_sid, auth_token, voice_config = creds

    # Execute the action
    result = _run_write_action(action, sub_sid, auth_token, voice_config, params)

    # Log to audit table
    _log_support_action(
        email=email,
        location_id=_get_location_id(email),
        sub_account_sid=sub_sid,
        action_type=action,
        action_params=params,
        action_result=result,
        consent_message=pending.get("description", ""),
        success=not result.get("error"),
    )

    return result


def _run_write_action(action: str, sub_sid: str, auth_token: str,
                      voice_config: dict, params: dict) -> dict:
    """Actually execute a whitelisted write action."""
    try:
        if action == "resubmit_voice_integrity":
            from twilio_provisioning import resubmit_voice_integrity
            ni = voice_config.get("number_integrity", {})
            tp_sid = ni.get("trust_product_sid")
            end_user_sid = ni.get("end_user_sid")
            if not tp_sid or not end_user_sid:
                return {"error": "No existing spam protection registration found to resubmit."}
            result = resubmit_voice_integrity(sub_sid, tp_sid, end_user_sid, auth_token)
            return {"success": True, "message": "Spam protection registration has been resubmitted for review. This typically takes 24-48 hours.", "details": result}

        elif action == "submit_voice_integrity":
            from twilio_provisioning import submit_voice_integrity_for_review
            ni = voice_config.get("number_integrity", {})
            tp_sid = ni.get("trust_product_sid")
            if not tp_sid:
                return {"error": "No spam protection registration found to submit."}
            result = submit_voice_integrity_for_review(sub_sid, tp_sid, auth_token)
            return {"success": True, "message": "Spam protection registration submitted for review. Expect approval within 24-48 hours.", "details": result}

        elif action == "update_cnam":
            from twilio_provisioning import update_cnam_for_number
            number_sid = params.get("number_sid")
            business_name = params.get("business_name", "")
            if not number_sid or not business_name:
                return {"error": "Phone number and business name are required to update caller ID."}
            result = update_cnam_for_number(sub_sid, number_sid, business_name)
            return {"success": True, "message": f"Caller ID updated to '{business_name[:15]}'. It may take a few hours to propagate to all carriers.", "details": result}

        elif action == "submit_cnam":
            from twilio_provisioning import submit_cnam_for_review
            ni = voice_config.get("number_integrity", {})
            cnam_sid = ni.get("cnam_trust_product_sid") or voice_config.get("cnam", {}).get("trust_product_sid")
            if not cnam_sid:
                return {"error": "No caller ID registration found to submit."}
            result = submit_cnam_for_review(sub_sid, cnam_sid, auth_token)
            return {"success": True, "message": "Caller ID registration submitted for review.", "details": result}

        elif action == "resubmit_a2p_brand":
            return {"error": "A2P brand resubmission requires additional business information. Please create a support ticket and our team will assist with the resubmission."}

        elif action == "update_profile_info":
            return {"error": "Business profile updates require manual verification. Please create a support ticket and our team will update your profile."}

        else:
            return {"error": f"Action '{action}' is not implemented."}

    except Exception as e:
        logger.error(f"Write action '{action}' failed: {e}")
        return {"error": f"The action failed: {_safe_error(e)}. Please try again or create a support ticket."}


def _exec_create_ticket(args: dict, ctx: dict) -> dict:
    """Create a support ticket."""
    from support_bot import create_support_ticket

    email = args.get("email") or ctx.get("email")
    location_id = ctx.get("location_id")
    summary = args.get("summary", "Support request")
    category = args.get("category", "technical")
    severity = args.get("severity", "medium")
    tech_details = args.get("technical_details", "")

    # Append technical details to summary for internal use
    full_summary = summary
    if tech_details:
        full_summary = f"{summary}\n\n--- Technical Details ---\n{tech_details}"

    conversation = ctx.get("conversation_log", [])
    ticket_id = create_support_ticket(
        email=email,
        location_id=location_id,
        conversation_log=conversation,
        summary=full_summary,
        category=category,
        severity=severity,
    )

    if ticket_id:
        return {
            "success": True,
            "ticket_id": ticket_id,
            "message": f"Support ticket #{ticket_id} has been created. Our team will review it shortly.",
            "severity": severity,
        }
    else:
        return {"error": "Could not create the ticket. Please try again."}


# ── Helper functions ────────────────────────────────────────────────────────

def _get_twilio_creds(email: str) -> tuple | None:
    """Get Twilio sub-account credentials from subscriber record.

    Returns (sub_account_sid, auth_token, voice_config) or None.
    """
    from db_legacy import get_db_connection, return_db_connection

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return None

        cur = conn.cursor()
        cur.execute(
            "SELECT voice_config FROM subscribers WHERE LOWER(email) = %s",
            (email.lower(),)
        )
        row = cur.fetchone()
        cur.close()

        if not row:
            return None

        vc = row.get("voice_config") or {}
        sub_sid = vc.get("twilio_sub_account_sid")
        auth_token = vc.get("twilio_sub_account_auth_token") or vc.get("twilio_auth_token", "")

        if not sub_sid:
            return None

        return sub_sid, auth_token, vc

    except Exception as e:
        logger.error(f"Failed to get Twilio creds for {email}: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def _get_location_id(email: str) -> str | None:
    """Get location_id for an email."""
    from db_legacy import get_db_connection, return_db_connection

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return None
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE LOWER(email) = %s", (email.lower(),))
        row = cur.fetchone()
        cur.close()
        return row["location_id"] if row else None
    except Exception:
        return None
    finally:
        if conn:
            return_db_connection(conn)


def _translate_trust_status(status: str) -> str:
    """Translate Twilio Trust status to plain English."""
    translations = {
        "draft": "Not submitted yet — registration is saved but hasn't been sent for review",
        "pending-review": "Submitted and waiting for review — typically takes 24-48 hours",
        "in-review": "Currently being reviewed by the carriers",
        "twilio-approved": "Approved and active — your numbers are protected",
        "twilio-rejected": "Rejected — the registration was denied and needs to be corrected and resubmitted",
    }
    return translations.get(status, f"Status: {status}")


def _safe_error(e: Exception) -> str:
    """Extract a safe error message (no SIDs or auth info)."""
    msg = str(e)
    # Strip Twilio SIDs
    for pattern, replacement in _SECRET_PATTERNS:
        msg = pattern.sub(replacement, msg)
    return msg[:200]


def _log_support_action(email: str, location_id: str, sub_account_sid: str,
                        action_type: str, action_params: dict, action_result: dict,
                        consent_message: str, success: bool):
    """Log a write action to the audit table."""
    from db_legacy import get_db_connection, return_db_connection

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return

        cur = conn.cursor()
        cur.execute("""
            INSERT INTO support_actions_log
            (email, location_id, sub_account_sid, action_type, action_params,
             action_result, consent_message, success)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            email, location_id, sub_account_sid, action_type,
            json.dumps(action_params), json.dumps(_scrub_secrets(action_result)),
            consent_message, success,
        ))
        conn.commit()
        cur.close()
        logger.info(f"Support action logged: {action_type} for {email} (success={success})")
    except Exception as e:
        logger.error(f"Failed to log support action: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            return_db_connection(conn)
