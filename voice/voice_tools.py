import json
import logging
import time

from db import log_webhook_event
from ghl_calendar import consolidated_calendar_op
from voice.call_state import active_calls, transfer_requests, overflow_transfer_alerts
from voice.predictive_engine import agent_state_manager, AgentState

logger = logging.getLogger("voice_bridge.voice_tools")


def get_voice_tools():
    """Return tool definitions for the XAI Realtime session."""
    return [
        {
            "type": "function",
            "name": "check_calendar_availability",
            "description": "Check available appointment time slots for the next few days. Call this when the lead asks about availability or when you want to offer appointment times.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief reason for checking, e.g. 'lead asked about availability'"
                    }
                },
                "required": []
            }
        },
        {
            "type": "function",
            "name": "book_appointment",
            "description": "Book a confirmed appointment at a specific date and time. Only call this after the lead has explicitly agreed to a time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selected_time": {
                        "type": "string",
                        "description": "The agreed-upon time, e.g. 'Tuesday at 2:30 PM', '4pm tomorrow'"
                    }
                },
                "required": ["selected_time"]
            }
        },
        {
            "type": "function",
            "name": "transfer_to_agent",
            "description": "Transfer this call to a live human agent right now. Use this when the lead is showing strong buying signals — asking about pricing, requesting quotes, wanting to start the process, saying 'sign me up' or 'let's do this', having their policy info ready, or expressing urgency about getting coverage today. This is your PRIMARY closing tool for hot leads. Do NOT book an appointment when someone is ready to buy NOW — transfer them instead. Naturally tell the caller you are connecting them with a senior advisor before calling this tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief reason for the transfer, e.g. 'lead ready to buy', 'needs underwriting details', 'requesting live agent'"
                    }
                },
                "required": []
            }
        },
        {
            "type": "function",
            "name": "end_call",
            "description": "End (hang up) this phone call. Use this ONLY when: (1) the lead explicitly says goodbye and the conversation is clearly over, (2) the lead asks to be removed from the call list, (3) the lead is abusive or the call has no productive path forward. Say a brief, natural closing line first — then call this tool to hang up.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief reason for ending the call, e.g. 'lead said goodbye', 'lead requested removal', 'no productive path'"
                    }
                },
                "required": []
            }
        },
    ]


# ──────────────────────────────────────────────────────────────
# TOOL EXECUTION
# ──────────────────────────────────────────────────────────────

