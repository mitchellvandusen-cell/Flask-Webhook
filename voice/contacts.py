import json
import os
import logging
import re
from datetime import datetime

import requests as http_requests
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from ghl_auth import jwt_or_session_required

from blueprints.team import require_permission
from db import (
    get_db_connection,
    return_db_connection,
    log_webhook_event,
    get_subscriber_info_hybrid,
    get_bot_settings_by_location,
)
from ghl_api import get_valid_token, fetch_targeted_ghl_history, fetch_contact_data_from_ghl
from ghl_message import send_sms_via_ghl
from openai import OpenAI
from llm_caller import generate_clean_reply
from voice.audio import XAI_API_KEY
from voice.call_state import custom_field_defs
from voice.helpers import _get_current_subscriber_voice

logger = logging.getLogger("voice_bridge.contacts")

contacts_bp = Blueprint('voice_contacts', __name__)

GHL_API_BASE = "https://services.leadconnectorhq.com"
GHL_API_VERSION = "2021-07-28"

_HTML_TAG_RE = re.compile(r'<[^>]+>')
_HTML_MULTI_SPACE_RE = re.compile(r'\s{2,}')


def _strip_html(text: str) -> str:
    """Strip HTML tags and collapse whitespace from note/text bodies."""
    if not text:
        return text
    clean = _HTML_TAG_RE.sub(' ', text)
    clean = _HTML_MULTI_SPACE_RE.sub(' ', clean)
    return clean.strip()


def _get_current_location_and_token():
    """Return (location_id, access_token, headers) for the current user, or raise."""
    conn = get_db_connection()
    if not conn:
        return None, None, None
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return None, None, None
        location_id = row['location_id']
    finally:
        return_db_connection(conn)

    access_token = get_valid_token(location_id)
    if not access_token:
        return location_id, None, None

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": GHL_API_VERSION,
        "Content-Type": "application/json",
    }
    return location_id, access_token, headers


def _get_custom_field_defs(location_id: str, headers: dict) -> dict:
    """
    Return {field_id: field_name} for every custom field in this location.
    Results are cached in custom_field_defs for the lifetime of the process.
    GHL endpoint: GET /locations/{locationId}/customFields
    """
    if location_id in custom_field_defs:
        return custom_field_defs[location_id]
    try:
        resp = http_requests.get(
            f"{GHL_API_BASE}/locations/{location_id}/customFields",
            headers=headers,
            timeout=8,
        )
        if resp.status_code == 200:
            defs = resp.json().get("customFields", [])
            custom_field_defs[location_id] = {
                d["id"]: d.get("name") or d.get("fieldKey") or d["id"]
                for d in defs
                if "id" in d
            }
        else:
            custom_field_defs[location_id] = {}
    except Exception as e:
        logger.warning(f"Could not fetch custom field defs for {location_id}: {e}")
        custom_field_defs[location_id] = {}
    return custom_field_defs[location_id]


