"""Module extracted from twilio_provisioning.py."""

def generate_voice_token(identity: str, twiml_app_sid: str,
                          sub_account_sid: str = "",
                          api_key_sid: str = "",
                          api_key_secret: str = "") -> str:
    """
    Generate a Twilio Access Token with Voice grant for browser calling.
    Uses per-subscriber API Key if provided, otherwise falls back to env vars.
    The API key MUST belong to the same account as account_sid.
    """
    # Use per-subscriber API key if available, otherwise fall back to env vars
    key_sid = api_key_sid or TWILIO_API_KEY_SID
    key_secret = api_key_secret or TWILIO_API_KEY_SECRET

    if not key_sid or not key_secret:
        raise ValueError("No API key available — set TWILIO_API_KEY_SID/SECRET or provision per-subscriber keys")

    account_sid = sub_account_sid or TWILIO_ACCOUNT_SID

    logger.info(f"[generate_voice_token] account_sid={account_sid} api_key={key_sid} identity={identity} twiml_app={twiml_app_sid}")

    token = AccessToken(
        account_sid,
        key_sid,
        key_secret,
        identity=identity,
        ttl=3600,  # 1 hour
    )

    voice_grant = VoiceGrant(
        outgoing_application_sid=twiml_app_sid,
        incoming_allow=True,
    )
    token.add_grant(voice_grant)

    jwt_token = token.to_jwt()
    # Some SDK versions return bytes — ensure we return a string
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode('utf-8')
    return jwt_token


# ──────────────────────────────────────────────────────────────
# CALL MANAGEMENT — Twilio REST API
# ──────────────────────────────────────────────────────────────

def create_outbound_call(sub_account_sid: str, to: str, from_number: str,
                          webhook_base_url: str, status_callback_events: list = None,
                          machine_detection: str = None,
                          custom_params: dict = None,
                          ring_timeout: int = None) -> dict:
    """
    Create an outbound call via Twilio REST API.
    Returns call details including call_sid.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        kwargs = {
            "to": to,
            "from_": from_number,
            "url": f"{webhook_base_url}/voice/outbound-twiml",
            "method": "POST",
            "status_callback": f"{webhook_base_url}/voice/status",
            "status_callback_method": "POST",
            "status_callback_event": status_callback_events or [
                "initiated", "ringing", "answered", "completed",
            ],
            "record": False,  # We handle recording separately
        }

        # Configurable ring timeout (Twilio enforces server-side)
        if ring_timeout and ring_timeout > 0:
            kwargs["timeout"] = min(max(ring_timeout, 15), 120)  # Twilio range: 15-600, sane max 120

        if machine_detection:
            kwargs["machine_detection"] = machine_detection
            kwargs["machine_detection_timeout"] = 8
            kwargs["machine_detection_speech_threshold"] = 2400
            kwargs["machine_detection_speech_end_threshold"] = 1200
            kwargs["machine_detection_silence_timeout"] = 5000
            kwargs["async_amd"] = True
            kwargs["async_amd_status_callback"] = f"{webhook_base_url}/voice/amd-status"
            kwargs["async_amd_status_callback_method"] = "POST"

        # Pass custom params as URL params so they arrive in the TwiML webhook
        if custom_params:
            url_params = "&".join(f"{k}={quote(str(v))}" for k, v in custom_params.items())
            kwargs["url"] = f"{webhook_base_url}/voice/outbound-twiml?{url_params}"

        call = client.calls.create(**kwargs)
        logger.info(f"Outbound call created: {call.sid} to {to} from {from_number}")
        return {
            "call_sid": call.sid,
            "status": call.status,
            "to": to,
            "from": from_number,
        }
    except TwilioRestException as e:
        logger.error(f"Failed to create outbound call: {e}")
        raise


def hangup_call(sub_account_sid: str, call_sid: str) -> bool:
    """Hang up an active call."""
    client = get_sub_account_client(sub_account_sid)
    try:
        client.calls(call_sid).update(status="completed")
        logger.info(f"Hung up call: {call_sid}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to hang up call {call_sid}: {e}")
        return False


def transfer_call(sub_account_sid: str, call_sid: str,
                   transfer_to: str, webhook_base_url: str) -> bool:
    """
    Transfer a live call by updating the call's URL to a transfer TwiML.
    Twilio will fetch the new TwiML and execute the <Dial> to the target.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        client.calls(call_sid).update(
            url=f"{webhook_base_url}/voice/transfer-twiml?transfer_to={quote(transfer_to, safe='')}",
            method="POST",
        )
        logger.info(f"Transfer initiated: {call_sid} -> {transfer_to}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to transfer call {call_sid}: {e}")
        return False


