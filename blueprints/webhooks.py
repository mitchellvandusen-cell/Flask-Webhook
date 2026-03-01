# blueprints/webhooks.py — GHL Webhook endpoints + Website bot
#
# Core inbound webhook handlers:
#   POST /webhook              — Main GHL lead/SMS webhook, queues to RQ
#   POST /webhook/app-installed — GHL Marketplace app.installed event
#   POST /website-bot-webhook  — Scripted website chat bot (no AI needed)

import os
import json
import logging

import redis
from flask import Blueprint, request, jsonify as flask_jsonify

import extensions
from extensions import ADMIN_EMAILS, safe_jsonify
from email_templates import _build_install_welcome_email
from send_email_api import send_email_via_api
from db import (get_db_connection, return_db_connection, log_webhook_event,
                save_marketplace_install, save_persistent_alert, mark_setup_email_sent)
from tasks import process_webhook_task

logger = logging.getLogger(__name__)

webhooks_bp = Blueprint('webhooks', __name__)


# ── Payload normalisation helpers ─────────────────────────────────────────────

def extract_field_flexible(payload: dict, field_name: str, search_nested: bool = True):
    """
    Extract a field from a webhook payload supporting all naming conventions:
    snake_case, camelCase, PascalCase, UPPERCASE, and space-separated variants.
    Also searches nested structures (extras, data, meta, contact, location).
    """
    base_name = field_name.replace("_", "").lower()
    words     = field_name.split("_")

    variations = [
        field_name,
        field_name.upper(),
        field_name.replace("_", " "),
        field_name.replace("_", " ").upper(),
        field_name.replace("_", " ").title(),
        base_name,
        base_name.upper(),
        "".join(w.capitalize() for w in words),
        words[0] + "".join(w.capitalize() for w in words[1:]),
    ]

    for var in variations:
        if var in payload:
            value = payload[var]
            if value is not None and str(value).strip():
                return value

    if search_nested:
        for nested_key in ["extras", "data", "meta", "contact", "location", "user", "calendar"]:
            if nested_key in payload and isinstance(payload[nested_key], dict):
                for var in variations:
                    if var in payload[nested_key]:
                        value = payload[nested_key][var]
                        if value is not None and str(value).strip():
                            return value

    return None


def normalize_payload_universal(payload: dict) -> dict:
    """Normalize any Lead Connector payload to a consistent snake_case dict."""
    id_fields   = ["contact_id", "location_id", "user_id", "calendar_id",
                   "appointment_id", "opportunity_id", "workflow_id", "company_id",
                   "conversation_id", "message_id", "task_id", "pipeline_id"]
    data_fields = ["first_name", "last_name", "full_name", "email", "phone",
                   "address", "city", "state", "zip", "country",
                   "age", "date_of_birth", "gender", "intent", "message",
                   "agent", "status", "type", "direction", "body"]

    normalized = {}
    for field in id_fields + data_fields:
        value = extract_field_flexible(payload, field, search_nested=True)
        if value is not None:
            normalized[field] = value

    normalized["_original_payload"] = payload
    normalized["_is_marketplace"]   = payload.get("isMarketplaceAction", False)
    return normalized


# ── Main webhook receiver ─────────────────────────────────────────────────────

@webhooks_bp.route("/webhook", methods=["POST"])
def webhook():
    """Main GHL webhook receiver — normalises payload and queues to RQ."""
    if not extensions.ensure_redis():
        logger.critical("Redis/RQ unavailable after reconnect attempt")
        return safe_jsonify({"status": "error", "reason": "redis_unavailable"}), 503

    payload     = request.get_json(silent=True) or request.form.to_dict() or {}
    payload     = normalize_payload_universal(payload)
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
    GHL Marketplace 'app.installed' webhook.
    Captures install events before or if OAuth redirect never fires.
    Configure in GHL Developer Portal > Webhooks > app.installed.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}

    logger.info(f"=== APP.INSTALLED WEBHOOK === payload keys: {list(payload.keys())}")
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
                html_body    = _build_install_welcome_email(display_name, domain_url)
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


# ── Website chat bot ──────────────────────────────────────────────────────────

