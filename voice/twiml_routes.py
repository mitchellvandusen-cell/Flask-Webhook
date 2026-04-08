import logging
import os
import threading
import time
from xml.sax.saxutils import escape as xml_escape, quoteattr as xml_quoteattr

from flask import Blueprint, request, Response

import twilio_provisioning
from number_health import select_outbound_number, update_number_health
from voice.call_state import (
    set_active_call, get_active_call, update_active_call, delete_active_call, call_exists,
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
                time.sleep(0.2)
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
            time.sleep(0.2)
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

    # ── Conversion analytics: track call connection ──
    if location_id and contact_id:
        try:
            from db import log_conversion_event
            log_conversion_event(location_id, contact_id, 'call_connected',
                                 {'dial_mode': dial_mode, 'answered_by': answered_by},
                                 source='voice')
        except Exception:
            pass

    # Start recording in background (applies to ALL dial modes including human)
    host = request.host
    subscriber = _get_subscriber_by_location(location_id) if location_id else None
    if subscriber:
        vc = subscriber.get('voice_config') or {}
        sub_sid = vc.get('twilio_sub_account_sid', '')
        if vc.get('auto_record', True) and sub_sid and call_sid:
            def _start_rec():
                time.sleep(0.2)
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

    # Clean up call state so the dialer UI can move on.
    # Delete the call entirely — keeping it with status='completed' causes it to
    # linger in Redis for up to 1hr, inflating active-call counts and blocking new dials.
    if original_sid and call_exists(original_sid):
        delete_active_call(original_sid)
    delete_transfer_request(original_sid)

    return Response(
        '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
        content_type='text/xml'
    )


# ──────────────────────────────────────────────────────────────
# WARM TRANSFER TwiML ENDPOINTS
# ──────────────────────────────────────────────────────────────

@twiml_bp.route('/voice/warm-transfer/conference-twiml', methods=['POST'])
def warm_transfer_conference_twiml():
    """
    TwiML for the CALLER leg: joins the Conference room with hold music.
    Twilio fetches this when we redirect the caller's call.
    """
    conf_name = request.values.get('conf_name', '')
    call_sid = request.values.get('CallSid', '')
    host = request.host

    logger.info(f"Warm transfer conference TwiML: CallSid={call_sid[:16] if call_sid else 'none'} conf={conf_name}")

    if not conf_name:
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Transfer failed.</Say></Response>',
            content_type='text/xml'
        )

    safe_conf = xml_escape(conf_name)
    hold_url = xml_escape(f"https://{host}/voice/warm-transfer/hold-music")
    status_cb = xml_escape(f"https://{host}/voice/warm-transfer/conference-status?conf_name={conf_name}")

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        '<Dial>'
        f'<Conference beep="false" startConferenceOnEnter="true" '
        f'endConferenceOnExit="false" waitUrl="{hold_url}" '
        f'statusCallback="{status_cb}" statusCallbackEvent="join leave end">'
        f'{safe_conf}'
        '</Conference>'
        '</Dial>'
        '</Response>'
    )
    return Response(twiml, content_type='text/xml')


@twiml_bp.route('/voice/warm-transfer/target-twiml', methods=['POST'])
def warm_transfer_target_twiml():
    """
    TwiML for the TRANSFER TARGET leg: joins the same Conference.
    endConferenceOnExit=true so conference ends when target hangs up
    (after agent has dropped off, leaving only caller + target).
    """
    conf_name = request.values.get('conf_name', '')
    call_sid = request.values.get('CallSid', '')

    logger.info(f"Warm transfer target TwiML: CallSid={call_sid[:16] if call_sid else 'none'} conf={conf_name}")

    if not conf_name:
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Transfer failed.</Say></Response>',
            content_type='text/xml'
        )

    safe_conf = xml_escape(conf_name)

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        '<Dial>'
        f'<Conference beep="false" startConferenceOnEnter="true" '
        f'endConferenceOnExit="true">'
        f'{safe_conf}'
        '</Conference>'
        '</Dial>'
        '</Response>'
    )
    return Response(twiml, content_type='text/xml')


