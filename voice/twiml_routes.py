import logging
import os
import threading
import time
from xml.sax.saxutils import escape as xml_escape, quoteattr as xml_quoteattr

from flask import Blueprint, request, Response

import twilio_provisioning
from number_health import select_outbound_number, update_number_health
from voice.call_state import (
    set_active_call, get_active_call, update_active_call, call_exists,
    delete_transfer_request,
    _encode_client_state, _build_twiml_stream, _twilio_hangup,
)
from voice.helpers import _get_subscriber_by_phone, _get_subscriber_by_location
from voice.predictive_engine import agent_state_manager, AgentState, tcpa_tracker

logger = logging.getLogger("voice_bridge.twiml")

twiml_bp = Blueprint('voice_twiml', __name__)


# ──────────────────────────────────────────────────────────────
# ROUTE: Inbound voice webhook
# ──────────────────────────────────────────────────────────────

@twiml_bp.route('/voice/inbound', methods=['POST'])
def voice_inbound():
    """
    Twilio voice webhook — handles inbound calls and browser-initiated VoIP calls.
    Twilio POSTs form data; we respond with TwiML XML.
    - True inbound calls → <Connect><Stream> to AI bridge
    - Browser VoIP calls (From=client:xxx) → <Dial><Number> to connect agent to lead
    """
    call_sid      = request.values.get('CallSid', '')
    caller        = request.values.get('From', '')
    called        = request.values.get('To', '')
    call_status   = request.values.get('CallStatus', '')
    stir_verstat  = request.values.get('StirVerstat', '')  # STIR/SHAKEN verification for inbound

    logger.info(f"Voice inbound: CallSid={call_sid[:16] if call_sid else 'none'} From={caller} To={called} stir_verstat={stir_verstat or 'n/a'}")

    # Browser VoIP call: From=client:agent_xxx, To=+1234567890
    if caller.startswith('client:'):
        identity = caller.replace('client:', '')
        location_id = identity.replace('agent_', '') if identity.startswith('agent_') else ''
        logger.info(f"Browser VoIP call: identity={identity} location_id={location_id} To={called}")

        subscriber = _get_subscriber_by_location(location_id) if location_id else None

        if not subscriber:
            logger.warning(f"Browser VoIP: subscriber not found for location_id={location_id}, returning empty TwiML")
            return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', content_type='text/xml')

        vc = subscriber.get('voice_config') or {}
        from_number = vc.get('twilio_phone_number', '')
        sub_sid = vc.get('twilio_sub_account_sid', '')
        host = request.host

        # Smart number rotation for browser VoIP calls
        rotation_result = select_outbound_number(location_id, vc, dest_phone=called)
        if rotation_result:
            from_number = rotation_result["phone"]
            logger.info(f"Smart rotation (VoIP) selected {from_number} (reason={rotation_result['reason']})")

        if not from_number:
            logger.warning(f"Browser VoIP: no twilio_phone_number in voice_config for location_id={location_id}")
        if not called:
            logger.warning(f"Browser VoIP: no destination number (To is empty) for call {call_sid}")
            return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', content_type='text/xml')

        # Track browser VoIP call for number health updates
        if call_sid:
            set_active_call(call_sid, {
                "status": "initiated",
                "duration": 0,
                "contact_id": "",
                "phone": called,
                "name": "",
                "_location_id": location_id,
                "_sub_sid": sub_sid,
                "_host": host,
                "_from_number": from_number,
            })

        # Start recording in background
        if vc.get('auto_record', True) and sub_sid and call_sid:
            def _start_rec():
                time.sleep(1)
                try:
                    twilio_provisioning.start_recording(sub_sid, call_sid, f'https://{host}')
                except Exception as e:
                    logger.warning(f"Auto-record failed: {e}")
            threading.Thread(target=_start_rec, daemon=True).start()

        # Respond with TwiML to dial the destination number
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            f'<Dial callerId={xml_quoteattr(from_number)} action={xml_quoteattr(f"https://{host}/voice/dial-status")} method="POST">'
            f'<Number>{xml_escape(called)}</Number>'
            '</Dial>'
            '</Response>'
        )
        logger.info(f"Browser VoIP TwiML: callerId={from_number} -> {called}")
        return Response(twiml, content_type='text/xml')

    # True inbound call — look up subscriber by the called number
    subscriber = _get_subscriber_by_phone(called)
    if not subscriber:
        logger.warning(f"No subscriber for {called}; returning empty TwiML")
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', content_type='text/xml')

    vc = subscriber.get('voice_config') or {}
    sub_sid = vc.get('twilio_sub_account_sid', '')

    if not vc.get('enabled'):
        logger.warning("Inbound call but voice not enabled")
        return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', content_type='text/xml')

    host = request.host
    stream_url = os.getenv('VOICE_WSS_URL') or f'wss://{host}/voice/stream'

    # Encode metadata as custom parameters for the WebSocket stream
    client_state = _encode_client_state({
        'location_id':  subscriber.get('location_id', ''),
        'caller':       caller,
        'called':       called,
        'direction':    'inbound',
        'contact_id':   '',
        'contact_name': 'there',
    })

    # Start recording in background
    if vc.get('auto_record', True) and sub_sid and call_sid:
        def _start_rec():
            time.sleep(1)
            try:
                twilio_provisioning.start_recording(sub_sid, call_sid, f'https://{host}')
            except Exception as e:
                logger.warning(f"Auto-record failed: {e}")
        threading.Thread(target=_start_rec, daemon=True).start()

    # Track the call
    set_active_call(call_sid, {
        "status": "in-progress",
        "duration": 0,
        "contact_id": "",
        "phone": caller,
        "name": "",
        "_host": host,
        "_location_id": subscriber.get('location_id', ''),
        "_from_number": called,  # The number that received the inbound call
        "_stir_verstat": stir_verstat or None,  # STIR/SHAKEN verification result
    })

    # Respond with TwiML to connect the media stream to AI bridge
    params = {'client_state': client_state, 'callSid': call_sid}
    twiml = _build_twiml_stream(stream_url, params)
    return Response(twiml, content_type='text/xml')


