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
        domain_url         = os.getenv("YOUR_DOMAIN", "https://omnisconn.click")
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
                    html_body = _build_reminder_24h_email(name, domain_url, user_type, missing, recipient_email=email)
                    text_body = (
                        f"Hi {name}, your Omnisconn account was created 24 hours ago. "
                        f"Complete your setup to start converting leads automatically: {domain_url}/dashboard"
                    )
                else:
                    subject   = "You're Missing Leads Right Now — Activate Omnisconn"
                    html_body = _build_reminder_72h_email(name, domain_url, user_type, missing, recipient_email=email)
                    text_body = (
                        f"Hi {name}, it's been 3 days since you signed up for Omnisconn. "
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
                    errors.append(f"{email}: send failed (check send_email_api logs for Mailgun response)")
                    logger.warning(f"Reminder {reminder_type} failed for {email} — Mailgun API returned failure (see send_email_api ERROR log above for details)")
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
        return safe_jsonify({"success": False, "error": "Internal server error"}), 200


# ── Token refresh ─────────────────────────────────────────────────────────────

@cron_bp.route("/api/cron/refresh-tokens", methods=["GET", "POST"])
def api_cron_refresh_tokens():
    """
    Proactively refresh OAuth tokens expiring within buffer_minutes (default 60).
    Handles both GHL tokens (24h expiry) and HubSpot tokens (6h expiry).
    Schedule every 15 minutes.
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        from ghl_api import refresh_tokens_proactively
        buffer_minutes = int(request.args.get("buffer", 60))
        stats = refresh_tokens_proactively(buffer_minutes=buffer_minutes)

        # Also refresh HubSpot tokens that are expiring soon
        hs_refreshed = 0
        try:
            from db import get_db_connection, return_db_connection
            from crm_providers.hubspot.oauth import refresh_hubspot_token
            import time as _time
            conn = get_db_connection()
            if conn:
                try:
                    cur = conn.cursor()
                    # Find HubSpot subscribers with tokens expiring within buffer
                    threshold = int(_time.time()) + (buffer_minutes * 60)
                    # token_expires_at may be a Unix timestamp (bigint string) or ISO datetime string.
                    # Handle both: if it's all digits treat as epoch, otherwise parse as timestamp.
                    cur.execute("""
                        SELECT location_id, crm_config FROM subscribers
                        WHERE crm_type = 'hubspot'
                          AND crm_config IS NOT NULL
                          AND CASE
                              WHEN crm_config->>'token_expires_at' ~ E'^\\d+$'
                              THEN (crm_config->>'token_expires_at')::bigint
                              ELSE EXTRACT(EPOCH FROM (crm_config->>'token_expires_at')::timestamptz)::bigint
                          END < %s
                    """, (threshold,))
                    for row in cur.fetchall():
                        try:
                            result = refresh_hubspot_token(dict(row))
                            if result:
                                hs_refreshed += 1
                        except Exception as e:
                            logger.warning(f"HubSpot token refresh failed for {row.get('location_id')}: {e}")
                finally:
                    return_db_connection(conn)
        except Exception as e:
            logger.warning(f"HubSpot token refresh scan failed (non-fatal): {e}")

        stats["hubspot_refreshed"] = hs_refreshed
        return safe_jsonify({"success": True, **stats})
    except Exception as e:
        logger.error(f"Cron refresh-tokens crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": "Internal server error"}), 200


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
        return safe_jsonify({"success": False, "error": "Internal server error"}), 200


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
        return safe_jsonify({"success": False, "error": "Internal server error"}), 200


# ── GHL data sync ────────────────────────────────────────────────────────────

@cron_bp.route("/api/cron/sync-ghl-data", methods=["GET", "POST"])
def api_cron_sync_ghl_data():
    """
    Run incremental CRM data sync for all active subscribers.
    Dispatches to the correct sync engine based on subscriber's crm_type:
      - GHL: ghl_sync.run_incremental_sync_all()
      - HubSpot: crm_providers.hubspot.sync.sync_all_hubspot()
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
            job_timeout=900,
            result_ttl=86400,
        )

        # Also queue HubSpot sync for HubSpot subscribers
        hubspot_job_id = None
        try:
            from crm_providers.hubspot.sync import sync_all_hubspot
            hs_job = extensions.q_website.enqueue(
                sync_all_hubspot,
                job_timeout=900,
                result_ttl=86400,
            )
            hubspot_job_id = hs_job.id
        except Exception as e:
            logger.warning(f"HubSpot sync queue failed (non-fatal): {e}")

        return safe_jsonify({
            "success": True, "queued": True,
            "ghl_job_id": job.id,
            "hubspot_job_id": hubspot_job_id,
        })
    except Exception as e:
        logger.error(f"Cron sync-ghl-data crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": "Internal server error"}), 200