@twiml_bp.route('/voice/warm-transfer/hold-music', methods=['POST', 'GET'])
def warm_transfer_hold_music():
    """Hold music TwiML for Conference waitUrl."""
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        '<Play loop="0">http://com.twilio.music.classical.s3.amazonaws.com/BusssyBoy_-_Its_Only.mp3</Play>'
        '</Response>'
    )
    return Response(twiml, content_type='text/xml')


@twiml_bp.route('/voice/warm-transfer/agent-twiml', methods=['POST'])
def warm_transfer_agent_twiml():
    """
    TwiML for the AGENT leg: joins the Conference for consultative transfer.
    endConferenceOnExit=false so conference continues when agent drops off.
    """
    conf_name = request.values.get('conf_name', '')
    call_sid = request.values.get('CallSid', '')

    logger.info(f"Warm transfer agent TwiML: CallSid={call_sid[:16] if call_sid else 'none'} conf={conf_name}")

    if not conf_name:
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Transfer failed.</Say></Response>',
            content_type='text/xml'
        )

    safe_conf = xml_escape(conf_name)

    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        '<Dial>'
        f'<Conference beep="false" startConferenceOnEnter="true" '
        f'endConferenceOnExit="false">'
        f'{safe_conf}'
        '</Conference>'
        '</Dial>'
        '</Response>'
    )
    return Response(twiml, content_type='text/xml')


@twiml_bp.route('/voice/warm-transfer/conference-status', methods=['POST'])
def warm_transfer_conference_status():
    """Status callback for warm transfer Conference events."""
    conf_name = request.values.get('conf_name', '') or request.values.get('FriendlyName', '')
    event = request.values.get('StatusCallbackEvent', '')
    call_sid = request.values.get('CallSid', '')

    logger.info(f"Warm transfer conf status: conf={conf_name} event={event} call={call_sid[:16] if call_sid else 'none'}")

    return Response('', status=200)


@twiml_bp.route('/voice/warm-transfer/reconnect-twiml', methods=['POST'])
def warm_transfer_reconnect_twiml():
    """
    TwiML to reconnect the caller back to the agent after a cancelled warm transfer.
    Dials the agent's browser client directly.
    """
    identity = request.values.get('identity', '')
    call_sid = request.values.get('CallSid', '')

    logger.info(f"Warm transfer reconnect TwiML: CallSid={call_sid[:16] if call_sid else 'none'} -> client:{identity}")

    if not identity:
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
            content_type='text/xml'
        )

    safe_identity = xml_escape(identity)
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Dial><Client>{safe_identity}</Client></Dial>'
        '</Response>'
    )
    return Response(twiml, content_type='text/xml')


# States that mean the call resolved on its own — screener thread should not hang up.
# 'in-progress' means a human connected (possibly through the screener), so we stop too.
_SCREENER_TERMINAL = frozenset({
    'in-progress', 'completed', 'busy', 'no-answer',
    'failed', 'canceled', 'transferred', 'voicemail-dropped',
})


def _screener_timeout_hangup(call_sid: str, sub_sid: str) -> None:
    """
    Background daemon thread started when AMD fires 'machine_start' (spam screener detected).
    Waits 28 s → sets _screener_warning so the dialer UI can notify the agent.
    Waits 2 s more → hangs up if the call is still active, marks no-answer for retry.
    If the call resolves on its own (human connects, ring timeout, etc.) before 30 s,
    the terminal-status check aborts the thread cleanly.
    """
    time.sleep(28)
    if not call_exists(call_sid):
        return
    info = get_active_call(call_sid) or {}
    if info.get('status') in _SCREENER_TERMINAL:
        return  # resolved naturally — nothing to do
    # Warn the agent: dialer UI polls this field and shows a toast
    update_active_call(call_sid, _screener_warning=True)
    logger.info(f"Screener timeout warning set for {call_sid[:16]}")

    time.sleep(2)
    if not call_exists(call_sid):
        return
    info = get_active_call(call_sid) or {}
    if info.get('status') in _SCREENER_TERMINAL:
        return
    # 30 s elapsed — hang up and let the dialer retry
    logger.info(f"Screener timeout: hanging up {call_sid[:16]} after 30 s")
    update_active_call(call_sid, _amd_result='no-answer')
    try:
        _twilio_hangup(call_sid, sub_sid)
    except Exception as e:
        logger.warning(f"Screener timeout hangup failed for {call_sid[:16]}: {e}")
    delete_transfer_request(call_sid)