def execute_voice_tool(tool_name, arguments, subscriber, contact_id=None, first_name=None):
    """Execute a tool call from the voice agent and return the result string."""
    logger.info(f"Voice Tool Call: {tool_name} | args={arguments}")

    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        args = {}

    if tool_name == "check_calendar_availability":
        try:
            slots = consolidated_calendar_op(
                operation="fetch_slots",
                subscriber_data=subscriber,
            )
            if slots and "CALENDAR_UNAVAILABLE" not in slots:
                logger.info(f"Voice: Calendar slots fetched: {slots[:100]}")
                return f"Available appointment slots: {slots}"
            else:
                return "You don't have your schedule pulled up right now. Ask the lead what day and time work best for them. Morning or afternoon? Get their preference so you can lock it in."
        except Exception as e:
            logger.error(f"Voice calendar check failed: {e}")
            return "You don't have your schedule in front of you right now. Ask the lead what day and time work best for them and you will get them booked."

    elif tool_name == "book_appointment":
        selected_time = args.get("selected_time", "")
        if not selected_time:
            return "No time was specified. Ask the lead to confirm the time they'd like."

        try:
            success = consolidated_calendar_op(
                operation="book",
                subscriber_data=subscriber,
                contact_id=contact_id,
                first_name=first_name or "Lead",
                selected_time=selected_time,
            )
            if success:
                logger.info(f"Voice: Appointment booked for {selected_time}")
                # Log the booking event
                try:
                    log_webhook_event(
                        location_id=subscriber.get("location_id"),
                        contact_id=contact_id,
                        event_type="voice_booking",
                        status="success",
                        summary=f"Voice call booking: {selected_time}",
                        details={"time": selected_time, "first_name": first_name}
                    )
                except Exception:
                    pass
                booked_display = success if isinstance(success, str) else selected_time
                return f"You just got them on the calendar for {booked_display}. Confirm the time and ask if they got the invite in their email."
            else:
                return f"That time just got taken. Let the lead know that slot filled up and ask what other time works for them."
        except Exception as e:
            logger.error(f"Voice booking failed: {e}")
            return "You could not get that time locked in. Let the lead know that time is not available and ask what other day or time works for them."

    elif tool_name == "transfer_to_agent":
        reason = args.get("reason", "lead requested transfer")
        logger.info(f"Transfer to agent requested: reason={reason}")

        # Get the transfer number from voice_config
        transfer_number = subscriber.get("voice_config", {}).get("transfer_number", "")
        if not transfer_number:
            logger.warning("Transfer requested but no transfer_number configured")
            return "Transfer is not available right now — no agent number configured. Continue the conversation and try to book an appointment instead."

        # Check if agent is available BEFORE attempting transfer.
        # For overflow calls, the agent may be on their primary call — if so,
        # log a transfer alert (frontend shows notification popup), and tell the AI
        # to stall naturally for ~30 seconds while agent gets the chance to accept.
        # If the agent doesn't accept within 30s, AI transitions to booking.
        location_id = subscriber.get("location_id", "")
        agent_email = subscriber.get("email", "")
        if location_id and agent_email:
            agent_st = agent_state_manager.get_agent_state(location_id, agent_email)
            if agent_st.get('state') == AgentState.ON_CALL:
                logger.info(f"Transfer blocked: agent {agent_email} is ON_CALL — logging alert + stall for booking")

                # Log the transfer alert for frontend notification
                if location_id not in overflow_transfer_alerts:
                    overflow_transfer_alerts[location_id] = []
                # Find the call_sid for this overflow call
                overflow_call_sid = None
                for csid, cinfo in active_calls.items():
                    if cinfo.get("contact_id") == contact_id or cinfo.get("name") == first_name:
                        overflow_call_sid = csid
                        break
                overflow_transfer_alerts[location_id].append({
                    "call_sid": overflow_call_sid or "",
                    "contact_id": contact_id or "",
                    "contact_name": first_name or "",
                    "phone": args.get("phone", ""),
                    "reason": reason,
                    "timestamp": time.time(),
                    "status": "pending",
                })
                logger.info(f"Overflow transfer alert logged for {location_id}: {first_name} wants transfer")

                return (
                    "The advisor is finishing up with another client right now. Let the lead know "
                    "that and keep chatting with them naturally for a moment — ask about their "
                    "family situation or what got them thinking about coverage today. "
                    "If the advisor doesn't become available in the next 30 seconds, go ahead and "
                    "book an appointment instead. Pull up the calendar and find a time that works."
                )

        # Signal the WebSocket bridge to perform the transfer
        for csid, cinfo in active_calls.items():
            if cinfo.get("contact_id") == contact_id or cinfo.get("name") == first_name:
                transfer_requests[csid] = {
                    "type": "transfer",
                    "target": transfer_number,
                    "reason": reason,
                }
                logger.info(f"Transfer signal set for call {csid} -> {transfer_number}")
                break
        else:
            logger.warning("Could not find active call for transfer — no matching call_sid found")
            return "The senior advisor is not available right now. Continue helping the lead directly and try to book an appointment instead."

        return f"Transfer initiated to the senior advisor. Tell the lead to hold on for just a moment while you connect them. The transfer is happening now."

    elif tool_name == "end_call":
        reason = args.get("reason", "conversation complete")
        logger.info(f"end_call tool invoked: reason={reason}")
        # The actual Twilio hangup is triggered in the bridge's response.done handler
        # (same pattern as transfer_to_agent) — returning this string lets xAI
        # generate its closing line before we hang up.
        return "Acknowledged. Ending the call now."

    else:
        logger.warning(f"Unknown voice tool: {tool_name}")
        return f"Unknown tool: {tool_name}"