# ──────────────────────────────────────────────────────────────
# ROUTE: Dial action callback (browser VoIP call dial result)
# ──────────────────────────────────────────────────────────────

@twiml_bp.route('/voice/dial-status', methods=['POST'])
def voice_dial_status():
    """
    Action URL callback for <Dial> in browser VoIP calls.
    Called when the dial attempt finishes (answered+hangup, busy, no-answer, failed).
    Logs the result and returns empty TwiML to end the parent call.
    """
    call_sid = request.values.get('CallSid', '')
    dial_call_status = request.values.get('DialCallStatus', '')
    dial_call_sid = request.values.get('DialCallSid', '')
    dial_call_duration = request.values.get('DialCallDuration', '0')
    dial_sip_code = request.values.get('SipResponseCode', '')

    logger.info(f"Browser VoIP dial result: CallSid={call_sid[:16] if call_sid else 'none'} "
                f"DialStatus={dial_call_status} DialDuration={dial_call_duration}s "
                f"DialCallSid={dial_call_sid[:16] if dial_call_sid else 'none'} sip={dial_sip_code}")

    # Update number health for browser VoIP calls
    if call_sid and dial_call_status:
        call_info = get_active_call(call_sid) or {}
        nh_location = call_info.get('_location_id', '')
        nh_from = call_info.get('_from_number', '')
        if nh_location and nh_from:
            # Map DialCallStatus to standard Twilio status for health tracking
            status_map = {'completed': 'completed', 'busy': 'busy', 'no-answer': 'no-answer',
                          'failed': 'failed', 'canceled': 'canceled'}
            effective_status = status_map.get(dial_call_status, dial_call_status)
            try:
                update_number_health(nh_location, nh_from, effective_status, int(dial_call_duration or 0), sip_code=dial_sip_code)
            except Exception as e:
                logger.warning(f"Number health update (VoIP dial) failed for {nh_from}: {e}")

    return Response('<?xml version="1.0" encoding="UTF-8"?><Response></Response>', content_type='text/xml')


# ──────────────────────────────────────────────────────────────
# ROUTE: TwiML for outbound calls (called when callee answers)
# ──────────────────────────────────────────────────────────────

