# api_v1.py — Enterprise API Platform (OpenAI-compatible interface)
#
# External CRMs (Ringy, Salesforce, custom apps, mobile apps) integrate
# by sending JSON to /api/v1/chat/completions with a Bearer token.
# GrokBot processes the message and POSTs the reply to the subscriber's
# configured outbound_webhook_url.

import time
import logging
import secrets
from functools import wraps
from flask import Blueprint, request, jsonify, g
from db import (
    get_subscriber_by_api_key, log_api_usage, get_api_request_count,
    api_key_prefix,
)

api_bp = Blueprint('api_v1', __name__, url_prefix='/api/v1')
logger = logging.getLogger("api_v1")

# Rate limit: requests per minute per API key
RATE_LIMIT_RPM = int(__import__('os').getenv("API_RATE_LIMIT_RPM", "120"))


# ═══════════════════════════════════════════════════════════════
# ERROR RESPONSE HELPERS (OpenAI-compatible format)
# ═══════════════════════════════════════════════════════════════

def api_error(message: str, error_type: str = "invalid_request_error",
              code: str = None, status: int = 400) -> tuple:
    """Return an OpenAI-style error response."""
    body = {
        "error": {
            "message": message,
            "type": error_type,
            "code": code or error_type,
        }
    }
    return jsonify(body), status


# ═══════════════════════════════════════════════════════════════
# AUTH DECORATOR — Bearer token validation
# ═══════════════════════════════════════════════════════════════

