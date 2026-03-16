# blueprints/workflows.py — Workflow Builder CRUD + AI Builder
#
# Routes:
#   GET    /api/workflows                                — List workflows for location
#   POST   /api/workflows                                — Create new workflow
#   GET    /api/workflows/<id>                            — Get workflow + steps + connections
#   PUT    /api/workflows/<id>                            — Update workflow metadata
#   DELETE /api/workflows/<id>                            — Delete workflow (CASCADE)
#   POST   /api/workflows/<id>/steps                     — Add step to workflow
#   PUT    /api/workflows/<id>/steps/<step_id>            — Update step
#   DELETE /api/workflows/<id>/steps/<step_id>            — Delete step + connections
#   POST   /api/workflows/<id>/connections                — Add connection
#   DELETE /api/workflows/<id>/connections/<conn_id>      — Remove connection
#   POST   /api/workflows/<id>/activate                   — Set status=active
#   POST   /api/workflows/<id>/pause                      — Set status=paused
#   POST   /api/workflows/<id>/duplicate                  — Clone workflow + steps + connections
#   GET    /api/workflows/<id>/runs                       — List recent runs (last 50)
#   PUT    /api/workflows/<id>/save-full                  — Bulk save canvas (steps + connections)
#   POST   /api/workflows/build-with-ai                   — AI workflow builder via xAI Grok
#   POST   /api/workflows/<id>/test                       — Test-run workflow on a contact
#   GET    /api/workflows/custom-actions                   — List custom actions
#   POST   /api/workflows/custom-actions                   — Create custom action
#   DELETE /api/workflows/custom-actions/<action_id>       — Delete custom action

import json
import logging
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user

from db import get_db_connection, return_db_connection
from extensions import get_client, ensure_redis, q_production

logger = logging.getLogger(__name__)

workflows_bp = Blueprint('workflows', __name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _verify_workflow_owner(workflow_id, location_id):
    """Return the workflow row if it belongs to location_id, else None."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM workflows WHERE id = %s AND location_id = %s",
            (workflow_id, location_id),
        )
        return cur.fetchone()
    finally:
        return_db_connection(conn)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _gen_id():
    return str(uuid.uuid4())


# ── List workflows ───────────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows", methods=["GET"])
@login_required
def list_workflows():
    location_id = current_user.location_id
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, description, status, trigger_type, trigger_config,
                   created_at, updated_at, stats
            FROM workflows
            WHERE location_id = %s
            ORDER BY updated_at DESC
        """, (location_id,))
        rows = cur.fetchall()
        return jsonify({"workflows": [dict(r) for r in rows]})
    finally:
        return_db_connection(conn)


