# workflow_engine.py — Workflow execution engine for InsuranceGrokBot
#
# Handles trigger matching, step execution, delay/wait resumption, and
# individual step handlers for the visual workflow builder.
#
# Entry points:
#   check_workflow_triggers()  — called from webhook processing when events arrive
#   execute_workflow_run()     — executes a run step-by-step until delay or completion
#   process_pending_delays()   — cron job to resume runs whose delays have elapsed

import json
import logging
import re
import time
import requests
from datetime import datetime, timedelta, timezone

from db import (
    get_db_connection,
    return_db_connection,
    get_subscriber_info_hybrid,
    log_webhook_event,
)
from ghl_api import get_valid_token
from extensions import ensure_redis, q_production

logger = logging.getLogger("workflow_engine")

GHL_BASE_URL = "https://services.leadconnectorhq.com"
GHL_HEADERS_BASE = {"Version": "2021-07-28", "Content-Type": "application/json"}

# Maximum steps per single execution pass (prevents infinite loops)
MAX_STEPS_PER_RUN = 200

# Trigger type → GHL webhook event_type mapping (default / GHL)
# Event-driven triggers fire when a webhook arrives with a matching event type.
# Time-based triggers (scheduled, no_response, lead_age, birthday_approaching)
# are polled via cron — see process_time_based_triggers().
TRIGGER_EVENT_MAP = {
    "contact_created": "ContactCreate",
    "sms_received": "InboundMessage",
    "inbound_call": "InboundCall",
    "missed_call": "MissedCall",
    "voicemail_received": "VoicemailReceived",
    "tag_added": "TagAdded",
    "tag_removed": "TagRemoved",
    "stage_changed": "OpportunityStageUpdate",
    "appointment_booked": "AppointmentCreate",
    "appointment_noshow": "AppointmentNoShow",
    "contact_dnd": "ContactDndUpdate",
    "field_updated": "ContactUpdate",
}

# Per-CRM trigger event type mappings (keyed by crm_type)
# GHL uses TRIGGER_EVENT_MAP above. Non-GHL CRMs define their own mappings
# via their provider's get_webhook_event_type_map().
TRIGGER_EVENT_MAP_BY_CRM = {
    "ghl": TRIGGER_EVENT_MAP,
    "gohighlevel": TRIGGER_EVENT_MAP,
}

# These triggers are NOT event-driven — they are polled by cron.
CRON_BASED_TRIGGERS = {
    "scheduled",       # Fires on a cron schedule
    "no_response",     # Fires when contact hasn't responded in X days
    "lead_age",        # Fires when lead is X days old
    "birthday_approaching",  # Fires X days before contact's birthday
    "manual",          # Only fires manually via API
}


def _is_ghl(crm_type):
    """Check if a crm_type string is GoHighLevel."""
    return (crm_type or "ghl").lower() in ("ghl", "gohighlevel", "")


def _get_subscriber_crm_type(location_id, subscriber=None):
    """
    Get the CRM type for a subscriber. Caches the subscriber in the
    module-level _subscriber_cache for the current execution.

    Returns: (crm_type: str, subscriber: dict)
    """
    cache_key = f"sub:{location_id}"
    if cache_key in _contact_cache and subscriber is None:
        sub = _contact_cache[cache_key]
        return ((sub.get("crm_type") or "ghl"), sub)
    if subscriber is None:
        subscriber = get_subscriber_info_hybrid(location_id)
    if subscriber:
        _contact_cache[cache_key] = subscriber
    crm_type = (subscriber or {}).get("crm_type", "ghl") or "ghl"
    return (crm_type, subscriber or {})