@twiml_bp.route('/voice/outbound-twiml', methods=['POST'])
def outbound_twiml():
    """
    TwiML endpoint for outbound calls created via REST API.
    Twilio fetches this when the callee answers. Returns <Connect><Stream> for AI.
    """
    call_sid     = request.values.get('CallSid', '')
    answered_by  = request.values.get('AnsweredBy', '')
    location_id  = request.values.get('location_id', '')
    caller       = request.values.get('caller', '')
    called       = request.values.get('called', '') or request.values.get('To', '')
    direction    = request.values.get('direction', 'outbound')
    contact_id   = request.values.get('contact_id', '')
    contact_name = request.values.get('contact_name', 'there')
    dial_mode    = request.values.get('dial_mode', 'ai')

    logger.info(f"Outbound TwiML: CallSid={call_sid[:16] if call_sid else 'none'} AnsweredBy={answered_by} mode={dial_mode}")

    # Update active calls
    if call_exists(call_sid):
        update_active_call(call_sid, status='in-progress', _host=request.host)

    # Start recording in background (applies to ALL dial modes including human)
    host = request.host
    subscriber = _get_subscriber_by_location(location_id) if location_id else None
    if subscriber:
        vc = subscriber.get('voice_config') or {}
        sub_sid = vc.get('twilio_sub_account_sid', '')
        if vc.get('auto_record', True) and sub_sid and call_sid:
            def _start_rec():
                time.sleep(0.5)
                try:
                    twilio_provisioning.start_recording(sub_sid, call_sid, f'https://{host}')
                except Exception as e:
                    logger.warning(f"Auto-record failed: {e}")
            threading.Thread(target=_start_rec, daemon=True).start()

    # ── Solo Predictive AI Overflow Detection ──
    # For solo_predictive tier with human dial_mode: atomically check if the agent
    # is available. If not, this is a COLLISION — route to AI overflow.
    #
    # RACE CONDITION GUARD: Two calls can answer at the exact same instant.
    # Both hit this endpoint concurrently, both see agent READY, both try to
    # bridge to the human → one gets dropped. Fix: use agent_state_manager's
    # thread-safe set_state as an atomic claim. The FIRST call to successfully
    # transition READY → ON_CALL wins. Any subsequent call sees ON_CALL → overflow.
    _is_overflow = False
    if dial_mode == 'human' and location_id:
        call_info = get_active_call(call_sid) or {}
        agent_email = call_info.get('_agent_email', '')
        _tier = call_info.get('_subscription_tier', '')

        # Fallback: if _agent_email wasn't set (external API call, webhook-initiated),
        # look it up from the subscriber record so the claim can still happen.
        if not agent_email and _tier == 'solo_predictive':
            _sub = _get_subscriber_by_location(location_id)
            if _sub:
                agent_email = _sub.get('email', '')
                if agent_email and call_exists(call_sid):
                    update_active_call(call_sid, _agent_email=agent_email)

        if _tier == 'solo_predictive' and agent_email:
            # ATOMIC claim: try_claim_for_call checks agent state AND sets
            # ON_CALL in a single lock acquisition. This prevents the race
            # condition where two calls answer at the same instant, both see
            # READY, and both try to bridge to the human (dropping one call).
            claimed, state, primary_sid = agent_state_manager.try_claim_for_call(
                location_id, agent_email, call_sid
            )

            if claimed:
                # This call won the claim → bridge to human agent
                logger.info(
                    f"SOLO PREDICTIVE: Agent {agent_email} claimed for "
                    f"{call_sid[:16]} (contact={contact_name})"
                )
            else:
                # Primary agent unavailable — check if any OTHER team member
                # at this location is READY (contingent team awareness).
                # Most locations are solo (no team members), so this is a no-op.
                # But if team members exist and one is READY, bridge to them
                # instead of AI overflow.
                alt_agent = agent_state_manager.get_any_available_agent(
                    location_id, exclude_email=agent_email
                )
                if alt_agent:
                    # Another team member is available — claim them instead
                    alt_claimed, alt_state, _ = agent_state_manager.try_claim_for_call(
                        location_id, alt_agent, call_sid
                    )
                    if alt_claimed:
                        logger.info(
                            f"SOLO PREDICTIVE TEAM FALLBACK: Agent {alt_agent} "
                            f"claimed for {call_sid[:16]} (primary {agent_email} "
                            f"was {state}, contact={contact_name})"
                        )
                        # Bridge to the alternate agent's browser client
                        # (claimed = True means _is_overflow stays False → human bridge)
                    else:
                        # Couldn't claim the alt agent either — overflow to AI
                        _is_overflow = True
                else:
                    # No team members available — overflow to AI
                    _is_overflow = True

                if _is_overflow:
                    logger.info(
                        f"AI OVERFLOW: Agent {agent_email} state={state} — "
                        f"routing {call_sid[:16]} to AI "
                        f"(contact={contact_name}"
                        f"{f', primary={primary_sid[:16]}' if primary_sid else ''})"
                    )
                    # Mark overflow in active_calls for frontend visibility
                    if call_exists(call_sid):
                        update_active_call(
                            call_sid,
                            _overflow=True,
                            _overflow_agent=agent_email,
                            _overflow_primary_sid=primary_sid or '',
                        )

    # For human VoIP mode (NOT overflow), bridge the PSTN callee to the browser agent
    if dial_mode == 'human' and not _is_overflow:
        identity = f"agent_{location_id}" if location_id else ""
        if identity:
            logger.info(f"Human mode outbound: bridging to browser client={identity}")
            twiml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<Response>'
                f'<Dial><Client>{identity}</Client></Dial>'
                '</Response>'
            )
        else:
            logger.warning(f"Human mode outbound: no location_id, cannot bridge to browser")
            twiml = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        return Response(twiml, content_type='text/xml')

    # AI mode (default) or AI overflow — connect to xAI Realtime WebSocket
    stream_url = os.getenv('VOICE_WSS_URL') or f'wss://{host}/voice/stream'

    client_state = _encode_client_state({
        'location_id':  location_id,
        'caller':       caller,
        'called':       called,
        'direction':    direction,
        'contact_id':   contact_id,
        'contact_name': contact_name,
        'dial_mode':    'ai_overflow' if _is_overflow else dial_mode,
    })

    params = {'client_state': client_state, 'callSid': call_sid}
    twiml = _build_twiml_stream(stream_url, params)
    return Response(twiml, content_type='text/xml')