# ── Create workflow ──────────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows", methods=["POST"])
@login_required
def create_workflow():
    location_id = current_user.location_id
    data = request.get_json(silent=True) or {}
    name = data.get("name", "Untitled Workflow")
    trigger_type = data.get("trigger_type", "manual")
    trigger_config = data.get("trigger_config", {})
    description = data.get("description", "")

    wf_id = _gen_id()
    now = _now()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO workflows (id, location_id, name, description, status,
                                   trigger_type, trigger_config, created_at, updated_at, created_by)
            VALUES (%s, %s, %s, %s, 'draft', %s, %s, %s, %s, %s)
            RETURNING *
        """, (wf_id, location_id, name, description, trigger_type,
              json.dumps(trigger_config), now, now, current_user.email))
        row = cur.fetchone()
        conn.commit()
        return jsonify({"workflow": dict(row)}), 201
    except Exception as e:
        conn.rollback()
        logger.error(f"Create workflow error: {e}")
        return jsonify({"error": "Failed to create workflow"}), 500
    finally:
        return_db_connection(conn)


# ── Get workflow detail ──────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>", methods=["GET"])
@login_required
def get_workflow(workflow_id):
    location_id = current_user.location_id
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM workflows WHERE id = %s AND location_id = %s",
            (workflow_id, location_id),
        )
        wf = cur.fetchone()
        if not wf:
            return jsonify({"error": "Workflow not found"}), 404

        cur.execute(
            "SELECT * FROM workflow_steps WHERE workflow_id = %s ORDER BY created_at",
            (workflow_id,),
        )
        steps = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT * FROM workflow_connections WHERE workflow_id = %s ORDER BY sort_order",
            (workflow_id,),
        )
        connections = [dict(r) for r in cur.fetchall()]

        return jsonify({
            "workflow": dict(wf),
            "steps": steps,
            "connections": connections,
        })
    finally:
        return_db_connection(conn)


# ── Update workflow metadata ─────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>", methods=["PUT"])
@login_required
def update_workflow(workflow_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    data = request.get_json(silent=True) or {}
    allowed = {"name", "description", "trigger_type", "trigger_config"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return jsonify({"error": "No valid fields to update"}), 400

    if "trigger_config" in fields:
        fields["trigger_config"] = json.dumps(fields["trigger_config"])

    set_parts = [f"{k} = %s" for k in fields]
    set_parts.append("updated_at = %s")
    values = list(fields.values()) + [_now(), workflow_id, location_id]

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE workflows SET {', '.join(set_parts)} "
            f"WHERE id = %s AND location_id = %s RETURNING *",
            values,
        )
        row = cur.fetchone()
        conn.commit()
        return jsonify({"workflow": dict(row)})
    except Exception as e:
        conn.rollback()
        logger.error(f"Update workflow error: {e}")
        return jsonify({"error": "Failed to update workflow"}), 500
    finally:
        return_db_connection(conn)


# ── Delete workflow ──────────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>", methods=["DELETE"])
@login_required
def delete_workflow(workflow_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM workflows WHERE id = %s AND location_id = %s",
            (workflow_id, location_id),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        logger.error(f"Delete workflow error: {e}")
        return jsonify({"error": "Failed to delete workflow"}), 500
    finally:
        return_db_connection(conn)


# ── Add step ─────────────────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>/steps", methods=["POST"])
@login_required
def add_step(workflow_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    data = request.get_json(silent=True) or {}
    step_type = data.get("step_type", "action")
    step_subtype = data.get("step_subtype", "send_sms")
    config = data.get("config", {})
    position_x = data.get("position_x", 0)
    position_y = data.get("position_y", 0)
    step_id = _gen_id()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO workflow_steps (id, workflow_id, step_type, step_subtype,
                                        config, position_x, position_y)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (step_id, workflow_id, step_type, step_subtype,
              json.dumps(config), position_x, position_y))
        row = cur.fetchone()

        cur.execute(
            "UPDATE workflows SET updated_at = %s WHERE id = %s",
            (_now(), workflow_id),
        )
        conn.commit()
        return jsonify({"step": dict(row)}), 201
    except Exception as e:
        conn.rollback()
        logger.error(f"Add step error: {e}")
        return jsonify({"error": "Failed to add step"}), 500
    finally:
        return_db_connection(conn)


# ── Update step ──────────────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>/steps/<step_id>", methods=["PUT"])
@login_required
def update_step(workflow_id, step_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    data = request.get_json(silent=True) or {}
    allowed = {"config", "position_x", "position_y"}
    fields = {k: v for k, v in data.items() if k in allowed}
    if not fields:
        return jsonify({"error": "No valid fields to update"}), 400

    if "config" in fields:
        fields["config"] = json.dumps(fields["config"])

    set_parts = [f"{k} = %s" for k in fields]
    values = list(fields.values()) + [step_id, workflow_id]

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE workflow_steps SET {', '.join(set_parts)} "
            f"WHERE id = %s AND workflow_id = %s RETURNING *",
            values,
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Step not found"}), 404

        cur.execute(
            "UPDATE workflows SET updated_at = %s WHERE id = %s",
            (_now(), workflow_id),
        )
        conn.commit()
        return jsonify({"step": dict(row)})
    except Exception as e:
        conn.rollback()
        logger.error(f"Update step error: {e}")
        return jsonify({"error": "Failed to update step"}), 500
    finally:
        return_db_connection(conn)


# ── Delete step ──────────────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>/steps/<step_id>", methods=["DELETE"])
@login_required
def delete_step(workflow_id, step_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM workflow_connections WHERE workflow_id = %s AND (from_step_id = %s OR to_step_id = %s)",
            (workflow_id, step_id, step_id),
        )
        cur.execute(
            "DELETE FROM workflow_steps WHERE id = %s AND workflow_id = %s",
            (step_id, workflow_id),
        )
        cur.execute(
            "UPDATE workflows SET updated_at = %s WHERE id = %s",
            (_now(), workflow_id),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        logger.error(f"Delete step error: {e}")
        return jsonify({"error": "Failed to delete step"}), 500
    finally:
        return_db_connection(conn)


# ── Add connection ───────────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>/connections", methods=["POST"])
@login_required
def add_connection(workflow_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    data = request.get_json(silent=True) or {}
    from_step_id = data.get("from_step_id")
    to_step_id = data.get("to_step_id")
    if not from_step_id or not to_step_id:
        return jsonify({"error": "from_step_id and to_step_id required"}), 400

    branch_key = data.get("branch_key", "default")
    conn_id = _gen_id()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO workflow_connections (id, workflow_id, from_step_id, to_step_id, branch_key)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
        """, (conn_id, workflow_id, from_step_id, to_step_id, branch_key))
        row = cur.fetchone()

        cur.execute(
            "UPDATE workflows SET updated_at = %s WHERE id = %s",
            (_now(), workflow_id),
        )
        conn.commit()
        return jsonify({"connection": dict(row)}), 201
    except Exception as e:
        conn.rollback()
        logger.error(f"Add connection error: {e}")
        return jsonify({"error": "Failed to add connection"}), 500
    finally:
        return_db_connection(conn)


