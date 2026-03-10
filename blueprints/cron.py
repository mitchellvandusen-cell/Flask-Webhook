# blueprints/cron.py — Scheduled cron job endpoints
#
# All endpoints authenticated via CRON_SECRET (Bearer header or ?key= query param).
# Scheduled by external services (cron-job.org, Railway cron) — not user-facing.

import os
import logging

from flask import Blueprint, request, jsonify as flask_jsonify
import extensions
from extensions import ADMIN_EMAILS, safe_jsonify
from email_templates import _build_reminder_24h_email, _build_reminder_72h_email
from send_email_api import send_email_via_api
from db import (get_users_needing_reminders, mark_reminder_sent,
                log_webhook_event)

logger = logging.getLogger(__name__)

cron_bp = Blueprint('cron', __name__)


def _cron_authorized() -> bool:
    """Return True if the request carries a valid CRON_SECRET."""
    import hmac
    cron_secret = os.getenv("CRON_SECRET", "")
    if not cron_secret:
        return False
    auth_header = request.headers.get("Authorization", "")
    query_key   = request.args.get("key", "")
    bearer_match = auth_header.startswith("Bearer ") and hmac.compare_digest(auth_header[7:], cron_secret)
    key_match = bool(query_key) and hmac.compare_digest(query_key, cron_secret)
    return bearer_match or key_match


# ── Reminder emails ───────────────────────────────────────────────────────────

@cron_bp.route("/api/cron/send-reminders", methods=["GET", "POST"])
def api_send_reminders():
    """
    Send 24h and 72h onboarding reminder emails to users who haven't subscribed yet.
    Schedule every 6 hours via cron-job.org or Railway cron.
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        domain_url         = os.getenv("YOUR_DOMAIN", "https://insurancegrokbot.click")
        users              = get_users_needing_reminders()
        sent_count         = 0
        errors             = []
        admin_emails_lower = [e.lower() for e in ADMIN_EMAILS]

        for user in users:
            email = user.get("email")
            if not email or email.lower() in admin_emails_lower:
                continue

            name          = user.get("full_name") or "there"
            reminder_type = user.get("reminder_type")
            user_type     = user.get("user_type", "individual")
            missing       = user.get("missing_fields", [])

            try:
                if reminder_type == "24h":
                    subject   = "Your AI Sales Assistant is Ready — Let's Get You Live"
                    html_body = _build_reminder_24h_email(name, domain_url, user_type, missing)
                    text_body = (
                        f"Hi {name}, your InsuranceGrokBot account was created 24 hours ago. "
                        f"Complete your setup to start converting leads automatically: {domain_url}/dashboard"
                    )
                else:
                    subject   = "You're Missing Leads Right Now — Activate InsuranceGrokBot"
                    html_body = _build_reminder_72h_email(name, domain_url, user_type, missing)
                    text_body = (
                        f"Hi {name}, it's been 3 days since you signed up for InsuranceGrokBot. "
                        f"Your bot is waiting to work your leads 24/7: {domain_url}/dashboard"
                    )

                sent = send_email_via_api(
                    to_email=email,
                    subject=subject,
                    html_body=html_body,
                    text_body=text_body,
                )

                if sent:
                    mark_reminder_sent(email, reminder_type, user_type)
                    log_webhook_event(
                        user.get("location_id", "unknown"),
                        f"reminder_{reminder_type}",
                        "success",
                        f"{reminder_type} reminder sent to {email} (missing: {', '.join(missing)})"
                    )
                    sent_count += 1
                    logger.info(f"Reminder {reminder_type} sent to {email} | missing={missing}")
                else:
                    errors.append(f"{email}: send failed")
                    logger.warning(f"Reminder {reminder_type} failed for {email}")
            except Exception as e:
                errors.append(f"{email}: {str(e)}")
                logger.error(f"Reminder email error for {email}: {e}")

        return safe_jsonify({
            "success": True,
            "checked": len(users),
            "sent":    sent_count,
            "errors":  errors,
        })

    except Exception as e:
        logger.error(f"Cron send-reminders crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": str(e)}), 200


# ── Token refresh ─────────────────────────────────────────────────────────────

@cron_bp.route("/api/cron/refresh-tokens", methods=["GET", "POST"])
def api_cron_refresh_tokens():
    """
    Proactively refresh GHL OAuth tokens expiring within buffer_minutes (default 60).
    Tokens are refreshed a full hour before expiry so they never lapse.
    Schedule every 15 minutes — matches cron at https://insurancegrokbot.click/api/cron/refresh-tokens
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        from ghl_api import refresh_tokens_proactively
        buffer_minutes = int(request.args.get("buffer", 60))
        stats = refresh_tokens_proactively(buffer_minutes=buffer_minutes)
        return safe_jsonify({"success": True, **stats})
    except Exception as e:
        logger.error(f"Cron refresh-tokens crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": str(e)}), 200