@contacts_bp.route('/voice/contact/<contact_id>', methods=['GET'])
@jwt_or_session_required
def get_contact_detail(contact_id):
    """Fetch full GHL contact details including notes and tags."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
    finally:
        return_db_connection(conn)

    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid auth token"}), 401

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Version": GHL_API_VERSION,
    }

    try:
        # Fetch contact details
        resp = http_requests.get(
            f"{GHL_API_BASE}/contacts/{contact_id}",
            headers=headers,
            timeout=10
        )
        if resp.status_code != 200:
            return jsonify({"error": f"CRM returned {resp.status_code}"}), resp.status_code

        contact = resp.json().get("contact", {})

        # Fetch notes
        notes = []
        try:
            notes_resp = http_requests.get(
                f"{GHL_API_BASE}/contacts/{contact_id}/notes",
                headers=headers,
                timeout=10
            )
            if notes_resp.status_code == 200:
                notes = notes_resp.json().get("notes", [])
        except Exception:
            pass

        # Enrich custom fields with human-readable names from the location's field definitions
        field_defs = _get_custom_field_defs(location_id, headers)
        raw_custom_fields = contact.get("customFields", [])
        enriched_custom_fields = [
            {
                **cf,
                "name": field_defs.get(cf.get("id", "")) or cf.get("name") or cf.get("fieldKey") or "Field",
            }
            for cf in raw_custom_fields
            if isinstance(cf, dict)
        ]

        result = {
            "id": contact.get("id", ""),
            "firstName": contact.get("firstName", ""),
            "lastName": contact.get("lastName", ""),
            "name": f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip(),
            "phone": contact.get("phone", ""),
            "email": contact.get("email", ""),
            "tags": contact.get("tags", []),
            "dnd": contact.get("dnd", False),
            "dndSettings": contact.get("dndSettings", {}),
            "address": contact.get("address1", ""),
            "city": contact.get("city", ""),
            "state": contact.get("state", ""),
            "source": contact.get("source", ""),
            "dateAdded": contact.get("dateAdded", ""),
            "customFields": enriched_custom_fields,
            "notes": [
                {
                    "id": n.get("id", ""),
                    "body": _strip_html(n.get("body", "")),
                    "dateAdded": n.get("dateAdded", ""),
                    "userId": n.get("userId", ""),
                }
                for n in sorted(notes, key=lambda n: n.get("dateAdded", ""), reverse=True)[:20]
            ],
        }

        # ── InsuranceGrokBot Engagement Data ──
        igb_conn = get_db_connection()
        if igb_conn:
            try:
                cur = igb_conn.cursor()

                # SMS message counts from our contact_messages table
                cur.execute("""
                    SELECT message_type, COUNT(*) as cnt,
                           MAX(created_at) as last_at
                    FROM contact_messages
                    WHERE contact_id = %s
                    GROUP BY message_type
                """, (contact_id,))
                msg_stats = {"lead": 0, "assistant": 0, "last_message_at": None}
                for r in cur.fetchall():
                    msg_stats[r['message_type']] = r['cnt']
                    ts = r['last_at']
                    if ts:
                        iso = ts.isoformat()
                        if not msg_stats["last_message_at"] or iso > msg_stats["last_message_at"]:
                            msg_stats["last_message_at"] = iso

                # Call history stats from call_history table
                cur.execute("""
                    SELECT COUNT(*) as total_calls,
                           COUNT(*) FILTER (WHERE status = 'completed') as connected,
                           COALESCE(SUM(duration), 0) as total_duration,
                           MAX(started_at) as last_call_at,
                           COUNT(*) FILTER (WHERE recording_url IS NOT NULL) as recordings
                    FROM call_history
                    WHERE contact_id = %s AND location_id = %s
                """, (contact_id, location_id))
                call_row = cur.fetchone()
                call_stats = {
                    "total_calls": call_row['total_calls'] if call_row else 0,
                    "connected": call_row['connected'] if call_row else 0,
                    "total_duration": call_row['total_duration'] if call_row else 0,
                    "last_call_at": call_row['last_call_at'].isoformat() if call_row and call_row['last_call_at'] else None,
                    "recordings": call_row['recordings'] if call_row else 0,
                }

                # Disposition breakdown
                cur.execute("""
                    SELECT disposition, COUNT(*) as cnt
                    FROM call_history
                    WHERE contact_id = %s AND location_id = %s
                          AND disposition IS NOT NULL
                    GROUP BY disposition
                """, (contact_id, location_id))
                call_stats["dispositions"] = {r['disposition']: r['cnt'] for r in cur.fetchall()}

                # AI narrative summary from contact_narratives
                cur.execute("""
                    SELECT story_narrative, updated_at
                    FROM contact_narratives
                    WHERE contact_id = %s
                """, (contact_id,))
                narr_row = cur.fetchone()
                narrative = {
                    "summary": narr_row['story_narrative'] if narr_row else None,
                    "updated_at": narr_row['updated_at'].isoformat() if narr_row and narr_row['updated_at'] else None,
                }

                # Contact facts from contact_facts
                cur.execute("""
                    SELECT fact_text FROM contact_facts
                    WHERE contact_id = %s
                    ORDER BY created_at DESC LIMIT 20
                """, (contact_id,))
                raw_facts = [r['fact_text'] for r in cur.fetchall()]

                # ── Clean facts: filter prompt artifacts + enforce 10-word max ──
                _prompt_patterns = [
                    'recap:', 'update previous', 'recent messages', 'already known',
                    'new facts', 'one fact per line', 'do not repeat', 'write none',
                    'conversation note', 'output exactly', 'no reasoning',
                    'facts:', 'previous recap', 'no commentary', 'output format',
                    'keep chronological', 'concise but complete', 'updated recap',
                    'should build on', 'maximum', 'per line', 'short fragments',
                ]
                clean_facts = []
                for f in raw_facts:
                    fl = f.lower().strip()
                    if len(fl) < 4 or fl.upper() == 'NONE':
                        continue
                    if any(pat in fl for pat in _prompt_patterns):
                        continue
                    # Enforce 10-word max per fact
                    words = f.strip().split()
                    if len(words) > 10:
                        f = " ".join(words[:10])
                    clean_facts.append(f)

                # ── Clean narrative: strip prompt artifacts + enforce 30-word max ──
                narr_text = narrative.get("summary")
                if narr_text:
                    narr_text = re.sub(r'^RECAP:\s*', '', narr_text, flags=re.IGNORECASE).strip()
                    narr_text = re.sub(r'FACTS:.*$', '', narr_text, flags=re.DOTALL | re.IGNORECASE).strip()
                    narr_text = re.sub(r'^Update(?:\s+the)?\s+previous\s+recap\s+with.*?\.?\s*', '', narr_text, flags=re.IGNORECASE).strip()
                    narr_text = re.sub(r'^(?:Output format|Updated RECAP|Keep chronological|Note what was).*$', '', narr_text, flags=re.MULTILINE | re.IGNORECASE).strip()
                    # Enforce 30-word max
                    if narr_text:
                        words = narr_text.split()
                        if len(words) > 30:
                            narr_text = " ".join(words[:30])
                            # End at last sentence boundary if possible
                            for end in ['. ', '? ', '! ']:
                                idx = narr_text.rfind(end)
                                if idx > len(narr_text) // 2:
                                    narr_text = narr_text[:idx + 1]
                                    break
                    narrative["summary"] = narr_text if narr_text else None

                # ── Opt-out detection: CRM DnD flag + stop keywords ──
                opted_out = False

                # Check GHL DnD flag (already in result from API)
                if contact.get("dnd", False):
                    opted_out = True

                # Check last lead message for stop keywords
                if not opted_out:
                    cur.execute("""
                        SELECT message_text FROM contact_messages
                        WHERE contact_id = %s AND message_type = 'lead'
                        ORDER BY created_at DESC LIMIT 1
                    """, (contact_id,))
                    last_lead_msg = cur.fetchone()
                    if last_lead_msg and last_lead_msg['message_text']:
                        # True TCPA opt-out words only — sales objections ("not interested",
                        # "leave me alone", "quit", "blocked", etc.) are NOT opt-outs and
                        # must NOT skip leads in the dialer.
                        _stop_words = {
                            'stop', 'unsubscribe', 'opt out', 'optout',
                            'remove me', 'do not contact', 'do not call',
                            'do not text', 'do not message', 'do not reach out',
                            'cancel my subscription',
                        }
                        msg_lower = last_lead_msg['message_text'].strip().lower()
                        if msg_lower in _stop_words or any(re.search(r'\b' + re.escape(w) + r'\b', msg_lower) for w in _stop_words):
                            opted_out = True

                cur.close()

                result["igb_engagement"] = {
                    "messages": msg_stats,
                    "calls": call_stats,
                    "narrative": narrative,
                    "facts": clean_facts,
                    "opted_out": opted_out,
                }
            except Exception as e:
                logger.warning(f"Non-critical: failed to load IGB engagement data for {contact_id}: {e}")
                result["igb_engagement"] = None
            finally:
                return_db_connection(igb_conn)
        else:
            result["igb_engagement"] = None

        return jsonify(result)

    except Exception as e:
        logger.error(f"Failed to fetch contact detail: {e}")
        return jsonify({"error": "Internal server error"}), 500


@contacts_bp.route('/voice/contact/<contact_id>/messages', methods=['GET'])
@login_required
def get_contact_messages(contact_id):
    """Fetch SMS/conversation history for a contact from GHL."""
    limit = min(int(request.args.get('limit', 40)), 100)

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
    finally:
        return_db_connection(conn)

    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid auth token"}), 401

    try:
        messages = fetch_targeted_ghl_history(contact_id, location_id, access_token, limit)

        # Also fetch call history for this contact from our DB
        calls = []
        ch_conn = get_db_connection()
        if ch_conn:
            try:
                cur = ch_conn.cursor()
                cur.execute("""
                    SELECT call_sid, direction, status, duration, recording_url,
                           recording_sid, transcript, started_at, ended_at
                    FROM call_history
                    WHERE contact_id = %s AND location_id = %s
                    ORDER BY created_at DESC LIMIT 20
                """, (contact_id, location_id))
                for r in cur.fetchall():
                    calls.append({
                        "call_sid": r['call_sid'],
                        "direction": r['direction'],
                        "status": r['status'],
                        "duration": r['duration'] or 0,
                        "recording_url": r['recording_url'],
                        "transcript": r['transcript'],
                        "started_at": r['started_at'].isoformat() if r['started_at'] else None,
                    })
                cur.close()
            finally:
                return_db_connection(ch_conn)

        return jsonify({"messages": messages, "calls": calls})

    except Exception as e:
        logger.error(f"Failed to fetch contact messages: {e}")
        return jsonify({"error": "Internal server error"}), 500


@contacts_bp.route('/voice/contact/<contact_id>/send-sms', methods=['POST'])
@login_required
@require_permission('can_text')
def send_contact_sms(contact_id):
    """Send an SMS to a contact via GHL or Twilio (A2P 10DLC), based on 'channel' param."""
    data = request.json or {}
    message = (data.get('message') or '').strip()
    channel = (data.get('channel') or 'ghl').lower().strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    if len(message) > 1600:
        return jsonify({"error": "Message too long (max 1600 characters)"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id, voice_config FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
        voice_config = row['voice_config'] or {}
    finally:
        return_db_connection(conn)

    # ── Channel: Twilio (InsuranceGrokBot number, A2P registered) ──
    if channel == 'twilio':
        a2p = voice_config.get('a2p', {})
        campaign_status = (a2p.get('campaign_status') or '').upper()
        ms_sid = a2p.get('messaging_service_sid', '')
        sub_sid = voice_config.get('twilio_sub_account_sid', '')
        from_number = voice_config.get('twilio_phone_number', '')

        if campaign_status != 'VERIFIED' or not ms_sid:
            return jsonify({"error": "A2P 10DLC campaign not approved yet. Use GHL to send."}), 400
        if not sub_sid or not from_number:
            return jsonify({"error": "Twilio phone number not provisioned."}), 400

        # Resolve contact phone number from GHL
        contact_phone = (data.get('contact_phone') or '').strip()
        if not contact_phone:
            # Fetch from GHL if not provided
            access_token = get_valid_token(location_id)
            if access_token:
                try:
                    ghl_resp = http_requests.get(
                        f"https://services.leadconnectorhq.com/contacts/{contact_id}",
                        headers={"Authorization": f"Bearer {access_token}", "Version": "2021-07-28"},
                        timeout=10,
                    )
                    if ghl_resp.ok:
                        contact_phone = ghl_resp.json().get("contact", {}).get("phone", "")
                except Exception as ghl_err:
                    logger.warning(f"Failed to fetch contact phone for Twilio send: {ghl_err}")

        if not contact_phone:
            return jsonify({"error": "No phone number found for this contact."}), 400

        try:
            import twilio_provisioning
            client = twilio_provisioning.get_sub_account_client(sub_sid)
            tw_msg = client.messages.create(
                messaging_service_sid=ms_sid,
                to=contact_phone,
                body=message,
            )
            logger.info(f"Twilio SMS sent: {tw_msg.sid} to {contact_id} by {current_user.email} (A2P)")
            # Log to GHL via Conversation Provider so CRM stays in sync
            try:
                from ghl_logger import log_outbound_sms_to_ghl
                ghl_token = get_valid_token(location_id)
                if ghl_token:
                    log_outbound_sms_to_ghl(
                        contact_id=contact_id,
                        message=message,
                        access_token=ghl_token,
                        location_id=location_id,
                        contact_phone=contact_phone,
                    )
            except Exception as ghl_log_err:
                logger.debug(f"GHL conversation log skipped for manual SMS: {ghl_log_err}")
            return jsonify({"status": "sent", "channel": "twilio", "sid": tw_msg.sid})
        except Exception as e:
            logger.error(f"Twilio SMS send error for {contact_id}: {e}")
            return jsonify({"error": f"Twilio send failed: {str(e)}"}), 500

    # ── Channel: GHL (default) ──
    # When GHL OAuth env vars are missing, skip GHL entirely and go straight
    # to Twilio fallback — avoids wasted HTTP calls with an unrefreshable token.
    from ghl_api import has_oauth_credentials
    success = False
    if has_oauth_credentials():
        access_token = get_valid_token(location_id)
        if not access_token:
            # Token expired AND refresh failed — fall through to Twilio fallback
            logger.warning(f"No valid GHL token for {location_id} — trying Twilio fallback")
        else:
            try:
                success, _fail_reason, _http_detail = send_sms_via_ghl(
                    contact_id=contact_id,
                    message=message,
                    access_token=access_token,
                    location_id=location_id,
                )
                if success:
                    logger.info(f"Manual SMS sent to {contact_id} via GHL by {current_user.email}")
                    return jsonify({"status": "sent", "channel": "ghl"})
            except Exception as e:
                logger.error(f"SMS send error for {contact_id}: {e}")
    else:
        logger.debug(f"GHL OAuth creds missing — skipping GHL SMS for {contact_id}")

    # GHL skipped or failed — try Twilio fallback
    if not success:
        try:
            from twilio_sms import send_sms_via_twilio, get_twilio_credentials
            fb_sid, fb_auth, fb_from = get_twilio_credentials(location_id)
            contact_phone = (data.get('contact_phone') or '').strip()
            if fb_sid and fb_auth and fb_from and contact_phone:
                logger.warning(f"GHL SMS failed for {contact_id} — trying Twilio fallback")
                tw_ok, _, _ = send_sms_via_twilio(
                    phone_to=contact_phone, message=message,
                    from_number=fb_from, twilio_sub_account_sid=fb_sid,
                    twilio_auth_token=fb_auth, contact_id=contact_id)
                if tw_ok:
                    logger.info(f"Manual SMS sent to {contact_id} via Twilio fallback by {current_user.email}")
                    return jsonify({"status": "sent", "channel": "twilio"})
        except Exception as fb_err:
            logger.warning(f"Twilio fallback also failed for {contact_id}: {fb_err}")
        return jsonify({"error": "Failed to send SMS. Check CRM connection or Twilio setup."}), 500


@contacts_bp.route('/voice/sms-channels', methods=['GET'])
@login_required
def sms_channels():
    """Return which SMS sending channels are available for the current user."""
    subscriber, vc, sub_sid = _get_current_subscriber_voice()
    if not subscriber:
        return jsonify({"channels": ["ghl"]}), 200

    location_id = subscriber.get('location_id', '')
    has_ghl = bool(location_id and get_valid_token(location_id))

    a2p = (vc or {}).get('a2p', {})
    campaign_status = (a2p.get('campaign_status') or '').upper()
    ms_sid = a2p.get('messaging_service_sid', '')
    from_number = (vc or {}).get('twilio_phone_number', '')
    has_twilio = bool(
        campaign_status == 'VERIFIED'
        and ms_sid
        and sub_sid
        and from_number
    )

    channels = []
    if has_ghl:
        channels.append("ghl")
    if has_twilio:
        channels.append("twilio")

    return jsonify({
        "channels": channels,
        "twilio_number": from_number if has_twilio else "",
        "a2p_status": campaign_status,
    })


@contacts_bp.route('/voice/contact/<contact_id>/ai-suggest', methods=['POST'])
@login_required
def ai_suggest_sms(contact_id):
    """
    Generate an InsuranceGrokBot reply draft using the full bot pipeline
    (generate_strategic_directive → build_system_prompt → generate_clean_reply).
    Does NOT send — just returns the draft for agent review.
    """
    # Re-use the same OpenAI client that tasks.py uses (XAI base_url)
    from tasks import client as _tasks_client

    if not _tasks_client:
        return jsonify({"error": "AI client not configured (XAI_API_KEY missing)"}), 503

    # Get location_id for this agent
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
    finally:
        return_db_connection(conn)

    # Full subscriber config (same as tasks.py uses)
    subscriber = get_subscriber_info_hybrid(location_id)
    if not subscriber:
        return jsonify({"error": "No subscriber config found"}), 400

    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid GHL auth token — reconnect CRM"}), 401
    subscriber['access_token'] = access_token

    # Pull subscriber settings (mirrors tasks.py exactly)
    bot_first_name = subscriber.get('bot_first_name', 'Grok')
    timezone = subscriber.get('timezone', 'America/Chicago')
    personal_website = subscriber.get('personal_website') or ''
    contracted_carriers = subscriber.get('contracted_carriers') or []
    if isinstance(contracted_carriers, str):
        try:
            contracted_carriers = json.loads(contracted_carriers)
        except Exception:
            contracted_carriers = []

    # Best-effort contact name from GHL
    first_name = ''
    try:
        contact_data = fetch_contact_data_from_ghl(contact_id, access_token, location_id)
        first_name = (contact_data or {}).get('firstName') or ''
    except Exception:
        pass

    bot_settings = get_bot_settings_by_location(location_id)

    # === Full InsuranceGrokBot pipeline (no send) ===
    # message="" means: it's the agent's turn to reach out / compose the next reply
    try:
        from sales_director import generate_strategic_directive
        director_output = generate_strategic_directive(
            contact_id=contact_id,
            message="",
            first_name=first_name,
            age=None,
            address='',
            bot_settings=bot_settings,
        )
    except Exception as e:
        logger.error(f"AI suggest: sales_director failed for {contact_id}: {e}")
        return jsonify({"error": "Failed to generate tactical context"}), 500

    extra_context = director_output.get('underwriting_context', '')
    if director_output.get('company_context'):
        extra_context = f"{extra_context}\n[COMPANY INTEL] {director_output['company_context']}".strip()

    try:
        from prompt import build_system_prompt
        system_prompt = build_system_prompt(
            bot_first_name=bot_first_name,
            timezone=timezone,
            profile_str=director_output["profile_str"],
            tactical_narrative=director_output["tactical_narrative"],
            known_facts=director_output["known_facts"],
            story_narrative=director_output["story_narrative"],
            stage=director_output["stage"],
            recent_exchanges=director_output["recent_exchanges"],
            message="",
            calendar_slots="",
            context_nudge=extra_context,
            lead_type="default",
            personal_website=personal_website,
            contracted_carriers=contracted_carriers,
            bot_settings=bot_settings,
        )
    except Exception as e:
        logger.error(f"AI suggest: build_system_prompt failed for {contact_id}: {e}")
        return jsonify({"error": "Failed to build AI prompt"}), 500

    try:
        reply = generate_clean_reply(
            client=_tasks_client,
            system_prompt=system_prompt,
            user_message="",
            bot_name=bot_first_name,
        )
    except Exception as e:
        logger.error(f"AI suggest: generate_clean_reply failed for {contact_id}: {e}")
        return jsonify({"error": "AI generation failed"}), 500

    if not reply:
        return jsonify({"error": "AI returned empty reply"}), 500

    # Same post-processing as tasks.py (strip markdown, normalize punctuation)
    reply = re.sub(r'\*\*([^*]+)\*\*', r'\1', reply)
    reply = re.sub(r'\*([^*]+)\*', r'\1', reply)
    reply = re.sub(r'__([^_]+)__', r'\1', reply)
    reply = re.sub(r'_([^_]+)_', r'\1', reply)
    reply = reply.replace("—", ",").replace("–", ",").replace("…", "...").strip()

    logger.info(f"InsuranceGrokBot draft generated for {contact_id} by {current_user.email} | '{reply[:60]}'")
    return jsonify({"suggestion": reply})


@contacts_bp.route('/voice/pipelines', methods=['GET'])
@login_required
def get_pipelines():
    """Fetch pipelines and stages for contact filtering (uses contacts.readonly scope)."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Database error"}), 500
    try:
        cur = conn.cursor()
        cur.execute("SELECT location_id FROM subscribers WHERE email = %s", (current_user.email,))
        row = cur.fetchone()
        cur.close()
        if not row or not row['location_id']:
            return jsonify({"error": "No location configured"}), 400
        location_id = row['location_id']
    finally:
        return_db_connection(conn)

    access_token = get_valid_token(location_id)
    if not access_token:
        return jsonify({"error": "No valid auth token"}), 401

    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Version": GHL_API_VERSION,
        }
        resp = http_requests.get(
            f"{GHL_API_BASE}/opportunities/pipelines",
            headers=headers,
            params={"locationId": location_id},
            timeout=10
        )
        # On 401, try one forced token refresh before giving up
        if resp.status_code == 401:
            logger.info("Pipelines 401 — attempting forced token refresh")
            from ghl_api import get_valid_token_with_status
            refreshed_token, _was_refreshed, err = get_valid_token_with_status(
                location_id, force_refresh=True
            )
            if refreshed_token and refreshed_token != access_token:
                headers["Authorization"] = f"Bearer {refreshed_token}"
                resp = http_requests.get(
                    f"{GHL_API_BASE}/opportunities/pipelines",
                    headers=headers,
                    params={"locationId": location_id},
                    timeout=10
                )
                logger.info(f"Pipelines retry after refresh: {resp.status_code}")
        if resp.status_code in (401, 403, 422):
            logger.warning(f"Pipelines fetch returned {resp.status_code} — "
                           f"token may need re-auth or scope missing. "
                           f"Response: {resp.text[:300]}")
            return jsonify({"pipelines": [], "scope_missing": True})
        if resp.status_code != 200:
            logger.warning(f"Pipelines fetch returned {resp.status_code}: {resp.text[:300]}")
            return jsonify({"pipelines": []})

        pipelines = resp.json().get("pipelines", [])
        result = []
        for p in pipelines:
            result.append({
                "id": p.get("id", ""),
                "name": p.get("name", ""),
                "stages": [
                    {"id": s.get("id", ""), "name": s.get("name", "")}
                    for s in p.get("stages", [])
                ]
            })
        return jsonify({"pipelines": result})

    except Exception as e:
        logger.error(f"Failed to fetch pipelines: {e}")
        return jsonify({"pipelines": []})