@twiml_bp.route('/voice/intercept-twiml', methods=['POST'])
def intercept_twiml():
    """TwiML endpoint for agent VoIP intercept. Dials the browser client."""
    identity = request.values.get('identity', '')
    call_sid = request.values.get('CallSid', '')

    logger.info(f"Intercept TwiML: CallSid={call_sid[:16] if call_sid else 'none'} -> client:{identity}")

    if not identity:
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Intercept failed.</Say></Response>',
            content_type='text/xml'
        )

    host = request.host
    action_url = f"https://{host}/voice/transfer-complete?original_sid={call_sid}"

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Dial action={xml_quoteattr(action_url)} method="POST"><Client>{xml_escape(identity)}</Client></Dial>'
        '</Response>'
    )
    return Response(twiml, content_type='text/xml')


@twiml_bp.route('/voice/transfer-twiml', methods=['POST'])
def transfer_twiml():
    """TwiML endpoint for call transfers. Twilio fetches this when we redirect a call."""
    transfer_to = request.values.get('transfer_to', '')
    call_sid = request.values.get('CallSid', '')

    logger.info(f"Transfer TwiML: CallSid={call_sid[:16] if call_sid else 'none'} -> {transfer_to}")

    if not transfer_to:
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Transfer failed.</Say></Response>',
            content_type='text/xml'
        )

    # action URL tells Twilio to POST here when the <Dial> leg ends,
    # so we can hang up the parent call cleanly instead of leaving it open.
    host = request.host
    action_url = f"https://{host}/voice/transfer-complete?original_sid={call_sid}"

    from xml.sax.saxutils import escape as xml_escape
    safe_number = xml_escape(transfer_to)
    safe_action = xml_escape(action_url)

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Dial action="{safe_action}" method="POST">{safe_number}</Dial>'
        '</Response>'
    )
    return Response(twiml, content_type='text/xml')


@twiml_bp.route('/voice/transfer-complete', methods=['POST'])
def transfer_complete():
    """
    Twilio calls this when the <Dial> leg of a transfer ends (callee hangs up,
    busy, no-answer, etc.).  We return <Hangup/> so the parent call is released
    instead of lingering in 'in-progress' forever.
    """
    original_sid = request.values.get('original_sid', '') or request.values.get('CallSid', '')
    dial_status = request.values.get('DialCallStatus', 'unknown')
    logger.info(f"Transfer complete: original_sid={original_sid[:16] if original_sid else 'none'} dial_status={dial_status}")

    # Clean up call state so the dialer UI can move on
    if original_sid and call_exists(original_sid):
        update_active_call(original_sid, status='completed')
    delete_transfer_request(original_sid)

    return Response(
        '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
        content_type='text/xml'
    )


@twiml_bp.route('/voice/amd-status', methods=['POST'])
def amd_status_callback():
    """
    Twilio async AMD callback. Called when machine detection finishes.
    NOTE: This fires LATE (after full voicemail greeting). Our software
    voicemail detection in the bridge is much faster. This is a safety net.
    - machine_end_beep / machine_end_silence / machine_end_other: hang up immediately
    - machine_start / fax: hang up immediately
    - human / not_sure: call continues with existing stream
    """
    call_sid    = request.values.get('CallSid', '')
    answered_by = request.values.get('AnsweredBy', '')

    logger.info(f"AMD result: CallSid={call_sid[:16] if call_sid else 'none'} AnsweredBy={answered_by}")

    call_info = get_active_call(call_sid) or {}
    sub_sid_amd = call_info.get('_sub_sid', '')

    # Cases where we can leave a voicemail (beep has passed)
    voicemail_opportunity = {'machine_end_beep', 'machine_end_silence', 'machine_end_other'}
    # Cases where there's no recording opportunity
    immediate_hangup = {'machine_start', 'fax'}

    all_machine = voicemail_opportunity | immediate_hangup

    if answered_by in all_machine and sub_sid_amd and call_sid:
        # Machine detected — hang up immediately and let the dialer retry
        # Mark FIRST so /voice/status preserves 'no-answer' even if it arrives before hangup completes
        if call_exists(call_sid):
            update_active_call(call_sid, _amd_result='no-answer')
        try:
            _twilio_hangup(call_sid, sub_sid_amd)
        except Exception as e:
            logger.warning(f"AMD hangup failed for {call_sid}: {e}")
        # Clean up any pending transfer request for this call
        delete_transfer_request(call_sid)

    # human or not_sure — call continues with existing media stream
    return '', 204
