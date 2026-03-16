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

# Trigger type → event_type mapping
TRIGGER_EVENT_MAP = {
    "contact_created": "ContactCreate",
    "sms_received": "InboundMessage",
    "inbound_call": "InboundCall",
    "missed_call": "MissedCall",
    "tag_added": "TagAdded",
    "tag_removed": "TagRemoved",
}


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
            context = {"event_data": event_data or {}, "loop_counters": {}}
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

    handler = STEP_HANDLERS.get(subtype)
    if not handler:
        logger.warning(f"Unknown step subtype: {subtype}")
        return {"status": "completed", "note": f"Unknown subtype {subtype}, skipped"}

    return handler(cur, conn, run_id, step, config, location_id, contact_id, context)


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
        return {"status": "error", "error": "Contact not found in GHL", "continue": True}

    message = _interpolate_merge_fields(message_template, contact)
    phone = contact.get("phone")
    if not phone:
        return {"status": "error", "error": "Contact has no phone number", "continue": True}

    # Determine send method based on subscriber config
    subscriber = get_subscriber_info_hybrid(location_id)
    if not subscriber:
        return {"status": "error", "error": "Subscriber not found", "continue": True}

    sms_send_via = subscriber.get("sms_send_via", "ghl")
    from_strategy = config.get("from_strategy", "default")

    if sms_send_via and sms_send_via.startswith("+"):
        # Send via Twilio
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
        from voice.call_state import active_calls
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

        domain = subscriber.get("your_domain") or "https://app.insurancegrokbot.com"
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
    """Add a tag to the GHL contact."""
    tag = config.get("tag", "").strip()
    if not tag:
        return {"status": "error", "error": "No tag specified", "continue": True}

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
    """Remove a tag from the GHL contact."""
    tag = config.get("tag", "").strip()
    if not tag:
        return {"status": "error", "error": "No tag specified", "continue": True}

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
    """Evaluate a condition and choose the 'true' or 'false' branch."""
    field = config.get("field", "")
    operator = config.get("operator", "equals")
    value = config.get("value", "")

    result = _evaluate_condition(field, operator, value, location_id, contact_id, context)
    branch = "true" if result else "false"

    logger.debug(f"If/else: field={field} op={operator} val={value} -> {branch}")
    return {"status": "completed", "branch_key": branch, "condition_result": result}


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
    """Update a field on the GHL contact."""
    field_key = config.get("field_key", "").strip()
    field_value = config.get("field_value", "")

    if not field_key:
        return {"status": "error", "error": "No field_key specified", "continue": True}

    # Interpolate merge fields in the value
    contact = _fetch_contact(location_id, contact_id)
    if contact:
        field_value = _interpolate_merge_fields(str(field_value), contact)

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
    """Add a note to the GHL contact."""
    body = config.get("body", "").strip()
    if not body:
        return {"status": "error", "error": "No note body specified", "continue": True}

    contact = _fetch_contact(location_id, contact_id)
    if contact:
        body = _interpolate_merge_fields(body, contact)

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
    """Custom/extensible action — log and mark as executed."""
    action_name = config.get("action_name", "custom")
    logger.info(f"Custom action '{action_name}' executed for run {run_id}, step {step['id']}")
    return {"status": "completed", "action": action_name, "note": "Custom action executed"}


def _handle_assign_agent(cur, conn, run_id, step, config, location_id, contact_id, context):
    """Update the contact owner (assigned user) in GHL."""
    assigned_to = config.get("assigned_to", "").strip()
    if not assigned_to:
        return {"status": "error", "error": "No assigned_to user specified", "continue": True}

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
    """Move contact to a pipeline stage — coming soon."""
    logger.info(f"Move stage step skipped (coming soon) for run {run_id}, step {step['id']}")
    return {"status": "skipped", "note": "move_stage coming soon"}


# Step handler registry
STEP_HANDLERS = {
    "send_sms": _handle_send_sms,
    "ai_call": _handle_ai_call,
    "add_tag": _handle_add_tag,
    "remove_tag": _handle_remove_tag,
    "wait": _handle_wait,
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

    logger.warning(f"Unknown condition operator: {operator}")
    return False


def _resolve_field_value(field, operator, location_id, contact_id, context):
    """Resolve a field name to its current value from the contact or context."""
    # Intelligence-based operators resolve their own data
    if operator in ("temperature_is", "score_above", "score_below",
                    "has_tag", "no_tag", "lead_age_days",
                    "responded_within", "total_messages_sent"):
        return None  # Handled directly in _evaluate_condition

    # Check context first (for workflow-injected variables)
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


def _fetch_contact(location_id, contact_id):
    """
    Fetch contact data from GHL API. Cached within the current process
    to avoid redundant API calls during a single run execution.
    """
    cache_key = f"{location_id}:{contact_id}"
    if cache_key in _contact_cache:
        return _contact_cache[cache_key]

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