# ── Failed webhook recovery ───────────────────────────────────────────────────

@cron_bp.route("/api/cron/recover-failed-webhooks", methods=["GET", "POST"])
def api_cron_recover_failed_webhooks():
    """
    Find webhook tasks that failed due to token errors in the last N hours,
    attempt to get a fresh token, and re-queue them.
    Schedule every 15 minutes.
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        from tasks import recover_failed_webhooks
        max_age_hours = int(request.args.get("max_age_hours", 24))
        stats = recover_failed_webhooks(max_age_hours=max_age_hours)
        return safe_jsonify({"success": True, **stats})
    except Exception as e:
        logger.error(f"Cron recover-failed-webhooks crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": str(e)}), 200


@cron_bp.route("/api/cron/backfill-failed-webhooks", methods=["GET", "POST"])
def api_cron_backfill_failed_webhooks():
    """
    One-shot backfill: recover webhooks that failed due to token errors BEFORE
    the failed_webhook_payloads table existed. Safe to run multiple times.
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        from tasks import backfill_failed_webhooks
        max_age_hours = int(request.args.get("max_age_hours", 96))

        if not extensions.ensure_redis():
            return safe_jsonify({"success": False, "error": "Redis unavailable"}), 503

        # Run in background via RQ — backfill sleeps 0.5s per entry
        # Access via module reference so we pick up the live queue after ensure_redis()
        job = extensions.q_website.enqueue(
            backfill_failed_webhooks,
            max_age_hours=max_age_hours,
            job_timeout=600,
            result_ttl=86400,
        )
        return safe_jsonify({"success": True, "queued": True, "job_id": job.id})
    except Exception as e:
        logger.error(f"Cron backfill-failed-webhooks crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": str(e)}), 200


# ── GHL data sync ────────────────────────────────────────────────────────────

@cron_bp.route("/api/cron/sync-ghl-data", methods=["GET", "POST"])
def api_cron_sync_ghl_data():
    """
    Run incremental GHL data sync for all active subscribers.
    Syncs conversations, opportunities, phone numbers, and location data.
    Schedule every 5-10 minutes via cron.
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        if not extensions.ensure_redis():
            return safe_jsonify({"success": False, "error": "Redis unavailable"}), 503

        from ghl_sync import run_incremental_sync_all
        job = extensions.q_website.enqueue(
            run_incremental_sync_all,
            job_timeout=120,   # dispatcher is fast — just enqueues per-location jobs
            result_ttl=86400,
        )
        return safe_jsonify({"success": True, "queued": True, "job_id": job.id})
    except Exception as e:
        logger.error(f"Cron sync-ghl-data crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": str(e)}), 200


# ── Number health daily maintenance ─────────────────────────────────────────

@cron_bp.route("/api/cron/number-health", methods=["GET", "POST"])
def api_cron_number_health():
    """
    Daily number health maintenance: reset daily call counters, expire resting/frozen
    numbers, advance warm-up stages. Schedule once daily (e.g. 00:05 UTC).
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        from number_health import reset_daily_metrics, expire_resting_numbers, advance_warmup_stages

        reset_count = reset_daily_metrics()
        expired_count = expire_resting_numbers()
        warmup_count = advance_warmup_stages()

        return safe_jsonify({
            "success": True,
            "daily_reset": reset_count,
            "expired_rest": expired_count,
            "warmup_advanced": warmup_count,
        })
    except Exception as e:
        logger.error(f"Cron number-health crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": str(e)}), 200


@cron_bp.route("/api/cron/number-health-expire", methods=["GET", "POST"])
def api_cron_number_health_expire():
    """
    Expire resting/frozen numbers more frequently (every 15 min).
    Only runs the rest/freeze expiry check (not daily reset or warm-up).
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        from number_health import expire_resting_numbers
        expired_count = expire_resting_numbers()
        return safe_jsonify({"success": True, "expired": expired_count})
    except Exception as e:
        logger.error(f"Cron number-health-expire crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": str(e)}), 200
