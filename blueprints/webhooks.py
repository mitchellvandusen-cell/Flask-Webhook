# blueprints/webhooks.py — GHL Webhook endpoints + Website bot
#
# Core inbound webhook handlers:
#   POST /webhook              — Main GHL lead/SMS webhook, queues to RQ
#   POST /webhook/app-installed — GHL Marketplace app.installed event
#   POST /website-bot-webhook  — AI-powered support bot with diagnostics + ticket creation

import os
import json
import hmac
import hashlib
import logging

import redis
from flask import Blueprint, request, jsonify as flask_jsonify

import extensions
from extensions import ADMIN_EMAILS, safe_jsonify
from email_templates import (_build_install_welcome_email,
                             _build_uninstall_feedback_email,
                             _build_uninstall_admin_notification)
from send_email_api import send_email_via_api
from payload_utils import normalize_payload_universal
from db import (get_db_connection, return_db_connection, log_webhook_event,
                save_marketplace_install, save_persistent_alert, mark_setup_email_sent,
                save_uninstall_record, get_subscriber_info_sql, find_marketplace_email,
                delete_subscriber_data)
from tasks import process_webhook_task

logger = logging.getLogger(__name__)

webhooks_bp = Blueprint('webhooks', __name__)


# ── GHL Conversation Provider outbound handler ────────────────────────────────
# When an agent types a message in GHL's conversation UI under the
# InsuranceGrokBot SMS tab, GHL sends a webhook to this delivery URL.
# Payload: {contactId, locationId, messageId, type:"SMS", phone, message, userId}
# We detect this and send the message via Twilio instead of treating it as a
# normal inbound lead webhook.

def _is_conversation_provider_outbound(payload: dict) -> bool:
    """Detect if this is a GHL Conversation Provider outbound webhook."""
    # CP outbound has: messageId + type=SMS + phone + message (not body)
    # Regular GHL webhooks have: type=InboundMessage or type=OutboundMessage
    has_message_id = bool(payload.get("messageId"))
    msg_type = (payload.get("type") or "").upper()
    has_phone = bool(payload.get("phone"))
    has_message = bool(payload.get("message"))
    # Must have all CP-specific fields and type must be SMS (not InboundMessage etc.)
    return has_message_id and has_phone and has_message and msg_type == "SMS"


def _handle_conversation_provider_outbound(payload: dict):
    """
    Handle a GHL Conversation Provider outbound SMS webhook.
    Sends the message via Twilio and returns the result to GHL.
    """
    contact_id = payload.get("contactId", "")
    location_id = payload.get("locationId", "")
    message_id = payload.get("messageId", "")
    phone = payload.get("phone", "")
    message = payload.get("message", "")
    user_id = payload.get("userId", "")

    logger.info(
        f"📤 CP Outbound SMS | contact={contact_id} | location={location_id} | "
        f"phone={phone[:6]}*** | msgId={message_id[:12]}..."
    )

    if not location_id or not phone or not message:
        logger.warning(f"CP outbound missing required fields: location={location_id}, phone={bool(phone)}, msg={bool(message)}")
        return safe_jsonify({"status": "error", "reason": "missing_fields"}), 400

    # Look up Twilio credentials for this subscriber
    try:
        from twilio_sms import get_twilio_credentials
        sub_sid, sub_auth, from_number = get_twilio_credentials(location_id)
        if not sub_sid or not sub_auth or not from_number:
            logger.warning(f"CP outbound: no Twilio credentials for {location_id}")
            return safe_jsonify({"status": "error", "reason": "no_twilio_creds"}), 400

        from twilio_sms import send_sms_via_twilio
        sent, fail_reason, http_detail = send_sms_via_twilio(
            phone_to=phone,
            message=message,
            from_number=from_number,
            twilio_sub_account_sid=sub_sid,
            twilio_auth_token=sub_auth,
            contact_id=contact_id,
        )

        if sent:
            logger.info(f"✅ CP outbound SMS sent via Twilio to {phone[:6]}*** for {location_id}")
            log_webhook_event(location_id, "cp_outbound_sms", "success",
                              f"GHL CP SMS sent to {contact_id} ({len(message)} chars)",
                              contact_id=contact_id)
            # Fire live inbox update event so dashboard refreshes instantly
            log_webhook_event(location_id, "new_inbound_message", "info",
                              f"Message to {contact_id}",
                              contact_id=contact_id,
                              details={
                                  "contact_name": "",
                                  "message_preview": message[:80],
                                  "direction": "outbound",
                              })
            return safe_jsonify({"status": "sent", "messageId": message_id}), 200
        else:
            logger.error(f"CP outbound SMS failed ({fail_reason}) for {contact_id}")
            log_webhook_event(location_id, "cp_outbound_sms", "error",
                              f"GHL CP SMS failed: {fail_reason}",
                              contact_id=contact_id)
            return safe_jsonify({"status": "error", "reason": fail_reason}), 500

    except Exception as e:
        logger.error(f"CP outbound SMS exception for {contact_id}: {e}")
        return safe_jsonify({"status": "error", "reason": str(e)}), 500