# ── Delete connection ────────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>/connections/<conn_id>", methods=["DELETE"])
@login_required
def delete_connection(workflow_id, conn_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM workflow_connections WHERE id = %s AND workflow_id = %s",
            (conn_id, workflow_id),
        )
        cur.execute(
            "UPDATE workflows SET updated_at = %s WHERE id = %s",
            (_now(), workflow_id),
        )
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        logger.error(f"Delete connection error: {e}")
        return jsonify({"error": "Failed to delete connection"}), 500
    finally:
        return_db_connection(conn)


# ── Activate workflow ────────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>/activate", methods=["POST"])
@login_required
def activate_workflow(workflow_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE workflows SET status = 'active', updated_at = %s "
            "WHERE id = %s AND location_id = %s RETURNING *",
            (_now(), workflow_id, location_id),
        )
        row = cur.fetchone()
        conn.commit()
        return jsonify({"workflow": dict(row)})
    except Exception as e:
        conn.rollback()
        logger.error(f"Activate workflow error: {e}")
        return jsonify({"error": "Failed to activate workflow"}), 500
    finally:
        return_db_connection(conn)


# ── Pause workflow ───────────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>/pause", methods=["POST"])
@login_required
def pause_workflow(workflow_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE workflows SET status = 'paused', updated_at = %s "
            "WHERE id = %s AND location_id = %s RETURNING *",
            (_now(), workflow_id, location_id),
        )
        row = cur.fetchone()
        conn.commit()
        return jsonify({"workflow": dict(row)})
    except Exception as e:
        conn.rollback()
        logger.error(f"Pause workflow error: {e}")
        return jsonify({"error": "Failed to pause workflow"}), 500
    finally:
        return_db_connection(conn)


