import json
import logging

from db import log_webhook_event
from ghl_calendar import consolidated_calendar_op
from voice.call_state import active_calls, transfer_requests

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
            "description": "Transfer this call to a live human agent right now. Use this when the lead is highly interested and ready to take action immediately — for example, they have their policy info ready, are asking detailed pricing questions, or explicitly want to speak with someone who can finalize things. Naturally let the caller know you are connecting them with a senior advisor before calling this.",
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
            if slots and "let me look" not in slots.lower():
                logger.info(f"Voice: Calendar slots fetched: {slots[:100]}")
                return f"Available appointment slots: {slots}"
            else:
                return "I wasn't able to check the calendar right now. Ask the lead if there's a general time that works for them and you can confirm."
        except Exception as e:
            logger.error(f"Voice calendar check failed: {e}")
            return "Calendar is temporarily unavailable. Ask the lead for their preferred time and let them know you'll confirm shortly."

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
                return f"Appointment successfully booked for {selected_time}. Confirm this to the lead and let them know they'll receive a confirmation."
            else:
                return f"That time slot ({selected_time}) wasn't available. Check calendar availability again and offer alternative times."
        except Exception as e:
            logger.error(f"Voice booking failed: {e}")
            return "Booking failed due to a technical issue. Apologize and ask the lead if you can call back to confirm."

    elif tool_name == "transfer_to_agent":
        reason = args.get("reason", "lead requested transfer")
        logger.info(f"Transfer to agent requested: reason={reason}")

        # Get the transfer number from voice_config
        transfer_number = subscriber.get("voice_config", {}).get("transfer_number", "")
        if not transfer_number:
            logger.warning("Transfer requested but no transfer_number configured")
            return "Transfer is not available right now — no agent number configured. Continue the conversation and try to book an appointment instead."

        # Signal the WebSocket bridge to perform the transfer
        # The bridge checks transfer_requests during audio relay
        location_id = subscriber.get("location_id", "")
        # Find the active call_sid for this location
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
            # Fallback: set by location_id match
            logger.warning("Could not find active call for transfer — setting global flag")

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