# ── Main webhook receiver ─────────────────────────────────────────────────────

@webhooks_bp.route("/webhook", methods=["POST"])
def webhook():
    """Main GHL webhook receiver — normalises payload and queues to RQ."""

    # ── Webhook signature verification ────────────────────────────────────
    # GHL signs webhooks with HMAC-SHA256 using the marketplace webhook secret.
    # We verify when both the secret is configured AND a signature header is present.
    # If the secret is set but no signature header arrives, we log a warning but
    # allow the request — GHL may not sign all webhook types (e.g. Conversation Provider).
    webhook_secret = os.getenv("MARKETPLACE_WEBHOOK_SECRET")
    if webhook_secret:
        signature = request.headers.get("X-Ghl-Signature") or request.headers.get("X-Hook-Secret") or ""
        if signature:
            body = request.get_data(as_text=True)
            expected = hmac.new(webhook_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                logger.warning("Webhook signature mismatch — rejecting request")
                return safe_jsonify({"status": "error", "reason": "invalid_signature"}), 401
        else:
            logger.debug("Webhook received without signature header — skipping verification")

    # ── Conversation Provider outbound webhook ────────────────────────────
    # When an agent sends a message from GHL UI via the InsuranceGrokBot SMS
    # custom provider, GHL fires a webhook here with {messageId, type, phone,
    # message, contactId, locationId}. We detect this and send via Twilio
    # instead of processing as a normal lead webhook.
    raw_payload = request.get_json(silent=True) or request.form.to_dict() or {}
    if _is_conversation_provider_outbound(raw_payload):
        return _handle_conversation_provider_outbound(raw_payload)

    if not extensions.ensure_redis():
        logger.critical("Redis/RQ unavailable after reconnect attempt")
        return safe_jsonify({"status": "error", "reason": "redis_unavailable"}), 503

    payload     = normalize_payload_universal(raw_payload)
    location_id = payload.get("location_id")
    contact_id  = payload.get("contact_id")
    message_body = payload.get("message") or payload.get("body")

    if (not contact_id
            or str(contact_id).strip().lower() in ["unknown", "none", "null", ""]
            or len(str(contact_id).strip()) < 5):
        logger.critical(f"🚨 WEBHOOK REJECTED | contact_id={contact_id} | location_id={location_id}")
        logger.critical(f"🚨 Original payload: {json.dumps(payload.get('_original_payload', {}), default=str)}")
        return safe_jsonify({"status": "rejected", "reason": "invalid_contact_id"}), 400

    logger.info(f"📨 Webhook received | contact_id={contact_id} | location_id={location_id} | queuing")

    # Demo optimisation: write user message immediately so UI updates instantly
    if location_id in ['DEMO_LOC', 'DEMO'] and contact_id and message_body:
        conn = cur = None
        try:
            conn = get_db_connection()
            if conn:
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO contact_messages (contact_id, message_type, message_text)
                    VALUES (%s, 'lead', %s)
                    ON CONFLICT DO NOTHING
                """, (contact_id, message_body))
                conn.commit()
        except Exception as e:
            logger.error(f"Instant demo write failed: {e}")
            if conn:
                conn.rollback()
        finally:
            if cur:
                cur.close()
            if conn:
                return_db_connection(conn)

    is_demo  = location_id in ['DEMO_LOC', 'DEMO', 'TEST_LOCATION_456']
    is_reply = bool(message_body and message_body.strip())

    for attempt in range(2):
        try:
            target_queue = extensions.q_demo if is_demo else extensions.q_production
            job = target_queue.enqueue(
                process_webhook_task,
                payload,
                job_timeout=120,
                result_ttl=86400,
                at_front=is_reply,
            )
            return safe_jsonify({"status": "queued", "job_id": job.id}), 202

        except (redis.ConnectionError, redis.TimeoutError, OSError) as e:
            if attempt == 0:
                logger.warning(f"⚠️ Enqueue failed (attempt 1), reconnecting: {e}")
                if not extensions.ensure_redis():
                    return safe_jsonify({"status": "error", "reason": "redis_unavailable"}), 503
            else:
                logger.error(f"❌ Enqueue failed after retry: {e}")
                return safe_jsonify({"status": "error", "reason": "redis_enqueue_failed"}), 503

        except Exception as e:
            logger.error(f"Queue failed: {e}")
            return safe_jsonify({"status": "error"}), 500


# ── Marketplace app.installed webhook ─────────────────────────────────────────

@webhooks_bp.route("/webhook/app-installed", methods=["POST"])
def app_installed_webhook():
    """
    GHL Marketplace 'app.installed' + 'UNINSTALL' webhook.
    Captures install events before or if OAuth redirect never fires.
    Also handles uninstall events — sends farewell feedback email.
    Configure in GHL Developer Portal > Webhooks > app.installed.
    """
    # ── Webhook signature verification (same as main /webhook) ──────────
    webhook_secret = os.getenv("MARKETPLACE_WEBHOOK_SECRET")
    if webhook_secret:
        signature = request.headers.get("X-Ghl-Signature") or request.headers.get("X-Hook-Secret") or ""
        if signature:
            body = request.get_data(as_text=True)
            expected = hmac.new(webhook_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                logger.warning("app-installed webhook signature mismatch — rejecting")
                return safe_jsonify({"status": "error", "reason": "invalid_signature"}), 401
        else:
            logger.debug("app-installed webhook without signature header — skipping verification")

    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    logger.info(f"=== APP WEBHOOK === payload keys: {list(payload.keys())}")

    # ── Handle UNINSTALL events ──────────────────────────────────────────────
    event_type = (payload.get("type") or payload.get("event") or "").upper()
    if event_type == "UNINSTALL":
        return _handle_uninstall(payload)

    log_webhook_event("marketplace", "app_installed", "info",
                      "App install webhook received",
                      details={"payload": payload})

    data       = payload.get("data", payload)
    company_id = data.get("companyId") or data.get("company_id") or payload.get("companyId") or ""
    location_id = data.get("locationId") or data.get("location_id") or payload.get("locationId") or ""
    user_email = data.get("email") or data.get("userEmail") or ""
    user_name  = data.get("name") or data.get("userName") or data.get("firstName") or ""

    install_id = save_marketplace_install(payload)

    if install_id:
        log_webhook_event("marketplace", "app_installed_saved", "success",
                          f"Install #{install_id} saved: company={company_id}, "
                          f"location={location_id}, email={user_email}, name={user_name}")

        if user_email:
            try:
                domain_url   = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
                display_name = user_name or "there"
                html_body    = _build_install_welcome_email(display_name, domain_url, recipient_email=user_email)
                text_body    = (
                    f"Hi {display_name}, thanks for installing InsuranceGrokBot! "
                    f"Complete your setup: {domain_url}/oauth/initiate"
                )
                sent = send_email_via_api(
                    to_email=user_email,
                    subject="Welcome to InsuranceGrokBot — Complete Your Setup",
                    html_body=html_body,
                    text_body=text_body,
                )
                if sent:
                    mark_setup_email_sent(install_id)
                    log_webhook_event("marketplace", "install_welcome_email", "success",
                                      f"Welcome email sent to {user_email} for install #{install_id}")
                else:
                    log_webhook_event("marketplace", "install_welcome_email", "error",
                                      f"Failed to send welcome email to {user_email}")
            except Exception as email_err:
                logger.error(f"Install welcome email error: {email_err}")

        try:
            for admin_email in ADMIN_EMAILS[:1]:
                save_persistent_alert(
                    admin_email, location_id or "marketplace",
                    "new_install", "info",
                    "New Marketplace Install",
                    f"New app install: {user_name or 'Unknown'} ({user_email or 'no email'}) "
                    f"— Company: {company_id or 'N/A'}, Location: {location_id or 'N/A'}"
                )
        except Exception:
            pass

    return safe_jsonify({"status": "received", "install_id": install_id}), 200


def _handle_uninstall(payload: dict):
    """Handle GHL UNINSTALL webhook — save record, send feedback email + admin notification."""
    data        = payload.get("data", payload)
    company_id  = data.get("companyId") or payload.get("companyId") or ""
    location_id = data.get("locationId") or payload.get("locationId") or ""

    log_webhook_event("marketplace", "app_uninstalled", "info",
                      f"App uninstalled: location={location_id}, company={company_id}",
                      details={"payload": payload})

    # Try to find the user's email from subscribers or marketplace_installs
    user_email = ""
    user_name  = ""
    if location_id:
        sub = get_subscriber_info_sql(location_id)
        if sub:
            user_email = sub.get("email") or ""
            user_name  = sub.get("full_name") or ""
    if not user_email and (location_id or company_id):
        mkt = find_marketplace_email(location_id=location_id, company_id=company_id)
        if mkt:
            user_email = mkt.get("user_email") or ""
            user_name  = user_name or mkt.get("user_name") or ""

    # Save uninstall record
    record_id = save_uninstall_record(payload, location_id, company_id, user_email, user_name)
    if not record_id:
        logger.error("Failed to save uninstall record")
        return safe_jsonify({"status": "error", "detail": "db_save_failed"}), 500

    # Delete the subscriber row and all related data for this location
    if location_id:
        deleted = delete_subscriber_data(location_id)
        if deleted:
            log_webhook_event("marketplace", "subscriber_deleted", "info",
                              f"Subscriber data deleted for location={location_id}")
        else:
            log_webhook_event("marketplace", "subscriber_deleted", "warning",
                              f"No subscriber row found to delete for location={location_id}")

    domain_url   = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
    display_name = user_name or "there"
    admin_email  = "mitch@insurancegrokbot.com"

    # Send farewell feedback email to the user (if we have their email)
    if user_email:
        try:
            html_body = _build_uninstall_feedback_email(display_name, domain_url, record_id, recipient_email=user_email)
            text_body = (
                f"Hi {display_name}, we're sorry to see you go. "
                f"We'd love your feedback: {domain_url}/uninstall-feedback?id={record_id}"
            )
            sent = send_email_via_api(
                to_email=user_email,
                subject="We're sorry to see you go — quick feedback?",
                html_body=html_body,
                text_body=text_body,
            )
            if sent:
                log_webhook_event("marketplace", "uninstall_feedback_email", "success",
                                  f"Farewell email sent to {user_email} (record #{record_id})")
            else:
                log_webhook_event("marketplace", "uninstall_feedback_email", "error",
                                  f"Failed to send farewell email to {user_email}")
        except Exception as email_err:
            logger.error(f"Uninstall farewell email error: {email_err}")

    # Send admin notification to mitch@insurancegrokbot.com
    try:
        admin_html = _build_uninstall_admin_notification(
            location_id, company_id, user_email, user_name, record_id
        )
        send_email_via_api(
            to_email=admin_email,
            subject=f"App Uninstalled — {user_name or 'Unknown'} ({location_id or 'N/A'})",
            html_body=admin_html,
            text_body=f"App uninstalled: {user_name} ({user_email}), location={location_id}, company={company_id}",
        )
    except Exception as admin_err:
        logger.error(f"Uninstall admin notification error: {admin_err}")

    # Persistent alert for admin dashboard
    try:
        for ae in ADMIN_EMAILS[:1]:
            save_persistent_alert(
                ae, location_id or "marketplace",
                "app_uninstalled", "warning",
                "App Uninstalled",
                f"Uninstall: {user_name or 'Unknown'} ({user_email or 'no email'}) "
                f"— Location: {location_id or 'N/A'}"
            )
    except Exception:
        pass

    return safe_jsonify({"status": "received", "uninstall_id": record_id}), 200


# ── AI-powered autonomous support agent ─────────────────────────────────────

# Max tool-calling rounds per request (prevents infinite loops)
_SUPPORT_MAX_TOOL_ROUNDS = 5
# Model for the support agent
_SUPPORT_MODEL = "grok-3-mini-fast"


@webhooks_bp.route("/website-bot-webhook", methods=["POST"])
def website_bot_webhook():
    """
    Autonomous AI support agent with function-calling tools.

    The agent can: look up accounts, check phone system registrations,
    read error logs, search the knowledge base, fix registration issues
    (with consent), and escalate to human support via tickets.

    Uses xAI Grok with OpenAI-compatible tool_use for multi-turn
    tool-calling loops — same pattern as blueprints/workflows.py.
    """
    import json as _json
    from openai import OpenAI
    from support_prompt import build_support_prompt
    from support_bot import (
        handle_quick_action, support_rate_limited, extract_email,
        has_consent, sanitize_support_reply,
        extract_options, extract_redirect,
    )
    from support_tools import (
        get_support_tool_definitions, execute_support_tool,
        execute_approved_action,
    )

    payload      = request.get_json(silent=True) or {}
    user_message = payload.get('message', '').strip()
    history      = payload.get('history', [])

    if not user_message:
        return flask_jsonify({"status": "error"}), 400

    # Rate limit
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
    if support_rate_limited(client_ip):
        return flask_jsonify({
            "text": "You're sending messages too quickly. Please wait a moment and try again."
        }), 429

    # Quick-action buttons (handled without AI call — zero cost, instant)
    quick = handle_quick_action(user_message)
    if quick is not None:
        return flask_jsonify(quick)

    # Handle consent-approved write actions
    if user_message.startswith("APPROVE_ACTION:"):
        action_id = user_message.split(":", 1)[1].strip()
        email_found = extract_email("", history)
        result = execute_approved_action(action_id, {
            "email": email_found,
            "location_id": None,
            "conversation_log": history,
        })
        if result.get("success"):
            return flask_jsonify({"text": result.get("message", "Done!")})
        else:
            return flask_jsonify({"text": result.get("error", "Something went wrong. Please try again.")})

    # Build user context from conversation history
    email_found = extract_email(user_message, history)
    user_context = {
        "email": email_found,
        "location_id": None,
        "has_consent": has_consent(history),
        "conversation_log": history,
    }

    # Build system prompt (diagnostics now handled via tools, not prompt injection)
    system_prompt = build_support_prompt()

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

    # Tool definitions for function calling
    tools = get_support_tool_definitions()

    # LLM client — Groq (free) if GROQ_API_KEY set, else xAI grok-3-mini-fast
    from free_llm import get_free_llm
    client, _SUPPORT_MODEL = get_free_llm("quality")
    if not client:
        logger.error("No LLM API key set — support bot cannot respond")
        return flask_jsonify({
            "text": "We're having a temporary issue. Please try again in a moment, or visit our contact page for help.",
            "options": [{"label": "Contact Page", "value": "QUICK_CONTACT"}]
        })
    reply = ""

    # ── Multi-turn tool-calling loop ────────────────────────────
    try:
        for _round in range(_SUPPORT_MAX_TOOL_ROUNDS):
            response = client.chat.completions.create(
                model=_SUPPORT_MODEL,
                messages=messages,
                tools=tools,
                temperature=0.7,
                max_tokens=800,
            )

            msg = response.choices[0].message

            if msg.tool_calls:
                # Append assistant message with tool calls
                messages.append(msg)

                for tool_call in msg.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        tool_args = _json.loads(tool_call.function.arguments)
                    except (_json.JSONDecodeError, TypeError):
                        tool_args = {}

                    logger.info(f"Support agent tool call: {tool_name}({list(tool_args.keys())})")

                    # Execute the tool
                    tool_result = execute_support_tool(
                        tool_name, tool_args, user_context
                    )

                    # Check if this is a consent-required write action
                    if isinstance(tool_result, dict) and tool_result.get("needs_consent"):
                        # Return consent prompt to user immediately
                        action_id = tool_result["action_id"]
                        description = tool_result["action_description"]
                        consent_text = tool_result.get("message", f"I can {description.lower()}. Would you like me to go ahead?")
                        consent_text = sanitize_support_reply(consent_text)
                        return flask_jsonify({
                            "text": consent_text,
                            "options": [
                                {"label": "Yes, Fix It", "value": f"APPROVE_ACTION:{action_id}"},
                                {"label": "No Thanks", "value": "no_thanks"},
                            ]
                        })

                    # Append tool result for the next LLM round
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": _json.dumps(tool_result, default=str),
                    })
            else:
                # No tool calls — final response
                reply = (msg.content or "").strip()
                break

    except Exception as e:
        logger.error(f"Support agent loop failed: {type(e).__name__}: {e}", exc_info=True)
        reply = ""

    if not reply:
        reply = "We're having a little trouble right now. You can reach us at our contact page, or try again in a moment."

    # Sanitize forbidden terms from final output
    reply = sanitize_support_reply(reply)

    # Parse options and redirect tags from AI output
    options, reply = extract_options(reply)
    redirect_url, reply = extract_redirect(reply)

    response = {"text": reply.strip()}
    if options:
        response["options"] = options
    if redirect_url:
        response["redirect"] = redirect_url

    return flask_jsonify(response)