# ─────────────────────────────────────────────────────────────────────────────
#  Note creation — POST /voice/contact/<contact_id>/notes
# ─────────────────────────────────────────────────────────────────────────────

@contacts_bp.route('/voice/contact/<contact_id>/notes', methods=['POST'])
@login_required
def create_contact_note(contact_id):
    """Create a note on a GHL contact (requires contacts.write scope)."""
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"error": "Note body is required"}), 400

    location_id, access_token, headers = _get_current_location_and_token()
    if not access_token:
        return jsonify({"error": "No valid auth token"}), 401

    try:
        resp = http_requests.post(
            f"{GHL_API_BASE}/contacts/{contact_id}/notes",
            headers=headers,
            json={"body": body, "userId": ""},
            timeout=10,
        )
        if resp.status_code in (200, 201):
            note = resp.json().get("note", {})
            return jsonify({
                "ok": True,
                "note": {
                    "id": note.get("id", ""),
                    "body": _strip_html(note.get("body", body)),
                    "dateAdded": note.get("dateAdded", ""),
                }
            })
        logger.warning(f"GHL note creation {resp.status_code}: {resp.text[:300]}")
        return jsonify({"error": f"GHL returned {resp.status_code}"}), resp.status_code
    except Exception as e:
        logger.error(f"create_contact_note error: {e}")
        return jsonify({"error": "Internal server error"}), 500


