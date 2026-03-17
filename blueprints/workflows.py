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

import requests
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user

from db import get_db_connection, return_db_connection
from extensions import get_client, ensure_redis, q_production
from ghl_api import get_valid_token

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


# ── Pre-built workflow templates ─────────────────────────────────────────────

PREBUILT_WORKFLOWS = [
    {
        "name": "Speed to Lead",
        "description": "Instantly engage new leads within seconds of import. Sends a personalized intro SMS, waits for a reply, then follows up with an AI call if no response.",
        "trigger_type": "contact_created",
        "trigger_config": {"exit_on_reply": True},
        "steps": [
            {"id": "s1", "step_type": "control", "step_subtype": "wait",
             "config": {"duration": 30, "unit": "seconds"}, "x": 400, "y": 80},
            {"id": "s2", "step_type": "action", "step_subtype": "send_sms",
             "config": {"message": "Hi {{firstName}}, this is {{operatorName}} with {{companyName}}. I saw you were looking into coverage options — I'd love to help you find the best fit. When's a good time to chat?"}, "x": 400, "y": 200},
            {"id": "s3", "step_type": "control", "step_subtype": "wait",
             "config": {"duration": 5, "unit": "minutes"}, "x": 400, "y": 320},
            {"id": "s4", "step_type": "condition", "step_subtype": "if_else",
             "config": {"conditions": [{"field": "responded", "operator": "responded_within", "value": "5"}], "logic": "and"}, "x": 400, "y": 440},
            {"id": "s5", "step_type": "action", "step_subtype": "ai_call",
             "config": {"ring_timeout": 30}, "x": 200, "y": 580},
            {"id": "s6", "step_type": "action", "step_subtype": "add_tag",
             "config": {"tag": "speed-to-lead-contacted"}, "x": 600, "y": 580},
            {"id": "s7", "step_type": "control", "step_subtype": "exit",
             "config": {}, "x": 400, "y": 720},
        ],
        "connections": [
            {"from": "s1", "to": "s2", "branch": "default"},
            {"from": "s2", "to": "s3", "branch": "default"},
            {"from": "s3", "to": "s4", "branch": "default"},
            {"from": "s4", "to": "s5", "branch": "false"},
            {"from": "s4", "to": "s6", "branch": "true"},
            {"from": "s5", "to": "s7", "branch": "default"},
            {"from": "s6", "to": "s7", "branch": "default"},
        ],
    },
    {
        "name": "Aged Lead Re-engagement",
        "description": "Automatically re-engages leads that are 30+ days old with no recent activity. Uses AI to send a warm check-in, then follows up based on temperature.",
        "trigger_type": "lead_age",
        "trigger_config": {"days_since_import": 30, "exit_on_reply": True},
        "steps": [
            {"id": "s1", "step_type": "condition", "step_subtype": "if_else",
             "config": {"conditions": [{"field": "temperature", "operator": "temperature_is", "value": "cold"}], "logic": "and"}, "x": 400, "y": 80},
            {"id": "s2", "step_type": "action", "step_subtype": "send_igb_message",
             "config": {"mode": "ai", "prompt_hint": "This is a cold lead we haven't heard from in over 30 days. Send a short, low-pressure check-in. Mention that rates have changed recently and offer an updated quote with no obligation."}, "x": 200, "y": 220},
            {"id": "s3", "step_type": "action", "step_subtype": "send_igb_message",
             "config": {"mode": "ai", "prompt_hint": "This is a warm lead we haven't spoken to recently. Re-engage naturally, reference past conversation if possible, and offer to provide an updated quote."}, "x": 600, "y": 220},
            {"id": "s4", "step_type": "action", "step_subtype": "add_tag",
             "config": {"tag": "re-engage-sent"}, "x": 400, "y": 380},
            {"id": "s5", "step_type": "control", "step_subtype": "wait",
             "config": {"duration": 2, "unit": "days"}, "x": 400, "y": 500},
            {"id": "s6", "step_type": "condition", "step_subtype": "if_else",
             "config": {"conditions": [{"field": "responded", "operator": "responded_within", "value": "2880"}], "logic": "and"}, "x": 400, "y": 620},
            {"id": "s7", "step_type": "action", "step_subtype": "add_tag",
             "config": {"tag": "re-engaged"}, "x": 600, "y": 760},
            {"id": "s8", "step_type": "action", "step_subtype": "send_igb_message",
             "config": {"mode": "ai", "prompt_hint": "This is a final follow-up for an aged lead who hasn't responded to our re-engagement. Send a friendly closing message — let them know the door is always open if they need help with coverage in the future."}, "x": 200, "y": 760},
            {"id": "s9", "step_type": "control", "step_subtype": "exit",
             "config": {}, "x": 400, "y": 900},
        ],
        "connections": [
            {"from": "s1", "to": "s2", "branch": "true"},
            {"from": "s1", "to": "s3", "branch": "false"},
            {"from": "s2", "to": "s4", "branch": "default"},
            {"from": "s3", "to": "s4", "branch": "default"},
            {"from": "s4", "to": "s5", "branch": "default"},
            {"from": "s5", "to": "s6", "branch": "default"},
            {"from": "s6", "to": "s7", "branch": "true"},
            {"from": "s6", "to": "s8", "branch": "false"},
            {"from": "s7", "to": "s9", "branch": "default"},
            {"from": "s8", "to": "s9", "branch": "default"},
        ],
    },
    {
        "name": "SMS Response Handler",
        "description": "Handles inbound SMS replies intelligently. Routes hot leads to AI call, warm leads to AI-drafted response, and manages opt-outs automatically.",
        "trigger_type": "sms_received",
        "trigger_config": {"exit_on_reply": False},
        "steps": [
            {"id": "s1", "step_type": "condition", "step_subtype": "if_else",
             "config": {"conditions": [{"field": "tags", "operator": "has_tag", "value": "do-not-contact"}], "logic": "and"}, "x": 400, "y": 80},
            {"id": "s2", "step_type": "control", "step_subtype": "exit",
             "config": {}, "x": 200, "y": 220},
            {"id": "s3", "step_type": "condition", "step_subtype": "if_else",
             "config": {"conditions": [{"field": "temperature", "operator": "temperature_is", "value": "hot"}], "logic": "and"}, "x": 500, "y": 220},
            {"id": "s4", "step_type": "action", "step_subtype": "add_tag",
             "config": {"tag": "hot-lead-responded"}, "x": 300, "y": 380},
            {"id": "s5", "step_type": "action", "step_subtype": "ai_call",
             "config": {"ring_timeout": 30}, "x": 300, "y": 520},
            {"id": "s6", "step_type": "action", "step_subtype": "send_igb_message",
             "config": {"mode": "ai", "prompt_hint": "The lead just replied to our SMS. Continue the conversation naturally and try to book an appointment or gather more details about their coverage needs."}, "x": 650, "y": 380},
            {"id": "s7", "step_type": "control", "step_subtype": "exit",
             "config": {}, "x": 400, "y": 660},
        ],
        "connections": [
            {"from": "s1", "to": "s2", "branch": "true"},
            {"from": "s1", "to": "s3", "branch": "false"},
            {"from": "s3", "to": "s4", "branch": "true"},
            {"from": "s3", "to": "s6", "branch": "false"},
            {"from": "s4", "to": "s5", "branch": "default"},
            {"from": "s5", "to": "s7", "branch": "default"},
            {"from": "s6", "to": "s7", "branch": "default"},
        ],
    },
    {
        "name": "Re-engage Cold Leads",
        "description": "Targets leads with no response in 7 days. Sends a gentle check-in, waits for a reply, then either tags as re-engaged or makes a final AI follow-up call.",
        "trigger_type": "no_response",
        "trigger_config": {"days": 7, "exit_on_reply": True},
        "steps": [
            {"id": "s1", "step_type": "action", "step_subtype": "send_sms",
             "config": {"message": "Hey {{firstName}}, just checking in! I know life gets busy. If you're still thinking about coverage, I'd be happy to answer any questions. Just reply here anytime."}, "x": 400, "y": 80},
            {"id": "s2", "step_type": "action", "step_subtype": "add_tag",
             "config": {"tag": "re-engage-attempt"}, "x": 400, "y": 200},
            {"id": "s3", "step_type": "control", "step_subtype": "wait",
             "config": {"duration": 1, "unit": "days"}, "x": 400, "y": 320},
            {"id": "s4", "step_type": "condition", "step_subtype": "if_else",
             "config": {"conditions": [{"field": "responded", "operator": "responded_within", "value": "1440"}], "logic": "and"}, "x": 400, "y": 440},
            {"id": "s5", "step_type": "action", "step_subtype": "add_tag",
             "config": {"tag": "re-engaged"}, "x": 600, "y": 580},
            {"id": "s6", "step_type": "action", "step_subtype": "ai_call",
             "config": {"ring_timeout": 25}, "x": 200, "y": 580},
            {"id": "s7", "step_type": "control", "step_subtype": "wait",
             "config": {"duration": 3, "unit": "days"}, "x": 200, "y": 720},
            {"id": "s8", "step_type": "action", "step_subtype": "send_sms",
             "config": {"message": "Hi {{firstName}}, I wanted to reach out one last time. If you ever need help with insurance coverage, don't hesitate to reach out. Wishing you the best!"}, "x": 200, "y": 860},
            {"id": "s9", "step_type": "action", "step_subtype": "add_tag",
             "config": {"tag": "nurture-complete"}, "x": 400, "y": 1000},
            {"id": "s10", "step_type": "control", "step_subtype": "exit",
             "config": {}, "x": 400, "y": 1120},
        ],
        "connections": [
            {"from": "s1", "to": "s2", "branch": "default"},
            {"from": "s2", "to": "s3", "branch": "default"},
            {"from": "s3", "to": "s4", "branch": "default"},
            {"from": "s4", "to": "s5", "branch": "true"},
            {"from": "s4", "to": "s6", "branch": "false"},
            {"from": "s5", "to": "s9", "branch": "default"},
            {"from": "s6", "to": "s7", "branch": "default"},
            {"from": "s7", "to": "s8", "branch": "default"},
            {"from": "s8", "to": "s9", "branch": "default"},
            {"from": "s9", "to": "s10", "branch": "default"},
        ],
    },
]