def get_trigger_event_map_for_crm(crm_type):
    """
    Get the trigger event type mapping for a given CRM.
    Falls back to the default GHL map for unknown CRMs.
    """
    if crm_type in TRIGGER_EVENT_MAP_BY_CRM:
        return TRIGGER_EVENT_MAP_BY_CRM[crm_type]
    # Try to get from provider
    try:
        from crm_providers import get_provider
        provider = get_provider(crm_type)
        if provider:
            mapping = provider.get_webhook_event_type_map()
            if mapping:
                TRIGGER_EVENT_MAP_BY_CRM[crm_type] = mapping
                return mapping
    except Exception:
        pass
    return TRIGGER_EVENT_MAP


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ TRIGGER MATCHING ═════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def check_workflow_triggers(location_id, event_type, contact_id, event_data=None):
    """
    Check if any active workflows match the given event and create runs for them.

    Args:
        location_id: Subscriber location ID
        event_type: GHL event type (e.g. 'ContactCreate', 'InboundMessage')
        contact_id: GHL contact ID
        event_data: Optional dict of event payload data

    Returns:
        list of created run IDs
    """
    if not location_id or not contact_id:
        return []

    # Reverse lookup: event_type → trigger_type(s)
    matching_triggers = [
        trigger for trigger, event in TRIGGER_EVENT_MAP.items()
        if event == event_type
    ]
    if not matching_triggers:
        return []

    conn = get_db_connection()
    if not conn:
        logger.error("workflow_engine: no DB connection for trigger check")
        return []

    run_ids = []
    try:
        cur = conn.cursor()

        # Find active workflows matching any of the trigger types
        placeholders = ",".join(["%s"] * len(matching_triggers))
        cur.execute(f"""
            SELECT id, name, trigger_type, trigger_config
            FROM workflows
            WHERE location_id = %s
              AND status = 'active'
              AND trigger_type IN ({placeholders})
        """, [location_id] + matching_triggers)

        workflows = cur.fetchall()
        if not workflows:
            return []

        for wf in workflows:
            wf_id = wf["id"]
            trigger_config = wf.get("trigger_config") or {}

            # Apply trigger_config filters (e.g. tag_added with specific tag name)
            if not _matches_trigger_config(wf["trigger_type"], trigger_config, event_data):
                logger.debug(f"Workflow {wf_id} trigger config mismatch, skipping")
                continue

            # Check for duplicate running workflows for the same contact
            cur.execute("""
                SELECT id FROM workflow_runs
                WHERE workflow_id = %s AND contact_id = %s AND status = 'running'
                LIMIT 1
            """, (wf_id, contact_id))
            if cur.fetchone():
                logger.info(f"Workflow {wf_id} already running for contact {contact_id}, skipping")
                continue

            # Find the first step (step with no incoming connections)
            cur.execute("""
                SELECT ws.id FROM workflow_steps ws
                WHERE ws.workflow_id = %s
                  AND ws.id NOT IN (
                      SELECT to_step_id FROM workflow_connections WHERE workflow_id = %s
                  )
                ORDER BY ws.position_y ASC, ws.position_x ASC
                LIMIT 1
            """, (wf_id, wf_id))
            first_step = cur.fetchone()
            if not first_step:
                logger.warning(f"Workflow {wf_id} has no entry step, skipping")
                continue

            # Create the run
            # exit_on_reply: if enabled in trigger_config, the workflow will
            # auto-terminate when the contact sends any inbound message
            exit_on_reply = trigger_config.get("exit_on_reply", True)
            context = {
                "event_data": event_data or {},
                "loop_counters": {},
                "exit_on_reply": exit_on_reply,
            }
            cur.execute("""
                INSERT INTO workflow_runs (workflow_id, contact_id, status, current_step_id, context)
                VALUES (%s, %s, 'running', %s, %s)
                RETURNING id
            """, (wf_id, contact_id, first_step["id"], json.dumps(context)))
            run_row = cur.fetchone()
            run_id = run_row["id"]
            run_ids.append(run_id)

            # Increment workflow stats
            cur.execute("""
                UPDATE workflows
                SET stats = jsonb_set(
                    COALESCE(stats, '{}'),
                    '{runs}',
                    to_jsonb(COALESCE((stats->>'runs')::int, 0) + 1)
                ),
                updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (wf_id,))

            conn.commit()
            logger.info(f"Workflow run {run_id} created for workflow '{wf['name']}' "
                        f"(contact={contact_id}, event={event_type})")

            # Queue execution
            _enqueue_run(run_id)

    except Exception as e:
        logger.error(f"Error checking workflow triggers: {e}", exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)

    return run_ids


def _matches_trigger_config(trigger_type, trigger_config, event_data):
    """Check if event_data satisfies the trigger's filter config."""
    if not trigger_config:
        return True
    if not event_data:
        event_data = {}

    if trigger_type == "tag_added":
        required_tag = trigger_config.get("tag")
        if required_tag:
            tags = event_data.get("tags", [])
            if isinstance(tags, str):
                tags = [tags]
            return required_tag.lower() in [t.lower() for t in tags]

    if trigger_type == "tag_removed":
        required_tag = trigger_config.get("tag")
        if required_tag:
            tags = event_data.get("tags", [])
            if isinstance(tags, str):
                tags = [tags]
            return required_tag.lower() in [t.lower() for t in tags]

    if trigger_type == "sms_received":
        keyword = trigger_config.get("keyword_filter", "").strip().lower()
        if keyword:
            message = str(event_data.get("message", "") or event_data.get("body", "")).lower()
            return keyword in message

    if trigger_type == "stage_changed":
        pipeline_id = trigger_config.get("pipeline_id", "")
        stage_id = trigger_config.get("stage_id", "")
        if pipeline_id and event_data.get("pipeline_id") != pipeline_id:
            return False
        if stage_id and event_data.get("stage_id") != stage_id:
            return False

    if trigger_type == "appointment_booked":
        calendar_id = trigger_config.get("calendar_id", "")
        if calendar_id and event_data.get("calendar_id") != calendar_id:
            return False

    if trigger_type == "contact_dnd":
        # Contact DND update — only trigger when DND is actually set
        dnd = event_data.get("dnd", event_data.get("doNotDisturb"))
        if dnd is not None and not dnd:
            return False  # DND was removed, not set

    if trigger_type == "field_updated":
        field_name = trigger_config.get("field_name", "").strip()
        if field_name:
            changed_fields = event_data.get("changed_fields", [])
            if isinstance(changed_fields, list) and field_name not in changed_fields:
                # Also check if the field exists in the top-level event data
                if field_name not in event_data:
                    return False

    return True


def _enqueue_run(run_id):
    """Queue a workflow run for execution via RQ."""
    try:
        if ensure_redis() and q_production:
            q_production.enqueue(
                execute_workflow_run,
                run_id,
                job_timeout=120,
                retry=None,
            )
            logger.debug(f"Enqueued workflow run {run_id}")
        else:
            logger.warning(f"Redis unavailable, executing workflow run {run_id} inline")
            execute_workflow_run(run_id)
    except Exception as e:
        logger.error(f"Failed to enqueue workflow run {run_id}: {e}")
        # Attempt inline execution as fallback
        try:
            execute_workflow_run(run_id)
        except Exception as e2:
            logger.error(f"Inline execution also failed for run {run_id}: {e2}")


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ RUN EXECUTION ════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def execute_workflow_run(run_id):
    """
    Execute a workflow run step-by-step from its current_step_id.

    Follows connections after each step. Stops on:
    - 'wait' step (sets next_execute_at)
    - 'exit' step (marks completed)
    - no more steps (marks completed)
    - error (marks failed)
    - step limit exceeded (marks failed)
    """
    conn = get_db_connection()
    if not conn:
        logger.error(f"No DB connection for workflow run {run_id}")
        return

    try:
        cur = conn.cursor()

        # Load run
        cur.execute("""
            SELECT wr.*, w.location_id, w.name as workflow_name
            FROM workflow_runs wr
            JOIN workflows w ON w.id = wr.workflow_id
            WHERE wr.id = %s
        """, (run_id,))
        run = cur.fetchone()
        if not run:
            logger.error(f"Workflow run {run_id} not found")
            return

        if run["status"] != "running":
            logger.info(f"Workflow run {run_id} is {run['status']}, skipping execution")
            return

        location_id = run["location_id"]
        contact_id = run["contact_id"]
        workflow_id = run["workflow_id"]
        context = run.get("context") or {}
        if isinstance(context, str):
            context = json.loads(context)

        current_step_id = run["current_step_id"]
        if not current_step_id:
            _complete_run(cur, conn, run_id, workflow_id)
            return

        # Load all steps and connections for this workflow (avoid N+1 queries)
        cur.execute("SELECT * FROM workflow_steps WHERE workflow_id = %s", (workflow_id,))
        steps_rows = cur.fetchall()
        steps = {s["id"]: s for s in steps_rows}

        cur.execute("SELECT * FROM workflow_connections WHERE workflow_id = %s ORDER BY sort_order", (workflow_id,))
        connections = cur.fetchall()

        # Build connection lookup: from_step_id -> {branch_key: to_step_id}
        conn_map = {}
        for c in connections:
            from_id = c["from_step_id"]
            branch = c.get("branch_key") or "default"
            if from_id not in conn_map:
                conn_map[from_id] = {}
            conn_map[from_id][branch] = c["to_step_id"]

        # Execute step chain
        steps_executed = 0
        while current_step_id and steps_executed < MAX_STEPS_PER_RUN:
            step = steps.get(current_step_id)
            if not step:
                logger.error(f"Step {current_step_id} not found in workflow {workflow_id}")
                _fail_run(cur, conn, run_id, workflow_id, f"Step {current_step_id} not found")
                return

            steps_executed += 1
            step_subtype = step["step_subtype"]

            # Update current step on run
            cur.execute("""
                UPDATE workflow_runs SET current_step_id = %s WHERE id = %s
            """, (current_step_id, run_id))
            conn.commit()

            # Execute the step
            try:
                result = _execute_step(
                    cur, conn, run_id, step, location_id, contact_id, context
                )
            except Exception as e:
                logger.error(f"Step {current_step_id} ({step_subtype}) failed: {e}", exc_info=True)
                _log_step(cur, conn, run_id, current_step_id, "error",
                          {"error": str(e)[:500]})
                _fail_run(cur, conn, run_id, workflow_id, str(e)[:500])
                return

            # Handle step result
            if result is None:
                result = {}

            status = result.get("status", "completed")
            branch_key = result.get("branch_key", "default")

            # Log the step execution
            _log_step(cur, conn, run_id, current_step_id, status, result)

            # Wait/delay — stop execution, cron will resume
            if status == "waiting":
                next_at = result.get("next_execute_at")
                if next_at:
                    next_step_id = conn_map.get(current_step_id, {}).get(branch_key)
                    cur.execute("""
                        UPDATE workflow_runs
                        SET next_execute_at = %s, current_step_id = %s, context = %s
                        WHERE id = %s
                    """, (next_at, next_step_id, json.dumps(context), run_id))
                    conn.commit()
                    logger.info(f"Run {run_id} waiting until {next_at}")
                    return
                # No next_execute_at means skip the wait
                pass

            # Exit — mark completed
            if status == "exit" or step_subtype == "exit":
                _complete_run(cur, conn, run_id, workflow_id)
                return

            # Error on this step but continue if possible
            if status == "error" and result.get("continue", False):
                pass  # fall through to find next step

            # Error that should halt
            if status == "error" and not result.get("continue", False):
                _fail_run(cur, conn, run_id, workflow_id, result.get("error", "Step error"))
                return

            # Goto — jump to target step
            if status == "goto":
                target_id = result.get("target_step_id")
                if target_id and target_id in steps:
                    current_step_id = target_id
                    continue
                else:
                    _fail_run(cur, conn, run_id, workflow_id, f"Goto target {target_id} not found")
                    return

            # Find next step via connections
            step_connections = conn_map.get(current_step_id, {})
            next_step_id = step_connections.get(branch_key) or step_connections.get("default")

            if not next_step_id:
                # No more steps — workflow complete
                _complete_run(cur, conn, run_id, workflow_id)
                return

            # Save context and advance
            cur.execute("""
                UPDATE workflow_runs SET context = %s WHERE id = %s
            """, (json.dumps(context), run_id))
            conn.commit()

            current_step_id = next_step_id

        # If we hit the step limit
        if steps_executed >= MAX_STEPS_PER_RUN:
            _fail_run(cur, conn, run_id, workflow_id,
                      f"Exceeded max steps ({MAX_STEPS_PER_RUN})")

    except Exception as e:
        logger.error(f"Fatal error executing workflow run {run_id}: {e}", exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)


def _complete_run(cur, conn, run_id, workflow_id):
    """Mark a workflow run as completed and update workflow stats."""
    try:
        cur.execute("""
            UPDATE workflow_runs
            SET status = 'completed', completed_at = CURRENT_TIMESTAMP, current_step_id = NULL
            WHERE id = %s
        """, (run_id,))
        cur.execute("""
            UPDATE workflows
            SET stats = jsonb_set(
                COALESCE(stats, '{}'),
                '{completed}',
                to_jsonb(COALESCE((stats->>'completed')::int, 0) + 1)
            ),
            updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (workflow_id,))
        conn.commit()
        logger.info(f"Workflow run {run_id} completed")
    except Exception as e:
        logger.error(f"Error completing run {run_id}: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def _fail_run(cur, conn, run_id, workflow_id, error_msg):
    """Mark a workflow run as failed and update workflow stats."""
    try:
        cur.execute("""
            UPDATE workflow_runs
            SET status = 'failed', completed_at = CURRENT_TIMESTAMP, error = %s
            WHERE id = %s
        """, (error_msg[:1000], run_id))
        cur.execute("""
            UPDATE workflows
            SET stats = jsonb_set(
                COALESCE(stats, '{}'),
                '{failed}',
                to_jsonb(COALESCE((stats->>'failed')::int, 0) + 1)
            ),
            updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (workflow_id,))
        conn.commit()
        logger.error(f"Workflow run {run_id} failed: {error_msg}")
    except Exception as e:
        logger.error(f"Error marking run {run_id} as failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def _log_step(cur, conn, run_id, step_id, status, result=None):
    """Insert a step execution log entry."""
    try:
        cur.execute("""
            INSERT INTO workflow_step_logs (run_id, step_id, status, result)
            VALUES (%s, %s, %s, %s)
        """, (run_id, step_id, status, json.dumps(result or {})))
        conn.commit()
    except Exception as e:
        logger.debug(f"Failed to log step execution: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ STEP DISPATCH ════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _execute_step(cur, conn, run_id, step, location_id, contact_id, context):
    """
    Dispatch to the correct step handler based on step_subtype.

    Returns a dict with at minimum:
        {"status": "completed"|"waiting"|"error"|"exit"|"goto"|"skipped"}
    """
    subtype = step["step_subtype"]
    config = step.get("config") or {}
    if isinstance(config, str):
        config = json.loads(config)

    # ── Global auto-exit on contact reply ──
    # If the workflow has exit_on_reply enabled, check if the contact has
    # sent an inbound message SINCE this workflow run started.
    if context.get("exit_on_reply", False) and contact_id != "scheduled_trigger":
        if _contact_replied_since_run_start(run_id, contact_id):
            logger.info(f"Auto-exit: contact {contact_id} replied during run {run_id}")
            return {"status": "exit", "reason": "contact_replied"}

    handler = STEP_HANDLERS.get(subtype)
    if not handler:
        logger.warning(f"Unknown step subtype: {subtype}")
        return {"status": "completed", "note": f"Unknown subtype {subtype}, skipped"}

    # Also allow if_else steps to reference query_results from context
    if subtype == "if_else" and "query_results" in context:
        # Inject query_results into config so conditions can reference them
        config["_query_results"] = context["query_results"]

    return handler(cur, conn, run_id, step, config, location_id, contact_id, context)


def _contact_replied_since_run_start(run_id, contact_id):
    """Check if a contact has sent an inbound message since the workflow run started."""
    conn2 = get_db_connection()
    if not conn2:
        return False
    try:
        cur2 = conn2.cursor()
        cur2.execute("""
            SELECT 1 FROM contact_messages cm
            JOIN workflow_runs wr ON wr.id = %s
            WHERE cm.contact_id = %s
              AND cm.message_type = 'lead'
              AND cm.created_at > wr.started_at
            LIMIT 1
        """, (run_id, contact_id))
        return cur2.fetchone() is not None
    except Exception:
        return False
    finally:
        return_db_connection(conn2)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ STEP HANDLERS ════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _handle_send_sms(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Send an SMS with merge field interpolation and from_strategy support."""
    message_template = config.get("message", "")
    if not message_template:
        return {"status": "error", "error": "No message configured", "continue": True}

    # Fetch contact data for merge fields
    contact = _fetch_contact(location_id, contact_id)
    if not contact:
        return {"status": "error", "error": "Contact not found", "continue": True}

    message = _interpolate_merge_fields(message_template, contact)
    phone = contact.get("phone")
    if not phone:
        return {"status": "error", "error": "Contact has no phone number", "continue": True}

    # Determine send method based on subscriber config
    subscriber = get_subscriber_info_hybrid(location_id)
    if not subscriber:
        return {"status": "error", "error": "Subscriber not found", "continue": True}

    crm_type = (subscriber.get("crm_type") or "ghl").lower()
    sms_send_via = subscriber.get("sms_send_via", "ghl")
    from_strategy = config.get("from_strategy", "default")

    if sms_send_via and sms_send_via.startswith("+"):
        # Send via Twilio (works for all CRM types)
        from_number = sms_send_via
        if from_strategy == "closest_state":
            from_number = _get_closest_state_number(subscriber, phone) or from_number
        elif from_strategy == "rotate":
            from_number = _get_rotated_number(subscriber) or from_number

        sub_sid = subscriber.get("twilio_sub_account_sid")
        sub_token = subscriber.get("twilio_auth_token")
        if not sub_sid or not sub_token:
            return {"status": "error", "error": "Twilio sub-account not configured", "continue": True}

        from twilio_sms import send_sms_via_twilio
        success, fail_reason, detail = send_sms_via_twilio(
            phone_to=phone,
            message=message,
            from_number=from_number,
            twilio_sub_account_sid=sub_sid,
            twilio_auth_token=sub_token,
            contact_id=contact_id,
        )
    elif not _is_ghl(crm_type):
        # Non-GHL CRM — use CRM adapter for messaging
        try:
            from crm_adapters.factory import get_adapter_for_subscriber
            adapter = get_adapter_for_subscriber(subscriber)
            success = adapter.send_message(contact_id, message, phone=phone)
            fail_reason = "" if success else "adapter_send_failed"
            detail = None
        except Exception as e:
            success, fail_reason, detail = False, f"adapter_error: {e}", str(e)
    else:
        # Send via GHL
        token = get_valid_token(location_id)
        if not token:
            return {"status": "error", "error": "No valid GHL token", "continue": True}

        from ghl_message import send_sms_via_ghl
        success, fail_reason, detail = send_sms_via_ghl(
            contact_id=contact_id,
            message=message,
            access_token=token,
            location_id=location_id,
        )

    if success:
        logger.info(f"Workflow SMS sent to {contact_id} (run={run_id})")
        log_webhook_event(location_id, "workflow_sms", "success",
                          f"Workflow SMS sent to contact", contact_id)
        return {"status": "completed", "message_sent": True}
    else:
        logger.warning(f"Workflow SMS failed for {contact_id}: {fail_reason}")
        return {"status": "error", "error": f"SMS failed: {fail_reason}",
                "detail": str(detail)[:200] if detail else None, "continue": True}


def _handle_ai_call(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Initiate an AI voice call to the contact."""
    contact = _fetch_contact(location_id, contact_id)
    if not contact:
        return {"status": "error", "error": "Contact not found", "continue": True}

    phone = contact.get("phone")
    if not phone:
        return {"status": "error", "error": "Contact has no phone number", "continue": True}

    subscriber = get_subscriber_info_hybrid(location_id)
    if not subscriber:
        return {"status": "error", "error": "Subscriber not found", "continue": True}

    # Queue AI call via the outbound call endpoint pattern
    try:
        from voice.helpers import _get_subscriber_by_location
        import twilio_provisioning
        from voice.call_history_helpers import save_call_to_history
        from number_health import select_outbound_number

        voice_config = subscriber.get("voice_config") or {}
        if isinstance(voice_config, str):
            voice_config = json.loads(voice_config)

        from_number = select_outbound_number(location_id, voice_config, dest_phone=phone)
        if not from_number:
            from_number = subscriber.get("twilio_phone_number")
        if not from_number:
            return {"status": "error", "error": "No outbound number available", "continue": True}

        sub_sid = subscriber.get("twilio_sub_account_sid")
        sub_token = subscriber.get("twilio_auth_token")
        if not sub_sid or not sub_token:
            return {"status": "error", "error": "Twilio credentials not configured", "continue": True}

        domain = subscriber.get("your_domain") or "https://insurancegrokbot.click"
        twiml_url = f"{domain}/voice/twiml/outbound?location_id={location_id}&contact_id={contact_id}"

        client = twilio_provisioning.get_sub_account_client_native(sub_sid, sub_token)
        call = client.calls.create(
            to=phone,
            from_=from_number,
            url=twiml_url,
            status_callback=f"{domain}/voice/status-callback",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            machine_detection="Enable",
        )

        logger.info(f"Workflow AI call initiated: {call.sid} to {phone} (run={run_id})")
        log_webhook_event(location_id, "workflow_call", "success",
                          f"AI call initiated to contact", contact_id)
        return {"status": "completed", "call_sid": call.sid}

    except Exception as e:
        logger.error(f"Workflow AI call failed: {e}", exc_info=True)
        return {"status": "error", "error": f"Call failed: {str(e)[:200]}", "continue": True}


def _handle_add_tag(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Add a tag to the contact (CRM-aware)."""
    tag = (config.get("tag") or config.get("tag_name") or "").strip()
    if not tag:
        return {"status": "error", "error": "No tag specified", "continue": True}

    crm_type, subscriber = _get_subscriber_crm_type(location_id)

    if not _is_ghl(crm_type):
        # Non-GHL: use CRM adapter
        try:
            from crm_adapters.factory import get_adapter_for_subscriber
            adapter = get_adapter_for_subscriber(subscriber)
            # Get current contact, add tag, update
            contact = adapter.get_contact(contact_id)
            if not contact:
                return {"status": "error", "error": "Contact not found", "continue": True}
            current_tags = contact.get("tags", [])
            if isinstance(current_tags, list) and tag in current_tags:
                return {"status": "completed", "note": "Tag already present"}
            success = adapter.update_contact(contact_id, {"tags": (current_tags or []) + [tag]})
            if success:
                logger.info(f"Tag '{tag}' added to {contact_id} via {crm_type} (run={run_id})")
                return {"status": "completed", "tag": tag}
            return {"status": "error", "error": f"{crm_type} tag update failed", "continue": True}
        except Exception as e:
            return {"status": "error", "error": f"Tag add failed: {e}", "continue": True}

    # GHL path — unchanged
    token = get_valid_token(location_id)
    if not token:
        return {"status": "error", "error": "No valid GHL token", "continue": True}

    headers = _ghl_headers(token)

    # Fetch current tags
    try:
        resp = requests.get(
            f"{GHL_BASE_URL}/contacts/{contact_id}",
            headers=headers, timeout=10,
        )
        resp.raise_for_status()
        contact_data = resp.json().get("contact", {})
        current_tags = contact_data.get("tags", [])
    except Exception as e:
        return {"status": "error", "error": f"Failed to fetch contact: {e}", "continue": True}

    if tag in current_tags:
        return {"status": "completed", "note": "Tag already present"}

    current_tags.append(tag)
    try:
        resp = requests.put(
            f"{GHL_BASE_URL}/contacts/{contact_id}",
            headers=headers, json={"tags": current_tags}, timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"Tag '{tag}' added to {contact_id} (run={run_id})")
        return {"status": "completed", "tag": tag}
    except Exception as e:
        return {"status": "error", "error": f"Failed to add tag: {e}", "continue": True}


def _handle_remove_tag(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Remove a tag from the contact (CRM-aware)."""
    tag = (config.get("tag") or config.get("tag_name") or "").strip()
    if not tag:
        return {"status": "error", "error": "No tag specified", "continue": True}

    crm_type, subscriber = _get_subscriber_crm_type(location_id)

    if not _is_ghl(crm_type):
        # Non-GHL: use CRM adapter
        try:
            from crm_adapters.factory import get_adapter_for_subscriber
            adapter = get_adapter_for_subscriber(subscriber)
            contact = adapter.get_contact(contact_id)
            if not contact:
                return {"status": "error", "error": "Contact not found", "continue": True}
            current_tags = contact.get("tags", [])
            if isinstance(current_tags, list) and tag not in current_tags:
                return {"status": "completed", "note": "Tag not present"}
            new_tags = [t for t in (current_tags or []) if t != tag]
            success = adapter.update_contact(contact_id, {"tags": new_tags})
            if success:
                logger.info(f"Tag '{tag}' removed from {contact_id} via {crm_type} (run={run_id})")
                return {"status": "completed", "tag": tag}
            return {"status": "error", "error": f"{crm_type} tag update failed", "continue": True}
        except Exception as e:
            return {"status": "error", "error": f"Tag remove failed: {e}", "continue": True}

    # GHL path — unchanged
    token = get_valid_token(location_id)
    if not token:
        return {"status": "error", "error": "No valid GHL token", "continue": True}

    headers = _ghl_headers(token)

    try:
        resp = requests.get(
            f"{GHL_BASE_URL}/contacts/{contact_id}",
            headers=headers, timeout=10,
        )
        resp.raise_for_status()
        contact_data = resp.json().get("contact", {})
        current_tags = contact_data.get("tags", [])
    except Exception as e:
        return {"status": "error", "error": f"Failed to fetch contact: {e}", "continue": True}

    if tag not in current_tags:
        return {"status": "completed", "note": "Tag not present"}

    current_tags.remove(tag)
    try:
        resp = requests.put(
            f"{GHL_BASE_URL}/contacts/{contact_id}",
            headers=headers, json={"tags": current_tags}, timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"Tag '{tag}' removed from {contact_id} (run={run_id})")
        return {"status": "completed", "tag": tag}
    except Exception as e:
        return {"status": "error", "error": f"Failed to remove tag: {e}", "continue": True}


def _handle_wait(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Set a delay and pause execution until the duration elapses."""
    duration = config.get("duration", 1)
    unit = config.get("unit", "minutes")

    try:
        duration = int(duration)
    except (ValueError, TypeError):
        duration = 1

    if duration <= 0:
        return {"status": "completed", "note": "Zero/negative wait, skipped"}

    if unit == "seconds":
        delta = timedelta(seconds=duration)
    elif unit == "minutes":
        delta = timedelta(minutes=duration)
    elif unit == "hours":
        delta = timedelta(hours=duration)
    elif unit == "days":
        delta = timedelta(days=duration)
    else:
        delta = timedelta(minutes=duration)

    next_at = datetime.now(timezone.utc) + delta
    logger.info(f"Run {run_id} waiting {duration} {unit} until {next_at}")
    return {
        "status": "waiting",
        "next_execute_at": next_at.isoformat(),
        "duration": duration,
        "unit": unit,
    }


def _handle_if_else(cur, conn, run_id, step, config, location_id, contact_id, context):
    """
    Evaluate conditions and choose the 'true' or 'false' branch.

    Supports both single condition format:
        {"field": "...", "operator": "...", "value": "..."}
    And multi-condition format:
        {"conditions": [{"field": "...", "operator": "...", "value": "..."}], "logic": "and"|"or"}
    """
    conditions = config.get("conditions", [])
    logic = config.get("logic", "and").lower()

    # Single condition fallback
    if not conditions:
        field = config.get("field", "")
        operator = config.get("operator", "equals")
        value = config.get("value", "")
        if field or operator not in ("equals", ""):
            conditions = [{"field": field, "operator": operator, "value": value}]

    if not conditions:
        # No conditions at all — default to true
        return {"status": "completed", "branch_key": "true", "condition_result": True}

    results = []
    for cond in conditions:
        field = cond.get("field", "")
        operator = cond.get("operator", "equals")
        value = cond.get("value", "")
        r = _evaluate_condition(field, operator, value, location_id, contact_id, context)
        results.append(r)

    if logic == "or":
        final = any(results)
    else:
        final = all(results)

    branch = "true" if final else "false"
    logger.debug(f"If/else ({logic}): {len(conditions)} conditions -> {results} -> {branch}")
    return {"status": "completed", "branch_key": branch, "condition_result": final,
            "individual_results": results}


def _handle_loop(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Increment loop counter and check against max_iterations."""
    step_id = step["id"]
    max_iterations = config.get("max_iterations", 5)
    try:
        max_iterations = int(max_iterations)
    except (ValueError, TypeError):
        max_iterations = 5

    loop_counters = context.get("loop_counters", {})
    current = loop_counters.get(step_id, 0)
    current += 1
    loop_counters[step_id] = current
    context["loop_counters"] = loop_counters

    if current >= max_iterations:
        logger.info(f"Loop {step_id} reached max iterations ({max_iterations}), exiting loop")
        return {"status": "completed", "branch_key": "exit",
                "iteration": current, "max": max_iterations}

    return {"status": "completed", "branch_key": "loop",
            "iteration": current, "max": max_iterations}


def _handle_update_field(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Update a field on the contact (CRM-aware)."""
    field_key = (config.get("field_key") or config.get("field") or "").strip()
    field_value = config.get("field_value") if config.get("field_value") is not None else config.get("value", "")

    if not field_key:
        return {"status": "error", "error": "No field_key specified", "continue": True}

    # Interpolate merge fields in the value
    contact = _fetch_contact(location_id, contact_id)
    if contact:
        field_value = _interpolate_merge_fields(str(field_value), contact)

    crm_type, subscriber = _get_subscriber_crm_type(location_id)

    if not _is_ghl(crm_type):
        # Non-GHL: use CRM adapter
        try:
            from crm_adapters.factory import get_adapter_for_subscriber
            adapter = get_adapter_for_subscriber(subscriber)
            success = adapter.update_contact(contact_id, {field_key: field_value})
            if success:
                logger.info(f"Updated field '{field_key}' on {contact_id} via {crm_type} (run={run_id})")
                return {"status": "completed", "field": field_key, "value": field_value}
            return {"status": "error", "error": f"{crm_type} field update failed", "continue": True}
        except Exception as e:
            return {"status": "error", "error": f"Field update failed: {e}", "continue": True}

    # GHL path — unchanged
    token = get_valid_token(location_id)
    if not token:
        return {"status": "error", "error": "No valid GHL token", "continue": True}

    try:
        resp = requests.put(
            f"{GHL_BASE_URL}/contacts/{contact_id}",
            headers=_ghl_headers(token),
            json={field_key: field_value},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"Updated field '{field_key}' on {contact_id} (run={run_id})")
        return {"status": "completed", "field": field_key, "value": field_value}
    except Exception as e:
        return {"status": "error", "error": f"Failed to update field: {e}", "continue": True}


def _handle_add_note(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Add a note to the contact (CRM-aware)."""
    body = (config.get("body") or config.get("note") or "").strip()
    if not body:
        return {"status": "error", "error": "No note body specified", "continue": True}

    contact = _fetch_contact(location_id, contact_id)
    if contact:
        body = _interpolate_merge_fields(body, contact)

    crm_type, subscriber = _get_subscriber_crm_type(location_id)

    if not _is_ghl(crm_type):
        # Non-GHL: use provider's log_note for CRMs that support it
        try:
            from crm_providers import get_provider
            provider = get_provider(crm_type)
            if provider and provider.HAS_ACTIVITY_LOGGING:
                crm_config = subscriber.get("crm_config") or {}
                token = crm_config.get("access_token", "")
                if provider.log_note(contact_id, body, token):
                    logger.info(f"Note added to {contact_id} via {crm_type} (run={run_id})")
                    return {"status": "completed", "note_added": True}
                return {"status": "error", "error": f"{crm_type} note failed", "continue": True}
        except Exception as e:
            return {"status": "error", "error": f"Note add failed: {e}", "continue": True}

    # GHL path — unchanged
    token = get_valid_token(location_id)
    if not token:
        return {"status": "error", "error": "No valid GHL token", "continue": True}

    try:
        resp = requests.post(
            f"{GHL_BASE_URL}/contacts/{contact_id}/notes",
            headers=_ghl_headers(token),
            json={"body": body},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"Note added to {contact_id} (run={run_id})")
        return {"status": "completed", "note_added": True}
    except Exception as e:
        return {"status": "error", "error": f"Failed to add note: {e}", "continue": True}


def _handle_send_webhook(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Send an outbound webhook to a configured URL."""
    from webhook_delivery import deliver_webhook

    url = config.get("url", "").strip()
    if not url:
        return {"status": "error", "error": "No webhook URL configured", "continue": True}

    payload = {
        "event": "workflow.step",
        "workflow_run_id": run_id,
        "step_id": step["id"],
        "location_id": location_id,
        "contact_id": contact_id,
        "data": config.get("payload", {}),
        "timestamp": int(time.time()),
    }

    secret = config.get("secret", "")
    success, status_code, error = deliver_webhook(url, payload, secret=secret)

    if success:
        return {"status": "completed", "webhook_sent": True, "status_code": status_code}
    else:
        return {"status": "error", "error": f"Webhook failed: {error}", "continue": True}


def _handle_goto(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Jump to a target step by ID."""
    target_id = config.get("target_step_id", "")
    if not target_id:
        return {"status": "error", "error": "No target_step_id configured", "continue": True}
    return {"status": "goto", "target_step_id": target_id}


def _handle_exit(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Mark the workflow run as completed."""
    return {"status": "exit"}


def _handle_custom(cur, conn, run_id, step, config, location_id, contact_id, context):
    """
    Custom action execution — interprets freeform config via AI when needed.

    Custom actions can be:
    1. Simple mapped types (webhook, tag combo, field update) — executed directly
    2. Complex AI-interpreted actions — AI generates execution plan from description
    """
    action_name = config.get("action_name", config.get("custom_type", "custom"))
    description = config.get("description", "")

    # If the custom action has sub-actions defined, execute them as a mini-pipeline
    sub_actions = config.get("sub_actions", [])
    if sub_actions:
        return _execute_custom_sub_actions(
            cur, conn, run_id, step, sub_actions, location_id, contact_id, context)

    # If this custom action maps to a known action type, delegate
    mapped_type = config.get("execute_as")
    if mapped_type and mapped_type in STEP_HANDLERS:
        mapped_config = config.get("execute_config", {})
        logger.info(f"Custom action '{action_name}' delegating to {mapped_type}")
        return STEP_HANDLERS[mapped_type](
            cur, conn, run_id, step, mapped_config, location_id, contact_id, context)

    # If it has a webhook URL, treat as webhook action
    if config.get("webhook_url"):
        webhook_config = {
            "url": config["webhook_url"],
            "payload": {
                "action": action_name,
                "description": description,
                "config": {k: v for k, v in config.items()
                           if k not in ("webhook_url", "action_name", "description")},
            },
        }
        return _handle_send_webhook(cur, conn, run_id, step, webhook_config,
                                      location_id, contact_id, context)

    # For truly freeform custom actions, attempt AI interpretation
    if description:
        return _ai_execute_custom_action(
            cur, conn, run_id, step, config, location_id, contact_id, context)

    logger.info(f"Custom action '{action_name}' has no executable config, marking complete")
    return {"status": "completed", "action": action_name, "note": "Custom action logged"}


def _execute_custom_sub_actions(cur, conn, run_id, step, sub_actions, location_id, contact_id, context):
    """Execute a list of sub-actions defined in a custom action's config."""
    results = []
    for i, sub in enumerate(sub_actions):
        sub_type = sub.get("type", "")
        sub_config = sub.get("config", {})
        handler = STEP_HANDLERS.get(sub_type)
        if not handler:
            results.append({"sub_action": i, "status": "skipped", "reason": f"Unknown type: {sub_type}"})
            continue
        try:
            result = handler(cur, conn, run_id, step, sub_config, location_id, contact_id, context)
            results.append({"sub_action": i, "type": sub_type, **result})
            # If any sub-action returns waiting or error (non-continue), stop
            if result.get("status") == "waiting":
                return result
            if result.get("status") == "error" and not result.get("continue", True):
                return result
        except Exception as e:
            results.append({"sub_action": i, "status": "error", "error": str(e)[:200]})

    return {"status": "completed", "sub_results": results}


def _ai_execute_custom_action(cur, conn, run_id, step, config, location_id, contact_id, context):
    """
    Use AI to interpret a freeform custom action description and execute it
    by mapping to available step handlers.
    """
    description = config.get("description", "")
    try:
        from extensions import get_client
        xai = get_client()
        if not xai:
            logger.warning("AI unavailable for custom action interpretation")
            return {"status": "completed", "note": "AI unavailable, action logged only"}

        contact = _fetch_contact(location_id, contact_id)
        contact_summary = ""
        if contact:
            contact_summary = (f"Contact: {contact.get('firstName', '')} {contact.get('lastName', '')}, "
                              f"phone: {contact.get('phone', '')}, tags: {contact.get('tags', [])}")

        system_prompt = """You are a workflow action interpreter. Given a custom action description
and contact context, return a JSON array of executable sub-actions.

Available action types and their config:
- send_sms: {"message": "text with {{firstName}} merge fields", "from_strategy": "default"}
- add_tag: {"tag": "tag-name"}
- remove_tag: {"tag": "tag-name"}
- update_field: {"field_key": "fieldName", "field_value": "value"}
- add_note: {"body": "note text"}
- send_webhook: {"url": "https://...", "payload": {}}
- wait: {"duration": 1, "unit": "hours"}

Return ONLY a JSON array like: [{"type": "add_tag", "config": {"tag": "hot-lead"}}]
If you cannot map the description to actions, return: [{"type": "note", "config": {"body": "Custom action: <description>"}}]"""

        response = xai.chat.completions.create(
            model="grok-3-mini-fast",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Action: {description}\n{contact_summary}"},
            ],
            temperature=0.1,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        sub_actions = json.loads(raw)
        if isinstance(sub_actions, list):
            return _execute_custom_sub_actions(
                cur, conn, run_id, step, sub_actions, location_id, contact_id, context)

    except Exception as e:
        logger.error(f"AI custom action interpretation failed: {e}")

    # Fallback: log as a note on the contact
    note_config = {"body": f"[Workflow] Custom action executed: {description}"}
    return _handle_add_note(cur, conn, run_id, step, note_config, location_id, contact_id, context)


def _handle_assign_agent(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Update the contact owner (assigned user) — CRM-aware."""
    assigned_to = config.get("assigned_to", "").strip()
    if not assigned_to:
        return {"status": "error", "error": "No assigned_to user specified", "continue": True}

    crm_type, subscriber = _get_subscriber_crm_type(location_id)

    if not _is_ghl(crm_type):
        # Non-GHL: use CRM adapter to update owner field
        try:
            from crm_adapters.factory import get_adapter_for_subscriber
            adapter = get_adapter_for_subscriber(subscriber)
            # Map to CRM-specific owner field
            owner_field = "hubspot_owner_id" if crm_type == "hubspot" else "assignedTo"
            success = adapter.update_contact(contact_id, {owner_field: assigned_to})
            if success:
                logger.info(f"Contact {contact_id} assigned to {assigned_to} via {crm_type} (run={run_id})")
                return {"status": "completed", "assigned_to": assigned_to}
            return {"status": "error", "error": f"{crm_type} assign failed", "continue": True}
        except Exception as e:
            return {"status": "error", "error": f"Assign agent failed: {e}", "continue": True}

    # GHL path — unchanged
    token = get_valid_token(location_id)
    if not token:
        return {"status": "error", "error": "No valid GHL token", "continue": True}

    try:
        resp = requests.put(
            f"{GHL_BASE_URL}/contacts/{contact_id}",
            headers=_ghl_headers(token),
            json={"assignedTo": assigned_to},
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"Contact {contact_id} assigned to {assigned_to} (run={run_id})")
        return {"status": "completed", "assigned_to": assigned_to}
    except Exception as e:
        return {"status": "error", "error": f"Failed to assign agent: {e}", "continue": True}


def _handle_move_stage(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Move a contact's opportunity/deal to a specified pipeline stage (CRM-aware)."""
    pipeline_id = config.get("pipeline_id", "").strip()
    stage_id = config.get("stage_id", "").strip()

    if not pipeline_id or not stage_id:
        return {"status": "error", "error": "pipeline_id and stage_id required", "continue": True}

    crm_type, subscriber = _get_subscriber_crm_type(location_id)

    if not _is_ghl(crm_type):
        # Non-GHL: use CRM-specific deal/pipeline API
        try:
            from crm_providers import get_provider
            provider = get_provider(crm_type)
            crm_config = subscriber.get("crm_config") or {}
            token = crm_config.get("access_token", "")
            if not token:
                return {"status": "error", "error": f"No {crm_type} token", "continue": True}

            if crm_type == "hubspot":
                # HubSpot: update deal stage via Deals API
                import requests as req
                hs_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                # Search for deals associated with this contact
                search_url = f"https://api.hubapi.com/crm/v3/objects/deals/search"
                search_resp = req.post(search_url, headers=hs_headers, json={
                    "filterGroups": [{"filters": [
                        {"propertyName": "associations.contact", "operator": "EQ", "value": contact_id}
                    ]}],
                    "properties": ["pipeline", "dealstage"],
                    "limit": 5,
                }, timeout=15)
                if search_resp.status_code == 200:
                    deals = search_resp.json().get("results", [])
                    target_deal = None
                    for d in deals:
                        if d.get("properties", {}).get("pipeline") == pipeline_id:
                            target_deal = d
                            break
                    if target_deal:
                        deal_id = target_deal["id"]
                        update_resp = req.patch(
                            f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}",
                            headers=hs_headers,
                            json={"properties": {"dealstage": stage_id}},
                            timeout=15,
                        )
                        if update_resp.status_code == 200:
                            logger.info(f"HubSpot deal {deal_id} moved to stage {stage_id} (run={run_id})")
                            return {"status": "completed", "deal_id": deal_id, "stage_id": stage_id}
                    else:
                        # Create new deal in pipeline
                        contact = _fetch_contact(location_id, contact_id)
                        name = "Workflow Deal"
                        if contact:
                            fn = contact.get("firstName", "")
                            ln = contact.get("lastName", "")
                            name = f"{fn} {ln}".strip() or name
                        create_resp = req.post(
                            "https://api.hubapi.com/crm/v3/objects/deals",
                            headers=hs_headers,
                            json={
                                "properties": {
                                    "dealname": name,
                                    "pipeline": pipeline_id,
                                    "dealstage": stage_id,
                                },
                                "associations": [{
                                    "to": {"id": contact_id},
                                    "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                               "associationTypeId": 3}]  # Deal to Contact
                                }],
                            },
                            timeout=15,
                        )
                        if create_resp.status_code in (200, 201):
                            new_deal = create_resp.json()
                            logger.info(f"HubSpot deal created at stage {stage_id} (run={run_id})")
                            return {"status": "completed", "deal_id": new_deal.get("id"),
                                    "stage_id": stage_id, "created": True}

                return {"status": "error", "error": f"HubSpot deal move failed", "continue": True}
            else:
                # Generic fallback for other CRMs
                return {"status": "error", "error": f"move_stage not supported for {crm_type}",
                        "continue": True}
        except Exception as e:
            return {"status": "error", "error": f"Move stage failed: {e}", "continue": True}

    # GHL path — unchanged
    token = get_valid_token(location_id)
    if not token:
        return {"status": "error", "error": "No valid GHL token", "continue": True}

    headers = _ghl_headers(token)

    try:
        # Find existing opportunity for this contact in the pipeline
        resp = requests.get(
            f"{GHL_BASE_URL}/opportunities/search",
            headers=headers,
            params={"location_id": location_id, "contact_id": contact_id,
                    "pipeline_id": pipeline_id},
            timeout=10,
        )
        resp.raise_for_status()
        opps = resp.json().get("opportunities", [])

        if opps:
            # Update existing opportunity
            opp_id = opps[0]["id"]
            resp = requests.put(
                f"{GHL_BASE_URL}/opportunities/{opp_id}",
                headers=headers,
                json={"pipelineStageId": stage_id},
                timeout=10,
            )
            resp.raise_for_status()
            logger.info(f"Moved opportunity {opp_id} to stage {stage_id} (run={run_id})")
            return {"status": "completed", "opportunity_id": opp_id, "stage_id": stage_id}
        else:
            # Create new opportunity in the pipeline at the target stage
            contact = _fetch_contact(location_id, contact_id)
            name = "Workflow Opportunity"
            if contact:
                fn = contact.get("firstName", "")
                ln = contact.get("lastName", "")
                name = f"{fn} {ln}".strip() or name

            resp = requests.post(
                f"{GHL_BASE_URL}/opportunities/",
                headers=headers,
                json={
                    "pipelineId": pipeline_id,
                    "pipelineStageId": stage_id,
                    "locationId": location_id,
                    "contactId": contact_id,
                    "name": name,
                    "status": "open",
                },
                timeout=10,
            )
            resp.raise_for_status()
            new_opp = resp.json().get("opportunity", {})
            opp_id = new_opp.get("id", "unknown")
            logger.info(f"Created opportunity {opp_id} at stage {stage_id} (run={run_id})")
            return {"status": "completed", "opportunity_id": opp_id, "stage_id": stage_id,
                    "created": True}

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response else 0
        if status_code == 403:
            return {"status": "error",
                    "error": "Missing opportunities.write scope — re-authorize the GHL app",
                    "continue": True}
        return {"status": "error", "error": f"GHL API error: {e}", "continue": True}
    except Exception as e:
        return {"status": "error", "error": f"Move stage failed: {e}", "continue": True}


def _handle_wait_until(cur, conn, run_id, step, config, location_id, contact_id, context):
    """
    Smart Wait — pauses until a CONDITION is met or a max timeout expires.

    Unlike regular 'wait' (which waits a fixed duration), wait_until checks
    a condition on each cron tick. When the condition becomes true, execution
    resumes. If max_wait elapses first, takes the 'timeout' branch.

    Config:
        condition: {"field": "...", "operator": "...", "value": "..."}
        max_wait_hours: 72 (safety cap — auto-resume after this many hours)
        check_interval_minutes: 5 (how often cron re-checks)
    """
    condition = config.get("condition", {})
    max_wait_hours = config.get("max_wait_hours", 72)
    try:
        max_wait_hours = int(max_wait_hours)
    except (ValueError, TypeError):
        max_wait_hours = 72

    # Check if we're on a re-check (cron resumed us to re-evaluate)
    wait_started = context.get("_wait_until_started")
    if wait_started:
        # We've been waiting — check the condition
        if condition:
            field = condition.get("field", "")
            operator = condition.get("operator", "equals")
            value = condition.get("value", "")
            result = _evaluate_condition(field, operator, value, location_id, contact_id, context)
            if result:
                logger.info(f"Wait_until condition met for run {run_id}")
                context.pop("_wait_until_started", None)
                return {"status": "completed", "branch_key": "condition_met",
                        "waited": True, "condition_met": True}

        # Check timeout
        try:
            started_dt = datetime.fromisoformat(wait_started)
            elapsed = (datetime.now(timezone.utc) - started_dt).total_seconds() / 3600
            if elapsed >= max_wait_hours:
                logger.info(f"Wait_until timed out after {elapsed:.1f}h for run {run_id}")
                context.pop("_wait_until_started", None)
                return {"status": "completed", "branch_key": "timeout",
                        "waited": True, "timed_out": True}
        except Exception:
            pass

        # Still waiting — schedule next check
        check_interval = config.get("check_interval_minutes", 5)
        try:
            check_interval = max(1, int(check_interval))
        except (ValueError, TypeError):
            check_interval = 5

        next_check = datetime.now(timezone.utc) + timedelta(minutes=check_interval)
        return {
            "status": "waiting",
            "next_execute_at": next_check.isoformat(),
            "note": "Re-checking condition",
        }

    # First time hitting this step — check condition immediately
    if condition:
        field = condition.get("field", "")
        operator = condition.get("operator", "equals")
        value = condition.get("value", "")
        result = _evaluate_condition(field, operator, value, location_id, contact_id, context)
        if result:
            return {"status": "completed", "branch_key": "condition_met",
                    "condition_met": True}

    # Condition not met — start waiting
    context["_wait_until_started"] = datetime.now(timezone.utc).isoformat()

    check_interval = config.get("check_interval_minutes", 5)
    try:
        check_interval = max(1, int(check_interval))
    except (ValueError, TypeError):
        check_interval = 5

    next_check = datetime.now(timezone.utc) + timedelta(minutes=check_interval)
    logger.info(f"Wait_until started for run {run_id}, next check at {next_check}")
    return {
        "status": "waiting",
        "next_execute_at": next_check.isoformat(),
        "note": f"Waiting for condition (max {max_wait_hours}h)",
    }


def _handle_state_query(cur, conn, run_id, step, config, location_id, contact_id, context):
    """
    State Query — queries the database and stores results in the workflow context.

    This lets subsequent if_else steps branch based on real-time data like
    "when was the last outbound message?" or "how many calls have been made?"

    Config:
        query_type: "last_outbound_message"|"last_inbound_message"|"message_count"|
                    "call_count"|"last_call_date"|"days_since_contact"|"contact_field"
        store_as: "variable_name" (stored in context.query_results)
        field: (for contact_field query type) the field name to read
    """
    query_type = config.get("query_type", "").strip()
    store_as = config.get("store_as", query_type or "query_result")

    if not query_type:
        return {"status": "error", "error": "No query_type specified", "continue": True}

    query_results = context.get("query_results", {})
    result_value = None

    conn2 = get_db_connection()
    if not conn2:
        return {"status": "error", "error": "Database unavailable", "continue": True}

    try:
        cur2 = conn2.cursor()

        if query_type == "last_outbound_message":
            cur2.execute("""
                SELECT created_at, message_text FROM contact_messages
                WHERE contact_id = %s AND message_type = 'assistant'
                ORDER BY created_at DESC LIMIT 1
            """, (contact_id,))
            row = cur2.fetchone()
            if row:
                result_value = row["created_at"].isoformat() if row["created_at"] else None
            else:
                result_value = None

        elif query_type == "last_inbound_message":
            cur2.execute("""
                SELECT created_at, message_text FROM contact_messages
                WHERE contact_id = %s AND message_type = 'lead'
                ORDER BY created_at DESC LIMIT 1
            """, (contact_id,))
            row = cur2.fetchone()
            result_value = row["created_at"].isoformat() if row and row["created_at"] else None

        elif query_type == "message_count":
            direction = config.get("direction", "outbound")
            msg_type = "assistant" if direction == "outbound" else "lead"
            cur2.execute("""
                SELECT COUNT(*) as cnt FROM contact_messages
                WHERE contact_id = %s AND message_type = %s
            """, (contact_id, msg_type))
            row = cur2.fetchone()
            result_value = row["cnt"] if row else 0

        elif query_type == "call_count":
            cur2.execute("""
                SELECT COUNT(*) as cnt FROM call_history
                WHERE contact_id = %s AND location_id = %s
            """, (contact_id, location_id))
            row = cur2.fetchone()
            result_value = row["cnt"] if row else 0

        elif query_type == "last_call_date":
            cur2.execute("""
                SELECT created_at FROM call_history
                WHERE contact_id = %s AND location_id = %s
                ORDER BY created_at DESC LIMIT 1
            """, (contact_id, location_id))
            row = cur2.fetchone()
            result_value = row["created_at"].isoformat() if row and row["created_at"] else None

        elif query_type == "days_since_contact":
            # Days since any inbound or outbound message
            cur2.execute("""
                SELECT MAX(created_at) as last_msg FROM contact_messages
                WHERE contact_id = %s
            """, (contact_id,))
            row = cur2.fetchone()
            if row and row["last_msg"]:
                delta = datetime.now(timezone.utc) - row["last_msg"].replace(tzinfo=timezone.utc)
                result_value = delta.days
            else:
                result_value = 999  # No messages ever

        elif query_type == "contact_field":
            field_name = config.get("field", "")
            if field_name:
                contact = _fetch_contact(location_id, contact_id)
                if contact:
                    result_value = contact.get(field_name)

        elif query_type == "workflow_run_count":
            # How many times this workflow has run for this contact
            wf_id = config.get("workflow_id") or context.get("event_data", {}).get("workflow_id")
            if wf_id:
                cur2.execute("""
                    SELECT COUNT(*) as cnt FROM workflow_runs
                    WHERE workflow_id = %s AND contact_id = %s
                """, (wf_id, contact_id))
            else:
                cur2.execute("""
                    SELECT COUNT(*) as cnt FROM workflow_runs
                    WHERE contact_id = %s
                """, (contact_id,))
            row = cur2.fetchone()
            result_value = row["cnt"] if row else 0

        else:
            return {"status": "error", "error": f"Unknown query_type: {query_type}", "continue": True}

    except Exception as e:
        logger.error(f"State query failed: {e}")
        return {"status": "error", "error": f"Query failed: {e}", "continue": True}
    finally:
        return_db_connection(conn2)

    # Store result in context for downstream steps to use
    query_results[store_as] = result_value
    context["query_results"] = query_results

    logger.debug(f"State query {query_type} = {result_value} (stored as {store_as})")
    return {"status": "completed", "query_type": query_type,
            "result": result_value, "stored_as": store_as}


def _handle_send_igb_message(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Trigger the InsuranceGrokBot AI SMS pipeline for this contact.

    Two modes:
    - "ai" (default): enqueues process_webhook_task with a workflow-outreach flag.
      The full AI pipeline runs: fetches contact context, loads conversation history,
      builds system prompt, calls xAI Grok, generates contextual reply, and sends via
      whatever SMS channel is configured (GHL API or Twilio sub-account).
    - "manual": sends exact user-provided text through the subscriber's configured
      channel, same routing as send_sms but triggers via the IGB pipeline for logging.
    """
    mode = config.get("mode", "ai")
    prompt_hint = config.get("prompt_hint", "")
    manual_message = config.get("manual_message", config.get("message", ""))

    if mode == "manual" and manual_message:
        # Manual mode: send exact text through IGB pipeline
        # Still goes through process_webhook_task for proper logging & channel routing
        contact = _fetch_contact(location_id, contact_id)
        if not contact:
            return {"status": "error", "error": "Contact not found", "continue": True}
        phone = contact.get("phone")
        if not phone:
            return {"status": "error", "error": "Contact has no phone number", "continue": True}
        message = _interpolate_merge_fields(manual_message, contact)

        payload = {
            "contact_id": contact_id,
            "location_id": location_id,
            "phone": phone,
            "_workflow_outreach": True,
            "_manual_message": message,
            "_run_id": run_id,
        }
    else:
        # AI mode: let InsuranceGrokBot generate the message
        payload = {
            "contact_id": contact_id,
            "location_id": location_id,
            "_workflow_outreach": True,
            "_prompt_hint": prompt_hint,
            "_run_id": run_id,
        }

    try:
        ensure_redis()
        q_production.enqueue(
            "tasks.process_webhook_task",
            payload,
            job_timeout=120,
        )
        logger.info(f"IGB message enqueued for {contact_id} mode={mode} (run={run_id})")
        log_webhook_event(location_id, "workflow_igb_message", "queued",
                          f"IGB {mode} message queued for contact", contact_id)
        return {"status": "completed", "queued": True, "mode": mode}
    except Exception as e:
        logger.error(f"Failed to enqueue IGB message for {contact_id}: {e}")
        return {"status": "error", "error": f"Failed to queue message: {str(e)[:100]}",
                "continue": True}


# Step handler registry
STEP_HANDLERS = {
    "send_sms": _handle_send_sms,
    "ai_call": _handle_ai_call,
    "add_tag": _handle_add_tag,
    "remove_tag": _handle_remove_tag,
    "wait": _handle_wait,
    "wait_until": _handle_wait_until,
    "if_else": _handle_if_else,
    "loop": _handle_loop,
    "update_field": _handle_update_field,
    "add_note": _handle_add_note,
    "send_webhook": _handle_send_webhook,
    "goto": _handle_goto,
    "exit": _handle_exit,
    "custom": _handle_custom,
    "assign_agent": _handle_assign_agent,
    "move_stage": _handle_move_stage,
    "state_query": _handle_state_query,
    "send_igb_message": _handle_send_igb_message,
}


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ CONDITION EVALUATION (if_else) ═══════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _evaluate_condition(field, operator, value, location_id, contact_id, context):
    """
    Evaluate a workflow condition against contact data, intelligence, or context.

    Returns True or False.
    """
    # Fetch the field value based on what we're checking
    field_value = _resolve_field_value(field, operator, location_id, contact_id, context)

    # Normalize to strings for comparison where appropriate
    str_field = str(field_value).strip().lower() if field_value is not None else ""
    str_value = str(value).strip().lower() if value is not None else ""

    if operator == "equals":
        return str_field == str_value

    if operator == "not_equals":
        return str_field != str_value

    if operator == "contains":
        return str_value in str_field

    if operator == "starts_with":
        return str_field.startswith(str_value)

    if operator == "is_empty":
        return field_value is None or str_field == ""

    if operator == "is_not_empty":
        return field_value is not None and str_field != ""

    if operator == "greater_than":
        try:
            return float(field_value or 0) > float(value or 0)
        except (ValueError, TypeError):
            return False

    if operator == "less_than":
        try:
            return float(field_value or 0) < float(value or 0)
        except (ValueError, TypeError):
            return False

    if operator == "has_tag":
        contact = _fetch_contact(location_id, contact_id)
        tags = (contact or {}).get("tags", [])
        return str(value).strip().lower() in [t.lower() for t in tags]

    if operator == "no_tag":
        contact = _fetch_contact(location_id, contact_id)
        tags = (contact or {}).get("tags", [])
        return str(value).strip().lower() not in [t.lower() for t in tags]

    if operator == "lead_age_days":
        contact = _fetch_contact(location_id, contact_id)
        if not contact:
            return False
        created = contact.get("dateAdded") or contact.get("createdAt")
        if not created:
            return False
        try:
            if isinstance(created, str):
                # Parse ISO 8601
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                created_dt = created
            age_days = (datetime.now(timezone.utc) - created_dt).days
            return age_days >= int(value or 0)
        except (ValueError, TypeError):
            return False

    if operator == "temperature_is":
        intel = _get_cached_intelligence(contact_id)
        if not intel:
            return False
        return str(intel.get("temperature", "")).lower() == str_value

    if operator == "score_above":
        intel = _get_cached_intelligence(contact_id)
        if not intel:
            return False
        try:
            return float(intel.get("score", 0)) > float(value or 0)
        except (ValueError, TypeError):
            return False

    if operator == "score_below":
        intel = _get_cached_intelligence(contact_id)
        if not intel:
            return False
        try:
            return float(intel.get("score", 0)) < float(value or 0)
        except (ValueError, TypeError):
            return False

    if operator == "responded_within":
        # Check if the contact has responded within N minutes
        try:
            minutes = int(value or 60)
        except (ValueError, TypeError):
            minutes = 60
        conn2 = get_db_connection()
        if not conn2:
            return False
        try:
            cur2 = conn2.cursor()
            cur2.execute("""
                SELECT 1 FROM contact_messages
                WHERE contact_id = %s AND message_type = 'lead'
                  AND created_at > NOW() - make_interval(mins => %s)
                LIMIT 1
            """, (contact_id, minutes))
            return cur2.fetchone() is not None
        except Exception:
            return False
        finally:
            return_db_connection(conn2)

    if operator == "total_messages_sent":
        # Check if total bot messages sent >= value
        conn2 = get_db_connection()
        if not conn2:
            return False
        try:
            cur2 = conn2.cursor()
            cur2.execute("""
                SELECT COUNT(*) as cnt FROM contact_messages
                WHERE contact_id = %s AND message_type = 'assistant'
            """, (contact_id,))
            row = cur2.fetchone()
            count = row["cnt"] if row else 0
            try:
                return count >= int(value or 0)
            except (ValueError, TypeError):
                return False
        except Exception:
            return False
        finally:
            return_db_connection(conn2)

    if operator == "in_state":
        # Check if contact's phone area code maps to the specified US state
        contact = _fetch_contact(location_id, contact_id)
        if not contact:
            return False
        phone = contact.get("phone", "")
        try:
            from voice.predictive_engine import area_code_to_state
            contact_state = area_code_to_state(phone)
            return contact_state and contact_state.lower() == str_value
        except ImportError:
            # Fallback to contact's state field
            return str(contact.get("state", "")).lower() == str_value

    if operator == "time_is_between":
        # Check if current time (in contact's timezone or location default) is between two times
        # Value format: "09:00-17:00" or "9:00 AM-5:00 PM"
        try:
            time_range = str(value).strip()
            if "-" not in time_range:
                return False
            start_str, end_str = time_range.split("-", 1)

            def _parse_time(t):
                t = t.strip()
                for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%H%M"):
                    try:
                        return datetime.strptime(t, fmt).time()
                    except ValueError:
                        continue
                return None

            start_time = _parse_time(start_str)
            end_time = _parse_time(end_str)
            if not start_time or not end_time:
                return False

            # Get local time for the contact
            try:
                import pytz
                # Try to get timezone from contact's area code
                from voice.predictive_engine import area_code_to_timezone
                contact = _fetch_contact(location_id, contact_id)
                phone = (contact or {}).get("phone", "")
                tz_name = area_code_to_timezone(phone) if phone else None
                if not tz_name:
                    sub = get_subscriber_info_hybrid(location_id)
                    tz_name = (sub or {}).get("timezone", "America/New_York")
                tz = pytz.timezone(tz_name)
                local_now = datetime.now(timezone.utc).astimezone(tz).time()
            except Exception:
                local_now = datetime.now(timezone.utc).time()

            if start_time <= end_time:
                return start_time <= local_now <= end_time
            else:
                # Wraps midnight (e.g. 22:00-06:00)
                return local_now >= start_time or local_now <= end_time
        except Exception:
            return False

    logger.warning(f"Unknown condition operator: {operator}")
    return False


def _resolve_field_value(field, operator, location_id, contact_id, context):
    """Resolve a field name to its current value from the contact or context."""
    # Intelligence-based operators resolve their own data
    if operator in ("temperature_is", "score_above", "score_below",
                    "has_tag", "no_tag", "lead_age_days",
                    "responded_within", "total_messages_sent",
                    "in_state", "time_is_between"):
        return None  # Handled directly in _evaluate_condition

    # Check query_results first (from state_query steps)
    query_results = context.get("query_results", {})
    if field in query_results:
        return query_results[field]

    # Check context (for workflow-injected variables)
    event_data = context.get("event_data", {})
    if field in event_data:
        return event_data[field]

    # Fetch from GHL contact
    contact = _fetch_contact(location_id, contact_id)
    if not contact:
        return None

    # Support nested field access with dot notation
    if "." in field:
        parts = field.split(".")
        val = contact
        for part in parts:
            if isinstance(val, dict):
                val = val.get(part)
            else:
                return None
        return val

    return contact.get(field)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ CRON: PROCESS PENDING DELAYS ════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def process_pending_delays():
    """
    Find workflow runs that have a next_execute_at in the past and resume them.
    Called by cron job on a regular interval (e.g. every 60 seconds).

    Returns the number of runs resumed.
    """
    conn = get_db_connection()
    if not conn:
        logger.error("workflow_engine: no DB connection for delay processing")
        return 0

    resumed = 0
    try:
        cur = conn.cursor()

        # Find runs whose delay has elapsed
        cur.execute("""
            SELECT id FROM workflow_runs
            WHERE status = 'running'
              AND next_execute_at IS NOT NULL
              AND next_execute_at <= NOW()
            ORDER BY next_execute_at ASC
            LIMIT 50
        """)
        runs = cur.fetchall()

        if not runs:
            return 0

        logger.info(f"Resuming {len(runs)} delayed workflow runs")

        for run in runs:
            run_id = run["id"]

            # Clear next_execute_at so it doesn't get picked up again
            cur.execute("""
                UPDATE workflow_runs
                SET next_execute_at = NULL
                WHERE id = %s AND status = 'running'
            """, (run_id,))
            conn.commit()

            # Queue execution
            _enqueue_run(run_id)
            resumed += 1

    except Exception as e:
        logger.error(f"Error processing pending delays: {e}", exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)

    logger.info(f"Resumed {resumed} delayed workflow runs")
    return resumed


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ CRON: TIME-BASED TRIGGER POLLING ═══════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def process_time_based_triggers():
    """
    Poll for time-based triggers that don't rely on webhook events.
    Called by cron every 1-5 minutes.

    Handles: scheduled, no_response, lead_age, birthday_approaching
    Returns the number of runs created.
    """
    conn = get_db_connection()
    if not conn:
        logger.error("workflow_engine: no DB for time-based triggers")
        return 0

    created = 0
    try:
        cur = conn.cursor()

        # Get all active workflows with cron-based triggers
        cron_types = list(CRON_BASED_TRIGGERS - {"manual"})
        if not cron_types:
            return 0
        placeholders = ",".join(["%s"] * len(cron_types))
        cur.execute(f"""
            SELECT w.id, w.location_id, w.trigger_type, w.trigger_config
            FROM workflows w
            WHERE w.status = 'active'
              AND w.trigger_type IN ({placeholders})
        """, cron_types)
        workflows = cur.fetchall()

        if not workflows:
            return 0

        now = datetime.now(timezone.utc)

        for wf in workflows:
            wf_id = wf["id"]
            location_id = wf["location_id"]
            trigger_type = wf["trigger_type"]
            trigger_config = wf.get("trigger_config") or {}
            if isinstance(trigger_config, str):
                trigger_config = json.loads(trigger_config)

            try:
                if trigger_type == "scheduled":
                    created += _process_scheduled_trigger(
                        cur, conn, wf_id, location_id, trigger_config, now)
                elif trigger_type == "no_response":
                    created += _process_no_response_trigger(
                        cur, conn, wf_id, location_id, trigger_config, now)
                elif trigger_type == "lead_age":
                    created += _process_lead_age_trigger(
                        cur, conn, wf_id, location_id, trigger_config, now)
                elif trigger_type == "birthday_approaching":
                    created += _process_birthday_trigger(
                        cur, conn, wf_id, location_id, trigger_config, now)
            except Exception as e:
                logger.error(f"Error processing time trigger for workflow {wf_id}: {e}",
                             exc_info=True)
                try:
                    conn.rollback()
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"Error in time-based trigger poll: {e}", exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        return_db_connection(conn)

    if created > 0:
        logger.info(f"Time-based triggers created {created} workflow runs")
    return created


def _process_scheduled_trigger(cur, conn, wf_id, location_id, config, now):
    """Process a scheduled/recurring trigger using cron expression."""
    cron_expr = config.get("cron", "").strip()
    tz_name = config.get("timezone", "America/New_York")
    if not cron_expr:
        return 0

    # Check if this workflow ran recently (within the last interval)
    # to avoid double-firing. Use 90-second window for 1-min cron.
    cur.execute("""
        SELECT 1 FROM workflow_runs
        WHERE workflow_id = %s AND started_at > NOW() - INTERVAL '90 seconds'
        LIMIT 1
    """, (wf_id,))
    if cur.fetchone():
        return 0

    # Parse cron expression (minute hour day_of_month month day_of_week)
    if not _cron_matches_now(cron_expr, tz_name, now):
        return 0

    # Scheduled triggers fire for ALL contacts matching optional tag filter,
    # or as a single trigger with no specific contact (contact_id = 'scheduled')
    tag_filter = config.get("tag_filter", [])
    if tag_filter:
        contacts = _get_contacts_with_tags(location_id, tag_filter)
        count = 0
        for contact_id in contacts:
            run_id = _create_workflow_run(cur, conn, wf_id, contact_id, {"scheduled": True})
            if run_id:
                _enqueue_run(run_id)
                count += 1
        return count
    else:
        # Fire once with a placeholder — useful for batch operations
        run_id = _create_workflow_run(cur, conn, wf_id, "scheduled_trigger",
                                       {"scheduled": True, "fire_time": now.isoformat()})
        if run_id:
            _enqueue_run(run_id)
            return 1
    return 0


def _process_no_response_trigger(cur, conn, wf_id, location_id, config, now):
    """Fire for contacts who haven't responded in X days."""
    days = config.get("days", 3)
    try:
        days = int(days)
    except (ValueError, TypeError):
        days = 3

    # Find contacts who received a bot message X+ days ago but haven't replied since
    cur.execute("""
        SELECT DISTINCT cm.contact_id
        FROM contact_messages cm
        WHERE cm.contact_id IN (
            SELECT cc.contact_id FROM contact_cache cc WHERE cc.location_id = %s
        )
        AND cm.message_type = 'assistant'
        AND cm.created_at < NOW() - make_interval(days => %s)
        AND cm.contact_id NOT IN (
            SELECT cm2.contact_id FROM contact_messages cm2
            WHERE cm2.message_type = 'lead'
              AND cm2.created_at > cm.created_at
        )
        AND cm.contact_id NOT IN (
            SELECT wr.contact_id FROM workflow_runs wr
            WHERE wr.workflow_id = %s AND wr.status IN ('running', 'completed')
              AND wr.started_at > NOW() - INTERVAL '7 days'
        )
        LIMIT 50
    """, (location_id, days, wf_id))

    contacts = cur.fetchall()
    count = 0
    for row in contacts:
        run_id = _create_workflow_run(cur, conn, wf_id, row["contact_id"],
                                       {"no_response_days": days})
        if run_id:
            _enqueue_run(run_id)
            count += 1
    return count


def _process_lead_age_trigger(cur, conn, wf_id, location_id, config, now):
    """Fire for contacts whose import date is X+ days ago."""
    days = config.get("days_since_import", 60)
    try:
        days = int(days)
    except (ValueError, TypeError):
        days = 60

    # Find contacts from contact_cache that were created X+ days ago
    # and haven't already been triggered by this workflow recently
    cur.execute("""
        SELECT cc.contact_id
        FROM contact_cache cc
        WHERE cc.location_id = %s
          AND cc.cached_at < NOW() - make_interval(days => %s)
          AND cc.contact_id NOT IN (
              SELECT wr.contact_id FROM workflow_runs wr
              WHERE wr.workflow_id = %s
                AND wr.started_at > NOW() - INTERVAL '30 days'
          )
        LIMIT 50
    """, (location_id, days, wf_id))

    contacts = cur.fetchall()
    count = 0
    for row in contacts:
        run_id = _create_workflow_run(cur, conn, wf_id, row["contact_id"],
                                       {"lead_age_days": days})
        if run_id:
            _enqueue_run(run_id)
            count += 1
    return count


def _process_birthday_trigger(cur, conn, wf_id, location_id, config, now):
    """Fire for contacts whose birthday is within X days from now."""
    days_before = config.get("days_before", 7)
    try:
        days_before = int(days_before)
    except (ValueError, TypeError):
        days_before = 7

    target_date = now + timedelta(days=days_before)
    target_month = target_date.month
    target_day = target_date.day

    # Find contacts with matching birthday month/day from contact_cache
    cur.execute("""
        SELECT cc.contact_id, cc.data
        FROM contact_cache cc
        WHERE cc.location_id = %s
          AND cc.data IS NOT NULL
          AND cc.contact_id NOT IN (
              SELECT wr.contact_id FROM workflow_runs wr
              WHERE wr.workflow_id = %s
                AND wr.started_at > NOW() - INTERVAL '350 days'
          )
    """, (location_id, wf_id))

    contacts = cur.fetchall()
    count = 0
    for row in contacts:
        data = row.get("data") or {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                continue

        dob = data.get("dateOfBirth") or data.get("date_of_birth") or ""
        if not dob:
            continue

        try:
            if isinstance(dob, str):
                # Try common formats
                for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%dT%H:%M:%S"):
                    try:
                        dob_dt = datetime.strptime(dob.split("T")[0] if "T" in dob else dob, fmt)
                        break
                    except ValueError:
                        continue
                else:
                    continue
            else:
                continue

            if dob_dt.month == target_month and dob_dt.day == target_day:
                run_id = _create_workflow_run(cur, conn, wf_id, row["contact_id"],
                                               {"birthday_days_before": days_before})
                if run_id:
                    _enqueue_run(run_id)
                    count += 1
        except Exception:
            continue

    return count


def _create_workflow_run(cur, conn, wf_id, contact_id, extra_context=None):
    """Create a workflow run, checking for duplicates. Returns run_id or None."""
    try:
        # Check for duplicate running workflows for same contact
        cur.execute("""
            SELECT id FROM workflow_runs
            WHERE workflow_id = %s AND contact_id = %s AND status = 'running'
            LIMIT 1
        """, (wf_id, contact_id))
        if cur.fetchone():
            return None

        # Find first step
        cur.execute("""
            SELECT ws.id FROM workflow_steps ws
            WHERE ws.workflow_id = %s
              AND ws.id NOT IN (
                  SELECT to_step_id FROM workflow_connections WHERE workflow_id = %s
              )
            ORDER BY ws.position_y ASC, ws.position_x ASC
            LIMIT 1
        """, (wf_id, wf_id))
        first_step = cur.fetchone()
        if not first_step:
            return None

        context = {"event_data": extra_context or {}, "loop_counters": {}}
        cur.execute("""
            INSERT INTO workflow_runs (workflow_id, contact_id, status, current_step_id, context)
            VALUES (%s, %s, 'running', %s, %s)
            RETURNING id
        """, (wf_id, contact_id, first_step["id"], json.dumps(context)))
        row = cur.fetchone()
        run_id = row["id"]

        # Increment stats
        cur.execute("""
            UPDATE workflows
            SET stats = jsonb_set(COALESCE(stats, '{}'), '{runs}',
                to_jsonb(COALESCE((stats->>'runs')::int, 0) + 1)),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (wf_id,))
        conn.commit()
        return run_id
    except Exception as e:
        logger.error(f"Failed to create workflow run: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def _cron_matches_now(cron_expr, tz_name, utc_now):
    """Check if a cron expression matches the current time in the given timezone."""
    try:
        import pytz
        tz = pytz.timezone(tz_name)
    except Exception:
        from datetime import timezone as _tz
        tz = _tz.utc

    try:
        local_now = utc_now.astimezone(tz)
    except Exception:
        local_now = utc_now

    parts = cron_expr.split()
    if len(parts) != 5:
        return False

    minute, hour, dom, month, dow = parts

    def _match(field, current):
        if field == "*":
            return True
        # Handle */N step values
        if field.startswith("*/"):
            try:
                step = int(field[2:])
                return current % step == 0
            except ValueError:
                return False
        # Handle comma-separated values
        if "," in field:
            return str(current) in field.split(",")
        # Handle ranges (e.g. 1-5)
        if "-" in field:
            try:
                low, high = field.split("-")
                return int(low) <= current <= int(high)
            except ValueError:
                return False
        try:
            return int(field) == current
        except ValueError:
            return False

    return (
        _match(minute, local_now.minute)
        and _match(hour, local_now.hour)
        and _match(dom, local_now.day)
        and _match(month, local_now.month)
        and _match(dow, local_now.weekday())  # 0=Monday in Python
    )


def _get_contacts_with_tags(location_id, tag_filter):
    """Get contact IDs from cache that have ALL specified tags."""
    conn2 = get_db_connection()
    if not conn2:
        return []
    try:
        cur2 = conn2.cursor()
        cur2.execute("""
            SELECT contact_id, data FROM contact_cache
            WHERE location_id = %s AND data IS NOT NULL
        """, (location_id,))
        results = []
        for row in cur2.fetchall():
            data = row.get("data") or {}
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    continue
            tags = [t.lower() for t in (data.get("tags") or [])]
            if all(t.lower() in tags for t in tag_filter):
                results.append(row["contact_id"])
        return results[:100]  # Cap at 100 per batch
    except Exception as e:
        logger.error(f"Failed to get contacts with tags: {e}")
        return []
    finally:
        return_db_connection(conn2)


# ═══════════════════════════════════════════════════════════════════════════════
# ═══ HELPERS ══════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _ghl_headers(token):
    """Build GHL API headers with authorization."""
    return {
        "Authorization": f"Bearer {token}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
    }


# Contact data cache (per-execution, not persisted)
_contact_cache = {}


def _fetch_contact(location_id, contact_id, subscriber=None):
    """
    Fetch contact data from the appropriate CRM API. Cached within the
    current process to avoid redundant API calls during a single run execution.

    CRM-aware: uses GHL API for GHL subscribers, CRM adapter for others.
    Returns a GHL-compatible contact dict for backward compatibility with
    merge fields (firstName, lastName, phone, email, etc.).
    """
    cache_key = f"{location_id}:{contact_id}"
    if cache_key in _contact_cache:
        return _contact_cache[cache_key]

    crm_type, sub = _get_subscriber_crm_type(location_id, subscriber)

    if _is_ghl(crm_type):
        # GHL path — unchanged
        token = get_valid_token(location_id)
        if not token:
            logger.warning(f"No GHL token to fetch contact {contact_id}")
            return None

        try:
            resp = requests.get(
                f"{GHL_BASE_URL}/contacts/{contact_id}",
                headers=_ghl_headers(token),
                timeout=10,
            )
            resp.raise_for_status()
            contact = resp.json().get("contact", {})
            _contact_cache[cache_key] = contact
            return contact
        except Exception as e:
            logger.warning(f"Failed to fetch contact {contact_id}: {e}")
            return None
    else:
        # Non-GHL CRM — use adapter
        try:
            from crm_adapters.factory import get_adapter_for_subscriber
            adapter = get_adapter_for_subscriber(sub)
            contact = adapter.get_contact(contact_id)
            if contact:
                # Normalize to GHL-compatible field names for merge field compat
                normalized = _normalize_contact_fields(contact, crm_type)
                _contact_cache[cache_key] = normalized
                return normalized
        except Exception as e:
            logger.warning(f"Failed to fetch {crm_type} contact {contact_id}: {e}")
        return None


def _normalize_contact_fields(contact, crm_type):
    """
    Normalize CRM adapter contact dict to GHL-compatible field names.
    Ensures merge fields like {{firstName}}, {{phone}} work regardless of CRM.
    """
    if not contact:
        return contact
    return {
        "id": contact.get("id", ""),
        "firstName": contact.get("firstName", contact.get("firstname", "")),
        "lastName": contact.get("lastName", contact.get("lastname", "")),
        "email": contact.get("email", ""),
        "phone": contact.get("phone", ""),
        "company": contact.get("company", contact.get("companyName", "")),
        "companyName": contact.get("company", contact.get("companyName", "")),
        "address1": contact.get("address", contact.get("address1", "")),
        "city": contact.get("city", ""),
        "state": contact.get("state", ""),
        "postalCode": contact.get("zip", contact.get("postalCode", "")),
        "tags": contact.get("tags", []),
        # Preserve any extra fields
        **{k: v for k, v in contact.items() if k not in (
            "id", "firstName", "lastName", "email", "phone", "company",
            "companyName", "address", "address1", "city", "state", "zip",
            "postalCode", "tags", "firstname", "lastname",
        )},
    }


def _interpolate_merge_fields(template, contact):
    """
    Replace {{fieldName}} merge fields with contact data.

    Supported fields: firstName, lastName, phone, email, city, state, companyName
    """
    if not template or not contact:
        return template

    field_map = {
        "firstName": contact.get("firstName") or contact.get("first_name", ""),
        "lastName": contact.get("lastName") or contact.get("last_name", ""),
        "phone": contact.get("phone", ""),
        "email": contact.get("email", ""),
        "city": contact.get("city", ""),
        "state": contact.get("state", ""),
        "companyName": contact.get("companyName") or contact.get("company_name", ""),
    }

    result = template
    for key, val in field_map.items():
        result = result.replace("{{" + key + "}}", str(val) if val else "")

    # Clean up any remaining unresolved merge fields
    result = re.sub(r"\{\{[a-zA-Z_]+\}\}", "", result)
    return result.strip()


def _get_cached_intelligence(contact_id):
    """Fetch cached AI intelligence for a contact from the DB."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT analysis FROM contact_intelligence
            WHERE contact_id = %s
            LIMIT 1
        """, (contact_id,))
        row = cur.fetchone()
        if row and row.get("analysis"):
            analysis = row["analysis"]
            if isinstance(analysis, str):
                analysis = json.loads(analysis)
            return analysis
        return None
    except Exception as e:
        logger.debug(f"Failed to fetch intelligence for {contact_id}: {e}")
        return None
    finally:
        return_db_connection(conn)


def _get_closest_state_number(subscriber, dest_phone):
    """
    Find the subscriber's phone number whose area code is in the same state
    as the destination phone number.
    """
    try:
        from voice.predictive_engine import area_code_to_state
    except ImportError:
        logger.debug("predictive_engine not available for closest_state routing")
        return None

    dest_state = area_code_to_state(dest_phone)
    if not dest_state:
        return None

    voice_config = subscriber.get("voice_config") or {}
    if isinstance(voice_config, str):
        voice_config = json.loads(voice_config)

    numbers = voice_config.get("phone_numbers", [])
    # Also check primary number
    primary = subscriber.get("twilio_phone_number")
    if primary:
        numbers = [primary] + [n for n in numbers if n != primary]

    for number in numbers:
        num_state = area_code_to_state(number)
        if num_state and num_state == dest_state:
            return number

    return None


def _get_rotated_number(subscriber):
    """
    Select the next number in rotation from the subscriber's phone numbers.
    Uses the number_health module's selection if available, otherwise simple rotation.
    """
    try:
        from number_health import select_outbound_number
        voice_config = subscriber.get("voice_config") or {}
        if isinstance(voice_config, str):
            voice_config = json.loads(voice_config)
        location_id = subscriber.get("location_id")
        if location_id:
            return select_outbound_number(location_id, voice_config)
    except Exception as e:
        logger.debug(f"Number rotation fallback: {e}")

    return None