# ─────────────────────────────────────────────────────────────────────────────
#  Contact update — PUT /voice/contact/<contact_id>
# ─────────────────────────────────────────────────────────────────────────────

_ALLOWED_UPDATE_FIELDS = {
    "firstName", "lastName", "phone", "email",
    "address1", "city", "state", "postalCode", "country",
    "companyName", "website", "source", "tags",
}

@contacts_bp.route('/voice/contact/<contact_id>', methods=['PUT'])
@login_required
def update_contact(contact_id):
    """Update GHL contact fields (requires contacts.write scope)."""
    data = request.get_json(silent=True) or {}
    payload = {k: v for k, v in data.items() if k in _ALLOWED_UPDATE_FIELDS}
    if not payload:
        return jsonify({"error": "No valid fields to update"}), 400

    location_id, access_token, headers = _get_current_location_and_token()
    if not access_token:
        return jsonify({"error": "No valid auth token"}), 401

    try:
        resp = http_requests.put(
            f"{GHL_API_BASE}/contacts/{contact_id}",
            headers=headers,
            json=payload,
            timeout=10,
        )
        if resp.status_code in (200, 201):
            return jsonify({"ok": True})
        logger.warning(f"GHL contact update {resp.status_code}: {resp.text[:300]}")
        return jsonify({"error": f"GHL returned {resp.status_code}"}), resp.status_code
    except Exception as e:
        logger.error(f"update_contact error: {e}")
        return jsonify({"error": "Internal server error"}), 500