@webhooks_bp.route("/website-bot-webhook", methods=["POST"])
def website_bot_webhook():
    """
    Scripted website chat — instant responses with no AI overhead.
    Qualifies visitors, answers objections, routes to signup or demo.
    """
    payload      = request.get_json(silent=True) or {}
    user_message = payload.get('message', '').strip()

    if not user_message:
        return flask_jsonify({"status": "error"}), 400

    msg_lower = user_message.lower()

    # ── Init & qualification ──────────────────────────────────────────────────

    if user_message == "INIT_CHAT":
        return flask_jsonify({
            "text": "Hey! I'm actually the product you're looking at right now. Quick question - are you a solo agent or do you run an agency?",
            "options": [
                {"label": "Solo Agent",    "value": "individual"},
                {"label": "Agency Owner",  "value": "agency"}
            ]
        })

    # ── Individual path ───────────────────────────────────────────────────────

    if user_message == "individual":
        return flask_jsonify({
            "text": "Nice. So right now you're manually following up with leads, right? Or maybe you've got some basic automation that sounds like a robot?",
            "options": [
                {"label": "Yeah, manual follow-up",          "value": "individual_manual"},
                {"label": "I have automation but it sucks",  "value": "individual_bad_auto"},
                {"label": "Just curious what this is",       "value": "individual_curious"}
            ]
        })

    if user_message == "individual_manual":
        return flask_jsonify({
            "text": "That's where most leads die. You get busy, forget to follow up, and that lead who was warm 3 days ago is now cold. I fix that. I respond instantly - even at 2am - and I actually sound human. Want to see how I handle a cold lead?",
            "options": [
                {"label": "Show me",           "value": "demo"},
                {"label": "What does it cost?", "value": "pricing_individual"}
            ]
        })

    if user_message == "individual_bad_auto":
        return flask_jsonify({
            "text": "Let me guess - keyword triggers, canned responses, and leads can tell it's a bot within 2 messages? I'm different. I use 5 actual sales methodologies - NEPQ, Gap Selling, Chris Voss tactics. I handle objections, remember everything about the lead, and book appointments on your calendar. Want to see?",
            "options": [
                {"label": "Try the demo",  "value": "demo"},
                {"label": "What's it cost?", "value": "pricing_individual"}
            ]
        })

    if user_message == "individual_curious":
        return flask_jsonify({
            "text": "Short version: I'm an AI that responds to your insurance leads via SMS. But I'm not a dumb chatbot - I use real sales frameworks, remember the entire conversation history, handle objections like a human setter, and book appointments directly on your calendar. All while you sleep.",
            "options": [
                {"label": "See it in action",     "value": "demo"},
                {"label": "How is this different?", "value": "comparison"},
                {"label": "Pricing",               "value": "pricing_individual"}
            ]
        })

    # ── Agency path ───────────────────────────────────────────────────────────

    if user_message == "agency":
        return flask_jsonify({
            "text": "Nice. How many agents do you have under you right now?",
            "options": [
                {"label": "Under 10", "value": "agency_small"},
                {"label": "10-50",    "value": "agency_medium"},
                {"label": "50+",      "value": "agency_large"}
            ]
        })

    if user_message == "agency_small":
        return flask_jsonify({
            "text": "Perfect size to start. Here's what I solve for you: inconsistent follow-up across your team. Some agents are great, some let leads rot. With me, every sub-account gets the same AI setter - same brain, same methodology, but books to THEIR calendar. You get a dashboard to see everything. Agency Starter covers up to 14 agents — book a call for pricing.",
            "options": [
                {"label": "How does that work exactly?", "value": "agency_how"},
                {"label": "Show me the demo",            "value": "demo"},
                {"label": "What's included?",            "value": "agency_features"}
            ]
        })

    if user_message in ["agency_medium", "agency_large"]:
        return flask_jsonify({
            "text": "At your scale, lead leakage is probably costing you six figures a year. Every single sub-account gets an AI setter. Same training, same methodology, same quality - but each one books to that agent's calendar. One dashboard for you to monitor everything. Agency Pro gives you unlimited sub-accounts — book a call for pricing.",
            "options": [
                {"label": "How does multi-tenant work?", "value": "agency_how"},
                {"label": "Show me the demo",            "value": "demo"},
                {"label": "What makes this different?",  "value": "comparison"}
            ]
        })

    if user_message == "agency_how":
        return flask_jsonify({
            "text": "Simple: You connect your Lead Connector agency account. I automatically see all your sub-accounts. Each one gets their own instance of me - same sales brain, but configured for their calendar and timezone. When a lead texts into Location A, I respond as Location A's setter and book on their calendar. You see all conversations from one dashboard.",
            "options": [
                {"label": "What do my agents see?", "value": "agency_agent_view"},
                {"label": "Try the demo",           "value": "demo"},
                {"label": "Pricing",                "value": "pricing_agency"}
            ]
        })

    if user_message == "agency_agent_view":
        return flask_jsonify({
            "text": "Your agents see conversations happening in their Lead Connector inbox like normal. They can jump in anytime if needed. But mostly they just see appointments showing up on their calendar with qualified leads. The AI does the grunt work, they do the closing.",
            "options": [
                {"label": "That sounds good", "value": "demo"},
                {"label": "What's pricing?",  "value": "pricing_agency"}
            ]
        })

    if user_message == "agency_features":
        return flask_jsonify({
            "text": "Agency Starter includes: Up to 14 sub-accounts, multi-tenant dashboard, shared memory across your agency, priority support, all 5 sales methodologies, auto-booking to each agent's calendar, and underwriting pre-qualification. No contracts, cancel anytime.",
            "options": [
                {"label": "Get started",              "value": "signup_agency_starter"},
                {"label": "See it work first",        "value": "demo"},
                {"label": "What if I have more than 10?", "value": "agency_pro_info"}
            ]
        })

    if user_message == "agency_pro_info":
        return flask_jsonify({
            "text": "Agency Pro gives you unlimited sub-accounts. Same features plus dedicated high-speed queue (faster responses) and white-glove onboarding. No cap on agents — scale as big as you want. Custom pricing — book a call for details.",
            "options": [
                {"label": "Get started",    "value": "signup_agency_pro"},
                {"label": "Try demo first", "value": "demo"}
            ]
        })

    # ── Features & comparison ─────────────────────────────────────────────────

    if user_message == "comparison" or "different" in msg_lower or "vs" in msg_lower or "compare" in msg_lower:
        return flask_jsonify({
            "text": "Most bots use keyword matching - they're dumb. I use 5 real sales frameworks: NEPQ for emotional gaps, Chris Voss tactics for objections, Gap Selling to create urgency, plus Straight Line and Zig Ziglar methods. I also have persistent memory and understand underwriting.",
            "redirect": "/comparison"
        })

    if "memory" in msg_lower or "remember" in msg_lower:
        return flask_jsonify({
            "text": "I remember everything. If a lead mentioned their wife's name 3 months ago, I still know it. If they said they had diabetes, I factor that into underwriting. No awkward 'what was your name again?' moments.",
            "options": [
                {"label": "See it in action",      "value": "demo"},
                {"label": "What else is different?", "value": "comparison"}
            ]
        })

    if "underwriting" in msg_lower or "pre-qualify" in msg_lower or "health" in msg_lower:
        return flask_jsonify({
            "text": "I ask the right health questions before they ever get on your calendar. Diabetes? Heart issues? Smoker? I know what carriers need and I gather that info naturally in conversation. You get on calls with qualified leads, not people who can't get approved.",
            "options": [
                {"label": "Show me how", "value": "demo"},
                {"label": "Pricing",     "value": "pricing_individual"}
            ]
        })

    if "methodology" in msg_lower or "framework" in msg_lower or "nepq" in msg_lower or "sales" in msg_lower:
        return flask_jsonify({
            "text": "I blend 5 proven frameworks: NEPQ (emotional gap questions), Gap Selling (current state vs future state), Chris Voss (labeling, no-oriented questions), Straight Line (always advancing), and Zig Ziglar (help first, objections = requests for clarity). This isn't scripted - I adapt to each conversation.",
            "options": [
                {"label": "See it handle objections", "value": "demo"},
                {"label": "Pricing",                  "value": "pricing_individual"}
            ]
        })

    if "book" in msg_lower or "calendar" in msg_lower or "appointment" in msg_lower:
        return flask_jsonify({
            "text": "I connect directly to your Lead Connector calendar. When a lead is ready, I show them available slots and book it - no links to click, no friction. The appointment shows up with all context: what they said, health info, objections that came up.",
            "options": [
                {"label": "Try the demo", "value": "demo"},
                {"label": "Pricing",      "value": "pricing_individual"}
            ]
        })

    # ── Pricing ───────────────────────────────────────────────────────────────

    if user_message == "pricing_individual" or (("price" in msg_lower or "cost" in msg_lower or "how much" in msg_lower) and "agency" not in msg_lower):
        return flask_jsonify({
            "text": "$149.99/month. Unlimited conversations, full memory, all 5 sales methodologies, calendar auto-booking, underwriting logic, smart dialer, AI voice agent. No contracts, cancel anytime.",
            "options": [
                {"label": "Get started", "value": "signup_individual"},
                {"label": "See it first",     "value": "demo"}
            ]
        })

    if user_message == "pricing_agency" or ("price" in msg_lower and "agency" in msg_lower):
        return flask_jsonify({
            "text": "Two options: Agency Starter for up to 14 sub-accounts, or Agency Pro for unlimited. Both include the full multi-tenant dashboard and all features. Custom pricing — book a call for details. No contracts, cancel anytime.",
            "options": [
                {"label": "Agency Starter (up to 14)",  "value": "signup_agency_starter"},
                {"label": "Agency Pro (unlimited)",     "value": "signup_agency_pro"},
                {"label": "See demo first",            "value": "demo"}
            ]
        })

    # ── Signup routes ─────────────────────────────────────────────────────────

    if user_message == "demo":
        return flask_jsonify({"text": "Let's do it. I'll show you exactly how I talk to a cold insurance lead.", "redirect": "/demo-chat"})

    if user_message == "signup_individual":
        return flask_jsonify({"text": "Let's get you set up. No contracts, cancel anytime.", "redirect": "/checkout"})

    if user_message == "signup_agency_starter":
        return flask_jsonify({"text": "Good choice. Up to 14 sub-accounts, cancel anytime.", "redirect": "/checkout/agency-starter"})

    if user_message == "signup_agency_pro":
        return flask_jsonify({"text": "Let's scale. Unlimited sub-accounts, one flat price.", "redirect": "/checkout/agency-pro"})

    # ── FAQ / objection handling ──────────────────────────────────────────────

    if "trial" in msg_lower or "free" in msg_lower:
        return flask_jsonify({
            "text": "We don't offer a free trial, but you can try the full AI demo right now to see exactly how it works. No signup required for the demo. When you're ready, it's $149.99/month — cancel anytime, no contracts.",
            "options": [
                {"label": "Try the demo",   "value": "demo"},
                {"label": "Get started",    "value": "signup_individual"}
            ]
        })

    if "ghl" in msg_lower or "gohighlevel" in msg_lower or "highlevel" in msg_lower or "crm" in msg_lower or "lead connector" in msg_lower:
        return flask_jsonify({
            "text": "I integrate directly with Lead Connector. You connect via OAuth (one click), and I automatically see your contacts, calendars, and conversations. Works with any plan - agency or location level.",
            "options": [
                {"label": "See integration", "value": "demo"},
                {"label": "Get started",     "value": "signup_individual"}
            ]
        })

    if "support" in msg_lower or "help" in msg_lower or "setup" in msg_lower:
        return flask_jsonify({
            "text": "Setup takes about 5 minutes - connect Lead Connector, configure your calendar, done. All plans include support. Agency Pro includes white-glove onboarding where we set everything up for you.",
            "options": [
                {"label": "Start setup",     "value": "signup_individual"},
                {"label": "Questions first", "value": "contact"}
            ]
        })

    if user_message == "contact" or "contact" in msg_lower or "talk to" in msg_lower or "human" in msg_lower:
        return flask_jsonify({"text": "Want to talk to the team?", "redirect": "/contact"})

    # ── Fallback ──────────────────────────────────────────────────────────────

    return flask_jsonify({
        "text": "Best way to understand what I do is to see it. I'll show you how I handle a real cold insurance lead.",
        "options": [
            {"label": "Show me",          "value": "demo"},
            {"label": "Just tell me pricing", "value": "pricing_individual"}
        ]
    })