# ── Duplicate workflow ───────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>/duplicate", methods=["POST"])
@login_required
def duplicate_workflow(workflow_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()

        new_wf_id = _gen_id()
        now = _now()
        cur.execute("""
            INSERT INTO workflows (id, location_id, name, description, status,
                                   trigger_type, trigger_config, created_at, updated_at, created_by)
            VALUES (%s, %s, %s, %s, 'draft', %s, %s, %s, %s, %s)
            RETURNING *
        """, (new_wf_id, location_id, wf["name"] + " (Copy)", wf["description"],
              wf["trigger_type"], json.dumps(wf["trigger_config"]) if isinstance(wf["trigger_config"], dict) else wf["trigger_config"],
              now, now, current_user.email))
        new_wf = cur.fetchone()

        cur.execute(
            "SELECT * FROM workflow_steps WHERE workflow_id = %s",
            (workflow_id,),
        )
        old_steps = cur.fetchall()

        step_id_map = {}
        for step in old_steps:
            new_step_id = _gen_id()
            step_id_map[step["id"]] = new_step_id
            cur.execute("""
                INSERT INTO workflow_steps (id, workflow_id, step_type, step_subtype,
                                            config, position_x, position_y)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (new_step_id, new_wf_id, step["step_type"], step["step_subtype"],
                  json.dumps(step["config"]) if isinstance(step["config"], dict) else step["config"],
                  step["position_x"], step["position_y"]))

        cur.execute(
            "SELECT * FROM workflow_connections WHERE workflow_id = %s",
            (workflow_id,),
        )
        old_conns = cur.fetchall()

        for c in old_conns:
            new_from = step_id_map.get(c["from_step_id"])
            new_to = step_id_map.get(c["to_step_id"])
            if new_from and new_to:
                cur.execute("""
                    INSERT INTO workflow_connections (id, workflow_id, from_step_id, to_step_id,
                                                      branch_key, sort_order)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (_gen_id(), new_wf_id, new_from, new_to,
                      c["branch_key"], c["sort_order"]))

        conn.commit()
        return jsonify({"workflow": dict(new_wf), "id": new_wf_id}), 201
    except Exception as e:
        conn.rollback()
        logger.error(f"Duplicate workflow error: {e}")
        return jsonify({"error": "Failed to duplicate workflow"}), 500
    finally:
        return_db_connection(conn)


# ── List runs ────────────────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>/runs", methods=["GET"])
@login_required
def list_runs(workflow_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, workflow_id, contact_id, status, current_step_id,
                   started_at, completed_at, error
            FROM workflow_runs
            WHERE workflow_id = %s
            ORDER BY started_at DESC
            LIMIT 50
        """, (workflow_id,))
        rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"runs": rows})
    finally:
        return_db_connection(conn)


# ── Bulk save (canvas save) ──────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>/save-full", methods=["PUT"])
@login_required
def save_full(workflow_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    data = request.get_json(silent=True) or {}
    now = _now()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()

        meta_fields = {}
        for key in ("name", "description", "trigger_type", "trigger_config"):
            if key in data:
                meta_fields[key] = data[key]
        if "trigger_config" in meta_fields:
            meta_fields["trigger_config"] = json.dumps(meta_fields["trigger_config"])

        if meta_fields:
            set_parts = [f"{k} = %s" for k in meta_fields]
            set_parts.append("updated_at = %s")
            vals = list(meta_fields.values()) + [now, workflow_id, location_id]
            cur.execute(
                f"UPDATE workflows SET {', '.join(set_parts)} "
                f"WHERE id = %s AND location_id = %s",
                vals,
            )
        else:
            cur.execute(
                "UPDATE workflows SET updated_at = %s WHERE id = %s AND location_id = %s",
                (now, workflow_id, location_id),
            )

        cur.execute("DELETE FROM workflow_connections WHERE workflow_id = %s", (workflow_id,))
        cur.execute("DELETE FROM workflow_steps WHERE workflow_id = %s", (workflow_id,))

        steps = data.get("steps", [])
        step_id_map = {}
        for s in steps:
            new_id = _gen_id()
            temp_id = s.get("id") or s.get("temp_id") or new_id
            step_id_map[temp_id] = new_id
            cur.execute("""
                INSERT INTO workflow_steps (id, workflow_id, step_type, step_subtype,
                                            config, position_x, position_y)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (new_id, workflow_id,
                  s.get("step_type", "action"), s.get("step_subtype", "send_sms"),
                  json.dumps(s.get("config", {})),
                  s.get("position_x", 0), s.get("position_y", 0)))

        connections = data.get("connections", [])
        for c in connections:
            from_id = step_id_map.get(c.get("from_step_id"), c.get("from_step_id"))
            to_id = step_id_map.get(c.get("to_step_id"), c.get("to_step_id"))
            cur.execute("""
                INSERT INTO workflow_connections (id, workflow_id, from_step_id, to_step_id,
                                                  branch_key)
                VALUES (%s, %s, %s, %s, %s)
            """, (_gen_id(), workflow_id, from_id, to_id,
                  c.get("branch_key", "default")))

        conn.commit()

        cur.execute(
            "SELECT * FROM workflow_steps WHERE workflow_id = %s ORDER BY created_at",
            (workflow_id,),
        )
        saved_steps = [dict(r) for r in cur.fetchall()]

        cur.execute(
            "SELECT * FROM workflow_connections WHERE workflow_id = %s",
            (workflow_id,),
        )
        saved_conns = [dict(r) for r in cur.fetchall()]

        return jsonify({
            "ok": True,
            "step_id_map": step_id_map,
            "steps": saved_steps,
            "connections": saved_conns,
        })
    except Exception as e:
        conn.rollback()
        logger.error(f"Save-full error: {e}")
        return jsonify({"error": "Failed to save workflow"}), 500
    finally:
        return_db_connection(conn)


# ── AI workflow builder ──────────────────────────────────────────────────────

_AI_BUILDER_SYSTEM_PROMPT = """You are an automation workflow builder for an insurance CRM platform.
Given a natural-language description, generate a structured workflow JSON.

Available trigger types:
contact_created, sms_received, inbound_call, missed_call, voicemail_received,
tag_added, tag_removed, stage_changed, appointment_booked, appointment_noshow,
contact_dnd, no_response, lead_age, birthday_approaching, field_updated, manual, scheduled

Available action step_subtypes:
send_sms, ai_call, add_tag, remove_tag, assign_agent, wait, update_field,
add_note, send_webhook, if_else, loop, goto, exit, custom

Merge fields for SMS templates:
{{firstName}}, {{lastName}}, {{phone}}, {{email}}, {{city}}, {{state}}, {{companyName}}, {{tags}}, {{source}}

SMS from_strategy options: default, closest_state, rotate

Condition operators (for if_else steps):
equals, not_equals, contains, starts_with, is_empty, is_not_empty,
greater_than, less_than, has_tag, no_tag, in_state, lead_age_days,
score_above, score_below, temperature_is, responded_within,
total_messages_sent, time_is_between

Return ONLY valid JSON (no markdown, no explanation) with this structure:
{
  "name": "Workflow Name",
  "description": "Brief description",
  "trigger_type": "one_of_the_triggers",
  "trigger_config": {},
  "steps": [
    {
      "temp_id": "step_1",
      "step_type": "action|condition|control",
      "step_subtype": "one_of_the_subtypes",
      "config": {},
      "position_x": 0,
      "position_y": 0
    }
  ],
  "connections": [
    {
      "from_temp_id": "step_1",
      "to_temp_id": "step_2",
      "branch_key": "default"
    }
  ]
}

For if_else steps, use branch_key "true" and "false" on outgoing connections.
For wait steps, config should include {"duration": N, "unit": "minutes|hours|days"}.
For send_sms, config should include {"message": "...", "from_strategy": "default"}.
Arrange steps in a top-down layout with ~150px vertical spacing."""


@workflows_bp.route("/api/workflows/build-with-ai", methods=["POST"])
@login_required
def build_with_ai():
    data = request.get_json(silent=True) or {}
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    xai = get_client()
    if not xai:
        return jsonify({"error": "AI service unavailable"}), 503

    try:
        response = xai.chat.completions.create(
            model="grok-3-mini-fast",
            messages=[
                {"role": "system", "content": _AI_BUILDER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        raw = response.choices[0].message.content.strip()

        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()

        workflow_data = json.loads(raw)
        return jsonify({"workflow": workflow_data})
    except json.JSONDecodeError:
        logger.error(f"AI builder returned invalid JSON: {raw[:500]}")
        return jsonify({"error": "AI returned invalid workflow structure"}), 422
    except Exception as e:
        logger.error(f"AI builder error: {e}")
        return jsonify({"error": "AI workflow generation failed"}), 500


# ── Test-run workflow ────────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/<workflow_id>/test", methods=["POST"])
@login_required
def test_workflow(workflow_id):
    location_id = current_user.location_id
    wf = _verify_workflow_owner(workflow_id, location_id)
    if not wf:
        return jsonify({"error": "Workflow not found"}), 404

    data = request.get_json(silent=True) or {}
    contact_id = data.get("contact_id")
    if not contact_id:
        return jsonify({"error": "contact_id is required"}), 400

    run_id = _gen_id()
    now = _now()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO workflow_runs (id, workflow_id, contact_id, status, started_at, context)
            VALUES (%s, %s, %s, 'running', %s, %s)
            RETURNING *
        """, (run_id, workflow_id, contact_id, now,
              json.dumps({"test": True, "triggered_by": current_user.email})))
        run = cur.fetchone()
        conn.commit()

        if ensure_redis() and q_production:
            try:
                q_production.enqueue(
                    "tasks.execute_workflow_run",
                    run_id,
                    job_timeout=120,
                    job_id=f"wf-test-{run_id[:8]}",
                )
            except Exception as e:
                logger.warning(f"Failed to enqueue workflow test run: {e}")

        return jsonify({"run": dict(run)}), 201
    except Exception as e:
        conn.rollback()
        logger.error(f"Test workflow error: {e}")
        return jsonify({"error": "Failed to start test run"}), 500
    finally:
        return_db_connection(conn)


# ── Custom actions CRUD ──────────────────────────────────────────────────────

@workflows_bp.route("/api/workflows/custom-actions", methods=["GET"])
@login_required
def list_custom_actions():
    location_id = current_user.location_id
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM workflow_custom_actions WHERE location_id = %s ORDER BY created_at DESC",
            (location_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"custom_actions": rows})
    finally:
        return_db_connection(conn)


@workflows_bp.route("/api/workflows/custom-actions", methods=["POST"])
@login_required
def create_custom_action():
    location_id = current_user.location_id
    data = request.get_json(silent=True) or {}

    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    action_id = _gen_id()
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO workflow_custom_actions (id, location_id, name, description,
                                                 icon, color, config_template)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
        """, (action_id, location_id, name,
              data.get("description", ""),
              data.get("icon", "fa-solid fa-puzzle-piece"),
              data.get("color", "#00b4ff"),
              json.dumps(data.get("config_template", {}))))
        row = cur.fetchone()
        conn.commit()
        return jsonify({"custom_action": dict(row)}), 201
    except Exception as e:
        conn.rollback()
        logger.error(f"Create custom action error: {e}")
        return jsonify({"error": "Failed to create custom action"}), 500
    finally:
        return_db_connection(conn)


@workflows_bp.route("/api/workflows/custom-actions/<action_id>", methods=["DELETE"])
@login_required
def delete_custom_action(action_id):
    location_id = current_user.location_id
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM workflow_custom_actions WHERE id = %s AND location_id = %s",
            (action_id, location_id),
        )
        if cur.rowcount == 0:
            return jsonify({"error": "Custom action not found"}), 404
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        logger.error(f"Delete custom action error: {e}")
        return jsonify({"error": "Failed to delete custom action"}), 500
    finally:
        return_db_connection(conn)