# ── Trust Hub status refresh ───────────────────────────────────────────────

@cron_bp.route("/api/cron/trust-hub-refresh", methods=["GET", "POST"])
def api_cron_trust_hub_refresh():
    """
    Poll all pending Trust Hub profiles and auto-create CNAM/Voice Integrity
    on approval. Schedule every 30 minutes via cron.
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        if not extensions.ensure_redis():
            return safe_jsonify({"success": False, "error": "Redis unavailable"}), 503

        from tasks import refresh_pending_trust_hub_profiles
        job = extensions.q_website.enqueue(
            refresh_pending_trust_hub_profiles,
            job_timeout=600,
            result_ttl=86400,
        )
        return safe_jsonify({"success": True, "queued": True, "job_id": job.id})
    except Exception as e:
        logger.error(f"Cron trust-hub-refresh crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": "Internal server error"}), 200


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
        return safe_jsonify({"success": False, "error": "Internal server error"}), 200


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
        return safe_jsonify({"success": False, "error": "Internal server error"}), 200


@cron_bp.route("/api/cron/process-ghl-loops", methods=["GET", "POST"])
def api_cron_process_ghl_loops():
    """
    Execute pending GHL custom action loop iterations.
    Finds active loops with next_execute_at <= NOW(), runs the action,
    and either schedules the next iteration or fires the loop_completed trigger.
    Schedule every 1-5 minutes via external cron.
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        from voice.outbound import process_ghl_action_loops
        result = process_ghl_action_loops()
        return safe_jsonify({"success": True, **result})
    except Exception as e:
        logger.error(f"Cron process-ghl-loops crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": "Internal server error"}), 200


@cron_bp.route("/api/cron/process-workflow-delays", methods=["GET", "POST"])
def api_cron_process_workflow_delays():
    """
    Resume workflow runs that have pending delays (next_execute_at <= NOW()).
    Run every 1 minute via external cron.
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        from workflow_engine import process_pending_delays
        result = process_pending_delays()
        return safe_jsonify({"success": True, "processed": result})
    except Exception as e:
        logger.error(f"Cron process-workflow-delays crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": "Internal server error"}), 200


@cron_bp.route("/api/cron/error-feed", methods=["GET"])
def api_error_feed():
    """
    Event-driven error monitoring endpoint. Returns recent ERROR+ logs
    captured across all services (Flask, Voice, Workers) via Redis.

    Query params:
        since: Unix epoch timestamp — only return errors after this time
        limit: max entries (default 50)

    Returns JSON: {errors: [...], count: N, latest_epoch: float}
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        from error_feed import get_recent_errors
        since = request.args.get("since", type=float)
        limit = request.args.get("limit", 50, type=int)

        if not extensions.ensure_redis():
            return safe_jsonify({"errors": [], "count": 0, "redis": False}), 503

        errors = get_recent_errors(extensions.redis_conn, since_epoch=since, limit=limit)
        latest = errors[0]["epoch"] if errors else (since or 0)

        return safe_jsonify({
            "errors": errors,
            "count": len(errors),
            "latest_epoch": latest,
        })
    except Exception as e:
        logger.error(f"Error feed crashed: {e}", exc_info=True)
        return safe_jsonify({"errors": [], "count": 0, "error": str(e)}), 200


@cron_bp.route("/api/cron/process-workflow-triggers", methods=["GET", "POST"])
def api_cron_process_workflow_triggers():
    """
    Poll time-based workflow triggers (scheduled, no_response, lead_age, birthday).
    These triggers don't depend on webhook events — they fire based on time/data conditions.
    Run every 1-5 minutes via external cron.
    """
    if not _cron_authorized():
        return safe_jsonify({"error": "Unauthorized"}), 401

    try:
        from workflow_engine import process_time_based_triggers
        result = process_time_based_triggers()
        return safe_jsonify({"success": True, "runs_created": result})
    except Exception as e:
        logger.error(f"Cron process-workflow-triggers crashed: {e}", exc_info=True)
        return safe_jsonify({"success": False, "error": "Internal server error"}), 200