def start_recording(sub_account_sid: str, call_sid: str,
                     webhook_base_url: str) -> str:
    """Start recording a call. Returns recording SID."""
    client = get_sub_account_client(sub_account_sid)
    try:
        recording = client.calls(call_sid).recordings.create(
            recording_channels="dual",
            recording_status_callback=f"{webhook_base_url}/voice/recording-status",
            recording_status_callback_method="POST",
        )
        logger.info(f"Recording started: {recording.sid} for call {call_sid}")
        return recording.sid
    except TwilioRestException as e:
        logger.error(f"Failed to start recording for {call_sid}: {e}")
        return ""


def get_recording_url(sub_account_sid: str, recording_sid: str) -> str:
    """Get the MP3 download URL for a recording."""
    return f"https://api.twilio.com/2010-04-01/Accounts/{sub_account_sid}/Recordings/{recording_sid}.mp3"


# ──────────────────────────────────────────────────────────────
# CONFERENCE PARTICIPANT MANAGEMENT (Warm Transfer)
# ──────────────────────────────────────────────────────────────

def add_conference_participant(sub_account_sid: str, conference_name: str,
                                to: str, from_number: str,
                                twiml_url: str,
                                status_callback: str = None) -> dict:
    """
    Add a participant to a Conference by creating an outbound call.
    The call's TwiML should join the same Conference room.
    Returns {call_sid, status} or raises on failure.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        kwargs = {
            "to": to,
            "from_": from_number,
            "url": twiml_url,
            "method": "POST",
            "status_callback_event": ["initiated", "ringing", "answered", "completed"],
            "timeout": 30,
        }
        if status_callback:
            kwargs["status_callback"] = status_callback
            kwargs["status_callback_method"] = "POST"

        call = client.calls.create(**kwargs)
        logger.info(f"Conference participant added: {call.sid} -> {to} (conf={conference_name})")
        return {"call_sid": call.sid, "status": call.status}
    except TwilioRestException as e:
        logger.error(f"Failed to add conference participant {to}: {e}")
        raise


def remove_conference_participant(sub_account_sid: str, conference_name: str,
                                   call_sid: str) -> bool:
    """
    Remove a participant from a Conference by hanging up their call leg.
    """
    client = get_sub_account_client(sub_account_sid)
    try:
        # Find the conference by friendly name
        conferences = client.conferences.list(
            friendly_name=conference_name, status="in-progress", limit=1
        )
        if not conferences:
            logger.warning(f"Conference '{conference_name}' not found or not in-progress")
            return False

        conf_sid = conferences[0].sid
        participants = client.conferences(conf_sid).participants.list()
        for p in participants:
            if p.call_sid == call_sid:
                p.update(status="completed")
                logger.info(f"Removed participant {call_sid} from conference {conference_name}")
                return True

        logger.warning(f"Participant {call_sid} not found in conference {conference_name}")
        return False
    except TwilioRestException as e:
        logger.error(f"Failed to remove conference participant {call_sid}: {e}")
        return False


def redirect_call_to_twiml(sub_account_sid: str, call_sid: str,
                            twiml_url: str) -> bool:
    """Redirect a live call to a new TwiML URL."""
    client = get_sub_account_client(sub_account_sid)
    try:
        client.calls(call_sid).update(url=twiml_url, method="POST")
        logger.info(f"Redirected call {call_sid} to {twiml_url}")
        return True
    except TwilioRestException as e:
        logger.error(f"Failed to redirect call {call_sid}: {e}")
        return False



# ──────────────────────────────────────────────────────────────
# VOICE INSIGHTS ADVANCED FEATURES
# ──────────────────────────────────────────────────────────────

def enable_voice_insights_advanced(sub_account_sid: str) -> bool:
    """
    Enable Voice Insights Advanced Features on a sub-account.

    Uses the master account credentials with SubaccountSid parameter,
    as documented at:
    https://www.twilio.com/docs/voice/voice-insights/api/call/voice-insights-settings-resource

    Advanced Features ($0.0025/min) unlock:
    - Call Summary API (PDD, SIP codes, carrier info, quality tags)
    - Call Events API (SIP signaling timeline)
    - Call Metrics API (jitter, packet loss, MOS time-series)
    - Event Streams integration
    """
    client = get_master_client()
    try:
        settings = client.insights.v1.settings().update(
            advanced_features=True,
            subaccount_sid=sub_account_sid,
        )
        logger.info(f"Voice Insights Advanced enabled for {sub_account_sid}: advanced={settings.advanced_features}")
        return True
    except Exception as e:
        logger.error(f"Failed to enable Voice Insights Advanced for {sub_account_sid}: {e}")
        return False


def get_voice_insights_settings(sub_account_sid: str = None) -> dict:
    """Check Voice Insights settings for an account."""
    client = get_master_client()
    try:
        kwargs = {}
        if sub_account_sid:
            kwargs['subaccount_sid'] = sub_account_sid
        settings = client.insights.v1.settings().fetch(**kwargs)
        return {
            "advanced_features": settings.advanced_features,
            "voice_trace": settings.voice_trace,
        }
    except Exception as e:
        logger.warning(f"Failed to fetch Voice Insights settings: {e}")
        return {"advanced_features": False, "voice_trace": False}


def fetch_call_insights_summary(call_sid: str, sub_account_sid: str = None,
                                 sub_account_auth_token: str = None) -> dict:
    """
    Fetch the Voice Insights Call Summary for a completed call.

    Returns the full summary including:
    - properties.pdd_ms (post-dial delay)
    - properties.last_sip_response_num
    - carrier_edge metrics (jitter, packet loss)
    - call_state, call_type, tags
    - from/to carrier info
    - trust data (branded calling, verified caller)

    API: GET https://insights.twilio.com/v1/Voice/{CallSid}/Summary

    The summary is partial within ~10 min of call end, complete within ~30 min.
    """
    if sub_account_sid and sub_account_auth_token:
        client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    elif sub_account_sid:
        client = get_sub_account_client(sub_account_sid)
    else:
        client = get_master_client()

    try:
        summary = client.insights.v1.calls(call_sid).summary().fetch()

        # Extract the key fields from the summary object
        result = {
            "call_sid": summary.call_sid,
            "call_type": summary.call_type,
            "call_state": summary.call_state,
            "processing_state": summary.processing_state,
            "duration": summary.duration,
            "connect_duration": summary.connect_duration,
            "start_time": str(summary.start_time) if summary.start_time else None,
            "end_time": str(summary.end_time) if summary.end_time else None,
            "tags": summary.tags or [],
            "attributes": summary.attributes or {},
            "properties": summary.properties or {},
            "carrier_edge": summary.carrier_edge or {},
            "client_edge": summary.client_edge or {},
            "sdk_edge": summary.sdk_edge or {},
            "sip_edge": summary.sip_edge or {},
            "trust": getattr(summary, 'trust', None) or {},
            "from_info": getattr(summary, 'from_', None) or {},
            "to_info": getattr(summary, 'to', None) or {},
            "annotation": summary.annotation or {},
        }

        # Extract commonly-accessed fields for quick lookups
        props = result["properties"] or {}
        result["_pdd_ms"] = props.get("pdd_ms")
        result["_last_sip_response"] = props.get("last_sip_response_num")
        result["_disconnected_by"] = props.get("disconnected_by")

        return result
    except Exception as e:
        logger.warning(f"Failed to fetch call insights for {call_sid}: {e}")
        return {}


def fetch_call_insights_events(call_sid: str, sub_account_sid: str = None,
                                sub_account_auth_token: str = None,
                                edge: str = None) -> list:
    """
    Fetch Call Insights Events (SIP signaling timeline) for a call.

    API: GET https://insights.twilio.com/v1/Voice/{CallSid}/Events

    Returns list of events with: edge, group, level, name, timestamp.
    Optional edge filter: carrier_edge, sip_edge, sdk_edge, client_edge.
    """
    if sub_account_sid and sub_account_auth_token:
        client = get_sub_account_client_native(sub_account_sid, sub_account_auth_token)
    elif sub_account_sid:
        client = get_sub_account_client(sub_account_sid)
    else:
        client = get_master_client()

    try:
        kwargs = {}
        if edge:
            kwargs['edge'] = edge
        events = client.insights.v1.calls(call_sid).events.list(**kwargs)
        return [
            {
                "edge": e.edge,
                "group": e.group,
                "level": e.level,
                "name": e.name,
                "timestamp": str(e.timestamp) if e.timestamp else None,
            }
            for e in events
        ]
    except Exception as e:
        logger.warning(f"Failed to fetch call events for {call_sid}: {e}")
        return []


