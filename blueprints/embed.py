# blueprints/embed.py — Embeddable Panel Routes
#
# Lightweight iframe-friendly routes for embedding IGB inside HubSpot
# (and later, any CRM). No sidebar, no topbar — minimal chrome designed
# for CRM Card iframes and Chrome extension popups.
#
# Routes:
#   GET /embed/panel             — Mini-dashboard (no sidebar/topbar)
#   GET /embed/dialer            — Dialer focused on one contact
#   GET /embed/intelligence/<id> — AI intelligence card only

import logging
import os

from flask import Blueprint, render_template, request, jsonify

from db import get_db_connection, return_db_connection

logger = logging.getLogger(__name__)

embed_bp = Blueprint("embed", __name__)


def _get_intelligence(contact_id, location_id):
    """Fetch cached AI intelligence for a contact."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT analysis, analyzed_at
            FROM contact_intelligence
            WHERE contact_id = %s AND location_id = %s
        """, (contact_id, location_id))
        row = cur.fetchone()
        if row and row.get("analysis"):
            import json
            analysis = row["analysis"]
            if isinstance(analysis, str):
                analysis = json.loads(analysis)
            analysis["analyzed_at"] = str(row.get("analyzed_at", ""))
            return analysis
        return None
    except Exception as e:
        logger.error(f"Embed intelligence fetch error: {e}")
        return None
    finally:
        return_db_connection(conn)


@embed_bp.route("/embed/panel")
def embed_panel():
    """Mini-dashboard without sidebar/topbar — for CRM iframe embedding."""
    return render_template("embed_base.html",
                           page_title="Omnisconn",
                           content_type="panel")


@embed_bp.route("/embed/dialer")
def embed_dialer():
    """Dialer focused on one contact — for CRM iframe embedding."""
    contact_id = request.args.get("contact", "")
    return render_template("embed_base.html",
                           page_title="IGB Dialer",
                           content_type="dialer",
                           contact_id=contact_id)


@embed_bp.route("/embed/intelligence/<contact_id>")
def embed_intelligence(contact_id):
    """
    AI intelligence card for a contact — zero AI cost (cache only).
    Designed for HubSpot CRM Card iframes.
    """
    # Try to identify the subscriber from query params
    location_id = request.args.get("location_id", "")
    portal_id = request.args.get("portalId", "")

    if portal_id and not location_id:
        # Look up location_id from HubSpot portal ID
        conn = get_db_connection()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT location_id FROM subscribers
                    WHERE crm_type = 'hubspot' AND crm_config->>'hub_id' = %s
                    LIMIT 1
                """, (str(portal_id),))
                row = cur.fetchone()
                if row:
                    location_id = row["location_id"]
            except Exception:
                pass
            finally:
                return_db_connection(conn)

    intelligence = None
    if location_id:
        intelligence = _get_intelligence(contact_id, location_id)

    return render_template("embed_base.html",
                           page_title="AI Intelligence",
                           content_type="intelligence",
                           contact_id=contact_id,
                           intelligence=intelligence)


@embed_bp.route("/embed/intelligence/<contact_id>/json")
def embed_intelligence_json(contact_id):
    """JSON API for intelligence data — used by CRM Card iframes via fetch()."""
    location_id = request.args.get("location_id", "")
    if not location_id:
        return jsonify({"error": "location_id required"}), 400

    intelligence = _get_intelligence(contact_id, location_id)
    if not intelligence:
        return jsonify({"status": "no_data", "contact_id": contact_id}), 200

    return jsonify({"status": "ok", "contact_id": contact_id, "intelligence": intelligence})