def _drop_voicemail(call_sid: str, sub_sid: str, location_id: str, call_info: dict) -> bool:
    """
    Play the subscriber's saved voicemail greeting on a live call then hang up.
    Uses inline TwiML (consistent with manual voicemail drop in call_history.py).
    Falls back to immediate hangup if the drop fails.
    Returns True if greeting was played, False if fell back to hangup.
    """
    import json
    from xml.sax.saxutils import escape as xml_escape
    from itsdangerous import URLSafeTimedSerializer
    try:
        secret = os.getenv('SESSION_SECRET') or os.getenv('SECRET_KEY') or 'dev-insecure'
        serializer = URLSafeTimedSerializer(secret, salt='voicemail-drop-v1')
        token = serializer.dumps(location_id)
        host = call_info.get('_host', '')
        domain = f"https://{host}" if host else os.getenv('YOUR_DOMAIN', '').rstrip('/')
        audio_url = f"{domain}/voice/voicemail-greeting/public/{token}"
        twiml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Response>'
            f'<Play>{xml_escape(audio_url)}</Play>'
            '<Hangup/>'
            '</Response>'
        )
        client = twilio_provisioning.get_sub_account_client(sub_sid)
        client.calls(call_sid).update(twiml=twiml)
        update_active_call(call_sid, _amd_result='voicemail-dropped')
        logger.info(f"Voicemail drop inline TwiML fired for {call_sid[:16]}")
        return True
    except Exception as e:
        logger.warning(f"Voicemail drop failed for {call_sid[:16]}: {e} — falling back to hangup")
        if call_exists(call_sid):
            update_active_call(call_sid, _amd_result='no-answer')
        try:
            _twilio_hangup(call_sid, sub_sid)
        except Exception:
            pass
        delete_transfer_request(call_sid)
        return False