def _seed_default_workflows(location_id, cur):
    """Insert pre-built workflow templates for a new location (draft status).

    Only runs once per location — checks for existing workflows first.
    """
    cur.execute("SELECT 1 FROM workflows WHERE location_id = %s LIMIT 1", (location_id,))
    if cur.fetchone():
        return  # Already has workflows — skip seeding

    now = _now()
    for template in PREBUILT_WORKFLOWS:
        wf_id = _gen_id()
        cur.execute("""
            INSERT INTO workflows (id, location_id, name, description, status,
                                   trigger_type, trigger_config, created_at, updated_at, created_by)
            VALUES (%s, %s, %s, %s, 'draft', %s, %s, %s, %s, 'system')
        """, (wf_id, location_id, template["name"], template["description"],
              template["trigger_type"], json.dumps(template["trigger_config"]),
              now, now))

        # Map template step IDs to real UUIDs
        id_map = {}
        for step in template["steps"]:
            real_id = _gen_id()
            id_map[step["id"]] = real_id
            cur.execute("""
                INSERT INTO workflow_steps (id, workflow_id, step_type, step_subtype,
                                           config, position_x, position_y, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (real_id, wf_id, step["step_type"], step["step_subtype"],
                  json.dumps(step["config"]), step["x"], step["y"], now))

        for conn in template["connections"]:
            cur.execute("""
                INSERT INTO workflow_connections (id, workflow_id, from_step_id, to_step_id,
                                                 branch_key, sort_order)
                VALUES (%s, %s, %s, %s, %s, 0)
            """, (_gen_id(), wf_id, id_map[conn["from"]], id_map[conn["to"]], conn["branch"]))

    logger.info(f"Seeded {len(PREBUILT_WORKFLOWS)} default workflows for {location_id}")


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

        # Lazy-seed pre-built templates on first load
        try:
            _seed_default_workflows(location_id, cur)
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Workflow seed failed (non-fatal): {e}")

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

_AI_BUILDER_SYSTEM_PROMPT = """You are an enterprise workflow automation builder for an insurance CRM platform.
You create FULLY FUNCTIONAL workflows that execute real actions (send SMS, make calls, update CRM data).
Given a natural-language description, generate a structured workflow JSON.

═══ TRIGGERS (what starts the workflow) ═══

Event-driven (fire immediately when event occurs):
- contact_created: New lead/contact imported. Config: {"source_filter": "any"}
- sms_received: Inbound text message. Config: {"keyword_filter": "optional keyword to match"}
- inbound_call: Contact calls in. Config: {}
- missed_call: Call was missed/unanswered. Config: {}
- voicemail_received: Voicemail left. Config: {}
- tag_added: Tag was added to contact. Config: {"tag": "specific-tag-name"}
- tag_removed: Tag was removed. Config: {"tag": "specific-tag-name"}
- stage_changed: Pipeline stage changed. Config: {"pipeline_id": "", "stage_id": ""}
- appointment_booked: Appointment created. Config: {"calendar_id": ""}
- appointment_noshow: Contact no-showed appointment. Config: {}
- contact_dnd: Contact opted out (Do Not Disturb set). Config: {}
- field_updated: Contact field was updated. Config: {"field_name": "fieldKey"}

Time-based (polled by cron every 1-5 minutes):
- scheduled: Recurring cron schedule. Config: {"cron": "0 9 * * *", "timezone": "America/New_York", "tag_filter": ["optional-tag"]}
  Cron format: minute hour day_of_month month day_of_week (0=Monday)
  Examples: "0 9 * * 1-5" = 9 AM weekdays, "*/30 * * * *" = every 30 min
- no_response: Contact hasn't replied in X days. Config: {"days": 3}
- lead_age: Contact was imported X+ days ago. Config: {"days_since_import": 60}
- birthday_approaching: Contact's birthday in X days. Config: {"days_before": 7}
- manual: Only triggered via API/test button. Config: {}

═══ ACTIONS (step_subtypes — what the workflow does) ═══

Communication:
- send_sms: Send text message. Config: {"message": "Hi {{firstName}}, ...", "from_strategy": "default"|"closest_state"|"rotate"}
  from_strategy: "default" = primary number, "closest_state" = number matching contact's state, "rotate" = round-robin
- ai_call: Make AI voice call. Config: {"voice_prompt": "Optional custom prompt", "ring_timeout": 30}

CRM Actions:
- add_tag: Add tag to contact. Config: {"tag": "tag-name"}
- remove_tag: Remove tag. Config: {"tag": "tag-name"}
- assign_agent: Assign contact to user. Config: {"assigned_to": "user_id"}
- update_field: Update any contact field. Config: {"field_key": "firstName|lastName|phone|email|city|state|companyName|customField.key", "field_value": "new value"}
- add_note: Add note to contact. Config: {"body": "Note text with {{firstName}} merge fields"}
- move_stage: Move contact in pipeline. Config: {"pipeline_id": "...", "stage_id": "..."}

AI-Powered Messaging:
- send_igb_message: Trigger InsuranceGrokBot's full AI pipeline to send an intelligent, context-aware SMS.
  Two modes:
  - AI mode (default): The bot reads the contact's full conversation history, understands context, and generates a smart reply.
    Config: {"mode": "ai", "prompt_hint": "Optional guidance for the AI, e.g. 'Re-engage about their life insurance quote'"}
  - Manual mode: Send exact text through the IGB-configured SMS channel (GHL or Twilio, respects voice_config).
    Config: {"mode": "manual", "manual_message": "Hi {{firstName}}, just checking in! Are you still looking for coverage?"}
  Use send_igb_message instead of send_sms when you want the AI to compose the message based on full conversation context.
  Use send_sms for simple templated messages. Use send_igb_message for intelligent, personalized outreach.

Integration:
- send_webhook: HTTP request to external URL. Config: {"url": "https://...", "payload": {"key": "value"}, "secret": "optional-hmac-secret"}

Logic & Flow Control:
- if_else: Conditional branching. Config: {"conditions": [{"field": "...", "operator": "...", "value": "..."}], "logic": "and"|"or"}
  Outgoing connections use branch_key "true" or "false"
  Field can reference query_results from state_query steps (e.g. "days_since_contact")
- loop: Repeat a section up to N times. Config: {"max_iterations": 15}
  Outgoing connections: "loop" (continue) or "exit" (done)
- wait: Fixed duration pause. Config: {"duration": 1, "unit": "seconds"|"minutes"|"hours"|"days"}
- wait_until: Smart conditional wait — pauses until a condition becomes true OR timeout.
  Config: {"condition": {"field": "...", "operator": "...", "value": "..."}, "max_wait_hours": 72, "check_interval_minutes": 5}
  Outgoing connections: "condition_met" or "timeout"
  Example: wait until contact replies: {"condition": {"field": "responded_within", "operator": "responded_within", "value": "5"}, "max_wait_hours": 48}
- state_query: Query the database and store results for downstream conditions.
  Config: {"query_type": "last_outbound_message|last_inbound_message|message_count|call_count|last_call_date|days_since_contact|contact_field|workflow_run_count", "store_as": "variable_name"}
  Results stored in context — use the "store_as" name as "field" in subsequent if_else conditions.
  Example: {"query_type": "days_since_contact", "store_as": "days_silent"} then if_else with {"field": "days_silent", "operator": "greater_than", "value": "7"}
- goto: Jump to another step. Config: {"target_step_id": "step_N"}
- exit: End the workflow. Config: {}

Custom Actions:
- custom: Freeform action interpreted by AI at runtime. Config: {"description": "what this action should do", "action_name": "descriptive-name"}
  The AI engine interprets the description and maps it to real actions (SMS, tags, webhooks, etc.)
  Can also include sub_actions for explicit multi-step: {"sub_actions": [{"type": "add_tag", "config": {"tag": "x"}}]}
  Or delegate to a known type: {"execute_as": "send_sms", "execute_config": {"message": "..."}}

═══ MERGE FIELDS (for SMS, notes, field values) ═══
{{firstName}}, {{lastName}}, {{phone}}, {{email}}, {{city}}, {{state}}, {{companyName}}, {{tags}}, {{source}}

═══ CONDITION OPERATORS (for if_else) ═══
String: equals, not_equals, contains, starts_with
Existence: is_empty, is_not_empty
Numeric: greater_than, less_than
Tags: has_tag, no_tag (value = tag name)
Location: in_state (value = "CA", "TX", etc. — checks phone area code)
Lead data: lead_age_days (value = minimum days since import)
AI intelligence: score_above, score_below (value = 0-100), temperature_is (value = "hot"|"warm"|"cool"|"cold")
Activity: responded_within (value = minutes), total_messages_sent (value = count threshold)
Timing: time_is_between (value = "09:00-17:00" — checks contact's timezone)

═══ OUTPUT FORMAT ═══
Return ONLY valid JSON (no markdown, no explanation):
{
  "name": "Descriptive Workflow Name",
  "description": "Brief description of what this workflow does",
  "trigger_type": "one_of_the_triggers_above",
  "trigger_config": {},
  "steps": [
    {"temp_id": "step_1", "step_type": "action", "step_subtype": "send_sms", "config": {"message": "Hi {{firstName}}!"}, "position_x": 400, "position_y": 100},
    {"temp_id": "step_2", "step_type": "control", "step_subtype": "wait", "config": {"duration": 4, "unit": "hours"}, "position_x": 400, "position_y": 250}
  ],
  "connections": [
    {"from_temp_id": "step_1", "to_temp_id": "step_2", "branch_key": "default"}
  ]
}

step_type values: "action" (sends/updates), "condition" (if_else), "control" (wait, loop, goto, exit)

═══ LAYOUT ═══
- Center steps at x=400, space y by 150px
- For if_else branches: true path at x=250, false path at x=550
- For loops: place loop body steps, then connect back to the loop step

═══ IMPORTANT ═══
- Every workflow MUST end with either an "exit" step or reach a dead end (no outgoing connection)
- Every non-trigger step must have at least one incoming connection
- Loops must use the "loop" step with max_iterations to prevent infinite execution
- For complex schedules (e.g. "every day for 4 days, then every 3rd day"), use loop + wait combinations
- Custom actions are REAL — they execute via AI interpretation at runtime, not cosmetic placeholders
- All triggers are REAL and fire actual workflow runs — event-driven triggers fire from webhooks, time-based triggers fire from cron
- AUTO-EXIT: By default, workflows automatically exit when the contact replies (sends an inbound message). This is a safety mechanism — follow-up sequences stop when the lead engages. This can be disabled in trigger_config: {"exit_on_reply": false}
- Use state_query + if_else combos for data-driven decisions (e.g. "query days_since_contact, then branch if > 7")
- Use wait_until for event-driven waits (e.g. "wait until contact replies, max 48 hours")
- For the pattern "send daily for X days, then every Nth day": use nested loop + wait combos

═══ EXAMPLE: Complex Follow-up Sequence ═══
"Text every day for 4 days, then every 3rd day for 4 cycles, then every 10 days until 15 total"
→ Use: loop(4) → send_sms → wait(1 day) → loop(4) → send_sms → wait(3 days) → loop(7) → send_sms → wait(10 days) → exit
Each loop's "exit" branch connects to the next loop. The final loop exits to an exit node."""


_AI_BUILDER_TOOL = {
    "type": "function",
    "function": {
        "name": "create_workflow",
        "description": "Create a structured workflow automation from a user's natural language description",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short descriptive workflow name"},
                "description": {"type": "string", "description": "Brief description of what this workflow does"},
                "trigger_type": {
                    "type": "string",
                    "enum": [
                        "contact_created", "sms_received", "inbound_call", "missed_call",
                        "voicemail_received", "tag_added", "tag_removed", "stage_changed",
                        "appointment_booked", "appointment_noshow", "contact_dnd",
                        "no_response", "lead_age", "birthday_approaching", "field_updated",
                        "manual", "scheduled",
                    ],
                },
                "trigger_config": {"type": "object", "description": "Trigger-specific configuration"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "temp_id": {"type": "string"},
                            "step_type": {"type": "string", "enum": ["action", "condition", "control"]},
                            "step_subtype": {
                                "type": "string",
                                "enum": [
                                    "send_sms", "send_igb_message", "ai_call", "add_tag", "remove_tag", "assign_agent",
                                    "wait", "wait_until", "update_field", "add_note", "send_webhook",
                                    "if_else", "loop", "goto", "exit", "custom", "move_stage",
                                    "state_query",
                                ],
                            },
                            "config": {"type": "object"},
                            "position_x": {"type": "number"},
                            "position_y": {"type": "number"},
                        },
                        "required": ["temp_id", "step_type", "step_subtype", "config"],
                    },
                },
                "connections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "from_temp_id": {"type": "string"},
                            "to_temp_id": {"type": "string"},
                            "branch_key": {"type": "string"},
                        },
                        "required": ["from_temp_id", "to_temp_id", "branch_key"],
                    },
                },
            },
            "required": ["name", "trigger_type", "steps", "connections"],
        },
    },
}


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
            model="grok-3-fast",
            messages=[
                {"role": "system", "content": _AI_BUILDER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            tools=[_AI_BUILDER_TOOL],
            tool_choice={"type": "function", "function": {"name": "create_workflow"}},
            temperature=0.2,
            max_tokens=4096,
        )

        # Extract from function call if available
        msg = response.choices[0].message
        workflow_data = None

        if msg.tool_calls:
            raw = msg.tool_calls[0].function.arguments
            workflow_data = json.loads(raw)
        elif msg.content:
            # Fallback: parse from content if function calling not used
            raw = msg.content.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()
            workflow_data = json.loads(raw)
        else:
            return jsonify({"error": "AI returned empty response"}), 422

        # Validate the AI output
        if not isinstance(workflow_data, dict):
            return jsonify({"error": "AI returned invalid format"}), 422

        # Ensure required fields
        workflow_data.setdefault("trigger_type", "manual")
        workflow_data.setdefault("steps", [])
        workflow_data.setdefault("connections", [])
        workflow_data.setdefault("name", "AI-Built Workflow")
        workflow_data.setdefault("trigger_config", {})

        # Auto-enable exit_on_reply for follow-up sequences
        if workflow_data["trigger_config"].get("exit_on_reply") is None:
            workflow_data["trigger_config"]["exit_on_reply"] = True

        # Validate trigger type
        valid_triggers = {
            "contact_created", "sms_received", "inbound_call", "missed_call",
            "voicemail_received", "tag_added", "tag_removed", "stage_changed",
            "appointment_booked", "appointment_noshow", "contact_dnd",
            "no_response", "lead_age", "birthday_approaching", "field_updated",
            "manual", "scheduled",
        }
        if workflow_data["trigger_type"] not in valid_triggers:
            logger.warning(f"AI used invalid trigger: {workflow_data['trigger_type']}")
            workflow_data["trigger_type"] = "manual"

        # Validate step subtypes
        valid_subtypes = {
            "send_sms", "send_igb_message", "ai_call", "add_tag", "remove_tag", "assign_agent",
            "wait", "wait_until", "update_field", "add_note", "send_webhook",
            "if_else", "loop", "goto", "exit", "custom", "move_stage",
            "state_query",
        }
        for step in workflow_data["steps"]:
            if step.get("step_subtype") not in valid_subtypes:
                original_subtype = step.get("step_subtype", "unknown")
                step["step_subtype"] = "custom"
                step.setdefault("config", {})
                step["config"]["description"] = step["config"].get("description",
                    f"Custom action: {original_subtype}")

            # Ensure position defaults
            step.setdefault("position_x", 400)
            step.setdefault("position_y", 100)

        return jsonify({"workflow": workflow_data})
    except json.JSONDecodeError:
        logger.error(f"AI builder returned invalid JSON: {raw[:500] if 'raw' in dir() else 'N/A'}")
        return jsonify({"error": "AI returned invalid workflow structure. Please try rephrasing."}), 422
    except Exception as e:
        logger.error(f"AI builder error: {e}", exc_info=True)
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
                from workflow_engine import execute_workflow_run
                q_production.enqueue(
                    execute_workflow_run,
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


# ── GHL Workflow Import ─────────────────────────────────────────────────────

GHL_BASE = "https://services.leadconnectorhq.com"


@workflows_bp.route("/api/workflows/ghl", methods=["GET"])
@login_required
def list_ghl_workflows():
    """Fetch workflows from GHL via workflows.readonly scope."""
    location_id = current_user.location_id
    token = get_valid_token(location_id)
    if not token:
        return jsonify({"error": "GHL not connected. Please connect your GoHighLevel account first."}), 401

    try:
        resp = requests.get(
            f"{GHL_BASE}/workflows/",
            headers={
                "Authorization": f"Bearer {token}",
                "Version": "2021-07-28",
            },
            params={"locationId": location_id},
            timeout=15,
        )
        if resp.status_code == 401:
            return jsonify({"error": "GHL token expired. Please reconnect."}), 401
        if resp.status_code != 200:
            logger.warning(f"GHL workflows API returned {resp.status_code}: {resp.text[:200]}")
            return jsonify({"error": "Failed to fetch GHL workflows"}), 502

        data = resp.json()
        workflows = data.get("workflows", [])

        # Normalize to a clean format for the frontend
        result = []
        for wf in workflows:
            result.append({
                "id": wf.get("id", ""),
                "name": wf.get("name", "Untitled"),
                "status": wf.get("status", "draft"),
                "created_at": wf.get("createdAt", ""),
                "updated_at": wf.get("updatedAt", ""),
                "source": "ghl",
                "version": wf.get("version", 1),
            })

        return jsonify({"workflows": result})

    except requests.Timeout:
        return jsonify({"error": "GHL API timeout"}), 504
    except Exception as e:
        logger.error(f"GHL workflow fetch error: {e}")
        return jsonify({"error": "Failed to fetch GHL workflows"}), 500