def require_api_key(f):
    """
    Authenticate requests via Bearer token.
    Uses constant-time comparison to prevent timing attacks.
    Attaches subscriber data to flask.g.api_subscriber.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        start_time = time.time()

        # Extract Bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return api_error(
                "Missing or invalid Authorization header. Expected: Bearer sk_live_...",
                error_type="authentication_error",
                code="missing_api_key",
                status=401
            )

        provided_key = auth_header[7:].strip()
        if not provided_key or not provided_key.startswith("sk_live_"):
            return api_error(
                "Invalid API key format. Keys start with sk_live_",
                error_type="authentication_error",
                code="invalid_api_key",
                status=401
            )

        # Look up subscriber
        subscriber = get_subscriber_by_api_key(provided_key)

        if not subscriber:
            # Constant-time delay to prevent key enumeration
            time.sleep(0.5)
            key_pfx = api_key_prefix(provided_key)
            log_api_usage(key_pfx, None, request.path, 403,
                          ip_address=request.remote_addr,
                          error_message="Invalid API key")
            return api_error(
                "Invalid API key.",
                error_type="authentication_error",
                code="invalid_api_key",
                status=403
            )

        # Verify with constant-time comparison
        stored_key = subscriber.get("api_key", "")
        if not secrets.compare_digest(provided_key, stored_key):
            time.sleep(0.5)
            return api_error("Invalid API key.", error_type="authentication_error",
                             code="invalid_api_key", status=403)

        # Rate limiting
        key_pfx = api_key_prefix(provided_key)
        request_count = get_api_request_count(key_pfx, window_seconds=60)
        if request_count >= RATE_LIMIT_RPM:
            log_api_usage(key_pfx, subscriber.get("location_id"), request.path, 429,
                          ip_address=request.remote_addr,
                          error_message="Rate limit exceeded")
            return api_error(
                f"Rate limit exceeded. Maximum {RATE_LIMIT_RPM} requests per minute.",
                error_type="rate_limit_error",
                code="rate_limit_exceeded",
                status=429
            )

        # Attach to request context
        g.api_subscriber = subscriber
        g.api_key_prefix = key_pfx
        g.api_start_time = start_time

        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════════════════
# GET /api/v1/models — List available models
# ═══════════════════════════════════════════════════════════════

@api_bp.route("/models", methods=["GET"])
@require_api_key
def list_models():
    """OpenAI-compatible model listing."""
    log_api_usage(g.api_key_prefix, g.api_subscriber.get("location_id"),
                  "/api/v1/models", 200, ip_address=request.remote_addr)
    return jsonify({
        "object": "list",
        "data": [
            {
                "id": "grok-sales-director-v1",
                "object": "model",
                "created": 1700000000,
                "owned_by": "insurancegrokbot",
                "description": "Full sales AI with booking, objection handling, and carrier knowledge."
            },
        ]
    })


# ═══════════════════════════════════════════════════════════════
# POST /api/v1/chat/completions — Main conversation endpoint
# ═══════════════════════════════════════════════════════════════

@api_bp.route("/chat/completions", methods=["POST"])
@require_api_key
def chat_completions():
    """
    OpenAI-compatible chat completions endpoint.

    Accepts:
        {
            "model": "grok-sales-director-v1",  (optional, default)
            "contact_id": "lead-phone-or-id",    (required)
            "messages": [
                {"role": "user", "content": "How much is life insurance?"}
            ],
            "first_name": "Maria",               (optional)
            "metadata": { ... }                  (optional, passed through)
        }

    Returns 202 with job ID (async processing).
    Reply delivered to your outbound_webhook_url.
    """
    data = request.get_json(silent=True)
    subscriber = g.api_subscriber
    key_pfx = g.api_key_prefix

    # ── Validate payload ──
    if not data:
        log_api_usage(key_pfx, subscriber.get("location_id"), "/api/v1/chat/completions",
                      400, ip_address=request.remote_addr, error_message="Invalid JSON")
        return api_error("Request body must be valid JSON.", status=400)

    contact_id = data.get("contact_id") or data.get("user_id")
    messages = data.get("messages")

    if not contact_id:
        return api_error("Missing required field: contact_id", code="missing_field", status=400)

    if not messages or not isinstance(messages, list) or len(messages) == 0:
        return api_error("Missing or empty 'messages' array.", code="missing_field", status=400)

    # Extract the last user message
    last_message = messages[-1]
    if not isinstance(last_message, dict) or not last_message.get("content"):
        return api_error("Last message must have a 'content' field.", code="invalid_message", status=400)

    message_body = last_message["content"].strip()
    if not message_body:
        return api_error("Message content cannot be empty.", code="empty_message", status=400)

    # Check webhook URL is configured
    webhook_url = subscriber.get("outbound_webhook_url")
    if not webhook_url:
        return api_error(
            "No outbound_webhook_url configured. Set it in your dashboard or via API.",
            code="webhook_not_configured",
            status=422
        )

    # ── Build normalized payload (same format process_webhook_task expects) ──
    location_id = subscriber.get("location_id") or f"api_{key_pfx}"
    first_name = data.get("first_name") or ""
    age = data.get("age") or data.get("date_of_birth") or ""

    task_payload = {
        # Core fields matching process_webhook_task's expected format
        "contact_id": str(contact_id),
        "location_id": location_id,
        "first_name": first_name,
        "message": message_body,
        "age": age,
        "address": data.get("address") or "",

        # API-specific fields
        "_source": "universal_api",
        "_api_key_prefix": key_pfx,
        "_outbound_webhook_url": webhook_url,
        "_webhook_secret": subscriber.get("webhook_secret") or "",
        "_api_metadata": data.get("metadata") or {},
        "_conversation_history": messages[:-1],  # Prior messages for context
    }

    # ── Enqueue to worker ──
    try:
        from main import q_production
        job = q_production.enqueue(
            "tasks.process_webhook_task",
            task_payload,
            job_timeout=120,
            result_ttl=86400,
        )
        job_id = job.get_id()
    except Exception as e:
        logger.error(f"API enqueue failed: {e}", exc_info=True)
        log_api_usage(key_pfx, location_id, "/api/v1/chat/completions", 503,
                      contact_id=str(contact_id), ip_address=request.remote_addr,
                      error_message=f"Queue error: {e}")
        return api_error("Service temporarily unavailable. Please retry.",
                         error_type="server_error", code="queue_unavailable", status=503)

    # ── Log and respond ──
    elapsed_ms = int((time.time() - g.api_start_time) * 1000)
    log_api_usage(key_pfx, location_id, "/api/v1/chat/completions", 202,
                  response_time_ms=elapsed_ms, contact_id=str(contact_id),
                  ip_address=request.remote_addr)

    return jsonify({
        "id": job_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get("model", "grok-sales-director-v1"),
        "status": "processing",
        "message": "Message queued. Reply will be delivered to your webhook URL.",
        "webhook_url": webhook_url,
    }), 202


# ═══════════════════════════════════════════════════════════════
# GET /api/v1/usage — Usage stats for the authenticated user
# ═══════════════════════════════════════════════════════════════

@api_bp.route("/usage", methods=["GET"])
@require_api_key
def get_usage():
    """Return API usage stats for the authenticated subscriber."""
    from db import get_db_connection, return_db_connection
    key_pfx = g.api_key_prefix
    conn = get_db_connection()
    if not conn:
        return api_error("Database unavailable.", error_type="server_error", status=503)
    try:
        cur = conn.cursor()
        # Last 24h stats
        cur.execute("""
            SELECT
                COUNT(*) as total_requests,
                COUNT(CASE WHEN status_code = 202 THEN 1 END) as successful,
                COUNT(CASE WHEN status_code >= 400 THEN 1 END) as errors,
                ROUND(AVG(response_time_ms)) as avg_response_ms
            FROM api_usage_logs
            WHERE api_key_prefix = %s AND created_at > NOW() - INTERVAL '24 hours'
        """, (key_pfx,))
        stats = dict(cur.fetchone())

        # Rate limit status
        cur.execute("""
            SELECT COUNT(*) as rpm FROM api_usage_logs
            WHERE api_key_prefix = %s AND created_at > NOW() - INTERVAL '1 minute'
        """, (key_pfx,))
        rpm = cur.fetchone()['rpm']
        cur.close()

        return jsonify({
            "object": "usage",
            "period": "last_24_hours",
            "total_requests": stats['total_requests'],
            "successful": stats['successful'],
            "errors": stats['errors'],
            "avg_response_ms": stats['avg_response_ms'],
            "rate_limit": {
                "current_rpm": rpm,
                "max_rpm": RATE_LIMIT_RPM,
                "remaining": max(0, RATE_LIMIT_RPM - rpm)
            }
        })
    except Exception as e:
        logger.error(f"Usage stats failed: {e}")
        return api_error("Failed to fetch usage stats.", error_type="server_error", status=500)
    finally:
        return_db_connection(conn)


# ═══════════════════════════════════════════════════════════════
# POST /api/v1/webhook/test — Test webhook delivery
# ═══════════════════════════════════════════════════════════════

@api_bp.route("/webhook/test", methods=["POST"])
@require_api_key
def test_webhook():
    """Send a test payload to the subscriber's webhook URL."""
    subscriber = g.api_subscriber
    webhook_url = subscriber.get("outbound_webhook_url")
    if not webhook_url:
        return api_error("No outbound_webhook_url configured.", code="webhook_not_configured", status=422)

    from webhook_delivery import deliver_webhook
    test_payload = {
        "event": "test",
        "contact_id": "test_contact_000",
        "message": "This is a test message from InsuranceGrokBot API.",
        "role": "assistant",
        "timestamp": int(time.time()),
        "metadata": {"test": True}
    }
    success, status_code, error = deliver_webhook(
        url=webhook_url,
        payload=test_payload,
        secret=subscriber.get("webhook_secret") or "",
        max_retries=1
    )

    log_api_usage(g.api_key_prefix, subscriber.get("location_id"),
                  "/api/v1/webhook/test", 200 if success else 502,
                  ip_address=request.remote_addr)

    if success:
        return jsonify({"status": "success", "message": f"Test delivered to {webhook_url}", "response_code": status_code})
    return jsonify({"status": "failed", "message": f"Webhook delivery failed: {error}", "response_code": status_code}), 502