# ── Stripe Sync (daily reconciliation) ───────────────────────────────────────

@cron_bp.route("/api/cron/stripe-sync", methods=["GET", "POST"])
def cron_stripe_sync():
    """Daily Stripe reconciliation: sync stripe_status and subscription_tier
    with live Stripe data for all subscribers with a stripe_customer_id.
    Catches any webhook events that were missed or fired before code updates.
    Schedule: once daily (e.g. 3:00 AM UTC).
    """
    if not _cron_authorized():
        return flask_jsonify({"error": "Unauthorized"}), 401

    import stripe as stripe_lib
    from psycopg2.extras import RealDictCursor
    from db import get_db_connection, return_db_connection

    conn = get_db_connection()
    if not conn:
        return flask_jsonify({"error": "DB unavailable"}), 503

    report = {"synced": [], "unchanged": 0, "total": 0, "errors": []}

    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT email, stripe_customer_id, stripe_status, subscription_tier
            FROM subscribers
            WHERE stripe_customer_id IS NOT NULL
              AND stripe_customer_id != ''
              AND stripe_customer_id NOT LIKE 'admin%%'
              AND stripe_customer_id NOT LIKE 'cus_demo%%'
        """)
        rows = cur.fetchall()
        report["total"] = len(rows)

        # Build price→tier mapping from env
        price_to_tier = {v: k for k, v in {
            "sms_bot": os.getenv("STRIPE_SMS_BOT_PRICE_ID"),
            "individual": os.getenv("STRIPE_PRICE_ID"),
            "pro_dialer": os.getenv("STRIPE_PRO_DIALER_PRICE_ID"),
            "solo_predictive": os.getenv("STRIPE_PREDICTIVE_DIALER_PRICE_ID"),
        }.items() if v}

        for row in rows:
            email = row['email']
            cust_id = row['stripe_customer_id']
            db_status = row.get('stripe_status')
            db_tier = row.get('subscription_tier')

            try:
                subs = stripe_lib.Subscription.list(customer=cust_id, limit=10)
                if not subs.data:
                    if db_status != 'canceled' or db_tier is not None:
                        cur.execute(
                            "UPDATE subscribers SET stripe_status='canceled', subscription_tier=NULL, updated_at=NOW() WHERE stripe_customer_id=%s",
                            (cust_id,))
                        conn.commit()
                        report["synced"].append({"email": email, "change": f"{db_status}->canceled, {db_tier}->NULL"})
                    else:
                        report["unchanged"] += 1
                    continue

                priority = {'active': 0, 'trialing': 1, 'past_due': 2, 'unpaid': 3,
                            'incomplete': 4, 'paused': 5, 'canceled': 6, 'incomplete_expired': 7}
                best = min(subs.data, key=lambda s: priority.get(s.status, 99))
                live_status = best.status
                items = best.get("items", {}).get("data", [])
                price_id = items[0]["price"]["id"] if items else ""
                live_tier = price_to_tier.get(price_id)
                if live_status in ('canceled', 'unpaid', 'incomplete_expired'):
                    live_tier = None
                elif live_tier is None and live_status in ('active', 'trialing'):
                    live_tier = 'individual'

                if db_status != live_status or db_tier != live_tier:
                    cur.execute(
                        "UPDATE subscribers SET stripe_status=%s, subscription_tier=%s, updated_at=NOW() WHERE stripe_customer_id=%s",
                        (live_status, live_tier, cust_id))
                    conn.commit()
                    report["synced"].append({"email": email, "change": f"status:{db_status}->{live_status}, tier:{db_tier}->{live_tier}"})
                else:
                    report["unchanged"] += 1

            except Exception as e:
                report["errors"].append({"email": email, "error": str(e)})
                try:
                    conn.rollback()
                except Exception:
                    pass

        cur.close()
    except Exception as e:
        logger.error(f"Cron stripe-sync failed: {e}", exc_info=True)
        return flask_jsonify({"error": str(e)}), 500
    finally:
        return_db_connection(conn)

    logger.info(f"Cron stripe-sync: {len(report['synced'])} synced, {report['unchanged']} unchanged, {len(report['errors'])} errors")
    return flask_jsonify(report)