@twiml_bp.route('/voice/amd-status', methods=['POST'])
def amd_status_callback():
    """
    Twilio async AMD callback. Called when machine detection finishes.

    AnsweredBy values and what we do:
    - machine_end_beep / machine_end_silence / machine_end_other:
        TRUE voicemail — greeting fully played.  Check on_machine_action:
        'voicemail_drop' → redirect call to play saved greeting then hang up.
        'hangup' (default) → hang up immediately, mark no-answer for retry.
    - fax:
        Not a voicemail — hang up immediately.
    - machine_start:
        Twilio detected automation early but message is still playing.
        Could be a spam screener (Google Call Screen, Nomorobo, carrier IVR)
        where the HUMAN is still on the line waiting to connect.
        DO NOT hang up — let the call continue. Ring timeout handles cleanup.
    - human / not_sure:
        Human answered or ambiguous — call continues with existing media stream.
    """
    call_sid    = request.values.get('CallSid', '')
    answered_by = request.values.get('AnsweredBy', '')

    logger.info(f"AMD result: CallSid={call_sid[:16] if call_sid else 'none'} AnsweredBy={answered_by}")

    if not call_sid:
        return '', 204

    call_info   = get_active_call(call_sid) or {}
    sub_sid_amd = call_info.get('_sub_sid', '')
    location_id = call_info.get('_location_id', '')

    # TRUE voicemail: greeting has fully played, we heard a beep or end-of-message
    voicemail_complete = {'machine_end_beep', 'machine_end_silence', 'machine_end_other'}

    if answered_by in voicemail_complete:
        if not sub_sid_amd:
            return '', 204
        # Look up on_machine_action, max attempts, and greeting data for this subscriber
        action = 'hangup'
        has_greeting = False
        max_attempts = 2  # default matches voice_config default
        if location_id:
            try:
                import json
                from db import get_db_connection, return_db_connection
                conn = get_db_connection()
                if conn:
                    try:
                        cur = conn.cursor()
                        cur.execute(
                            "SELECT voice_config FROM subscribers WHERE location_id = %s",
                            (location_id,)
                        )
                        row = cur.fetchone()
                        cur.close()
                        if row:
                            vc = row.get('voice_config') or {}
                            if isinstance(vc, str):
                                vc = json.loads(vc)
                            action = vc.get('on_machine_action', 'hangup')
                            has_greeting = bool(vc.get('voicemail_greeting_data'))
                            try:
                                max_attempts = int(vc.get('dial_attempts', 2))
                            except (ValueError, TypeError):
                                max_attempts = 2
                    finally:
                        return_db_connection(conn)
            except Exception as e:
                logger.warning(f"AMD: failed to fetch voice_config for {location_id}: {e}")

        # Voicemail drop only fires on the FINAL attempt so the dialer exhausts
        # all configured retries before leaving a message.
        # Attempts 1..N-1 → hang up and let the dialer retry.
        # Attempt N (final) → drop the greeting.
        this_attempt = int(call_info.get('attempt', 1))
        is_final_attempt = (this_attempt >= max_attempts)

        if action == 'voicemail_drop' and has_greeting and is_final_attempt and call_exists(call_sid):
            _drop_voicemail(call_sid, sub_sid_amd, location_id, call_info)
        else:
            # hangup: covers (a) action=hangup, (b) no greeting saved,
            # (c) voicemail_drop but not yet the final attempt — retry coming
            if call_exists(call_sid):
                update_active_call(call_sid, _amd_result='no-answer')
            try:
                _twilio_hangup(call_sid, sub_sid_amd)
            except Exception as e:
                logger.warning(f"AMD hangup failed for {call_sid}: {e}")
            delete_transfer_request(call_sid)

    elif answered_by == 'fax':
        # Fax line — hang up immediately, no voicemail opportunity
        if sub_sid_amd and call_exists(call_sid):
            update_active_call(call_sid, _amd_result='no-answer')
            try:
                _twilio_hangup(call_sid, sub_sid_amd)
            except Exception as e:
                logger.warning(f"AMD fax hangup failed for {call_sid}: {e}")
            delete_transfer_request(call_sid)

    elif answered_by == 'machine_start':
        # Possible spam screener (Google Call Screen, Nomorobo, carrier IVR).
        # Don't hang up — a human may still connect.
        #
        # FINAL ATTEMPT + VOICEMAIL DROP exception:
        # If this is the last configured attempt AND the subscriber has voicemail_drop
        # enabled with a greeting saved, skip the 30-second screener thread entirely.
        # The screener thread would hang up at 30s — BEFORE AMD fires machine_end_beep
        # (~T+10s after the beep) — which would prevent the greeting from ever playing.
        # Instead, let the call continue; ring timeout cleans up if no voicemail,
        # and AMD will fire machine_end_beep naturally if voicemail answers.
        if sub_sid_amd:
            _skip_screener = False
            if location_id:
                try:
                    import json as _json
                    from db import get_db_connection as _gdc, return_db_connection as _rdc
                    _conn = _gdc()
                    if _conn:
                        try:
                            _cur = _conn.cursor()
                            _cur.execute(
                                "SELECT voice_config FROM subscribers WHERE location_id = %s",
                                (location_id,)
                            )
                            _row = _cur.fetchone()
                            _cur.close()
                            if _row:
                                _vc = _row.get('voice_config') or {}
                                if isinstance(_vc, str):
                                    _vc = _json.loads(_vc)
                                _action = _vc.get('on_machine_action', 'hangup')
                                _has_greeting = bool(_vc.get('voicemail_greeting_data'))
                                try:
                                    _max_att = int(_vc.get('dial_attempts', 2))
                                except (ValueError, TypeError):
                                    _max_att = 2
                                _this_att = int(call_info.get('attempt', 1))
                                if (_action == 'voicemail_drop' and _has_greeting
                                        and _this_att >= _max_att):
                                    _skip_screener = True
                                    logger.info(
                                        f"AMD machine_start: skipping screener thread on "
                                        f"final attempt {_this_att}/{_max_att} — "
                                        f"voicemail_drop active, letting AMD handle {call_sid[:16]}"
                                    )
                        finally:
                            _rdc(_conn)
                except Exception as _e:
                    logger.warning(f"AMD machine_start: failed to check voice_config for {location_id}: {_e}")

            if not _skip_screener:
                t = threading.Thread(
                    target=_screener_timeout_hangup,
                    args=(call_sid, sub_sid_amd),
                    daemon=True,
                    name=f"screener-{call_sid[:8]}",
                )
                t.start()

    # human / not_sure → human answered, AI bridge handles it
    return '', 204
